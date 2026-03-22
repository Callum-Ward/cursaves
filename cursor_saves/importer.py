"""Import operations -- writes to Cursor's databases with safety checks."""

import gzip
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

from . import db, paths


def _get_shard_paths(base_path: Path) -> list[Path]:
    """Return ordered shard paths for a sharded snapshot, or empty list."""
    shards = sorted(base_path.parent.glob(f"{base_path.name}.*"))
    return [s for s in shards if s.suffix.lstrip(".").isdigit()]


def read_snapshot_file(path: Path) -> dict:
    """Read a snapshot file (supports .json, .json.gz, and sharded .json.gz.NN)."""
    if path.suffix == ".gz":
        # Check for shards first
        shards = _get_shard_paths(path)
        if shards and not path.exists():
            compressed = b"".join(s.read_bytes() for s in shards)
            raw = gzip.decompress(compressed)
            return json.loads(raw)
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    else:
        return json.loads(path.read_text())


def list_snapshot_files(directory: Path) -> list[Path]:
    """List all logical snapshot files in a directory.

    Returns one Path per snapshot. For sharded snapshots (*.json.gz.00, .01, ...),
    returns the base path (*.json.gz) even though that file doesn't exist on disk.
    Excludes .meta.json sidecar files.
    """
    files = set()
    for f in directory.glob("*.json"):
        if not f.name.endswith(".meta.json"):
            files.add(f)
    files.update(directory.glob("*.json.gz"))

    # Detect sharded snapshots: *.json.gz.00 indicates a sharded set
    for f in directory.glob("*.json.gz.00"):
        base = f.parent / f.name[:-3]  # strip ".00"
        files.add(base)

    # Remove individual shard files from the set (they're represented by the base)
    files = {f for f in files if not (f.suffix.lstrip(".").isdigit() and ".json.gz." in f.name)}

    return sorted(files)


def read_snapshot_meta(snapshot_path: Path) -> dict:
    """Read snapshot metadata from the sidecar .meta.json file.

    Falls back to reading the full snapshot if no sidecar exists.
    Returns a dict with: composerId, name, messageCount, exportedAt,
    sourceMachine, sourceProjectPath, projectIdentifier.
    """
    # Try sidecar first (instant)
    stem = snapshot_path.stem
    if stem.endswith(".json"):
        stem = stem[:-5]
    meta_path = snapshot_path.parent / f"{stem}.meta.json"
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    # Fallback: read full snapshot (slow for large files)
    try:
        data = read_snapshot_file(snapshot_path)
        cd = data.get("composerData", {})
        return {
            "composerId": data.get("composerId"),
            "name": cd.get("name"),
            "messageCount": len(cd.get("fullConversationHeadersOnly", [])),
            "exportedAt": data.get("exportedAt"),
            "sourceMachine": data.get("sourceMachine"),
            "sourceHost": data.get("sourceHost"),
            "sourceProjectPath": data.get("sourceProjectPath"),
            "projectIdentifier": data.get("projectIdentifier"),
            "version": data.get("version"),
        }
    except Exception:
        return {
            "composerId": stem,
            "name": None,
            "messageCount": 0,
            "exportedAt": None,
            "sourceMachine": None,
            "sourceProjectPath": None,
        }


def is_cursor_running() -> bool:
    """Check if the main Cursor app process is running.

    On macOS, pgrep -x fails because the comm field is truncated to 16
    characters. Instead we parse `ps -axo args` and look for the main
    Cursor executable while excluding helpers, crash handlers, and the
    macOS CursorUIViewService system process.
    """
    try:
        result = subprocess.run(
            ["ps", "-axo", "args"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return False
        for line in result.stdout.splitlines():
            if "Cursor.app/Contents/MacOS/Cursor" in line \
                    and "Helper" not in line \
                    and "Frameworks" not in line:
                return True
        return False
    except FileNotFoundError:
        return False


def rewrite_paths(data: Any, old_prefix: str, new_prefix: str) -> Any:
    """Recursively rewrite absolute paths in conversation data.

    Replaces old_prefix with new_prefix in all string values that
    look like file paths.
    """
    if isinstance(data, str):
        if old_prefix in data:
            return data.replace(old_prefix, new_prefix)
        return data
    elif isinstance(data, dict):
        return {k: rewrite_paths(v, old_prefix, new_prefix) for k, v in data.items()}
    elif isinstance(data, list):
        return [rewrite_paths(item, old_prefix, new_prefix) for item in data]
    else:
        return data


def find_or_create_workspace(project_path: str) -> Path:
    """Find an existing workspace dir for the project, or create a new one.

    Returns the workspace directory path.
    """
    # Check for existing workspace
    existing = paths.find_workspace_dirs_for_project(project_path)
    if existing:
        return existing[0]  # Use the most recent one

    # Create a new workspace directory
    ws_storage = paths.get_workspace_storage_dir()
    ws_id = uuid.uuid4().hex  # Random 32-char hex ID
    ws_dir = ws_storage / ws_id
    ws_dir.mkdir(parents=True, exist_ok=True)

    # Create workspace.json
    folder_uri = "file://" + os.path.normpath(project_path)
    ws_json = ws_dir / "workspace.json"
    ws_json.write_text(json.dumps({"folder": folder_uri}))

    # Create an empty state.vscdb
    _init_workspace_db(ws_dir / "state.vscdb")

    return ws_dir


def _init_workspace_db(db_path: Path):
    """Create a minimal state.vscdb with the required tables."""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE IF NOT EXISTS ItemTable (key TEXT UNIQUE, value BLOB)")
    conn.execute("CREATE TABLE IF NOT EXISTS cursorDiskKV (key TEXT UNIQUE, value BLOB)")
    conn.commit()
    conn.close()


def _check_conflict(
    global_db_path: Path,
    composer_id: str,
    incoming_bubble_ids: set[str],
    incoming_header_ids: Optional[set[str]] = None,
) -> str:
    """Compare local chat state against incoming snapshot.

    Compares both bubble IDs and conversation header IDs to determine
    the relationship. This is necessary because bubbles can exist locally
    (from a previous import) without being listed in the composerData
    headers, making bubble-only comparison misleading.

    Returns one of:
      "new"            - chat doesn't exist locally
      "identical"      - same messages in both
      "incoming_newer" - incoming has content the local doesn't
      "local_ahead"    - local has all incoming content plus more
      "diverged"       - both have content the other doesn't
    """
    if not global_db_path.exists():
        return "new"

    with db.CursorDB(global_db_path) as cdb:
        local_keys = cdb.list_keys(f"bubbleId:{composer_id}:")
        local_data = cdb.get_json(f"composerData:{composer_id}")

    if not local_keys:
        return "new"

    if not incoming_bubble_ids:
        return "local_ahead"

    prefix_len = len(f"bubbleId:{composer_id}:")
    local_bubble_ids = {k[prefix_len:] for k in local_keys}

    local_only_bubbles = local_bubble_ids - incoming_bubble_ids
    incoming_only_bubbles = incoming_bubble_ids - local_bubble_ids

    # Also compare headers if provided
    local_header_ids = set()
    if local_data:
        local_header_ids = {
            h.get("bubbleId") for h in local_data.get("fullConversationHeadersOnly", [])
            if h.get("bubbleId")
        }
    incoming_only_headers = set()
    if incoming_header_ids:
        incoming_only_headers = incoming_header_ids - local_header_ids

    has_local_only = bool(local_only_bubbles)
    has_incoming_only = bool(incoming_only_bubbles) or bool(incoming_only_headers)

    if not has_local_only and not has_incoming_only:
        return "identical"
    elif has_local_only and has_incoming_only:
        return "diverged"
    elif has_local_only:
        return "local_ahead"
    else:
        return "incoming_newer"


def import_snapshot(
    snapshot_path: Path,
    target_project_path: str,
    target_workspace_dir: Optional[Path] = None,
    skip_backup: bool = False,
) -> bool:
    """Import a conversation snapshot into Cursor's databases.

    Args:
        snapshot_path: Path to the .json snapshot file.
        target_project_path: The project path on this machine.
        target_workspace_dir: Optional workspace directory to import into.
            If not provided, uses find_or_create_workspace() to find/create one.
        skip_backup: If True, skip creating DB backups (caller handles it).

    Returns True on success, False on failure.
    """
    # Load snapshot
    try:
        snapshot = read_snapshot_file(snapshot_path)
    except (json.JSONDecodeError, OSError, gzip.BadGzipFile) as e:
        print(f"Error reading snapshot: {e}", file=sys.stderr)
        return False

    if snapshot.get("version") not in (1, 2, 3):
        print(f"Error: Unsupported snapshot version: {snapshot.get('version')}", file=sys.stderr)
        return False

    composer_id = snapshot["composerId"]
    source_path = snapshot.get("sourceProjectPath", "")
    target_path = os.path.normpath(target_project_path)

    composer_data = snapshot["composerData"]

    # Skip empty conversations (new-but-never-used chats)
    headers = composer_data.get("fullConversationHeadersOnly", [])
    if not headers and not composer_data.get("name"):
        print(f"  Skipping empty conversation {composer_id[:12]}...")
        return True  # Not an error, just nothing to import

    # Rewrite paths if the project is at a different location
    if source_path and source_path != target_path:
        print(f"  Rewriting paths: {source_path} -> {target_path}")
        composer_data = rewrite_paths(composer_data, source_path, target_path)

    content_blobs = snapshot.get("contentBlobs", {})
    message_contexts = snapshot.get("messageContexts", {})
    bubble_entries = snapshot.get("bubbleEntries", {})
    checkpoints = snapshot.get("checkpoints", {})
    agent_blobs = snapshot.get("agentBlobs", {})

    # ── Conflict check ──────────────────────────────────────────────
    global_db_path = paths.get_global_db_path()
    incoming_bubble_ids = set(bubble_entries.keys())
    incoming_header_ids = {
        h.get("bubbleId") for h in headers if h.get("bubbleId")
    }
    conflict = _check_conflict(
        global_db_path, composer_id, incoming_bubble_ids, incoming_header_ids,
    )
    chat_name = composer_data.get("name", "Untitled")
    source_label = snapshot.get("sourceHost") or snapshot.get("sourceMachine") or "remote"

    if conflict == "local_ahead":
        with db.CursorDB(global_db_path) as cdb:
            ld = cdb.get_json(f"composerData:{composer_id}")
        local_count = len((ld or {}).get("fullConversationHeadersOnly", []))
        snap_count = len(headers)
        print(
            f"  Skipped: \"{chat_name}\" — local has {local_count} msgs, "
            f"snapshot has {snap_count} (local is newer, nothing to import)"
        )
        return True

    if conflict == "identical":
        print(f"  Skipped: \"{chat_name}\" — already up to date ({len(headers)} msgs)")
        return True

    if conflict == "new":
        print(f"  New chat: \"{chat_name}\" ({len(headers)} msgs from {source_label})")

    if conflict == "incoming_newer":
        with db.CursorDB(global_db_path) as cdb:
            ld = cdb.get_json(f"composerData:{composer_id}")
        local_count = len((ld or {}).get("fullConversationHeadersOnly", []))
        snap_count = len(headers)
        print(
            f"  Updating: \"{chat_name}\" — local has {local_count} msgs, "
            f"snapshot has {snap_count} from {source_label}"
        )

    if conflict == "diverged":
        # Both local and incoming have unique messages — they've branched.
        # Keep the local version untouched and import the incoming snapshot
        # as a separate conversation with a new ID and a renamed title.
        new_id = str(uuid.uuid4())
        new_name = f"{chat_name} (from {source_label})"
        composer_data["composerId"] = new_id
        composer_data["name"] = new_name
        composer_id = new_id
        print(
            f"  Diverged: \"{chat_name}\" — local and {source_label} both have unique messages"
        )
        print(
            f"            Importing as separate chat: \"{new_name}\""
        )

    # ── Step 1: Backup global DB ────────────────────────────────────
    if not skip_backup and global_db_path.exists():
        backup_path = db.backup_db(global_db_path)
        print(f"  Backed up global DB to {backup_path.name}")

    # ── Step 2: Write conversation data to global DB ────────────────
    global_cdb = db.CursorDB(global_db_path)
    try:
        # Write the main conversation data
        global_cdb.write_json(f"composerData:{composer_id}", composer_data)

        # Write content blobs
        if content_blobs:
            global_cdb.write_batch(
                [(f"composer.content.{h}", v) for h, v in content_blobs.items()]
            )

        # Write message contexts (batch)
        if message_contexts:
            global_cdb.write_json_batch([
                (f"messageRequestContext:{composer_id}:{msg_key}", context)
                for msg_key, context in message_contexts.items()
            ])

        # Write bubble entries in a single transaction (can be 50K+ entries)
        if bubble_entries:
            if source_path and source_path != target_path:
                bubble_entries = {
                    bid: rewrite_paths(bdata, source_path, target_path)
                    for bid, bdata in bubble_entries.items()
                }
            global_cdb.write_json_batch([
                (f"bubbleId:{composer_id}:{bubble_id}", bubble_data)
                for bubble_id, bubble_data in bubble_entries.items()
            ])

        # Write checkpoint data (workspace state snapshots for agent continuation)
        if checkpoints:
            if source_path and source_path != target_path:
                checkpoints = {
                    cp_id: rewrite_paths(cp_data, source_path, target_path)
                    for cp_id, cp_data in checkpoints.items()
                }
            global_cdb.write_json_batch([
                (f"checkpointId:{composer_id}:{cp_id}", cp_data)
                for cp_id, cp_data in checkpoints.items()
            ])

        # Write agent state blobs (encrypted context for conversation continuation)
        if agent_blobs:
            import base64
            global_cdb.write_batch([
                (f"agentKv:blob:{bid}", base64.b64decode(bdata))
                for bid, bdata in agent_blobs.items()
            ])
    finally:
        global_cdb.close()

    # ── Step 3: Register conversation in workspace DB ───────────────
    if target_workspace_dir is not None:
        ws_dir = target_workspace_dir
    else:
        ws_dir = find_or_create_workspace(target_path)
    ws_db_path = ws_dir / "state.vscdb"

    if not skip_backup and ws_db_path.exists():
        backup_path = db.backup_db(ws_db_path)
        print(f"  Backed up workspace DB to {backup_path.name}")

    ws_cdb = db.CursorDB(ws_db_path)
    try:
        # Read existing composer list
        existing = ws_cdb.get_json("composer.composerData", table="ItemTable")
        if existing is None:
            existing = {"allComposers": [], "selectedComposerIds": []}

        # Check if this conversation is already registered
        all_composers = existing.get("allComposers", [])
        existing_ids = {c.get("composerId") for c in all_composers}

        if composer_id not in existing_ids:
            all_composers.append({
                "type": "head",
                "composerId": composer_id,
                "lastUpdatedAt": composer_data.get("lastUpdatedAt", composer_data.get("createdAt", 0)),
                "createdAt": composer_data.get("createdAt", 0),
                "unifiedMode": composer_data.get("unifiedMode", "agent"),
                "forceMode": composer_data.get("forceMode", ""),
                "hasUnreadMessages": False,
                "totalLinesAdded": composer_data.get("totalLinesAdded", 0),
                "totalLinesRemoved": composer_data.get("totalLinesRemoved", 0),
                "filesChangedCount": composer_data.get("filesChangedCount", 0),
                "subtitle": composer_data.get("subtitle", ""),
                "isArchived": False,
                "isDraft": False,
                "isWorktree": False,
                "isSpec": False,
                "isBestOfNSubcomposer": False,
                "numSubComposers": len(composer_data.get("subComposerIds", [])),
                "referencedPlans": [],
                "name": composer_data.get("name", "Imported conversation"),
            })
            existing["allComposers"] = all_composers

        # Set as selected so it shows up in the sidebar
        selected = existing.get("selectedComposerIds", [])
        if composer_id not in selected:
            selected.append(composer_id)
            existing["selectedComposerIds"] = selected

        # Ensure migration flags are set (Cursor expects these)
        existing.setdefault("hasMigratedComposerData", True)
        existing.setdefault("hasMigratedMultipleComposers", True)

        ws_cdb.write_json("composer.composerData", existing, table="ItemTable")
    finally:
        ws_cdb.close()

    # ── Step 4: Verify writes ─────────────────────────────────────────
    verify_cdb = db.CursorDB(global_db_path)
    try:
        written = verify_cdb.get_json(f"composerData:{composer_id}")
        if not written:
            print("  WARNING: composerData not found in global DB after write!", file=sys.stderr)
            return False
        if bubble_entries:
            sample_key = next(iter(bubble_entries))
            sample = verify_cdb.get_json(f"bubbleId:{composer_id}:{sample_key}")
            if not sample:
                print("  WARNING: bubble entries not found in global DB after write!", file=sys.stderr)
                return False

        final_name = composer_data.get("name", chat_name)
        final_msgs = len(written.get("fullConversationHeadersOnly", []))
        if conflict == "new":
            print(f"  Imported: \"{final_name}\" ({final_msgs} msgs, {len(bubble_entries)} bubbles)")
        elif conflict == "diverged":
            print(f"  Copied: \"{final_name}\" ({final_msgs} msgs) — original \"{chat_name}\" left unchanged")
        elif conflict == "incoming_newer":
            print(f"  Updated: \"{final_name}\" → {final_msgs} msgs")
        else:
            print(f"  Done: \"{final_name}\" ({final_msgs} msgs)")
    finally:
        verify_cdb.close()

    return True


def get_sync_status_for_snapshot(
    composer_id: str,
    snapshot_msg_count: int,
    _cdb: "Optional[db.CursorDB]" = None,
) -> str:
    """Lightweight sync status check using only message counts.

    Compares the local header count against the snapshot's messageCount
    from the .meta.json sidecar (no decompression needed).

    Pass an open CursorDB via _cdb to avoid re-copying the global DB.

    Returns one of:
      "not_local"     - conversation doesn't exist in local DB
      "up_to_date"    - same message count
      "local_ahead"   - local has more messages than snapshot
      "behind"        - snapshot has more messages than local
    """
    if _cdb is not None:
        local_data = _cdb.get_json(f"composerData:{composer_id}")
    else:
        global_db_path = paths.get_global_db_path()
        if not global_db_path.exists():
            return "not_local"
        with db.CursorDB(global_db_path) as cdb:
            local_data = cdb.get_json(f"composerData:{composer_id}")

    if not local_data:
        return "not_local"

    local_count = len(local_data.get("fullConversationHeadersOnly", []))

    if local_count == snapshot_msg_count:
        return "up_to_date"
    elif local_count > snapshot_msg_count:
        return "local_ahead"
    else:
        return "behind"


def get_push_status_for_conversation(
    composer_id: str,
    project_identifier: str,
    _cdb: "Optional[db.CursorDB]" = None,
) -> str:
    """Check whether a local conversation has been pushed and if the snapshot is current.

    Pass an open CursorDB via _cdb to avoid re-copying the global DB.

    Returns one of:
      "never_pushed"  - no snapshot exists for this conversation
      "up_to_date"    - snapshot matches local message count
      "local_ahead"   - local has more messages than the snapshot
      "behind"        - snapshot has more messages (pushed from elsewhere)
    """
    snapshots_dir = paths.get_snapshots_dir()
    project_dir = snapshots_dir / project_identifier
    if not project_dir.exists():
        return "never_pushed"

    meta_path = project_dir / f"{composer_id}.meta.json"
    if not meta_path.exists():
        return "never_pushed"

    try:
        meta = json.loads(meta_path.read_text())
    except (json.JSONDecodeError, OSError):
        return "never_pushed"

    snapshot_count = meta.get("messageCount", 0)

    if _cdb is not None:
        local_data = _cdb.get_json(f"composerData:{composer_id}")
    else:
        global_db_path = paths.get_global_db_path()
        with db.CursorDB(global_db_path) as cdb:
            local_data = cdb.get_json(f"composerData:{composer_id}")

    if not local_data:
        return "never_pushed"

    local_count = len(local_data.get("fullConversationHeadersOnly", []))

    if local_count == snapshot_count:
        return "up_to_date"
    elif local_count > snapshot_count:
        return "local_ahead"
    else:
        return "behind"


_SYNC_STATUS_LABELS = {
    "not_local": "new",
    "up_to_date": "synced",
    "local_ahead": "ahead",
    "behind": "behind",
    "never_pushed": "not pushed",
}


def format_sync_status(status: str) -> str:
    """Return a short human-readable label for a sync status."""
    return _SYNC_STATUS_LABELS.get(status, status)


def list_snapshot_projects(snapshots_dir: Optional[Path] = None) -> list[dict]:
    """List all project directories in the snapshots store.

    Returns list of dicts with: name, path, count, source_paths (set of
    sourceProjectPath values found in snapshots), sources (set of
    sourceMachine values).
    """
    if snapshots_dir is None:
        snapshots_dir = paths.get_snapshots_dir()

    if not snapshots_dir.exists():
        return []

    projects = []
    for project_dir in sorted(snapshots_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        snapshot_files = list_snapshot_files(project_dir)
        if not snapshot_files:
            continue

        source_paths = set()
        source_machines = set()
        latest_export = None
        for sf in snapshot_files:
            meta = read_snapshot_meta(sf)
            sp = meta.get("sourceProjectPath", "")
            if sp:
                source_paths.add(sp)
            sm = meta.get("sourceMachine", "")
            if sm:
                source_machines.add(sm)
            exported_at = meta.get("exportedAt", "")
            if exported_at and (latest_export is None or exported_at > latest_export):
                latest_export = exported_at

        projects.append({
            "name": project_dir.name,
            "path": project_dir,
            "count": len(snapshot_files),
            "source_paths": source_paths,
            "sources": source_machines,
            "latest_export": latest_export,
        })

    return projects


def find_snapshot_dir_for_project(
    target_project_path: str,
    snapshots_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Find the snapshot directory matching a project path.

    Tries in order:
    1. Exact match by project identifier (git remote URL based)
    2. Basename match (for SSH workspaces where git -C fails locally)
    3. Scan snapshot metadata for matching sourceProjectPath basenames

    Returns the snapshot directory path, or None.
    """
    if snapshots_dir is None:
        snapshots_dir = paths.get_snapshots_dir()

    # 1. Exact match by project identifier
    project_id = paths.get_project_identifier(target_project_path)
    exact = snapshots_dir / project_id
    if exact.exists() and list_snapshot_files(exact):
        return exact

    # 2. Basename match (covers SSH workspace push → local pull)
    basename = os.path.basename(os.path.normpath(target_project_path))
    basename_dir = snapshots_dir / basename
    if basename_dir.exists() and basename_dir != exact and list_snapshot_files(basename_dir):
        return basename_dir

    # 3. Scan snapshot dirs for matching source path basenames
    # This handles the case where the project was pushed from a different
    # machine with a different directory structure but same repo
    for project_dir in snapshots_dir.iterdir():
        if not project_dir.is_dir() or project_dir == exact or project_dir == basename_dir:
            continue
        # Check first snapshot file for a matching source path basename
        for sf in list_snapshot_files(project_dir):
            try:
                data = read_snapshot_file(sf)
                source_path = data.get("sourceProjectPath", "")
                if source_path and os.path.basename(os.path.normpath(source_path)) == basename:
                    return project_dir
            except (json.JSONDecodeError, OSError, gzip.BadGzipFile):
                pass
            break  # Only need to check one file per directory

    return None


def import_from_snapshot_dir(
    snapshot_dir: Path,
    target_project_path: str,
    force: bool = False,
    target_workspace_dir: Optional[Path] = None,
) -> tuple[int, int]:
    """Import all snapshots from a specific snapshot directory.

    Args:
        snapshot_dir: Directory containing snapshot files.
        target_project_path: The project path on this machine.
        force: Suppress Cursor-running warning.
        target_workspace_dir: Optional workspace directory to import into.

    Returns (success_count, failure_count).
    """
    if not force and is_cursor_running():
        print(
            "WARNING: Cursor is running. Close Cursor FIRST (Cmd+Q / quit),\n"
            "then import, then reopen Cursor. If you import while Cursor is\n"
            "running, Cursor will overwrite the sidebar registration on exit\n"
            "and the imported chats will disappear.\n"
            "Use --force to import anyway (not recommended).\n",
            file=sys.stderr,
        )
        return 0, 0

    snapshot_files = list_snapshot_files(snapshot_dir)
    if not snapshot_files:
        return 0, 0

    # Back up DBs once for the entire batch (global DB can be multi-GB)
    global_db_path = paths.get_global_db_path()
    if global_db_path.exists():
        backup_path = db.backup_db(global_db_path)
        print(f"Backed up global DB to {backup_path.name}")

    if target_workspace_dir is not None:
        ws_dir = target_workspace_dir
    else:
        ws_dir = find_or_create_workspace(os.path.normpath(target_project_path))
    ws_db_path = ws_dir / "state.vscdb"
    if ws_db_path.exists():
        backup_path = db.backup_db(ws_db_path)
        print(f"Backed up workspace DB to {backup_path.name}")

    success = 0
    failure = 0

    for sf in snapshot_files:
        print(f"Importing {sf.name}...")
        if import_snapshot(sf, target_project_path, ws_dir, skip_backup=True):
            success += 1
            print(f"  OK")
        else:
            failure += 1
            print(f"  FAILED")

    return success, failure


def import_all_snapshots(
    target_project_path: str,
    snapshots_dir: Optional[Path] = None,
    force: bool = False,
    target_workspace_dir: Optional[Path] = None,
) -> tuple[int, int]:
    """Import all snapshots for a project.

    Args:
        target_project_path: The project path on this machine.
        snapshots_dir: Directory containing snapshot subdirectories.
        force: Suppress Cursor-running warning.
        target_workspace_dir: Optional workspace directory to import into.

    Returns (success_count, failure_count).
    """
    if not force and is_cursor_running():
        print(
            "WARNING: Cursor is running. Close Cursor FIRST (Cmd+Q / quit),\n"
            "then import, then reopen Cursor. If you import while Cursor is\n"
            "running, Cursor will overwrite the sidebar registration on exit\n"
            "and the imported chats will disappear.\n"
            "Use --force to import anyway (not recommended).\n",
            file=sys.stderr,
        )
        return 0, 0

    if snapshots_dir is None:
        snapshots_dir = paths.get_snapshots_dir()

    project_snapshots = find_snapshot_dir_for_project(target_project_path, snapshots_dir)

    if not project_snapshots:
        project_id = paths.get_project_identifier(target_project_path)
        print(f"No snapshots found for project '{project_id}'", file=sys.stderr)
        print(f"Run 'cursaves snapshots' to see available snapshot projects.", file=sys.stderr)
        return 0, 0

    project_id = paths.get_project_identifier(target_project_path)
    if project_snapshots.name != project_id:
        print(
            f"Note: Matched snapshots at {project_snapshots.name}/ "
            f"(looked for {project_id})",
            file=sys.stderr,
        )

    return import_from_snapshot_dir(
        project_snapshots, target_project_path, force=force,
        target_workspace_dir=target_workspace_dir,
    )


# ── Local workspace copy ───────────────────────────────────────────────


def _register_in_workspace(
    composer_id: str,
    composer_data: dict,
    ws_dir: Path,
) -> bool:
    """Register a conversation in a workspace's sidebar.

    The conversation data must already exist in the global DB.
    This only updates the workspace DB's allComposers list.
    """
    ws_db_path = ws_dir / "state.vscdb"
    ws_cdb = db.CursorDB(ws_db_path)
    try:
        existing = ws_cdb.get_json("composer.composerData", table="ItemTable")
        if existing is None:
            existing = {"allComposers": [], "selectedComposerIds": []}

        all_composers = existing.get("allComposers", [])
        existing_ids = {c.get("composerId") for c in all_composers}

        if composer_id in existing_ids:
            return True  # Already registered

        all_composers.append({
            "type": "head",
            "composerId": composer_id,
            "lastUpdatedAt": composer_data.get("lastUpdatedAt", composer_data.get("createdAt", 0)),
            "createdAt": composer_data.get("createdAt", 0),
            "unifiedMode": composer_data.get("unifiedMode", "agent"),
            "forceMode": composer_data.get("forceMode", ""),
            "hasUnreadMessages": False,
            "totalLinesAdded": composer_data.get("totalLinesAdded", 0),
            "totalLinesRemoved": composer_data.get("totalLinesRemoved", 0),
            "filesChangedCount": composer_data.get("filesChangedCount", 0),
            "subtitle": composer_data.get("subtitle", ""),
            "isArchived": False,
            "isDraft": False,
            "isWorktree": False,
            "isSpec": False,
            "isBestOfNSubcomposer": False,
            "numSubComposers": len(composer_data.get("subComposerIds", [])),
            "referencedPlans": [],
            "name": composer_data.get("name", "Imported conversation"),
        })
        existing["allComposers"] = all_composers

        selected = existing.get("selectedComposerIds", [])
        if composer_id not in selected:
            selected.append(composer_id)
            existing["selectedComposerIds"] = selected

        existing.setdefault("hasMigratedComposerData", True)
        existing.setdefault("hasMigratedMultipleComposers", True)

        ws_cdb.write_json("composer.composerData", existing, table="ItemTable")
        return True
    finally:
        ws_cdb.close()


def copy_between_workspaces(
    composer_ids: list[str],
    source_ws_dir: Path,
    target_ws_dir: Path,
    source_path: str,
    target_path: str,
    force: bool = False,
) -> tuple[int, int]:
    """Deep copy conversations between workspaces on the same machine.

    Creates independent copies with new composerIds and rewrites file
    paths from source to target workspace.

    Returns (success_count, failure_count).
    """
    if not force and is_cursor_running():
        print(
            "WARNING: Cursor is running. Close Cursor FIRST (Cmd+Q / quit),\n"
            "then run this command, then reopen Cursor.\n"
            "Use --force to override (not recommended).\n",
            file=sys.stderr,
        )
        return 0, 0

    global_db_path = paths.get_global_db_path()
    source_norm = os.path.normpath(source_path)
    target_norm = os.path.normpath(target_path)
    needs_rewrite = source_norm != target_norm
    success = 0
    failure = 0

    # Read target workspace's existing chats for conflict detection
    target_db_path = target_ws_dir / "state.vscdb"
    target_names = {}
    if target_db_path.exists():
        with db.CursorDB(target_db_path) as tcdb:
            target_data = tcdb.get_json("composer.composerData", table="ItemTable")
            if target_data:
                for c in target_data.get("allComposers", []):
                    cid = c.get("composerId")
                    if cid:
                        target_names[cid] = c.get("name", "Untitled")

    # Read source data and write copies
    read_cdb = db.CursorDB(global_db_path)
    write_cdb = db.CursorDB(global_db_path)
    try:
        for old_id in composer_ids:
            composer_data = read_cdb.get_json(f"composerData:{old_id}")
            if not composer_data:
                print(f"  {old_id[:12]}... not found in global DB", file=sys.stderr)
                failure += 1
                continue

            name = composer_data.get("name", "Untitled")

            # Check for same-name conflict in target
            existing_same_name = [n for n in target_names.values() if n == name]
            if existing_same_name:
                print(f"  Note: target already has a chat named \"{name}\"")

            # Deep copy: new ID, rewrite paths, duplicate all data
            new_id = str(uuid.uuid4())

            # Copy and transform composerData
            new_data = json.loads(json.dumps(composer_data))
            new_data["composerId"] = new_id
            if needs_rewrite:
                new_data = rewrite_paths(new_data, source_norm, target_norm)
            write_cdb.write_json(f"composerData:{new_id}", new_data)

            # Copy bubble entries
            bubble_keys = read_cdb.list_keys(f"bubbleId:{old_id}:")
            if bubble_keys:
                bubble_items = []
                for key in bubble_keys:
                    bubble_id = key[len(f"bubbleId:{old_id}:"):]
                    val = read_cdb.get_json(key)
                    if val:
                        if needs_rewrite:
                            val = rewrite_paths(val, source_norm, target_norm)
                        bubble_items.append((f"bubbleId:{new_id}:{bubble_id}", val))
                if bubble_items:
                    write_cdb.write_json_batch(bubble_items)

            # Copy message contexts
            ctx_keys = read_cdb.list_keys(f"messageRequestContext:{old_id}:")
            if ctx_keys:
                ctx_items = []
                for key in ctx_keys:
                    msg_key = key[len(f"messageRequestContext:{old_id}:"):]
                    val = read_cdb.get_json(key)
                    if val:
                        ctx_items.append((f"messageRequestContext:{new_id}:{msg_key}", val))
                if ctx_items:
                    write_cdb.write_json_batch(ctx_items)

            # Copy checkpoint data
            cp_keys = read_cdb.list_keys(f"checkpointId:{old_id}:")
            if cp_keys:
                cp_items = []
                for key in cp_keys:
                    cp_id = key[len(f"checkpointId:{old_id}:"):]
                    val = read_cdb.get_json(key)
                    if val:
                        if needs_rewrite:
                            val = rewrite_paths(val, source_norm, target_norm)
                        cp_items.append((f"checkpointId:{new_id}:{cp_id}", val))
                if cp_items:
                    write_cdb.write_json_batch(cp_items)

            # Register in target workspace
            if _register_in_workspace(new_id, new_data, target_ws_dir):
                if needs_rewrite:
                    print(f"  Copied: {name} (paths rewritten)")
                else:
                    print(f"  Copied: {name}")
                target_names[new_id] = name
                success += 1
            else:
                print(f"  Failed: {name}", file=sys.stderr)
                failure += 1
    finally:
        read_cdb.close()
        write_cdb.close()

    return success, failure


# ── Blob repair ──────────────────────────────────────────────────────────


def repair_missing_blobs(verbose: bool = False) -> tuple[int, int]:
    """Scan all conversations for missing agentKv blobs and backfill from snapshots.

    Returns (conversations_repaired, blobs_restored).
    """
    import base64
    from .export import _extract_agent_blob_ids

    global_db_path = paths.get_global_db_path()
    if not global_db_path.exists():
        return 0, 0

    snapshots_dir = paths.get_snapshots_dir()
    if not snapshots_dir.exists():
        return 0, 0

    # Phase 1: Find conversations with missing blobs
    missing_map: dict[str, set[str]] = {}  # composerId -> set of missing blob hex IDs

    with db.CursorDB(global_db_path) as cdb:
        all_keys = cdb.list_keys("composerData:")
        for key in all_keys:
            cd = cdb.get_json(key)
            if not cd:
                continue
            refs = _extract_agent_blob_ids(cd)
            if not refs:
                continue

            missing = set()
            for bid in refs:
                val = cdb.get_item_binary(f"agentKv:blob:{bid}", table="cursorDiskKV")
                if val is None:
                    missing.add(bid)

            if missing:
                cid = key.split(":", 1)[1]
                missing_map[cid] = missing

    if not missing_map:
        if verbose:
            print("  No conversations with missing blobs.")
        return 0, 0

    all_missing_ids = set()
    for s in missing_map.values():
        all_missing_ids |= s

    if verbose:
        print(f"  {len(missing_map)} conversation(s) with {len(all_missing_ids)} unique missing blob(s)")

    # Phase 2: Scan snapshots that contain agentBlobs (version >= 3).
    # Only decompress snapshots that might contain the missing blobs.
    restored_blobs: dict[str, bytes] = {}

    for project_dir in snapshots_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for sf in list_snapshot_files(project_dir):
            if not all_missing_ids - set(restored_blobs.keys()):
                break

            meta = read_snapshot_meta(sf)
            if meta.get("version", 1) < 3:
                continue

            if verbose:
                print(f"  Scanning: {sf.name}")

            try:
                snap = read_snapshot_file(sf)
            except Exception:
                continue

            snap_blobs = snap.get("agentBlobs", {})
            if not snap_blobs:
                continue

            found_any = False
            for bid, b64val in snap_blobs.items():
                if bid in all_missing_ids and bid not in restored_blobs:
                    try:
                        restored_blobs[bid] = base64.b64decode(b64val)
                        found_any = True
                    except Exception:
                        pass

            if found_any and verbose:
                count = sum(1 for b in snap_blobs if b in all_missing_ids)
                print(f"    Found {count} matching blob(s)")

        if not all_missing_ids - set(restored_blobs.keys()):
            break

    if not restored_blobs:
        if verbose:
            still_missing = len(all_missing_ids)
            print(f"  No matching blobs found in snapshots ({still_missing} still missing)")
        return 0, 0

    # Phase 3: Write restored blobs to the global DB
    backup_path = db.backup_db(global_db_path)
    if verbose:
        print(f"  Backed up global DB to {backup_path.name}")

    with db.CursorDB(global_db_path) as cdb:
        cdb.write_batch([
            (f"agentKv:blob:{bid}", val)
            for bid, val in restored_blobs.items()
        ])

    conversations_fixed = 0
    for cid, missing in missing_map.items():
        if missing & set(restored_blobs.keys()):
            conversations_fixed += 1

    remaining = len(all_missing_ids) - len(restored_blobs)
    if verbose and remaining > 0:
        print(f"  {remaining} blob(s) not found in any snapshot (from conversations not yet pushed)")

    return conversations_fixed, len(restored_blobs)
