"""Background daemon for automatic checkpoint + git sync."""

import hashlib
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import export, paths


def _get_db_fingerprint(project_path: str) -> Optional[str]:
    """Get a fingerprint of the current conversation state.

    Uses the modification time + size of the workspace and global DBs
    as a cheap change-detection signal.
    """
    parts = []

    # Global DB mtime + size
    global_db = paths.get_global_db_path()
    if global_db.exists():
        st = global_db.stat()
        parts.append(f"global:{st.st_mtime}:{st.st_size}")
        # Also check WAL file (most writes go here first)
        wal = global_db.parent / (global_db.name + "-wal")
        if wal.exists():
            wst = wal.stat()
            parts.append(f"wal:{wst.st_mtime}:{wst.st_size}")

    # Workspace DB mtime + size
    ws_dirs = paths.find_workspace_dirs_for_project(project_path)
    for ws_dir in ws_dirs[:1]:  # Just check the most recent
        ws_db = ws_dir / "state.vscdb"
        if ws_db.exists():
            st = ws_db.stat()
            parts.append(f"ws:{st.st_mtime}:{st.st_size}")

    if not parts:
        return None

    return hashlib.md5("|".join(parts).encode()).hexdigest()


def _git_repo_root(snapshots_dir: Path = None) -> Optional[Path]:
    """Return the sync repo root if it's a git repo."""
    sync_dir = paths.get_sync_dir()
    if (sync_dir / ".git").exists():
        return sync_dir
    return None


def _git_has_remote(repo_root: Path) -> bool:
    """Check if the git repo has a remote configured."""
    try:
        result = subprocess.run(
            ["git", "remote"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
        )
        return result.returncode == 0 and result.stdout.strip() != ""
    except FileNotFoundError:
        return False


def _git_sync(repo_root: Path, project_path: str) -> tuple[bool, str]:
    """Pull from remote, then add + commit + push snapshots.

    Returns (success, message).
    """
    hostname = paths.get_machine_id()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    try:
        # Pull first (rebase to keep history linear)
        # Use explicit fetch + rebase to avoid "no tracking info" failures
        subprocess.run(
            ["git", "fetch", "origin"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=30,
        )
        # Set up tracking if not already configured
        subprocess.run(
            ["git", "branch", "--set-upstream-to=origin/main", "main"],
            capture_output=True,
            cwd=str(repo_root),
        )
        pull_result = subprocess.run(
            ["git", "rebase", "--autostash", "origin/main"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=30,
        )
        if pull_result.returncode != 0:
            return False, f"git pull failed: {pull_result.stderr.strip()}"

        # Stage snapshot files
        add_result = subprocess.run(
            ["git", "add", "snapshots/"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=10,
        )
        if add_result.returncode != 0:
            return False, f"git add failed: {add_result.stderr.strip()}"

        # Check if there's anything to commit
        status_result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
        )
        if status_result.returncode == 0:
            return True, "no changes to commit"

        # Commit
        project_name = os.path.basename(os.path.normpath(project_path))
        commit_msg = f"[{hostname}] checkpoint {project_name} ({timestamp})"
        commit_result = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=10,
        )
        if commit_result.returncode != 0:
            return False, f"git commit failed: {commit_result.stderr.strip()}"

        # Push
        if _git_has_remote(repo_root):
            push_result = subprocess.run(
                ["git", "push", "-u", "origin", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(repo_root),
                timeout=30,
            )
            if push_result.returncode != 0:
                return False, f"git push failed: {push_result.stderr.strip()}"
            return True, "committed and pushed"
        else:
            return True, "committed (no remote configured)"

    except subprocess.TimeoutExpired:
        return False, "git operation timed out"
    except Exception as e:
        return False, f"git error: {e}"


def watch_loop(
    project_path: str,
    interval: int = 60,
    git_sync: bool = True,
    verbose: bool = False,
):
    """Main watch loop -- polls for changes and auto-checkpoints.

    Args:
        project_path: The project to watch.
        interval: Seconds between checks.
        git_sync: Whether to auto-commit and push to git.
        verbose: Print status messages on every check.
    """
    print(f"cursaves watch started")
    print(f"  Project: {project_path}")
    print(f"  Interval: {interval}s")
    print(f"  Git sync: {'enabled' if git_sync else 'disabled'}")
    print(f"  Machine: {paths.get_machine_id()}")
    print()

    snapshots_dir = paths.get_snapshots_dir()
    repo_root = _git_repo_root(snapshots_dir) if git_sync else None

    if git_sync and not repo_root:
        print("Warning: snapshots directory is not in a git repo. Git sync disabled.")
        git_sync = False

    last_fingerprint = _get_db_fingerprint(project_path)
    checkpoint_count = 0

    # Handle graceful shutdown
    running = True

    def handle_signal(signum, frame):
        nonlocal running
        print(f"\nShutting down (received signal {signum})...")
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while running:
        time.sleep(interval)

        if not running:
            break

        current_fingerprint = _get_db_fingerprint(project_path)

        if current_fingerprint == last_fingerprint:
            if verbose:
                print(f"[{_now()}] no changes detected")
            continue

        # Change detected -- checkpoint
        print(f"[{_now()}] change detected, checkpointing...")
        try:
            saved = export.checkpoint_project(project_path)
            if saved:
                checkpoint_count += 1
                print(f"[{_now()}] checkpointed {len(saved)} conversation(s) (total: {checkpoint_count})")

                # Git sync
                if git_sync and repo_root:
                    success, msg = _git_sync(repo_root, project_path)
                    print(f"[{_now()}] git: {msg}")
            else:
                if verbose:
                    print(f"[{_now()}] no conversations to checkpoint")

        except Exception as e:
            print(f"[{_now()}] error during checkpoint: {e}", file=sys.stderr)

        last_fingerprint = current_fingerprint

    print(f"\nwatch stopped. Total checkpoints: {checkpoint_count}")


def _now() -> str:
    """Return current time as a short string."""
    return datetime.now().strftime("%H:%M:%S")
