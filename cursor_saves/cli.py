"""CLI entry point for cursaves."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import __version__, export, paths
from .importer import import_all_snapshots, import_snapshot
from .watch import watch_loop


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
            print("Reload Cursor window (Cmd+Shift+P -> 'Reload Window') to see them.")
    elif args.file:
        snapshot_path = Path(args.file)
        if not snapshot_path.exists():
            print(f"Error: File not found: {snapshot_path}", file=sys.stderr)
            sys.exit(1)
        print(f"Importing {snapshot_path.name}...")
        if import_snapshot(snapshot_path, project_path):
            print("Done. Reload Cursor window to see the imported conversation.")
        else:
            print("Import failed.", file=sys.stderr)
            sys.exit(1)
    else:
        print("Error: Specify --all or --file <path>", file=sys.stderr)
        sys.exit(1)


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


def cmd_push(args):
    """Checkpoint + git commit + push in one command."""
    from .watch import _git_has_remote

    sync_dir = _require_sync_repo()
    project_path = _resolve_project(args)

    # Step 1: Checkpoint
    print(f"Checkpointing conversations for {project_path}...")
    saved = export.checkpoint_project(project_path)

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
        push_result = subprocess.run(
            ["git", "push", "-u", "origin", "HEAD"],
            cwd=str(sync_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if push_result.returncode == 0:
            print(" done")
        else:
            print(f" failed: {push_result.stderr.strip()}", file=sys.stderr)
    else:
        print("  No remote configured, skipping push")

    print(f"\nDone. {len(saved)} conversation(s) saved and pushed.")


def cmd_pull(args):
    """Git pull + import snapshots in one command."""
    from .watch import _git_has_remote

    sync_dir = _require_sync_repo()
    project_path = _resolve_project(args)

    # Step 1: Git pull
    if _git_has_remote(sync_dir):
        print("Pulling latest snapshots...", end="", flush=True)
        pull_result = subprocess.run(
            ["git", "pull", "--rebase", "--autostash"],
            cwd=str(sync_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if pull_result.returncode == 0:
            print(" done")
        else:
            print(f" failed: {pull_result.stderr.strip()}", file=sys.stderr)
            return
    else:
        print("No git remote configured, importing from local snapshots only.")

    # Step 2: Import
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
        print("Reload Cursor window (Cmd+Shift+P -> 'Reload Window') to see them.")


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
    project_path = _resolve_project(args)
    project_id = paths.get_project_identifier(project_path)
    snapshots_dir = paths.get_snapshots_dir() / project_id

    # Get local conversations
    local_convos = export.list_conversations(project_path)
    local_ids = {c["id"] for c in local_convos}

    # Get snapshot conversations
    snapshot_ids = set()
    if snapshots_dir.exists():
        for f in snapshots_dir.glob("*.json"):
            snapshot_ids.add(f.stem)

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
    p_import.set_defaults(func=cmd_import)

    # ── push ────────────────────────────────────────────────────────
    p_push = subparsers.add_parser(
        "push", help="Checkpoint + commit + push (one command to save and sync)"
    )
    add_project_args(p_push)
    p_push.set_defaults(func=cmd_push)

    # ── pull ────────────────────────────────────────────────────────
    p_pull = subparsers.add_parser(
        "pull", help="Git pull + import snapshots (one command to sync and restore)"
    )
    add_project_args(p_pull)
    p_pull.add_argument(
        "--force", action="store_true",
        help="Suppress the Cursor-running warning",
    )
    p_pull.set_defaults(func=cmd_pull)

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
            "cursaves - sync Cursor agent chats between machines\n"
            "\n"
            "Usage: cursaves <command> [options]\n"
            "\n"
            "Commands:\n"
            "  push          Checkpoint + commit + push to remote\n"
            "  pull          Pull from remote + import into Cursor\n"
            "  init          Initialize ~/.cursaves/ sync repo\n"
            "  workspaces    List all Cursor workspaces (local + SSH)\n"
            "  list          List conversations for a project\n"
            "  status        Show sync status (local vs snapshots)\n"
            "  export <id>   Export a single conversation\n"
            "  checkpoint    Export all conversations (no git)\n"
            "  import        Import snapshots (no git)\n"
            "  watch         Auto-checkpoint and sync in the background\n"
            "\n"
            "Options:\n"
            "  -w <number>   Select workspace by number (from 'cursaves workspaces')\n"
            "  -p <path>     Specify project path directly\n"
            "\n"
            "Run 'cursaves <command> --help' for details on a specific command."
        )
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
