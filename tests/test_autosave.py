import io
import json
import os
import time
from pathlib import Path

import pytest

from cursor_saves import autosave, cli


@pytest.fixture
def autosave_state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(autosave, "_state_dir", lambda: state)
    return state


@pytest.mark.parametrize("status", ["completed", "aborted"])
def test_stop_hook_schedules_all_terminal_statuses(status, monkeypatch):
    scheduled = []
    monkeypatch.setattr(
        autosave, "schedule", lambda executable=None: scheduled.append(executable)
    )

    executable = Path("/absolute/cursaves")
    autosave.handle_hook(io.StringIO(json.dumps({"status": status})), executable)

    assert scheduled == [executable]


def test_stop_hook_skips_error_status(monkeypatch):
    scheduled = []
    monkeypatch.setattr(
        autosave, "schedule", lambda executable=None: scheduled.append(executable)
    )

    autosave.handle_hook(io.StringIO(json.dumps({"status": "error"})))

    assert scheduled == []


def test_autosave_hook_cli_is_wired_and_returns_json(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(autosave, "handle_hook", lambda: called.append(True))
    monkeypatch.setattr("sys.argv", ["cursaves", "autosave", "--hook"])

    cli.main()

    assert called == [True]
    assert capsys.readouterr().out == "{}\n"


def test_schedule_coalesces_on_one_detached_worker(autosave_state, monkeypatch):
    spawned = []
    monkeypatch.setattr(autosave, "_spawn_worker", lambda executable: spawned.append(executable))

    executable = Path("/absolute/cursaves")
    autosave.schedule(executable)
    autosave.schedule(executable)

    assert spawned == [executable]
    assert len(list(autosave._pending_dir().iterdir())) == 2


def test_worker_processes_events_arriving_during_push(autosave_state):
    first = autosave._record_pending()
    old = time.time() - 10
    os.utime(first, (old, old))
    pushes = 0

    def push():
        nonlocal pushes
        pushes += 1
        if pushes == 1:
            new = autosave._record_pending()
            os.utime(new, (old, old))

    assert autosave.run_worker(push=push, debounce=0)
    assert pushes == 2
    assert list(autosave._pending_dir().iterdir()) == []
    assert not autosave._lock_dir().exists()


def test_worker_leaves_failed_batch_for_next_turn(autosave_state):
    pending = autosave._record_pending()

    def fail():
        raise RuntimeError("network unavailable")

    assert not autosave.run_worker(push=fail, debounce=0)
    assert pending.exists()
    assert "will retry after next stop" in (
        autosave_state / "autosave.log"
    ).read_text()
    assert not autosave._lock_dir().exists()


def test_push_ahead_includes_new_conversations(monkeypatch, tmp_path):
    calls = []
    backend = object()
    monkeypatch.setattr("cursor_saves.paths.is_sync_repo_initialized", lambda: True)
    monkeypatch.setattr("cursor_saves.paths.get_sync_dir", lambda: tmp_path)
    monkeypatch.setattr("cursor_saves.backends.get_backend", lambda: backend)
    monkeypatch.setattr(
        "cursor_saves.cli._push_ahead",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    autosave._push_ahead()

    assert calls == [
        (
            (tmp_path,),
            {
                "auto": True,
                "backend": backend,
                "include_never_pushed": True,
                "fail_on_push_error": True,
            },
        )
    ]


def test_worker_does_not_join_an_existing_live_worker(autosave_state, monkeypatch):
    autosave._record_pending()
    lock = autosave._lock_dir()
    lock.mkdir()
    (lock / "pid").write_text("123")
    monkeypatch.setattr(autosave, "_pid_is_running", lambda pid: pid == 123)
    pushes = []

    assert autosave.run_worker(push=lambda: pushes.append(True), debounce=0)
    assert pushes == []
    assert (lock / "pid").read_text() == "123"


def test_install_hook_merges_and_is_idempotent(tmp_path):
    hooks_path = tmp_path / ".cursor" / "hooks.json"
    hooks_path.parent.mkdir()
    hooks_path.write_text(
        json.dumps(
            {
                "version": 1,
                "theme": "custom",
                "hooks": {
                    "stop": [{"command": "/other/tool --stop", "timeout": 10}],
                    "afterFileEdit": [{"command": "/other/tool --edit"}],
                },
            }
        )
    )
    executable = tmp_path / "bin with spaces" / "cursaves"

    path, changed = autosave.install_hook(executable, hooks_path)
    _, changed_again = autosave.install_hook(executable, hooks_path)
    installed = json.loads(hooks_path.read_text())

    assert path == hooks_path
    assert changed
    assert not changed_again
    assert installed["theme"] == "custom"
    assert installed["hooks"]["afterFileEdit"] == [{"command": "/other/tool --edit"}]
    assert installed["hooks"]["stop"][0]["command"] == "/other/tool --stop"
    assert installed["hooks"]["stop"][1] == {
        "command": f"'{executable}' autosave --hook",
        "timeout": 5,
    }


def test_install_hook_replaces_previous_cursaves_executable(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "stop": [
                        {"command": "/old/bin/cursaves autosave --hook", "timeout": 5},
                        {"command": "keep-me"},
                    ]
                }
            }
        )
    )

    autosave.install_hook(Path("/new/bin/cursaves"), hooks_path)
    stop_hooks = json.loads(hooks_path.read_text())["hooks"]["stop"]

    assert stop_hooks == [
        {"command": "keep-me"},
        {"command": "/new/bin/cursaves autosave --hook", "timeout": 5},
    ]


def test_install_hook_does_not_overwrite_invalid_json(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text("{broken")

    with pytest.raises(json.JSONDecodeError):
        autosave.install_hook(Path("/bin/cursaves"), hooks_path)

    assert hooks_path.read_text() == "{broken"
