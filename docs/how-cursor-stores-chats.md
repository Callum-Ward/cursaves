# How Cursor Stores Chat Data

This document describes how Cursor IDE stores agent/chat conversation data internally, based on reverse-engineering the storage format. Originally documented in February 2026 (Cursor ~2.6), updated in April 2026 for the Cursor 3.0 migration.

## Overview

Cursor stores all conversation data **locally**, even when connected to a remote host via SSH. The data lives in SQLite databases on the machine running Cursor's UI, not on the remote server.

There are two databases that matter, plus some auxiliary files:

```
~/Library/Application Support/Cursor/User/   (macOS)
~/.config/Cursor/User/                        (Linux)
├── globalStorage/
│   └── state.vscdb                           # Global DB -- conversation content + central index (3.0+)
└── workspaceStorage/
    ├── <workspace-id-1>/
    │   ├── workspace.json                    # Maps this workspace to a project path
    │   └── state.vscdb                       # Workspace DB -- chat list (2.x) / selected tabs (3.0+)
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

In Cursor ≤2.6, this is what populates the **sidebar** — the list of conversations you see when you open a project. In Cursor 3.0+, this data is moved to the global DB (see [Cursor 3.0 Migration](#cursor-30-migration-april-2026)). It contains metadata only (name, timestamps, mode), not the actual conversation content.

### Global DB (shared, large)

**Location:** `globalStorage/state.vscdb`

This single database stores the actual conversation content for **all projects**. It has the same two tables (`ItemTable`, `cursorDiskKV`). Conversation data lives in `cursorDiskKV`. In Cursor 3.0+, the central chat-workspace index (`composer.composerHeaders`) lives in `ItemTable`.

The global DB stores five types of entries for each conversation, all keyed by `composerId`:

| Key pattern | Content |
|-------------|---------|
| `composerData:{composerId}` | Conversation metadata, headers, and state |
| `bubbleId:{composerId}:{bubbleId}` | Individual message content (one per message) |
| `checkpointId:{composerId}:{checkpointId}` | Workspace state snapshots (file diffs per agent turn) |
| `messageRequestContext:{composerId}:{messageId}` | Full request context sent to the model |
| `composer.content.{hash}` | Content-addressed blobs (shared across conversations) |

All of these are required for a conversation to be fully functional on another machine. Missing `composerData` or `bubbleId` entries means the conversation can't render. Missing `checkpointId` entries means the conversation can't be continued (agent mode fails with "Blob not found").

### How a conversation loads

**Cursor ≤2.6:**
```
Open project
  → Cursor reads workspace DB
  → Gets list of composer IDs from allComposers
  → Shows them in the sidebar
```

**Cursor 3.0+:**
```
Open project
  → Cursor reads composer.composerHeaders from global DB
  → Filters entries by workspaceIdentifier matching this workspace
  → Shows them in the sidebar
```

**Both versions:**
```
Click a conversation
  → Cursor queries global DB for composerData:{UUID}
  → Gets the full JSON blob
  → Renders the conversation
```

## Cursor 3.0 Migration (April 2026)

Cursor 3.0 (released April 2, 2026) introduced a **breaking, one-way database migration** that fundamentally changed how chats are associated with workspaces. The per-workspace chat index was **centralized** into the global DB.

### Design decision: decentralized → centralized

**Before (Cursor ≤2.6):** Each workspace DB independently tracked its own chats via an `allComposers` array in `composer.composerData` (ItemTable). This was a decentralized model — each workspace was the source of truth for its own chats.

```json
// Workspace DB → composer.composerData (ItemTable)
{
  "allComposers": [
    {"composerId": "abc-123", "name": "My chat", ...},
    {"composerId": "def-456", "name": "Another chat", ...}
  ],
  "selectedComposerIds": ["abc-123"]
}
```

**After (Cursor 3.0+):** The per-workspace `allComposers` array is **removed** on first launch. A **single central index** is created in the **global DB's ItemTable** at `composer.composerHeaders`. Each entry is tagged with a `workspaceIdentifier` that links it back to a workspace.

```json
// Global DB → composer.composerHeaders (ItemTable)
{
  "allComposers": [
    {
      "composerId": "abc-123",
      "name": "My chat",
      "workspaceIdentifier": {
        "id": "b9d81724966720d40c87d7f4e9983d73",
        "uri": {
          "fsPath": "/Users/callum/Projects/my-app",
          "scheme": "file",
          "external": "file:///Users/callum/Projects/my-app"
        }
      },
      ...
    },
    ...
  ]
}
```

The `workspaceIdentifier.id` matches the workspace directory hash under `workspaceStorage/`. The `uri` contains the full workspace folder URI.

### Why they made this change

1. **Single source of truth** — the old model had workspace DBs and global DB as independent sources that could get out of sync (causing orphaned chats). Now there's one canonical index.
2. **Portability** — if a workspace directory gets regenerated (new hash), the central index still knows which chats belong to that project path via the URI.
3. **Ephemeral window support** — Cursor 3.0 supports chats not tied to any workspace (started from the welcome screen). These get timestamp-based workspace identifiers like `{"id": "1775744516184"}` with no URI.
4. **Unified chat/agent UI** — Cursor 3.0 merged the Chat and Agents windows into a single interface. Having a central index simplifies chat discovery across the unified UI.

### What the workspace DB retains

After migration, the workspace DB's `composer.composerData` keeps only:

```json
{
  "selectedComposerIds": ["abc-123"],
  "lastFocusedComposerIds": ["abc-123"],
  "hasMigratedComposerData": true,
  "hasMigratedMultipleComposers": true
}
```

New keys also appear in the workspace DB's ItemTable:

| Key pattern | Purpose |
|------------|---------|
| `workbench.panel.composerChatViewPane.*` | Records which chat tabs have been opened (each tab gets a UUID, containing view references to composerIds) |
| `newAgentSidebar.*` | Agent sidebar section collapsed/expanded state |
| `cursor/agentLayout.*` | Agent panel layout (widths, visibility) |
| `agentSidebar.section.*.collapsed` | Sidebar time-period section states (today, yesterday, last 7 days, etc.) |

### Important: the global index is not immediately complete

`composer.composerHeaders` only tracks chats that have been **actively opened or created since the migration**. Older chats that existed before the 3.0 migration but haven't been re-opened are **not** in the global index. They can still be discovered via:

- `composerChatViewPane.*` entries in the workspace DB (if the chat was opened in a tab before)
- `selectedComposerIds` / `lastFocusedComposerIds` in the workspace DB
- The global DB's `composerData:{id}` entries (the actual chat data is unchanged)

### Chat and Agent views

Cursor 3.0 initially introduced a dedicated **Agents window** alongside the existing **Chat window**, but subsequently merged them into a unified interface. Chats and agents are now the same thing — you start an agent conversation and can switch views within it.

Each conversation has a `unifiedMode` field in its `composerData`:

- `"agent"` — the default mode for new conversations
- `"chat"` — legacy chat-only mode (no tool use, file editing, etc.)

Both types are stored identically. The mode determines what capabilities the AI has during the conversation.

### How to detect which schema a workspace uses

```python
data = cdb.get_json("composer.composerData", table="ItemTable")
is_migrated = data is not None and "allComposers" not in data
```

On a machine running Cursor 3.0, recently-opened workspaces will have migrated while older workspaces (not opened since the update) retain the old format.

### How cursaves handles this

As of v0.8.2, cursaves combines multiple sources for maximum coverage:

**Discovery (reading):**
1. `composer.composerHeaders` in global DB (Cursor 3.0+ central index — fast, authoritative for recent chats)
2. `allComposers` in workspace DB (Cursor 2.x — complete for old workspaces)
3. `selectedComposerIds`, `lastFocusedComposerIds`, and `composerChatViewPane.*` in workspace DB (catches chats not yet in the global index)
4. For IDs found only via (3), metadata is fetched from the global DB's `composerData:{id}` entry

**Registration (writing):**
- **Cursor 2.x** workspaces: append to `allComposers` + `selectedComposerIds` in workspace DB
- **Cursor 3.0+** workspaces: add to `composer.composerHeaders` in global DB (with `workspaceIdentifier`) + `selectedComposerIds` in workspace DB
- In both cases, conversation data is written to the global DB identically

**Migration:**

`cursaves migrate` scans all workspaces for chats tracked in the old format that are missing from `composer.composerHeaders`, and adds them with the correct `workspaceIdentifier`. This makes old chats appear in Cursor 3.0's sidebar without needing to manually re-open each one. Use `--dry-run` to preview first.

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
| `conversationMap` | Legacy message content, keyed by bubble ID. Empty in newer conversations. |
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

## Individual Bubble Entries (v3 storage)

**Location:** `globalStorage/state.vscdb`, `cursorDiskKV` table, keys matching `bubbleId:{composerId}:{bubbleId}`

As of early 2026, Cursor stores message content as individual key-value entries rather than in the `conversationMap` field inside `composerData`. Each message gets its own entry keyed by `bubbleId:{composerId}:{bubbleId}`.

A typical bubble entry contains ~60+ fields. Some notable ones:

| Field | Description |
|-------|-------------|
| `text` | The actual message text (user prompt or assistant response) |
| `type` | 1 = user, 2 = assistant |
| `richText` | Structured representation of user input (present on user messages) |
| `context` | Context attached to this specific message |
| `codeBlocks` | Code blocks in assistant responses |
| `suggestedCodeBlocks` | Diffs the assistant proposed |
| `toolResults` | Results from tool calls (file edits, terminal commands, etc.) |
| `checkpointId` | Reference to a workspace checkpoint (user messages in agent mode) |
| `allThinkingBlocks` | The model's chain-of-thought reasoning blocks |
| `createdAt` | Timestamp for this individual message |

A conversation with 1000 messages will have 1000 separate `bubbleId:` entries in the global DB. The `composerData` entry's `fullConversationHeadersOnly` array provides the ordering, while the actual content lives in these individual entries.

### How to identify the storage format

- **Legacy (v1/v2):** `conversationMap` in `composerData` is populated with message content
- **Current (v3):** `conversationMap` is empty or absent; messages are in `bubbleId:` entries

Both formats use `fullConversationHeadersOnly` as the ordered message index.

## Checkpoint Data

**Location:** `globalStorage/state.vscdb`, `cursorDiskKV` table, keys matching `checkpointId:{composerId}:{checkpointId}`

When running in agent mode, Cursor takes a workspace state snapshot before each agent turn. These checkpoints record which files were modified and the diffs applied, so the agent can restore the workspace to a known state if it needs to retry or the user wants to continue the conversation later.

Each checkpoint is a JSON object:

```json
{
  "files": [
    {
      "uri": {
        "path": "/path/to/file.py",
        "scheme": "vscode-remote",
        "authority": "ssh-remote+hostname"
      },
      "originalModelDiffWrtV0": [
        {
          "original": { "startLineNumber": 10, "endLineNumberExclusive": 15 },
          "modified": ["new line 1", "new line 2"]
        }
      ]
    }
  ],
  "nonExistentFiles": [],
  "newlyCreatedFolders": [],
  "activeInlineDiffs": [],
  "inlineDiffNewlyCreatedResources": []
}
```

User messages (type 1) reference checkpoints via their `checkpointId` field in the bubble entry. When continuing a conversation, the agent loop reads the checkpoint referenced by the last user message to restore the workspace state. **If the checkpoint is missing, Cursor fails with an "[internal] Blob not found" error** and the conversation cannot be continued.

A long conversation can accumulate hundreds of checkpoints. They compress well (mostly text diffs) — a conversation with 189 checkpoints is roughly 9 MB uncompressed / 1.4 MB compressed.

## Message Request Contexts

**Location:** `globalStorage/state.vscdb`, `cursorDiskKV` table, keys matching `messageRequestContext:{composerId}:{messageId}`

Each user message can have an associated request context that captures the full state of what was sent to the model. This includes file contents, git diffs, terminal output, and other context that was part of the request. These are supplementary — the conversation is readable without them, but they enable richer replay and continuation.

## Content Cache

**Location:** `globalStorage/state.vscdb`, `cursorDiskKV` table, keys matching `composer.content.{hash}`

Large text blobs (e.g., full file contents pasted into a conversation) are stored separately under content-addressed keys. The conversation JSON references these by hash. This avoids duplicating large text across conversations that reference the same file.

## Workspace Identification

### workspace.json

Each workspace directory contains a `workspace.json` that maps it to a project path. For single-folder workspaces:

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

For custom workspaces (`.code-workspace` files), Cursor uses a `workspace` key instead of `folder`:

```json
{
  "workspace": "file:///Users/callum/Desktop/Projects/my-proj.code-workspace"
}
```

The workspace directory name (hash) under `workspaceStorage/` can be used with `cursaves -w <hash>` when the workspace doesn't appear via number or path (e.g. workspace).

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
