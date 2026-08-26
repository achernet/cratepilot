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

from .acquisition import AcquisitionService
from .analysis import analyze_paths, scan_library
from .crates import materialize_crate, write_m3u8
from .discovery import Catalog, DiscoveryService
from .exporter import write_rekordbox_package
from .identity import stable_id
from .jobs import JobRunner
from .models import SmartCrateV1, to_dict
from .planner import generate_drafts, replan_sequence
from .providers import ProviderTrack, ShazamRelatedProvider, SpotifyMetadataProvider, YtDlpSearchProvider
from .recognition import ShazamMusicBrainzVerifier
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
            Catalog(state.store).import_analyses(tracks)
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
            count=int(payload.get("count", 3)),
        )
        for draft in drafts:
            state.store.save_plan(draft)
        return {"plans": [to_dict(item) for item in drafts]}

    @app.get("/api/v1/catalog")
    async def catalog() -> dict[str, Any]:
        return {
            "tracks": [to_dict(item) for item in state.store.catalog_tracks()],
            "edges": [to_dict(item) for item in state.store.discovery_edges()],
        }

    @app.post("/api/v1/catalog/merge")
    async def merge_catalog(request: Request) -> dict[str, Any]:
        payload = await request.json()
        try:
            merged = Catalog(state.store).merge(str(payload["target_id"]), str(payload["source_id"]))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"track": to_dict(merged)}

    @app.post("/api/v1/discovery/sessions")
    async def create_discovery(request: Request) -> dict[str, Any]:
        payload = await request.json()
        seeds: list[ProviderTrack] = []
        for seed in payload.get("seeds", []):
            kind = seed.get("kind", "manual")
            if kind == "manual":
                seeds.append(ProviderTrack(str(seed["artist"]), str(seed["title"]), "manual"))
            elif kind == "spotify":
                try:
                    seeds.extend(SpotifyMetadataProvider().resolve(str(seed["url"])))
                except (ValueError, RuntimeError) as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
            elif kind == "local":
                path = state.validate_path(str(seed["path"]))
                tracks = analyze_paths([path], cache_directory=state.cache_directory)
                state.store.save_tracks(tracks)
                Catalog(state.store).import_analyses(tracks)
                seeds.extend(
                    ProviderTrack(item.artist, item.title, "local", external_id=item.id, evidence={"path": item.path})
                    for item in tracks
                )
            else:
                raise HTTPException(status_code=422, detail=f"Unsupported seed kind: {kind}")
        if not seeds:
            raise HTTPException(status_code=422, detail="At least one discovery seed is required.")
        service = DiscoveryService(state.store)
        session = service.create_session(
            seeds, max_depth=int(payload.get("max_depth", 2)), max_nodes=int(payload.get("max_nodes", 150)),
            review_batch_size=int(payload.get("review_batch_size", 30)),
            readiness_target=int(payload.get("readiness_target", 8)), result_count=int(payload.get("result_count", 30)),
        )
        return {"session": to_dict(session)}

    @app.post("/api/v1/discovery/sessions/{session_id}/run")
    def run_discovery(session_id: str) -> dict[str, str]:
        if state.store.discovery_session(session_id) is None:
            raise HTTPException(status_code=404, detail="Discovery session not found.")

        def operation() -> dict[str, Any]:
            session = DiscoveryService(
                state.store, similarity=ShazamRelatedProvider(), video_search=YtDlpSearchProvider()
            ).run(session_id)
            return {"session": to_dict(session)}

        return {"job_id": state.jobs.submit("discovery", operation)}

    @app.get("/api/v1/discovery/sessions/{session_id}")
    async def discovery_session(session_id: str) -> dict[str, Any]:
        value = state.store.discovery_session(session_id)
        if value is None:
            raise HTTPException(status_code=404, detail="Discovery session not found.")
        return {"session": to_dict(value)}

    def acquisition_service() -> AcquisitionService:
        return AcquisitionService(state.store, verifier=ShazamMusicBrainzVerifier())

    @app.put("/api/v1/acquisition/acknowledgement")
    async def acquisition_acknowledgement(request: Request) -> dict[str, bool]:
        payload = await request.json()
        acquisition_service().acknowledge(bool(payload.get("accepted")))
        return {"accepted": acquisition_service().is_acknowledged()}

    @app.get("/api/v1/acquisition/candidates")
    async def acquisition_candidates() -> dict[str, Any]:
        return {"candidates": [to_dict(item) for item in state.store.candidates()]}

    @app.post("/api/v1/acquisition/jobs")
    async def approve_acquisition(request: Request) -> dict[str, Any]:
        payload = await request.json()
        try:
            value = acquisition_service().approve(tuple(str(item) for item in payload.get("candidate_ids", [])))
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"job": to_dict(value)}

    @app.post("/api/v1/acquisition/jobs/{acquisition_job_id}/run")
    def run_acquisition(acquisition_job_id: str) -> dict[str, str]:
        value = next((item for item in state.store.acquisition_jobs() if item.id == acquisition_job_id), None)
        if value is None:
            raise HTTPException(status_code=404, detail="Acquisition job not found.")

        def operation() -> dict[str, Any]:
            return {"acquisition": to_dict(acquisition_service().run(value))}

        return {"job_id": state.jobs.submit("acquisition", operation)}

    @app.get("/api/v1/crates")
    async def crates() -> dict[str, Any]:
        return {"crates": [to_dict(item) for item in state.store.crates()]}

    @app.post("/api/v1/crates")
    async def save_crate(request: Request) -> dict[str, Any]:
        payload = await request.json()
        name = str(payload.get("name", "New crate")).strip()
        crate = SmartCrateV1(
            id=str(payload.get("id") or stable_id("crate", name)), name=name,
            rules=tuple(payload.get("rules", [])), include_track_ids=tuple(payload.get("include_track_ids", [])),
            exclude_track_ids=tuple(payload.get("exclude_track_ids", [])), order_by=str(payload.get("order_by", "energy")),
            descending=bool(payload.get("descending", False)),
        )
        try:
            crate = materialize_crate(crate, state.store.catalog_tracks(), state.store.tracks())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        state.store.save_crate(crate)
        return {"crate": to_dict(crate)}

    @app.post("/api/v1/crates/{crate_id}/m3u8")
    async def export_crate(crate_id: str, request: Request) -> dict[str, str]:
        crate = state.store.crate(crate_id)
        if crate is None:
            raise HTTPException(status_code=404, detail="Smart crate not found.")
        payload = await request.json()
        target = Path(str(payload["path"])).expanduser().resolve()
        write_m3u8(target, crate, state.store.catalog_tracks())
        return {"path": str(target)}

    @app.get("/api/v1/catalog/{track_id}/audio")
    async def stream_audio(track_id: str):
        track = state.store.catalog_track(track_id)
        if not track:
            raise HTTPException(status_code=404, detail="Catalog track not found.")
        asset = next((item for item in track.assets if item.kind == "dj-mp3"), None)
        if not asset:
            raise HTTPException(status_code=404, detail="No playable derivative exists for this track.")
        path = Path(asset.path).resolve()
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Playable derivative is missing.")
        return FileResponse(path, media_type=asset.mime_type or "audio/mpeg", filename=path.name)

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
