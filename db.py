from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS plans (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL UNIQUE COLLATE NOCASE,
    price_label       TEXT NOT NULL DEFAULT '',
    duration_days     INTEGER NOT NULL CHECK (duration_days > 0),
    max_slots         INTEGER NOT NULL CHECK (max_slots >= 0),
    description       TEXT NOT NULL DEFAULT '',
    benefits          TEXT NOT NULL DEFAULT '',
    category_id       INTEGER,
    is_free           INTEGER NOT NULL DEFAULT 0,
    permissions       TEXT NOT NULL DEFAULT '{}',
    here_per_day      INTEGER NOT NULL DEFAULT 2,
    everyone_per_week INTEGER NOT NULL DEFAULT 1,
    active            INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS slots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id       INTEGER NOT NULL,
    plan_id        INTEGER NOT NULL REFERENCES plans(id),
    channel_id     INTEGER UNIQUE,
    status         TEXT NOT NULL CHECK (status IN ('pending','active','expired','banned','removed','deleted')),
    created_at     INTEGER NOT NULL,
    expires_at     INTEGER,
    key_hash       TEXT UNIQUE,
    owner_left_at  INTEGER,
    payment_status TEXT NOT NULL DEFAULT 'none',
    verified       INTEGER NOT NULL DEFAULT 0,
    note           TEXT
);
-- One open slot per user. Enforced by the database, not just by code.
CREATE UNIQUE INDEX IF NOT EXISTS one_open_slot_per_user
    ON slots(owner_id) WHERE status IN ('pending','active','banned');
CREATE INDEX IF NOT EXISTS slots_status ON slots(status);
CREATE TABLE IF NOT EXISTS slot_notices (
    slot_id INTEGER NOT NULL,
    kind    TEXT NOT NULL,
    PRIMARY KEY (slot_id, kind)
);
CREATE TABLE IF NOT EXISTS ping_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_id INTEGER NOT NULL,
    kind    TEXT NOT NULL,
    used_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ping_lookup ON ping_log(slot_id, kind, used_at);
CREATE TABLE IF NOT EXISTS violations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    slot_id    INTEGER,
    kind       TEXT NOT NULL,
    reason     TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS blacklist (
    user_id    INTEGER PRIMARY KEY,
    reason     TEXT NOT NULL,
    actor_id   INTEGER,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_id    INTEGER,
    user_id    INTEGER,
    actor_id   INTEGER,
    action     TEXT NOT NULL,
    details    TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "requirement_enabled": "0",
    "requirement_phrase": "",
    "gl_text": "",
    "expiry_action": "archive",
    "blacklist_threshold": "3",
    "rules_text": (
        "1. Slot channels are view-only for members.\n"
        "2. Only the slot owner publishes, using `/post-promo`.\n"
        "3. Mention allowances are set per plan. Check `/slot-info`.\n"
        "4. Going over your allowance removes your slot.\n"
        "5. Repeated removals lead to an automatic blacklist.\n"
        "6. Keep your recovery key private."
    ),
}


class _Tx:
    """Query helper bound to an open transaction. Never takes the write lock itself."""

    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    async def one(self, sql: str, params=()):
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def all(self, sql: str, params=()):
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchall()

    async def exec(self, sql: str, params=()) -> tuple[int, int]:
        async with self._conn.execute(sql, params) as cur:
            return cur.lastrowid, cur.rowcount


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        for key, value in DEFAULT_SETTINGS.items():
            await self._conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
            )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def one(self, sql: str, params=()):
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def all(self, sql: str, params=()):
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchall()

    async def write(self, sql: str, params=()) -> tuple[int, int]:
        async with self._write_lock:
            async with self._conn.execute(sql, params) as cur:
                return cur.lastrowid, cur.rowcount

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[_Tx]:
        """Atomic block. Commits on success, rolls back on any exception."""
        async with self._write_lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield _Tx(self._conn)
            except BaseException:
                await self._conn.execute("ROLLBACK")
                raise
            await self._conn.execute("COMMIT")

    async def setting(self, key: str, default=None):
        row = await self.one("SELECT value FROM settings WHERE key = ?", (key,))
        if row is None or row["value"] is None:
            return default
        return row["value"]

    async def set_setting(self, key: str, value) -> None:
        await self.write(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, None if value is None else str(value)),
        )
