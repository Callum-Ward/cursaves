import json
import sqlite3

from cursor_saves import importer, paths
from cursor_saves.db import CursorDB


def _create_global_db(path, typed=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE ItemTable (key TEXT UNIQUE, value BLOB)")
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT UNIQUE, value BLOB)")
        if typed:
            conn.execute(
                """CREATE TABLE composerHeaders (
                    composerId TEXT PRIMARY KEY, workspaceId TEXT,
                    createdAt INTEGER, lastUpdatedAt INTEGER,
                    isArchived INTEGER, isSubagent INTEGER, recency INTEGER,
                    checkpointAt INTEGER, value TEXT
                )"""
            )


def _create_workspace(path, project):
    path.mkdir(parents=True)
    (path / "workspace.json").write_text(
        json.dumps({"folder": project.as_uri()})
    )
    with sqlite3.connect(path / "state.vscdb") as conn:
        conn.execute("CREATE TABLE ItemTable (key TEXT UNIQUE, value BLOB)")
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT UNIQUE, value BLOB)")
        conn.execute(
            "INSERT INTO ItemTable VALUES (?, ?)",
            (
                "composer.composerData",
                json.dumps({"selectedComposerIds": []}),
            ),
        )


def test_workspace_registration_dual_writes_header_stores(tmp_path, monkeypatch):
    global_db = tmp_path / "global" / "state.vscdb"
    _create_global_db(global_db)
    project = tmp_path / "project"
    project.mkdir()
    ws_dir = tmp_path / "workspaceStorage" / "workspace-id"
    _create_workspace(ws_dir, project)
    monkeypatch.setattr(paths, "get_global_db_path", lambda: global_db)

    composer_data = {
        "composerId": "chat-1",
        "name": "Imported",
        "createdAt": 10,
        "lastUpdatedAt": 20,
    }
    assert importer._register_in_workspace("chat-1", composer_data, ws_dir)

    with CursorDB(global_db) as cdb:
        legacy = cdb.get_json("composer.composerHeaders", table="ItemTable")
        typed = cdb.get_composer_headers()
    assert legacy["allComposers"][0]["composerId"] == "chat-1"
    assert typed[0]["composerId"] == "chat-1"
    assert typed[0]["workspaceIdentifier"]["id"] == "workspace-id"


def test_path_import_refuses_unopened_workspace_before_global_write(
    tmp_path, monkeypatch, capsys
):
    global_db = tmp_path / "global" / "state.vscdb"
    snapshot = tmp_path / "chat.json"
    snapshot.write_text(
        json.dumps(
            {
                "version": 3,
                "composerId": "chat-1",
                "sourceProjectPath": "/source/project",
                "composerData": {
                    "composerId": "chat-1",
                    "name": "Import me",
                    "fullConversationHeadersOnly": [],
                },
            }
        )
    )
    monkeypatch.setattr(paths, "get_global_db_path", lambda: global_db)
    monkeypatch.setattr(paths, "find_workspace_dirs_for_project", lambda _: [])

    assert not importer.import_snapshot(snapshot, str(tmp_path / "never-opened"))
    assert not global_db.exists()
    assert "Open that folder in Cursor once" in capsys.readouterr().err


def test_purge_deletes_both_header_formats(tmp_path, monkeypatch):
    global_db = tmp_path / "global" / "state.vscdb"
    _create_global_db(global_db)
    entry = {
        "composerId": "chat-1",
        "name": "Delete me",
        "createdAt": 1,
        "workspaceIdentifier": {"id": "workspace-id"},
    }
    with CursorDB(global_db) as cdb:
        cdb.write_json("composerData:chat-1", {"name": "Delete me"})
        cdb.write_json(
            "composer.composerHeaders",
            {"allComposers": [entry]},
            table="ItemTable",
        )
        cdb.write_composer_headers([entry])

    monkeypatch.setattr(paths, "get_global_db_path", lambda: global_db)
    monkeypatch.setattr(paths, "list_all_workspaces", lambda: [])
    monkeypatch.setattr(importer, "is_cursor_running", lambda: False)
    monkeypatch.setattr(importer.db, "backup_db", lambda path: path)

    assert importer.purge_chats(["chat-1"]) == (1, 1)
    with CursorDB(global_db) as cdb:
        legacy = cdb.get_json("composer.composerHeaders", table="ItemTable")
        assert legacy["allComposers"] == []
        assert cdb.get_composer_headers() == []


def test_doctor_repoints_duplicate_workspace_headers(tmp_path, monkeypatch):
    global_db = tmp_path / "global" / "state.vscdb"
    _create_global_db(global_db)
    storage = tmp_path / "workspaceStorage"
    project = tmp_path / "project"
    project.mkdir()
    stale = storage / "stale-id"
    active = storage / "active-id"
    _create_workspace(stale, project)
    _create_workspace(active, project)
    with CursorDB(stale / "state.vscdb") as cdb:
        cdb.write_json(
            "composer.composerData",
            {"selectedComposerIds": ["chat-1"]},
            table="ItemTable",
        )

    stale_entry = {
        "composerId": "chat-1",
        "name": "Moved",
        "createdAt": 1,
        "workspaceIdentifier": {"id": "stale-id"},
    }
    with CursorDB(global_db) as cdb:
        cdb.write_json(
            "workspaceMetadata.entries",
            {"entries": [{"workspaceId": "active-id"}]},
            table="ItemTable",
        )
        cdb.write_json(
            "composer.composerHeaders",
            {"allComposers": [stale_entry]},
            table="ItemTable",
        )
        cdb.write_composer_headers([stale_entry])

    monkeypatch.setattr(paths, "get_global_db_path", lambda: global_db)
    monkeypatch.setattr(paths, "get_workspace_storage_dir", lambda: storage)

    result = importer.repair_header_store_drift_and_duplicates()
    assert result["duplicates_repointed"] == 1
    assert result["registrations_merged"] == 1
    with CursorDB(global_db) as cdb:
        legacy = cdb.get_json("composer.composerHeaders", table="ItemTable")
        typed = cdb.get_composer_headers()
    with CursorDB(active / "state.vscdb") as cdb:
        canonical_registration = cdb.get_json(
            "composer.composerData", table="ItemTable"
        )
    assert legacy["allComposers"][0]["workspaceIdentifier"]["id"] == "active-id"
    assert typed[0]["workspaceIdentifier"]["id"] == "active-id"
    assert canonical_registration["selectedComposerIds"] == ["chat-1"]
