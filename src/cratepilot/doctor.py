from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Sequence


@dataclass(frozen=True)
class DependencyStatus:
    command: str
    available: bool
    path: str | None
    version: str | None
    purpose: str


DEPENDENCIES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("python", ("--version",), "runs CratePilot and its analysis services"),
    ("ffmpeg", ("-version",), "decodes, previews, and renders audio"),
    ("ffprobe", ("-version",), "inspects audio duration and stream metadata"),
    ("songrec", ("--version",), "verifies track identity from sampled audio"),
    ("mp3gain", ("-v",), "normalizes acquired DJ MP3 derivatives"),
    ("yt-dlp", ("--version",), "searches and acquires an explicitly approved source"),
)


def _first_line(value: str) -> str | None:
    line = next((item.strip() for item in value.splitlines() if item.strip()), "")
    return line[:240] or None


def dependency_status(
    dependencies: Sequence[tuple[str, tuple[str, ...], str]] = DEPENDENCIES,
) -> tuple[DependencyStatus, ...]:
    results: list[DependencyStatus] = []
    for command, version_args, purpose in dependencies:
        path = shutil.which(command)
        version = None
        if path:
            try:
                completed = subprocess.run(
                    [path, *version_args], capture_output=True, text=True, timeout=15, check=False
                )
                version = _first_line(completed.stdout or completed.stderr)
            except (OSError, subprocess.SubprocessError):
                pass
        results.append(DependencyStatus(command, bool(path), path, version, purpose))
    return tuple(results)


def doctor_report() -> dict[str, object]:
    dependencies = dependency_status()
    return {
        "ok": all(item.available for item in dependencies),
        "runtime": sys.version.split()[0],
        "dependencies": [asdict(item) for item in dependencies],
    }
