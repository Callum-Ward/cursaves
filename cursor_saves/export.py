"""Export and list operations -- read-only, safe to run while Cursor is open."""

import gzip
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import db, paths


def get_workspace_conversations(
    project_path: str,
    workspace_dir: Optional[Path] = None,
) -> list[dict]:
    """Get the list of conversations for a project from its workspace DB.

    If workspace_dir is provided, only reads from that specific workspace
    (avoids cross-host contamination for SSH workspaces with the same path).

    Returns the allComposers array from composer.composerData, enriched
    with the workspace directory path.
    """
    if workspace_dir is not None:
        ws_dirs = [workspace_dir]
    else:
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


def list_conversations(
    project_path: str,
    workspace_dir: Optional[Path] = None,
) -> list[dict]:
    """List all conversations for a project with display-friendly info.

    Returns list of dicts with: id, name, date, mode, messageCount.
    """
    conversations = get_workspace_conversations(project_path, workspace_dir=workspace_dir)
    if not conversations:
        return []

    results = []
    global_db = paths.get_global_db_path()

    # Single DB connection for all lookups
    with db.CursorDB(global_db) as cdb:
        for c in conversations:
            composer_id = c.get("composerId", "unknown")

            msg_count = 0
            conv_data = cdb.get_json(f"composerData:{composer_id}")
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


MAX_COMPRESSED_SIZE_MB = 95  # Stay under GitHub's 100MB limit
SHARD_SIZE_BYTES = 90 * 1024 * 1024  # 90MB per shard (GitHub rejects files > 100MB)
MAX_RECENT_CONTEXTS = 20     # Always keep this many recent message contexts


def _trim_message_contexts(contexts: dict[str, Any], max_size_bytes: int) -> dict[str, Any]:
    """Trim older message contexts to stay under size limit.
    
    Keeps the most recent contexts (by key, which includes message ID).
    """
    if not contexts:
        return contexts
    
    # Sort by key (message IDs are typically chronological or we keep all if small)
    sorted_keys = sorted(contexts.keys())
    
    # Always keep the last N contexts
    recent_keys = set(sorted_keys[-MAX_RECENT_CONTEXTS:])
    
    # Calculate current size
    current_size = sum(len(json.dumps(v)) for v in contexts.values())
    
    if current_size <= max_size_bytes:
        return contexts
    
    # Remove oldest contexts until we're under the limit
    trimmed = {}
    kept_size = 0
    
    # First, always include recent contexts
    for key in sorted_keys[-MAX_RECENT_CONTEXTS:]:
        trimmed[key] = contexts[key]
        kept_size += len(json.dumps(contexts[key]))
    
    # Then add older ones if there's room
    for key in sorted_keys[:-MAX_RECENT_CONTEXTS]:
        entry_size = len(json.dumps(contexts[key]))
        if kept_size + entry_size <= max_size_bytes:
            trimmed[key] = contexts[key]
            kept_size += entry_size
    
    return trimmed


def _extract_agent_blob_ids(conv_data: dict) -> set[str]:
    """Extract agentKv blob IDs referenced by a conversation.

    The composerData.conversationState field is a base64-encoded protobuf
    prefixed with '~'. It contains repeated 32-byte blob IDs (protobuf
    field 1, wire type 2, length 32) which reference agentKv:blob:{hex}
    entries in cursorDiskKV.
    """
    import base64

    cs = conv_data.get("conversationState", "")
    if not cs or not isinstance(cs, str) or not cs.startswith("~") or len(cs) < 10:
        return set()

    try:
        raw = base64.b64decode(cs[1:])
    except Exception:
        return set()

    blob_ids: set[str] = set()
    i = 0
    while i < len(raw) - 33:
        if raw[i] == 0x0A and raw[i + 1] == 0x20:
            blob_ids.add(raw[i + 2 : i + 34].hex())
            i += 34
        else:
            i += 1
    return blob_ids


def _extract_agent_blobs(
    conv_data: dict,
    cdb: "db.CursorDB",
) -> dict[str, str]:
    """Fetch agentKv blob entries referenced by a conversation.

    Returns a dict mapping hex blob IDs to their base64-encoded values.
    Values are stored as binary in the DB; we base64-encode them for JSON
    serialization in the snapshot.
    """
    import base64

    blob_ids = _extract_agent_blob_ids(conv_data)
    if not blob_ids:
        return {}

    blobs: dict[str, str] = {}
    for bid in blob_ids:
        key = f"agentKv:blob:{bid}"
        val = cdb.get_item_binary(key, table="cursorDiskKV")
        if val is not None:
            blobs[bid] = base64.b64encode(val).decode("ascii")
    return blobs


def export_conversation(
    project_path: str,
    composer_id: str,
    _cdb: Optional[db.CursorDB] = None,
    source_host: Optional[str] = None,
) -> Optional[dict]:
    """Export a single conversation to a self-contained snapshot dict.

    Includes messageContexts (file contents, git diffs) for seamless continuation.
    Size trimming happens in save_snapshot after checking compressed size.

    Pass an open CursorDB via _cdb to avoid re-copying the global DB.
    Pass source_host for SSH workspaces (e.g. "core-3").
    """
    global_db = paths.get_global_db_path()
    own_cdb = _cdb is None
    if own_cdb:
        _cdb = db.CursorDB(global_db)

    try:
        conv_data = _cdb.get_json(f"composerData:{composer_id}")
        if not conv_data:
            return None

        # Bubble entries (individual message content)
        bubbles = {}
        for key in _cdb.list_keys(f"bubbleId:{composer_id}:"):
            val = _cdb.get_json(key)
            if val:
                bubble_id = key[len(f"bubbleId:{composer_id}:"):]
                bubbles[bubble_id] = val

        # Content blobs referenced by this conversation
        conv_json = json.dumps(conv_data)
        blobs = {}
        for key in _cdb.list_keys("composer.content."):
            content_hash = key[len("composer.content."):]
            if content_hash in conv_json:
                val = _cdb.get_disk_kv(key)
                if val:
                    blobs[content_hash] = val

        # Message request contexts
        contexts = {}
        for key in _cdb.list_keys(f"messageRequestContext:{composer_id}:"):
            val = _cdb.get_json(key)
            if val:
                short_key = key[len(f"messageRequestContext:{composer_id}:"):]
                contexts[short_key] = val

        # Checkpoint data (workspace state snapshots at each agent turn)
        checkpoints = {}
        for key in _cdb.list_keys(f"checkpointId:{composer_id}:"):
            val = _cdb.get_json(key)
            if val:
                cp_id = key[len(f"checkpointId:{composer_id}:"):]
                checkpoints[cp_id] = val

        # Agent state blobs (encrypted agent context needed for continuation).
        # The conversationState field in composerData is a protobuf containing
        # references to agentKv:blob:{hex} entries. Without these, Cursor's
        # agent loop fails with "Blob not found" when continuing the chat.
        agent_blobs = _extract_agent_blobs(conv_data, _cdb)

        snapshot = {
            "version": 3,
            "exportedAt": datetime.now(timezone.utc).isoformat(),
            "sourceMachine": paths.get_machine_id(),
            "sourceHost": source_host,
            "sourceProjectPath": os.path.normpath(project_path),
            "projectIdentifier": paths.get_project_identifier(project_path),
            "composerId": composer_id,
            "composerData": conv_data,
            "contentBlobs": blobs,
            "bubbleEntries": bubbles,
            "checkpoints": checkpoints,
            "agentBlobs": agent_blobs,
            "transcript": get_transcript(project_path, composer_id),
            "messageContexts": contexts,
        }

        return snapshot
    finally:
        if own_cdb:
            _cdb.close()


def _compress_snapshot(snapshot: dict) -> bytes:
    """Compress a snapshot dict to gzip bytes."""
    json_bytes = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    import io
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9) as f:
        f.write(json_bytes)
    return buf.getvalue()


def save_snapshot(snapshot: dict, snapshots_dir: Path) -> Path:
    """Save a snapshot dict to a compressed JSON file.
    
    If compressed size exceeds the limit, trims older messageContexts
    while keeping recent ones for seamless continuation.

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
    
    # Remove old uncompressed file if it exists
    old_file = project_dir / f"{composer_id}.json"
    if old_file.exists():
        old_file.unlink()
    
    # Compress and check size
    max_size = MAX_COMPRESSED_SIZE_MB * 1024 * 1024
    compressed = _compress_snapshot(snapshot)
    
    # If too large, trim messageContexts and retry
    if len(compressed) > max_size and snapshot.get("messageContexts"):
        contexts = snapshot["messageContexts"]
        # Binary search for acceptable context size
        # Start by keeping only recent contexts
        trimmed_contexts = _trim_message_contexts(contexts, max_size * 5)  # Rough estimate
        snapshot["messageContexts"] = trimmed_contexts
        compressed = _compress_snapshot(snapshot)
        
        # If still too large, keep removing contexts
        while len(compressed) > max_size and len(snapshot.get("messageContexts", {})) > MAX_RECENT_CONTEXTS:
            # Remove half of the remaining non-recent contexts
            sorted_keys = sorted(snapshot["messageContexts"].keys())
            keep_keys = sorted_keys[len(sorted_keys)//2:]  # Keep newer half
            snapshot["messageContexts"] = {k: snapshot["messageContexts"][k] for k in keep_keys}
            compressed = _compress_snapshot(snapshot)
        
        # If still too large even with only recent contexts, remove contexts entirely
        if len(compressed) > max_size:
            snapshot["messageContexts"] = {}
            compressed = _compress_snapshot(snapshot)
    
    # Clean up any previous shards or single file
    for old in project_dir.glob(f"{composer_id}.json.gz*"):
        if not old.name.endswith(".meta.json"):
            old.unlink()

    # Save snapshot (shard if too large for GitHub)
    snapshot_file = project_dir / f"{composer_id}.json.gz"
    if len(compressed) > SHARD_SIZE_BYTES:
        num_shards = 0
        for i in range(0, len(compressed), SHARD_SIZE_BYTES):
            shard_path = project_dir / f"{composer_id}.json.gz.{i // SHARD_SIZE_BYTES:02d}"
            shard_path.write_bytes(compressed[i:i + SHARD_SIZE_BYTES])
            num_shards += 1
        print(f"  Sharded into {num_shards} parts ({len(compressed) / 1024 / 1024:.1f} MB total)")
    else:
        snapshot_file.write_bytes(compressed)

    # Write lightweight metadata sidecar (avoids decompressing for listings)
    cd = snapshot.get("composerData", {})
    num_shards = 0
    if len(compressed) > SHARD_SIZE_BYTES:
        num_shards = (len(compressed) + SHARD_SIZE_BYTES - 1) // SHARD_SIZE_BYTES
    meta = {
        "composerId": composer_id,
        "name": cd.get("name"),
        "messageCount": len(cd.get("fullConversationHeadersOnly", [])),
        "exportedAt": snapshot.get("exportedAt"),
        "sourceMachine": snapshot.get("sourceMachine"),
        "sourceHost": snapshot.get("sourceHost"),
        "sourceProjectPath": snapshot.get("sourceProjectPath"),
        "projectIdentifier": snapshot.get("projectIdentifier"),
        "version": snapshot.get("version"),
        "shardCount": num_shards if num_shards else None,
    }
    meta_file = project_dir / f"{composer_id}.meta.json"
    meta_file.write_text(json.dumps(meta, indent=2))

    return snapshot_file


def _composer_id_from_snapshot_path(path: Path) -> str:
    """Extract composer ID from a snapshot filename (e.g. uuid.json.gz -> uuid)."""
    name = path.name
    if name.endswith(".json.gz"):
        return name[:-8]
    if name.endswith(".json"):
        return name[:-5]
    return path.stem


def prune_project_snapshots(project_dir: Path, composer_ids_to_keep: set[str]) -> int:
    """Remove snapshot files whose composer_id is not in the keep set.

    Deletes the snapshot file (or shards), its .meta.json sidecar, and any
    legacy .json files. Use after checkpoint_project to ensure removed
    conversations don't linger in git.

    Returns the number of snapshots pruned.
    """
    from .importer import list_snapshot_files

    keep = set(composer_ids_to_keep)
    pruned = 0
    for sf in list_snapshot_files(project_dir):
        cid = _composer_id_from_snapshot_path(sf)
        if cid in keep:
            continue
        # Delete snapshot, shards, and meta
        if sf.exists():
            sf.unlink()
        for shard in project_dir.glob(f"{cid}.json.gz.*"):
            if not shard.name.endswith(".meta.json"):
                shard.unlink()
        meta = project_dir / f"{cid}.meta.json"
        if meta.exists():
            meta.unlink()
        legacy = project_dir / f"{cid}.json"
        if legacy.exists():
            legacy.unlink()
        pruned += 1
    return pruned


def checkpoint_project(
    project_path: str,
    composer_ids: Optional[list[str]] = None,
    workspace_dir: Optional[Path] = None,
    source_host: Optional[str] = None,
) -> list[Path]:
    """Export conversations for a project to snapshots/.

    If composer_ids is given, only export those conversations.
    If workspace_dir is given, only reads from that specific workspace.
    Otherwise, export all conversations from all matching workspaces.

    Returns list of saved snapshot file paths.
    """
    snapshots_dir = paths.get_snapshots_dir()

    t0 = time.time()
    print("  Fetching workspace conversations...", file=sys.stderr, flush=True)
    conversations = get_workspace_conversations(project_path, workspace_dir=workspace_dir)
    print(f"  Found {len(conversations)} conversation(s) in workspace(s)", file=sys.stderr, flush=True)

    # Filter to selected ids and count how many we'll actually process
    to_process: list[tuple[dict, str]] = []
    for c in conversations:
        composer_id: str | None = c.get("composerId")
        if not composer_id:
            continue
        if composer_ids is not None and composer_id not in composer_ids:
            continue
        to_process.append((c, composer_id))

    print(f"  Processing {len(to_process)} conversation(s)...", file=sys.stderr, flush=True)

    last_log_time = t0
    saved = []
    global_db = paths.get_global_db_path()
    with db.CursorDB(global_db) as cdb:
        for i, (c, composer_id) in enumerate(to_process, 1):
            # Export the conversation
            snapshot = export_conversation(project_path, composer_id, _cdb=cdb, source_host=source_host)
            if snapshot:
                path = save_snapshot(snapshot, snapshots_dir)
                saved.append(path)
            
            # Log progress: every 10 items, or every 10 seconds since last log
            if i % 10 == 0 or (time.time() - last_log_time) >= 10:
                print(f"  [{i}/{len(to_process)}] {composer_id}", file=sys.stderr, flush=True)
                last_log_time = time.time()

    total = time.time() - t0
    print(f"  Completed in {total:.1f}s", file=sys.stderr, flush=True)
    return saved
