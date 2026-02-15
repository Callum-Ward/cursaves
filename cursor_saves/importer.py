"""Import operations -- writes to Cursor's databases with safety checks."""

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

from . import db, paths


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
    force: bool = False,
) -> bool:
    """Import a conversation snapshot into Cursor's databases.

    Args:
        snapshot_path: Path to the .json snapshot file.
        target_project_path: The project path on this machine.
        force: If True, skip the Cursor-running check.

    Returns True on success, False on failure.
    """
    # Safety check
    if not force and is_cursor_running():
        print(
            "Error: Cursor appears to be running. Close Cursor before importing,\n"
            "or use --force to skip this check (not recommended).",
            file=sys.stderr,
        )
        return False

    # Load snapshot
    try:
        snapshot = json.loads(snapshot_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"Error reading snapshot: {e}", file=sys.stderr)
        return False

    if snapshot.get("version") not in (1, 2):
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


def import_all_snapshots(
    target_project_path: str,
    snapshots_dir: Optional[Path] = None,
    force: bool = False,
) -> tuple[int, int]:
    """Import all snapshots for a project.

    Returns (success_count, failure_count).
    """
    if snapshots_dir is None:
        snapshots_dir = paths.get_snapshots_dir()

    project_id = paths.get_project_identifier(target_project_path)
    project_snapshots = snapshots_dir / project_id

    # Fallback: also check the old basename-based directory for v1 snapshots
    if not project_snapshots.exists():
        basename_dir = snapshots_dir / os.path.basename(os.path.normpath(target_project_path))
        if basename_dir.exists() and basename_dir != project_snapshots:
            print(
                f"Note: No snapshots at {project_id}/, "
                f"falling back to {basename_dir.name}/",
                file=sys.stderr,
            )
            project_snapshots = basename_dir

    if not project_snapshots.exists():
        print(f"No snapshots found for project '{project_name}'", file=sys.stderr)
        return 0, 0

    snapshot_files = sorted(project_snapshots.glob("*.json"))
    if not snapshot_files:
        print(f"No snapshot files found in {project_snapshots}", file=sys.stderr)
        return 0, 0

    success = 0
    failure = 0

    for sf in snapshot_files:
        print(f"Importing {sf.name}...")
        if import_snapshot(sf, target_project_path, force=force):
            success += 1
            print(f"  OK")
        else:
            failure += 1
            print(f"  FAILED")

    return success, failure
