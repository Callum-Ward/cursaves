import json
import os
import sqlite3

from cursor_saves import paths
from cursor_saves.db import CursorDB


def _create_global_db(path):
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE ItemTable (key TEXT UNIQUE, value BLOB)")
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT UNIQUE, value BLOB)")
        conn.execute(
            """CREATE TABLE composerHeaders (
                composerId TEXT PRIMARY KEY, workspaceId TEXT,
                createdAt INTEGER, lastUpdatedAt INTEGER,
                isArchived INTEGER, isSubagent INTEGER, recency INTEGER,
                checkpointAt INTEGER, value TEXT
            )"""
        )


def test_global_headers_merge_with_typed_rows_authoritative(tmp_path, monkeypatch):
    global_db = tmp_path / "global" / "state.vscdb"
    _create_global_db(global_db)
    monkeypatch.setattr(paths, "get_global_db_path", lambda: global_db)

    legacy = {
        "allComposers": [
            {
                "composerId": "shared",
                "name": "legacy",
                "workspaceIdentifier": {"id": "old-workspace"},
            },
            {"composerId": "legacy-only", "name": "old Cursor"},
        ]
    }
    typed = {
        "composerId": "shared",
        "name": "typed",
        "createdAt": 1,
        "workspaceIdentifier": {"id": "active-workspace"},
    }
    with CursorDB(global_db) as cdb:
        cdb.write_json("composer.composerHeaders", legacy, table="ItemTable")
        cdb.write_composer_headers([typed])

    merged = {
        entry["composerId"]: entry
        for entry in paths.get_global_composer_headers()
    }
    assert set(merged) == {"shared", "legacy-only"}
    assert merged["shared"]["name"] == "typed"
    assert merged["shared"]["workspaceIdentifier"]["id"] == "active-workspace"


def test_workspace_resolution_prefers_metadata_workspace_id(tmp_path, monkeypatch):
    global_db = tmp_path / "global" / "state.vscdb"
    _create_global_db(global_db)
    storage = tmp_path / "workspaceStorage"
    storage.mkdir()
    project = tmp_path / "project"
    project.mkdir()

    stale = storage / "stale-id"
    active = storage / "active-id"
    for ws_dir in (stale, active):
        ws_dir.mkdir()
        (ws_dir / "workspace.json").write_text(
            json.dumps({"folder": project.as_uri()})
        )
    os.utime(stale, (200, 200))
    os.utime(active, (100, 100))

    with CursorDB(global_db) as cdb:
        cdb.write_json(
            "workspaceMetadata.entries",
            {"entries": [{"workspaceId": "active-id"}]},
            table="ItemTable",
        )

    monkeypatch.setattr(paths, "get_global_db_path", lambda: global_db)
    monkeypatch.setattr(paths, "get_workspace_storage_dir", lambda: storage)

    assert paths.find_workspace_dirs_for_project(str(project)) == [active, stale]
    listed = paths.list_all_workspaces()
    assert next(ws for ws in listed if ws["is_canonical"])["workspace_dir"] == active
