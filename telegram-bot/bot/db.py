"""Хранилище SQLite: известные чаты, связки копирования и расписания публикаций."""

import json
import time
from dataclasses import dataclass

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id  INTEGER PRIMARY KEY,
    title    TEXT    NOT NULL,
    type     TEXT    NOT NULL,
    added_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS routes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_chat_id INTEGER NOT NULL,
    target_chat_id INTEGER NOT NULL,
    enabled        INTEGER NOT NULL DEFAULT 1,
    UNIQUE (source_chat_id, target_chat_id)
);

CREATE TABLE IF NOT EXISTS posts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id         INTEGER NOT NULL,
    from_chat_id     INTEGER NOT NULL,
    message_ids      TEXT    NOT NULL,
    target_chat_id   INTEGER NOT NULL,
    start_at         INTEGER NOT NULL,
    next_run_at      INTEGER NOT NULL,
    interval_seconds INTEGER NOT NULL,
    total_runs       INTEGER NOT NULL,
    runs_done        INTEGER NOT NULL DEFAULT 0,
    status           TEXT    NOT NULL DEFAULT 'active',
    last_error       TEXT,
    created_at       INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS posts_due ON posts (status, next_run_at);
"""

ACTIVE = "active"
DONE = "done"
CANCELLED = "cancelled"


@dataclass(frozen=True)
class Chat:
    chat_id: int
    title: str
    type: str


@dataclass(frozen=True)
class Route:
    id: int
    source_chat_id: int
    target_chat_id: int
    enabled: bool
    source_title: str | None
    target_title: str | None


@dataclass(frozen=True)
class Post:
    id: int
    owner_id: int
    from_chat_id: int
    message_ids: list[int]
    target_chat_id: int
    start_at: int
    next_run_at: int
    interval_seconds: int
    total_runs: int
    runs_done: int
    status: str
    last_error: str | None
    target_title: str | None


class Database:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    @classmethod
    async def open(cls, path: str) -> "Database":
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    # --- чаты ---

    async def upsert_chat(self, chat_id: int, title: str, type_: str) -> None:
        await self._conn.execute(
            "INSERT INTO chats (chat_id, title, type, added_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (chat_id) DO UPDATE SET title = excluded.title, type = excluded.type",
            (chat_id, title, type_, int(time.time())),
        )
        await self._conn.commit()

    async def remove_chat(self, chat_id: int) -> None:
        await self._conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
        await self._conn.commit()

    async def list_chats(self) -> list[Chat]:
        async with self._conn.execute("SELECT chat_id, title, type FROM chats ORDER BY title") as cur:
            return [Chat(r["chat_id"], r["title"], r["type"]) async for r in cur]

    async def get_chat(self, chat_id: int) -> Chat | None:
        async with self._conn.execute(
            "SELECT chat_id, title, type FROM chats WHERE chat_id = ?", (chat_id,)
        ) as cur:
            r = await cur.fetchone()
        return Chat(r["chat_id"], r["title"], r["type"]) if r else None

    async def migrate_chat(self, old_id: int, new_id: int) -> None:
        """Группа стала супергруппой — Telegram сменил её chat_id."""
        await self._conn.execute("UPDATE OR IGNORE chats SET chat_id = ? WHERE chat_id = ?", (new_id, old_id))
        await self._conn.execute("DELETE FROM chats WHERE chat_id = ?", (old_id,))
        await self._conn.execute(
            "UPDATE OR IGNORE routes SET source_chat_id = ? WHERE source_chat_id = ?", (new_id, old_id)
        )
        await self._conn.execute(
            "UPDATE OR IGNORE routes SET target_chat_id = ? WHERE target_chat_id = ?", (new_id, old_id)
        )
        # то, что не удалось переписать из-за UNIQUE, — дубликаты уже существующих связок
        await self._conn.execute(
            "DELETE FROM routes WHERE source_chat_id = ? OR target_chat_id = ?", (old_id, old_id)
        )
        await self._conn.execute("UPDATE posts SET target_chat_id = ? WHERE target_chat_id = ?", (new_id, old_id))
        await self._conn.commit()

    # --- связки копирования ---

    async def add_route(self, source_chat_id: int, target_chat_id: int) -> bool:
        cur = await self._conn.execute(
            "INSERT OR IGNORE INTO routes (source_chat_id, target_chat_id) VALUES (?, ?)",
            (source_chat_id, target_chat_id),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def list_routes(self) -> list[Route]:
        async with self._conn.execute(
            "SELECT r.id, r.source_chat_id, r.target_chat_id, r.enabled, s.title AS st, t.title AS tt "
            "FROM routes r "
            "LEFT JOIN chats s ON s.chat_id = r.source_chat_id "
            "LEFT JOIN chats t ON t.chat_id = r.target_chat_id "
            "ORDER BY r.id"
        ) as cur:
            return [
                Route(r["id"], r["source_chat_id"], r["target_chat_id"], bool(r["enabled"]), r["st"], r["tt"])
                async for r in cur
            ]

    async def targets_for(self, source_chat_id: int) -> list[int]:
        async with self._conn.execute(
            "SELECT target_chat_id FROM routes WHERE source_chat_id = ? AND enabled = 1 ORDER BY id",
            (source_chat_id,),
        ) as cur:
            return [r["target_chat_id"] async for r in cur]

    async def toggle_route(self, route_id: int) -> None:
        await self._conn.execute("UPDATE routes SET enabled = 1 - enabled WHERE id = ?", (route_id,))
        await self._conn.commit()

    async def delete_route(self, route_id: int) -> None:
        await self._conn.execute("DELETE FROM routes WHERE id = ?", (route_id,))
        await self._conn.commit()

    # --- расписания ---

    async def add_post(
        self,
        owner_id: int,
        from_chat_id: int,
        message_ids: list[int],
        target_chat_id: int,
        start_at: int,
        interval_seconds: int,
        total_runs: int,
    ) -> int:
        cur = await self._conn.execute(
            "INSERT INTO posts (owner_id, from_chat_id, message_ids, target_chat_id, start_at, next_run_at, "
            "interval_seconds, total_runs, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                owner_id,
                from_chat_id,
                json.dumps(sorted(message_ids)),
                target_chat_id,
                start_at,
                start_at,
                interval_seconds,
                total_runs,
                int(time.time()),
            ),
        )
        await self._conn.commit()
        return cur.lastrowid

    _POST_SELECT = (
        "SELECT p.*, c.title AS target_title FROM posts p LEFT JOIN chats c ON c.chat_id = p.target_chat_id "
    )

    @staticmethod
    def _post(r: aiosqlite.Row) -> Post:
        return Post(
            id=r["id"],
            owner_id=r["owner_id"],
            from_chat_id=r["from_chat_id"],
            message_ids=json.loads(r["message_ids"]),
            target_chat_id=r["target_chat_id"],
            start_at=r["start_at"],
            next_run_at=r["next_run_at"],
            interval_seconds=r["interval_seconds"],
            total_runs=r["total_runs"],
            runs_done=r["runs_done"],
            status=r["status"],
            last_error=r["last_error"],
            target_title=r["target_title"],
        )

    async def get_post(self, post_id: int) -> Post | None:
        async with self._conn.execute(self._POST_SELECT + "WHERE p.id = ?", (post_id,)) as cur:
            r = await cur.fetchone()
        return self._post(r) if r else None

    async def list_active_posts(self) -> list[Post]:
        async with self._conn.execute(
            self._POST_SELECT + "WHERE p.status = ? ORDER BY p.next_run_at", (ACTIVE,)
        ) as cur:
            return [self._post(r) async for r in cur]

    async def due_posts(self, now: int) -> list[Post]:
        async with self._conn.execute(
            self._POST_SELECT + "WHERE p.status = ? AND p.next_run_at <= ? ORDER BY p.next_run_at",
            (ACTIVE, now),
        ) as cur:
            return [self._post(r) async for r in cur]

    async def update_post_run(
        self, post_id: int, runs_done: int, next_run_at: int, status: str, last_error: str | None
    ) -> None:
        await self._conn.execute(
            "UPDATE posts SET runs_done = ?, next_run_at = ?, status = ?, last_error = ? WHERE id = ?",
            (runs_done, next_run_at, status, last_error, post_id),
        )
        await self._conn.commit()

    async def postpone_post(self, post_id: int, next_run_at: int, last_error: str) -> None:
        await self._conn.execute(
            "UPDATE posts SET next_run_at = ?, last_error = ? WHERE id = ?", (next_run_at, last_error, post_id)
        )
        await self._conn.commit()

    async def cancel_post(self, post_id: int) -> bool:
        cur = await self._conn.execute(
            "UPDATE posts SET status = ? WHERE id = ? AND status = ?", (CANCELLED, post_id, ACTIVE)
        )
        await self._conn.commit()
        return cur.rowcount > 0
