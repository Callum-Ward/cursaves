"""Safe SQLite reader/writer for Cursor's state.vscdb databases."""

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Optional


class CursorDB:
    """Safe interface to a Cursor state.vscdb database.

    All reads are performed on a temporary copy of the database to avoid
    locking conflicts with a running Cursor instance. Writes operate on
    the original file and require Cursor to be closed.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._tmp_path: Optional[Path] = None
        self._conn: Optional[sqlite3.Connection] = None

    def _ensure_read_copy(self) -> sqlite3.Connection:
        """Copy the database to a temp file and open a read-only connection."""
        if self._conn is not None:
            return self._conn

        if not self.db_path.exists():
            raise FileNotFoundError(f"Database not found: {self.db_path}")

        # Copy the main db file and any WAL/SHM files
        tmp_dir = tempfile.mkdtemp(prefix="cursaves-")
        tmp_db = Path(tmp_dir) / "state.vscdb"
        shutil.copy2(self.db_path, tmp_db)

        # Also copy WAL and SHM if they exist (needed for recent writes)
        for suffix in ("-wal", "-shm"):
            wal_file = self.db_path.parent / (self.db_path.name + suffix)
            if wal_file.exists():
                shutil.copy2(wal_file, Path(tmp_dir) / (tmp_db.name + suffix))

        self._tmp_path = tmp_db
        self._conn = sqlite3.connect(str(tmp_db))
        # Checkpoint WAL into main db for consistent reads
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass  # Not in WAL mode, that's fine
        return self._conn

    def close(self):
        """Close connection and clean up temp files."""
        if self._conn:
            self._conn.close()
            self._conn = None
        if self._tmp_path:
            shutil.rmtree(self._tmp_path.parent, ignore_errors=True)
            self._tmp_path = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ── Read operations (on temp copy) ──────────────────────────────

    def get_item(self, key: str, table: str = "ItemTable") -> Optional[str]:
        """Get a value from the key-value store."""
        conn = self._ensure_read_copy()
        try:
            row = conn.execute(
                f"SELECT value FROM {table} WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            val = row[0]
            if isinstance(val, bytes):
                return val.decode("utf-8", errors="replace")
            return val
        except sqlite3.OperationalError:
            return None

    def get_disk_kv(self, key: str) -> Optional[str]:
        """Get a value from the cursorDiskKV table."""
        return self.get_item(key, table="cursorDiskKV")

    def list_keys(self, prefix: str = "", table: str = "cursorDiskKV") -> list[str]:
        """List all keys in a table, optionally filtered by prefix."""
        conn = self._ensure_read_copy()
        try:
            if prefix:
                rows = conn.execute(
                    f"SELECT key FROM {table} WHERE key LIKE ?", (prefix + "%",)
                ).fetchall()
            else:
                rows = conn.execute(f"SELECT key FROM {table}").fetchall()
            return [r[0] for r in rows]
        except sqlite3.OperationalError:
            return []

    def get_json(self, key: str, table: str = "cursorDiskKV") -> Optional[Any]:
        """Get and parse a JSON value."""
        raw = self.get_item(key, table=table)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    # ── Write operations (on original file) ─────────────────────────

    def write_item(self, key: str, value: str, table: str = "ItemTable"):
        """Write a value to the key-value store on the ORIGINAL database.

        This operates directly on the original file, not the temp copy.
        Caller must ensure Cursor is not running.
        """
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                f"INSERT OR REPLACE INTO {table} (key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()
        finally:
            conn.close()

    def write_disk_kv(self, key: str, value: str):
        """Write a value to cursorDiskKV on the ORIGINAL database."""
        self.write_item(key, value, table="cursorDiskKV")

    def write_json(self, key: str, data: Any, table: str = "cursorDiskKV"):
        """Write a JSON value to the ORIGINAL database."""
        self.write_item(key, json.dumps(data, separators=(",", ":")), table=table)


def backup_db(db_path: Path) -> Path:
    """Create a timestamped backup of a database file.

    Returns the path to the backup.
    """
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.parent / f"{db_path.stem}.backup_{timestamp}{db_path.suffix}"
    shutil.copy2(db_path, backup_path)

    # Also backup WAL/SHM
    for suffix in ("-wal", "-shm"):
        wal = db_path.parent / (db_path.name + suffix)
        if wal.exists():
            shutil.copy2(wal, db_path.parent / (backup_path.name + suffix))

    return backup_path
