"""Export and list operations -- read-only, safe to run while Cursor is open."""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import db, paths


def get_workspace_conversations(project_path: str) -> list[dict]:
    """Get the list of conversations for a project from its workspace DB.

    Returns the allComposers array from composer.composerData, enriched
    with the workspace directory path.
    """
    ws_dirs = paths.find_workspace_dirs_for_project(project_path)
    if not ws_dirs:
        return []

    all_conversations = []
    seen_ids = set()

    for ws_dir in ws_dirs:
        db_path = ws_dir / "state.vscdb"
        if not db_path.exists():
            continue

        with db.CursorDB(db_path) as cdb:
            data = cdb.get_json("composer.composerData", table="ItemTable")
            if not data:
                continue

            composers = data.get("allComposers", [])
            for c in composers:
                cid = c.get("composerId")
                if cid and cid not in seen_ids:
                    seen_ids.add(cid)
                    c["_workspaceDir"] = str(ws_dir)
                    all_conversations.append(c)

    # Sort by creation time, newest first
    all_conversations.sort(
        key=lambda c: c.get("createdAt", 0), reverse=True
    )
    return all_conversations


def get_conversation_data(composer_id: str) -> Optional[dict]:
    """Fetch the full conversation data from the global DB."""
    global_db = paths.get_global_db_path()
    if not global_db.exists():
        return None

    try:
        with db.CursorDB(global_db) as cdb:
            return cdb.get_json(f"composerData:{composer_id}")
    except (OSError, FileNotFoundError) as e:
        print(f"Warning: Could not read global DB: {e}", file=sys.stderr)
        return None


def get_content_blobs(composer_id: str) -> dict[str, str]:
    """Fetch all content blobs referenced by a conversation.

    Scans the conversation data for content hash references and
    retrieves them from the global DB.
    """
    global_db = paths.get_global_db_path()
    if not global_db.exists():
        return {}

    conv_data = get_conversation_data(composer_id)
    if not conv_data:
        return {}

    # Serialise once for searching
    conv_json = json.dumps(conv_data)

    # Collect all content hashes referenced in the conversation
    # They appear in fullConversationHeadersOnly as bubbleId references
    # and the actual content is stored under composer.content.{hash}
    blobs = {}
    try:
        with db.CursorDB(global_db) as cdb:
            content_keys = cdb.list_keys("composer.content.")
            for key in content_keys:
                content_hash = key[len("composer.content."):]
                if content_hash in conv_json:
                    val = cdb.get_disk_kv(key)
                    if val:
                        blobs[content_hash] = val
    except (OSError, FileNotFoundError):
        pass  # Non-fatal: content blobs are supplementary

    return blobs


def get_message_contexts(composer_id: str) -> dict[str, Any]:
    """Fetch messageRequestContext entries for a conversation."""
    global_db = paths.get_global_db_path()
    if not global_db.exists():
        return {}

    contexts = {}
    with db.CursorDB(global_db) as cdb:
        keys = cdb.list_keys(f"messageRequestContext:{composer_id}:")
        for key in keys:
            val = cdb.get_json(key)
            if val:
                # Store with a short key (just the message part)
                short_key = key[len(f"messageRequestContext:{composer_id}:"):]
                contexts[short_key] = val

    return contexts


def get_bubble_entries(composer_id: str) -> dict[str, Any]:
    """Fetch individual message bubble entries for a conversation.

    Cursor stores message content under bubbleId:{composerId}:{bubbleId} keys.
    This is the new storage format (as of 2026) where conversationMap is empty
    and messages are stored individually.
    """
    global_db = paths.get_global_db_path()
    if not global_db.exists():
        return {}

    bubbles = {}
    with db.CursorDB(global_db) as cdb:
        keys = cdb.list_keys(f"bubbleId:{composer_id}:")
        for key in keys:
            val = cdb.get_json(key)
            if val:
                # Store with just the bubble ID as key
                bubble_id = key[len(f"bubbleId:{composer_id}:"):]
                bubbles[bubble_id] = val

    return bubbles


def get_transcript(project_path: str, composer_id: str) -> Optional[str]:
    """Get the agent transcript for a conversation, if it exists."""
    transcript_dir = paths.find_transcript_dir(project_path)
    if not transcript_dir:
        return None

    transcript_file = transcript_dir / f"{composer_id}.txt"
    if transcript_file.exists():
        try:
            return transcript_file.read_text()
        except OSError:
            return None

    return None


def format_timestamp(ts_ms: int) -> str:
    """Format a millisecond timestamp to a readable string."""
    if not ts_ms:
        return "unknown"
    try:
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OSError):
        return "unknown"


def list_conversations(project_path: str) -> list[dict]:
    """List all conversations for a project with display-friendly info.

    Returns list of dicts with: id, name, date, mode, messageCount.
    """
    conversations = get_workspace_conversations(project_path)
    results = []

    for c in conversations:
        composer_id = c.get("composerId", "unknown")

        # Get message count from the full conversation data
        msg_count = 0
        conv_data = get_conversation_data(composer_id)
        if conv_data:
            headers = conv_data.get("fullConversationHeadersOnly", [])
            msg_count = len(headers)

        results.append({
            "id": composer_id,
            "name": c.get("name", "Untitled"),
            "date": format_timestamp(c.get("createdAt", 0)),
            "lastUpdated": format_timestamp(c.get("lastUpdatedAt", c.get("createdAt", 0))),
            "mode": c.get("unifiedMode", c.get("forceMode", "unknown")),
            "messageCount": msg_count,
        })

    return results


def export_conversation(project_path: str, composer_id: str) -> Optional[dict]:
    """Export a single conversation to a self-contained snapshot dict."""
    conv_data = get_conversation_data(composer_id)
    if not conv_data:
        return None

    # Get bubble entries (individual message content - new storage format)
    bubbles = get_bubble_entries(composer_id)

    return {
        "version": 3,  # Bumped for bubbleEntries support
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "sourceMachine": paths.get_machine_id(),
        "sourceProjectPath": os.path.normpath(project_path),
        "projectIdentifier": paths.get_project_identifier(project_path),
        "composerId": composer_id,
        "composerData": conv_data,
        "contentBlobs": get_content_blobs(composer_id),
        "messageContexts": get_message_contexts(composer_id),
        "bubbleEntries": bubbles,  # Individual message content
        "transcript": get_transcript(project_path, composer_id),
    }


def save_snapshot(snapshot: dict, snapshots_dir: Path) -> Path:
    """Save a snapshot dict to a JSON file.

    Returns the path to the saved file.
    """
    # Organise by project identifier (git remote URL or directory name)
    project_id = snapshot.get("projectIdentifier")
    if not project_id:
        # Fallback for v1 snapshots without projectIdentifier
        project_id = os.path.basename(snapshot.get("sourceProjectPath", "unknown"))
    project_dir = snapshots_dir / project_id
    project_dir.mkdir(parents=True, exist_ok=True)

    composer_id = snapshot["composerId"]
    snapshot_file = project_dir / f"{composer_id}.json"
    snapshot_file.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))
    return snapshot_file


def checkpoint_project(
    project_path: str,
    composer_ids: Optional[list[str]] = None,
) -> list[Path]:
    """Export conversations for a project to snapshots/.

    If composer_ids is given, only export those conversations.
    Otherwise, export all conversations.

    Returns list of saved snapshot file paths.
    """
    snapshots_dir = paths.get_snapshots_dir()
    conversations = get_workspace_conversations(project_path)
    saved = []

    for c in conversations:
        composer_id = c.get("composerId")
        if not composer_id:
            continue
        if composer_ids is not None and composer_id not in composer_ids:
            continue

        snapshot = export_conversation(project_path, composer_id)
        if snapshot:
            path = save_snapshot(snapshot, snapshots_dir)
            saved.append(path)

    return saved
