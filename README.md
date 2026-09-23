# reviewguard

A small plugin for Claude Code that ensures edits to chosen files are reviewed before they are written. Lets you use 
auto-mode for tasks such as writing and running tests, while keeping track of changes to important files.

## Install

```
/plugin marketplace add dqian3/reviewguard
/plugin install reviewguard@reviewguard
```

## Usage

Add a `.reviewguard` file to your repo. Edits to any file it matches need your approval; everything else follows your
normal permission mode.

`.reviewguard` uses the same [syntax](https://git-scm.com/docs/gitignore) as `.gitignore`. For example:

```
# Review any human facing documentation
*.tex
README.md

# Allow edits under this repo's scratch/ dir. (! exempts a path)
!/scratch/
```

As with `.gitignore`, a `.reviewguard` can also go in a subdirectory, and one closer to a file takes priority. Each
applies only to files under its own directory, and a leading `/` means that directory. In a git repo, files above the
repo root are ignored. Rules for paths anywhere else go
in `~/.reviewguard/rules`, where a leading `/` means the real filesystem root:

```
# Review .tex files everywhere, except under /tmp
**/*.tex
!/tmp/**
```

### Session overrides

Within a session, `/reviewguard` changes rules for that session only, leaving the files alone:

```
/reviewguard off                          # stop reviewing for now
/reviewguard on
/reviewguard guard 'src/**'               # also review src/ this session
/reviewguard allow README.md              # stop reviewing README.md this session
/reviewguard reset                        # drop this session's overrides
```

Session rules take priority over the repo's `.reviewguard`, which takes priority over the global rules. `/reviewguard status` shows what is in force.

## Caveats

Agents can get around the guard by writing files through the shell (e.g. `sed -i`). The plugin tells the agent not to do
this, but cannot enforce it.
