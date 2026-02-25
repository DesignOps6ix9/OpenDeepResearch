from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventStore:
    """SQLite-backed event store for runs, events, and steer commands."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            cursor = self._conn.cursor()
            cursor.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    step INTEGER NOT NULL,
                    node TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS steer_commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    scope TEXT NOT NULL,
                    content TEXT NOT NULL,
                    consumed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    consumed_at TEXT,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                """
            )
            self._conn.commit()

    def create_run(self, run_id: str, goal: str, config: dict[str, Any], state: dict[str, Any]) -> None:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO runs (run_id, status, goal, config_json, state_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, "queued", goal, json.dumps(config), json.dumps(state), now, now),
            )
            self._conn.commit()

    def update_run(self, run_id: str, status: str, state: dict[str, Any] | None = None) -> None:
        now = utc_now_iso()
        with self._lock:
            if state is None:
                self._conn.execute(
                    "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
                    (status, now, run_id),
                )
            else:
                self._conn.execute(
                    "UPDATE runs SET status = ?, state_json = ?, updated_at = ? WHERE run_id = ?",
                    (status, json.dumps(state), now, run_id),
                )
            self._conn.commit()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT run_id, status, goal, config_json, state_json, created_at, updated_at FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "status": row["status"],
            "goal": row["goal"],
            "config": json.loads(row["config_json"]),
            "state": json.loads(row["state_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_runs(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT run_id, status, goal, state_json, created_at, updated_at
                FROM runs
                ORDER BY datetime(updated_at) DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()

        runs: list[dict[str, Any]] = []
        for row in rows:
            state = json.loads(row["state_json"])
            final_report = state.get("final_report") if isinstance(state, dict) else None
            final_summary = ""
            if isinstance(final_report, dict):
                final_summary = str(final_report.get("summary", "")).strip()

            runs.append(
                {
                    "run_id": row["run_id"],
                    "status": row["status"],
                    "goal": row["goal"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "has_final_report": bool(final_summary),
                    "final_summary": final_summary,
                }
            )
        return runs

    def insert_event(
        self,
        run_id: str,
        step: int,
        node: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now_iso()
        payload_json = json.dumps(payload)
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO events (run_id, step, node, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, step, node, event_type, payload_json, now),
            )
            self._conn.commit()
            event_id = cursor.lastrowid

        return {
            "id": event_id,
            "run_id": run_id,
            "step": step,
            "node": node,
            "event_type": event_type,
            "payload": payload,
            "created_at": now,
        }

    def list_events(self, run_id: str, after_id: int = 0, limit: int = 300) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, run_id, step, node, event_type, payload_json, created_at
                FROM events
                WHERE run_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (run_id, after_id, limit),
            ).fetchall()

        events: list[dict[str, Any]] = []
        for row in rows:
            events.append(
                {
                    "id": row["id"],
                    "run_id": row["run_id"],
                    "step": row["step"],
                    "node": row["node"],
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload_json"]),
                    "created_at": row["created_at"],
                }
            )
        return events

    def insert_steer(self, run_id: str, scope: str, content: str) -> dict[str, Any]:
        now = utc_now_iso()
        with self._lock:
            version_row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM steer_commands WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            version = int(version_row["next_version"])
            cursor = self._conn.execute(
                """
                INSERT INTO steer_commands (run_id, version, scope, content, consumed, created_at)
                VALUES (?, ?, ?, ?, 0, ?)
                """,
                (run_id, version, scope, content, now),
            )
            self._conn.commit()
            steer_id = cursor.lastrowid

        return {
            "id": steer_id,
            "run_id": run_id,
            "version": version,
            "scope": scope,
            "content": content,
            "consumed": False,
            "created_at": now,
        }

    def list_unconsumed_steer(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, run_id, version, scope, content, consumed, created_at, consumed_at
                FROM steer_commands
                WHERE run_id = ? AND consumed = 0
                ORDER BY id ASC
                """,
                (run_id,),
            ).fetchall()

        items: list[dict[str, Any]] = []
        for row in rows:
            items.append(
                {
                    "id": row["id"],
                    "run_id": row["run_id"],
                    "version": row["version"],
                    "scope": row["scope"],
                    "content": row["content"],
                    "consumed": bool(row["consumed"]),
                    "created_at": row["created_at"],
                    "consumed_at": row["consumed_at"],
                }
            )
        return items

    def mark_steer_consumed(self, steer_ids: list[int]) -> None:
        if not steer_ids:
            return
        now = utc_now_iso()
        placeholders = ",".join("?" for _ in steer_ids)
        query = f"UPDATE steer_commands SET consumed = 1, consumed_at = ? WHERE id IN ({placeholders})"
        with self._lock:
            self._conn.execute(query, (now, *steer_ids))
            self._conn.commit()
