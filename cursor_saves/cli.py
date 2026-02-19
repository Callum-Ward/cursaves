"""CLI entry point for cursaves."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import __version__, export, paths
from .importer import (
    find_snapshot_dir_for_project,
    import_all_snapshots,
    import_from_snapshot_dir,
    import_snapshot,
    list_snapshot_projects,
    list_snapshot_files,
    read_snapshot_file,
)


def _get_snapshot_id(path: Path) -> str:
    """Extract the snapshot ID (composer ID) from a snapshot filename."""
    name = path.name
    if name.endswith(".json.gz"):
        return name[:-8]
    elif name.endswith(".json"):
        return name[:-5]
    return path.stem
from .reload import print_reload_hint
from .watch import watch_loop


def _git_reset_to_origin(sync_dir: Path) -> bool:
    """Reset hard to origin/main. Remote is always ground truth. Returns True on success."""
    from .watch import _git_has_remote

    if not sync_dir.exists():
        return False

    # Abort any in-progress rebase/merge/cherry-pick
    rebase_dir = sync_dir / ".git" / "rebase-merge"
    rebase_apply_dir = sync_dir / ".git" / "rebase-apply"
    if rebase_dir.exists() or rebase_apply_dir.exists():
        subprocess.run(["git", "rebase", "--abort"], cwd=str(sync_dir), capture_output=True)
    
    # Also try to abort merge if in progress
    subprocess.run(["git", "merge", "--abort"], cwd=str(sync_dir), capture_output=True)
    subprocess.run(["git", "cherry-pick", "--abort"], cwd=str(sync_dir), capture_output=True)

    if not _git_has_remote(sync_dir):
        # No remote, just ensure we're on main
        subprocess.run(["git", "checkout", "-f", "-B", "main"], cwd=str(sync_dir), capture_output=True)
        return True

    try:
        # Fetch latest from origin
        fetch_result = subprocess.run(
            ["git", "fetch", "origin"],
            cwd=str(sync_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if fetch_result.returncode != 0:
            return False

        # Force checkout to main and reset hard (discard all local state)
        subprocess.run(
            ["git", "checkout", "-f", "-B", "main", "origin/main"],
            cwd=str(sync_dir),
            capture_output=True,
        )
        subprocess.run(
            ["git", "reset", "--hard", "origin/main"],
            cwd=str(sync_dir),
            capture_output=True,
        )
        subprocess.run(
            ["git", "branch", "--set-upstream-to=origin/main", "main"],
            cwd=str(sync_dir),
            capture_output=True,
        )
        # Clean up any untracked files
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=str(sync_dir),
            capture_output=True,
        )
        return True
    except subprocess.TimeoutExpired:
        return False


def _ensure_synced() -> None:
    """Reset to origin to ensure we have the latest state. Remote is ground truth."""
    sync_dir = paths.get_sync_dir()
    if sync_dir.exists():
        _git_reset_to_origin(sync_dir)


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


def cmd_workspaces(args):
    """List Cursor workspaces that have conversations."""
    from datetime import datetime, timezone

    workspaces = paths.list_workspaces_with_conversations()
    if not workspaces:
        print("No workspaces with conversations found.")
        return

    print(f"{'#':<4} {'Type':<6} {'Path':<50} {'Host':<15} {'Chats':>5}  {'Last Active'}")
    print("-" * 110)

    for i, ws in enumerate(workspaces, 1):
        path = ws["path"]
        if len(path) > 48:
            path = "..." + path[-45:]
        host = ws["host"] or ""
        convos = ws.get("conversations", 0)
        if ws["mtime"]:
            dt = datetime.fromtimestamp(ws["mtime"], tz=timezone.utc)
            active = dt.strftime("%Y-%m-%d %H:%M")
        else:
            active = "unknown"

        print(f"{i:<4} {ws['type']:<6} {path:<50} {host:<15} {convos:>5}  {active}")

    print(f"\n{len(workspaces)} workspace(s) with conversations")
    print("\nUse 'cursaves push -w <number>' to push a specific workspace.")


def cmd_snapshots(args):
    """List all snapshot projects available in ~/.cursaves/snapshots/."""
    _ensure_synced()  # Pull latest from remote first
    snapshots_dir = paths.get_snapshots_dir()
    projects = list_snapshot_projects(snapshots_dir)

    if not projects:
        print("No snapshots found in ~/.cursaves/snapshots/")
        print("Run 'cursaves push' to checkpoint and push conversations.")
        return

    print(f"{'#':<4} {'Project':<40} {'Chats':>5}  {'Source Machine':<20} {'Source Path'}")
    print("-" * 110)

    for i, p in enumerate(projects, 1):
        sources = ", ".join(sorted(p["sources"])) or "unknown"
        # Show the most representative source path
        source_path = ""
        if p["source_paths"]:
            source_path = sorted(p["source_paths"])[0]
            if len(source_path) > 40:
                source_path = "..." + source_path[-37:]

        name = p["name"]
        if len(name) > 38:
            name = name[:35] + "..."
        print(f"{i:<4} {name:<40} {p['count']:>5}  {sources:<20} {source_path}")

    print(f"\n{len(projects)} project(s) with snapshots")
    print(f"\nUse 'cursaves pull -s' to interactively select which to import.")


def cmd_init(args):
    """Initialize the sync directory (~/.cursaves/) as a git repo."""
    sync_dir = paths.get_sync_dir()
    snapshots_dir = sync_dir / "snapshots"

    if paths.is_sync_repo_initialized():
        print(f"Sync repo already initialized at {sync_dir}")
        # Allow adding/updating remote on an existing repo
        if args.remote:
            # Check if remote already exists
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=str(sync_dir),
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                old_remote = result.stdout.strip()
                if old_remote == args.remote:
                    print(f"  Remote already set to {args.remote}")
                else:
                    subprocess.run(
                        ["git", "remote", "set-url", "origin", args.remote],
                        cwd=str(sync_dir),
                        capture_output=True,
                    )
                    print(f"  Updated remote: {old_remote} -> {args.remote}")
            else:
                subprocess.run(
                    ["git", "remote", "add", "origin", args.remote],
                    cwd=str(sync_dir),
                    capture_output=True,
                )
                print(f"  Added remote: {args.remote}")
        return

    print(f"Initializing sync repo at {sync_dir}...")
    sync_dir.mkdir(parents=True, exist_ok=True)
    snapshots_dir.mkdir(exist_ok=True)

    # git init with main as default branch
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(sync_dir),
        capture_output=True,
    )

    # Create .gitignore
    gitignore = sync_dir / ".gitignore"
    gitignore.write_text(".DS_Store\n")

    # Initial commit
    subprocess.run(["git", "add", "."], cwd=str(sync_dir), capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initialize cursaves sync repo"],
        cwd=str(sync_dir),
        capture_output=True,
    )

    print(f"  Created {sync_dir}")

    # Add remote if provided
    if args.remote:
        subprocess.run(
            ["git", "remote", "add", "origin", args.remote],
            cwd=str(sync_dir),
            capture_output=True,
        )
        print(f"  Added remote: {args.remote}")
        print(f"\nDone. Run 'cursaves push' from any project directory to start syncing.")
    else:
        print(f"\nDone. To sync between machines, add a remote:")
        print(f"  cursaves init --remote git@github.com:you/my-cursaves.git")


def cmd_list(args):
    """List conversations for the current project."""
    project_path = _resolve_project(args)
    conversations = export.list_conversations(project_path)

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
    project_path = _resolve_project(args)

    print(f"Checkpointing conversations for {project_path}...")
    saved = export.checkpoint_project(project_path)

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


def _maybe_reload(args):
    """Print restart hint after import."""
    print_reload_hint()


def cmd_reload(args):
    """Print restart instructions."""
    print_reload_hint()


def _require_sync_repo():
    """Check that the sync repo is initialized, exit with help if not."""
    if not paths.is_sync_repo_initialized():
        print(
            "Error: Sync repo not initialized.\n"
            "Run 'cursaves init' first to set up ~/.cursaves/\n\n"
            "Example:\n"
            "  cursaves init --remote git@github.com:you/my-cursaves.git",
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


def _select_workspace() -> str | None:
    """Show all Cursor workspaces and let the user pick one.

    Returns the project path for the selected workspace, or None.
    """
    workspaces = paths.list_all_workspaces()
    if not workspaces:
        print("No Cursor workspaces found.")
        return None

    print(f"\nCursor workspaces (most recent first)\n")
    print(f"  {'#':<4} {'Project':<50} {'Path'}")
    print(f"  {'-' * 90}")

    for i, ws in enumerate(workspaces, 1):
        name = ws["name"]
        path = ws["path"]
        if len(path) > 50:
            path = "..." + path[-47:]
        print(f"  {i:<4} {name:<50} {path}")

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

    return workspaces[idx - 1]["path"]


def _select_conversations(project_path: str) -> list[str]:
    """Show conversations for a workspace and let the user pick.

    Returns a list of selected composer IDs, or empty list.
    """
    conversations = export.list_conversations(project_path)
    if not conversations:
        print(f"No conversations found for {project_path}")
        return []

    conversations.sort(key=lambda c: c.get("lastUpdated", ""), reverse=True)

    print(f"\nConversations for {project_path}\n")
    print(f"  {'#':<4} {'Name':<35} {'Mode':<8} {'Msgs':>5}  {'Last Updated'}")
    print(f"  {'-' * 85}")

    for i, c in enumerate(conversations, 1):
        name = c["name"]
        if len(name) > 33:
            name = name[:30] + "..."
        print(
            f"  {i:<4} {name:<35} {c['mode']:<8} {c['messageCount']:>5}  {c['lastUpdated']}"
        )

    print(f"\nSelect conversations to push (e.g. 1,3,5 or 1-3 or 'all'):")
    try:
        choice = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return []

    if not choice:
        return []

    indices = _parse_selection(choice, len(conversations))
    return [conversations[i - 1]["id"] for i in indices]


def cmd_push(args):
    """Checkpoint + git commit + push in one command."""
    from .watch import _git_has_remote

    sync_dir = _require_sync_repo()

    # Step 0: Reset to origin (remote is ground truth)
    if _git_has_remote(sync_dir):
        if not _git_reset_to_origin(sync_dir):
            print("Warning: Could not sync with remote, continuing anyway...", file=sys.stderr)

    # Interactive selection mode: pick workspace, then conversations
    composer_ids = None
    if args.select:
        project_path = _select_workspace()
        if not project_path:
            return
        composer_ids = _select_conversations(project_path)
        if not composer_ids:
            print("No conversations selected.")
            return
    else:
        project_path = _resolve_project(args)

    # Step 1: Checkpoint
    if composer_ids:
        print(f"\nCheckpointing {len(composer_ids)} selected conversation(s)...")
    else:
        print(f"Checkpointing conversations for {project_path}...")
    saved = export.checkpoint_project(project_path, composer_ids=composer_ids)

    if not saved:
        print("No conversations found to checkpoint.")
        return

    print(f"  {len(saved)} conversation(s) checkpointed")

    # Step 2: Git add + commit + push
    subprocess.run(["git", "add", "snapshots/"], cwd=str(sync_dir), capture_output=True)

    # Check if there's anything to commit
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(sync_dir),
        capture_output=True,
    )
    if result.returncode == 0:
        print("  No changes to commit (snapshots already up to date)")
        return

    # Commit
    hostname = paths.get_machine_id()
    project_name = os.path.basename(os.path.normpath(project_path))
    msg = f"[{hostname}] checkpoint {project_name}"
    subprocess.run(["git", "commit", "-m", msg], cwd=str(sync_dir), capture_output=True)
    print(f"  Committed")

    # Push
    if _git_has_remote(sync_dir):
        print("  Pushing...", end="", flush=True)
        try:
            push_result = subprocess.run(
                ["git", "push", "-u", "origin", "main"],
                cwd=str(sync_dir),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if push_result.returncode == 0:
                print(" done")
            else:
                print(f" failed: {push_result.stderr.strip()}", file=sys.stderr)
        except subprocess.TimeoutExpired:
            print(" timed out (changes saved locally, push manually with: cd ~/.cursaves && git push)", file=sys.stderr)
    else:
        print("  No remote configured, skipping push")

    print(f"\nDone. {len(saved)} conversation(s) saved.")


def _git_pull_quiet(sync_dir: Path) -> bool:
    """Reset to origin without printing status. Returns True on success."""
    return _git_reset_to_origin(sync_dir)


def _git_commit_and_push(sync_dir: Path, message: str) -> bool:
    """Add all changes, commit, and push. Returns True if changes were pushed."""
    from .watch import _git_has_remote

    # Stage all changes (including deletions)
    subprocess.run(["git", "add", "-A"], cwd=str(sync_dir), capture_output=True)

    # Check if there's anything to commit
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(sync_dir),
        capture_output=True,
    )
    if result.returncode == 0:
        return False  # Nothing to commit

    # Commit
    subprocess.run(["git", "commit", "-m", message], cwd=str(sync_dir), capture_output=True)

    # Push if remote exists
    if _git_has_remote(sync_dir):
        try:
            push_result = subprocess.run(
                ["git", "push", "-u", "origin", "main"],
                cwd=str(sync_dir),
                capture_output=True,
                text=True,
                timeout=120,
            )
            return push_result.returncode == 0
        except subprocess.TimeoutExpired:
            print("Push timed out (changes saved locally)", file=sys.stderr)
            return False

    return True


def _git_pull(sync_dir: Path) -> bool:
    """Reset to origin/main. Remote is ground truth. Returns True on success."""
    from .watch import _git_has_remote

    if not _git_has_remote(sync_dir):
        print("No git remote configured, importing from local snapshots only.")
        return True

    print("Syncing with remote...", end="", flush=True)
    if _git_reset_to_origin(sync_dir):
        print(" done")
        return True
    else:
        print(" failed", file=sys.stderr)
        return False


def cmd_pull(args):
    """Git pull + import snapshots in one command."""
    sync_dir = _require_sync_repo()

    # Step 1: Git pull
    if not _git_pull(sync_dir):
        return

    # Step 2: Select what to import
    if args.select:
        # Interactive: show available snapshot projects and let user pick
        projects = list_snapshot_projects()
        if not projects:
            print("No snapshots found. Run 'cursaves push' on another machine first.")
            return

        print(f"\nAvailable snapshot projects:\n")
        print(f"  {'#':<4} {'Project':<40} {'Chats':>5}  {'Source'}")
        print(f"  {'-' * 75}")

        for i, p in enumerate(projects, 1):
            sources = ", ".join(sorted(p["sources"])) or "unknown"
            name = p["name"]
            if len(name) > 38:
                name = name[:35] + "..."
            print(f"  {i:<4} {name:<40} {p['count']:>5}  {sources}")

        print(f"\nSelect project(s) to import (e.g. 1,3 or 1-3 or 'all'):")
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

        # For each selected project, ask for target project path
        total_success = 0
        total_failure = 0
        for idx in indices:
            project = projects[idx - 1]
            print(f"\nImporting from {project['name']}/ ({project['count']} snapshot(s))...")

            # Use the source path from the snapshots as the target,
            # or ask the user if it doesn't exist locally.
            # For SSH workspaces, the path won't exist locally but we can
            # still import if there's a matching Cursor workspace.
            target_path = None
            for sp in sorted(project["source_paths"]):
                # Check if path exists locally (for local projects)
                if os.path.isdir(sp):
                    target_path = sp
                    break
                # Check if there's an existing Cursor workspace for this path
                # (handles SSH remote workspaces where the path doesn't exist locally)
                ws_dirs = paths.find_workspace_dirs_for_project(sp)
                if ws_dirs:
                    target_path = sp
                    break

            if not target_path:
                # Try CWD as fallback
                cwd = os.getcwd()
                cwd_basename = os.path.basename(os.path.normpath(cwd))
                source_basenames = {os.path.basename(os.path.normpath(sp)) for sp in project["source_paths"]}
                if cwd_basename in source_basenames or project["name"] == paths.get_project_identifier(cwd):
                    target_path = cwd
                else:
                    print(f"  Source path(s): {', '.join(sorted(project['source_paths']))}")
                    print(f"  No matching local or SSH workspace found.")
                    print(f"  Enter the project path to import into:")
                    try:
                        target_path = input("  > ").strip()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        continue
                    if not target_path:
                        print("  Skipped.")
                        continue

            print(f"  Target: {target_path}")
            success, failure = import_from_snapshot_dir(
                project["path"], target_path, force=args.force,
            )
            total_success += success
            total_failure += failure

        if total_success == 0 and total_failure == 0:
            print("\nNo snapshots imported.")
            return

        print(f"\nDone: {total_success} imported, {total_failure} failed.")
        if total_success > 0:
            _maybe_reload(args)
    else:
        # Non-interactive: import for the resolved project
        project_path = _resolve_project(args)
        print(f"Importing snapshots for {project_path}...")
        success, failure = import_all_snapshots(
            project_path,
            force=args.force,
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


def cmd_status(args):
    """Show sync status -- what's local vs what's in snapshots."""
    _ensure_synced()  # Pull latest from remote first
    project_path = _resolve_project(args)
    project_id = paths.get_project_identifier(project_path)
    snapshots_dir = paths.get_snapshots_dir() / project_id

    # Get local conversations
    local_convos = export.list_conversations(project_path)
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
    from .watch import _git_has_remote

    sync_dir = paths.get_sync_dir()
    snapshots_base = paths.get_snapshots_dir()

    # Reset to origin (remote is ground truth)
    if sync_dir.exists() and _git_has_remote(sync_dir):
        _git_reset_to_origin(sync_dir)

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
        if sync_dir.exists() and _git_has_remote(sync_dir):
            hostname = paths.get_machine_id()
            if _git_commit_and_push(sync_dir, f"[{hostname}] delete all snapshots"):
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
        if sync_dir.exists() and _git_has_remote(sync_dir):
            hostname = paths.get_machine_id()
            msg = f"[{hostname}] delete {', '.join(deleted_names[:3])}"
            if len(deleted_names) > 3:
                msg += f" +{len(deleted_names) - 3} more"
            if _git_commit_and_push(sync_dir, msg):
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
            f.unlink()
        print(f"Deleted {count} snapshot(s).")

        # Sync deletion to remote
        if sync_dir.exists() and _git_has_remote(sync_dir):
            hostname = paths.get_machine_id()
            if _git_commit_and_push(sync_dir, f"[{hostname}] delete all from {project_id}"):
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
        match.unlink()
        print(f"Deleted {_get_snapshot_id(match)}")

        # Sync deletion to remote
        if sync_dir.exists() and _git_has_remote(sync_dir):
            hostname = paths.get_machine_id()
            if _git_commit_and_push(sync_dir, f"[{hostname}] delete {_get_snapshot_id(match)[:12]}"):
                print("Synced to remote.")
        return

    # Interactive mode: list and select snapshots for current project
    print(f"\nCached snapshots for {project_path}\n")
    snapshot_info = []
    for i, f in enumerate(snapshot_files, 1):
        try:
            data = read_snapshot_file(f)
            name = data.get("composerData", {}).get("name", "Untitled")
            exported_at = data.get("exportedAt", "unknown")
            source = data.get("sourceMachine", "unknown")
        except Exception:
            name = "Untitled"
            exported_at = "unknown"
            source = "unknown"

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
        snapshot_info[idx - 1]["file"].unlink()
        print(f"  Deleted: {snapshot_info[idx - 1]['name']}")
        deleted_names.append(snapshot_info[idx - 1]["name"])

    print(f"\nDeleted {len(indices)} snapshot(s).")

    # Sync deletion to remote
    if sync_dir.exists() and _git_has_remote(sync_dir):
        hostname = paths.get_machine_id()
        if _git_commit_and_push(sync_dir, f"[{hostname}] delete {len(indices)} from {project_id}"):
            print("Synced to remote.")


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
        "init", help="Initialize ~/.cursaves/ sync repo"
    )
    p_init.add_argument(
        "--remote", "-r",
        help="Git remote URL for syncing (e.g., git@github.com:you/my-saves.git)",
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
        help="Interactively select which conversations to push",
    )
    p_push.set_defaults(func=cmd_push)

    # ── pull ────────────────────────────────────────────────────────
    p_pull = subparsers.add_parser(
        "pull", help="Git pull + import snapshots (one command to sync and restore)"
    )
    add_project_args(p_pull)
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

    args = parser.parse_args()
    if not args.command:
        print(
            "cursaves - sync Cursor chats between machines\n"
            "\n"
            "Usage: cursaves <command> [options]\n"
            "\n"
            "─── Run from anywhere ───────────────────────────────────────────\n"
            "\n"
            "  init                  Initialize ~/.cursaves/ sync repo\n"
            "  init -r <url>         Initialize with git remote URL\n"
            "  workspaces            List Cursor workspaces (local + SSH)\n"
            "  snapshots             List saved snapshots in ~/.cursaves/\n"
            "  push -s               Select workspace + chats to push\n"
            "  pull -s               Select which snapshots to import\n"
            "  delete -s             Select which snapshots to delete\n"
            "  delete --all-projects Delete ALL snapshots\n"
            "\n"
            "─── Run from project directory (or use -w/-p) ──────────────────\n"
            "\n"
            "  push                  Save + commit + push chats\n"
            "  pull                  Pull + import chats\n"
            "  list                  List chats for this project\n"
            "  status                Show synced vs local-only chats\n"
            "  delete                Delete snapshots for this project\n"
            "  delete --all          Delete all snapshots for this project\n"
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
