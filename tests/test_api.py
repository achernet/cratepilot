from pathlib import Path

import pytest
from fastapi import HTTPException

from cratepilot.api import LocalState, is_allowed_origin, is_local_host, validate_paths_payload
from cratepilot.storage import Store


def test_local_server_boundary_helpers():
    assert is_local_host("127.0.0.1:8765")
    assert is_local_host("localhost:8765")
    assert not is_local_host("cratepilot.attacker.invalid")
    assert is_allowed_origin(None)
    assert is_allowed_origin("http://127.0.0.1:8765")
    assert not is_allowed_origin("https://attacker.invalid")


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
