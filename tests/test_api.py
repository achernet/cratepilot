from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from cratepilot.api import LocalState, create_app, is_allowed_origin, is_local_host, validate_paths_payload
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
        assert (await client.get("/api/v1/health", headers={"x-cratepilot-token": token})).status_code == 200
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
            json={"seeds": [{"kind": "manual", "artist": "Artist", "title": "Track"}], "readiness_target": 8},
        )
        assert response.status_code == 200
        session = response.json()["session"]
        assert session["max_depth"] == 2
        assert session["max_nodes"] == 150
        assert session["review_batch_size"] == 30
        catalog = await client.get("/api/v1/catalog", headers=headers)
        assert catalog.status_code == 200
        acknowledgement = await client.put(
            "/api/v1/acquisition/acknowledgement", headers=headers, json={"accepted": True}
        )
        assert acknowledgement.json() == {"accepted": True}
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
