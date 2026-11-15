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
    detail TEXT,
    channel TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS releases (
    channel_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel_id, version)
);
CREATE TABLE IF NOT EXISTS builds (
    channel_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    system TEXT NOT NULL,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel_id, version, system)
);
"""


class Store:
    def __init__(self, path: str | Path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        if "channel" not in {row[1] for row in self._conn.execute("PRAGMA table_info(events)")}:
            self._conn.execute("ALTER TABLE events ADD COLUMN channel TEXT")   # databases from before channels
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

    def adopt_untagged_events(self, channel_id: str) -> int:
        """Give events recorded before channels existed a channel; returns how many."""
        with self._lock:
            cur = self._conn.execute("UPDATE events SET channel = ? WHERE channel IS NULL", (channel_id,))
            self._conn.commit()
        return cur.rowcount

    def save_channel(self, channel_id: str, data: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO channels(channel_id, data, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(channel_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                (channel_id, json.dumps(data, default=str), utcnow().isoformat()),
            )
            self._conn.commit()

    def load_channels(self) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute("SELECT channel_id, data FROM channels").fetchall()
        return {channel_id: json.loads(data) for channel_id, data in rows}

    def delete_channel(self, channel_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
            self._conn.execute("DELETE FROM releases WHERE channel_id = ?", (channel_id,))
            self._conn.execute("DELETE FROM builds WHERE channel_id = ?", (channel_id,))
            self._conn.commit()
        return cur.rowcount > 0

    # Releases (code + env + machines as one version) and their per-machine builds.
    def save_release(self, channel_id: str, version: int, data: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO releases(channel_id, version, data, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(channel_id, version) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                (channel_id, version, json.dumps(data, default=str), utcnow().isoformat()),
            )
            self._conn.commit()

    def load_releases(self, channel_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT data FROM releases WHERE channel_id = ? ORDER BY version", (channel_id,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def save_build(self, channel_id: str, version: int, system: str, data: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO builds(channel_id, version, system, data, updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(channel_id, version, system) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                (channel_id, version, system, json.dumps(data, default=str), utcnow().isoformat()),
            )
            self._conn.commit()

    def load_builds(self, channel_id: str, version: int) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute("SELECT system, data FROM builds WHERE channel_id = ? AND version = ?",
                                      (channel_id, version)).fetchall()
        return {system: json.loads(data) for system, data in rows}

    def add_event(self, kind: str, message: str, task_id: Optional[str] = None,
                  detail: Optional[dict] = None, channel: Optional[str] = None) -> dict:
        ts = utcnow().isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(ts, task_id, kind, message, detail, channel) VALUES(?,?,?,?,?,?)",
                (ts, task_id, kind, message, json.dumps(detail or {}, default=str), channel),
            )
            self._conn.commit()
        return {"ts": ts, "task_id": task_id, "kind": kind, "message": message, "detail": detail or {},
                "channel": channel}

    def events(self, limit: int = 100, task_id: Optional[str] = None, channel: Optional[str] = None) -> list[dict]:
        where, params = [], []
        if task_id:
            where.append("task_id = ?"); params.append(task_id)
        if channel:
            where.append("channel = ?"); params.append(channel)
        query = "SELECT ts, task_id, kind, message, detail, channel FROM events"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(query, (*params, limit)).fetchall()
        return [{"ts": ts, "task_id": tid, "kind": kind, "message": message,
                 "detail": json.loads(detail) if detail else {}, "channel": ch}
                for ts, tid, kind, message, detail, ch in rows]
