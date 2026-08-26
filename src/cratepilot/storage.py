from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from platformdirs import user_data_path

from .models import SetPlanV1, TrackAnalysisV1, plan_from_dict, to_dict, track_from_dict

DATABASE_VERSION = 1


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (user_data_path("CratePilot", "Chernetz") / "cratepilot.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.migrate()

    def migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS tracks (
                id TEXT PRIMARY KEY,
                path TEXT,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS plans (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                progress REAL NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                result TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        row = self.connection.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
        if row is None:
            self.connection.execute("INSERT INTO schema_meta(version) VALUES (?)", (DATABASE_VERSION,))
        elif int(row["version"]) > DATABASE_VERSION:
            raise RuntimeError("This CratePilot database was created by a newer version.")
        self.connection.commit()

    def save_tracks(self, tracks: Iterable[TrackAnalysisV1]) -> None:
        self.connection.executemany(
            "INSERT INTO tracks(id, path, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET path=excluded.path, payload=excluded.payload, updated_at=CURRENT_TIMESTAMP",
            ((track.id, track.path, json.dumps(to_dict(track), ensure_ascii=False)) for track in tracks),
        )
        self.connection.commit()

    def tracks(self) -> list[TrackAnalysisV1]:
        rows = self.connection.execute("SELECT payload FROM tracks ORDER BY id").fetchall()
        return [track_from_dict(json.loads(row["payload"])) for row in rows]

    def save_plan(self, plan: SetPlanV1) -> None:
        self.connection.execute(
            "INSERT INTO plans(id, payload) VALUES (?, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=CURRENT_TIMESTAMP",
            (plan.id, json.dumps(to_dict(plan), ensure_ascii=False)),
        )
        self.connection.commit()

    def plan(self, plan_id: str) -> SetPlanV1 | None:
        row = self.connection.execute("SELECT payload FROM plans WHERE id=?", (plan_id,)).fetchone()
        return plan_from_dict(json.loads(row["payload"])) if row else None

    def update_job(self, job_id: str, kind: str, status: str, progress: float, message: str, result: dict | None = None) -> None:
        self.connection.execute(
            "INSERT INTO jobs(id, kind, status, progress, message, result) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, progress=excluded.progress, "
            "message=excluded.message, result=excluded.result, updated_at=CURRENT_TIMESTAMP",
            (job_id, kind, status, progress, message, json.dumps(result) if result is not None else None),
        )
        self.connection.commit()

    def job(self, job_id: str) -> dict | None:
        row = self.connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        value = dict(row)
        value["result"] = json.loads(value["result"]) if value["result"] else None
        return value

