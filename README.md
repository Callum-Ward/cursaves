# cursaves

Cursor stores chat history locally. Switch machines and it's gone. This tool checkpoints conversations to a git repo so you can restore them anywhere.

## Quick Start

```bash
# Install globally (once per machine)
uv tool install git+https://github.com/Callum-Ward/cursaves.git

# Initialize the sync repo (once per machine)
cursaves init --remote git@github.com:you/my-cursaves.git
```

Then from any project directory:

```bash
# Save all conversations and push to remote
cursaves push

# On another machine: pull and restore conversations
cursaves pull
```

For SSH remote projects, Cursor stores chats on your local machine. Use `-w` to target a workspace:

```bash
# See all workspaces (local + SSH remote)
cursaves workspaces

# Push/pull a specific workspace by number
cursaves push -w 3
```

`push` checkpoints your conversations, commits, and pushes to git. `pull` fetches from git and imports into Cursor's database.

### Example

```
$ cursaves push

Checkpointing conversations for /Users/you/Projects/my-app...
  3 conversation(s) checkpointed
  Committed
  Pushing... done

Done. 3 conversation(s) saved and pushed.
```

```
$ cursaves list

Conversations for /Users/you/Projects/my-app

ID                                       Name                           Mode      Msgs  Last Updated
--------------------------------------------------------------------------------------------------------------
fda95e1a-7d3a-4113-942f-7e033e454bef     Project structure and iss...   agent     1203  2026-01-19 20:11 UTC
cadfb263-3326-4aff-8887-dcc12f736b11     Feedback on documentation...   agent      595  2025-12-15 12:36 UTC
76b5729a-375a-4e07-ba38-d58b322c85fc     Adjust layout for better ...   agent      317  2025-10-02 11:19 UTC

3 conversation(s) total
```

## Installation

**Requirements:** Python 3.10+, [uv](https://docs.astral.sh/uv/), macOS or Linux, Git. Zero external Python dependencies.

### Install as a global CLI tool (recommended)

```bash
uv tool install git+https://github.com/Callum-Ward/cursaves.git
```

This puts `cursaves` on your PATH so you can run it from any directory. Run this on each machine you want to sync between.

If `~/.local/bin` is not on your PATH, run `uv tool update-shell` or add it manually.

### Update

```bash
uv tool upgrade cursaves
```

### Alternative: clone and run locally

```bash
git clone git@github.com:Callum-Ward/cursaves.git
cd cursaves
uv sync
uv run cursaves <command>

# Or without uv:
python -m cursor_saves <command>
```

## Setup

`cursaves` stores conversation snapshots in a local git repo at `~/.cursaves/`. To sync between machines, you point this at a remote repository.

### 1. Create a private repo for your checkpoints

Go to GitHub (or GitLab, etc.) and create a **new private repository**. This is where your conversation data will be stored -- keep it private since snapshots contain your full chat history, file paths, and machine info.

For example: `github.com/you/cursaves-data` (private).

Don't add a README or any files -- leave it completely empty.

### 2. Initialize on each machine

```bash
cursaves init --remote git@github.com:you/cursaves-data.git
```

This creates `~/.cursaves/` with a git repo, a `snapshots/` directory, and the remote configured. Run this once on every machine you want to sync between.

If you only want local checkpoints (no syncing), just run `cursaves init` without `--remote`. You can add a remote later with `cd ~/.cursaves && git remote add origin <url>`.

### 3. Start syncing

```bash
# From any project directory:
cursaves push    # checkpoint + commit + push
cursaves pull    # pull + import (close Cursor first)
```

The first `push` will create the initial commit on the remote. After that, `push` and `pull` keep everything in sync.

## Commands

All commands default to the current working directory as the project path. Use `-w <number>` to target a workspace by number (from `cursaves workspaces`), or `-p /path` to specify a path directly.

| Command | Description | Modifies Cursor data? |
|---------|-------------|----------------------|
| **`push`** | **Checkpoint + commit + push (the main command)** | No |
| **`pull`** | **Git pull + import snapshots** | Yes |
| `init` | Initialize the sync repo at ~/.cursaves/ | No |
| `workspaces` | List all Cursor workspaces (local + SSH remote) | No |
| `list` | Show conversations for a project | No |
| `status` | Compare local conversations vs snapshots | No |
| `export <id>` | Export one conversation to a snapshot | No |
| `checkpoint` | Export all conversations (no git) | No |
| `import --all` | Import snapshots (no git) | Yes |
| `watch` | Auto-checkpoint and sync in the background | No (reads only) |

Most of the time you only need `push` and `pull`. The other commands are there for finer-grained control.

### Auto-sync with `watch`

```bash
# Run in a terminal on each machine -- handles everything automatically
cursaves watch -p /path/to/your/project

# Options
cursaves watch --interval 30     # check every 30s (default: 60)
cursaves watch --no-git          # checkpoint only, no git push/pull
cursaves watch --verbose         # log every check, not just changes
```

The watch daemon polls for database changes, auto-checkpoints when conversations update, and commits + pushes to git. On the other end, it pulls and picks up new snapshots.

## How Cursor Stores Chat Data

Cursor stores conversations in two local SQLite databases, not as files you can easily copy:

- **Workspace DB** (`workspaceStorage/{id}/state.vscdb`): A list of conversation IDs and sidebar metadata for each project. This is what populates the chat list in the sidebar.
- **Global DB** (`globalStorage/state.vscdb`): The actual conversation content -- one JSON blob per conversation, keyed by `composerData:{UUID}`.

Data locations:
- macOS: `~/Library/Application Support/Cursor/User/`
- Linux: `~/.config/Cursor/User/`

Notably, **chat data is always stored on the machine running Cursor's UI**, even when connected to a remote host via SSH. This is why switching machines means losing your conversation context.

For more details, see [docs/how-cursor-stores-chats.md](docs/how-cursor-stores-chats.md).

## Cross-Platform Support

### Project identity

Projects are identified by their **git remote origin URL**, not the local directory name. This means:

- `~/Projects/bob` and `~/repos/alice` with the same `origin` are treated as the same project -- conversations sync between them.
- Two unrelated repos both named `myapp` won't collide, because their remotes differ.
- Non-git directories fall back to matching by directory name.

You can see what identity is being used with `cursaves status`.

### Path rewriting

When importing conversations on a different machine, absolute file paths in conversation metadata (e.g., which files were attached as context) are automatically rewritten to match the target project path. The actual conversation content -- your messages and AI responses -- is fully portable with no modification.

For example, a conversation started on macOS at `/Users/you/Projects/myapp` will have its file references rewritten to `/home/you/repos/myapp` when imported on a Linux machine.

## Safety

- **Read operations** (`list`, `export`, `checkpoint`, `status`, `watch`) work on a temporary copy of the database. They never touch Cursor's files and are safe to run while Cursor is open.
- **Write operations** (`import`, `pull`) back up the target database before writing, and refuse to run while Cursor is detected as running. Use `--force` to override (not recommended).
- Snapshots are self-contained JSON -- even if import goes wrong, you always have the raw data and the backup.

## Privacy Warning

Snapshot files contain your **full conversation data**: your prompts, AI responses, file paths from your machine, your machine's hostname, and timestamps.

**Use a private repository** for the `~/.cursaves/` remote. Do not push conversation snapshots to a public repo.

## Typical Workflows

### Local projects

```bash
# On Machine A -- before switching, from your project directory:
cursaves push

# On Machine B -- after switching, from your project directory:
cursaves pull
# Reload Cursor window (Cmd+Shift+P -> 'Reload Window')
```

### SSH remote projects

When you connect to a VM via SSH in Cursor, chat data is stored **on your local machine**, not on the VM. This means `cursaves` must run locally, not on the VM.

Use `cursaves workspaces` to see your SSH workspaces, then reference them by number:

```bash
# See all workspaces (local and SSH remote):
cursaves workspaces
#  #    Type   Path                              Host
#  1    ssh    /home/user/repos/my-project        my-vm
#  2    local  /Users/me/Projects/other-app
#  ...

# Push conversations from an SSH workspace:
cursaves push -w 1

# On another machine, pull them:
cursaves pull -w 1
```

Run these commands in a **regular terminal** on your local machine (not in Cursor's integrated terminal, which runs on the VM).

### Automatic sync

```bash
# Run on each machine -- handles everything in the background:
cursaves watch -p /path/to/your/project
```

The daemon handles checkpoint + git push/pull automatically. When you switch machines, conversations are already synced.

## Architecture

```
~/.cursaves/                   # Sync repo (git, private remote)
  snapshots/
    github.com-user-repo/      # Identified by git remote URL
      <composer-id>.json       # Self-contained conversation snapshot

~/.local/bin/cursaves          # Global CLI tool (installed via uv)

cursaves/                      # Source repo (this repo, public)
  cursor_saves/                # Python package
  docs/
  pyproject.toml
  LICENSE
```

The tool code (this repo) is separate from your conversation data (`~/.cursaves/`). Install the tool once, point it at a private remote, and sync from any project directory.

## License

GPL-3.0. See [LICENSE](LICENSE) for details.
