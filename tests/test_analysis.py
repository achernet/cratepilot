"""Behavioral tests for concurrent library analysis."""

from __future__ import annotations

import threading
from pathlib import Path

from cratepilot.analysis import analyze_paths


def test_analyze_paths_runs_tracks_in_parallel_and_preserves_input_order(tmp_path: Path, monkeypatch):
    paths = [tmp_path / "first.mp3", tmp_path / "second.mp3"]
    barrier = threading.Barrier(2, timeout=5)
    lock = threading.Lock()
    active = 0
    peak_active = 0
    scratch_directories: set[Path] = set()

    def fake_analyze(path: Path, **kwargs):
        nonlocal active, peak_active
        with lock:
            active += 1
            peak_active = max(peak_active, active)
            scratch_directories.add(kwargs["temporary_dir"])
        barrier.wait()
        with lock:
            active -= 1
        return path

    monkeypatch.setattr("cratepilot.analysis.djmix.analyze_track", fake_analyze)
    monkeypatch.setattr("cratepilot.analysis.public_analysis", lambda path: path.name)
    progress: list[str] = []

    result = analyze_paths(
        paths,
        cache_directory=tmp_path / "cache",
        max_workers=2,
        progress_callback=lambda _value, message: progress.append(message),
    )

    assert result == ["first.mp3", "second.mp3"]
    assert peak_active == 2
    assert len(scratch_directories) == 2
    assert progress[-1] == "Analyzed 2 tracks with 2 parallel workers."


def test_analyze_paths_handles_an_empty_library_without_starting_workers(tmp_path: Path):
    messages: list[str] = []

    assert analyze_paths([], cache_directory=tmp_path / "cache", progress_callback=lambda _, msg: messages.append(msg)) == []
    assert messages == ["No audio files to analyze."]

