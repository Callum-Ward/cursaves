"""Import operations -- writes to Cursor's databases with safety checks."""

import gzip
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

from . import db, paths


def read_snapshot_file(path: Path) -> dict:
    """Read a snapshot file (supports both .json and .json.gz)."""
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    else:
        return json.loads(path.read_text())


def list_snapshot_files(directory: Path) -> list[Path]:
    """List all snapshot files in a directory (both .json and .json.gz)."""
    files = list(directory.glob("*.json")) + list(directory.glob("*.json.gz"))
    return sorted(files)


def is_cursor_running() -> bool:
    """Check if Cursor is currently running."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "Cursor"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        # pgrep not available, try ps
        try:
            result = subprocess.run(
                ["ps", "aux"],
                capture_output=True,
                text=True,
            )
            return "Cursor" in result.stdout
        except FileNotFoundError:
            return False  # Can't check, assume not running


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


def import_snapshot(
    snapshot_path: Path,
    target_project_path: str,
) -> bool:
    """Import a conversation snapshot into Cursor's databases.

    Args:
        snapshot_path: Path to the .json snapshot file.
        target_project_path: The project path on this machine.

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

    # Rewrite paths if the project is at a different location
    composer_data = snapshot["composerData"]
    if source_path and source_path != target_path:
        print(f"  Rewriting paths: {source_path} -> {target_path}")
        composer_data = rewrite_paths(composer_data, source_path, target_path)

    content_blobs = snapshot.get("contentBlobs", {})
    message_contexts = snapshot.get("messageContexts", {})
    bubble_entries = snapshot.get("bubbleEntries", {})

    # ── Step 1: Backup global DB ────────────────────────────────────
    global_db_path = paths.get_global_db_path()
    if global_db_path.exists():
        backup_path = db.backup_db(global_db_path)
        print(f"  Backed up global DB to {backup_path.name}")

    # ── Step 2: Write conversation data to global DB ────────────────
    global_cdb = db.CursorDB(global_db_path)
    try:
        # Write the main conversation data
        global_cdb.write_json(f"composerData:{composer_id}", composer_data)

        # Write content blobs
        for content_hash, content in content_blobs.items():
            global_cdb.write_disk_kv(f"composer.content.{content_hash}", content)

        # Write message contexts
        for msg_key, context in message_contexts.items():
            global_cdb.write_json(
                f"messageRequestContext:{composer_id}:{msg_key}", context
            )

        # Write bubble entries (individual message content - v3 format)
        for bubble_id, bubble_data in bubble_entries.items():
            # Rewrite paths in bubble data too
            if source_path and source_path != target_path:
                bubble_data = rewrite_paths(bubble_data, source_path, target_path)
            global_cdb.write_json(f"bubbleId:{composer_id}:{bubble_id}", bubble_data)
    finally:
        global_cdb.close()

    # ── Step 3: Register conversation in workspace DB ───────────────
    ws_dir = find_or_create_workspace(target_path)
    ws_db_path = ws_dir / "state.vscdb"

    if ws_db_path.exists():
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
            # Add the conversation metadata
            all_composers.append({
                "composerId": composer_id,
                "name": composer_data.get("name", "Imported conversation"),
                "createdAt": composer_data.get("createdAt", 0),
                "lastUpdatedAt": composer_data.get("lastUpdatedAt", 0),
                "unifiedMode": composer_data.get("unifiedMode", "agent"),
                "forceMode": composer_data.get("forceMode", ""),
            })
            existing["allComposers"] = all_composers

        # Set as selected so it shows up in the sidebar
        selected = existing.get("selectedComposerIds", [])
        if composer_id not in selected:
            selected.append(composer_id)
            existing["selectedComposerIds"] = selected

        ws_cdb.write_json("composer.composerData", existing, table="ItemTable")
    finally:
        ws_cdb.close()

    return True


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
            try:
                data = read_snapshot_file(sf)
                sp = data.get("sourceProjectPath", "")
                if sp:
                    source_paths.add(sp)
                sm = data.get("sourceMachine", "")
                if sm:
                    source_machines.add(sm)
                exported_at = data.get("exportedAt", "")
                if exported_at and (latest_export is None or exported_at > latest_export):
                    latest_export = exported_at
            except (json.JSONDecodeError, OSError, gzip.BadGzipFile):
                pass

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
) -> tuple[int, int]:
    """Import all snapshots from a specific snapshot directory.

    Returns (success_count, failure_count).
    """
    if not force and is_cursor_running():
        print(
            "Warning: Cursor is running. Imports will write to the database,\n"
            "but you'll need to restart Cursor (quit and reopen) to see the chats.\n"
            "Use --force to suppress this warning.\n",
            file=sys.stderr,
        )

    snapshot_files = list_snapshot_files(snapshot_dir)
    if not snapshot_files:
        return 0, 0

    success = 0
    failure = 0

    for sf in snapshot_files:
        print(f"Importing {sf.name}...")
        if import_snapshot(sf, target_project_path):
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
) -> tuple[int, int]:
    """Import all snapshots for a project.

    Returns (success_count, failure_count).
    """
    # Warn once if Cursor is running (but proceed anyway)
    if not force and is_cursor_running():
        print(
            "Warning: Cursor is running. Imports will write to the database,\n"
            "but you'll need to restart Cursor (quit and reopen) to see the chats.\n"
            "Use --force to suppress this warning.\n",
            file=sys.stderr,
        )

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

    return import_from_snapshot_dir(project_snapshots, target_project_path, force=force)
