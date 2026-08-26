from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from test_planner import track

from cratepilot.exporter import ExportError, sanitize_filename, write_rekordbox_package
from cratepilot.planner import replan_sequence


def test_filename_sanitization_is_windows_safe():
    assert sanitize_filename('The / Artist: "Track"?') == "The - Artist - Track"
    assert sanitize_filename(". ") == "Untitled track"


def test_export_is_relative_non_destructive_and_repeat_protected(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    library = []
    original_bytes = {}
    for index in range(3):
        source = sources / f"source-{index}.flac"
        source.write_bytes(f"audio-{index}".encode())
        original_bytes[source] = source.read_bytes()
        library.append(replace(track(index), path=str(source), artist='The / Artist' if index == 0 else f"Artist {index}"))
    plan = replan_sequence(library, title="First booth")
    output = tmp_path / "export"
    manifest = write_rekordbox_package(plan, {item.id: item for item in library}, output)

    playlist = (output / "playlist.m3u8").read_text()
    assert "tracks/01 - The - Artist - Track 0.flac" in playlist
    assert str(tmp_path) not in playlist
    assert (output / "cue-sheet.html").is_file()
    assert (output / "transition-notes.csv").is_file()
    assert (output / "set-plan.json").is_file()
    assert (output / "export-manifest.json").is_file()
    assert all(path.read_bytes() == value for path, value in original_bytes.items())
    assert manifest.mastering["true_peak_db"] == -1.0

    with pytest.raises(ExportError, match="not empty"):
        write_rekordbox_package(plan, {item.id: item for item in library}, output)


def test_export_manifest_has_stable_hashes(tmp_path: Path):
    source = tmp_path / "one.flac"
    source.write_bytes(b"stable audio")
    value = replace(track(1), path=str(source))
    plan = replan_sequence([value, replace(track(2), path=str(source), id="second")], title="Stable")
    output = tmp_path / "package"
    manifest = write_rekordbox_package(plan, {value.id: value, "second": replace(track(2), path=str(source), id="second")}, output)
    hashes = {item["path"]: item["sha256"] for item in manifest.files}
    assert hashes["tracks/01 - Artist 1 - Track 1.flac"] == hashes["tracks/02 - Artist 2 - Track 2.flac"]


def test_manifest_is_reproducible_and_does_not_leak_output_path(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    library = []
    for index in range(2):
        source = sources / f"{index}.flac"
        source.write_bytes(f"fixture-{index}".encode())
        library.append(replace(track(index), path=str(source)))
    plan = replan_sequence(library, title="Repeatable")
    first = write_rekordbox_package(plan, {item.id: item for item in library}, tmp_path / "one")
    second = write_rekordbox_package(plan, {item.id: item for item in library}, tmp_path / "two")
    assert first == second
    assert first.output_directory == "."
