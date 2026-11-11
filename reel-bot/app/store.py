"""Учёт обработанных сообщений. Meta повторяет доставку, поэтому нужна защита от дублей."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS items ("
            " key TEXT PRIMARY KEY, created REAL, status TEXT, details TEXT)"
        )
        self._db.commit()

    def claim(self, key: str) -> bool:
        """True, если сообщение новое и его нужно обработать."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO items(key, created, status, details) VALUES (?,?,?,?)",
                (key, time.time(), "received", "{}"),
            )
            self._db.commit()
            return cur.rowcount == 1

    def finish(self, key: str, status: str, details: dict) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE items SET status=?, details=? WHERE key=?",
                (status, json.dumps(details, ensure_ascii=False), key),
            )
            self._db.commit()

    def get(self, key: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT key, created, status, details FROM items WHERE key=?", (key,)
            ).fetchone()
        if not row:
            return None
        return {"key": row[0], "created": row[1], "status": row[2], "details": json.loads(row[3])}

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT key, created, status, details FROM items ORDER BY created DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"key": r[0], "created": r[1], "status": r[2], "details": json.loads(r[3])} for r in rows
        ]
