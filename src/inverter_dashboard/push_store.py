"""Private, bounded SQLite persistence for Web Push state.

Endpoint URLs and subscription keys are bearer capabilities. They stay in this
owner-only database and are never included in operational logs or list APIs.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any

MAX_SUBSCRIPTIONS = 64
MAX_EVENTS = 4096
MAX_DELIVERIES = 1024
DELIVERY_COUNT_SQL = "SELECT COUNT(*) FROM deliveries"
EXPIRE_DELIVERIES_SQL = "DELETE FROM deliveries WHERE expires_at<=?"


class SubscriptionConflict(ValueError):
    """Existing endpoint capability has different encryption material."""


class PushStore:
    """Single-process store; transactionally reserve events before queueing."""

    def __init__(self, directory: Path):
        if os.name != "posix":
            raise OSError("Private Web Push storage is unavailable on this platform")
        self.db = None
        self._lock_fd = None
        directory = directory.absolute()
        if any(parent.is_symlink() for parent in (directory, *directory.parents)):
            raise ValueError("Push data directory must not traverse symlinks")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or not self._owned(info):
            raise ValueError("Push data directory must be owner-private")
        directory.chmod(0o700)
        try:
            self._lock_fd = self._private_file(directory / "push.lock")
            self._lock(self._lock_fd)
            marker = os.read(self._lock_fd, 32)
            if marker not in (b"", b"initialized-v1\n"):
                raise ValueError("Push store initialization marker is invalid")
            self._initialized = bool(marker)
            self._open_database(directory)
        except Exception:
            self.close()
            raise

    @staticmethod
    def _owned(info) -> bool:
        return not hasattr(os, "getuid") or info.st_uid == os.getuid()

    @classmethod
    def _validate_file(cls, info) -> None:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not cls._owned(info):
            raise ValueError("Push store files must be owner-private regular files")

    @classmethod
    def _private_file(cls, path: Path) -> int:
        # lstat also rejects symlinks on platforms without O_NOFOLLOW. The
        # containing directory is private and the lifetime lock precedes SQLite.
        if path.exists() or path.is_symlink():
            cls._validate_file(path.lstat())
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            cls._validate_file(os.fstat(fd))
            os.fchmod(fd, 0o600)
        except Exception:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def _lock(fd: int) -> None:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _open_database(self, directory: Path) -> None:
        path = directory / "push.sqlite3"
        self.new_database = not path.exists()
        if self.new_database and self._initialized:
            raise ValueError("Initialized push store database is missing")
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = directory / (path.name + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                self._validate_file(sidecar.lstat())
                sidecar.chmod(0o600)
        fd = self._private_file(path)
        try:
            self.db = sqlite3.connect(path, timeout=2)
        finally:
            os.close(fd)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA max_page_count=16384")
        if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Push store integrity check failed")
        if self.new_database:
            self.db.executescript(
                """
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value BLOB NOT NULL);
                CREATE TABLE subscriptions (
                    id TEXT PRIMARY KEY, subscription TEXT NOT NULL, preferences TEXT NOT NULL,
                    updated_at REAL NOT NULL, last_test_at REAL NOT NULL DEFAULT 0);
                CREATE TABLE events (id TEXT PRIMARY KEY, observed_at REAL NOT NULL);
                CREATE TABLE deliveries (
                    event_id TEXT NOT NULL, subscription_id TEXT NOT NULL,
                    payload TEXT NOT NULL, expires_at REAL NOT NULL, next_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, epoch TEXT NOT NULL,
                    PRIMARY KEY(event_id, subscription_id),
                    FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE);
                """
            )
        self._validate_existing_rows()

    def _validate_existing_rows(self) -> None:
        # Read every bounded row before starting workers. Missing tables, bad
        # JSON or broken foreign keys never trigger a reset or key regeneration.
        if self.db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Push store reference check failed")
        if (
            self.count() > MAX_SUBSCRIPTIONS
            or self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] > MAX_EVENTS
            or self.db.execute(DELIVERY_COUNT_SQL).fetchone()[0] > MAX_DELIVERIES
        ):
            raise ValueError("Push store exceeds limits")
        self.db.execute("SELECT key,value FROM metadata").fetchall()
        self.subscriptions()
        for row in self.db.execute("SELECT payload,attempts FROM deliveries"):
            payload = json.loads(row["payload"])
            if not isinstance(payload, dict) or row["attempts"] not in range(3):
                raise ValueError("Push store contains invalid delivery")

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def queued_payloads(self):
        for row in self.db.execute("SELECT event_id,payload FROM deliveries"):
            yield row["event_id"], json.loads(row["payload"])

    def mark_initialized(self) -> None:
        """Remember initialized state separately so DB loss never rotates VAPID."""
        if not self._initialized:
            os.lseek(self._lock_fd, 0, os.SEEK_SET)
            os.write(self._lock_fd, b"initialized-v1\n")
            os.fsync(self._lock_fd)
            self._initialized = True

    def metadata(self, key: str) -> bytes | None:
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return bytes(row[0]) if row is not None else None

    def initialize_metadata(self, key: str, value: bytes) -> bytes:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES (?, ?)", (key, value))
        return self.metadata(key) or value

    def register(self, subscription: dict, preferences: dict, now: float) -> str:
        identifier = hashlib.sha256(subscription["endpoint"].encode()).hexdigest()
        with self.db:
            current = self.get(identifier)
            if current is not None and current["subscription"] != subscription:
                raise SubscriptionConflict("Push subscription keys conflict")
            if current is None and self.count() >= MAX_SUBSCRIPTIONS:
                raise ValueError("Push subscription limit reached")
            self.db.execute(
                "INSERT INTO subscriptions(id,subscription,preferences,updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET subscription=excluded.subscription, "
                "preferences=excluded.preferences, updated_at=excluded.updated_at",
                (identifier, json.dumps(subscription), json.dumps(preferences), now),
            )
            self.db.execute("DELETE FROM deliveries WHERE subscription_id=?", (identifier,))
        return identifier

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]

    def get(self, identifier: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM subscriptions WHERE id=?", (identifier,)).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "subscription": json.loads(row["subscription"]),
            "preferences": json.loads(row["preferences"]),
            "updated_at": row["updated_at"],
        }

    def subscriptions(self) -> list[dict[str, Any]]:
        identifiers = self.db.execute("SELECT id FROM subscriptions ORDER BY id").fetchall()
        return [self.get(row[0]) for row in identifiers]

    def delete(self, identifier: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM subscriptions WHERE id=?", (identifier,))

    def allow_test(self, identifier: str, now: float) -> bool:
        with self.db:
            result = self.db.execute(
                "UPDATE subscriptions SET last_test_at=? WHERE id=? AND last_test_at<=?",
                (now, identifier, now - 60),
            )
        return result.rowcount == 1

    def remember(self, identifier: str, now: float) -> bool:
        """Keep event identities even if no subscriber currently wants them."""
        with self.db:
            return self._remember(identifier, now)

    def _remember(self, identifier: str, now: float) -> bool:
        result = self.db.execute("INSERT OR IGNORE INTO events VALUES (?,?)", (identifier, now))
        self.db.execute(
            "DELETE FROM events WHERE id IN (SELECT id FROM events "
            "ORDER BY observed_at DESC, id DESC LIMIT -1 OFFSET ?)",
            (MAX_EVENTS,),
        )
        return result.rowcount == 1

    def has_delivery_capacity(self, now: float) -> bool:
        with self.db:
            self.db.execute(EXPIRE_DELIVERIES_SQL, (now,))
        return self.db.execute(DELIVERY_COUNT_SQL).fetchone()[0] < MAX_DELIVERIES

    def enqueue(
        self, event_id: str, payload: dict, targets: list[str], now: float, epoch: str = "test"
    ) -> bool:
        """Reserve once and persist a bounded outbox in the same transaction."""
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        if len(encoded.encode()) > 3072:
            raise ValueError("Push payload exceeds limit")
        with self.db:
            self.db.execute(EXPIRE_DELIVERIES_SQL, (now,))
            if not self._remember(event_id, now):
                return False
            available = MAX_DELIVERIES - self.db.execute(DELIVERY_COUNT_SQL).fetchone()[0]
            for identifier in targets[: max(0, available)]:
                self.db.execute(
                    "INSERT OR IGNORE INTO deliveries VALUES (?,?,?,?,?,0,?)",
                    (
                        event_id,
                        identifier,
                        encoded,
                        min(now + 300, payload.get("sourceTimestampMs", now * 1000) / 1000 + 300),
                        now,
                        epoch,
                    ),
                )
        return True

    def retire_epochs(self, epoch: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM deliveries WHERE epoch NOT IN (?, 'test')", (epoch,))

    def next_delivery(self, now: float, *, claim: bool = False) -> dict[str, Any] | None:
        with self.db:
            self.db.execute(EXPIRE_DELIVERIES_SQL, (now,))
        row = self.db.execute(
            "SELECT * FROM deliveries WHERE next_at<=? ORDER BY next_at LIMIT 1", (now,)
        ).fetchone()
        if row is None:
            return None
        if claim:
            with self.db:
                self.db.execute(
                    "UPDATE deliveries SET next_at=? WHERE event_id=? AND subscription_id=?",
                    (now + 30, row["event_id"], row["subscription_id"]),
                )
        return {**dict(row), "payload": json.loads(row["payload"])}

    def owns_delivery(self, delivery: dict, now: float) -> bool:
        """Registration updates delete its outbox; stale completions lose ownership."""
        row = self.db.execute(
            "SELECT epoch,attempts,expires_at FROM deliveries WHERE event_id=? AND subscription_id=?",
            (delivery["event_id"], delivery["subscription_id"]),
        ).fetchone()
        return (
            row is not None
            and row["epoch"] == delivery["epoch"]
            and row["attempts"] == delivery["attempts"]
            and row["expires_at"] > now
        )

    def finish(self, delivery: dict, retry_at: float | None = None) -> None:
        args = (delivery["event_id"], delivery["subscription_id"])
        with self.db:
            if retry_at is None or delivery["attempts"] >= 2:
                self.db.execute(
                    "DELETE FROM deliveries WHERE event_id=? AND subscription_id=?", args
                )
            else:
                self.db.execute(
                    "UPDATE deliveries SET attempts=attempts+1,next_at=? "
                    "WHERE event_id=? AND subscription_id=?",
                    (retry_at, *args),
                )
