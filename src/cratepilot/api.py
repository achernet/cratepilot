from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from platformdirs import user_cache_path

from .analysis import analyze_paths, scan_library
from .exporter import write_rekordbox_package
from .jobs import JobRunner
from .models import to_dict
from .planner import generate_drafts, replan_sequence
from .storage import Store


class LocalState:
    def __init__(self, library_root: Path, store: Store | None = None) -> None:
        self.library_root = library_root.expanduser().resolve()
        self.token = secrets.token_urlsafe(24)
        self.store = store or Store()
        self.jobs = JobRunner(self.store)
        self.cache_directory = user_cache_path("CratePilot", "Chernetz") / "analysis"

    def validate_path(self, value: str) -> Path:
        path = Path(value).expanduser().resolve()
        try:
            path.relative_to(self.library_root)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="Path is outside the selected music library.") from exc
        return path


def is_local_host(value: str) -> bool:
    return value.partition(":")[0].casefold() in {"127.0.0.1", "localhost", "testserver"}


def is_allowed_origin(value: str | None) -> bool:
    return value is None or value in {"http://127.0.0.1:8765", "http://localhost:8765"}


def validate_paths_payload(value: Any) -> list[str] | None:
    if value is not None and (not isinstance(value, list) or not all(isinstance(item, str) for item in value)):
        raise HTTPException(status_code=422, detail="paths must be a list of files inside the selected library.")
    return value


def create_app(library_root: Path, *, store: Store | None = None, web_directory: Path | None = None) -> FastAPI:
    state = LocalState(library_root, store=store)
    app = FastAPI(title="CratePilot Local API", version="1.0.0", docs_url=None, redoc_url=None)
    app.state.cratepilot = state

    @app.middleware("http")
    async def protect_local_api(request: Request, call_next):
        if not is_local_host(request.headers.get("host", "")):
            return JSONResponse({"detail": "CratePilot accepts localhost requests only."}, status_code=403)
        if request.url.path.startswith("/api/v1"):
            token = request.headers.get("x-cratepilot-token") or request.query_params.get("token")
            if not secrets.compare_digest(token or "", state.token):
                return JSONResponse({"detail": "Invalid local session token."}, status_code=403)
            origin = request.headers.get("origin")
            if not is_allowed_origin(origin):
                return JSONResponse({"detail": "Cross-origin API requests are not allowed."}, status_code=403)
        return await call_next(request)

    @app.get("/api/v1/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "mode": "local", "library": str(state.library_root)}

    @app.get("/api/v1/library")
    def library() -> dict[str, Any]:
        return {"tracks": [to_dict(track) for track in state.store.tracks()]}

    @app.post("/api/v1/library/analyze")
    async def analyze(request: Request) -> dict[str, str]:
        payload = await request.json()
        supplied = validate_paths_payload(payload.get("paths"))

        def operation() -> dict[str, Any]:
            paths = [state.validate_path(item) for item in supplied] if supplied else scan_library(state.library_root)
            tracks = analyze_paths(paths, cache_directory=state.cache_directory)
            state.store.save_tracks(tracks)
            return {"tracks": [to_dict(track) for track in tracks]}

        return {"job_id": state.jobs.submit("analysis", operation)}

    @app.post("/api/v1/plans/generate")
    async def generate(request: Request) -> dict[str, Any]:
        payload = await request.json()
        target_minutes = float(payload.get("target_minutes", 45.0))
        drafts = generate_drafts(
            state.store.tracks(),
            target_duration_seconds=target_minutes * 60.0,
            locked_positions={int(key): value for key, value in payload.get("locked_positions", {}).items()},
        )
        for draft in drafts:
            state.store.save_plan(draft)
        return {"plans": [to_dict(item) for item in drafts]}

    @app.put("/api/v1/plans/{plan_id}/order")
    async def reorder(plan_id: str, request: Request) -> dict[str, Any]:
        payload = await request.json()
        library = {track.id: track for track in state.store.tracks()}
        try:
            sequence = [library[item] for item in payload["track_ids"]]
        except KeyError as exc:
            raise HTTPException(status_code=400, detail="Order contains an unknown track.") from exc
        plan = replan_sequence(sequence, title=payload.get("title", "Edited set"))
        state.store.save_plan(plan)
        return {"plan": to_dict(plan), "replaces": plan_id}

    @app.post("/api/v1/plans/{plan_id}/export")
    async def export(plan_id: str, request: Request) -> dict[str, str]:
        plan = state.store.plan(plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="Plan not found.")
        payload = await request.json()
        output = Path(payload["output_directory"]).expanduser().resolve()
        render_reference = bool(payload.get("render_reference", True))

        def operation() -> dict[str, Any]:
            library = {track.id: track for track in state.store.tracks()}
            manifest = write_rekordbox_package(
                plan, library, output, render_reference=render_reference, analysis_cache=state.cache_directory
            )
            return {"manifest": to_dict(manifest)}

        return {"job_id": state.jobs.submit("export", operation)}

    @app.get("/api/v1/jobs/{job_id}")
    def job(job_id: str) -> dict[str, Any]:
        value = state.store.job(job_id)
        if value is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        return value

    @app.get("/api/v1/jobs/{job_id}/events")
    async def job_events(job_id: str):
        async def events():
            last = None
            while True:
                value = state.store.job(job_id)
                if value is None:
                    yield "event: error\ndata: {\"detail\":\"Job not found\"}\n\n"
                    return
                encoded = json.dumps(value)
                if encoded != last:
                    yield f"data: {encoded}\n\n"
                    last = encoded
                if value["status"] in {"complete", "failed"}:
                    return
                await asyncio.sleep(0.35)

        return StreamingResponse(events(), media_type="text/event-stream")

    if web_directory and web_directory.is_dir():
        static_directory = web_directory / "assets"
        if static_directory.is_dir():
            app.mount("/assets", StaticFiles(directory=static_directory), name="assets")

        @app.get("/{requested_path:path}", response_class=FileResponse)
        def web(requested_path: str):
            candidate = (web_directory / requested_path).resolve()
            if candidate.is_file() and web_directory.resolve() in candidate.parents:
                return candidate
            return web_directory / "index.html"

    return app
