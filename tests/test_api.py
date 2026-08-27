import dataclasses
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from cratepilot.api import LocalState, create_app, is_allowed_origin, is_local_host, validate_paths_payload
from cratepilot.file_picker import FilePickerError, import_into_library
from cratepilot.storage import Store


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_local_server_boundary_helpers():
    assert is_local_host("127.0.0.1:8765")
    assert is_local_host("localhost:8765")
    assert not is_local_host("cratepilot.attacker.invalid")
    assert is_allowed_origin(None)
    assert is_allowed_origin("http://127.0.0.1:8765")
    assert not is_allowed_origin("https://attacker.invalid")


@pytest.mark.anyio
async def test_local_api_requires_session_token_and_rejects_foreign_origins(tmp_path: Path):
    library = tmp_path / "music"
    library.mkdir()
    app = create_app(library, store=Store(tmp_path / "db.sqlite"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        assert (await client.get("/api/v1/health")).status_code == 403
        token = app.state.cratepilot.token
        healthy = await client.get("/api/v1/health", headers={"x-cratepilot-token": token})
        assert healthy.status_code == 200
        assert "system" in healthy.json()
        response = await client.get(
            "/api/v1/health",
            headers={"x-cratepilot-token": token, "origin": "https://attacker.invalid"},
        )
        assert response.status_code == 403


def test_local_state_rejects_paths_outside_library_and_symlink_escapes(tmp_path: Path):
    library = tmp_path / "music"
    library.mkdir()
    outside = tmp_path / "outside.flac"
    outside.write_bytes(b"not audio")
    state = LocalState(library, store=Store(tmp_path / "db.sqlite"))
    with pytest.raises(HTTPException) as direct:
        state.validate_path(str(outside))
    assert direct.value.status_code == 403
    link = library / "escape.flac"
    link.symlink_to(outside)
    with pytest.raises(HTTPException):
        state.validate_path(str(link))


def test_analysis_paths_payload_must_be_a_list():
    assert validate_paths_payload(None) is None
    assert validate_paths_payload(["one.flac"]) == ["one.flac"]
    with pytest.raises(HTTPException) as invalid:
        validate_paths_payload("not-a-list")
    assert invalid.value.status_code == 422


def test_import_into_library_preserves_inside_files_and_copies_outside_without_overwriting(tmp_path: Path):
    library = tmp_path / "music"
    library.mkdir()
    inside = library / "Owned.flac"
    inside.write_bytes(b"owned")
    assert import_into_library(inside, library) == (inside.resolve(), False)

    outside = tmp_path / "Owned.flac"
    outside.write_bytes(b"new audio")
    imported, copied = import_into_library(outside, library)
    assert copied is True
    assert imported == library / "Owned (2).flac"
    assert imported.read_bytes() == b"new audio"
    assert inside.read_bytes() == b"owned"

    unsupported = tmp_path / "notes.txt"
    unsupported.write_text("not music")
    with pytest.raises(FilePickerError):
        import_into_library(unsupported, library)


@pytest.mark.anyio
async def test_browse_endpoint_starts_at_library_and_imports_outside_selection(tmp_path: Path):
    library = tmp_path / "music"
    library.mkdir()
    outside = tmp_path / "Chosen.opus"
    outside.write_bytes(b"audio fixture")
    opened_at: list[Path] = []

    def picker(initial_directory: Path) -> Path:
        opened_at.append(initial_directory)
        return outside

    app = create_app(library, store=Store(tmp_path / "db.sqlite"), file_picker=picker)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/library/browse", headers={"x-cratepilot-token": app.state.cratepilot.token}
        )
    assert response.status_code == 200
    assert opened_at == [library.resolve()]
    payload = response.json()
    assert payload["display_name"] == "Chosen.opus"
    assert payload["copied_to_library"] is True
    assert Path(payload["path"]).is_relative_to(library)


@pytest.mark.anyio
async def test_browse_endpoint_reports_cancel_without_changing_library(tmp_path: Path):
    library = tmp_path / "music"
    library.mkdir()
    app = create_app(library, store=Store(tmp_path / "db.sqlite"), file_picker=lambda _root: None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/library/browse", headers={"x-cratepilot-token": app.state.cratepilot.token}
        )
    assert response.json() == {"cancelled": True}


@pytest.mark.anyio
async def test_playlist_picker_filters_to_analyzed_tracks_inside_library(tmp_path: Path):
    from test_planner import track

    library = tmp_path / "music"
    library.mkdir()
    first = library / "one.flac"
    second = library / "two.flac"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    outside = tmp_path / "outside.flac"
    outside.write_bytes(b"outside")
    playlist = tmp_path / "focus.m3u8"
    playlist.write_text(f"#EXTM3U\n{first}\n{outside}\n{second}\n")
    store = Store(tmp_path / "db.sqlite")
    store.save_tracks([dataclasses.replace(track(1), path=str(first)), dataclasses.replace(track(2), path=str(second))])
    app = create_app(library, store=store, playlist_picker=lambda _root: playlist)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/library/browse-playlist", headers={"x-cratepilot-token": app.state.cratepilot.token}
        )
    payload = response.json()
    assert payload["entry_count"] == 2
    assert payload["matched_count"] == 2
    assert payload["track_ids"] == ["track-01", "track-02"]


@pytest.mark.anyio
async def test_analysis_endpoint_rejects_non_list_paths(tmp_path: Path):
    library = tmp_path / "music"
    library.mkdir()
    app = create_app(library, store=Store(tmp_path / "db.sqlite"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/library/analyze",
            headers={"x-cratepilot-token": app.state.cratepilot.token},
            json={"paths": "not-a-list"},
        )
        assert response.status_code == 422


@pytest.mark.anyio
async def test_v2_manual_discovery_session_and_safe_acquisition_boundary(tmp_path: Path):
    library = tmp_path / "music"
    library.mkdir()
    app = create_app(library, store=Store(tmp_path / "db.sqlite"))
    transport = httpx.ASGITransport(app=app)
    headers = {"x-cratepilot-token": app.state.cratepilot.token}
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/discovery/sessions", headers=headers,
            json={
                "seeds": [{"kind": "manual", "artist": "Artist", "title": "Track"}],
                "readiness_target": 8,
                "result_count": 42,
            },
        )
        assert response.status_code == 200
        session = response.json()["session"]
        assert session["max_depth"] == 2
        assert session["max_nodes"] == 150
        assert session["review_batch_size"] == 42
        catalog = await client.get("/api/v1/catalog", headers=headers)
        assert catalog.status_code == 200
        acknowledgement = await client.put(
            "/api/v1/acquisition/acknowledgement", headers=headers, json={"accepted": True}
        )
        assert acknowledgement.json() == {"accepted": True}
        current = await client.get("/api/v1/acquisition/acknowledgement", headers=headers)
        assert current.json() == {"accepted": True}
        revoked = await client.put(
            "/api/v1/acquisition/acknowledgement", headers=headers, json={"accepted": False}
        )
        assert revoked.json() == {"accepted": False}


@pytest.mark.anyio
async def test_audio_stream_does_not_accept_arbitrary_paths(tmp_path: Path):
    library = tmp_path / "music"
    library.mkdir()
    app = create_app(library, store=Store(tmp_path / "db.sqlite"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/api/v1/catalog/not-a-track/audio", headers={"x-cratepilot-token": app.state.cratepilot.token}
        )
        assert response.status_code == 404
