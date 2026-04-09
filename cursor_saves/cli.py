"""CLI entry point for cursaves."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import __version__, db, export, paths
from .backends import GitBackend, S3Backend, SyncBackend, get_backend, load_config, save_config
from .importer import (
    copy_between_workspaces,
    doctor_audit,
    doctor_recover,
    find_snapshot_dir_for_project,
    format_sync_status,
    get_push_status_for_conversation,
    get_sync_status_for_snapshot,
    import_all_snapshots,
    import_from_snapshot_dir,
    import_snapshot,
    list_snapshot_projects,
    list_snapshot_files,
    read_snapshot_file,
    read_snapshot_meta,
    repair_missing_blobs,
)


def _get_snapshot_id(path: Path) -> str:
    """Extract the snapshot ID (composer ID) from a snapshot filename."""
    name = path.name
    if name.endswith(".json.gz"):
        return name[:-8]
    elif name.endswith(".json"):
        return name[:-5]
    return path.stem


def _delete_snapshot(path: Path):
    """Delete a snapshot file (or its shards) and metadata sidecar."""
    sid = _get_snapshot_id(path)
    if path.exists():
        path.unlink()
    # Remove any shard files (*.json.gz.00, .01, ...)
    for shard in path.parent.glob(f"{sid}.json.gz.*"):
        if not shard.name.endswith(".meta.json"):
            shard.unlink()
    meta = path.parent / f"{sid}.meta.json"
    if meta.exists():
        meta.unlink()
from .reload import print_reload_hint
from .watch import watch_loop



def _ensure_synced() -> None:
    """Pull latest from remote to ensure we have the latest state."""
    if paths.is_sync_repo_initialized():
        backend = get_backend()
        snapshots_dir = paths.get_snapshots_dir()
        if backend.has_remote():
            backend.pull(snapshots_dir)


def _resolve_project(args) -> str:
    """Resolve the project path from --workspace, --project, or cwd."""
    if hasattr(args, "workspace") and args.workspace:
        ws = paths.resolve_workspace(args.workspace)
        if ws is None:
            print(
                f"Error: No workspace matching '{args.workspace}'.\n"
                f"Run 'cursaves workspaces' to see available workspaces.",
                file=sys.stderr,
            )
            sys.exit(1)
        return ws["path"]
    return args.project if (hasattr(args, "project") and args.project) else paths.get_project_path()


def _resolve_project_and_workspace(args) -> tuple[str, "Path | None", str | None]:
    """Resolve project path, workspace_dir, and host from --workspace, --project, or cwd.

    When -w is used, returns the specific workspace_dir so operations
    are scoped to that exact workspace (prevents cross-host contamination
    for SSH workspaces with the same remote path).
    """
    if hasattr(args, "workspace") and args.workspace:
        ws = paths.resolve_workspace(args.workspace)
        if ws is None:
            print(
                f"Error: No workspace matching '{args.workspace}'.\n"
                f"Run 'cursaves workspaces' to see available workspaces.",
                file=sys.stderr,
            )
            sys.exit(1)
        return ws["path"], ws["workspace_dir"], ws.get("host")
    project = args.project if (hasattr(args, "project") and args.project) else paths.get_project_path()
    return project, None, None


def _resolve_workspace_for_import(args) -> tuple[str, "Path | None"]:
    """Resolve the project path and optional workspace directory for import.

    When -w is specified, returns (project_path, workspace_dir) so imports go
    directly into that specific workspace. Otherwise returns (project_path, None)
    and the importer will find/create a workspace automatically.
    """
    from pathlib import Path

    if hasattr(args, "workspace") and args.workspace:
        ws = paths.resolve_workspace(args.workspace)
        if ws is None:
            print(
                f"Error: No workspace matching '{args.workspace}'.\n"
                f"Run 'cursaves workspaces' to see available workspaces.",
                file=sys.stderr,
            )
            sys.exit(1)
        return ws["path"], ws["workspace_dir"]

    project_path = args.project if (hasattr(args, "project") and args.project) else paths.get_project_path()
    return project_path, None


def _workspace_sync_summary(ws: dict, _global_cdb: "Optional[db.CursorDB]" = None) -> str:
    """Compute a short sync summary for a workspace.

    Reads the workspace's conversations and checks each against snapshots.
    Pass _global_cdb to avoid re-copying the global DB per workspace.
    Returns a string like "3 synced, 2 not pushed" or "5 synced".
    """
    ws_dir = ws["workspace_dir"]
    db_path = ws_dir / "state.vscdb"
    if not db_path.exists():
        return ""

    try:
        with db.CursorDB(db_path) as cdb:
            data = cdb.get_json("composer.composerData", table="ItemTable")
    except Exception:
        return ""

    if not data:
        return ""

    composers = data.get("allComposers", [])
    if not composers:
        return ""

    project_id = paths.get_project_identifier(ws["path"])

    counts = {"up_to_date": 0, "local_ahead": 0, "behind": 0, "never_pushed": 0}
    for c in composers:
        cid = c.get("composerId")
        if not cid:
            continue
        status = get_push_status_for_conversation(cid, project_id, _cdb=_global_cdb)
        counts[status] = counts.get(status, 0) + 1

    parts = []
    if counts["up_to_date"]:
        parts.append(f"{counts['up_to_date']} synced")
    if counts["local_ahead"]:
        parts.append(f"{counts['local_ahead']} ahead")
    if counts["behind"]:
        parts.append(f"{counts['behind']} behind")
    if counts["never_pushed"]:
        parts.append(f"{counts['never_pushed']} not pushed")

    return ", ".join(parts) if parts else ""


def cmd_workspaces(args):
    """List Cursor workspaces that have conversations."""
    from datetime import datetime, timezone

    workspaces = paths.list_workspaces_with_conversations()
    if not workspaces:
        print("No workspaces with conversations found.")
        return

    print(f"{'#':<4} {'Type':<6} {'Path':<40} {'Host':<12} {'Chats':>5}  {'Sync Status'}")
    print("-" * 105)

    global_db_path = paths.get_global_db_path()
    global_cdb = db.CursorDB(global_db_path) if global_db_path.exists() else None
    try:
        for i, ws in enumerate(workspaces, 1):
            path = ws["path"]
            if len(path) > 38:
                path = "..." + path[-35:]
            host = ws["host"] or ""
            convos = ws.get("conversations", 0)
            sync = _workspace_sync_summary(ws, _global_cdb=global_cdb)

            print(f"{i:<4} {ws['type']:<6} {path:<40} {host:<12} {convos:>5}  {sync}")
    finally:
        if global_cdb:
            global_cdb.close()

    print(f"\n{len(workspaces)} workspace(s) with conversations")
    print("\nUse 'cursaves push -w <number>' to push a specific workspace.")


def _is_remote_path(path: str, source_machine: str) -> bool:
    """Check if a path looks like it came from an SSH remote session."""
    import platform
    
    # If path doesn't exist locally, it's likely remote
    if not os.path.exists(path):
        return True
    
    # On Mac, local paths start with /Users
    if platform.system() == "Darwin" and not path.startswith("/Users"):
        return True
    
    return False


def cmd_snapshots(args):
    """List all snapshot projects available in ~/.cursaves/snapshots/."""
    _ensure_synced()  # Pull latest from remote first
    snapshots_dir = paths.get_snapshots_dir()
    projects = list_snapshot_projects(snapshots_dir)

    if not projects:
        print("No snapshots found in ~/.cursaves/snapshots/")
        print("Run 'cursaves push' to checkpoint and push conversations.")
        return

    global_db_path = paths.get_global_db_path()
    global_cdb = db.CursorDB(global_db_path) if global_db_path.exists() else None
    try:
        for i, p in enumerate(projects, 1):
            name = p["name"]
            print(f"\n  {name}/ ({p['count']} snapshot(s))")

            snapshot_files = list_snapshot_files(p["path"])
            for sf in snapshot_files:
                meta = read_snapshot_meta(sf)
                chat_name = meta.get("name") or "Untitled"
                msgs = meta.get("messageCount", 0)
                exported = (meta.get("exportedAt") or "")[:16] or "unknown"
                source_host = meta.get("sourceHost")
                source = source_host or meta.get("sourceMachine") or "unknown"
                cid = meta.get("composerId")
                if cid:
                    status = get_sync_status_for_snapshot(cid, msgs, _cdb=global_cdb)
                    status_label = f"[{format_sync_status(status)}]"
                else:
                    status_label = ""

                if len(chat_name) > 36:
                    chat_name = chat_name[:33] + "..."
                print(f"    {chat_name:<38} {msgs:>5} msgs  from {source:<16} {status_label}")
    finally:
        if global_cdb:
            global_cdb.close()

    print(f"\n{len(projects)} project(s) with snapshots")
    print(f"\nUse 'cursaves pull -s' to interactively select which to import.")


def cmd_init(args):
    """Initialize cursaves sync — git repo or S3 bucket."""
    sync_dir = paths.get_sync_dir()
    snapshots_dir = sync_dir / "snapshots"
    backend_type = getattr(args, "backend", None) or "git"

    if backend_type == "s3":
        bucket = getattr(args, "bucket", None)
        if not bucket:
            print("Error: --bucket is required for S3 backend.", file=sys.stderr)
            print("  cursaves init --backend s3 --bucket my-cursor-saves", file=sys.stderr)
            sys.exit(1)

        snapshots_dir.mkdir(parents=True, exist_ok=True)

        config = load_config()
        config["backend"] = "s3"
        config.setdefault("s3", {})
        config["s3"]["bucket"] = bucket
        if getattr(args, "prefix", None):
            config["s3"]["prefix"] = args.prefix
        if getattr(args, "region", None):
            config["s3"]["region"] = args.region
        save_config(config)

        backend = S3Backend(
            bucket=bucket,
            prefix=config["s3"].get("prefix", "snapshots/"),
            region=config["s3"].get("region"),
        )

        print(f"Configured S3 backend:")
        print(f"  Bucket: {bucket}")
        print(f"  Prefix: {config['s3'].get('prefix', 'snapshots/')}")
        if config["s3"].get("region"):
            print(f"  Region: {config['s3']['region']}")
        print(f"  Snapshots: {snapshots_dir}")

        # Verify access
        try:
            if backend.is_initialized():
                print(f"\n  Bucket access verified.")
            else:
                print(f"\n  Warning: Could not access bucket '{bucket}'.", file=sys.stderr)
                print(f"  Check your AWS credentials and bucket permissions.", file=sys.stderr)
        except Exception as e:
            print(f"\n  Warning: Could not verify bucket access: {e}", file=sys.stderr)

        print(f"\nDone. Run 'cursaves sync' to synchronize conversations.")
        return

    # Git backend (default / backward-compatible)
    if paths.is_sync_repo_initialized():
        config = load_config()
        if config.get("backend") == "s3":
            print(f"Currently configured with S3 backend (bucket: {config.get('s3', {}).get('bucket')})")
            if args.remote:
                print("Switching to git backend...")
                config["backend"] = "git"
                save_config(config)
            else:
                return

        git_backend = GitBackend(sync_dir)
        print(f"Sync repo already initialized at {sync_dir}")
        if args.remote:
            git_backend.update_remote(args.remote)
            print(f"  Remote updated: {args.remote}")
        return

    print(f"Initializing sync repo at {sync_dir}...")
    git_backend = GitBackend(sync_dir)
    git_backend.init_repo(remote=args.remote)
    print(f"  Created {sync_dir}")

    if args.remote:
        print(f"  Added remote: {args.remote}")
        print(f"\nDone. Run 'cursaves push' from any project directory to start syncing.")
    else:
        print(f"\nDone. To sync between machines, add a remote:")
        print(f"  cursaves init --remote git@github.com:you/my-cursaves.git")
        print(f"  cursaves init --backend s3 --bucket my-cursor-saves")


def cmd_list(args):
    """List conversations for the current project."""
    project_path, workspace_dir, _ = _resolve_project_and_workspace(args)
    conversations = export.list_conversations(project_path, workspace_dir=workspace_dir)

    if not conversations:
        print(f"No conversations found for {project_path}", file=sys.stderr)
        ws_dirs = paths.find_workspace_dirs_for_project(project_path)
        if not ws_dirs:
            print(
                f"\nNo Cursor workspace found for this path. Possible causes:\n"
                f"  - This directory has never been opened in Cursor\n"
                f"  - The path doesn't match exactly (try an absolute path with -p)\n"
                f"  - Cursor data is in a non-standard location",
                file=sys.stderr,
            )
        else:
            print("(Workspace found but contains no conversations.)", file=sys.stderr)
        return

    # JSON output mode
    if args.json:
        print(json.dumps(conversations, indent=2))
        return

    print(f"Conversations for {project_path}\n")
    print(f"{'ID':<40} {'Name':<30} {'Mode':<8} {'Msgs':>5}  {'Last Updated'}")
    print("-" * 110)

    for c in conversations:
        name = c["name"]
        if len(name) > 28:
            name = name[:25] + "..."
        print(
            f"{c['id']:<40} {name:<30} {c['mode']:<8} {c['messageCount']:>5}  {c['lastUpdated']}"
        )

    print(f"\n{len(conversations)} conversation(s) total")


def cmd_export(args):
    """Export a single conversation to a snapshot file."""
    project_path = _resolve_project(args)
    composer_id = args.id

    print(f"Exporting conversation {composer_id}...")
    snapshot = export.export_conversation(project_path, composer_id)

    if snapshot is None:
        print(f"Error: Conversation '{composer_id}' not found.", file=sys.stderr)
        sys.exit(1)

    snapshots_dir = paths.get_snapshots_dir()
    saved_path = export.save_snapshot(snapshot, snapshots_dir)
    print(f"Saved to {saved_path}")

    # Show summary
    data = snapshot["composerData"]
    headers = data.get("fullConversationHeadersOnly", [])
    blobs = snapshot.get("contentBlobs", {})
    print(f"  Messages: {len(headers)}")
    print(f"  Content blobs: {len(blobs)}")
    print(f"  Source: {snapshot['sourceMachine']}")


def cmd_checkpoint(args):
    """Checkpoint all conversations for the current project."""
    project_path, workspace_dir, _ = _resolve_project_and_workspace(args)

    print(f"Checkpointing conversations for {project_path}...")
    saved = export.checkpoint_project(project_path, workspace_dir=workspace_dir)

    if not saved:
        print("No conversations found to checkpoint.")
        return

    print(f"\nCheckpointed {len(saved)} conversation(s):")
    for p in saved:
        print(f"  {p}")

    print(f"\nSnapshots saved to {paths.get_snapshots_dir()}")
    print("Run 'git add snapshots/ && git commit -m \"checkpoint\"' to commit.")


def cmd_import(args):
    """Import conversation snapshots."""
    project_path = _resolve_project(args)

    if args.all:
        print(f"Importing all snapshots for {project_path}...")
        success, failure = import_all_snapshots(
            project_path,
            force=args.force,
        )
        print(f"\nDone: {success} imported, {failure} failed.")
        if success > 0:
            _maybe_reload(args)
    elif args.file:
        snapshot_path = Path(args.file)
        if not snapshot_path.exists():
            print(f"Error: File not found: {snapshot_path}", file=sys.stderr)
            sys.exit(1)
        print(f"Importing {snapshot_path.name}...")
        if import_snapshot(snapshot_path, project_path):
            print("Done.")
            _maybe_reload(args)
        else:
            print("Import failed.", file=sys.stderr)
            sys.exit(1)
    else:
        print("Error: Specify --all or --file <path>", file=sys.stderr)
        sys.exit(1)


def _select_target_workspaces(source_paths: set[str]) -> list[dict]:
    """Find and optionally prompt user to select target workspaces for import.

    Args:
        source_paths: Set of source project paths from snapshots.

    Returns:
        List of workspace dicts to import into, or empty list if cancelled.
        Each dict has: type, host, path, workspace_dir
    """
    # Find all matching workspaces across all source paths
    all_matches = []
    seen_ws_dirs = set()
    for sp in sorted(source_paths):
        matches = paths.find_all_matching_workspaces(sp)
        for ws in matches:
            ws_dir_str = str(ws["workspace_dir"])
            if ws_dir_str not in seen_ws_dirs:
                seen_ws_dirs.add(ws_dir_str)
                all_matches.append(ws)

    if not all_matches:
        return []

    if len(all_matches) == 1:
        # Single match - use it directly
        ws = all_matches[0]
        display = paths.format_workspace_display(ws)
        print(f"  Target workspace: {display}")
        return [ws]

    # Multiple matches - ask user to select
    print(f"\n  Multiple workspaces match this project:")
    print(f"  {'#':<4} {'Type':<6} {'Host':<15} {'Path'}")
    print(f"  {'-' * 70}")

    for i, ws in enumerate(all_matches, 1):
        host = ws.get("host") or ""
        ws_path = ws["path"]
        if len(ws_path) > 45:
            ws_path = "..." + ws_path[-42:]
        print(f"  {i:<4} {ws['type']:<6} {host:<15} {ws_path}")

    print(f"\n  Select workspace(s) to import into (e.g. 1,2 or 'all'):")
    try:
        choice = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return []

    if not choice:
        return []

    indices = _parse_selection(choice, len(all_matches))
    if not indices:
        return []

    return [all_matches[i - 1] for i in indices]


def _maybe_reload(args):
    """Print restart hint after import."""
    print_reload_hint()


def cmd_reload(args):
    """Print restart instructions."""
    print_reload_hint()


def _require_sync_repo():
    """Check that the sync repo is initialized, exit with help if not.

    Returns the sync directory path (for backward compat with existing callers).
    """
    if not paths.is_sync_repo_initialized():
        print(
            "Error: Sync repo not initialized.\n"
            "Run 'cursaves init' first to set up ~/.cursaves/\n\n"
            "Examples:\n"
            "  cursaves init --remote git@github.com:you/my-cursaves.git\n"
            "  cursaves init --backend s3 --bucket my-cursor-saves",
            file=sys.stderr,
        )
        sys.exit(1)
    return paths.get_sync_dir()


def _parse_selection(choice: str, max_items: int) -> list[int]:
    """Parse a user selection string into a list of 1-based indices.

    Supports: 1,3,5 and 1-3 and combinations like 1-3,5 and 'all'.
    Returns sorted list of valid indices, or empty list on error.
    """
    if choice.lower() == "all":
        return list(range(1, max_items + 1))

    selected = set()
    for part in choice.split(","):
        part = part.strip()
        if "-" in part:
            try:
                start, end = part.split("-", 1)
                for i in range(int(start), int(end) + 1):
                    selected.add(i)
            except ValueError:
                print(f"Invalid range: {part}", file=sys.stderr)
                return []
        else:
            try:
                selected.add(int(part))
            except ValueError:
                print(f"Invalid number: {part}", file=sys.stderr)
                return []

    # Filter to valid range
    valid = sorted(i for i in selected if 1 <= i <= max_items)
    invalid = sorted(i for i in selected if i < 1 or i > max_items)
    for i in invalid:
        print(f"Warning: #{i} out of range, skipping.", file=sys.stderr)

    return valid


def _select_workspace() -> tuple[str, "Path", str | None] | None:
    """Show all Cursor workspaces and let the user pick one.

    Returns (project_path, workspace_dir, host) for the selected workspace, or None.
    """
    workspaces = paths.list_workspaces_with_conversations()
    if not workspaces:
        print("No Cursor workspaces found.")
        return None

    print(f"\nCursor workspaces (most recent first)\n")
    print(f"  {'#':<4} {'Project':<40} {'Chats':>5}  {'Sync Status'}")
    print(f"  {'-' * 80}")

    global_db_path = paths.get_global_db_path()
    global_cdb = db.CursorDB(global_db_path) if global_db_path.exists() else None
    try:
        for i, ws in enumerate(workspaces, 1):
            name = os.path.basename(os.path.normpath(ws["path"])) or ws["path"]
            ws_type = ws.get("type", "local")
            host = ws.get("host", "")
            label = f"{name} ({host})" if host else name
            if len(label) > 38:
                label = label[:35] + "..."
            convos = ws.get("conversations", 0)
            sync = _workspace_sync_summary(ws, _global_cdb=global_cdb)
            print(f"  {i:<4} {label:<40} {convos:>5}  {sync}")
    finally:
        if global_cdb:
            global_cdb.close()

    print(f"\nSelect a workspace:")
    try:
        choice = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None

    if not choice:
        return None

    try:
        idx = int(choice)
    except ValueError:
        print(f"Invalid selection: {choice}", file=sys.stderr)
        return None

    if idx < 1 or idx > len(workspaces):
        print(f"#{idx} out of range.", file=sys.stderr)
        return None

    ws = workspaces[idx - 1]
    return ws["path"], ws["workspace_dir"], ws.get("host")


def _select_conversations(project_path: str, prompt: str = "push", workspace_dir: "Path | None" = None) -> list[str]:
    """Show conversations for a workspace and let the user pick.

    Returns a list of selected composer IDs, or empty list.
    """
    conversations = export.list_conversations(project_path, workspace_dir=workspace_dir)
    if not conversations:
        print(f"No conversations found for {project_path}")
        return []

    conversations.sort(key=lambda c: c.get("lastUpdated", ""), reverse=True)

    project_name = os.path.basename(os.path.normpath(project_path)) or project_path
    project_id = paths.get_project_identifier(project_path)
    print(f"\n  Conversations in {project_name}  ({len(conversations)} total)\n")
    print(f"  {'#':<4} {'Name':<36} {'Msgs':>5}  {'Last Updated':<20} {'Status'}")
    print(f"  {'-' * 95}")

    global_db_path = paths.get_global_db_path()
    with db.CursorDB(global_db_path) as global_cdb:
        for i, c in enumerate(conversations, 1):
            name = c["name"]
            if len(name) > 34:
                name = name[:31] + "..."
            status = get_push_status_for_conversation(c["id"], project_id, _cdb=global_cdb)
            status_label = format_sync_status(status)
            print(
                f"  {i:<4} {name:<36} {c['messageCount']:>5}  {c['lastUpdated']:<20} {status_label}"
            )

    print(f"\n  Select chats to {prompt} (e.g. 1,3,5 or 1-3 or 'all') [all]:")
    try:
        choice = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return []

    if not choice:
        choice = "all"

    indices = _parse_selection(choice, len(conversations))
    return [conversations[i - 1]["id"] for i in indices]


def _find_ahead_conversations() -> list[dict]:
    """Scan all workspaces for conversations that are ahead of their snapshots."""
    workspaces = paths.list_workspaces_with_conversations()
    ahead_items: list[dict] = []

    global_db_path = paths.get_global_db_path()
    if not global_db_path.exists():
        return ahead_items

    with db.CursorDB(global_db_path) as global_cdb:
        for ws in workspaces:
            ws_dir = ws["workspace_dir"]
            db_path = ws_dir / "state.vscdb"
            if not db_path.exists():
                continue

            try:
                with db.CursorDB(db_path) as cdb:
                    data = cdb.get_json("composer.composerData", table="ItemTable")
            except Exception:
                continue
            if not data:
                continue

            project_id = paths.get_project_identifier(ws["path"])
            composers = data.get("allComposers", [])

            for c in composers:
                cid = c.get("composerId")
                if not cid:
                    continue
                status = get_push_status_for_conversation(cid, project_id, _cdb=global_cdb)
                if status == "local_ahead":
                    ws_name = os.path.basename(os.path.normpath(ws["path"])) or ws["path"]
                    host = ws.get("host", "")
                    ws_label = f"{ws_name} ({host})" if host else ws_name
                    ahead_items.append({
                        "composerId": cid,
                        "name": c.get("name", "Untitled"),
                        "workspace_label": ws_label,
                        "workspace_dir": ws_dir,
                        "project_path": ws["path"],
                        "host": host,
                    })

    return ahead_items


def _export_and_push(sync_dir: Path, items: list[dict], backend: Optional[SyncBackend] = None) -> int:
    """Export a list of ahead conversation items and push via the backend.

    Returns the number of conversations successfully exported.
    """
    from collections import defaultdict

    by_workspace: dict[tuple, list[dict]] = defaultdict(list)
    for item in items:
        key = (item["project_path"], str(item["workspace_dir"]))
        by_workspace[key].append(item)

    total_saved = 0
    for (project_path, ws_dir_str), ws_items in by_workspace.items():
        ws_dir = Path(ws_dir_str)
        host = ws_items[0].get("host")
        composer_ids = [it["composerId"] for it in ws_items]
        saved = export.checkpoint_project(
            project_path,
            composer_ids=composer_ids,
            workspace_dir=ws_dir,
            source_host=host or None,
        )
        total_saved += len(saved)

    if total_saved == 0:
        return 0

    if backend is None:
        backend = get_backend()

    snapshots_dir = paths.get_snapshots_dir()
    if backend.has_remote():
        print("  Pushing...", end="", flush=True)
        if backend.push(snapshots_dir):
            print(" done")
        else:
            print(" failed", file=sys.stderr)

    return total_saved


def _push_ahead(sync_dir: Path, auto: bool = False, backend: Optional[SyncBackend] = None) -> int:
    """Find conversations ahead of snapshots and push them.

    Args:
        sync_dir: The sync repo directory.
        auto: If True, skip prompts and push all ahead conversations.
        backend: Sync backend to use for push. Auto-detected if None.

    Returns the number of conversations pushed.
    """
    if backend is None:
        backend = get_backend()

    if not auto:
        if backend.has_remote():
            snapshots_dir = paths.get_snapshots_dir()
            print("Syncing with remote...", end="", flush=True)
            if backend.pull(snapshots_dir):
                print(" done")
            else:
                print(" failed (continuing with local state)", file=sys.stderr)

    ahead_items = _find_ahead_conversations()

    if not ahead_items:
        if not auto:
            print("All synced conversations are up to date.")
        return 0

    if auto:
        print(f"  Pushing {len(ahead_items)} ahead conversation(s)...")
        for item in ahead_items:
            name = item["name"]
            if len(name) > 40:
                name = name[:37] + "..."
            print(f"    {name} [{item['workspace_label']}]")
        total = _export_and_push(sync_dir, ahead_items, backend=backend)
        return total

    print(f"\n  {len(ahead_items)} conversation(s) ahead of snapshots:\n")
    print(f"  {'#':<4} {'Name':<36} {'Workspace'}")
    print(f"  {'-' * 70}")

    for i, item in enumerate(ahead_items, 1):
        name = item["name"]
        if len(name) > 34:
            name = name[:31] + "..."
        ws_label = item["workspace_label"]
        if len(ws_label) > 30:
            ws_label = ws_label[:27] + "..."
        print(f"  {i:<4} {name:<36} {ws_label}")

    print(f"\n  Push these? (e.g. 1,3,5 or 1-3 or 'all') [all]:")
    try:
        choice = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return 0

    if not choice:
        choice = "all"

    indices = _parse_selection(choice, len(ahead_items))
    if not indices:
        print("No conversations selected.")
        return 0

    selected = [ahead_items[i - 1] for i in indices]
    total = _export_and_push(sync_dir, selected, backend=backend)

    if total == 0:
        print("No conversations exported.")
    else:
        print(f"\n  {total} conversation(s) checkpointed")

    print(f"\nDone. {total} conversation(s) pushed.")
    return total


def _get_sync_state_path() -> Path:
    """Path for local sync state (outside the git repo to survive git clean)."""
    return Path.home() / ".config" / "cursaves" / "sync_state.json"


def _load_sync_state() -> dict:
    """Load the local sync state (tracks diverged snapshots to avoid re-importing)."""
    state_path = _get_sync_state_path()
    if state_path.exists():
        try:
            return json.loads(state_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_sync_state(state: dict):
    """Persist the local sync state."""
    state_path = _get_sync_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2))


def _pull_behind(sync_dir: Path) -> int:
    """Find all snapshots where local is behind and import them automatically.

    For each behind/new snapshot, finds workspaces that already have the
    conversation registered and imports only into those.  This prevents
    duplicating imports across every matching workspace.

    Returns the number of snapshots successfully imported.
    """
    projects = list_snapshot_projects()
    if not projects:
        return 0

    global_db_path = paths.get_global_db_path()
    global_cdb = db.CursorDB(global_db_path) if global_db_path.exists() else None

    sync_state = _load_sync_state()
    handled = sync_state.get("handled_diverged", {})

    total_imported = 0
    backed_up_global = False
    backed_up_ws: set[str] = set()

    try:
        for project in projects:
            snapshot_files = list_snapshot_files(project["path"])
            if not snapshot_files:
                continue

            behind_snapshots: list[tuple[Path, dict]] = []
            for sf in snapshot_files:
                meta = read_snapshot_meta(sf)
                cid = meta.get("composerId")
                if not cid:
                    continue

                # Skip snapshots we've already handled as diverged
                msg_count = meta.get("messageCount", 0)
                prev_handled = handled.get(cid)
                if prev_handled and prev_handled >= msg_count:
                    continue

                status = get_sync_status_for_snapshot(cid, msg_count, _cdb=global_cdb)
                if status in ("behind", "not_local"):
                    behind_snapshots.append((sf, meta))

            if not behind_snapshots:
                continue

            # Find all matching workspaces for this project
            all_matches = []
            seen_ws_dirs: set[str] = set()
            for sp in sorted(project.get("source_paths", set())):
                matches = paths.find_all_matching_workspaces(sp)
                for ws in matches:
                    ws_dir_str = str(ws["workspace_dir"])
                    if ws_dir_str not in seen_ws_dirs:
                        seen_ws_dirs.add(ws_dir_str)
                        all_matches.append(ws)

            if not all_matches:
                continue

            # Build a map: composerId -> list of workspaces that have it registered
            cid_to_workspaces: dict[str, list[dict]] = {}
            for ws in all_matches:
                ws_db_path = ws["workspace_dir"] / "state.vscdb"
                if not ws_db_path.exists():
                    continue
                try:
                    with db.CursorDB(ws_db_path) as wdb:
                        ws_data = wdb.get_json("composer.composerData", table="ItemTable")
                except Exception:
                    continue
                if not ws_data:
                    continue
                registered_ids = {
                    c.get("composerId")
                    for c in ws_data.get("allComposers", [])
                    if c.get("composerId")
                }
                for sf, meta in behind_snapshots:
                    cid = meta.get("composerId", "")
                    if cid in registered_ids:
                        cid_to_workspaces.setdefault(cid, []).append(ws)

            for sf, meta in behind_snapshots:
                cid = meta.get("composerId", "")
                target_list = cid_to_workspaces.get(cid, [])

                if not target_list:
                    # Not registered anywhere — pick the first matching workspace
                    target_list = all_matches[:1]

                for ws in target_list:
                    if not backed_up_global and global_db_path.exists():
                        db.backup_db(global_db_path)
                        backed_up_global = True

                    ws_dir_str = str(ws["workspace_dir"])
                    if ws_dir_str not in backed_up_ws:
                        ws_db_path = ws["workspace_dir"] / "state.vscdb"
                        if ws_db_path.exists():
                            db.backup_db(ws_db_path)
                        backed_up_ws.add(ws_dir_str)

                    ok = import_snapshot(
                        sf, ws["path"],
                        target_workspace_dir=ws["workspace_dir"],
                        skip_backup=True,
                    )
                    if ok:
                        total_imported += 1

                # Record that we've handled this snapshot at this message count
                # so diverged conversations don't get re-imported every sync
                msg_count = meta.get("messageCount", 0)
                handled[cid] = msg_count
    finally:
        if global_cdb:
            global_cdb.close()

    # Persist sync state so handled diverged snapshots are remembered
    sync_state["handled_diverged"] = handled
    _save_sync_state(sync_state)

    return total_imported


def cmd_repair(args):
    """Repair conversations with missing agent blobs by restoring from snapshots."""
    print("Scanning for missing blobs...")
    fixed, restored = repair_missing_blobs(verbose=True)
    if fixed > 0:
        print(f"\nRepaired {fixed} conversation(s), restored {restored} blob(s).")
        print("Restart Cursor to apply fixes.")
    elif restored == 0 and fixed == 0:
        print("\nNo blobs could be restored from available snapshots.")
        print("To fix remaining conversations, re-push them from the original machine")
        print("using the latest cursaves (which exports agent blobs).")


def cmd_sync(args):
    """Pull behind conversations then push ahead ones — fully automatic."""
    sync_dir = _require_sync_repo()
    backend = get_backend()
    snapshots_dir = paths.get_snapshots_dir()

    # Step 1: Pull remote → local snapshots
    if backend.has_remote():
        print("Syncing with remote...", end="", flush=True)
        if backend.pull(snapshots_dir):
            print(" done")
        else:
            print(" failed", file=sys.stderr)
            return

    # Step 2: Import — pull behind conversations from snapshots into Cursor DBs
    print("\n── Pull ──")
    imported = _pull_behind(sync_dir)
    if imported > 0:
        print(f"  Imported {imported} conversation(s)")
    else:
        print("  Everything up to date")

    # Step 3: Push — export ahead conversations from Cursor DBs into snapshots
    print("\n── Push ──")
    pushed = _push_ahead(sync_dir, auto=True, backend=backend)
    if pushed == 0:
        print("  Nothing to push")

    # Summary
    print()
    if imported > 0 or pushed > 0:
        parts = []
        if imported > 0:
            parts.append(f"{imported} pulled")
        if pushed > 0:
            parts.append(f"{pushed} pushed")
        print(f"Sync complete: {', '.join(parts)}.")
        if imported > 0:
            print("Restart Cursor to see imported chats.")
    else:
        print("Already in sync.")


def cmd_push(args):
    """Checkpoint + push in one command."""
    sync_dir = _require_sync_repo()
    backend = get_backend()
    snapshots_dir = paths.get_snapshots_dir()

    if getattr(args, "ahead", False):
        _push_ahead(sync_dir, backend=backend)
        return

    # Step 0: Pull latest from remote
    if backend.has_remote():
        if not backend.pull(snapshots_dir):
            print("Warning: Could not sync with remote, continuing anyway...", file=sys.stderr)

    # Resolve workspace and select conversations
    composer_ids = None
    workspace_dir = None
    source_host = None
    if args.select:
        result = _select_workspace()
        if not result:
            return
        project_path, workspace_dir, source_host = result
    else:
        project_path, workspace_dir, source_host = _resolve_project_and_workspace(args)

    # Always show conversation list for selection (unless --all flag)
    if not getattr(args, "all_chats", False):
        composer_ids = _select_conversations(project_path, prompt="push", workspace_dir=workspace_dir)
        if not composer_ids:
            print("No conversations selected.")
            return

    # Step 1: Checkpoint
    if composer_ids:
        print(f"\nCheckpointing {len(composer_ids)} conversation(s)...")
    else:
        print(f"Checkpointing all conversations for {project_path}...")
    saved = export.checkpoint_project(
        project_path, composer_ids=composer_ids,
        workspace_dir=workspace_dir, source_host=source_host,
    )

    if not saved:
        print("No conversations found to checkpoint.")
        return

    print(f"  {len(saved)} conversation(s) checkpointed")

    # Step 2: Push to remote
    if backend.has_remote():
        print("  Pushing...", end="", flush=True)
        if backend.push(snapshots_dir):
            print(" done")
        else:
            print(" failed", file=sys.stderr)
    else:
        print("  No remote configured, skipping push")

    print(f"\nDone. {len(saved)} conversation(s) saved.")


def _git_pull_quiet(sync_dir: Path) -> bool:
    """Pull from remote without printing status. Returns True on success."""
    backend = get_backend()
    snapshots_dir = paths.get_snapshots_dir()
    return backend.pull(snapshots_dir)


def _commit_and_push(sync_dir: Path, message: str) -> bool:
    """Push snapshot changes to the remote backend. Returns True on success."""
    backend = get_backend()
    snapshots_dir = paths.get_snapshots_dir()
    if backend.has_remote():
        return backend.push(snapshots_dir)
    return True


def _backend_pull() -> bool:
    """Pull latest snapshots from the configured backend."""
    backend = get_backend()
    snapshots_dir = paths.get_snapshots_dir()

    if not backend.has_remote():
        print("No remote configured, importing from local snapshots only.")
        return True

    print("Syncing with remote...", end="", flush=True)
    if backend.pull(snapshots_dir):
        print(" done")
        return True
    else:
        print(" failed", file=sys.stderr)
        return False


def cmd_pull(args):
    """Pull + import snapshots in one command."""
    sync_dir = _require_sync_repo()

    # Step 1: Pull from remote
    if not _backend_pull():
        return

    # Step 2: Select what to import
    if args.select:
        # Interactive: show available snapshot projects and let user pick
        projects = list_snapshot_projects()
        if not projects:
            print("No snapshots found. Run 'cursaves push' on another machine first.")
            return

        print(f"\n  Available projects:\n")
        print(f"  {'#':<4} {'Project':<30} {'Chats':>5}  {'Last Saved':<20} {'Source'}")
        print(f"  {'-' * 85}")

        for i, p in enumerate(projects, 1):
            sources = ", ".join(sorted(p["sources"])) or "unknown"
            name = p["name"]
            if len(name) > 28:
                name = name[:25] + "..."
            last_saved = p.get("latest_export", "")[:16] or "unknown"
            print(f"  {i:<4} {name:<30} {p['count']:>5}  {last_saved:<20} {sources}")

        print(f"\n  Select project (e.g. 1):")
        try:
            choice = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not choice:
            return

        indices = _parse_selection(choice, len(projects))
        if not indices:
            return

        total_success = 0
        total_failure = 0
        for idx in indices:
            project = projects[idx - 1]

            # Show individual snapshots with dates for this project
            snapshot_files = list_snapshot_files(project["path"])
            snapshots_info = []
            for sf in snapshot_files:
                meta = read_snapshot_meta(sf)
                source_host = meta.get("sourceHost")
                snapshots_info.append({
                    "file": sf,
                    "composerId": meta.get("composerId"),
                    "name": meta.get("name") or "Untitled",
                    "msgs": meta.get("messageCount", 0),
                    "exported": (meta.get("exportedAt") or "")[:16] or "unknown",
                    "source": source_host or meta.get("sourceMachine") or "unknown",
                })

            if not snapshots_info:
                print(f"  No snapshots in {project['name']}/")
                continue

            print(f"\n  Chats in {project['name']}:\n")
            print(f"  {'#':<4} {'Name':<36} {'Msgs':>5}  {'From':<16} {'Local Status'}")
            print(f"  {'-' * 90}")

            for i, si in enumerate(snapshots_info, 1):
                name = si["name"]
                if len(name) > 34:
                    name = name[:31] + "..."
                cid = si.get("composerId")
                if cid:
                    status = get_sync_status_for_snapshot(cid, si["msgs"])
                    status_label = format_sync_status(status)
                else:
                    status_label = ""
                source = si["source"]
                if len(source) > 14:
                    source = source[:11] + "..."
                print(f"  {i:<4} {name:<36} {si['msgs']:>5}  {source:<16} {status_label}")

            print(f"\n  Select chats to import (e.g. 1,3,5 or 1-3 or 'all') [all]:")
            try:
                snap_choice = input("  > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue

            if not snap_choice:
                snap_choice = "all"

            snap_indices = _parse_selection(snap_choice, len(snapshots_info))
            if not snap_indices:
                continue

            selected_files = [snapshots_info[i - 1]["file"] for i in snap_indices]
            selected_names = [snapshots_info[i - 1]["name"] for i in snap_indices]
            print(f"\n  Importing {len(selected_files)} chat(s) from {project['name']}/...")

            # Find target workspace
            target_workspaces = _select_target_workspaces(project["source_paths"])

            if not target_workspaces:
                cwd = os.getcwd()
                cwd_basename = os.path.basename(os.path.normpath(cwd))
                source_basenames = {os.path.basename(os.path.normpath(sp)) for sp in project["source_paths"]}
                if cwd_basename in source_basenames or project["name"] == paths.get_project_identifier(cwd):
                    target_path = cwd
                else:
                    print(f"  No matching workspaces found.")
                    print(f"  Enter a local project path to import into (or press Enter to skip):")
                    try:
                        target_path = input("  > ").strip()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        continue
                    if not target_path:
                        print("  Skipped.")
                        continue

                for sf in selected_files:
                    print(f"  Importing {sf.name}...")
                    if import_snapshot(sf, target_path):
                        total_success += 1
                        print(f"    OK")
                    else:
                        total_failure += 1
                        print(f"    FAILED")
            else:
                for ws in target_workspaces:
                    display = paths.format_workspace_display(ws)
                    print(f"  Importing into: {display}")
                    for sf in selected_files:
                        print(f"    {sf.name}...")
                        if import_snapshot(sf, ws["path"], target_workspace_dir=ws["workspace_dir"]):
                            total_success += 1
                        else:
                            total_failure += 1

        if total_success == 0 and total_failure == 0:
            print("\nNo snapshots imported.")
            return

        print(f"\nDone: {total_success} imported, {total_failure} failed.")
        if total_success > 0:
            _maybe_reload(args)
    else:
        # Non-interactive: import for the resolved project/workspace
        project_path, workspace_dir = _resolve_workspace_for_import(args)
        if workspace_dir:
            # Show which workspace we're importing into
            ws_info = paths.format_workspace_display(
                {"type": "ssh" if "ssh" in str(workspace_dir) else "local",
                 "host": None, "path": project_path},
                include_path=True
            )
            print(f"Importing into workspace: {project_path}")
        else:
            print(f"Importing snapshots for {project_path}...")

        success, failure = import_all_snapshots(
            project_path,
            force=args.force,
            target_workspace_dir=workspace_dir,
        )

        if success == 0 and failure == 0:
            print("No snapshots found to import.")
            return

        print(f"\nDone: {success} imported, {failure} failed.")
        if success > 0:
            _maybe_reload(args)


def cmd_watch(args):
    """Run the background watch daemon."""
    project_path = _resolve_project(args)
    watch_loop(
        project_path=project_path,
        interval=args.interval,
        git_sync=not args.no_git,
        verbose=args.verbose,
    )


def cmd_copy(args):
    """Copy conversations between workspaces on the same machine."""
    # Select source workspace
    print(f"\n  Select SOURCE workspace (copy from):")
    source = _select_workspace()
    if not source:
        return
    source_path, source_ws_dir, source_host = source

    # Select conversations from source
    composer_ids = _select_conversations(
        source_path, prompt="copy", workspace_dir=source_ws_dir
    )
    if not composer_ids:
        print("No conversations selected.")
        return

    # Select target workspace
    print(f"\n  Select TARGET workspace (copy to):")
    target = _select_workspace()
    if not target:
        return
    target_path, target_ws_dir, target_host = target

    if str(source_ws_dir) == str(target_ws_dir):
        print("Source and target are the same workspace.", file=sys.stderr)
        return

    source_label = f"{os.path.basename(source_path)}"
    target_label = f"{os.path.basename(target_path)}"
    if source_host:
        source_label += f" ({source_host})"
    if target_host:
        target_label += f" ({target_host})"

    print(f"\n  Copying {len(composer_ids)} chat(s): {source_label} → {target_label}\n")

    success, failure = copy_between_workspaces(
        composer_ids, source_ws_dir, target_ws_dir,
        source_path=source_path, target_path=target_path,
        force=getattr(args, "force", False),
    )

    if success > 0:
        print(f"\nDone. Copied {success} chat(s).")
        from .reload import print_reload_hint
        print_reload_hint()
    elif failure > 0:
        print(f"\nFailed to copy {failure} chat(s).")
    else:
        print("Nothing done.")


def cmd_status(args):
    """Show sync status -- what's local vs what's in snapshots."""
    _ensure_synced()  # Pull latest from remote first
    project_path, workspace_dir, _ = _resolve_project_and_workspace(args)
    project_id = paths.get_project_identifier(project_path)
    snapshots_dir = paths.get_snapshots_dir() / project_id

    # Get local conversations
    local_convos = export.list_conversations(project_path, workspace_dir=workspace_dir)
    local_ids = {c["id"] for c in local_convos}

    # Get snapshot conversations
    snapshot_ids = set()
    if snapshots_dir.exists():
        for f in list_snapshot_files(snapshots_dir):
            snapshot_ids.add(_get_snapshot_id(f))

    only_local = local_ids - snapshot_ids
    only_snapshot = snapshot_ids - local_ids
    in_both = local_ids & snapshot_ids

    print(f"Project: {project_path}")
    print(f"Identity: {project_id}")
    print(f"Snapshots: {snapshots_dir}\n")
    print(f"  Local conversations:     {len(local_ids)}")
    print(f"  Snapshot files:          {len(snapshot_ids)}")
    print(f"  In both:                 {len(in_both)}")
    print(f"  Local only (unexported): {len(only_local)}")
    print(f"  Snapshot only (not imported): {len(only_snapshot)}")

    if only_local:
        print(f"\nLocal only (run 'checkpoint' to export):")
        for c in local_convos:
            if c["id"] in only_local:
                print(f"  {c['id'][:12]}...  {c['name']}")

    if only_snapshot:
        print(f"\nSnapshot only (run 'import --all' to import):")
        for sid in sorted(only_snapshot):
            print(f"  {sid[:12]}...")


def cmd_delete(args):
    """Delete cached snapshots and sync to remote."""
    import shutil

    sync_dir = paths.get_sync_dir()
    snapshots_base = paths.get_snapshots_dir()
    backend = get_backend()

    if backend.has_remote():
        backend.pull(snapshots_base)

    deleted_any = False

    # --all-projects: delete everything
    if args.all_projects:
        projects = list_snapshot_projects(snapshots_base)
        if not projects:
            print("No snapshots found.")
            return

        total_count = sum(p["count"] for p in projects)
        if not args.yes:
            print(f"This will delete {total_count} snapshot(s) across {len(projects)} project(s):")
            for p in projects:
                print(f"  {p['name']}: {p['count']} snapshot(s)")
            try:
                confirm = input("\nContinue? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if confirm not in ("y", "yes"):
                print("Cancelled.")
                return

        for p in projects:
            shutil.rmtree(p["path"])
            print(f"  Deleted: {p['name']}/ ({p['count']} snapshots)")

        print(f"\nDeleted {total_count} snapshot(s) across {len(projects)} project(s).")

        # Sync deletion to remote
        hostname = paths.get_machine_id()
        if _commit_and_push(sync_dir, f"[{hostname}] delete all snapshots"):
            print("Synced to remote.")
        return

    # --select: interactive selection across projects
    if args.select:
        projects = list_snapshot_projects(snapshots_base)
        if not projects:
            print("No snapshots found.")
            return

        print(f"\nSnapshot projects:\n")
        print(f"  {'#':<4} {'Project':<40} {'Chats':>5}  {'Source'}")
        print(f"  {'-' * 70}")

        for i, p in enumerate(projects, 1):
            sources = ", ".join(sorted(p["sources"])) or "unknown"
            name = p["name"]
            if len(name) > 38:
                name = name[:35] + "..."
            print(f"  {i:<4} {name:<40} {p['count']:>5}  {sources}")

        print(f"\nSelect project(s) to delete (e.g. 1,3 or 1-3 or 'all'):")
        try:
            choice = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not choice:
            return

        indices = _parse_selection(choice, len(projects))
        if not indices:
            return

        total_deleted = 0
        deleted_names = []
        for idx in indices:
            p = projects[idx - 1]
            shutil.rmtree(p["path"])
            print(f"  Deleted: {p['name']}/ ({p['count']} snapshots)")
            total_deleted += p["count"]
            deleted_names.append(p["name"])

        print(f"\nDeleted {total_deleted} snapshot(s) across {len(indices)} project(s).")

        # Sync deletion to remote
        hostname = paths.get_machine_id()
        msg = f"[{hostname}] delete {', '.join(deleted_names[:3])}"
        if len(deleted_names) > 3:
            msg += f" +{len(deleted_names) - 3} more"
        if _commit_and_push(sync_dir, msg):
            print("Synced to remote.")
        return

    # Single project mode (original behavior)
    project_path = args.project or paths.get_project_path()
    project_id = paths.get_project_identifier(project_path)
    snapshots_dir = snapshots_base / project_id

    if not snapshots_dir.exists():
        print(f"No snapshots found for {project_path}")
        return

    snapshot_files = list_snapshot_files(snapshots_dir)
    if not snapshot_files:
        print(f"No snapshots found for {project_path}")
        return

    if args.all:
        # Delete all snapshots for this project
        count = len(snapshot_files)
        if not args.yes:
            print(f"This will delete {count} snapshot(s) from {snapshots_dir}")
            try:
                confirm = input("Continue? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if confirm not in ("y", "yes"):
                print("Cancelled.")
                return

        for f in snapshot_files:
            _delete_snapshot(f)
        print(f"Deleted {count} snapshot(s).")

        # Sync deletion to remote
        hostname = paths.get_machine_id()
        if _commit_and_push(sync_dir, f"[{hostname}] delete all from {project_id}"):
            print("Synced to remote.")
        return

    if args.id:
        # Delete a specific snapshot by ID (supports partial match)
        target = args.id
        matches = [f for f in snapshot_files if _get_snapshot_id(f).startswith(target)]
        if not matches:
            print(f"No snapshot matching '{target}' found.", file=sys.stderr)
            sys.exit(1)
        if len(matches) > 1:
            print(f"Multiple snapshots match '{target}':", file=sys.stderr)
            for f in matches:
                print(f"  {_get_snapshot_id(f)}", file=sys.stderr)
            print("Be more specific.", file=sys.stderr)
            sys.exit(1)

        match = matches[0]
        _delete_snapshot(match)
        print(f"Deleted {_get_snapshot_id(match)}")

        # Sync deletion to remote
        hostname = paths.get_machine_id()
        if _commit_and_push(sync_dir, f"[{hostname}] delete {_get_snapshot_id(match)[:12]}"):
            print("Synced to remote.")
        return

    # Interactive mode: list and select snapshots for current project
    print(f"\nCached snapshots for {project_path}\n")
    snapshot_info = []
    for i, f in enumerate(snapshot_files, 1):
        meta = read_snapshot_meta(f)
        name = meta.get("name") or "Untitled"
        exported_at = meta.get("exportedAt") or "unknown"
        source = meta.get("sourceMachine") or "unknown"

        if len(name) > 33:
            name = name[:30] + "..."
        snapshot_info.append({"file": f, "name": name, "exported_at": exported_at, "source": source})
        print(f"  {i:<4} {name:<35} {exported_at[:19]:<20} from {source}")

    print(f"\nEnter numbers to delete (e.g. 1,3,5 or 1-3 or 'all'):")
    try:
        choice = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    if not choice:
        return

    indices = _parse_selection(choice, len(snapshot_info))
    if not indices:
        return

    deleted_names = []
    for idx in indices:
        _delete_snapshot(snapshot_info[idx - 1]["file"])
        print(f"  Deleted: {snapshot_info[idx - 1]['name']}")
        deleted_names.append(snapshot_info[idx - 1]["name"])

    print(f"\nDeleted {len(indices)} snapshot(s).")

    # Sync deletion to remote
    hostname = paths.get_machine_id()
    if _commit_and_push(sync_dir, f"[{hostname}] delete {len(indices)} from {project_id}"):
        print("Synced to remote.")


def cmd_doctor(args):
    """Audit and recover orphaned chats."""
    from .export import format_timestamp

    audit = doctor_audit()
    storage = audit["storage"]

    print(
        f"\n  ─── Cursor Storage ──────────────────────────────────────────\n"
        f"\n"
        f"  Global DB:           {storage['global_db_mb']:.0f} MB\n"
        f"  WAL file:            {storage.get('wal_mb', 0):.1f} MB\n"
        f"  Workspace storage:   {storage['workspace_storage_mb']:.0f} MB\n"
    )

    print(
        f"  ─── Chat Audit ─────────────────────────────────────────────\n"
        f"\n"
        f"  Total chats in DB:   {audit['total']}\n"
        f"  Registered:          {audit['registered']}\n"
        f"  Orphaned (content):  {len(audit['orphaned'])}\n"
        f"  Empty stubs:         {audit['empty']}\n"
    )

    if audit["workspaces"]:
        print(
            f"  ─── Workspaces with chats ───────────────────────────────────\n"
        )
        for ws in audit["workspaces"]:
            print(f"  {ws['chat_count']:>3} chats   {ws['label']}")
        print()

    orphaned = audit["orphaned"]
    if not orphaned:
        print("  No orphaned chats found.\n")
        return

    print(
        f"  ─── Orphaned chats ({len(orphaned)}) ──────────────────────────────────\n"
    )
    print(f"  {'#':<4} {'Name':<36} {'Msgs':>5}  {'Likely Workspace'}")
    print(f"  {'-' * 75}")

    for i, chat in enumerate(orphaned, 1):
        name = chat["name"]
        if len(name) > 34:
            name = name[:31] + "..."
        ws = chat.get("likelyWorkspace") or "unknown"
        if len(ws) > 22:
            ws = ws[:19] + "..."
        print(f"  {i:<4} {name:<36} {chat['messageCount']:>5}  {ws}")

    print()

    if args.recover:
        if args.select:
            print(f"  Select chats to recover (e.g. 1,3,5 or 1-3 or 'all') [all]:")
            try:
                choice = input("  > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not choice:
                choice = "all"
            indices = _parse_selection(choice, len(orphaned))
            if not indices:
                return
            selected_ids = [orphaned[i - 1]["composerId"] for i in indices]
        else:
            selected_ids = [o["composerId"] for o in orphaned]

        print(f"\n  Recovering {len(selected_ids)} chat(s)...\n")
        recovered, failed = doctor_recover(composer_ids=selected_ids)

        if recovered > 0:
            print(f"\n  Recovered {recovered} chat(s).")
            from .reload import print_reload_hint
            print_reload_hint()
        if failed > 0:
            print(f"  {failed} chat(s) could not be matched to a workspace.")
    else:
        print(
            f"  Run 'cursaves doctor --recover' to re-register orphaned chats.\n"
            f"  Run 'cursaves doctor --recover -s' to select which chats to recover.\n"
        )


def main():
    parser = argparse.ArgumentParser(
        prog="cursaves",
        description="Sync Cursor agent chat sessions between machines.",
    )
    parser.add_argument(
        "--version", action="version", version=f"cursaves {__version__}"
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Helper to add -w and -p flags to a subparser
    def add_project_args(p):
        p.add_argument(
            "--workspace", "-w",
            help="Workspace number from 'cursaves workspaces' (for SSH remotes)",
        )
        p.add_argument("--project", "-p", help="Project path (default: current directory)")

    # ── init ────────────────────────────────────────────────────────
    p_init = subparsers.add_parser(
        "init", help="Initialize sync (git repo, S3 bucket, etc.)"
    )
    p_init.add_argument(
        "--remote", "-r",
        help="Git remote URL (e.g., git@github.com:you/my-saves.git)",
    )
    p_init.add_argument(
        "--backend", "-b",
        choices=["git", "s3"],
        help="Sync backend to use (default: git)",
    )
    p_init.add_argument(
        "--bucket",
        help="S3 bucket name (required for --backend s3)",
    )
    p_init.add_argument(
        "--prefix",
        help="S3 key prefix (default: snapshots/)",
    )
    p_init.add_argument(
        "--region",
        help="AWS region for S3 bucket",
    )
    p_init.set_defaults(func=cmd_init)

    # ── workspaces ─────────────────────────────────────────────────
    p_workspaces = subparsers.add_parser(
        "workspaces", help="List all Cursor workspaces (local and SSH remote)"
    )
    p_workspaces.set_defaults(func=cmd_workspaces)

    # ── snapshots ──────────────────────────────────────────────────
    p_snapshots = subparsers.add_parser(
        "snapshots", help="List snapshot projects available in ~/.cursaves/"
    )
    p_snapshots.set_defaults(func=cmd_snapshots)

    # ── list ────────────────────────────────────────────────────────
    p_list = subparsers.add_parser("list", help="List conversations for a project")
    add_project_args(p_list)
    p_list.add_argument("--json", action="store_true", help="Output as JSON for scripting")
    p_list.set_defaults(func=cmd_list)

    # ── export ──────────────────────────────────────────────────────
    p_export = subparsers.add_parser("export", help="Export a single conversation")
    p_export.add_argument("id", help="Conversation (composer) ID")
    add_project_args(p_export)
    p_export.set_defaults(func=cmd_export)

    # ── checkpoint ──────────────────────────────────────────────────
    p_checkpoint = subparsers.add_parser(
        "checkpoint", help="Export all conversations for a project"
    )
    add_project_args(p_checkpoint)
    p_checkpoint.set_defaults(func=cmd_checkpoint)

    # ── import ──────────────────────────────────────────────────────
    p_import = subparsers.add_parser("import", help="Import conversation snapshots")
    p_import.add_argument("--all", action="store_true", help="Import all snapshots for the project")
    p_import.add_argument("--file", "-f", help="Import a specific snapshot file")
    add_project_args(p_import)
    p_import.add_argument(
        "--force", action="store_true",
        help="Suppress the Cursor-running warning",
    )
    p_import.add_argument(
        "--reload", action="store_true",
        help="(deprecated, no effect) Cursor requires a full restart to see imports",
    )
    p_import.set_defaults(func=cmd_import)

    # ── push ────────────────────────────────────────────────────────
    p_push = subparsers.add_parser(
        "push", help="Checkpoint + commit + push (one command to save and sync)"
    )
    add_project_args(p_push)
    p_push.add_argument(
        "--select", "-s", action="store_true",
        help="Interactively select workspace first",
    )
    p_push.add_argument(
        "--all", dest="all_chats", action="store_true",
        help="Push all conversations without selection prompt",
    )
    p_push.add_argument(
        "--ahead", "-a", action="store_true",
        help="Find and push all conversations ahead of snapshots across all workspaces",
    )
    p_push.set_defaults(func=cmd_push)

    # ── pull ────────────────────────────────────────────────────────
    p_pull = subparsers.add_parser(
        "pull", help="Git pull + import snapshots (one command to sync and restore)"
    )
    p_pull.add_argument(
        "--workspace", "-w",
        help="Target workspace to import into (number from 'cursaves workspaces' or path substring)",
    )
    p_pull.add_argument("--project", "-p", help="Project path (default: current directory)")
    p_pull.add_argument(
        "--select", "-s", action="store_true",
        help="Interactively select which snapshot projects to import",
    )
    p_pull.add_argument(
        "--force", action="store_true",
        help="Suppress the Cursor-running warning",
    )
    p_pull.add_argument(
        "--reload", action="store_true",
        help="(deprecated, no effect) Cursor requires a full restart to see imports",
    )
    p_pull.set_defaults(func=cmd_pull)

    # ── sync ──────────────────────────────────────────────────────
    p_sync = subparsers.add_parser(
        "sync", help="Pull behind + push ahead — one command to stay in sync across machines"
    )
    p_sync.set_defaults(func=cmd_sync)

    # ── repair ─────────────────────────────────────────────────────
    p_repair = subparsers.add_parser(
        "repair", help="Restore missing agent blobs from snapshots (fixes 'Blob not found' errors)"
    )
    p_repair.set_defaults(func=cmd_repair)

    # ── reload ─────────────────────────────────────────────────────
    p_reload = subparsers.add_parser(
        "reload", help="(deprecated) Print restart instructions"
    )
    p_reload.set_defaults(func=cmd_reload)

    # ── delete ─────────────────────────────────────────────────────
    p_delete = subparsers.add_parser(
        "delete", help="Delete cached snapshots"
    )
    p_delete.add_argument("--project", "-p", help="Project path (default: current directory)")
    p_delete.add_argument("--all", action="store_true", help="Delete all snapshots for the project")
    p_delete.add_argument("--id", help="Delete a specific snapshot by ID (supports partial match)")
    p_delete.add_argument(
        "--select", "-s", action="store_true",
        help="Interactively select which project(s) to delete",
    )
    p_delete.add_argument(
        "--all-projects", action="store_true",
        help="Delete ALL snapshots across ALL projects",
    )
    p_delete.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip confirmation prompt",
    )
    p_delete.set_defaults(func=cmd_delete)

    # ── copy ───────────────────────────────────────────────────────
    p_copy = subparsers.add_parser(
        "copy", help="Copy conversations between workspaces (same machine)"
    )
    p_copy.add_argument(
        "--force", action="store_true",
        help="Suppress the Cursor-running warning",
    )
    p_copy.set_defaults(func=cmd_copy)

    # ── status ──────────────────────────────────────────────────────
    p_status = subparsers.add_parser("status", help="Show sync status")
    add_project_args(p_status)
    p_status.set_defaults(func=cmd_status)

    # ── watch ────────────────────────────────────────────────────────
    p_watch = subparsers.add_parser(
        "watch", help="Auto-checkpoint and sync in the background"
    )
    add_project_args(p_watch)
    p_watch.add_argument(
        "--interval", "-i", type=int, default=60,
        help="Seconds between checks (default: 60)",
    )
    p_watch.add_argument(
        "--no-git", action="store_true",
        help="Disable automatic git commit/push",
    )
    p_watch.add_argument("--verbose", "-v", action="store_true", help="Print on every check")
    p_watch.set_defaults(func=cmd_watch)

    # ── doctor ─────────────────────────────────────────────────────
    p_doctor = subparsers.add_parser(
        "doctor", help="Audit chats and recover orphaned conversations"
    )
    p_doctor.add_argument(
        "--recover", action="store_true",
        help="Re-register orphaned chats in their workspaces",
    )
    p_doctor.add_argument(
        "--select", "-s", action="store_true",
        help="Interactively select which orphaned chats to recover",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    args = parser.parse_args()
    if not args.command:
        print(
            "cursaves - sync Cursor chats between machines\n"
            "\n"
            "Usage: cursaves <command> [options]\n"
            "\n"
            "─── Sync between machines ──────────────────────────────────────\n"
            "\n"
            "  init                  Initialize ~/.cursaves/ sync repo\n"
            "  init -r <url>         Initialize with git remote URL\n"
            "  push                  Save + commit + push chats\n"
            "  push -s               Select workspace + chats to push\n"
            "  pull                  Pull + import chats\n"
            "  pull -s               Select which snapshots to import\n"
            "\n"
            "─── Copy between workspaces (same machine) ─────────────────────\n"
            "\n"
            "  copy                  Copy chats between workspaces\n"
            "\n"
            "─── Info & management ──────────────────────────────────────────\n"
            "\n"
            "  workspaces            List Cursor workspaces (local + SSH)\n"
            "  list                  List chats for this project\n"
            "  snapshots             List saved snapshots in ~/.cursaves/\n"
            "  status                Show synced vs local-only chats\n"
            "  doctor                Audit chats, find orphaned conversations\n"
            "  doctor --recover      Re-register orphaned chats in workspaces\n"
            "  delete -s             Select which snapshots to delete\n"
            "  delete --all-projects Delete ALL snapshots\n"
            "\n"
            "─── Options ─────────────────────────────────────────────────────\n"
            "\n"
            "  -w <number>           Target workspace # (from 'workspaces')\n"
            "  -p <path>             Target project path\n"
            "  -s, --select          Interactive selection mode\n"
            "  -y, --yes             Skip confirmation prompts\n"
            "\n"
            "After importing, restart Cursor (quit + reopen) to see chats.\n"
            "\n"
            "Run 'cursaves <command> --help' for more options.\n"
            "Update: uv tool upgrade cursaves"
        )
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
