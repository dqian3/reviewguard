---
name: reviewguard
description: "Toggle and configure reviewguard, which forces file edits to be approved as a diff. Use when the user types /reviewguard, or asks to turn edit review on or off, check whether it is on, or exempt files and file types from it."
---

Review-mode makes changes to chosen files land as a diff you approve rather than
a silent write. It is on by default; which files it covers is listed in rules
files that read like `.gitignore`.

It runs under Claude Code with two hooks shipped by the plugin:

- **PreToolUse** on the edit tools returns `ask` for a reviewed path, so the
  change is shown as a diff for approval. It names the line that decided. The
  covered tools are Edit / Write / MultiEdit / NotebookEdit.
- **UserPromptSubmit** does two things: it runs `/reviewguard ...` itself and
  stops there (see below), and otherwise injects one line of context while
  reviewguard is on, telling the agent to make reviewed changes with the edit
  tools and not from the shell.

Shell commands are not inspected. Blocking
rests on the agent reaching for the edit tools when asked to, which is the only
part that ever produced reviewable diffs; guessing write targets out of command
text was leaky in one direction and blocked ordinary commands in the other.

## /reviewguard runs in the hook, not through the model

The session commands are handled by the UserPromptSubmit hook, and the prompt is
blocked there, so they take effect immediately with no model turn:

```
/reviewguard status                       # is it on, and every rule in force
/reviewguard check src/main.rs paper.tex  # is this reviewed, and by which line
/reviewguard on | off                     # this session only
/reviewguard reset                        # drop this session's overrides
/reviewguard guard 'src/**'               # review src/ this session
/reviewguard allow README.md              # exempt README.md this session
/reviewguard unguard | unallow <pattern>
```

They run only from the user's prompt. There is no shell command for them, so you
cannot turn review off or change what it covers. If you are reading this skill,
the hook did *not* handle the request: the user asked in prose, or typed
something that isn't a session command. For a session change, tell the user
the `/reviewguard ...` line to type. `$ARGUMENTS` holds anything typed after
`/reviewguard`.

For a lasting change, edit the rules file directly, `<repo>/.reviewguard` or
`~/.reviewguard/rules`, and say which file you changed. That edit is itself
reviewed if the file is covered.

## The rules files

One file per scope, listing the files to **review**. Same syntax as
`.gitignore`, with the sense inverted: a line names a file to review, and a `!`
line exempts one. `#` comments and blank lines are ignored.

Three scopes, nearest first:

1. **session** — `~/.reviewguard/sessions/claude-<id>`, written by
   `/reviewguard guard`/`allow`. Swept a week after its last change.
2. **project** — `<repo>/.reviewguard`. These rules apply only to files inside
   that tree.
3. **global** — `~/.reviewguard/rules`, for rules that should hold everywhere.

**Within a file, the last matching line wins**, as in `.gitignore`. **Across
scopes, the nearest scope that matches a path decides it**; a scope that matches
nothing hands the decision outward.

That is what makes local overrides work in both directions: a repo can force
review on something the global rules exempt, not just carve exemptions.

```gitignore
# ~/.reviewguard/rules — .tex is reviewed wherever you are
**/*.tex
!/tmp/**
!**/.git/**

# <repo>/.reviewguard — this repo reviews everything but scratch,
# and scratch/paper.tex anyway
**
!scratch/
scratch/paper.tex
```

Anchoring follows `.gitignore` too: a pattern containing a slash is anchored to
the directory its file governs (the repo root, for `.reviewguard`), one without
matches at any depth, and a trailing `/` covers a directory and everything under
it. The global and session files govern no particular directory, so a relative
pattern there matches anywhere and only a leading `/` anchors it — which is how
`!/tmp/**` exempts the real `/tmp`.

**Turning review on for a repo means putting `**` in its `.reviewguard`**, not a
separate switch, then exempting files with `!`. The one thing to say when doing
it: a repo's `**` matches every file in that tree, so the *global* exemptions
stop deciding anything there and need re-adding to the repo's file if wanted.

## The on/off toggle

Separate from the rules, and per session like the permission mode. Every session
starts on; `/reviewguard off` turns it off for that session only, and does not
touch another terminal. `reset` turns it back on. With no rules files, nothing
is reviewed.

## Approving some edits and not others

A rejected tool call takes its siblings with it: if several edits go out in one
turn and the user turns down the first, the rest are cancelled too, and the user
has to ask for them again. So while reviewguard is on:

- **Send reviewed edits one per turn.** Not several in parallel, even when they
  are independent. The prompt hook says this every turn, but it is worth knowing
  why — it is the difference between "no to this one" and "no to all ten".
- **Keep each edit small enough to judge on its own.** One file, one coherent
  change. A diff that bundles two decisions can only be approved or rejected as
  a pair.
- **Two kinds of rejection.** The approval dialog has one deny option, but it
  opens a box for feedback, and that text reaches you. A bare rejection means
  stop and wait — that is what the host's own message says. A rejection that
  says "do the rest", "next" or anything else meaning carry on means: drop that
  one edit, re-send the ones that were cancelled with it, and say at the end
  what was left out. The reason line in the dialog reminds the user of this.

If the user is rejecting a lot in one file, that file is probably worth talking
about rather than editing — or worth an exemption, if they are only rejecting
because they don't want it reviewed at all.

## Notes

- `/reviewguard check <path>` answers "why was this file asked about" — it
  prints the scope, the line number and the pattern that decided. Suggest it
  before theorising about the rules.
- While reviewguard is on, make changes to reviewed files with the edit tools.
  Nothing stops a shell write, so this is the whole mechanism.
- The guard fails open: if the script errors, edits proceed normally.
