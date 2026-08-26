from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from platformdirs import user_data_path

from .models import (
    AcquisitionCandidateV1,
    AcquisitionJobV1,
    CatalogTrackV2,
    DiscoveryEdgeV1,
    DiscoverySessionV1,
    SetPlanV1,
    SmartCrateV1,
    TrackAnalysisV1,
    acquisition_candidate_from_dict,
    acquisition_job_from_dict,
    catalog_track_from_dict,
    discovery_edge_from_dict,
    discovery_session_from_dict,
    plan_from_dict,
    smart_crate_from_dict,
    to_dict,
    track_from_dict,
)

DATABASE_VERSION = 2


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
            CREATE TABLE IF NOT EXISTS catalog_tracks (
                id TEXT PRIMARY KEY, normalized_identity TEXT NOT NULL, payload TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS catalog_identity_idx ON catalog_tracks(normalized_identity);
            CREATE TABLE IF NOT EXISTS discovery_edges (
                id TEXT PRIMARY KEY, source_track_id TEXT NOT NULL, target_track_id TEXT NOT NULL,
                payload TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS discovery_sessions (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS acquisition_candidates (
                id TEXT PRIMARY KEY, catalog_track_id TEXT NOT NULL, review_state TEXT NOT NULL,
                payload TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS acquisition_jobs (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS smart_crates (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, payload TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS preferences (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        row = self.connection.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
        if row is None:
            self.connection.execute("INSERT INTO schema_meta(version) VALUES (?)", (DATABASE_VERSION,))
        elif int(row["version"]) > DATABASE_VERSION:
            raise RuntimeError("This CratePilot database was created by a newer version.")
        elif int(row["version"]) < DATABASE_VERSION:
            self.connection.execute("UPDATE schema_meta SET version=?", (DATABASE_VERSION,))
        self.connection.commit()

    def _save_payload(self, table: str, columns: tuple[str, ...], values: tuple[object, ...], payload: object) -> None:
        allowed = {
            "catalog_tracks", "discovery_edges", "discovery_sessions", "acquisition_candidates",
            "acquisition_jobs", "smart_crates",
        }
        if table not in allowed:
            raise ValueError("Unsupported storage table")
        all_columns = ("id",) + columns + ("payload",)
        placeholders = ", ".join("?" for _ in all_columns)
        assignments = ", ".join(f"{column}=excluded.{column}" for column in columns + ("payload",))
        self.connection.execute(
            f"INSERT INTO {table}({', '.join(all_columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {assignments}, updated_at=CURRENT_TIMESTAMP",
            values + (json.dumps(to_dict(payload), ensure_ascii=False),),
        )
        self.connection.commit()

    def _payloads(self, table: str, parser) -> list:
        allowed = {
            "catalog_tracks", "discovery_edges", "discovery_sessions", "acquisition_candidates",
            "acquisition_jobs", "smart_crates",
        }
        if table not in allowed:
            raise ValueError("Unsupported storage table")
        rows = self.connection.execute(f"SELECT payload FROM {table} ORDER BY id").fetchall()
        return [parser(json.loads(row["payload"])) for row in rows]

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

    def save_catalog_track(self, track: CatalogTrackV2) -> None:
        self._save_payload("catalog_tracks", ("normalized_identity",), (track.id, track.normalized_identity), track)

    def catalog_tracks(self) -> list[CatalogTrackV2]:
        return self._payloads("catalog_tracks", catalog_track_from_dict)

    def catalog_track(self, track_id: str) -> CatalogTrackV2 | None:
        row = self.connection.execute("SELECT payload FROM catalog_tracks WHERE id=?", (track_id,)).fetchone()
        return catalog_track_from_dict(json.loads(row["payload"])) if row else None

    def catalog_track_by_identity(self, identity: str) -> CatalogTrackV2 | None:
        row = self.connection.execute("SELECT payload FROM catalog_tracks WHERE normalized_identity=?", (identity,)).fetchone()
        return catalog_track_from_dict(json.loads(row["payload"])) if row else None

    def delete_catalog_track(self, track_id: str) -> None:
        self.connection.execute("DELETE FROM catalog_tracks WHERE id=?", (track_id,))
        self.connection.commit()

    def save_discovery_edge(self, edge: DiscoveryEdgeV1) -> None:
        self._save_payload(
            "discovery_edges", ("source_track_id", "target_track_id"),
            (edge.id, edge.source_track_id, edge.target_track_id), edge,
        )

    def discovery_edges(self) -> list[DiscoveryEdgeV1]:
        return self._payloads("discovery_edges", discovery_edge_from_dict)

    def save_discovery_session(self, session: DiscoverySessionV1) -> None:
        self._save_payload("discovery_sessions", ("status",), (session.id, session.status), session)

    def discovery_session(self, session_id: str) -> DiscoverySessionV1 | None:
        row = self.connection.execute("SELECT payload FROM discovery_sessions WHERE id=?", (session_id,)).fetchone()
        return discovery_session_from_dict(json.loads(row["payload"])) if row else None

    def discovery_sessions(self) -> list[DiscoverySessionV1]:
        return self._payloads("discovery_sessions", discovery_session_from_dict)

    def save_candidate(self, candidate: AcquisitionCandidateV1) -> None:
        self._save_payload(
            "acquisition_candidates", ("catalog_track_id", "review_state"),
            (candidate.id, candidate.catalog_track_id, candidate.review_state), candidate,
        )

    def candidates(self, *, state: str | None = None) -> list[AcquisitionCandidateV1]:
        values = self._payloads("acquisition_candidates", acquisition_candidate_from_dict)
        return [item for item in values if state is None or item.review_state == state]

    def candidate(self, candidate_id: str) -> AcquisitionCandidateV1 | None:
        row = self.connection.execute("SELECT payload FROM acquisition_candidates WHERE id=?", (candidate_id,)).fetchone()
        return acquisition_candidate_from_dict(json.loads(row["payload"])) if row else None

    def save_acquisition_job(self, job: AcquisitionJobV1) -> None:
        self._save_payload("acquisition_jobs", ("status",), (job.id, job.status), job)

    def acquisition_jobs(self) -> list[AcquisitionJobV1]:
        return self._payloads("acquisition_jobs", acquisition_job_from_dict)

    def save_crate(self, crate: SmartCrateV1) -> None:
        self._save_payload("smart_crates", ("name",), (crate.id, crate.name), crate)

    def crates(self) -> list[SmartCrateV1]:
        return self._payloads("smart_crates", smart_crate_from_dict)

    def crate(self, crate_id: str) -> SmartCrateV1 | None:
        row = self.connection.execute("SELECT payload FROM smart_crates WHERE id=?", (crate_id,)).fetchone()
        return smart_crate_from_dict(json.loads(row["payload"])) if row else None

    def set_preference(self, key: str, value: object) -> None:
        self.connection.execute(
            "INSERT INTO preferences(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        self.connection.commit()

    def preference(self, key: str, default=None):
        row = self.connection.execute("SELECT value FROM preferences WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default
