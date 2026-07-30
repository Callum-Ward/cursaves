import json
import sqlite3

from cursor_saves.db import CursorDB


def _create_db(path, typed=True):
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


def test_typed_composer_header_crud(tmp_path):
    db_path = tmp_path / "state.vscdb"
    _create_db(db_path)
    entry = {
        "composerId": "chat-1",
        "name": "Typed",
        "createdAt": 10,
        "lastUpdatedAt": 20,
        "workspaceIdentifier": {"id": "workspace-1"},
    }

    with CursorDB(db_path) as cdb:
        assert cdb.write_composer_headers([entry])

    # Typed columns override stale fields inside the denormalized JSON value.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """UPDATE composerHeaders
               SET workspaceId = ?, lastUpdatedAt = ?, value = ?""",
            (
                "workspace-2",
                30,
                json.dumps({**entry, "lastUpdatedAt": 1}),
            ),
        )

    with CursorDB(db_path) as cdb:
        assert cdb.get_composer_headers() == [
            {
                **entry,
                "lastUpdatedAt": 30,
                "workspaceIdentifier": {"id": "workspace-2"},
                "isArchived": False,
            }
        ]
        assert cdb.delete_composer_headers(["chat-1"]) == 1

    with CursorDB(db_path) as cdb:
        assert cdb.get_composer_headers() == []


def test_typed_header_writes_are_optional_for_old_cursor(tmp_path):
    db_path = tmp_path / "state.vscdb"
    _create_db(db_path, typed=False)

    with CursorDB(db_path) as cdb:
        assert cdb.get_composer_headers() == []
        assert not cdb.write_composer_headers([{"composerId": "chat-1"}])
        assert cdb.delete_composer_headers(["chat-1"]) == 0
