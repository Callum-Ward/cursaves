"""Safe SQLite reader/writer for Cursor's state.vscdb databases."""

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Optional


class CursorDB:
    """Safe interface to a Cursor state.vscdb database.

    All reads are performed on a temporary copy of the database to avoid
    locking conflicts with a running Cursor instance. Writes operate on
    the original file and require Cursor to be closed.
    """

    def __init__(self, db_path: Path, no_copy: bool = True):
        self.db_path = db_path
        self.no_copy = no_copy
        self._tmp_path: Optional[Path] = None
        self._conn: Optional[sqlite3.Connection] = None
        self._in_transaction = False

    @staticmethod
    def _probe_readable(conn: sqlite3.Connection) -> None:
        """Raise OperationalError if the connection cannot read Cursor tables."""
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('cursorDiskKV', 'ItemTable')"
            ).fetchall()
        ]
        if not tables:
            raise sqlite3.OperationalError("no Cursor tables visible")
        for table in tables:
            conn.execute(f"SELECT key FROM {table} LIMIT 1").fetchone()

    def _ensure_read_copy(self) -> sqlite3.Connection:
        """Copy the database to a temp file and open a read-only connection."""
        if self._conn is not None:
            return self._conn

        if not self.db_path.exists():
            raise FileNotFoundError(f"Database not found: {self.db_path}")

        # Try to connect directly in read-only and lock-free mode for speed.
        # Must validate real table reads: nolock can open the file but return an
        # empty/unreadable WAL view (paths with spaces, or Cursor actively writing).
        if self.no_copy:
            try:
                db_uri = f"{self.db_path.resolve().as_uri()}?mode=ro&nolock=1"
                conn = sqlite3.connect(db_uri, uri=True)
                conn.execute("SELECT 1").fetchone()
                self._probe_readable(conn)
                self._conn = conn
                return self._conn
            except sqlite3.OperationalError:
                pass

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
        """Close connections and clean up temp files."""
        if self._conn:
            self._conn.close()
            self._conn = None
        if hasattr(self, "_write_conn") and self._write_conn:
            self._write_conn.close()
            self._write_conn = None
        if self._tmp_path:
            shutil.rmtree(self._tmp_path.parent, ignore_errors=True)
            self._tmp_path = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def transaction(self):
        """Context manager to run multiple write operations in a single transaction."""
        conn = self._get_write_conn()
        if self._in_transaction:
            yield
            return

        self._in_transaction = True
        conn.execute("BEGIN")
        try:
            yield
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            self._in_transaction = False

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

    def get_item_binary(self, key: str, table: str = "ItemTable") -> Optional[bytes]:
        """Get a raw binary value from the key-value store."""
        conn = self._ensure_read_copy()
        try:
            row = conn.execute(
                f"SELECT value FROM {table} WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            val = row[0]
            if isinstance(val, str):
                return val.encode("utf-8")
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

    def count_keys_by_chat_prefix(
        self, key_type: str, table: str = "cursorDiskKV"
    ) -> dict[str, int]:
        """Count keys grouped by chat ID for a given key type prefix.

        For example, key_type="bubbleId" counts all keys like
        "bubbleId:<uuid>:..." and returns {<uuid>: count, ...}.

        Uses a single SQL query — efficient even on large databases.
        """
        conn = self._ensure_read_copy()
        result: dict[str, int] = {}
        try:
            prefix = key_type + ":"
            rows = conn.execute(
                f"""SELECT SUBSTR(key, {len(prefix) + 1}, 36) AS cid, COUNT(*)
                    FROM {table}
                    WHERE key LIKE ?
                    GROUP BY cid""",
                (prefix + "%",),
            ).fetchall()
            for cid, count in rows:
                if cid and len(cid) == 36:
                    result[cid] = count
        except sqlite3.OperationalError:
            pass
        return result

    def get_json(self, key: str, table: str = "cursorDiskKV") -> Optional[Any]:
        """Get and parse a JSON value."""
        raw = self.get_item(key, table=table)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def get_items_by_prefix(self, prefix: str, table: str = "cursorDiskKV") -> dict[str, str]:
        """Get all key-value pairs matching a prefix as strings."""
        conn = self._ensure_read_copy()
        try:
            rows = conn.execute(
                f"SELECT key, value FROM {table} WHERE key LIKE ?", (prefix + "%",)
            ).fetchall()
            result = {}
            for k, v in rows:
                if isinstance(v, bytes):
                    result[k] = v.decode("utf-8", errors="replace")
                elif v is not None:
                    result[k] = v
            return result
        except sqlite3.OperationalError:
            return {}

    def get_json_items_by_prefix(self, prefix: str, table: str = "cursorDiskKV") -> dict[str, Any]:
        """Get all key-value pairs matching a prefix and parse values as JSON."""
        conn = self._ensure_read_copy()
        try:
            rows = conn.execute(
                f"SELECT key, value FROM {table} WHERE key LIKE ?", (prefix + "%",)
            ).fetchall()
            result = {}
            for k, v in rows:
                if isinstance(v, bytes):
                    v = v.decode("utf-8", errors="replace")
                try:
                    result[k] = json.loads(v) if v is not None else None
                except json.JSONDecodeError:
                    result[k] = None
            return result
        except sqlite3.OperationalError:
            return {}

    def get_items_by_keys_binary(self, keys: list[str], table: str = "cursorDiskKV") -> dict[str, bytes]:
        """Get multiple raw binary values from the key-value store in a single query."""
        if not keys:
            return {}
        conn = self._ensure_read_copy()
        result = {}
        try:
            for i in range(0, len(keys), 500):
                batch = keys[i : i + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT key, value FROM {table} WHERE key IN ({placeholders})", batch
                ).fetchall()
                for k, v in rows:
                    if isinstance(v, str):
                        result[k] = v.encode("utf-8")
                    elif v is not None:
                        result[k] = v
            return result
        except sqlite3.OperationalError:
            return {}

    # ── Write operations (on original file) ─────────────────────────

    def _get_write_conn(self) -> sqlite3.Connection:
        """Get or create a connection for write operations on the ORIGINAL database."""
        if not hasattr(self, "_write_conn") or self._write_conn is None:
            self._write_conn = sqlite3.connect(str(self.db_path))
        return self._write_conn

    def write_item(self, key: str, value: str, table: str = "ItemTable"):
        """Write a value to the key-value store on the ORIGINAL database.

        This operates directly on the original file, not the temp copy.
        Caller must ensure Cursor is not running.
        """
        conn = self._get_write_conn()
        conn.execute(
            f"INSERT OR REPLACE INTO {table} (key, value) VALUES (?, ?)",
            (key, value),
        )
        if not self._in_transaction:
            conn.commit()

    def write_disk_kv(self, key: str, value: str):
        """Write a value to cursorDiskKV on the ORIGINAL database."""
        self.write_item(key, value, table="cursorDiskKV")

    def write_json(self, key: str, data: Any, table: str = "cursorDiskKV"):
        """Write a JSON value to the ORIGINAL database."""
        self.write_item(key, json.dumps(data, separators=(",", ":")), table=table)

    def write_batch(self, items: list[tuple[str, str]], table: str = "cursorDiskKV"):
        """Write multiple key-value pairs in a single transaction.

        Much faster than calling write_item() in a loop -- uses one
        connection and one transaction for all items.
        """
        conn = self._get_write_conn()
        in_txn = self._in_transaction
        if not in_txn:
            conn.execute("BEGIN")
        try:
            conn.executemany(
                f"INSERT OR REPLACE INTO {table} (key, value) VALUES (?, ?)",
                items,
            )
            if not in_txn:
                conn.execute("COMMIT")
        except Exception:
            if not in_txn:
                conn.execute("ROLLBACK")
            raise

    def write_json_batch(self, items: list[tuple[str, Any]], table: str = "cursorDiskKV"):
        """Write multiple JSON key-value pairs in a single transaction."""
        serialized = [
            (key, json.dumps(data, separators=(",", ":")))
            for key, data in items
        ]
        self.write_batch(serialized, table=table)

    def delete_keys(self, keys: list[str], table: str = "cursorDiskKV") -> int:
        """Delete multiple keys in a single transaction on the ORIGINAL database.

        Returns the number of rows deleted.
        """
        if not keys:
            return 0
        conn = self._get_write_conn()
        in_txn = self._in_transaction
        if not in_txn:
            conn.execute("BEGIN")
        try:
            total = 0
            for batch_start in range(0, len(keys), 500):
                batch = keys[batch_start:batch_start + 500]
                placeholders = ",".join("?" for _ in batch)
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE key IN ({placeholders})", batch
                )
                total += cur.rowcount
            if not in_txn:
                conn.execute("COMMIT")
            return total
        except Exception:
            if not in_txn:
                conn.execute("ROLLBACK")
            raise

    def delete_keys_by_prefix(self, prefix: str, table: str = "cursorDiskKV") -> int:
        """Delete all keys matching a prefix on the ORIGINAL database.

        Returns the number of rows deleted.
        """
        conn = self._get_write_conn()
        cur = conn.execute(
            f"DELETE FROM {table} WHERE key LIKE ?", (prefix + "%",)
        )
        if not self._in_transaction:
            conn.commit()
        return cur.rowcount



def backup_db(db_path: Path, keep: int = 2) -> Path:
    """Create a timestamped backup of a database file.

    Keeps only the most recent `keep` backups (default 2) and deletes
    older ones to prevent unbounded disk usage. The global DB can be
    multi-GB, so even a handful of stale backups can fill a disk.

    Returns the path to the new backup.
    """
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.parent / f"{db_path.stem}.backup_{timestamp}{db_path.suffix}"
    shutil.copy2(db_path, backup_path)

    for suffix in ("-wal", "-shm"):
        wal = db_path.parent / (db_path.name + suffix)
        if wal.exists():
            shutil.copy2(wal, db_path.parent / (backup_path.name + suffix))

    # Clean up old backups, keeping only the newest `keep`
    pattern = f"{db_path.stem}.backup_*{db_path.suffix}"
    old_backups = sorted(
        db_path.parent.glob(pattern),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in old_backups[keep:]:
        stale.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            sidecar = stale.parent / (stale.name + suffix)
            sidecar.unlink(missing_ok=True)

    return backup_path
