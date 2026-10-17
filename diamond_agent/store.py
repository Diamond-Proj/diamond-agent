"""SQLite persistence for tasks and the agent event feed.

Task records are stored as one JSON document per task (they carry their full
decision trace and timeline); the events table is the flat, queryable agent
activity feed shown on the dashboard.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from .schemas import utcnow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    task_id TEXT,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS groups (
    group_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def save_task(self, task_id: str, data: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks(task_id, data, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(task_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                (task_id, json.dumps(data, default=str), utcnow().isoformat()),
            )
            self._conn.commit()

    def load_tasks(self) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute("SELECT task_id, data FROM tasks").fetchall()
        return {task_id: json.loads(data) for task_id, data in rows}

    def save_group(self, group_id: str, data: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO groups(group_id, data, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(group_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                (group_id, json.dumps(data, default=str), utcnow().isoformat()),
            )
            self._conn.commit()

    def load_groups(self) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute("SELECT group_id, data FROM groups").fetchall()
        return {group_id: json.loads(data) for group_id, data in rows}

    # Operator settings (e.g. policy overrides edited from the web UI) live
    # here rather than in the catalog so the YAML the human maintains is never
    # rewritten by the service.
    def save_setting(self, key: str, data: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key, data, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                (key, json.dumps(data, default=str), utcnow().isoformat()),
            )
            self._conn.commit()

    def load_setting(self, key: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT data FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def add_event(self, kind: str, message: str, task_id: Optional[str] = None,
                  detail: Optional[dict] = None) -> dict:
        ts = utcnow().isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(ts, task_id, kind, message, detail) VALUES(?,?,?,?,?)",
                (ts, task_id, kind, message, json.dumps(detail or {}, default=str)),
            )
            self._conn.commit()
        return {"ts": ts, "task_id": task_id, "kind": kind, "message": message, "detail": detail or {}}

    def events(self, limit: int = 100, task_id: Optional[str] = None) -> list[dict]:
        query = "SELECT ts, task_id, kind, message, detail FROM events"
        params: tuple = ()
        if task_id:
            query += " WHERE task_id = ?"
            params = (task_id,)
        query += " ORDER BY id DESC LIMIT ?"
        params += (limit,)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {"ts": ts, "task_id": tid, "kind": kind, "message": message,
             "detail": json.loads(detail) if detail else {}}
            for ts, tid, kind, message, detail in rows
        ]
