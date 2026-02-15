# How Cursor Stores Chat Data

This document describes how Cursor IDE stores agent/chat conversation data internally, based on reverse-engineering the storage format in February 2026 (Cursor version at that time). This may change in future Cursor versions.

## Overview

Cursor stores all conversation data **locally**, even when connected to a remote host via SSH. The data lives in SQLite databases on the machine running Cursor's UI, not on the remote server.

There are two databases that matter, plus some auxiliary files:

```
~/Library/Application Support/Cursor/User/   (macOS)
~/.config/Cursor/User/                        (Linux)
├── globalStorage/
│   └── state.vscdb                           # Global DB -- all conversation content
└── workspaceStorage/
    ├── <workspace-id-1>/
    │   ├── workspace.json                    # Maps this workspace to a project path
    │   └── state.vscdb                       # Workspace DB -- conversation list
    ├── <workspace-id-2>/
    │   ├── workspace.json
    │   └── state.vscdb
    └── ...
```

## The Two Databases

### Workspace DB (per project, small)

**Location:** `workspaceStorage/{id}/state.vscdb`

Each project you open in Cursor gets its own workspace directory. Inside is a small SQLite database with two tables: `ItemTable` and `cursorDiskKV`.

The key entry is `composer.composerData` in `ItemTable`. Its value is a JSON object listing every conversation for that project:

```json
{
  "allComposers": [
    {
      "composerId": "fda95e1a-7d3a-4113-942f-7e033e454bef",
      "name": "Project structure and issues",
      "createdAt": 1737316260000,
      "lastUpdatedAt": 1737316260000,
      "unifiedMode": "agent",
      "forceMode": "edit"
    },
    ...
  ],
  "selectedComposerIds": ["fda95e1a-7d3a-4113-942f-7e033e454bef"]
}
```

This is what Cursor reads to populate the **sidebar** -- the list of conversations you see when you open a project. It contains metadata only (name, timestamps, mode), not the actual conversation content.

### Global DB (shared, large)

**Location:** `globalStorage/state.vscdb`

This single database stores the actual conversation content for **all projects**. It has the same two tables (`ItemTable`, `cursorDiskKV`), but the important data is in `cursorDiskKV`.

Each conversation is stored under a key `composerData:{UUID}`, where the UUID matches the `composerId` from the workspace DB. The value is a large JSON blob containing the full conversation state.

### How a conversation loads

```
Open project
  → Cursor reads workspace DB
  → Gets list of composer IDs from allComposers
  → Shows them in the sidebar

Click a conversation
  → Cursor queries global DB for composerData:{UUID}
  → Gets the full JSON blob
  → Renders the conversation
```

## Conversation Data Structure

Each `composerData:{UUID}` entry in the global DB is a JSON object with this structure:

```json
{
  "_v": 13,
  "composerId": "fda95e1a-...",
  "name": "Project structure and issues",

  "fullConversationHeadersOnly": [
    { "bubbleId": "uuid-1", "type": 1 },
    { "bubbleId": "uuid-2", "type": 2, "serverBubbleId": "..." }
  ],

  "conversationMap": {
    "uuid-1": { ... message data ... },
    "uuid-2": { ... message data ... }
  },

  "context": {
    "fileSelections": [...],
    "folderSelections": [...],
    "terminalSelections": [...],
    "cursorRules": [...],
    "selectedDocs": [...],
    ...
  },

  "status": "completed",
  "unifiedMode": "agent",
  "forceMode": "edit",
  "createdAt": 1737316260000,
  "isAgentic": true,
  "modelConfig": { "modelName": "composer-1", "maxMode": false },

  ... UI state flags ...
}
```

### Key fields

| Field | Description |
|-------|-------------|
| `fullConversationHeadersOnly` | Ordered list of messages. Each has a `bubbleId` (UUID) and a `type` (1 = user, 2 = assistant). |
| `conversationMap` | Actual message content, keyed by bubble ID. |
| `context` | What files, folders, terminals, docs, rules, etc. were attached as context. |
| `unifiedMode` | The conversation mode: `"agent"`, `"chat"`, `"plan"`, `"edit"`. |
| `modelConfig` | Which model was used. |
| `createdAt` | Unix timestamp in milliseconds. |
| `status` | `"none"`, `"completed"`, etc. |

### Message types

- `type: 1` -- User message
- `type: 2` -- Assistant message

### Subagent conversations

When the agent spawns subagents (e.g., for exploration tasks), they get their own `composerId` with a prefix like `task-toolu_...`. These appear as separate conversations in the workspace DB.

## Content Cache

**Location:** `globalStorage/state.vscdb`, `cursorDiskKV` table, keys matching `composer.content.{hash}`

Large text blobs (e.g., full file contents pasted into a conversation) are stored separately under content-addressed keys. The conversation JSON references these by hash. This avoids duplicating large text across conversations that reference the same file.

## Workspace Identification

### workspace.json

Each workspace directory contains a `workspace.json` that maps it to a project path:

```json
{
  "folder": "file:///Users/callum/Desktop/Projects/my-app"
}
```

For SSH remote workspaces:

```json
{
  "folder": "vscode-remote://ssh-remote%2Bhostname/path/on/remote"
}
```

### Workspace IDs are not deterministic

The workspace directory name (e.g., `497e8ab0309311f4974c80f4621bdc8e`) is an opaque identifier. Importantly:

- The same project path can have **multiple** workspace directories (observed in practice)
- For remote workspaces (`vscode-remote://`), the ID appears to be `MD5(URI)`
- For local workspaces (`file://`), the ID does not match MD5, SHA1, or SHA256 of the URI
- Cursor identifies workspaces by reading `workspace.json`, not by the directory name

This means you can create a new workspace directory with any unique ID, put a correct `workspace.json` inside, and Cursor will adopt it.

## Agent Transcripts

**Location:** `~/.cursor/projects/{sanitized-path}/agent-transcripts/{composerId}.txt`

Cursor also writes plain text transcripts of agent conversations. The directory name is the project path with `/` replaced by `-` and the leading slash stripped:

```
/Users/callum/Desktop/Projects/my-app
→ Users-callum-Desktop-Projects-my-app
```

These are read-only logs. Cursor does not load conversations from these files -- they're supplementary to the SQLite data.

## Path Handling

Absolute file paths appear in conversation metadata in several places:

| Field | Path type | Example |
|-------|-----------|---------|
| `context.fileSelections[].uri.fsPath` | Absolute | `/Users/callum/Projects/app/src/foo.ts` |
| `context.fileSelections[].uri.path` | Absolute | `/Users/callum/Projects/app/src/foo.ts` |
| `context.fileSelections[].uri.external` | File URI | `file:///Users/callum/Projects/app/src/foo.ts` |
| `tokenDetailsUpUntilHere[].relativeWorkspacePath` | Absolute (despite the name) | `/Users/callum/Projects/app/src/foo.ts` |
| `relevantFiles` | Relative | `src/foo.ts` |
| `multiFileLinterErrors[].relativeWorkspacePath` | Relative | `src/foo.ts` |

The actual conversation text (user messages and AI responses) does **not** contain embedded absolute paths. Only metadata fields do. This means conversation content is portable across machines; only the metadata paths need rewriting.

## SQLite Details

Both databases use SQLite 3 with WAL (Write-Ahead Logging) mode. This means:

- The main `.vscdb` file may not contain the most recent data
- A `-wal` file alongside it contains uncommitted writes
- A `-shm` file is used for shared memory coordination
- To read consistent data, you should copy all three files (`.vscdb`, `-wal`, `-shm`) together

The databases have two tables:

```sql
CREATE TABLE ItemTable (key TEXT UNIQUE, value BLOB);
CREATE TABLE cursorDiskKV (key TEXT UNIQUE, value BLOB);
```

Both are simple key-value stores. Values are stored as BLOBs but are typically UTF-8 encoded JSON strings.

## SSH Remote Behaviour

When you connect to a remote host via Cursor's "Connect to Host via SSH" feature:

- Cursor's **UI runs locally** on your machine
- The **workspace files** are on the remote host
- **Chat data is stored locally**, not on the remote host
- The workspace URI uses the `vscode-remote://` scheme

This means switching machines always means losing chat context, because the chats are on whichever local machine was running Cursor's UI.
