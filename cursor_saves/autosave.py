"""Cursor stop-hook scheduling for push-only autosaves."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TextIO


DEBOUNCE_SECONDS = 3.0
_STARTING_LOCK_GRACE_SECONDS = 30.0


def _state_dir() -> Path:
    return Path.home() / ".config" / "cursaves" / "autosave"


def _pending_dir() -> Path:
    return _state_dir() / "pending"


def _lock_dir() -> Path:
    return _state_dir() / "worker.lock"


def _log(message: str) -> None:
    try:
        state_dir = _state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with (state_dir / "autosave.log").open("a", encoding="utf-8") as log:
            log.write(f"{timestamp} {message}\n")
    except OSError:
        pass


def _record_pending() -> Path:
    pending_dir = _pending_dir()
    pending_dir.mkdir(parents=True, exist_ok=True)
    pending = pending_dir / f"{time.time_ns()}-{uuid.uuid4().hex}"
    pending.touch()
    return pending


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lock_owner(lock_dir: Path) -> int | None:
    try:
        return int((lock_dir / "pid").read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None


def _release_lock(lock_dir: Path, pid: int | None = None) -> None:
    if pid is not None and _lock_owner(lock_dir) not in (None, pid):
        return
    try:
        shutil.rmtree(lock_dir)
    except FileNotFoundError:
        pass
    except OSError as exc:
        _log(f"could not release worker lock: {exc}")


def _acquire_lock(pid: int | None = None) -> bool:
    lock_dir = _lock_dir()
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_dir.mkdir()
    except FileExistsError:
        owner = _lock_owner(lock_dir)
        try:
            age = time.time() - lock_dir.stat().st_mtime
        except OSError:
            return False
        if (owner is None and age < _STARTING_LOCK_GRACE_SECONDS) or (
            owner is not None and _pid_is_running(owner)
        ):
            return False
        _release_lock(lock_dir)
        try:
            lock_dir.mkdir()
        except FileExistsError:
            return False
    if pid is not None:
        (lock_dir / "pid").write_text(str(pid), encoding="ascii")
    return True


def _current_executable() -> Path:
    candidate = Path(sys.argv[0]).expanduser()
    if candidate.name.lower().startswith("cursaves") and candidate.exists():
        return candidate.resolve()
    found = shutil.which("cursaves")
    if found:
        return Path(found).resolve()
    raise RuntimeError("could not resolve the current cursaves executable")


def _spawn_worker(executable: Path) -> None:
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(
        [str(executable), "autosave", "--worker"],
        **kwargs,
    )
    (_lock_dir() / "pid").write_text(str(process.pid), encoding="ascii")


def schedule(executable: Path | None = None) -> None:
    """Coalesce a stop event and ensure one detached worker is running."""
    _record_pending()
    if not _acquire_lock():
        return
    try:
        _spawn_worker(executable or _current_executable())
    except Exception:
        _release_lock(_lock_dir())
        raise


def handle_hook(stream: TextIO | None = None, executable: Path | None = None) -> None:
    """Consume a stop payload and schedule autosave without blocking Cursor."""
    try:
        payload = json.load(stream or sys.stdin)
    except Exception:
        payload = {}
    if payload.get("status", "completed") not in ("completed", "aborted"):
        return
    try:
        schedule(executable=executable)
    except Exception as exc:
        _log(f"hook scheduling failed: {exc}")


def _push_ahead() -> None:
    # Lazy import avoids making the CLI and hook scheduler import each other.
    from . import paths
    from .backends import get_backend
    from .cli import _push_ahead as push_ahead

    if not paths.is_sync_repo_initialized():
        raise RuntimeError("sync backend is not initialized")
    push_ahead(
        paths.get_sync_dir(),
        auto=True,
        backend=get_backend(),
        include_never_pushed=True,
        fail_on_push_error=True,
    )


def run_worker(
    push: Callable[[], None] = _push_ahead,
    debounce: float = DEBOUNCE_SECONDS,
) -> bool:
    """Process coalesced events. Return false so failures retry next turn."""
    pid = os.getpid()
    lock_dir = _lock_dir()
    if lock_dir.exists():
        owner = _lock_owner(lock_dir)
        if owner not in (None, pid):
            if _pid_is_running(owner):
                return True
            _release_lock(lock_dir)
            if not _acquire_lock(pid):
                return True
    elif not _acquire_lock(pid):
        return True
    try:
        (lock_dir / "pid").write_text(str(pid), encoding="ascii")
        while True:
            pending = list(_pending_dir().glob("*"))
            if not pending:
                return True

            latest = max(path.stat().st_mtime for path in pending)
            delay = debounce - (time.time() - latest)
            if delay > 0:
                time.sleep(delay)
                continue

            batch = list(_pending_dir().glob("*"))
            try:
                push()
            except Exception as exc:
                _log(f"autosave failed; will retry after next stop: {exc}")
                return False

            for path in batch:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
    finally:
        _release_lock(lock_dir, pid)


def _hook_command(executable: Path) -> str:
    argv = [str(executable.resolve()), "autosave", "--hook"]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def _is_our_hook(entry: object) -> bool:
    if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
        return False
    command = entry["command"]
    try:
        argv = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return False
    return len(argv) >= 3 and Path(argv[0].strip('"')).name.lower().startswith(
        "cursaves"
    ) and argv[-2:] == ["autosave", "--hook"]


def install_hook(
    executable: Path | None = None,
    hooks_path: Path | None = None,
) -> tuple[Path, bool]:
    """Merge the user stop hook while preserving every unrelated setting."""
    executable = (executable or _current_executable()).resolve()
    hooks_path = hooks_path or Path.home() / ".cursor" / "hooks.json"

    if hooks_path.exists():
        config = json.loads(hooks_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("hooks.json must contain a JSON object")
    else:
        config = {}

    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks.json 'hooks' must be an object")
    stop_hooks = hooks.setdefault("stop", [])
    if not isinstance(stop_hooks, list):
        raise ValueError("hooks.json 'hooks.stop' must be an array")

    desired = {"command": _hook_command(executable), "timeout": 5}
    merged = [entry for entry in stop_hooks if not _is_our_hook(entry)]
    merged.append(desired)
    changed = stop_hooks != merged or "version" not in config
    if not changed:
        return hooks_path, False

    hooks["stop"] = merged
    config.setdefault("version", 1)
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{hooks_path.name}.",
        dir=hooks_path.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(config, output, indent=2)
            output.write("\n")
        os.replace(temporary, hooks_path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return hooks_path, True
