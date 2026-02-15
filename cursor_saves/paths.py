"""Platform detection and Cursor storage path resolution."""

import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional


def get_cursor_user_dir() -> Path:
    """Return the Cursor User data directory for the current platform.

    macOS:  ~/Library/Application Support/Cursor/User
    Linux:  ~/.config/Cursor/User
    """
    system = platform.system()
    if system == "Darwin":
        base = Path.home() / "Library" / "Application Support" / "Cursor" / "User"
    elif system == "Linux":
        base = Path.home() / ".config" / "Cursor" / "User"
    else:
        print(
            f"Error: Unsupported platform '{system}'.\n"
            f"cursaves supports macOS and Linux.\n"
            f"On macOS, Cursor data is at ~/Library/Application Support/Cursor/User/\n"
            f"On Linux, Cursor data is at ~/.config/Cursor/User/",
            file=sys.stderr,
        )
        sys.exit(1)

    if not base.exists():
        print(
            f"Error: Cursor data directory not found at:\n"
            f"  {base}\n\n"
            f"This usually means:\n"
            f"  - Cursor is not installed on this machine, or\n"
            f"  - Cursor has never been opened (no data created yet), or\n"
            f"  - Cursor stores data at a non-standard location\n\n"
            f"Expected path for {system}: {base}",
            file=sys.stderr,
        )
        sys.exit(1)

    return base


def get_global_db_path() -> Path:
    """Return the path to Cursor's global state.vscdb."""
    return get_cursor_user_dir() / "globalStorage" / "state.vscdb"


def get_workspace_storage_dir() -> Path:
    """Return the path to Cursor's workspace storage directory."""
    return get_cursor_user_dir() / "workspaceStorage"


def get_cursor_projects_dir() -> Path:
    """Return the path to ~/.cursor/projects/ (agent transcripts, etc.)."""
    return Path.home() / ".cursor" / "projects"


def sanitize_project_path(project_path: str) -> str:
    """Convert a project path to Cursor's sanitized directory name format.

    /Users/callum/Desktop/Projects/myrepo -> Users-callum-Desktop-Projects-myrepo
    """
    # Strip leading slash and replace / with -
    return project_path.strip("/").replace("/", "-")


def find_workspace_dirs_for_project(project_path: str) -> list[Path]:
    """Find all workspace directories that map to a given project path.

    Scans workspace.json files in workspaceStorage/ to find matches.
    Returns list of workspace directory paths, newest first.
    """
    ws_storage = get_workspace_storage_dir()
    if not ws_storage.exists():
        return []

    # Normalise the target path for comparison
    target = os.path.normpath(os.path.expanduser(project_path))

    matches = []
    for ws_dir in ws_storage.iterdir():
        if not ws_dir.is_dir():
            continue
        ws_json = ws_dir / "workspace.json"
        if not ws_json.exists():
            continue
        try:
            data = json.loads(ws_json.read_text())
            folder_uri = data.get("folder", "")
            # Handle file:// URIs
            if folder_uri.startswith("file://"):
                folder_path = folder_uri[len("file://"):]
                # URL-decode common escapes
                folder_path = folder_path.replace("%20", " ")
            elif folder_uri.startswith("vscode-remote://"):
                # SSH remote workspace - extract the path portion
                # Format: vscode-remote://ssh-remote%2B<host>/<path>
                parts = folder_uri.split("/", 3)
                if len(parts) >= 4:
                    folder_path = "/" + parts[3]
                else:
                    continue
            else:
                continue

            if os.path.normpath(folder_path) == target:
                matches.append(ws_dir)
        except (json.JSONDecodeError, OSError):
            continue

    # Sort by modification time, newest first
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches


def find_transcript_dir(project_path: str) -> Optional[Path]:
    """Find the agent-transcripts directory for a project."""
    projects_dir = get_cursor_projects_dir()
    if not projects_dir.exists():
        return None

    sanitized = sanitize_project_path(project_path)
    transcript_dir = projects_dir / sanitized / "agent-transcripts"
    if transcript_dir.exists():
        return transcript_dir

    return None


def get_project_path() -> str:
    """Get the current project path (current working directory)."""
    return os.getcwd()


def list_all_workspaces() -> list[dict]:
    """List all Cursor workspaces with metadata.

    Returns a list of dicts with:
      - folder_uri: raw URI from workspace.json
      - path: extracted filesystem path
      - type: 'local' or 'ssh'
      - host: SSH hostname (for ssh type, None for local)
      - workspace_dir: Path to the workspace directory
      - mtime: modification time of the workspace DB
    """
    ws_storage = get_workspace_storage_dir()
    if not ws_storage.exists():
        return []

    workspaces = []
    for ws_dir in ws_storage.iterdir():
        if not ws_dir.is_dir():
            continue
        ws_json = ws_dir / "workspace.json"
        if not ws_json.exists():
            continue
        try:
            data = json.loads(ws_json.read_text())
            folder_uri = data.get("folder", "")
            if not folder_uri:
                continue

            ws_type = "local"
            host = None
            folder_path = ""

            if folder_uri.startswith("file://"):
                folder_path = folder_uri[len("file://"):]
                folder_path = folder_path.replace("%20", " ")
            elif folder_uri.startswith("vscode-remote://"):
                ws_type = "ssh"
                # Format: vscode-remote://ssh-remote%2B<host>/<path>
                authority = folder_uri.split("/")[2]  # ssh-remote%2B<host>
                if "%2B" in authority:
                    host = authority.split("%2B", 1)[1]
                elif "+" in authority:
                    host = authority.split("+", 1)[1]
                parts = folder_uri.split("/", 3)
                if len(parts) >= 4:
                    folder_path = "/" + parts[3]
                else:
                    continue
            else:
                continue

            # Get DB modification time
            db_path = ws_dir / "state.vscdb"
            mtime = db_path.stat().st_mtime if db_path.exists() else 0

            workspaces.append({
                "folder_uri": folder_uri,
                "path": os.path.normpath(folder_path),
                "type": ws_type,
                "host": host,
                "workspace_dir": ws_dir,
                "mtime": mtime,
            })
        except (json.JSONDecodeError, OSError):
            continue

    # Sort by modification time, newest first
    workspaces.sort(key=lambda w: w["mtime"], reverse=True)
    return workspaces


def list_workspaces_with_conversations() -> list[dict]:
    """List workspaces that have at least one conversation.

    Returns the same dicts as list_all_workspaces(), plus a
    'conversations' key with the count.
    """
    from . import db

    result = []
    for ws in list_all_workspaces():
        db_path = ws["workspace_dir"] / "state.vscdb"
        if not db_path.exists():
            continue
        try:
            with db.CursorDB(db_path) as cdb:
                data = cdb.get_json("composer.composerData", table="ItemTable")
                if data:
                    composers = data.get("allComposers", [])
                    if composers:
                        ws["conversations"] = len(composers)
                        result.append(ws)
        except Exception:
            continue
    return result


def resolve_workspace(selector: str) -> Optional[dict]:
    """Resolve a workspace selector to a workspace dict.

    The selector can be:
      - A number (1-based index from list_workspaces_with_conversations)
      - A path substring (matched against workspace paths)
    """
    workspaces = list_workspaces_with_conversations()
    if not workspaces:
        return None

    # Try as index
    try:
        idx = int(selector)
        if 1 <= idx <= len(workspaces):
            return workspaces[idx - 1]
        return None
    except ValueError:
        pass

    # Try as path substring
    for ws in workspaces:
        if selector in ws["path"]:
            return ws

    return None


def get_sync_dir() -> Path:
    """Return the cursaves sync directory (~/.cursaves/).

    This is the git repo that holds snapshots and is synced between machines.
    """
    return Path.home() / ".cursaves"


def get_snapshots_dir() -> Path:
    """Return the snapshots directory (~/.cursaves/snapshots/)."""
    snapshots = get_sync_dir() / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    return snapshots


def is_sync_repo_initialized() -> bool:
    """Check if the sync directory is a git repo."""
    sync_dir = get_sync_dir()
    return (sync_dir / ".git").exists()


def get_machine_id() -> str:
    """Return a human-readable machine identifier."""
    import socket
    return socket.gethostname()


# ── Project identification ────────────────────────────────────────────


def get_project_identifier(project_path: str) -> str:
    """Get a stable identifier for a project, used as the snapshot subdirectory.

    Uses the git remote origin URL if available (normalized to a filesystem-safe
    string).  Falls back to the directory basename for non-git projects.

    This means:
      - Same repo under different local names (bob/ vs alice/) → same identifier
      - Different repos that happen to share a name → different identifiers
    """
    remote_url = _get_git_remote_url(project_path)
    if remote_url:
        return _normalize_remote_url(remote_url)
    return os.path.basename(os.path.normpath(project_path))


def _get_git_remote_url(project_path: str) -> Optional[str]:
    """Get the git remote origin URL for a project, if any."""
    try:
        result = subprocess.run(
            ["git", "-C", project_path, "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _normalize_remote_url(url: str) -> str:
    """Normalize a git remote URL to a stable, filesystem-safe directory name.

    git@github.com:user/repo.git     → github.com-user-repo
    https://github.com/user/repo.git → github.com-user-repo
    ssh://git@github.com/user/repo   → github.com-user-repo
    """
    # Strip trailing .git
    url = re.sub(r"\.git$", "", url)

    # SSH shorthand: git@host:user/repo
    m = re.match(r"^[\w.-]+@([\w.-]+):(.*)", url)
    if m:
        host, path = m.group(1), m.group(2)
        return _sanitize_identifier(f"{host}/{path}")

    # HTTPS / SSH URI: https://host/path or ssh://git@host/path
    m = re.match(r"^(?:https?|ssh)://(?:[\w.-]+@)?([\w.-]+)/(.*)", url)
    if m:
        host, path = m.group(1), m.group(2)
        return _sanitize_identifier(f"{host}/{path}")

    # Unknown format -- sanitize whatever we got
    return _sanitize_identifier(url)


def _sanitize_identifier(s: str) -> str:
    """Turn an arbitrary string into a safe directory name.

    Replaces slashes, colons, @, etc. with '-' and collapses runs of dashes.
    """
    s = re.sub(r"[/:@\\]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")
