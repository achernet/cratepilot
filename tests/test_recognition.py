from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cratepilot.recognition import RecognitionError, _recognize


def test_songrec_recognition_uses_json_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    sample = tmp_path / "sample.wav"
    sample.write_bytes(b"RIFF")
    payload = {"track": {"subtitle": "Artist", "title": "Track", "url": "https://example.test"}}
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(subprocess, "run", run)
    assert _recognize(sample) == payload
    assert calls == [["songrec", "recognize", "-j", str(sample)]]


def test_songrec_missing_has_actionable_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def missing(*args, **kwargs):
        raise FileNotFoundError("songrec")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(RecognitionError, match="songrec is required on PATH"):
        _recognize(tmp_path / "sample.wav")


def test_songrec_invalid_json_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "not-json", ""),
    )
    with pytest.raises(RecognitionError, match="invalid JSON"):
        _recognize(tmp_path / "sample.wav")
