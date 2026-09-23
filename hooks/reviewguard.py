#!/usr/bin/env python3
"""reviewguard: make edits to chosen files land as a prompt, not a silent write.

Runs under Claude Code and Codex, which share a hook protocol. Two hook entry
points:

    reviewguard.py edit-hook            # an edit tool is about to run -> ask
    reviewguard.py prompt-hook          # tell the agent to use the edit tools

Either takes `--host claude` (default) or `--host codex`.

The session commands are typed by the user as `/reviewguard ...` and run inside
the prompt hook, never from a shell, so the agent cannot change them:

    /reviewguard on | off | reset | status
    /reviewguard check   <path>...      # why a path is or isn't reviewed
    /reviewguard guard   <pattern>...   # review these, this session
    /reviewguard allow   <pattern>...   # exempt these, this session
    /reviewguard unguard | unallow <pattern>...

Which files are reviewed is written in rules files that read like .gitignore,
except that a listed file is one to *review* and `!` exempts it. Three scopes,
nearest first: session, then project (<project>/.reviewguard), then global
(~/.reviewguard/rules). Within a file the last matching line wins, as in
.gitignore. The nearest scope that matches a path decides it, so a repo can
force review on something the global rules exempt, not just carve exemptions.

The on/off toggle is separate and per session, like the permission mode. Every
session starts on.
"""

import io
import json
import os
import re
import shlex
import sys
import time

# --------------------------------------------------------------------------
# where things live

HOME = os.path.expanduser("~")
ROOT = os.path.join(HOME, ".reviewguard")
GLOBAL_RULES = os.path.join(ROOT, "rules")
SESSION_DIR = os.path.join(ROOT, "sessions")
PROJECT_RULES = ".reviewguard"
SESSION_TTL = 7 * 86400  # session files outlive their session; sweep the strays

DEFAULT_ON = True
SCOPE_NAMES = {"session": "this session", "project": "this repo", "global": "global"}
SCOPES = ("session", "project", "global")


# --------------------------------------------------------------------------
# hosts

HOSTS = {
    "claude": {
        "name": "Claude Code",
        "session_env": "CLAUDE_CODE_SESSION_ID",
        "project_env": "CLAUDE_PROJECT_DIR",
        "edit_tools": {"Edit", "Write", "MultiEdit", "NotebookEdit"},
        "edit_with": "the Edit or Write tool",
        "shell_tool": "Bash",
    },
    "codex": {
        "name": "Codex",
        "session_env": "CODEX_SESSION_ID",
        "project_env": "CODEX_PROJECT_DIR",
        # Codex edits through apply_patch, which also arrives as a shell
        # heredoc; the patch body is read for paths either way.
        "edit_tools": {"apply_patch", "shell", "local_shell", "unified_exec"},
        "edit_with": "apply_patch",
        "shell_tool": "shell",
    },
}

HOST = "claude"


def host():
    return HOSTS[HOST]


# --------------------------------------------------------------------------
# rules files


def read_file(path):
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return None


def write_file(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(text if text.endswith("\n") else text + "\n")


def rules_text(scope, root, sid):
    """(text, path) for one scope."""
    if scope == "global":
        return read_file(GLOBAL_RULES), GLOBAL_RULES
    if scope == "project":
        path = os.path.join(root, PROJECT_RULES)
        return read_file(path), path
    path = session_path(sid) if sid else None
    return (read_file(path) if path else None), path


# --------------------------------------------------------------------------
# matching, .gitignore style


class Rule:
    __slots__ = ("pattern", "negated", "regex", "line")

    def __init__(self, pattern, negated, regex, line):
        self.pattern = pattern
        self.negated = negated
        self.regex = regex
        self.line = line

    def __str__(self):
        return ("!" if self.negated else "") + self.pattern


def translate(pat):
    """A .gitignore pattern body as a regex fragment over one path."""
    out, i, n = [], 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if pat[i : i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
            elif pat[i : i + 2] == "**":
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and pat[j] in "!^":
                j += 1
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape(c))
                i += 1
            else:
                body = pat[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body + "]")
                i = j + 1
        elif c == "\\" and i + 1 < n:
            out.append(re.escape(pat[i + 1]))
            i += 2
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def compile_rule(pat, base):
    """A pattern as a regex over absolute paths.

    Anchoring follows .gitignore: a pattern with a slash in it is anchored to
    the directory its rules file governs, one without matches at any depth. The
    global and session files govern no particular directory, so a relative
    pattern there matches anywhere and only a leading / anchors it — to the
    filesystem root, which is how /tmp/** exempts the real /tmp.
    """
    dir_only = pat.endswith("/")
    core = pat[:-1] if dir_only else pat
    absolute = core.startswith("/")
    if absolute:
        core = core[1:]
    anchored = absolute or "/" in core.rstrip("/")

    body = translate(core)
    if base:
        head = re.escape(base.rstrip("/")) + "/"
        if not anchored:
            head += "(?:.*/)?"
    elif absolute:
        head = "/"
    else:
        head = "(?:.*/)?"
    # A pattern that names a directory covers everything under it.
    return re.compile(head + body + "(?:/.*)?$")


def parse_rules(text, base):
    """(rules, toggle) from one rules file. Only the session file's toggle is used."""
    rules, toggle = [], None
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = re.match(r"#!\s*(on|off)\b", line)
            if m:
                toggle = m.group(1) == "on"
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:].strip()
        elif line.startswith("\\!"):
            line = line[1:]
        if not line:
            continue
        try:
            rules.append(Rule(line, negated, compile_rule(line, base), lineno))
        except re.error:
            continue  # a broken pattern must not take the whole file down
    return rules, toggle


def last_match(path, rules):
    """The last rule that matches, as .gitignore resolves a file."""
    hit = None
    for rule in rules:
        if rule.regex.match(path):
            hit = rule
    return hit


# --------------------------------------------------------------------------
# scopes


def project_root(hook_input=None):
    root = os.environ.get(host()["project_env"]) or os.environ.get("CLAUDE_PROJECT_DIR")
    if not root and hook_input:
        root = hook_input.get("cwd")
    return os.path.abspath(root or os.getcwd())


def session_id(payload=None):
    if payload and payload.get("session_id"):
        return str(payload["session_id"])
    return os.environ.get(host()["session_env"]) or None


def session_path(sid):
    return os.path.join(SESSION_DIR, "%s-%s" % (HOST, sid))


def sweep_sessions():
    cutoff = time.time() - SESSION_TTL
    try:
        for name in os.listdir(SESSION_DIR):
            path = os.path.join(SESSION_DIR, name)
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
    except OSError:
        pass


def settings(root, sid=None):
    """The three scopes of rules, plus the toggle and what decided it."""
    layers, session_toggle = [], None
    for scope in SCOPES:
        text, path = rules_text(scope, root, sid)
        base = root if scope == "project" else None
        rules, toggle = parse_rules(text, base)
        if scope == "session":
            session_toggle = toggle
        layers.append({"scope": scope, "rules": rules, "path": path})

    if session_toggle is not None:
        enabled, source = session_toggle, "this session"
    else:
        enabled, source = DEFAULT_ON, "the default"

    return {"enabled": enabled, "source": source, "layers": layers}


def under(path, root):
    root = os.path.abspath(root).rstrip("/")
    return path == root or path.startswith(root + "/")


def absolute(path, root):
    return os.path.abspath(path if os.path.isabs(path) else os.path.join(root, path))


def decide(path, cfg, root):
    """(reviewed, scope, rule) — the nearest scope matching this path wins."""
    if not path or not cfg["enabled"]:
        return False, None, None
    ap = absolute(path, root)
    for layer in cfg["layers"]:
        if layer["scope"] == "project" and not under(ap, root):
            continue  # a repo's rules stop at its own tree
        hit = last_match(ap, layer["rules"])
        if hit:
            return not hit.negated, layer["scope"], hit
    return False, None, None


# --------------------------------------------------------------------------
# what an edit is about to touch

# apply_patch names its files in the patch body, whether it arrives as a tool
# call of its own or as a heredoc inside a shell command.
PATCH_PATHS = re.compile(
    r"^\*\*\* (?:Add|Update|Delete) File:\s*(.+?)\s*$|^\*\*\* Move to:\s*(.+?)\s*$",
    re.M,
)
PATH_KEYS = ("file_path", "notebook_path", "path")


def target_paths(tool_input):
    """Every file this tool call would write."""
    paths = []
    if isinstance(tool_input, dict):
        for key in PATH_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                paths.append(value)
    if isinstance(tool_input, str):
        blob = tool_input
    else:
        try:
            blob = json.dumps(tool_input)
        except Exception:
            blob = str(tool_input)
    # The patch body is a JSON string by the time it reaches us.
    blob = blob.replace("\\n", "\n")
    for m in PATCH_PATHS.finditer(blob):
        paths.append((m.group(1) or m.group(2)).strip())

    seen, out = set(), []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


# --------------------------------------------------------------------------
# hooks


def edit_hook(data):
    if data.get("tool_name") not in host()["edit_tools"]:
        return
    root = project_root(data)
    cfg = settings(root, session_id(data))
    for path in target_paths(data.get("tool_input")):
        on, scope, rule = decide(path, cfg, root)
        if not on:
            continue
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "ask",
                        "permissionDecisionReason": (
                            f"reviewguard is on and {os.path.basename(path)} is "
                            f"reviewed by `{rule}` ({SCOPE_NAMES[scope]}). Show this "
                            f"diff for approval. Rejecting ends the turn and skips "
                            f"any edits queued behind this one — reject with \"do "
                            f"the rest\" to keep them. Exempt this file with "
                            f"/reviewguard allow <pattern>, or /reviewguard off."
                        ),
                    }
                }
            )
        )
        sys.exit(0)


# Shell commands are not inspected, beyond reading a patch one carries. Asking
# the agent to reach for the edit tools is what keeps changes reviewable; a
# scanner that guessed at write targets from command text was both leaky and
# prone to blocking ordinary commands.
GUIDANCE = (
    "reviewguard is on for this session. Make every change to a reviewed file "
    "with {edit_with}, so it is shown as a diff for approval. Do not write "
    "reviewed files from {shell_tool} — no `>`/`>>` redirects, `sed -i`, "
    "`perl -pi`, `tee`, `cp`/`mv` over one, or a script that opens one for "
    "writing. Reading them with cat/grep/sed -n is fine, and so is git. "
    "Send reviewed edits one per turn and keep each one small: several in one "
    "turn are cancelled together the moment any of them is rejected. A "
    "rejection is a note about that one edit: if the user rejects with \"do the "
    "rest\", \"next\" or anything else meaning carry on, re-send the edits that "
    "were skipped alongside it, one per turn, and say at the end what you left "
    "out. A bare rejection with no such instruction means stop and wait. "
    "Rules, nearest scope first — the nearest scope matching a path decides "
    "it, and within a scope the last matching line wins: {rules}"
)


def rules_sentence(cfg):
    parts = []
    for layer in cfg["layers"]:
        if not layer["rules"]:
            continue
        review = [str(r) for r in layer["rules"] if not r.negated]
        exempt = [r.pattern for r in layer["rules"] if r.negated]
        part = f"{SCOPE_NAMES[layer['scope']]} — review {', '.join(review) or '(nothing)'}"
        if exempt:
            part += f", except {', '.join(exempt)}"
        parts.append(part)
    return "; ".join(parts) or "no rules set, so nothing is reviewed."


# Commands run inside the hook.
SESSION_COMMANDS = {
    "on", "off", "reset", "status", "check",
    "guard", "allow", "unguard", "unallow",
}


def fast_path(data):
    """Run `/reviewguard ...` here, so the toggle lands without a model turn."""
    m = re.match(r"^/?review[- ]?guard\b(.*)$", (data.get("prompt") or "").strip(), re.I)
    if not m:
        return None
    try:
        argv = shlex.split(m.group(1).strip()) or ["status"]
    except ValueError:
        return None
    if argv[0] not in SESSION_COMMANDS:
        return None

    sid = session_id(data)
    if not sid:
        return f"reviewguard: no {host()['name']} session id; nothing changed"

    out = io.StringIO()
    real, sys.stdout = sys.stdout, out
    try:
        session_command(argv, sid, project_root(data))
    except SystemExit:
        pass
    except Exception as exc:
        return f"reviewguard: {exc}"
    finally:
        sys.stdout = real
    return out.getvalue().strip() or "done"


def prompt_hook(data):
    done = fast_path(data)
    if done is not None:
        # Blocking stops the prompt here: the result is shown and no model runs.
        print(json.dumps({"decision": "block", "reason": done}))
        return

    root = project_root(data)
    cfg = settings(root, session_id(data))
    if not cfg["enabled"]:
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": GUIDANCE.format(
                        edit_with=host()["edit_with"],
                        shell_tool=host()["shell_tool"],
                        rules=rules_sentence(cfg),
                    ),
                }
            }
        )
    )


# --------------------------------------------------------------------------
# CLI


def set_toggle(path, on):
    """Set the `#! on` / `#! off` line in a rules file, adding it if absent."""
    text = read_file(path)
    directive = "#! %s" % ("on" if on else "off")
    if text is None:
        write_file(path, directive)
        return
    lines, replaced = text.splitlines(), False
    for i, line in enumerate(lines):
        if re.match(r"#!\s*(on|off)\b", line.strip()):
            lines[i], replaced = directive, True
            break
    if not replaced:
        lines.insert(0, directive)
    write_file(path, "\n".join(lines))


def toggle(sid, on):
    set_toggle(session_path(sid), on)
    sweep_sessions()
    print(f"reviewguard {'on' if on else 'off'} for this session")


def status(root, sid):
    cfg = settings(root, sid)

    print(f"reviewguard: {'ON' if cfg['enabled'] else 'OFF'}  (from {cfg['source']})")
    print("  rules, nearest scope first; the nearest one matching a path decides,")
    print("  and within a scope the last matching line wins:")
    for layer in cfg["layers"]:
        name = SCOPE_NAMES[layer["scope"]]
        if not layer["rules"]:
            print(f"    {name:12s} (no rules)  {layer['path'] or ''}")
            continue
        print(f"    {name:12s} {layer['path']}")
        for rule in layer["rules"]:
            print(f"    {'':12s}   {rule}")
    print()
    print(f"  session:  {sid[:8]}  {session_path(sid)}")


def check(paths, root, sid):
    """Say whether each path is reviewed, and which line decided it."""
    cfg = settings(root, sid)
    if not cfg["enabled"]:
        print("reviewguard is OFF for this session; nothing is reviewed.")
        print("(showing what would happen with it on)\n")
        cfg = {**cfg, "enabled": True}
    for path in paths:
        on, scope, rule = decide(path, cfg, root)
        if rule is None:
            print(f"{path}: no review  (no line matches)")
        else:
            verdict = "REVIEW" if on else "no review"
            print(f"{path}: {verdict}  ({SCOPE_NAMES[scope]}, line {rule.line}: {rule})")


def edit_patterns(cmd, patterns, sid):
    """Add or remove pattern lines in this session's rules."""
    removing = cmd.startswith("un")
    negated = cmd.endswith("allow")
    path = session_path(sid)
    lines = (read_file(path) or "").splitlines()

    wanted = [("!" + p if negated else p) for p in patterns]
    changed, skipped = [], []
    for line in wanted:
        present = line in [l.strip() for l in lines]
        if removing and present:
            lines = [l for l in lines if l.strip() != line]
            changed.append(line)
        elif not removing and not present:
            lines.append(line)
            changed.append(line)
        else:
            skipped.append(line)

    if changed:
        write_file(path, "\n".join(lines))
        print(f"{'-' if removing else '+'} {', '.join(changed)}  ({path})")
    if skipped:
        state = "not in" if removing else "already in"
        print(f"{', '.join(skipped)} {state} {path}")

    rules, _ = parse_rules("\n".join(lines), None)
    print(f"  now: {', '.join(str(r) for r in rules) or '(empty)'}")

    if not removing and not negated and any(p in ("**", "/**") for p in patterns):
        print(
            "  note: ** matches every file, so the repo and global rules — "
            "including their exemptions — no longer decide anything this "
            "session. Re-add the ones you want with `/reviewguard allow <pattern>`."
        )


def session_command(argv, sid, root):
    """One `/reviewguard ...` command, applied to session `sid`."""
    cmd = argv[0]

    if cmd in ("on", "off"):
        toggle(sid, cmd == "on")
        return

    if cmd == "reset":
        try:
            os.remove(session_path(sid))
            gone = True
        except OSError:
            gone = False
        cfg = settings(root, sid)
        print(
            f"session override {'cleared' if gone else 'was already unset'}; "
            f"reviewguard is {'ON' if cfg['enabled'] else 'OFF'} from {cfg['source']}"
        )
        return

    if cmd == "status":
        status(root, sid)
        return

    if cmd == "check":
        paths = argv[1:]
        if not paths:
            print("usage: /reviewguard check <path>...")
            return
        check(paths, root, sid)
        return

    patterns = argv[1:]
    if not patterns:
        print(f"usage: /reviewguard {cmd} <pattern>...")
        return
    edit_patterns(cmd, patterns, sid)


ENTRY_POINTS = {
    "edit-hook": edit_hook,
    "prompt-hook": prompt_hook,
}


def main():
    global HOST
    argv = sys.argv[1:]
    if "--host" in argv:
        i = argv.index("--host")
        if i + 1 < len(argv) and argv[i + 1] in HOSTS:
            HOST = argv[i + 1]
        del argv[i : i + 2]

    if not argv or argv[0] not in ENTRY_POINTS:
        print(__doc__)
        sys.exit(1)
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    try:
        ENTRY_POINTS[argv[0]](payload)
    except SystemExit:
        raise
    except Exception:
        pass  # a broken guard must never block ordinary work
    sys.exit(0)


main()
