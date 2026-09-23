"""End-to-end tests: run the hook script as Claude Code would, in a fake home.

    python3 -m unittest discover tests
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "hooks", "reviewguard.py")


class Env(unittest.TestCase):
    """A temp dir holding a fake home with one git repo in it."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = self.mkdir("home")
        self.repo = self.mkdir("home/repo/.git")[: -len("/.git")]
        self.sid = "s1"

    # -- setup helpers

    def mkdir(self, rel):
        path = os.path.join(self.tmp, rel)
        os.makedirs(path, exist_ok=True)
        return path

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def rules(self, directory, *lines):
        self.write(os.path.join(directory, ".reviewguard"), "\n".join(lines) + "\n")

    def global_rules(self, *lines):
        self.write(os.path.join(self.home, ".reviewguard", "rules"), "\n".join(lines) + "\n")

    def session_file(self, sid=None):
        return os.path.join(self.home, ".reviewguard", "sessions", "claude-" + (sid or self.sid))

    # -- running the hooks

    def run_hook(self, entry, payload, project=None):
        env = dict(os.environ, HOME=self.home, CLAUDE_PROJECT_DIR=project or self.repo)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        proc = subprocess.run(
            [sys.executable, SCRIPT, entry], input=stdin, capture_output=True, text=True
            , env=env
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    def edit(self, path, tool="Edit", sid=None, project=None):
        """True if the edit hook asks for review."""
        key = "notebook_path" if tool == "NotebookEdit" else "file_path"
        out = self.run_hook(
            "edit-hook",
            {"session_id": sid or self.sid, "tool_name": tool, "tool_input": {key: path}},
            project,
        )
        if out is None:
            return False
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        return True

    def prompt(self, text, sid=None, project=None):
        return self.run_hook("prompt-hook", {"session_id": sid or self.sid, "prompt": text}, project)

    def command(self, text, sid=None):
        """Run a /reviewguard command; return what it printed."""
        out = self.prompt(text, sid)
        self.assertEqual(out["decision"], "block")
        return out["reason"]

    def assertReviewed(self, rel, **kw):
        self.assertTrue(self.edit(os.path.join(self.repo, rel), **kw), rel)

    def assertNotReviewed(self, rel, **kw):
        self.assertFalse(self.edit(os.path.join(self.repo, rel), **kw), rel)


class Patterns(Env):
    def test_unanchored_matches_at_any_depth(self):
        self.rules(self.repo, "*.md")
        self.assertReviewed("README.md")
        self.assertReviewed("a/b/c.md")
        self.assertNotReviewed("a/b/c.py")

    def test_slash_anchors_to_the_rules_file(self):
        self.rules(self.repo, "/docs/*.md")
        self.assertReviewed("docs/a.md")
        self.assertNotReviewed("docs/sub/a.md")
        self.assertNotReviewed("other/docs/a.md")

    def test_last_matching_line_wins(self):
        self.rules(self.repo, "*.md", "!notes/", "notes/keep.md")
        self.assertReviewed("a.md")
        self.assertNotReviewed("notes/a.md")
        self.assertReviewed("notes/keep.md")

    def test_trailing_slash_matches_only_a_directory(self):
        self.rules(self.repo, "build/")
        self.assertReviewed("build/out.txt")
        self.assertReviewed("sub/build/out.txt")
        self.assertNotReviewed("build")

    def test_double_star(self):
        self.rules(self.repo, "a/**/b", "c/**")
        self.assertReviewed("a/b")
        self.assertReviewed("a/x/y/b")
        self.assertReviewed("c/x/y")
        self.assertNotReviewed("c")

    def test_double_star_inside_a_name_is_a_single_star(self):
        self.rules(self.repo, "/foo**bar")
        self.assertReviewed("fooxbar")
        self.assertNotReviewed("foo/x/bar")

    def test_everything(self):
        self.rules(self.repo, "**")
        self.assertReviewed("any/file.py")

    def test_relative_path_resolves_against_the_project(self):
        self.rules(self.repo, "*.md")
        self.assertTrue(self.edit("docs/a.md"))

    def test_notebook_edits_are_checked(self):
        self.rules(self.repo, "*.ipynb")
        self.assertReviewed("nb.ipynb", tool="NotebookEdit")


class Scopes(Env):
    def test_no_rules_means_no_review(self):
        self.assertNotReviewed("README.md")

    def test_global_leading_slash_is_the_filesystem_root(self):
        scratch = self.mkdir("scratch")
        self.global_rules("*.tex", "!%s/**" % scratch)
        self.assertReviewed("paper.tex")
        self.assertFalse(self.edit(os.path.join(scratch, "paper.tex")))

    def test_repo_overrides_global_both_ways(self):
        self.global_rules("*.tex", "!*.md")
        self.rules(self.repo, "*.md", "!draft.tex")
        self.assertReviewed("a.md")
        self.assertNotReviewed("draft.tex")
        self.assertReviewed("paper.tex")  # repo has no line for it; global decides

    def test_repo_rules_do_not_reach_outside_it(self):
        self.rules(self.repo, "**")
        self.assertFalse(self.edit(os.path.join(self.home, "elsewhere", "a.md")))

    def test_session_overrides_repo(self):
        self.rules(self.repo, "*.md")
        self.command("/reviewguard allow README.md")
        self.command("/reviewguard guard '*.py'")
        self.assertNotReviewed("README.md")
        self.assertReviewed("a.md")
        self.assertReviewed("main.py")
        self.command("/reviewguard unallow README.md")
        self.assertReviewed("README.md")


class NestedRules(Env):
    def test_deeper_file_wins(self):
        self.rules(self.repo, "*.md")
        self.rules(os.path.join(self.repo, "docs"), "!drafts/", "!*.md", "keep.md")
        self.assertReviewed("README.md")
        self.assertNotReviewed("docs/a.md")
        self.assertReviewed("docs/keep.md")
        self.assertReviewed("docs/drafts/keep.md")

    def test_subdir_file_only_covers_its_own_tree(self):
        self.rules(os.path.join(self.repo, "docs"), "*.md")
        self.assertReviewed("docs/a.md")
        self.assertNotReviewed("src/a.md")

    def test_subdir_leading_slash_is_that_directory(self):
        self.rules(os.path.join(self.repo, "docs"), "/a.md")
        self.assertReviewed("docs/a.md")
        self.assertNotReviewed("a.md")
        self.assertNotReviewed("docs/sub/a.md")

    def test_stops_at_repo_root(self):
        self.rules(self.home, "**")  # above the repo: ignored
        self.assertNotReviewed("a.md")

    def test_stops_at_home(self):
        self.rules(self.tmp, "**")  # above home: ignored
        self.assertFalse(self.edit(os.path.join(self.home, "loose", "a.md")))

    def test_outside_home_and_repos(self):
        outside = self.mkdir("outside")
        self.rules(outside, "*.txt")
        self.assertTrue(self.edit(os.path.join(outside, "x", "y.txt")))

    def test_edit_in_another_repo_uses_its_rules(self):
        other = self.mkdir("home/other/.git")[: -len("/.git")]
        self.rules(other, "*.md")
        self.assertTrue(self.edit(os.path.join(other, "a.md")))
        self.assertNotReviewed("a.md")

    def test_launched_from_a_subdirectory(self):
        self.rules(self.repo, "*.md")
        self.assertReviewed("README.md", project=os.path.join(self.repo, "src"))

    def test_status_lists_each_file_found(self):
        self.rules(self.repo, "*.md")
        self.rules(os.path.join(self.repo, "docs"), "!*.md")
        out = self.prompt("/reviewguard status", project=os.path.join(self.repo, "docs"))
        reason = out["reason"]
        inner = reason.index(os.path.join(self.repo, "docs", ".reviewguard"))
        outer = reason.index(os.path.join(self.repo, ".reviewguard"))
        self.assertLess(inner, outer)

    def test_status_says_when_none_found(self):
        self.assertIn("no .reviewguard", self.command("/reviewguard status"))


class Toggle(Env):
    def test_off_and_on(self):
        self.rules(self.repo, "*.md")
        self.command("/reviewguard off")
        self.assertNotReviewed("a.md")
        self.command("/reviewguard on")
        self.assertReviewed("a.md")

    def test_off_is_per_session(self):
        self.rules(self.repo, "*.md")
        self.command("/reviewguard off", sid="s1")
        self.assertNotReviewed("a.md", sid="s1")
        self.assertReviewed("a.md", sid="s2")

    def test_reset_drops_overrides(self):
        self.rules(self.repo, "*.md")
        self.command("/reviewguard off")
        self.command("/reviewguard allow a.md")
        self.command("/reviewguard reset")
        self.assertReviewed("a.md")
        self.assertFalse(os.path.exists(self.session_file()))

    def test_check_names_the_deciding_line(self):
        self.rules(self.repo, "*.md", "!notes/")
        out = self.command("/reviewguard check a.md notes/b.md c.py")
        self.assertIn("a.md: REVIEW", out)
        self.assertIn("line 1: *.md", out)
        self.assertIn("notes/b.md: no review", out)
        self.assertIn("c.py: no review  (no line matches)", out)


class PromptHook(Env):
    def test_guidance_lists_rules(self):
        self.rules(self.repo, "*.md")
        ctx = self.prompt("hello")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("review *.md", ctx)

    def test_no_guidance_without_rules(self):
        self.assertIsNone(self.prompt("hello"))

    def test_no_guidance_when_off(self):
        self.rules(self.repo, "*.md")
        self.command("/reviewguard off")
        self.assertIsNone(self.prompt("hello"))

    def test_prose_is_not_a_command(self):
        self.rules(self.repo, "*.md")
        out = self.prompt("reviewguard off please")
        self.assertNotIn("decision", out)
        self.assertReviewed("a.md")

    def test_unknown_subcommand_goes_to_the_model(self):
        self.rules(self.repo, "*.md")
        self.assertNotIn("decision", self.prompt("/reviewguard what is this"))

    def test_multiline_prompt_is_not_a_command(self):
        self.rules(self.repo, "*.md")
        self.assertNotIn("decision", self.prompt("/reviewguard off\nand then more"))


class SessionCleanup(Env):
    def age(self, path, days):
        t = time.time() - days * 86400
        os.utime(path, (t, t))

    def test_idle_sessions_are_swept_and_used_ones_kept(self):
        self.command("/reviewguard allow a", sid="live")
        self.command("/reviewguard allow b", sid="idle")
        self.age(self.session_file("live"), 10)
        self.age(self.session_file("idle"), 10)
        self.prompt("hello", sid="live")
        self.command("/reviewguard status", sid="other")
        self.assertTrue(os.path.exists(self.session_file("live")))
        self.assertFalse(os.path.exists(self.session_file("idle")))

    def test_recent_sessions_are_kept(self):
        self.command("/reviewguard allow a", sid="recent")
        self.age(self.session_file("recent"), 3)
        self.command("/reviewguard status", sid="other")
        self.assertTrue(os.path.exists(self.session_file("recent")))


class FailsOpen(Env):
    def test_other_tools_are_ignored(self):
        self.rules(self.repo, "**")
        out = self.run_hook(
            "edit-hook",
            {"session_id": self.sid, "tool_name": "Bash", "tool_input": {"command": "ls"}},
        )
        self.assertIsNone(out)

    def test_bad_input_allows(self):
        self.rules(self.repo, "**")
        self.assertIsNone(self.run_hook("edit-hook", "not json"))

    def test_broken_pattern_is_skipped(self):
        self.rules(self.repo, "[z-a]", "*.md")
        self.assertReviewed("a.md")


if __name__ == "__main__":
    unittest.main()
