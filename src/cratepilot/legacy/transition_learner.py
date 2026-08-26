#!/usr/bin/env python3
"""
Learn DJ-transition patterns from complete recorded sets without flooding Shazam.

The script can:

1. Accept a local audio file or a URL supported by yt-dlp.
2. Prefer published chapter/tracklist timestamps when available.
3. Otherwise identify tracks with sparse, cached SongRec queries.
4. Adaptively refine only intervals where the recognized track changes.
5. Estimate transition entry, midpoint, bass handoff, and exit using local DSP.
6. Append compact transition examples to a reusable JSONL dataset.
7. Recommend transition parameters for a new pair of local tracks by retrieving
   similar learned examples.

External commands:
    ffmpeg, ffprobe, songrec
    yt-dlp is required only for URL inputs.

Python packages:
    librosa, numpy, scipy

Example:
    python luminosity_transition_learner.py analyze \
        "https://www.youtube.com/watch?v=..." \
        --work-dir .djlearn

    python luminosity_transition_learner.py recommend \
        "Current Track.mp3" "Next Track.mp3" \
        --dataset .djlearn/transitions.jsonl
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import math
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import librosa
import numpy as np
from scipy.ndimage import median_filter

LOG = logging.getLogger("djlearn")
VERSION = "1.0.0"

COMMON_BAR_COUNTS = (4, 8, 12, 16, 24, 32, 48, 64)
PHRASE_BAR_COUNTS = (8, 16, 24, 32, 48, 64)
CURATION_WEIGHTS = {
    "unrated": 1.0,
    "liked": 1.25,
    "gold": 1.5,
}
NON_TRACK_CUE_LABELS = {
    "intro",
    "introduction",
    "opening",
    "outro",
    "ending",
    "end",
    "credits",
}

NOTE_NAMES = (
    "C",
    "C#",
    "D",
    "Eb",
    "E",
    "F",
    "F#",
    "G",
    "Ab",
    "A",
    "Bb",
    "B",
)

# Krumhansl-Schmuckler key profiles, ordered C through B.
MAJOR_PROFILE = np.asarray(
    [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88],
    dtype=np.float64,
)
MINOR_PROFILE = np.asarray(
    [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17],
    dtype=np.float64,
)

CAMELOT_MAJOR = {
    "C": "8B",
    "C#": "3B",
    "D": "10B",
    "Eb": "5B",
    "E": "12B",
    "F": "7B",
    "F#": "2B",
    "G": "9B",
    "Ab": "4B",
    "A": "11B",
    "Bb": "6B",
    "B": "1B",
}
CAMELOT_MINOR = {
    "C": "5A",
    "C#": "12A",
    "D": "7A",
    "Eb": "2A",
    "E": "9A",
    "F": "4A",
    "F#": "11A",
    "G": "6A",
    "Ab": "1A",
    "A": "8A",
    "Bb": "3A",
    "B": "10A",
}


class DjLearnError(RuntimeError):
    """Base error raised by this program."""


class QueryBudgetExhausted(DjLearnError):
    """Raised when the configured SongRec request budget is exhausted."""


class RecognitionCircuitOpen(DjLearnError):
    """Raised when repeated recognition failures stop further requests."""


@dataclass(frozen=True)
class TrackIdentity:
    """A normalized track identity returned by SongRec or a published tracklist."""

    track_key: str
    artist: str
    title: str
    album: str | None = None

    @property
    def label(self) -> str:
        if self.artist:
            return f"{self.artist} - {self.title}"
        return self.title


@dataclass(frozen=True)
class Recognition:
    """One recognition sample from a point in a set."""

    anchor_seconds: float
    window_start_seconds: float
    window_seconds: float
    status: str
    identity: TrackIdentity | None
    cached: bool = False


@dataclass(frozen=True)
class PublishedCue:
    """A timestamp and label obtained from chapters or the video description."""

    start_seconds: float
    label: str
    source: str


@dataclass(frozen=True)
class ChangeBracket:
    """Two stable labels surrounding a possible track transition."""

    left_anchor_seconds: float
    right_anchor_seconds: float
    outgoing: TrackIdentity
    incoming: TrackIdentity
    source: str
    cue_seconds: float | None = None


@dataclass(frozen=True)
class KeyEstimate:
    """Estimated musical key and confidence."""

    name: str
    mode: str
    camelot: str
    confidence: float

    @property
    def label(self) -> str:
        return f"{self.name} {self.mode}"


@dataclass(frozen=True)
class ContextFeatures:
    """Compact features describing one side of a transition."""

    bpm: float
    key: str
    camelot: str
    key_confidence: float
    rms_db: float
    low_ratio: float
    mid_ratio: float
    high_ratio: float
    spectral_centroid_hz: float
    onset_strength: float


@dataclass(frozen=True)
class TransitionExample:
    """A learned transition summarized without storing source audio."""

    schema_version: int
    transition_id: str
    created_at: str
    source_key: str
    source_title: str
    source_path: str
    outgoing: dict[str, Any]
    incoming: dict[str, Any]
    bracket_start_seconds: float
    bracket_end_seconds: float
    transition_entry_seconds: float
    transition_midpoint_seconds: float
    bass_handoff_seconds: float
    transition_exit_seconds: float
    overlap_seconds: float
    overlap_bars_raw: float
    crossfade_bars: int
    phrase_bars: int
    bass_handoff_fraction: float
    estimated_bass_cut_db: float
    filter_sweep_score: float
    filter_sweep: bool
    fade_shape: str
    outgoing_context: dict[str, Any]
    incoming_context: dict[str, Any]
    bpm_delta: float
    camelot_distance: float
    analysis_confidence: float
    bracket_source: str
    curation: str
    preference_weight: float
    cue_timestamp_seconds: float | None
    timing_basis: str
    timing_confidence: float


@dataclass(frozen=True)
class SourceInfo:
    """Resolved local media and optional yt-dlp metadata."""

    audio_path: Path
    source_key: str
    title: str
    metadata: dict[str, Any]


@dataclass
class QueryBudget:
    """Hard limit on uncached recognition requests."""

    maximum: int
    used: int = 0

    def consume(self) -> None:
        if self.used >= self.maximum:
            raise QueryBudgetExhausted(
                f"SongRec query budget exhausted ({self.used}/{self.maximum})"
            )
        self.used += 1


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )


def run_command(
    command: Sequence[str],
    *,
    timeout: float | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a command without invoking a shell."""

    LOG.debug("Running: %s", shlex.join(command))
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise DjLearnError(f"Required command not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DjLearnError(f"Command timed out: {shlex.join(command)}") from exc

    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise DjLearnError(
            f"Command failed ({completed.returncode}): {shlex.join(command)}\n{detail}"
        )
    return completed


def require_commands(commands: Iterable[str]) -> None:
    missing = [command for command in commands if shutil.which(command) is None]
    if missing:
        raise DjLearnError(f"Missing required command(s): {', '.join(missing)}")


def is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def safe_slug(value: str, maximum_length: int = 80) -> str:
    value = re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE).strip("-._")
    return (value or "source")[:maximum_length]


def source_fingerprint(path: Path) -> str:
    """
    Create a stable, inexpensive identity from file size plus its first and last MiB.

    This avoids hashing a multi-hour recording in full.
    """

    digest = hashlib.sha256()
    size = path.stat().st_size
    digest.update(str(size).encode("ascii"))

    with path.open("rb") as stream:
        digest.update(stream.read(1024 * 1024))
        if size > 1024 * 1024:
            stream.seek(max(0, size - 1024 * 1024))
            digest.update(stream.read(1024 * 1024))

    return digest.hexdigest()


def resolve_source(source: str, work_dir: Path) -> SourceInfo:
    """Resolve a local media file or download one URL with yt-dlp."""

    if not is_url(source):
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise DjLearnError(f"Input file does not exist: {path}")
        return SourceInfo(
            audio_path=path,
            source_key=source_fingerprint(path),
            title=path.stem,
            metadata={},
        )

    require_commands(("yt-dlp",))
    metadata_result = run_command(
        ["yt-dlp", "--no-playlist", "--dump-single-json", source],
        timeout=180,
    )
    try:
        metadata = json.loads(metadata_result.stdout)
    except json.JSONDecodeError as exc:
        raise DjLearnError("yt-dlp returned invalid metadata JSON") from exc

    video_id = str(metadata.get("id") or hashlib.sha256(source.encode()).hexdigest()[:16])
    title = str(metadata.get("title") or video_id)
    source_dir = work_dir / "sources" / safe_slug(video_id)
    source_dir.mkdir(parents=True, exist_ok=True)

    metadata_path = source_dir / "metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    existing = [
        candidate
        for candidate in source_dir.glob("source.*")
        if candidate.is_file()
        and candidate.suffix not in {".part", ".ytdl", ".json"}
    ]
    if existing:
        audio_path = max(existing, key=lambda candidate: candidate.stat().st_size)
    else:
        output_template = str(source_dir / "source.%(ext)s")
        run_command(
            [
                "yt-dlp",
                "--no-playlist",
                "--format",
                "bestaudio/best",
                "--output",
                output_template,
                source,
            ],
            timeout=None,
        )
        downloaded = [
            candidate
            for candidate in source_dir.glob("source.*")
            if candidate.is_file()
            and candidate.suffix not in {".part", ".ytdl", ".json"}
        ]
        if not downloaded:
            raise DjLearnError("yt-dlp completed without creating an audio file")
        audio_path = max(downloaded, key=lambda candidate: candidate.stat().st_size)

    source_key = f"youtube:{video_id}"
    return SourceInfo(
        audio_path=audio_path.resolve(),
        source_key=source_key,
        title=title,
        metadata=metadata,
    )


def probe_duration(path: Path) -> float:
    result = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise DjLearnError(f"Could not determine duration of {path}") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise DjLearnError(f"Invalid duration reported for {path}: {duration}")
    return duration


def timestamp_to_seconds(value: str) -> float:
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return float(minutes * 60 + seconds)
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return float(hours * 3600 + minutes * 60 + seconds)
    raise ValueError(f"Unsupported timestamp: {value}")


def clean_published_label(label: str) -> str:
    """Remove list numbering added to otherwise useful chapter labels."""

    cleaned = re.sub(
        r"^\s*(?:track\s*)?#?\d+\s*(?:[.)]{1,3}|[-:])\s*",
        "",
        label,
        flags=re.IGNORECASE,
    )
    return cleaned.strip()


def is_non_track_cue(label: str) -> bool:
    """Return whether a published cue names set structure instead of a track."""

    normalized = normalize_text(clean_published_label(label))
    return normalized in NON_TRACK_CUE_LABELS


def discover_published_cues(
    metadata: dict[str, Any],
    duration: float,
) -> list[PublishedCue]:
    """Read chapters first, then timestamped lines from a description."""

    chapters = metadata.get("chapters")
    chapter_cues: list[PublishedCue] = []
    if isinstance(chapters, list):
        for chapter in chapters:
            if not isinstance(chapter, dict):
                continue
            start = chapter.get("start_time")
            title = chapter.get("title")
            if isinstance(start, (int, float)) and isinstance(title, str):
                if 0 <= float(start) < duration and title.strip():
                    label = clean_published_label(title)
                    if label:
                        chapter_cues.append(
                            PublishedCue(float(start), label, "chapter")
                        )
    chapter_cues.sort(key=lambda cue: cue.start_seconds)
    if len(chapter_cues) >= 2:
        return deduplicate_cues(chapter_cues)

    description = metadata.get("description")
    if not isinstance(description, str):
        return []

    pattern = re.compile(
        r"(?m)^\s*(?:(?:[-*•]|\d+[.)])\s*)?\[?"
        r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\]?"
        r"\s*(?:[-–—|:]\s*)?"
        r"(?P<label>.+?)\s*$"
    )
    cues: list[PublishedCue] = []
    for match in pattern.finditer(description):
        try:
            timestamp = timestamp_to_seconds(match.group("time"))
        except ValueError:
            continue
        label = clean_published_label(match.group("label"))
        if (
            0 <= timestamp < duration
            and 2 <= len(label) <= 180
            and not label.startswith(("http://", "https://"))
        ):
            cues.append(PublishedCue(timestamp, label, "description"))

    cues.sort(key=lambda cue: cue.start_seconds)
    return deduplicate_cues(cues)


def deduplicate_cues(cues: Sequence[PublishedCue]) -> list[PublishedCue]:
    result: list[PublishedCue] = []
    for cue in cues:
        if result and abs(cue.start_seconds - result[-1].start_seconds) < 1.0:
            continue
        result.append(cue)
    return result


def normalize_text(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}", " ", value)
    value = re.sub(r"\bthe\b", " ", value)
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def identity_from_label(label: str, namespace: str = "published") -> TrackIdentity:
    separators = (" - ", " – ", " — ")
    artist = ""
    title = label.strip()
    for separator in separators:
        if separator in label:
            artist, title = (part.strip() for part in label.split(separator, 1))
            break

    normalized = normalize_text(f"{artist} {title}")
    track_key = f"{namespace}:{hashlib.sha256(normalized.encode()).hexdigest()[:20]}"
    return TrackIdentity(track_key=track_key, artist=artist, title=title)


def identities_equal(
    left: TrackIdentity | None,
    right: TrackIdentity | None,
) -> bool:
    if left is None or right is None:
        return False
    if left.track_key == right.track_key:
        return True
    return normalize_text(left.label) == normalize_text(right.label)


class RecognitionCache:
    """Persistent SQLite cache for recognition requests and no-match results."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS recognition_cache (
                source_key TEXT NOT NULL,
                start_ms INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL,
                recognizer TEXT NOT NULL,
                status TEXT NOT NULL,
                track_key TEXT,
                artist TEXT,
                title TEXT,
                album TEXT,
                payload_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (source_key, start_ms, duration_ms, recognizer)
            )
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def get(
        self,
        *,
        source_key: str,
        start_seconds: float,
        duration_seconds: float,
        recognizer: str,
        anchor_seconds: float,
    ) -> Recognition | None:
        start_ms = round(start_seconds * 1000)
        duration_ms = round(duration_seconds * 1000)
        row = self.connection.execute(
            """
            SELECT *
            FROM recognition_cache
            WHERE source_key = ?
              AND start_ms = ?
              AND duration_ms = ?
              AND recognizer = ?
            """,
            (source_key, start_ms, duration_ms, recognizer),
        ).fetchone()
        if row is None:
            return None

        identity = None
        if row["status"] == "match":
            identity = TrackIdentity(
                track_key=row["track_key"],
                artist=row["artist"] or "",
                title=row["title"] or "",
                album=row["album"],
            )
        return Recognition(
            anchor_seconds=anchor_seconds,
            window_start_seconds=start_seconds,
            window_seconds=duration_seconds,
            status=row["status"],
            identity=identity,
            cached=True,
        )

    def put(
        self,
        *,
        source_key: str,
        recognition: Recognition,
        recognizer: str,
        payload: dict[str, Any] | None,
    ) -> None:
        identity = recognition.identity
        self.connection.execute(
            """
            INSERT OR REPLACE INTO recognition_cache (
                source_key,
                start_ms,
                duration_ms,
                recognizer,
                status,
                track_key,
                artist,
                title,
                album,
                payload_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_key,
                round(recognition.window_start_seconds * 1000),
                round(recognition.window_seconds * 1000),
                recognizer,
                recognition.status,
                identity.track_key if identity else None,
                identity.artist if identity else None,
                identity.title if identity else None,
                identity.album if identity else None,
                json.dumps(payload, ensure_ascii=False) if payload else None,
                dt.datetime.now(dt.timezone.utc).isoformat(),
            ),
        )
        self.connection.commit()

    def stats(self) -> dict[str, int]:
        rows = self.connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM recognition_cache
            GROUP BY status
            """
        ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}


def find_first_mapping_with_track(data: Any) -> dict[str, Any] | None:
    """Find a Shazam-style track mapping in nested JSON."""

    if isinstance(data, dict):
        track = data.get("track")
        if isinstance(track, dict):
            return track

        has_title = isinstance(data.get("title") or data.get("name"), str)
        has_artist = any(
            isinstance(data.get(key), (str, list, dict))
            for key in ("subtitle", "artist", "artists", "byArtist")
        )
        if has_title and has_artist:
            return data

        for value in data.values():
            found = find_first_mapping_with_track(value)
            if found is not None:
                return found

    if isinstance(data, list):
        for value in data:
            found = find_first_mapping_with_track(value)
            if found is not None:
                return found

    return None


def artist_from_track_mapping(track: dict[str, Any]) -> str:
    for key in ("subtitle", "artist", "byArtist"):
        value = track.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            name = value.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()

    artists = track.get("artists")
    if isinstance(artists, list):
        names: list[str] = []
        for artist in artists:
            if isinstance(artist, str) and artist.strip():
                names.append(artist.strip())
            elif isinstance(artist, dict):
                name = artist.get("name")
                if isinstance(name, str) and name.strip():
                    names.append(name.strip())
        if names:
            return ", ".join(names)

    return ""


def identity_from_songrec_json(payload: dict[str, Any]) -> TrackIdentity | None:
    track = find_first_mapping_with_track(payload)
    if track is None:
        return None

    title = track.get("title") or track.get("name")
    if not isinstance(title, str) or not title.strip():
        return None

    artist = artist_from_track_mapping(track)
    album = track.get("album")
    if not isinstance(album, str):
        album = None

    raw_key = (
        track.get("key")
        or track.get("id")
        or track.get("adamid")
        or track.get("url")
    )
    if raw_key is None:
        raw_key = hashlib.sha256(
            normalize_text(f"{artist} {title}").encode()
        ).hexdigest()[:24]

    return TrackIdentity(
        track_key=f"songrec:{raw_key}",
        artist=artist,
        title=title.strip(),
        album=album,
    )


class SongRecRecognizer:
    """Rate-limited, cached wrapper around SongRec."""

    def __init__(
        self,
        *,
        audio_path: Path,
        duration_seconds: float,
        source_key: str,
        cache: RecognitionCache,
        temporary_dir: Path,
        command: Sequence[str],
        window_seconds: float,
        minimum_request_interval: float,
        budget: QueryBudget,
        timeout_seconds: float,
        maximum_retries: int,
        retry_delay_seconds: float,
        maximum_consecutive_failures: int,
        maximum_consecutive_no_matches: int,
    ) -> None:
        self.audio_path = audio_path
        self.duration_seconds = duration_seconds
        self.source_key = source_key
        self.cache = cache
        self.temporary_dir = temporary_dir
        self.command = tuple(command)
        self.window_seconds = window_seconds
        self.minimum_request_interval = minimum_request_interval
        self.budget = budget
        self.timeout_seconds = timeout_seconds
        self.maximum_retries = maximum_retries
        self.retry_delay_seconds = retry_delay_seconds
        self.maximum_consecutive_failures = maximum_consecutive_failures
        self.maximum_consecutive_no_matches = maximum_consecutive_no_matches
        self.last_request_monotonic: float | None = None
        self.consecutive_failures = 0
        self.consecutive_no_matches = 0
        self.successful_matches = 0
        self.recognizer_name = " ".join(self.command)

    def recognize(self, anchor_seconds: float) -> Recognition:
        window_start = min(
            max(0.0, anchor_seconds - self.window_seconds / 2.0),
            max(0.0, self.duration_seconds - self.window_seconds),
        )
        anchor = window_start + self.window_seconds / 2.0

        cached = self.cache.get(
            source_key=self.source_key,
            start_seconds=window_start,
            duration_seconds=self.window_seconds,
            recognizer=self.recognizer_name,
            anchor_seconds=anchor,
        )
        if cached is not None:
            LOG.info(
                "Recognition cache %8.1fs: %s",
                anchor,
                cached.identity.label if cached.identity else cached.status,
            )
            return cached

        self.budget.consume()
        self._pace_request()

        temporary_path = (
            self.temporary_dir
            / f"recognition-{round(window_start * 1000):012d}.wav"
        )
        extract_audio(
            input_path=self.audio_path,
            output_path=temporary_path,
            start_seconds=window_start,
            duration_seconds=self.window_seconds,
            sample_rate=16_000,
        )

        try:
            recognition, payload = self._run_songrec_with_retries(
                temporary_path=temporary_path,
                anchor_seconds=anchor,
                window_start_seconds=window_start,
            )
        finally:
            temporary_path.unlink(missing_ok=True)

        self.cache.put(
            source_key=self.source_key,
            recognition=recognition,
            recognizer=self.recognizer_name,
            payload=payload,
        )
        LOG.info(
            "Recognition query %8.1fs [%d/%d]: %s",
            anchor,
            self.budget.used,
            self.budget.maximum,
            recognition.identity.label if recognition.identity else recognition.status,
        )
        return recognition

    def _pace_request(self) -> None:
        if self.last_request_monotonic is not None:
            elapsed = time.monotonic() - self.last_request_monotonic
            remaining = self.minimum_request_interval - elapsed
            if remaining > 0:
                LOG.debug("Pacing SongRec request for %.1f seconds", remaining)
                time.sleep(remaining)
        self.last_request_monotonic = time.monotonic()

    def _run_songrec_with_retries(
        self,
        *,
        temporary_path: Path,
        anchor_seconds: float,
        window_start_seconds: float,
    ) -> tuple[Recognition, dict[str, Any] | None]:
        attempts = self.maximum_retries + 1
        last_error = ""

        for attempt in range(attempts):
            completed = run_command(
                [*self.command, str(temporary_path)],
                timeout=self.timeout_seconds,
                check=False,
            )
            if completed.returncode == 0:
                payload = self._parse_json_output(completed.stdout)
                if payload is not None:
                    identity = identity_from_songrec_json(payload)
                    status = "match" if identity else "no_match"
                    recognition = Recognition(
                        anchor_seconds=anchor_seconds,
                        window_start_seconds=window_start_seconds,
                        window_seconds=self.window_seconds,
                        status=status,
                        identity=identity,
                    )
                    self._update_circuit_state(recognition)
                    return recognition, payload

                last_error = "SongRec returned invalid or empty JSON"
            else:
                last_error = (
                    completed.stderr.strip()
                    or completed.stdout.strip()
                    or f"SongRec exited with {completed.returncode}"
                )

            self.consecutive_failures += 1
            if self.consecutive_failures >= self.maximum_consecutive_failures:
                raise RecognitionCircuitOpen(
                    "Recognition stopped after repeated SongRec failures. "
                    "Cached progress is safe; resume later.\n"
                    f"Last error: {last_error}"
                )

            if attempt + 1 < attempts:
                delay = self.retry_delay_seconds * (2**attempt)
                LOG.warning("%s; retrying after %.0f seconds", last_error, delay)
                time.sleep(delay)

        raise DjLearnError(last_error or "SongRec recognition failed")

    @staticmethod
    def _parse_json_output(stdout: str) -> dict[str, Any] | None:
        text = stdout.strip()
        if not text:
            return None

        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else {"result": value}
        except json.JSONDecodeError:
            pass

        # Some commands print status lines before the final JSON object.
        for line in reversed(text.splitlines()):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            return value if isinstance(value, dict) else {"result": value}
        return None

    def _update_circuit_state(self, recognition: Recognition) -> None:
        self.consecutive_failures = 0
        if recognition.identity is not None:
            self.successful_matches += 1
            self.consecutive_no_matches = 0
            return

        self.consecutive_no_matches += 1
        if (
            self.successful_matches > 0
            and self.consecutive_no_matches >= self.maximum_consecutive_no_matches
        ):
            raise RecognitionCircuitOpen(
                "Recognition stopped after repeated no-match responses following "
                "successful matches. This may indicate throttling. Cached progress "
                "is safe; resume later."
            )


def extract_audio(
    *,
    input_path: Path,
    output_path: Path,
    start_seconds: float,
    duration_seconds: float,
    sample_rate: int,
) -> None:
    """Decode one mono PCM WAV segment with ffmpeg."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-ss",
            f"{max(0.0, start_seconds):.3f}",
            "-t",
            f"{max(0.05, duration_seconds):.3f}",
            "-i",
            str(input_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            "-y",
            str(output_path),
        ]
    )


def sparse_anchor_times(
    duration: float,
    window_seconds: float,
    step_seconds: float,
) -> list[float]:
    if duration <= window_seconds:
        return [duration / 2.0]

    first = window_seconds / 2.0
    last = duration - window_seconds / 2.0
    values: list[float] = []
    current = first
    while current <= last:
        values.append(current)
        current += step_seconds

    if values and last - values[-1] > step_seconds * 0.4:
        values.append(last)
    return values


def smooth_recognition_labels(
    recognitions: Sequence[Recognition],
) -> list[Recognition]:
    """Remove one-sample A-B-A label glitches."""

    result = list(sorted(recognitions, key=lambda item: item.anchor_seconds))
    for index in range(1, len(result) - 1):
        previous = result[index - 1]
        current = result[index]
        following = result[index + 1]
        if (
            previous.identity
            and current.identity
            and following.identity
            and identities_equal(previous.identity, following.identity)
            and not identities_equal(previous.identity, current.identity)
        ):
            result[index] = dataclasses.replace(
                current,
                identity=previous.identity,
                status="match",
            )
    return result


def brackets_from_recognitions(
    recognitions: Sequence[Recognition],
) -> list[ChangeBracket]:
    matched = [
        item
        for item in smooth_recognition_labels(recognitions)
        if item.identity is not None
    ]
    if len(matched) < 2:
        return []

    brackets: list[ChangeBracket] = []
    previous = matched[0]
    for current in matched[1:]:
        if not identities_equal(previous.identity, current.identity):
            assert previous.identity is not None
            assert current.identity is not None
            brackets.append(
                ChangeBracket(
                    left_anchor_seconds=previous.anchor_seconds,
                    right_anchor_seconds=current.anchor_seconds,
                    outgoing=previous.identity,
                    incoming=current.identity,
                    source="songrec",
                )
            )
        previous = current
    return merge_repeated_brackets(brackets)


def merge_repeated_brackets(
    brackets: Sequence[ChangeBracket],
) -> list[ChangeBracket]:
    """Merge repeated A->B brackets caused by alternating recognition in a blend."""

    result: list[ChangeBracket] = []
    for bracket in brackets:
        if (
            result
            and identities_equal(result[-1].outgoing, bracket.outgoing)
            and identities_equal(result[-1].incoming, bracket.incoming)
            and bracket.left_anchor_seconds - result[-1].right_anchor_seconds < 180
        ):
            previous = result[-1]
            result[-1] = dataclasses.replace(
                previous,
                right_anchor_seconds=max(
                    previous.right_anchor_seconds,
                    bracket.right_anchor_seconds,
                ),
            )
        else:
            result.append(bracket)
    return result


def brackets_from_cues(
    cues: Sequence[PublishedCue],
    duration: float,
    padding_seconds: float,
    *,
    include_non_track_cues: bool = False,
) -> list[ChangeBracket]:
    brackets: list[ChangeBracket] = []
    for index in range(1, len(cues)):
        previous = cues[index - 1]
        current = cues[index]
        if not include_non_track_cues and (
            is_non_track_cue(previous.label) or is_non_track_cue(current.label)
        ):
            continue
        following_start = (
            cues[index + 1].start_seconds if index + 1 < len(cues) else duration
        )
        previous_midpoint = (previous.start_seconds + current.start_seconds) / 2.0
        following_midpoint = (current.start_seconds + following_start) / 2.0

        left_anchor = max(
            previous_midpoint,
            current.start_seconds - padding_seconds,
        )
        right_anchor = min(
            following_midpoint,
            current.start_seconds + padding_seconds,
        )
        if right_anchor <= left_anchor:
            continue

        brackets.append(
            ChangeBracket(
                left_anchor_seconds=left_anchor,
                right_anchor_seconds=right_anchor,
                outgoing=identity_from_label(previous.label),
                incoming=identity_from_label(current.label),
                source=current.source,
                cue_seconds=current.start_seconds,
            )
        )
    return brackets


def refine_bracket(
    recognizer: SongRecRecognizer,
    bracket: ChangeBracket,
    *,
    minimum_interval_seconds: float,
    maximum_queries: int,
) -> ChangeBracket:
    """Binary-refine one identity change while all results remain A or B."""

    left = bracket.left_anchor_seconds
    right = bracket.right_anchor_seconds

    for _ in range(maximum_queries):
        if right - left <= minimum_interval_seconds:
            break

        midpoint = (left + right) / 2.0
        recognition = recognizer.recognize(midpoint)
        identity = recognition.identity
        if identity is None:
            break
        if identities_equal(identity, bracket.outgoing):
            left = recognition.anchor_seconds
        elif identities_equal(identity, bracket.incoming):
            right = recognition.anchor_seconds
        else:
            LOG.info(
                "Refinement found a third track at %.1fs: %s",
                midpoint,
                identity.label,
            )
            break

    return dataclasses.replace(
        bracket,
        left_anchor_seconds=left,
        right_anchor_seconds=right,
    )


def estimate_key(chroma_vector: np.ndarray) -> KeyEstimate:
    vector = np.asarray(chroma_vector, dtype=np.float64)
    if vector.shape != (12,) or not np.any(np.isfinite(vector)):
        return KeyEstimate("C", "major", "8B", 0.0)

    vector = np.nan_to_num(vector)
    vector = vector - vector.mean()
    vector_norm = np.linalg.norm(vector)
    if vector_norm < 1e-9:
        return KeyEstimate("C", "major", "8B", 0.0)

    scores: list[tuple[float, str, str]] = []
    for tonic, note in enumerate(NOTE_NAMES):
        for mode, profile in (("major", MAJOR_PROFILE), ("minor", MINOR_PROFILE)):
            template = np.roll(profile, tonic)
            template = template - template.mean()
            denominator = vector_norm * np.linalg.norm(template)
            score = float(np.dot(vector, template) / denominator)
            scores.append((score, note, mode))

    scores.sort(reverse=True, key=lambda item: item[0])
    best, second = scores[0], scores[1]
    confidence = max(0.0, min(1.0, (best[0] - second[0]) / 0.25))
    camelot = (
        CAMELOT_MAJOR[best[1]]
        if best[2] == "major"
        else CAMELOT_MINOR[best[1]]
    )
    return KeyEstimate(best[1], best[2], camelot, confidence)


def normalize_dance_bpm(bpm: float, low: float = 100.0, high: float = 190.0) -> float:
    """Correct common half-time or double-time tempo estimates."""

    if not math.isfinite(bpm) or bpm <= 0:
        return 0.0
    while bpm < low:
        bpm *= 2.0
    while bpm > high:
        bpm /= 2.0
    return bpm


def estimate_bpm(y: np.ndarray, sample_rate: int) -> float:
    """Estimate dance tempo on a beat-resolution grid, not the slow DSP grid."""

    try:
        tempo_hop_length = 512
        tempo_onset = librosa.onset.onset_strength(
            y=y,
            sr=sample_rate,
            hop_length=tempo_hop_length,
        )
        tempo_values = librosa.feature.tempo(
            onset_envelope=tempo_onset,
            sr=sample_rate,
            hop_length=tempo_hop_length,
            aggregate=np.median,
        )
        return normalize_dance_bpm(float(np.ravel(tempo_values)[0]))
    except Exception:
        return 0.0


def camelot_distance(left: str, right: str) -> float:
    match_left = re.fullmatch(r"(1[0-2]|[1-9])([AB])", left or "")
    match_right = re.fullmatch(r"(1[0-2]|[1-9])([AB])", right or "")
    if not match_left or not match_right:
        return 6.0

    left_number = int(match_left.group(1))
    right_number = int(match_right.group(1))
    number_distance = abs(left_number - right_number)
    number_distance = min(number_distance, 12 - number_distance)
    letter_penalty = 0.0 if match_left.group(2) == match_right.group(2) else 1.0
    return float(number_distance + letter_penalty)


def nearest_choice(value: float, choices: Sequence[int]) -> int:
    return min(choices, key=lambda choice: abs(choice - value))


def next_choice(value: float, choices: Sequence[int]) -> int:
    for choice in choices:
        if choice >= value:
            return choice
    return choices[-1]


def robust_scale(matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    center = np.nanmedian(matrix, axis=1, keepdims=True)
    deviation = np.nanmedian(np.abs(matrix - center), axis=1, keepdims=True)
    scale = np.where(deviation > 1e-8, deviation * 1.4826, 1.0)
    return center, scale


def sustained_crossing(
    values: np.ndarray,
    *,
    threshold: float,
    start_index: int,
    frames: int,
) -> int | None:
    last_start = len(values) - frames
    for index in range(max(0, start_index), max(0, last_start) + 1):
        window = values[index : index + frames]
        if window.size and np.mean(window >= threshold) >= 0.8:
            return index
    return None


def context_from_mask(
    *,
    mask: np.ndarray,
    onset: np.ndarray,
    rms_db: np.ndarray,
    low_ratio: np.ndarray,
    mid_ratio: np.ndarray,
    high_ratio: np.ndarray,
    centroid: np.ndarray,
    chroma: np.ndarray,
    sample_rate: int,
    hop_length: int,
    bpm_override: float | None = None,
) -> ContextFeatures:
    indices = np.flatnonzero(mask)
    if indices.size == 0:
        indices = np.arange(len(rms_db))

    chroma_vector = np.nanmedian(chroma[:, indices], axis=1)
    key = estimate_key(chroma_vector)

    if bpm_override is not None and bpm_override > 0:
        bpm = normalize_dance_bpm(bpm_override)
    else:
        onset_slice = onset[indices]
        try:
            tempo_values = librosa.feature.tempo(
                onset_envelope=onset_slice,
                sr=sample_rate,
                hop_length=hop_length,
                aggregate=np.median,
            )
            bpm = normalize_dance_bpm(float(np.ravel(tempo_values)[0]))
        except Exception:
            bpm = 0.0

    if not math.isfinite(bpm):
        bpm = 0.0

    return ContextFeatures(
        bpm=bpm,
        key=key.label,
        camelot=key.camelot,
        key_confidence=key.confidence,
        rms_db=float(np.nanmedian(rms_db[indices])),
        low_ratio=float(np.nanmedian(low_ratio[indices])),
        mid_ratio=float(np.nanmedian(mid_ratio[indices])),
        high_ratio=float(np.nanmedian(high_ratio[indices])),
        spectral_centroid_hz=float(np.nanmedian(centroid[indices])),
        onset_strength=float(np.nanmedian(onset[indices])),
    )


def analyze_transition(
    *,
    source_info: SourceInfo,
    duration_seconds: float,
    bracket: ChangeBracket,
    temporary_dir: Path,
    sample_rate: int,
    padding_seconds: float,
    reference_seconds: float,
    frame_seconds: float,
    curation: str,
    preference_weight: float,
    published_transition_bars: int,
    bpm_hint: float | None,
) -> TransitionExample:
    """Analyze one transition locally, without any further network requests."""

    region_start = max(0.0, bracket.left_anchor_seconds - padding_seconds)
    region_end = min(duration_seconds, bracket.right_anchor_seconds + padding_seconds)
    region_duration = region_end - region_start
    if region_duration < 10:
        raise DjLearnError("Transition analysis region is too short")

    segment_path = (
        temporary_dir
        / f"analysis-{round(region_start * 1000)}-{round(region_end * 1000)}.wav"
    )
    extract_audio(
        input_path=source_info.audio_path,
        output_path=segment_path,
        start_seconds=region_start,
        duration_seconds=region_duration,
        sample_rate=sample_rate,
    )

    try:
        y, sr = librosa.load(segment_path, sr=sample_rate, mono=True)
    finally:
        segment_path.unlink(missing_ok=True)

    if y.size < sr:
        raise DjLearnError("Decoded transition region contains too little audio")

    n_fft = 4096
    hop_length = max(512, round(sr * frame_seconds))
    magnitude = np.abs(
        librosa.stft(
            y,
            n_fft=n_fft,
            hop_length=hop_length,
            window="hann",
            center=True,
        )
    )
    power = magnitude**2
    frequencies = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    frame_count = magnitude.shape[1]
    local_times = librosa.frames_to_time(
        np.arange(frame_count),
        sr=sr,
        hop_length=hop_length,
    )
    global_times = region_start + local_times

    rms = librosa.feature.rms(
        S=magnitude,
        frame_length=n_fft,
        hop_length=hop_length,
    )[0]
    rms_db = librosa.amplitude_to_db(np.maximum(rms, 1e-10), ref=1.0)
    centroid = librosa.feature.spectral_centroid(S=magnitude, sr=sr)[0]
    chroma = librosa.feature.chroma_stft(
        S=power,
        sr=sr,
        n_fft=n_fft,
        hop_length=hop_length,
    )

    log_power = librosa.power_to_db(np.maximum(power, 1e-12), ref=np.max)
    onset = librosa.onset.onset_strength(
        S=log_power,
        sr=sr,
        hop_length=hop_length,
    )
    if onset.shape[0] != frame_count:
        onset = np.resize(onset, frame_count)

    region_bpm = estimate_bpm(y, sr)
    if bpm_hint is not None and (
        region_bpm <= 0 or abs(region_bpm / bpm_hint - 1.0) > 0.08
    ):
        region_bpm = bpm_hint

    mel = librosa.feature.melspectrogram(
        S=power,
        sr=sr,
        n_mels=40,
    )
    mfcc = librosa.feature.mfcc(
        S=librosa.power_to_db(np.maximum(mel, 1e-12), ref=np.max),
        n_mfcc=8,
    )

    total_power = np.sum(power, axis=0) + 1e-12
    low_power = np.sum(power[(frequencies >= 20) & (frequencies < 250)], axis=0)
    mid_power = np.sum(power[(frequencies >= 250) & (frequencies < 4_000)], axis=0)
    high_power = np.sum(power[frequencies >= 4_000], axis=0)
    low_ratio = low_power / total_power
    mid_ratio = mid_power / total_power
    high_ratio = high_power / total_power
    low_db = librosa.power_to_db(np.maximum(low_power, 1e-12), ref=1.0)

    left_mask = (
        np.abs(global_times - bracket.left_anchor_seconds)
        <= reference_seconds / 2.0
    )
    right_mask = (
        np.abs(global_times - bracket.right_anchor_seconds)
        <= reference_seconds / 2.0
    )
    if np.count_nonzero(left_mask) < 3:
        left_mask = global_times <= bracket.left_anchor_seconds
    if np.count_nonzero(right_mask) < 3:
        right_mask = global_times >= bracket.right_anchor_seconds

    outgoing_context = context_from_mask(
        mask=left_mask,
        onset=onset,
        rms_db=rms_db,
        low_ratio=low_ratio,
        mid_ratio=mid_ratio,
        high_ratio=high_ratio,
        centroid=centroid,
        chroma=chroma,
        sample_rate=sr,
        hop_length=hop_length,
        bpm_override=region_bpm,
    )
    incoming_context = context_from_mask(
        mask=right_mask,
        onset=onset,
        rms_db=rms_db,
        low_ratio=low_ratio,
        mid_ratio=mid_ratio,
        high_ratio=high_ratio,
        centroid=centroid,
        chroma=chroma,
        sample_rate=sr,
        hop_length=hop_length,
        bpm_override=region_bpm,
    )

    feature_matrix = np.vstack(
        [
            rms_db[np.newaxis, :] / 12.0,
            np.log1p(centroid)[np.newaxis, :],
            low_ratio[np.newaxis, :] * 4.0,
            mid_ratio[np.newaxis, :] * 2.0,
            high_ratio[np.newaxis, :] * 2.0,
            onset[np.newaxis, :],
            chroma * 1.5,
            mfcc / 20.0,
        ]
    )
    _, scale = robust_scale(feature_matrix)
    left_profile = np.nanmedian(feature_matrix[:, left_mask], axis=1, keepdims=True)
    right_profile = np.nanmedian(feature_matrix[:, right_mask], axis=1, keepdims=True)

    distance_left = np.sqrt(
        np.nanmean(((feature_matrix - left_profile) / scale) ** 2, axis=0)
    )
    distance_right = np.sqrt(
        np.nanmean(((feature_matrix - right_profile) / scale) ** 2, axis=0)
    )
    progress = distance_left / (distance_left + distance_right + 1e-9)

    frame_period = hop_length / sr
    smoothing_frames = max(3, round(5.0 / frame_period))
    if smoothing_frames % 2 == 0:
        smoothing_frames += 1
    progress = median_filter(progress, size=smoothing_frames, mode="nearest")
    progress = np.clip(progress, 0.0, 1.0)

    search_start = int(
        np.searchsorted(global_times, bracket.left_anchor_seconds, side="left")
    )
    sustained_frames = max(2, round(5.0 / frame_period))

    entry_index = sustained_crossing(
        progress,
        threshold=0.20,
        start_index=max(0, search_start - sustained_frames),
        frames=sustained_frames,
    )
    if entry_index is None:
        entry_index = int(
            np.argmin(np.abs(global_times - bracket.left_anchor_seconds))
        )

    midpoint_index = sustained_crossing(
        progress,
        threshold=0.50,
        start_index=entry_index,
        frames=sustained_frames,
    )
    if midpoint_index is None:
        midpoint_index = entry_index + int(
            np.argmin(np.abs(progress[entry_index:] - 0.5))
        )

    exit_index = sustained_crossing(
        progress,
        threshold=0.80,
        start_index=midpoint_index,
        frames=sustained_frames,
    )
    if exit_index is None:
        exit_index = int(
            np.argmin(np.abs(global_times - bracket.right_anchor_seconds))
        )

    midpoint_index = int(np.clip(midpoint_index, 0, frame_count - 2))
    exit_index = int(
        np.clip(max(exit_index, midpoint_index + 1), midpoint_index + 1, frame_count - 1)
    )
    entry_index = int(np.clip(min(entry_index, midpoint_index), 0, midpoint_index))
    transition_slice = slice(entry_index, exit_index + 1)

    smoothed_low_db = median_filter(low_db, size=smoothing_frames, mode="nearest")
    low_gradient = np.abs(np.gradient(smoothed_low_db))
    transition_indices = np.arange(entry_index, exit_index + 1)
    if transition_indices.size:
        midpoint_weight = np.exp(
            -(
                (transition_indices - midpoint_index)
                / max(1.0, transition_indices.size / 3.0)
            )
            ** 2
        )
        handoff_local = int(
            np.argmax(low_gradient[transition_slice] * midpoint_weight)
        )
        handoff_index = entry_index + handoff_local
    else:
        handoff_index = midpoint_index

    entry_time = float(global_times[entry_index])
    midpoint_time = float(global_times[midpoint_index])
    exit_time = float(global_times[exit_index])
    bass_handoff_time = float(global_times[handoff_index])
    overlap_seconds = max(frame_period, exit_time - entry_time)

    average_bpm = np.mean(
        [
            value
            for value in (outgoing_context.bpm, incoming_context.bpm)
            if value > 0
        ]
    )
    if not math.isfinite(average_bpm):
        average_bpm = 0.0

    timing_basis = "dsp-change-points"
    timing_confidence = 0.8
    boundary_pinned = (
        entry_time <= bracket.left_anchor_seconds + frame_period
        or exit_time >= bracket.right_anchor_seconds - frame_period
    )
    if bracket.cue_seconds is not None and boundary_pinned:
        timing_basis = f"published-cue-assumed-{published_transition_bars}-bars"
        timing_confidence = 0.4
        seconds_per_bar = (
            (60.0 / average_bpm) * 4.0
            if average_bpm > 0
            else 1.875
        )
        assumed_overlap = published_transition_bars * seconds_per_bar
        entry_time = max(region_start, bracket.cue_seconds - assumed_overlap / 2.0)
        exit_time = min(region_end, bracket.cue_seconds + assumed_overlap / 2.0)
        entry_index = int(np.argmin(np.abs(global_times - entry_time)))
        exit_index = int(np.argmin(np.abs(global_times - exit_time)))
        if not entry_time <= midpoint_time <= exit_time:
            midpoint_time = bracket.cue_seconds
            midpoint_index = int(np.argmin(np.abs(global_times - midpoint_time)))
        if not entry_time <= bass_handoff_time <= exit_time:
            bass_handoff_time = midpoint_time
            handoff_index = midpoint_index
        transition_slice = slice(entry_index, exit_index + 1)
        overlap_seconds = max(frame_period, exit_time - entry_time)

    if average_bpm > 0:
        raw_bars = overlap_seconds / ((60.0 / average_bpm) * 4.0)
    else:
        raw_bars = overlap_seconds / 1.875

    crossfade_bars = nearest_choice(raw_bars, COMMON_BAR_COUNTS)
    phrase_bars = next_choice(max(8.0, raw_bars - 0.25), PHRASE_BAR_COUNTS)

    bass_handoff_fraction = float(
        np.clip(
            (bass_handoff_time - entry_time) / max(overlap_seconds, 1e-9),
            0.0,
            1.0,
        )
    )

    left_low_db = float(np.nanmedian(low_db[left_mask]))
    right_low_db = float(np.nanmedian(low_db[right_mask]))
    transition_low_min = float(np.nanmin(smoothed_low_db[transition_slice]))
    endpoint_floor = min(left_low_db, right_low_db)
    low_dip_db = min(0.0, transition_low_min - endpoint_floor)
    estimated_bass_cut_db = float(np.clip(round(-low_dip_db, 1), 0.0, 18.0))

    entry_centroid = float(centroid[entry_index])
    handoff_centroid = float(centroid[handoff_index])
    filter_sweep_score = float(
        np.clip(
            (entry_centroid - handoff_centroid) / max(entry_centroid, 1.0),
            -1.0,
            1.0,
        )
    )
    filter_sweep = bool(
        overlap_seconds >= 20.0 and filter_sweep_score >= 0.15
    )

    midpoint_fraction = (midpoint_time - entry_time) / max(overlap_seconds, 1e-9)
    if midpoint_fraction < 0.40:
        fade_shape = "early"
    elif midpoint_fraction > 0.60:
        fade_shape = "late"
    else:
        fade_shape = "balanced"

    endpoint_separation = float(
        np.linalg.norm((left_profile - right_profile) / scale)
        / math.sqrt(feature_matrix.shape[0])
    )
    monotonicity = float(
        np.mean(np.diff(progress[entry_index : exit_index + 1]) >= -0.04)
    )
    analysis_confidence = float(
        np.clip(
            0.55 * min(1.0, endpoint_separation / 3.0)
            + 0.45 * monotonicity,
            0.0,
            1.0,
        )
    )

    transition_id = hashlib.sha256(
        "\x1f".join(
            (
                source_info.source_key,
                bracket.outgoing.track_key,
                bracket.incoming.track_key,
                f"{bracket.left_anchor_seconds:.1f}",
                f"{bracket.right_anchor_seconds:.1f}",
            )
        ).encode()
    ).hexdigest()[:32]

    return TransitionExample(
        schema_version=1,
        transition_id=transition_id,
        created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        source_key=source_info.source_key,
        source_title=source_info.title,
        source_path=str(source_info.audio_path),
        outgoing=dataclasses.asdict(bracket.outgoing),
        incoming=dataclasses.asdict(bracket.incoming),
        bracket_start_seconds=round(bracket.left_anchor_seconds, 3),
        bracket_end_seconds=round(bracket.right_anchor_seconds, 3),
        transition_entry_seconds=round(entry_time, 3),
        transition_midpoint_seconds=round(midpoint_time, 3),
        bass_handoff_seconds=round(bass_handoff_time, 3),
        transition_exit_seconds=round(exit_time, 3),
        overlap_seconds=round(overlap_seconds, 3),
        overlap_bars_raw=round(float(raw_bars), 3),
        crossfade_bars=crossfade_bars,
        phrase_bars=phrase_bars,
        bass_handoff_fraction=round(bass_handoff_fraction, 4),
        estimated_bass_cut_db=round(estimated_bass_cut_db, 1),
        filter_sweep_score=round(filter_sweep_score, 4),
        filter_sweep=filter_sweep,
        fade_shape=fade_shape,
        outgoing_context=dataclasses.asdict(outgoing_context),
        incoming_context=dataclasses.asdict(incoming_context),
        bpm_delta=round(incoming_context.bpm - outgoing_context.bpm, 3),
        camelot_distance=camelot_distance(
            outgoing_context.camelot,
            incoming_context.camelot,
        ),
        analysis_confidence=round(analysis_confidence, 4),
        bracket_source=bracket.source,
        curation=curation,
        preference_weight=round(preference_weight, 3),
        cue_timestamp_seconds=(
            round(bracket.cue_seconds, 3)
            if bracket.cue_seconds is not None
            else None
        ),
        timing_basis=timing_basis,
        timing_confidence=round(timing_confidence, 3),
    )


def transition_identifier(value: dict[str, Any]) -> str:
    """Return a stable identity used to prevent duplicate training examples."""

    explicit = value.get("transition_id")
    if isinstance(explicit, str) and explicit:
        return explicit

    outgoing = value.get("outgoing") or {}
    incoming = value.get("incoming") or {}
    components = (
        str(value.get("source_key") or ""),
        str(outgoing.get("track_key") or outgoing.get("title") or ""),
        str(incoming.get("track_key") or incoming.get("title") or ""),
        f"{float(value.get('bracket_start_seconds') or 0.0):.1f}",
        f"{float(value.get('bracket_end_seconds') or 0.0):.1f}",
    )
    return hashlib.sha256("\x1f".join(components).encode()).hexdigest()[:32]


def append_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> int:
    """Append only examples that are not already present in the dataset."""

    path.parent.mkdir(parents=True, exist_ok=True)
    existing_ids: set[str] = set()
    if path.is_file():
        for existing in load_jsonl(path):
            existing_ids.add(transition_identifier(existing))

    count = 0
    with path.open("a", encoding="utf-8") as stream:
        for value in values:
            identifier = transition_identifier(value)
            if identifier in existing_ids:
                continue
            value = {**value, "transition_id": identifier}
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True))
            stream.write("\n")
            existing_ids.add(identifier)
            count += 1
    return count


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise DjLearnError(f"Dataset does not exist: {path}")

    values: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DjLearnError(
                    f"Invalid JSON on {path}:{line_number}"
                ) from exc
            if isinstance(value, dict):
                values.append(value)
    return values


def context_for_track(
    *,
    path: Path,
    section: str,
    context_seconds: float,
    sample_rate: int,
    temporary_dir: Path,
) -> ContextFeatures:
    duration = probe_duration(path)
    actual_duration = min(context_seconds, duration)
    start = 0.0 if section == "intro" else max(0.0, duration - actual_duration)
    segment_path = temporary_dir / f"{safe_slug(path.stem)}-{section}.wav"

    extract_audio(
        input_path=path,
        output_path=segment_path,
        start_seconds=start,
        duration_seconds=actual_duration,
        sample_rate=sample_rate,
    )
    try:
        y, sr = librosa.load(segment_path, sr=sample_rate, mono=True)
    finally:
        segment_path.unlink(missing_ok=True)

    n_fft = 4096
    hop_length = max(512, round(sr * 0.5))
    magnitude = np.abs(
        librosa.stft(y, n_fft=n_fft, hop_length=hop_length, center=True)
    )
    power = magnitude**2
    frequencies = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    total = np.sum(power, axis=0) + 1e-12
    low = np.sum(power[(frequencies >= 20) & (frequencies < 250)], axis=0)
    mid = np.sum(power[(frequencies >= 250) & (frequencies < 4_000)], axis=0)
    high = np.sum(power[frequencies >= 4_000], axis=0)

    rms = librosa.feature.rms(
        S=magnitude,
        frame_length=n_fft,
        hop_length=hop_length,
    )[0]
    rms_db = librosa.amplitude_to_db(np.maximum(rms, 1e-10), ref=1.0)
    centroid = librosa.feature.spectral_centroid(S=magnitude, sr=sr)[0]
    chroma = librosa.feature.chroma_stft(
        S=power,
        sr=sr,
        n_fft=n_fft,
        hop_length=hop_length,
    )
    onset = librosa.onset.onset_strength(
        S=librosa.power_to_db(np.maximum(power, 1e-12), ref=np.max),
        sr=sr,
        hop_length=hop_length,
    )
    frame_count = magnitude.shape[1]
    if onset.shape[0] != frame_count:
        onset = np.resize(onset, frame_count)

    mask = np.ones(frame_count, dtype=bool)
    return context_from_mask(
        mask=mask,
        onset=onset,
        rms_db=rms_db,
        low_ratio=low / total,
        mid_ratio=mid / total,
        high_ratio=high / total,
        centroid=centroid,
        chroma=chroma,
        sample_rate=sr,
        hop_length=hop_length,
        bpm_override=estimate_bpm(y, sr),
    )


def weighted_median(values: Sequence[float], weights: Sequence[float]) -> float:
    if not values:
        raise ValueError("weighted_median requires at least one value")
    order = np.argsort(values)
    sorted_values = np.asarray(values, dtype=float)[order]
    sorted_weights = np.asarray(weights, dtype=float)[order]
    cumulative = np.cumsum(sorted_weights)
    threshold = cumulative[-1] / 2.0
    return float(sorted_values[np.searchsorted(cumulative, threshold)])


def weighted_mode(values: Sequence[str], weights: Sequence[float]) -> str:
    totals: dict[str, float] = {}
    for value, weight in zip(values, weights, strict=True):
        totals[value] = totals.get(value, 0.0) + float(weight)
    return max(totals, key=totals.get)


def balanced_neighbor_indices(
    examples: Sequence[dict[str, Any]],
    distances: np.ndarray,
    count: int,
    max_per_source: int,
) -> np.ndarray:
    """Select nearby examples without allowing one of several sets to dominate."""

    order = np.argsort(distances)
    source_keys = {str(example.get("source_key") or "") for example in examples}
    if max_per_source <= 0 or len(source_keys) <= 1:
        return order[:count]

    selected: list[int] = []
    source_counts: dict[str, int] = {}
    for raw_index in order:
        index = int(raw_index)
        source_key = str(examples[index].get("source_key") or "")
        if source_counts.get(source_key, 0) >= max_per_source:
            continue
        selected.append(index)
        source_counts[source_key] = source_counts.get(source_key, 0) + 1
        if len(selected) >= count:
            break
    return np.asarray(selected, dtype=int)


def example_preference_weight(example: dict[str, Any]) -> float:
    try:
        return float(
            np.clip(float(example.get("preference_weight", 1.0)), 0.1, 5.0)
        )
    except (TypeError, ValueError):
        return 1.0


def example_timing_weight(example: dict[str, Any]) -> float:
    try:
        return float(
            np.clip(float(example.get("timing_confidence", 1.0)), 0.1, 1.0)
        )
    except (TypeError, ValueError):
        return 1.0


def recommendation_vector(
    outgoing: ContextFeatures,
    incoming: ContextFeatures,
) -> np.ndarray:
    return np.asarray(
        [
            incoming.bpm - outgoing.bpm,
            camelot_distance(outgoing.camelot, incoming.camelot),
            outgoing.rms_db,
            incoming.rms_db,
            outgoing.low_ratio,
            incoming.low_ratio,
            math.log1p(outgoing.spectral_centroid_hz),
            math.log1p(incoming.spectral_centroid_hz),
            outgoing.onset_strength,
            incoming.onset_strength,
        ],
        dtype=np.float64,
    )


def example_vector(example: dict[str, Any]) -> np.ndarray | None:
    try:
        outgoing = example["outgoing_context"]
        incoming = example["incoming_context"]
        return np.asarray(
            [
                float(example["bpm_delta"]),
                float(example["camelot_distance"]),
                float(outgoing["rms_db"]),
                float(incoming["rms_db"]),
                float(outgoing["low_ratio"]),
                float(incoming["low_ratio"]),
                math.log1p(float(outgoing["spectral_centroid_hz"])),
                math.log1p(float(incoming["spectral_centroid_hz"])),
                float(outgoing["onset_strength"]),
                float(incoming["onset_strength"]),
            ],
            dtype=np.float64,
        )
    except (KeyError, TypeError, ValueError):
        return None


def recommend_transition(args: argparse.Namespace) -> int:
    require_commands(("ffmpeg", "ffprobe"))
    dataset = load_jsonl(args.dataset)
    if not dataset:
        raise DjLearnError("The transition dataset is empty")

    outgoing_path = args.outgoing.expanduser().resolve()
    incoming_path = args.incoming.expanduser().resolve()
    if not outgoing_path.is_file() or not incoming_path.is_file():
        raise DjLearnError("Both recommended-track inputs must be local files")

    with tempfile.TemporaryDirectory(prefix="djlearn-recommend-") as directory:
        temporary_dir = Path(directory)
        outgoing_context = context_for_track(
            path=outgoing_path,
            section="outro",
            context_seconds=args.context_seconds,
            sample_rate=args.analysis_sample_rate,
            temporary_dir=temporary_dir,
        )
        incoming_context = context_for_track(
            path=incoming_path,
            section="intro",
            context_seconds=args.context_seconds,
            sample_rate=args.analysis_sample_rate,
            temporary_dir=temporary_dir,
        )

    valid_examples: list[dict[str, Any]] = []
    vectors: list[np.ndarray] = []
    for example in dataset:
        vector = example_vector(example)
        if vector is not None and np.all(np.isfinite(vector)):
            valid_examples.append(example)
            vectors.append(vector)

    if not vectors:
        raise DjLearnError("Dataset contains no usable transition feature vectors")

    matrix = np.vstack(vectors)
    query = recommendation_vector(outgoing_context, incoming_context)
    center = np.nanmedian(matrix, axis=0)
    deviation = np.nanmedian(np.abs(matrix - center), axis=0) * 1.4826
    deviation = np.where(deviation > 1e-6, deviation, 1.0)

    feature_weights = np.asarray(
        [2.0, 2.0, 0.7, 0.7, 1.0, 1.0, 0.5, 0.5, 0.7, 0.7],
        dtype=np.float64,
    )
    distances = np.sqrt(
        np.sum(
            feature_weights
            * ((matrix - query[np.newaxis, :]) / deviation[np.newaxis, :]) ** 2,
            axis=1,
        )
    )
    neighbor_count = min(args.neighbors, len(valid_examples))
    indices = balanced_neighbor_indices(
        valid_examples,
        distances,
        neighbor_count,
        args.max_neighbors_per_source,
    )
    neighbor_weights = np.asarray(
        [
            example_preference_weight(valid_examples[index])
            * example_timing_weight(valid_examples[index])
            * max(
                0.1,
                float(valid_examples[index].get("analysis_confidence", 0.5)),
            )
            / (distances[index] + 0.20)
            for index in indices
        ],
        dtype=float,
    )

    phrase = round(
        weighted_median(
            [float(valid_examples[index]["phrase_bars"]) for index in indices],
            neighbor_weights,
        )
    )
    crossfade = round(
        weighted_median(
            [float(valid_examples[index]["crossfade_bars"]) for index in indices],
            neighbor_weights,
        )
    )
    bass_cut = round(
        weighted_median(
            [
                float(valid_examples[index]["estimated_bass_cut_db"])
                for index in indices
            ],
            neighbor_weights,
        )
    )
    handoff = weighted_median(
        [
            float(valid_examples[index]["bass_handoff_fraction"])
            for index in indices
        ],
        neighbor_weights,
    )
    filter_weight = sum(
        weight
        for index, weight in zip(indices, neighbor_weights, strict=True)
        if bool(valid_examples[index]["filter_sweep"])
    )
    filter_enabled = filter_weight >= float(np.sum(neighbor_weights)) / 2.0
    fade_shape = weighted_mode(
        [
            str(valid_examples[index].get("fade_shape", "balanced"))
            for index in indices
        ],
        neighbor_weights,
    )
    if fade_shape not in {"early", "balanced", "late"}:
        fade_shape = "balanced"

    phrase = nearest_choice(phrase, PHRASE_BAR_COUNTS)
    crossfade = nearest_choice(crossfade, COMMON_BAR_COUNTS)
    bass_cut = int(np.clip(bass_cut, 0, 18))
    directive = (
        f"-P{phrase}C{crossfade}B{bass_cut}F{int(filter_enabled)}"
        f"H{handoff:.2f}S{fade_shape[0].upper()}"
    )

    neighbors: list[dict[str, Any]] = []
    for index in indices:
        example = valid_examples[index]
        neighbors.append(
            {
                "distance": round(float(distances[index]), 4),
                "source_title": example.get("source_title"),
                "outgoing": example.get("outgoing"),
                "incoming": example.get("incoming"),
                "phrase_bars": example.get("phrase_bars"),
                "crossfade_bars": example.get("crossfade_bars"),
                "bass_handoff_fraction": example.get("bass_handoff_fraction"),
                "analysis_confidence": example.get("analysis_confidence"),
                "curation": example.get("curation", "unrated"),
                "preference_weight": example_preference_weight(example),
                "timing_basis": example.get("timing_basis", "legacy"),
                "timing_confidence": example_timing_weight(example),
            }
        )

    result = {
        "directive": directive,
        "phrase_bars": phrase,
        "crossfade_bars": crossfade,
        "bass_cut_db": -bass_cut,
        "filter_sweep": filter_enabled,
        "bass_handoff_fraction": round(handoff, 3),
        "fade_shape": fade_shape,
        "outgoing_context": dataclasses.asdict(outgoing_context),
        "incoming_context": dataclasses.asdict(incoming_context),
        "neighbors": neighbors,
    }
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


def analyze_set(args: argparse.Namespace) -> int:
    require_commands(("ffmpeg", "ffprobe"))

    work_dir = args.work_dir.expanduser().resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    source_info = resolve_source(args.source, work_dir)
    duration = probe_duration(source_info.audio_path)
    LOG.info("Source: %s", source_info.title)
    LOG.info("Audio:  %s", source_info.audio_path)
    LOG.info("Length: %.1f minutes", duration / 60.0)

    cues = discover_published_cues(source_info.metadata, duration)
    use_cues = (
        args.tracklist_mode != "ignore"
        and len(cues) >= args.minimum_published_cues
    )
    if args.tracklist_mode == "prefer" and not use_cues:
        raise DjLearnError(
            f"Only {len(cues)} published cues found; "
            f"{args.minimum_published_cues} required"
        )

    if not use_cues or args.verify_published_cues:
        songrec_command = shlex.split(args.songrec_command)
        if not songrec_command:
            raise DjLearnError("--songrec-command cannot be empty")
        require_commands((songrec_command[0],))

    cache = RecognitionCache(work_dir / "recognitions.sqlite3")
    budget = QueryBudget(args.max_queries)
    recognitions: list[Recognition] = []
    brackets: list[ChangeBracket] = []

    with tempfile.TemporaryDirectory(
        prefix="djlearn-",
        dir=work_dir,
    ) as directory:
        temporary_dir = Path(directory)

        recognizer: SongRecRecognizer | None = None
        if not use_cues or args.verify_published_cues:
            command = shlex.split(args.songrec_command)
            if not command:
                raise DjLearnError("--songrec-command cannot be empty")
            recognizer = SongRecRecognizer(
                audio_path=source_info.audio_path,
                duration_seconds=duration,
                source_key=source_info.source_key,
                cache=cache,
                temporary_dir=temporary_dir,
                command=command,
                window_seconds=args.recognition_window,
                minimum_request_interval=args.minimum_request_interval,
                budget=budget,
                timeout_seconds=args.songrec_timeout,
                maximum_retries=args.max_retries,
                retry_delay_seconds=args.retry_delay,
                maximum_consecutive_failures=args.maximum_consecutive_failures,
                maximum_consecutive_no_matches=args.maximum_consecutive_no_matches,
            )

        if use_cues:
            LOG.info(
                "Using %d published %s cues; no coarse SongRec scan needed",
                len(cues),
                cues[0].source,
            )
            brackets = brackets_from_cues(
                cues,
                duration,
                args.published_cue_padding,
                include_non_track_cues=args.include_non_track_cues,
            )
            if args.verify_published_cues and recognizer is not None:
                verified: list[ChangeBracket] = []
                for bracket in brackets:
                    try:
                        left = recognizer.recognize(bracket.left_anchor_seconds)
                        right = recognizer.recognize(bracket.right_anchor_seconds)
                    except (
                        QueryBudgetExhausted,
                        RecognitionCircuitOpen,
                        DjLearnError,
                    ) as exc:
                        LOG.warning("Cue verification stopped: %s", exc)
                        break

                    if left.identity and right.identity:
                        verified.append(
                            dataclasses.replace(
                                bracket,
                                outgoing=left.identity,
                                incoming=right.identity,
                                source=f"{bracket.source}+songrec",
                            )
                        )
                    else:
                        verified.append(bracket)
                if verified:
                    brackets = verified
        else:
            assert recognizer is not None
            anchors = sparse_anchor_times(
                duration,
                args.recognition_window,
                args.coarse_step,
            )
            LOG.info(
                "Sparse scan: %d possible samples at %.0f-second spacing",
                len(anchors),
                args.coarse_step,
            )
            for anchor in anchors:
                try:
                    recognitions.append(recognizer.recognize(anchor))
                except QueryBudgetExhausted as exc:
                    LOG.warning("%s", exc)
                    break
                except RecognitionCircuitOpen as exc:
                    LOG.warning("%s", exc)
                    break
                except DjLearnError as exc:
                    LOG.warning("Recognition failed at %.1fs: %s", anchor, exc)

            brackets = brackets_from_recognitions(recognitions)
            LOG.info("%d coarse track changes found", len(brackets))

            refined: list[ChangeBracket] = []
            for bracket in brackets:
                try:
                    refined.append(
                        refine_bracket(
                            recognizer,
                            bracket,
                            minimum_interval_seconds=args.minimum_refinement_interval,
                            maximum_queries=args.max_refinement_queries,
                        )
                    )
                except QueryBudgetExhausted as exc:
                    LOG.warning("%s", exc)
                    refined.append(bracket)
                    break
                except RecognitionCircuitOpen as exc:
                    LOG.warning("%s", exc)
                    refined.append(bracket)
                    break
                except DjLearnError as exc:
                    LOG.warning(
                        "Could not refine %s -> %s: %s",
                        bracket.outgoing.label,
                        bracket.incoming.label,
                        exc,
                    )
                    refined.append(bracket)
            brackets = refined

        transitions: list[TransitionExample] = []
        for number, bracket in enumerate(brackets, start=1):
            LOG.info(
                "Analyzing transition %d/%d: %s -> %s",
                number,
                len(brackets),
                bracket.outgoing.label,
                bracket.incoming.label,
            )
            try:
                transition = analyze_transition(
                    source_info=source_info,
                    duration_seconds=duration,
                    bracket=bracket,
                    temporary_dir=temporary_dir,
                    sample_rate=args.analysis_sample_rate,
                    padding_seconds=args.analysis_padding,
                    reference_seconds=args.reference_seconds,
                    frame_seconds=args.analysis_frame_seconds,
                    curation=args.curation,
                    preference_weight=args.preference_weight,
                    published_transition_bars=args.published_transition_bars,
                    bpm_hint=args.bpm_hint,
                )
            except DjLearnError as exc:
                LOG.warning("Transition analysis failed: %s", exc)
                continue
            transitions.append(transition)

    cache_stats = cache.stats()
    cache.close()

    report = {
        "schema_version": 1,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": {
            "source_key": source_info.source_key,
            "title": source_info.title,
            "audio_path": str(source_info.audio_path),
            "duration_seconds": duration,
            "bpm_hint": args.bpm_hint,
            "curation": args.curation,
            "preference_weight": args.preference_weight,
        },
        "published_cues": [dataclasses.asdict(cue) for cue in cues],
        "used_published_cues": use_cues,
        "recognitions": [
            {
                **dataclasses.asdict(recognition),
                "identity": (
                    dataclasses.asdict(recognition.identity)
                    if recognition.identity
                    else None
                ),
            }
            for recognition in recognitions
        ],
        "songrec_queries_used": budget.used,
        "recognition_cache_stats": cache_stats,
        "transitions": [
            dataclasses.asdict(transition) for transition in transitions
        ],
    }

    output_path = (
        args.output
        if args.output is not None
        else work_dir / f"{safe_slug(source_info.title)}-analysis.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    appended = 0
    if args.dataset and transitions:
        appended = append_jsonl(
            args.dataset,
            (dataclasses.asdict(transition) for transition in transitions),
        )

    LOG.info("Wrote report: %s", output_path)
    if args.dataset:
        LOG.info("Appended %d transitions to %s", appended, args.dataset)
    LOG.info(
        "SongRec network queries this run: %d; cache: %s",
        budget.used,
        cache_stats,
    )

    print(
        json.dumps(
            {
                "report": str(output_path),
                "dataset": str(args.dataset) if args.dataset else None,
                "transitions": len(transitions),
                "songrec_queries": budget.used,
                "used_published_cues": use_cues,
            },
            indent=2,
        )
    )
    return 0


def summarize_dataset(args: argparse.Namespace) -> int:
    examples = load_jsonl(args.dataset)
    if not examples:
        print("Dataset is empty")
        return 0

    overlaps = np.asarray(
        [float(example["overlap_seconds"]) for example in examples],
        dtype=float,
    )
    crossfades = [int(example["crossfade_bars"]) for example in examples]
    phrases = [int(example["phrase_bars"]) for example in examples]
    confidence = np.asarray(
        [float(example.get("analysis_confidence", 0.0)) for example in examples],
        dtype=float,
    )

    def counts(values: Sequence[int]) -> dict[str, int]:
        result: dict[str, int] = {}
        for value in values:
            key = str(value)
            result[key] = result.get(key, 0) + 1
        return dict(sorted(result.items(), key=lambda item: int(item[0])))

    summary = {
        "examples": len(examples),
        "sources": len({example.get("source_key") for example in examples}),
        "overlap_seconds": {
            "median": round(float(np.median(overlaps)), 2),
            "p25": round(float(np.percentile(overlaps, 25)), 2),
            "p75": round(float(np.percentile(overlaps, 75)), 2),
        },
        "crossfade_bars": counts(crossfades),
        "phrase_bars": counts(phrases),
        "filter_sweep_fraction": round(
            float(np.mean([bool(example["filter_sweep"]) for example in examples])),
            3,
        ),
        "median_analysis_confidence": round(float(np.median(confidence)), 3),
        "curation": {
            level: sum(
                example.get("curation", "unrated") == level
                for example in examples
            )
            for level in CURATION_WEIGHTS
        },
        "timing_basis": {
            basis: sum(
                example.get("timing_basis", "legacy") == basis
                for example in examples
            )
            for basis in sorted(
                {example.get("timing_basis", "legacy") for example in examples}
            )
        },
    }
    print(json.dumps(summary, indent=2))
    return 0


def show_cache_stats(args: argparse.Namespace) -> int:
    cache = RecognitionCache(args.cache)
    try:
        print(json.dumps(cache.stats(), indent=2))
    finally:
        cache.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Learn transition behavior from complete DJ sets with sparse, "
            "rate-limited SongRec recognition and local audio analysis."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )

    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    analyze = subparsers.add_parser(
        "analyze",
        help="Analyze one complete DJ set and append learned transitions",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    analyze.add_argument("source", help="Local audio file or yt-dlp-supported URL")
    analyze.add_argument(
        "--work-dir",
        type=Path,
        default=Path(".djlearn"),
        help="Downloads, cache, and reports",
    )
    analyze.add_argument(
        "--output",
        type=Path,
        help="Analysis report path; generated under --work-dir by default",
    )
    analyze.add_argument(
        "--dataset",
        type=Path,
        default=Path(".djlearn/transitions.jsonl"),
        help="Append learned transition examples here",
    )
    analyze.add_argument(
        "--tracklist-mode",
        choices=("auto", "prefer", "ignore"),
        default="auto",
        help=(
            "auto uses published cues when enough exist; prefer requires them; "
            "ignore always uses SongRec"
        ),
    )
    analyze.add_argument(
        "--minimum-published-cues",
        type=int,
        default=3,
    )
    analyze.add_argument(
        "--verify-published-cues",
        action="store_true",
        help="Spend two SongRec requests per published transition to verify labels",
    )
    analyze.add_argument(
        "--include-non-track-cues",
        action="store_true",
        help="Include intro/outro boundaries as ordinary training transitions",
    )
    analyze.add_argument(
        "--curation",
        choices=tuple(CURATION_WEIGHTS),
        default="unrated",
        help="How strongly this set represents the desired mixing style",
    )
    analyze.add_argument(
        "--preference-weight",
        type=float,
        help="Override the default weight implied by --curation",
    )
    analyze.add_argument(
        "--published-cue-padding",
        type=float,
        default=60.0,
        help="Pure-reference distance on either side of a published cue",
    )
    analyze.add_argument(
        "--published-transition-bars",
        type=int,
        choices=COMMON_BAR_COUNTS,
        default=32,
        help=(
            "Conservative overlap assumption when DSP change points pin to "
            "the published cue bracket"
        ),
    )
    analyze.add_argument(
        "--bpm-hint",
        type=float,
        help=(
            "Set-level BPM used when a blended region's estimate differs by "
            "more than 8 percent"
        ),
    )
    analyze.add_argument(
        "--coarse-step",
        type=float,
        default=120.0,
        help="Seconds between initial SongRec samples",
    )
    analyze.add_argument(
        "--recognition-window",
        type=float,
        default=30.0,
        help="Length of each SongRec sample",
    )
    analyze.add_argument(
        "--minimum-request-interval",
        type=float,
        default=12.0,
        help="Minimum seconds between uncached SongRec requests",
    )
    analyze.add_argument(
        "--max-queries",
        type=int,
        default=80,
        help="Hard SongRec request budget for one run",
    )
    analyze.add_argument(
        "--max-refinement-queries",
        type=int,
        default=3,
        help="Maximum extra requests around each detected identity change",
    )
    analyze.add_argument(
        "--minimum-refinement-interval",
        type=float,
        default=30.0,
        help="Stop binary refinement once a bracket is this narrow",
    )
    analyze.add_argument(
        "--songrec-command",
        default="songrec recognize -j",
        help="Command prefix; the temporary WAV path is appended",
    )
    analyze.add_argument(
        "--songrec-timeout",
        type=float,
        default=30,
    )
    analyze.add_argument(
        "--max-retries",
        type=int,
        default=0,
        help="Retries per failed network request; zero is safest for throttling",
    )
    analyze.add_argument(
        "--retry-delay",
        type=float,
        default=60.0,
    )
    analyze.add_argument(
        "--maximum-consecutive-failures",
        type=int,
        default=10,
        help="Open the circuit after this many command/network failures",
    )
    analyze.add_argument(
        "--maximum-consecutive-no-matches",
        type=int,
        default=10,
        help="Stop after this many no-matches following successful matches",
    )
    analyze.add_argument(
        "--analysis-padding",
        type=float,
        default=60.0,
        help="Local DSP padding outside each refined bracket",
    )
    analyze.add_argument(
        "--reference-seconds",
        type=float,
        default=20.0,
        help="Audio used to characterize each stable side",
    )
    analyze.add_argument(
        "--analysis-sample-rate",
        type=int,
        default=22_050,
    )
    analyze.add_argument(
        "--analysis-frame-seconds",
        type=float,
        default=0.5,
    )
    analyze.set_defaults(handler=analyze_set)

    recommend = subparsers.add_parser(
        "recommend",
        help="Recommend transition parameters from learned examples",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    recommend.add_argument("outgoing", type=Path)
    recommend.add_argument("incoming", type=Path)
    recommend.add_argument(
        "--dataset",
        type=Path,
        default=Path(".djlearn/transitions.jsonl"),
    )
    recommend.add_argument("--neighbors", type=int, default=7)
    recommend.add_argument(
        "--max-neighbors-per-source",
        type=int,
        default=2,
        help=(
            "Per-set neighbor cap once the dataset contains multiple sets; "
            "zero disables"
        ),
    )
    recommend.add_argument("--context-seconds", type=float, default=90.0)
    recommend.add_argument("--analysis-sample-rate", type=int, default=22_050)
    recommend.add_argument("--output", type=Path)
    recommend.set_defaults(handler=recommend_transition)

    summary = subparsers.add_parser(
        "summary",
        help="Summarize a learned transition dataset",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    summary.add_argument(
        "--dataset",
        type=Path,
        default=Path(".djlearn/transitions.jsonl"),
    )
    summary.set_defaults(handler=summarize_dataset)

    cache = subparsers.add_parser(
        "cache-stats",
        help="Show recognition-cache counts",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    cache.add_argument(
        "--cache",
        type=Path,
        default=Path(".djlearn/recognitions.sqlite3"),
    )
    cache.set_defaults(handler=show_cache_stats)

    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.subcommand == "analyze":
        if args.preference_weight is None:
            args.preference_weight = CURATION_WEIGHTS[args.curation]
        positive_options = {
            "--coarse-step": args.coarse_step,
            "--recognition-window": args.recognition_window,
            "--minimum-request-interval": args.minimum_request_interval,
            "--max-queries": args.max_queries,
            "--analysis-padding": args.analysis_padding,
            "--reference-seconds": args.reference_seconds,
            "--analysis-frame-seconds": args.analysis_frame_seconds,
        }
        invalid = [name for name, value in positive_options.items() if value <= 0]
        if invalid:
            raise DjLearnError(
                f"These options must be positive: {', '.join(invalid)}"
            )
        if args.max_refinement_queries < 0 or args.max_retries < 0:
            raise DjLearnError("Query and retry counts cannot be negative")
        if not 0.1 <= args.preference_weight <= 5.0:
            raise DjLearnError("--preference-weight must be between 0.1 and 5.0")
        if args.bpm_hint is not None and not 60.0 <= args.bpm_hint <= 220.0:
            raise DjLearnError("--bpm-hint must be between 60 and 220")
    elif args.subcommand == "recommend":
        if (
            args.neighbors <= 0
            or args.context_seconds <= 0
            or args.max_neighbors_per_source < 0
        ):
            raise DjLearnError(
                "--neighbors and --context-seconds must be positive; "
                "--max-neighbors-per-source cannot be negative"
            )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)

    try:
        validate_args(args)
        return int(args.handler(args))
    except KeyboardInterrupt:
        LOG.error("Interrupted; completed recognition results remain cached")
        return 130
    except DjLearnError as exc:
        LOG.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

