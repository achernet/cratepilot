#!/usr/bin/env python3
"""
Build trance DJ sets and choose the best next track from an M3U playlist.

The script is designed to work with transition examples produced by
``luminosity_transition_learner.py``. It can still operate without a learned
dataset by using tempo, Camelot-key, energy, timbre, and intro/outro heuristics.

Main commands
=============

Rank the best next track:

    djmix.py next "Current Track.mp3" --playlist trance.m3u

Create an automatically ordered playlist:

    djmix.py order trance.m3u --start "Opening Track.mp3" -o ordered.m3u

Render a playlist in its existing order:

    djmix.py render trance.m3u -o set.flac

Render and automatically reorder the playlist:

    djmix.py render trance.m3u --auto-order --start "Opening Track.mp3" -o set.flac

Render an explicit sequence with per-transition directives:

    djmix.py render -o set.flac \
        "Track 1.mp3" -P32C24B12F1H0.58SB \
        "Track 2.mp3" -P16C16B10F0H0.50SE \
        "Track 3.mp3"

Directive fields
================

P32     Phrase grid size in bars
C24     Audible overlap/crossfade in bars
B12     Maximum outgoing bass attenuation, expressed as a positive number
F1      Enable a smooth outgoing low-pass sweep (F0 disables it)
H0.58   Bass handoff position, from 0.0 to 1.0 through the overlap
SE      Fade shape: SE early, SB balanced, or SL late

An M3U file can contain a transition directive between two tracks:

    Track 1.mp3
    #DJMIX -P32C24B12F1H0.58SB
    Track 2.mp3

Dependencies
============

External:
    ffmpeg, ffprobe
    rubberband is optional and preferred for time stretching when installed.

Python:
    librosa, numpy, scipy, soundfile

The script targets Python 3.11 or newer.
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
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import librosa
import numpy as np
import soundfile as sf
from scipy.ndimage import median_filter
from scipy.signal import butter, sosfilt

LOG = logging.getLogger("djmix")
VERSION = "1.0.0"

DIRECTIVE_PATTERN = re.compile(
    r"^-?"
    r"(?:(?:P(?P<phrase>\d+)))?"
    r"(?:(?:C(?P<crossfade>\d+)))?"
    r"(?:(?:B(?P<bass>\d+(?:\.\d+)?)))?"
    r"(?:(?:F(?P<filter>[01])))?"
    r"(?:(?:H(?P<handoff>(?:0(?:\.\d+)?|1(?:\.0+)?))))?"
    r"(?:(?:S(?P<shape>[EBL])))?"
    r"$",
    flags=re.IGNORECASE,
)
DIRECTIVE_SENTINEL = "__DJMIX_DIRECTIVE__"

COMMON_BAR_COUNTS = (4, 8, 12, 16, 24, 32, 48, 64)
PHRASE_BAR_COUNTS = (8, 16, 24, 32, 48, 64)

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


class DjMixError(RuntimeError):
    """Base exception raised for expected command-line failures."""


@dataclass(frozen=True)
class KeyEstimate:
    """A key estimate in conventional and Camelot notation."""

    name: str
    mode: Literal["major", "minor"]
    camelot: str
    confidence: float

    @property
    def label(self) -> str:
        return f"{self.name} {self.mode}"


@dataclass(frozen=True)
class AudioContext:
    """Features describing an intro or outro section."""

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
    dynamic_range_db: float

    def to_vector(self) -> np.ndarray:
        return np.asarray(
            [
                self.bpm,
                self.rms_db,
                self.low_ratio,
                self.mid_ratio,
                self.high_ratio,
                math.log1p(max(self.spectral_centroid_hz, 0.0)),
                self.onset_strength,
                self.dynamic_range_db,
            ],
            dtype=np.float64,
        )


@dataclass(frozen=True)
class TrackAnalysis:
    """Cached analysis for one local track."""

    schema_version: int
    fingerprint: str
    path: str
    duration_seconds: float
    bpm: float
    key: str
    camelot: str
    key_confidence: float
    audible_start_seconds: float
    audible_end_seconds: float
    mix_in_seconds: float
    mix_out_seconds: float
    beat_times_seconds: tuple[float, ...]
    intro: AudioContext
    outro: AudioContext
    artist: str
    title: str

    @property
    def label(self) -> str:
        if self.artist:
            return f"{self.artist} - {self.title}"
        return self.title


@dataclass(frozen=True)
class TransitionSpec:
    """Instructions for one outgoing-to-incoming transition."""

    phrase_bars: int = 32
    crossfade_bars: int = 24
    bass_cut_db: float = 12.0
    filter_sweep: bool = False
    bass_handoff_fraction: float = 0.55
    fade_shape: Literal["early", "balanced", "late"] = "balanced"
    source: str = "default"

    @property
    def directive(self) -> str:
        bass = int(round(abs(self.bass_cut_db)))
        return (
            f"-P{self.phrase_bars}"
            f"C{self.crossfade_bars}"
            f"B{bass}"
            f"F{int(self.filter_sweep)}"
            f"H{self.bass_handoff_fraction:.2f}"
            f"S{self.fade_shape[0].upper()}"
        )


@dataclass(frozen=True)
class PlaylistEntry:
    """One playlist item and the transition directive preceding it."""

    path: Path
    transition_before: TransitionSpec | None = None


@dataclass(frozen=True)
class RankedCandidate:
    """One possible next track with score details."""

    path: Path
    label: str
    score: float
    bpm: float
    bpm_delta: float
    tempo_factor: float
    camelot: str
    camelot_distance: float
    learned_score: float
    heuristic_score: float
    artist_repeat_penalty: float
    recommended_transition: TransitionSpec


@dataclass(frozen=True)
class PreparedTrack:
    """A tempo-matched PCM file ready for set rendering."""

    source_path: Path
    pcm_path: Path
    analysis: TrackAnalysis
    tempo_factor: float
    sample_rate: int
    channels: int
    gain_linear: float
    audible_start_seconds: float
    audible_end_seconds: float
    mix_in_seconds: float
    mix_out_seconds: float


@dataclass
class RenderState:
    """Mutable cursor state for sequential set rendering."""

    current_start_seconds: float
    rendered_seconds: float = 0.0
    transitions: list[dict[str, Any]] = field(default_factory=list)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )


def run_command(
    command: Sequence[str],
    *,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess without invoking a shell."""

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
        raise DjMixError(f"Required command not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DjMixError(f"Command timed out: {shlex.join(command)}") from exc

    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise DjMixError(
            f"Command failed ({completed.returncode}): "
            f"{shlex.join(command)}\n{detail}"
        )
    return completed


def require_commands(commands: Iterable[str]) -> None:
    missing = [command for command in commands if shutil.which(command) is None]
    if missing:
        raise DjMixError(f"Missing required command(s): {', '.join(missing)}")


def preprocess_directive_arguments(argv: Sequence[str]) -> list[str]:
    """
    Hide compact transition directives from argparse.

    argparse treats ``-P32C24...`` as an option. Replacing the leading dash
    before parsing lets directives remain ordinary sequence tokens.
    """

    processed: list[str] = []
    for value in argv:
        if value.startswith("-") and DIRECTIVE_PATTERN.fullmatch(value):
            processed.append(DIRECTIVE_SENTINEL + value[1:])
        else:
            processed.append(value)
    return processed


def restore_directive(value: str) -> str:
    if value.startswith(DIRECTIVE_SENTINEL):
        return "-" + value[len(DIRECTIVE_SENTINEL) :]
    return value


def parse_transition_directive(
    value: str,
    defaults: TransitionSpec | None = None,
) -> TransitionSpec:
    """Parse ``-P32C24B12F1H0.58SE`` into a transition specification."""

    value = restore_directive(value).strip()
    match = DIRECTIVE_PATTERN.fullmatch(value)
    if match is None or not any(match.groupdict().values()):
        raise DjMixError(f"Invalid transition directive: {value}")

    base = defaults or TransitionSpec()
    phrase = int(match.group("phrase") or base.phrase_bars)
    crossfade = int(match.group("crossfade") or base.crossfade_bars)
    bass = float(match.group("bass") or abs(base.bass_cut_db))
    filter_sweep = (
        bool(int(match.group("filter")))
        if match.group("filter") is not None
        else base.filter_sweep
    )
    handoff = float(match.group("handoff") or base.bass_handoff_fraction)
    shape_codes = {"E": "early", "B": "balanced", "L": "late"}
    shape = (
        shape_codes[match.group("shape").upper()]
        if match.group("shape") is not None
        else base.fade_shape
    )

    if phrase <= 0 or crossfade <= 0:
        raise DjMixError("Phrase and crossfade bars must be positive")
    if not 0.0 <= handoff <= 1.0:
        raise DjMixError("Bass handoff must be between 0.0 and 1.0")

    return TransitionSpec(
        phrase_bars=phrase,
        crossfade_bars=crossfade,
        bass_cut_db=bass,
        filter_sweep=filter_sweep,
        bass_handoff_fraction=handoff,
        fade_shape=shape,
        source="directive",
    )


def normalize_text(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}", " ", value)
    value = re.sub(r"\bthe\b", " ", value)
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def artist_and_title(path: Path) -> tuple[str, str]:
    stem = path.stem
    stem = re.sub(r"\s*\(\d{4}\)\s*$", "", stem)
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        return artist.strip(), title.strip()
    return "", stem.strip()


def file_fingerprint(path: Path) -> str:
    """Hash file metadata plus the first and last MiB."""

    stat = path.stat()
    digest = hashlib.sha256()
    digest.update(str(stat.st_size).encode("ascii"))
    digest.update(str(stat.st_mtime_ns).encode("ascii"))
    with path.open("rb") as stream:
        digest.update(stream.read(1024 * 1024))
        if stat.st_size > 1024 * 1024:
            stream.seek(max(0, stat.st_size - 1024 * 1024))
            digest.update(stream.read(1024 * 1024))
    return digest.hexdigest()


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
        raise DjMixError(f"Could not determine duration of {path}") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise DjMixError(f"Invalid duration for {path}: {duration}")
    return duration


def decode_mono(
    input_path: Path,
    output_path: Path,
    *,
    sample_rate: int,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(input_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_f32le",
            "-y",
            str(output_path),
        ]
    )


def normalize_dance_bpm(
    bpm: float,
    low: float = 100.0,
    high: float = 190.0,
) -> float:
    """Correct common half-time and double-time estimates."""

    if not math.isfinite(bpm) or bpm <= 0:
        return 0.0
    while bpm < low:
        bpm *= 2.0
    while bpm > high:
        bpm /= 2.0
    return bpm


def estimate_key(chroma_vector: np.ndarray) -> KeyEstimate:
    vector = np.asarray(chroma_vector, dtype=np.float64)
    vector = np.nan_to_num(vector)
    if vector.shape != (12,) or np.linalg.norm(vector) < 1e-8:
        return KeyEstimate("C", "major", "8B", 0.0)

    vector = vector - vector.mean()
    scores: list[tuple[float, str, Literal["major", "minor"]]] = []
    for tonic, note in enumerate(NOTE_NAMES):
        for mode, profile in (
            ("major", MAJOR_PROFILE),
            ("minor", MINOR_PROFILE),
        ):
            template = np.roll(profile, tonic)
            template = template - template.mean()
            denominator = np.linalg.norm(vector) * np.linalg.norm(template)
            score = float(np.dot(vector, template) / max(denominator, 1e-9))
            scores.append((score, note, mode))

    scores.sort(key=lambda item: item[0], reverse=True)
    best, second = scores[0], scores[1]
    confidence = float(np.clip((best[0] - second[0]) / 0.25, 0.0, 1.0))
    camelot = (
        CAMELOT_MAJOR[best[1]]
        if best[2] == "major"
        else CAMELOT_MINOR[best[1]]
    )
    return KeyEstimate(best[1], best[2], camelot, confidence)


def camelot_distance(left: str, right: str) -> float:
    left_match = re.fullmatch(r"(1[0-2]|[1-9])([AB])", left or "")
    right_match = re.fullmatch(r"(1[0-2]|[1-9])([AB])", right or "")
    if left_match is None or right_match is None:
        return 6.0

    left_number = int(left_match.group(1))
    right_number = int(right_match.group(1))
    number_distance = abs(left_number - right_number)
    number_distance = min(number_distance, 12 - number_distance)
    mode_penalty = 0.0 if left_match.group(2) == right_match.group(2) else 1.0
    return float(number_distance + mode_penalty)


def nearest_beat(
    beat_times: np.ndarray,
    target: float,
    *,
    direction: Literal["before", "after", "nearest"],
) -> float:
    if beat_times.size == 0:
        return target

    index = int(np.searchsorted(beat_times, target))
    if direction == "after":
        return float(beat_times[min(index, beat_times.size - 1)])
    if direction == "before":
        return float(beat_times[max(0, index - 1)])

    candidates = []
    if index < beat_times.size:
        candidates.append(float(beat_times[index]))
    if index > 0:
        candidates.append(float(beat_times[index - 1]))
    return min(candidates, key=lambda value: abs(value - target))


def context_features(
    *,
    y: np.ndarray,
    sample_rate: int,
    start_seconds: float,
    end_seconds: float,
    bpm: float,
    hop_length: int,
) -> AudioContext:
    start_sample = max(0, round(start_seconds * sample_rate))
    end_sample = min(y.size, round(end_seconds * sample_rate))
    segment = y[start_sample:end_sample]
    if segment.size < sample_rate:
        segment = y

    n_fft = 4096
    magnitude = np.abs(
        librosa.stft(
            segment,
            n_fft=n_fft,
            hop_length=hop_length,
            center=True,
        )
    )
    power = magnitude**2
    frequencies = librosa.fft_frequencies(sr=sample_rate, n_fft=n_fft)
    total_power = np.sum(power, axis=0) + 1e-12

    low = np.sum(power[(frequencies >= 20) & (frequencies < 250)], axis=0)
    mid = np.sum(power[(frequencies >= 250) & (frequencies < 4_000)], axis=0)
    high = np.sum(power[frequencies >= 4_000], axis=0)

    rms = librosa.feature.rms(
        S=magnitude,
        frame_length=n_fft,
        hop_length=hop_length,
    )[0]
    rms_db_frames = librosa.amplitude_to_db(np.maximum(rms, 1e-10), ref=1.0)
    centroid = librosa.feature.spectral_centroid(
        S=magnitude,
        sr=sample_rate,
    )[0]
    onset = librosa.onset.onset_strength(
        S=librosa.power_to_db(np.maximum(power, 1e-12), ref=np.max),
        sr=sample_rate,
        hop_length=hop_length,
    )
    chroma = librosa.feature.chroma_cqt(
        y=segment,
        sr=sample_rate,
        hop_length=hop_length,
    )
    key = estimate_key(np.nanmedian(chroma, axis=1))

    rms_db = float(np.nanmedian(rms_db_frames))
    dynamic_range = float(
        np.nanpercentile(rms_db_frames, 90)
        - np.nanpercentile(rms_db_frames, 10)
    )
    return AudioContext(
        bpm=bpm,
        key=key.label,
        camelot=key.camelot,
        key_confidence=key.confidence,
        rms_db=rms_db,
        low_ratio=float(np.nanmedian(low / total_power)),
        mid_ratio=float(np.nanmedian(mid / total_power)),
        high_ratio=float(np.nanmedian(high / total_power)),
        spectral_centroid_hz=float(np.nanmedian(centroid)),
        onset_strength=float(np.nanmedian(onset)),
        dynamic_range_db=dynamic_range,
    )


class AnalysisCache:
    """JSON cache keyed by a content fingerprint."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, fingerprint: str) -> Path:
        return self.directory / f"{fingerprint}.json"

    def get(self, fingerprint: str) -> TrackAnalysis | None:
        path = self.path_for(fingerprint)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return track_analysis_from_dict(payload)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            LOG.warning("Ignoring invalid analysis cache entry: %s", path)
            return None

    def put(self, analysis: TrackAnalysis) -> None:
        target = self.path_for(analysis.fingerprint)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                dataclasses.asdict(analysis),
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        temporary.replace(target)


def audio_context_from_dict(value: dict[str, Any]) -> AudioContext:
    return AudioContext(**value)


def track_analysis_from_dict(value: dict[str, Any]) -> TrackAnalysis:
    return TrackAnalysis(
        schema_version=int(value["schema_version"]),
        fingerprint=str(value["fingerprint"]),
        path=str(value["path"]),
        duration_seconds=float(value["duration_seconds"]),
        bpm=float(value["bpm"]),
        key=str(value["key"]),
        camelot=str(value["camelot"]),
        key_confidence=float(value["key_confidence"]),
        audible_start_seconds=float(value["audible_start_seconds"]),
        audible_end_seconds=float(value["audible_end_seconds"]),
        mix_in_seconds=float(value["mix_in_seconds"]),
        mix_out_seconds=float(value["mix_out_seconds"]),
        beat_times_seconds=tuple(float(item) for item in value["beat_times_seconds"]),
        intro=audio_context_from_dict(value["intro"]),
        outro=audio_context_from_dict(value["outro"]),
        artist=str(value["artist"]),
        title=str(value["title"]),
    )


def analyze_track(
    path: Path,
    *,
    cache: AnalysisCache,
    temporary_dir: Path,
    sample_rate: int,
    context_seconds: float,
    silence_top_db: float,
) -> TrackAnalysis:
    """Analyze a complete local track and cache the result."""

    path = path.expanduser().resolve()
    if not path.is_file():
        raise DjMixError(f"Track does not exist: {path}")

    fingerprint = file_fingerprint(path)
    cached = cache.get(fingerprint)
    if cached is not None:
        LOG.info(
            "Analysis cache: %-6.2f BPM %-3s %s",
            cached.bpm,
            cached.camelot,
            cached.label,
        )
        return cached

    LOG.info("Analyzing: %s", path.name)
    decoded = temporary_dir / f"analyze-{fingerprint[:16]}.wav"
    decode_mono(path, decoded, sample_rate=sample_rate)
    try:
        y, sr = librosa.load(decoded, sr=sample_rate, mono=True)
    finally:
        decoded.unlink(missing_ok=True)

    if y.size < sr:
        raise DjMixError(f"Track is too short to analyze: {path}")

    duration = y.size / sr
    hop_length = 512

    onset = librosa.onset.onset_strength(
        y=y,
        sr=sr,
        hop_length=hop_length,
    )
    tempo_result, beat_frames = librosa.beat.beat_track(
        onset_envelope=onset,
        sr=sr,
        hop_length=hop_length,
        units="frames",
        sparse=True,
    )
    bpm = normalize_dance_bpm(float(np.ravel(tempo_result)[0]))
    beat_times = librosa.frames_to_time(
        np.asarray(beat_frames),
        sr=sr,
        hop_length=hop_length,
    )

    non_silent_intervals = librosa.effects.split(
        y,
        top_db=silence_top_db,
        frame_length=2048,
        hop_length=hop_length,
    )
    if non_silent_intervals.size:
        audible_start = float(non_silent_intervals[0, 0] / sr)
        audible_end = float(non_silent_intervals[-1, 1] / sr)
    else:
        audible_start = 0.0
        audible_end = duration

    if beat_times.size:
        stable_after = audible_start + min(8.0, max(1.0, context_seconds / 8.0))
        stable_before = audible_end - min(8.0, max(1.0, context_seconds / 8.0))
        mix_in = nearest_beat(beat_times, stable_after, direction="after")
        mix_out = nearest_beat(beat_times, stable_before, direction="before")
    else:
        mix_in = audible_start
        mix_out = audible_end

    if mix_out <= mix_in + 20.0:
        mix_in = audible_start
        mix_out = audible_end

    intro_start = mix_in
    intro_end = min(mix_out, mix_in + context_seconds)
    outro_start = max(mix_in, mix_out - context_seconds)
    outro_end = mix_out

    intro = context_features(
        y=y,
        sample_rate=sr,
        start_seconds=intro_start,
        end_seconds=intro_end,
        bpm=bpm,
        hop_length=hop_length,
    )
    outro = context_features(
        y=y,
        sample_rate=sr,
        start_seconds=outro_start,
        end_seconds=outro_end,
        bpm=bpm,
        hop_length=hop_length,
    )

    full_chroma = librosa.feature.chroma_cqt(
        y=y,
        sr=sr,
        hop_length=hop_length,
    )
    key = estimate_key(np.nanmedian(full_chroma, axis=1))
    artist, title = artist_and_title(path)

    analysis = TrackAnalysis(
        schema_version=1,
        fingerprint=fingerprint,
        path=str(path),
        duration_seconds=duration,
        bpm=bpm,
        key=key.label,
        camelot=key.camelot,
        key_confidence=key.confidence,
        audible_start_seconds=audible_start,
        audible_end_seconds=audible_end,
        mix_in_seconds=mix_in,
        mix_out_seconds=mix_out,
        beat_times_seconds=tuple(float(value) for value in beat_times),
        intro=intro,
        outro=outro,
        artist=artist,
        title=title,
    )
    cache.put(analysis)
    LOG.info(
        "Analyzed:       %-6.2f BPM %-3s %s",
        analysis.bpm,
        analysis.camelot,
        analysis.label,
    )
    return analysis


def load_jsonl(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.is_file():
        return []

    values: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DjMixError(
                    f"Invalid JSON in {path}:{line_number}"
                ) from exc
            if isinstance(value, dict):
                values.append(value)
    return values


def learned_query_vector(
    outgoing: AudioContext,
    incoming: AudioContext,
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
            outgoing.dynamic_range_db,
            incoming.dynamic_range_db,
        ],
        dtype=np.float64,
    )


def learned_example_vector(example: dict[str, Any]) -> np.ndarray | None:
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
                float(outgoing.get("dynamic_range_db", 6.0)),
                float(incoming.get("dynamic_range_db", 6.0)),
            ],
            dtype=np.float64,
        )
    except (KeyError, TypeError, ValueError):
        return None


def robust_distance(
    query: np.ndarray,
    matrix: np.ndarray,
    feature_weights: np.ndarray,
) -> np.ndarray:
    center = np.nanmedian(matrix, axis=0)
    deviation = np.nanmedian(np.abs(matrix - center), axis=0) * 1.4826
    deviation = np.where(deviation > 1e-6, deviation, 1.0)
    return np.sqrt(
        np.sum(
            feature_weights
            * ((matrix - query[np.newaxis, :]) / deviation[np.newaxis, :]) ** 2,
            axis=1,
        )
    )


def weighted_median(values: Sequence[float], weights: Sequence[float]) -> float:
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


def nearest_choice(value: float, choices: Sequence[int]) -> int:
    return min(choices, key=lambda choice: abs(choice - value))


class TransitionModel:
    """Nearest-example transition policy with a heuristic fallback."""

    def __init__(
        self,
        examples: Sequence[dict[str, Any]],
        *,
        neighbors: int = 7,
        max_neighbors_per_source: int = 2,
        defaults: TransitionSpec | None = None,
    ) -> None:
        self.defaults = defaults or TransitionSpec()
        self.neighbors = max(1, neighbors)
        self.max_neighbors_per_source = max(0, max_neighbors_per_source)
        self.examples: list[dict[str, Any]] = []
        vectors: list[np.ndarray] = []
        for example in examples:
            vector = learned_example_vector(example)
            if vector is not None and np.all(np.isfinite(vector)):
                self.examples.append(example)
                vectors.append(vector)
        self.matrix = np.vstack(vectors) if vectors else None
        self.feature_weights = np.asarray(
            [2.0, 2.2, 0.7, 0.7, 1.1, 1.1, 0.5, 0.5, 0.8, 0.8, 0.4, 0.4],
            dtype=np.float64,
        )

    @property
    def available(self) -> bool:
        return self.matrix is not None and bool(self.examples)

    def neighbors_for(
        self,
        outgoing: AudioContext,
        incoming: AudioContext,
    ) -> tuple[np.ndarray, np.ndarray]:
        if self.matrix is None:
            return np.asarray([], dtype=int), np.asarray([], dtype=float)

        query = learned_query_vector(outgoing, incoming)
        distances = robust_distance(query, self.matrix, self.feature_weights)
        count = min(self.neighbors, len(self.examples))
        indices = balanced_neighbor_indices(
            self.examples,
            distances,
            count,
            self.max_neighbors_per_source,
        )
        return indices, distances[indices]

    def learned_similarity(
        self,
        outgoing: AudioContext,
        incoming: AudioContext,
    ) -> float:
        indices, distances = self.neighbors_for(outgoing, incoming)
        if indices.size == 0:
            return 0.5
        weighted = np.average(
            np.exp(-distances),
            weights=np.asarray(
                [
                    max(
                        0.05,
                        float(
                            self.examples[index].get("analysis_confidence", 0.5)
                        ),
                    )
                    * example_preference_weight(self.examples[index])
                    for index in indices
                ],
                dtype=float,
            ),
        )
        return float(np.clip(weighted, 0.0, 1.0))

    def recommend(
        self,
        outgoing: AudioContext,
        incoming: AudioContext,
    ) -> TransitionSpec:
        indices, distances = self.neighbors_for(outgoing, incoming)
        if indices.size == 0:
            return self.heuristic_recommendation(outgoing, incoming)

        distance_weights = 1.0 / (distances + 0.20)
        example_weights = np.asarray(
            [
                max(
                    0.1,
                    float(
                        self.examples[index].get(
                            "analysis_confidence",
                            0.5,
                        )
                    ),
                )
                * example_preference_weight(self.examples[index])
                * example_timing_weight(self.examples[index])
                for index in indices
            ],
            dtype=float,
        )
        weights = distance_weights * example_weights

        phrase = nearest_choice(
            weighted_median(
                [
                    float(self.examples[index].get("phrase_bars", 32))
                    for index in indices
                ],
                weights,
            ),
            PHRASE_BAR_COUNTS,
        )
        crossfade = nearest_choice(
            weighted_median(
                [
                    float(self.examples[index].get("crossfade_bars", 24))
                    for index in indices
                ],
                weights,
            ),
            COMMON_BAR_COUNTS,
        )
        bass = float(
            np.clip(
                weighted_median(
                    [
                        float(
                            self.examples[index].get(
                                "estimated_bass_cut_db",
                                12.0,
                            )
                        )
                        for index in indices
                    ],
                    weights,
                ),
                0.0,
                18.0,
            )
        )
        handoff = float(
            np.clip(
                weighted_median(
                    [
                        float(
                            self.examples[index].get(
                                "bass_handoff_fraction",
                                0.55,
                            )
                        )
                        for index in indices
                    ],
                    weights,
                ),
                0.0,
                1.0,
            )
        )
        filter_weight = sum(
            weight
            for index, weight in zip(indices, weights, strict=True)
            if bool(self.examples[index].get("filter_sweep", False))
        )
        filter_sweep = filter_weight >= float(np.sum(weights)) / 2.0
        fade_shape = weighted_mode(
            [
                str(self.examples[index].get("fade_shape", "balanced"))
                for index in indices
            ],
            weights,
        )
        if fade_shape not in {"early", "balanced", "late"}:
            fade_shape = "balanced"

        return TransitionSpec(
            phrase_bars=phrase,
            crossfade_bars=min(crossfade, phrase),
            bass_cut_db=bass,
            filter_sweep=filter_sweep,
            bass_handoff_fraction=handoff,
            fade_shape=fade_shape,
            source="learned",
        )

    def heuristic_recommendation(
        self,
        outgoing: AudioContext,
        incoming: AudioContext,
    ) -> TransitionSpec:
        key_distance = camelot_distance(outgoing.camelot, incoming.camelot)
        energy_delta = incoming.rms_db - outgoing.rms_db
        busy = outgoing.onset_strength + incoming.onset_strength

        if key_distance <= 1.0 and busy < 3.0:
            phrase = 32
            crossfade = 24
        elif key_distance <= 2.0:
            phrase = 32
            crossfade = 16
        else:
            phrase = 16
            crossfade = 8

        bass = 14.0 if outgoing.low_ratio + incoming.low_ratio > 0.45 else 10.0
        filter_sweep = bool(
            crossfade >= 16
            and (
                key_distance >= 2.0
                or incoming.spectral_centroid_hz
                < outgoing.spectral_centroid_hz * 0.80
            )
        )
        handoff = 0.52 if energy_delta >= 0 else 0.60

        return TransitionSpec(
            phrase_bars=phrase,
            crossfade_bars=crossfade,
            bass_cut_db=bass,
            filter_sweep=filter_sweep,
            bass_handoff_fraction=handoff,
            source="heuristic",
        )


def gaussian_score(value: float, sigma: float) -> float:
    return math.exp(-0.5 * (value / sigma) ** 2)


def tempo_factor_for(source_bpm: float, target_bpm: float) -> float:
    if source_bpm <= 0 or target_bpm <= 0:
        return 1.0
    return target_bpm / source_bpm


def score_candidate(
    current: TrackAnalysis,
    candidate: TrackAnalysis,
    model: TransitionModel,
    *,
    maximum_tempo_change: float,
    same_artist_penalty: float,
) -> RankedCandidate:
    bpm_delta = candidate.bpm - current.bpm
    factor = tempo_factor_for(candidate.bpm, current.bpm)
    tempo_change = abs(factor - 1.0)
    if tempo_change > maximum_tempo_change:
        tempo_score = 0.0
    else:
        tempo_score = gaussian_score(bpm_delta, sigma=3.5)

    key_distance = camelot_distance(
        current.outro.camelot,
        candidate.intro.camelot,
    )
    key_score = math.exp(-0.72 * key_distance)

    rms_delta = candidate.intro.rms_db - current.outro.rms_db
    energy_score = gaussian_score(rms_delta, sigma=4.5)

    low_delta = candidate.intro.low_ratio - current.outro.low_ratio
    low_score = gaussian_score(low_delta, sigma=0.15)

    centroid_ratio = math.log(
        max(candidate.intro.spectral_centroid_hz, 1.0)
        / max(current.outro.spectral_centroid_hz, 1.0)
    )
    timbre_score = gaussian_score(centroid_ratio, sigma=0.50)

    onset_ratio = math.log(
        max(candidate.intro.onset_strength, 1e-4)
        / max(current.outro.onset_strength, 1e-4)
    )
    rhythm_score = gaussian_score(onset_ratio, sigma=0.80)

    learned_score = model.learned_similarity(
        current.outro,
        candidate.intro,
    )
    heuristic_score = (
        0.28 * tempo_score
        + 0.24 * key_score
        + 0.15 * energy_score
        + 0.10 * low_score
        + 0.08 * timbre_score
        + 0.07 * rhythm_score
        + 0.08 * candidate.intro.key_confidence
    )

    artist_penalty = 0.0
    if (
        current.artist
        and candidate.artist
        and normalize_text(current.artist) == normalize_text(candidate.artist)
    ):
        artist_penalty = same_artist_penalty

    score = (
        0.62 * heuristic_score
        + 0.38 * learned_score
        - artist_penalty
    )
    if tempo_change > maximum_tempo_change:
        score -= 0.5 + tempo_change

    recommendation = model.recommend(current.outro, candidate.intro)
    return RankedCandidate(
        path=Path(candidate.path),
        label=candidate.label,
        score=float(score),
        bpm=candidate.bpm,
        bpm_delta=bpm_delta,
        tempo_factor=factor,
        camelot=candidate.intro.camelot,
        camelot_distance=key_distance,
        learned_score=learned_score,
        heuristic_score=heuristic_score,
        artist_repeat_penalty=artist_penalty,
        recommended_transition=recommendation,
    )


def parse_m3u(path: Path, defaults: TransitionSpec) -> list[PlaylistEntry]:
    """Parse local paths and optional ``#DJMIX`` transition comments."""

    path = path.expanduser().resolve()
    if not path.is_file():
        raise DjMixError(f"Playlist does not exist: {path}")

    entries: list[PlaylistEntry] = []
    pending_directive: TransitionSpec | None = None
    for raw_line in path.read_text(
        encoding="utf-8-sig",
        errors="replace",
    ).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.upper().startswith("#DJMIX"):
            value = line[len("#DJMIX") :].strip()
            pending_directive = parse_transition_directive(
                value,
                defaults=defaults,
            )
            continue
        if line.startswith("#"):
            continue
        if re.match(r"^[a-z]+://", line, flags=re.IGNORECASE):
            raise DjMixError(
                f"Remote playlist entries are not supported: {line}"
            )

        track_path = Path(os.path.expandvars(os.path.expanduser(line)))
        if not track_path.is_absolute():
            track_path = path.parent / track_path
        track_path = track_path.resolve()
        if not track_path.is_file():
            raise DjMixError(f"Playlist entry does not exist: {track_path}")

        entries.append(
            PlaylistEntry(
                path=track_path,
                transition_before=pending_directive,
            )
        )
        pending_directive = None

    if not entries:
        raise DjMixError(f"Playlist has no local tracks: {path}")
    if entries[0].transition_before is not None:
        LOG.warning("Ignoring transition directive before the first track")
        entries[0] = PlaylistEntry(entries[0].path, None)
    return entries


def parse_sequence(
    values: Sequence[str],
    defaults: TransitionSpec,
) -> list[PlaylistEntry]:
    entries: list[PlaylistEntry] = []
    pending: TransitionSpec | None = None
    for raw_value in values:
        value = restore_directive(raw_value)
        if DIRECTIVE_PATTERN.fullmatch(value) and value.startswith("-"):
            if pending is not None:
                raise DjMixError(
                    "Two transition directives appeared without a track between them"
                )
            pending = parse_transition_directive(value, defaults=defaults)
            continue

        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise DjMixError(f"Track does not exist: {path}")
        entries.append(PlaylistEntry(path, pending))
        pending = None

    if pending is not None:
        raise DjMixError("A transition directive must be followed by a track")
    if not entries:
        raise DjMixError("No tracks were supplied")
    if entries[0].transition_before is not None:
        raise DjMixError("A transition directive cannot precede the first track")
    return entries


def entries_from_sources(
    values: Sequence[str],
    defaults: TransitionSpec,
) -> list[PlaylistEntry]:
    if len(values) == 1 and Path(restore_directive(values[0])).suffix.lower() in {
        ".m3u",
        ".m3u8",
    }:
        return parse_m3u(Path(restore_directive(values[0])), defaults)
    return parse_sequence(values, defaults)


def write_m3u(
    output: Path,
    entries: Sequence[PlaylistEntry],
    *,
    relative_to: Path | None = None,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U"]
    for index, entry in enumerate(entries):
        if index > 0 and entry.transition_before is not None:
            lines.append(f"#DJMIX {entry.transition_before.directive}")
        value: Path | str = entry.path
        if relative_to is not None:
            try:
                value = entry.path.relative_to(relative_to)
            except ValueError:
                value = entry.path
        lines.append(str(value))
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def analyze_entries(
    entries: Sequence[PlaylistEntry],
    *,
    cache: AnalysisCache,
    temporary_dir: Path,
    sample_rate: int,
    context_seconds: float,
    silence_top_db: float,
) -> dict[Path, TrackAnalysis]:
    analyses: dict[Path, TrackAnalysis] = {}
    for entry in entries:
        if entry.path in analyses:
            continue
        analyses[entry.path] = analyze_track(
            entry.path,
            cache=cache,
            temporary_dir=temporary_dir,
            sample_rate=sample_rate,
            context_seconds=context_seconds,
            silence_top_db=silence_top_db,
        )
    return analyses


def rank_candidates(
    current: TrackAnalysis,
    candidates: Iterable[TrackAnalysis],
    model: TransitionModel,
    *,
    maximum_tempo_change: float,
    same_artist_penalty: float,
) -> list[RankedCandidate]:
    ranked = [
        score_candidate(
            current,
            candidate,
            model,
            maximum_tempo_change=maximum_tempo_change,
            same_artist_penalty=same_artist_penalty,
        )
        for candidate in candidates
        if Path(candidate.path).resolve() != Path(current.path).resolve()
    ]
    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked


def auto_order_entries(
    entries: Sequence[PlaylistEntry],
    analyses: dict[Path, TrackAnalysis],
    model: TransitionModel,
    *,
    start_path: Path | None,
    maximum_tempo_change: float,
    same_artist_penalty: float,
) -> list[PlaylistEntry]:
    remaining = list(entries)
    if not remaining:
        return []

    if start_path is not None:
        start_path = start_path.expanduser().resolve()
        start_index = next(
            (
                index
                for index, entry in enumerate(remaining)
                if entry.path == start_path
            ),
            None,
        )
        if start_index is None:
            raise DjMixError(f"Start track is not in the playlist: {start_path}")
        current_entry = remaining.pop(start_index)
    else:
        current_entry = remaining.pop(0)

    ordered = [PlaylistEntry(current_entry.path, None)]
    while remaining:
        current = analyses[current_entry.path]
        candidate_analyses = [analyses[entry.path] for entry in remaining]
        ranked = rank_candidates(
            current,
            candidate_analyses,
            model,
            maximum_tempo_change=maximum_tempo_change,
            same_artist_penalty=same_artist_penalty,
        )
        if not ranked:
            break

        winner = ranked[0]
        winner_index = next(
            index
            for index, entry in enumerate(remaining)
            if entry.path == winner.path
        )
        current_entry = remaining.pop(winner_index)
        ordered.append(
            PlaylistEntry(
                path=current_entry.path,
                transition_before=winner.recommended_transition,
            )
        )
        LOG.info(
            "Auto-order: %s -> %s  score=%.3f  %s",
            current.label,
            winner.label,
            winner.score,
            winner.recommended_transition.directive,
        )
    return ordered


def choose_target_bpm(
    analyses: Sequence[TrackAnalysis],
    value: str,
) -> float:
    if not analyses:
        raise DjMixError("Cannot choose target BPM for an empty set")

    normalized = value.casefold()
    if normalized == "first":
        return analyses[0].bpm
    if normalized == "median":
        return float(np.median([analysis.bpm for analysis in analyses]))
    try:
        target = float(value)
    except ValueError as exc:
        raise DjMixError(
            "--target-bpm must be first, median, or a positive number"
        ) from exc
    if target <= 0:
        raise DjMixError("--target-bpm must be positive")
    return target


def atempo_filter(factor: float) -> str:
    """Build an FFmpeg atempo chain whose individual factors stay in range."""

    if factor <= 0:
        raise DjMixError(f"Invalid tempo factor: {factor}")

    factors: list[float] = []
    remaining = factor
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    factors.append(remaining)
    return ",".join(f"atempo={item:.10f}" for item in factors)


def prepare_track(
    analysis: TrackAnalysis,
    *,
    target_bpm: float,
    output_path: Path,
    sample_rate: int,
    target_rms_db: float,
    maximum_tempo_change: float,
    stretcher: Literal["auto", "rubberband", "ffmpeg"],
) -> PreparedTrack:
    source = Path(analysis.path)
    factor = tempo_factor_for(analysis.bpm, target_bpm)
    if abs(factor - 1.0) > maximum_tempo_change:
        raise DjMixError(
            f"{analysis.label} needs {100 * (factor - 1):+.1f}% tempo change, "
            f"exceeding --maximum-tempo-change "
            f"{maximum_tempo_change * 100:.1f}%"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    use_rubberband = (
        stretcher == "rubberband"
        or (stretcher == "auto" and shutil.which("rubberband") is not None)
    )

    if use_rubberband:
        decoded = output_path.with_name(output_path.stem + "-decoded.wav")
        run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-i",
                str(source),
                "-vn",
                "-ac",
                "2",
                "-ar",
                str(sample_rate),
                "-c:a",
                "pcm_f32le",
                "-y",
                str(decoded),
            ]
        )
        try:
            run_command(
                [
                    "rubberband",
                    "--quiet",
                    "--tempo",
                    f"{factor:.10f}",
                    "--crisp",
                    "5",
                    str(decoded),
                    str(output_path),
                ]
            )
        finally:
            decoded.unlink(missing_ok=True)
    else:
        filters = atempo_filter(factor)
        run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-i",
                str(source),
                "-vn",
                "-af",
                filters,
                "-ac",
                "2",
                "-ar",
                str(sample_rate),
                "-c:a",
                "pcm_f32le",
                "-y",
                str(output_path),
            ]
        )

    gain_db = target_rms_db - (
        0.5 * analysis.intro.rms_db + 0.5 * analysis.outro.rms_db
    )
    gain_db = float(np.clip(gain_db, -12.0, 12.0))
    gain_linear = 10.0 ** (gain_db / 20.0)
    time_scale = 1.0 / factor

    prepared = PreparedTrack(
        source_path=source,
        pcm_path=output_path,
        analysis=analysis,
        tempo_factor=factor,
        sample_rate=sample_rate,
        channels=2,
        gain_linear=gain_linear,
        audible_start_seconds=analysis.audible_start_seconds * time_scale,
        audible_end_seconds=analysis.audible_end_seconds * time_scale,
        mix_in_seconds=analysis.mix_in_seconds * time_scale,
        mix_out_seconds=analysis.mix_out_seconds * time_scale,
    )
    LOG.info(
        "Prepared: %-6.2f -> %-6.2f BPM  gain=%+.1f dB  %s",
        analysis.bpm,
        target_bpm,
        gain_db,
        analysis.label,
    )
    return prepared


def read_audio_segment(
    track: PreparedTrack,
    start_seconds: float,
    end_seconds: float,
) -> np.ndarray:
    if end_seconds <= start_seconds:
        return np.empty((0, track.channels), dtype=np.float32)

    with sf.SoundFile(track.pcm_path) as stream:
        start_frame = max(0, round(start_seconds * stream.samplerate))
        end_frame = min(len(stream), round(end_seconds * stream.samplerate))
        stream.seek(start_frame)
        data = stream.read(
            end_frame - start_frame,
            dtype="float32",
            always_2d=True,
        )
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    return data[:, :2] * track.gain_linear


def write_audio(writer: sf.SoundFile, data: np.ndarray) -> None:
    if data.size:
        writer.write(np.asarray(data, dtype=np.float32))


def equal_power_envelopes(
    length: int,
    fade_shape: Literal["early", "balanced", "late"] = "balanced",
) -> tuple[np.ndarray, np.ndarray]:
    progress = np.linspace(0.0, 1.0, length, endpoint=True)
    exponent = {
        "early": 0.72,
        "balanced": 1.0,
        "late": 1.38,
    }[fade_shape]
    phase = np.power(progress, exponent) * (math.pi / 2.0)
    return np.cos(phase), np.sin(phase)


def sigmoid_envelope(
    length: int,
    center_fraction: float,
    *,
    steepness: float = 12.0,
) -> np.ndarray:
    x = np.linspace(0.0, 1.0, length, endpoint=True)
    center_fraction = float(np.clip(center_fraction, 0.0, 1.0))
    raw = 1.0 / (1.0 + np.exp(-steepness * (x - center_fraction)))
    start = raw[0]
    end = raw[-1]
    return (raw - start) / max(end - start, 1e-9)


def split_bands(
    data: np.ndarray,
    sample_rate: int,
    crossover_hz: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Split audio with complementary second-order Butterworth filters."""

    normalized = crossover_hz / (sample_rate / 2.0)
    normalized = float(np.clip(normalized, 1e-4, 0.99))
    low_sos = butter(2, normalized, btype="lowpass", output="sos")
    high_sos = butter(2, normalized, btype="highpass", output="sos")
    low = sosfilt(low_sos, data, axis=0)
    high = sosfilt(high_sos, data, axis=0)
    return low.astype(np.float32), high.astype(np.float32)


def smooth_lowpass_sweep(
    data: np.ndarray,
    sample_rate: int,
    *,
    start_hz: float,
    end_hz: float,
    chunk_frames: int = 512,
) -> np.ndarray:
    """
    Apply a smooth, stateful low-pass sweep with slowly changing coefficients.

    Filter state is carried between chunks. The logarithmic frequency path more
    closely resembles a DJ filter control than a linear Hertz ramp.
    """

    if data.size == 0:
        return data

    output = np.empty_like(data, dtype=np.float32)
    chunk_count = max(1, math.ceil(len(data) / chunk_frames))
    cutoffs = np.geomspace(
        max(40.0, start_hz),
        max(40.0, end_hz),
        chunk_count,
    )
    state: np.ndarray | None = None

    for chunk_index, start in enumerate(range(0, len(data), chunk_frames)):
        end = min(len(data), start + chunk_frames)
        normalized = float(
            np.clip(cutoffs[chunk_index] / (sample_rate / 2.0), 1e-4, 0.99)
        )
        sos = butter(2, normalized, btype="lowpass", output="sos")
        chunk = data[start:end]
        if state is None:
            state = np.zeros(
                (sos.shape[0], 2, chunk.shape[1]),
                dtype=np.float64,
            )
        filtered, state = sosfilt(
            sos,
            chunk,
            axis=0,
            zi=state,
        )
        output[start:end] = filtered.astype(np.float32)
    return output


def align_lengths(
    left: np.ndarray,
    right: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    length = min(len(left), len(right))
    return left[:length], right[:length]


def render_transition(
    outgoing: np.ndarray,
    incoming: np.ndarray,
    *,
    sample_rate: int,
    spec: TransitionSpec,
    crossover_hz: float,
    filter_start_hz: float,
    filter_end_hz: float,
) -> np.ndarray:
    outgoing, incoming = align_lengths(outgoing, incoming)
    length = len(outgoing)
    if length == 0:
        return np.empty((0, 2), dtype=np.float32)

    outgoing_low, outgoing_high = split_bands(
        outgoing,
        sample_rate,
        crossover_hz,
    )
    incoming_low, incoming_high = split_bands(
        incoming,
        sample_rate,
        crossover_hz,
    )

    if spec.filter_sweep:
        outgoing_high = smooth_lowpass_sweep(
            outgoing_high,
            sample_rate,
            start_hz=filter_start_hz,
            end_hz=filter_end_hz,
        )

    outgoing_gain, incoming_gain = equal_power_envelopes(length, spec.fade_shape)
    bass_handoff = sigmoid_envelope(
        length,
        spec.bass_handoff_fraction,
        steepness=14.0,
    )

    minimum_outgoing_low_gain = 10.0 ** (-abs(spec.bass_cut_db) / 20.0)
    outgoing_low_gain = (
        1.0 - bass_handoff * (1.0 - minimum_outgoing_low_gain)
    )
    incoming_low_gain = bass_handoff

    outgoing_mix = (
        outgoing_high * outgoing_gain[:, np.newaxis]
        + outgoing_low
        * outgoing_gain[:, np.newaxis]
        * outgoing_low_gain[:, np.newaxis]
    )
    incoming_mix = (
        incoming_high * incoming_gain[:, np.newaxis]
        + incoming_low
        * incoming_gain[:, np.newaxis]
        * incoming_low_gain[:, np.newaxis]
    )

    mixed = outgoing_mix + incoming_mix
    peak = float(np.max(np.abs(mixed)))
    if peak > 1.25:
        mixed *= 1.25 / peak
    return mixed.astype(np.float32)


def resolve_transition_spec(
    entry: PlaylistEntry,
    outgoing: TrackAnalysis,
    incoming: TrackAnalysis,
    model: TransitionModel,
) -> TransitionSpec:
    if entry.transition_before is not None:
        return entry.transition_before
    return model.recommend(outgoing.outro, incoming.intro)


def render_set(
    entries: Sequence[PlaylistEntry],
    analyses: dict[Path, TrackAnalysis],
    model: TransitionModel,
    *,
    output: Path,
    work_dir: Path,
    target_bpm_value: str,
    sample_rate: int,
    target_rms_db: float,
    maximum_tempo_change: float,
    stretcher: Literal["auto", "rubberband", "ffmpeg"],
    crossover_hz: float,
    filter_start_hz: float,
    filter_end_hz: float,
    master_lufs: float | None,
    true_peak_db: float,
    keep_temporary: bool,
) -> dict[str, Any]:
    if len(entries) < 2:
        raise DjMixError("Rendering requires at least two tracks")

    ordered_analyses = [analyses[entry.path] for entry in entries]
    target_bpm = choose_target_bpm(ordered_analyses, target_bpm_value)
    bar_seconds = 4.0 * 60.0 / target_bpm
    LOG.info("Set target BPM: %.3f", target_bpm)

    work_dir.mkdir(parents=True, exist_ok=True)
    if keep_temporary:
        temporary_root = work_dir / (
            "render-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        )
        temporary_root.mkdir(parents=True, exist_ok=True)
        cleanup_context: tempfile.TemporaryDirectory[str] | None = None
    else:
        cleanup_context = tempfile.TemporaryDirectory(
            prefix="djmix-render-",
            dir=work_dir,
        )
        temporary_root = Path(cleanup_context.name)

    try:
        prepared_tracks: list[PreparedTrack] = []
        for index, analysis in enumerate(ordered_analyses):
            prepared_tracks.append(
                prepare_track(
                    analysis,
                    target_bpm=target_bpm,
                    output_path=temporary_root / f"track-{index:03d}.wav",
                    sample_rate=sample_rate,
                    target_rms_db=target_rms_db,
                    maximum_tempo_change=maximum_tempo_change,
                    stretcher=stretcher,
                )
            )

        transition_specs = [
            resolve_transition_spec(
                entries[index + 1],
                ordered_analyses[index],
                ordered_analyses[index + 1],
                model,
            )
            for index in range(len(entries) - 1)
        ]

        timeline_path = temporary_root / "timeline.wav"
        state = RenderState(
            current_start_seconds=prepared_tracks[0].audible_start_seconds
        )

        with sf.SoundFile(
            timeline_path,
            mode="w",
            samplerate=sample_rate,
            channels=2,
            subtype="FLOAT",
            format="WAV",
        ) as writer:
            for index, spec in enumerate(transition_specs):
                outgoing = prepared_tracks[index]
                incoming = prepared_tracks[index + 1]

                requested_overlap = spec.crossfade_bars * bar_seconds
                phrase_seconds = spec.phrase_bars * bar_seconds
                earliest_start = max(
                    state.current_start_seconds,
                    outgoing.mix_in_seconds,
                )
                latest_start = outgoing.mix_out_seconds - requested_overlap

                # Enter the incoming track on a phrase boundary in the
                # outgoing track. The last available P-bar boundary is chosen
                # so the requested C-bar overlap still fits before mix-out.
                if latest_start >= earliest_start:
                    phrase_index = math.floor(
                        (latest_start - outgoing.mix_in_seconds)
                        / phrase_seconds
                    )
                    transition_start = (
                        outgoing.mix_in_seconds
                        + max(0, phrase_index) * phrase_seconds
                    )
                    if transition_start < earliest_start:
                        phrase_index = math.ceil(
                            (earliest_start - outgoing.mix_in_seconds)
                            / phrase_seconds
                        )
                        transition_start = (
                            outgoing.mix_in_seconds
                            + max(0, phrase_index) * phrase_seconds
                        )
                else:
                    transition_start = earliest_start

                available_outgoing = max(
                    0.0,
                    outgoing.mix_out_seconds - transition_start,
                )
                available_incoming = max(
                    0.0,
                    incoming.audible_end_seconds - incoming.mix_in_seconds,
                )
                overlap = min(
                    requested_overlap,
                    available_outgoing,
                    available_incoming,
                )
                if overlap < 4.0 * bar_seconds:
                    raise DjMixError(
                        f"Insufficient mixable audio for "
                        f"{outgoing.analysis.label} -> {incoming.analysis.label}: "
                        f"{overlap:.1f}s available"
                    )

                actual_bars = overlap / bar_seconds
                if abs(actual_bars - spec.crossfade_bars) > 0.25:
                    LOG.warning(
                        "Shortening transition %s -> %s from %d to %.1f bars",
                        outgoing.analysis.label,
                        incoming.analysis.label,
                        spec.crossfade_bars,
                        actual_bars,
                    )

                solo = read_audio_segment(
                    outgoing,
                    state.current_start_seconds,
                    transition_start,
                )
                write_audio(writer, solo)
                state.rendered_seconds += len(solo) / sample_rate

                outgoing_overlap = read_audio_segment(
                    outgoing,
                    transition_start,
                    outgoing.mix_out_seconds,
                )
                incoming_overlap = read_audio_segment(
                    incoming,
                    incoming.mix_in_seconds,
                    incoming.mix_in_seconds + overlap,
                )
                transition_audio = render_transition(
                    outgoing_overlap,
                    incoming_overlap,
                    sample_rate=sample_rate,
                    spec=spec,
                    crossover_hz=crossover_hz,
                    filter_start_hz=filter_start_hz,
                    filter_end_hz=filter_end_hz,
                )
                write_audio(writer, transition_audio)
                transition_duration = len(transition_audio) / sample_rate
                state.rendered_seconds += transition_duration
                state.transitions.append(
                    {
                        "outgoing": outgoing.analysis.label,
                        "incoming": incoming.analysis.label,
                        "directive": spec.directive,
                        "source": spec.source,
                        "fade_shape": spec.fade_shape,
                        "timeline_start_seconds": round(
                            state.rendered_seconds - transition_duration,
                            3,
                        ),
                        "duration_seconds": round(transition_duration, 3),
                        "bars": round(transition_duration / bar_seconds, 3),
                    }
                )

                state.current_start_seconds = (
                    incoming.mix_in_seconds + transition_duration
                )
                LOG.info(
                    "Transition: %s -> %s  %s  %.1fs",
                    outgoing.analysis.label,
                    incoming.analysis.label,
                    spec.directive,
                    transition_duration,
                )

            final_track = prepared_tracks[-1]
            final_audio = read_audio_segment(
                final_track,
                state.current_start_seconds,
                final_track.audible_end_seconds,
            )
            write_audio(writer, final_audio)
            state.rendered_seconds += len(final_audio) / sample_rate

        output.parent.mkdir(parents=True, exist_ok=True)
        filters: list[str] = []
        if master_lufs is not None:
            filters.append(
                f"loudnorm=I={master_lufs:.1f}:"
                f"TP={true_peak_db:.1f}:"
                "LRA=11"
            )
        filters.append(
            f"alimiter=limit={10.0 ** (true_peak_db / 20.0):.8f}:"
            "attack=5:release=50"
        )

        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(timeline_path),
            "-af",
            ",".join(filters),
            "-y",
        ]
        suffix = output.suffix.casefold()
        if suffix == ".mp3":
            command.extend(["-c:a", "libmp3lame", "-q:a", "0"])
        elif suffix in {".m4a", ".mp4"}:
            command.extend(["-c:a", "aac", "-b:a", "320k"])
        elif suffix == ".opus":
            command.extend(["-c:a", "libopus", "-b:a", "256k"])
        elif suffix == ".flac":
            command.extend(["-c:a", "flac", "-compression_level", "8"])
        elif suffix in {".wav", ".wave"}:
            command.extend(["-c:a", "pcm_s24le"])
        else:
            raise DjMixError(
                "Output extension must be wav, flac, mp3, m4a, or opus"
            )
        command.append(str(output))
        run_command(command)

        manifest = {
            "schema_version": 1,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "output": str(output.resolve()),
            "target_bpm": target_bpm,
            "sample_rate": sample_rate,
            "duration_seconds": round(state.rendered_seconds, 3),
            "tracks": [
                {
                    "path": str(entry.path),
                    "label": analyses[entry.path].label,
                    "source_bpm": analyses[entry.path].bpm,
                    "camelot": analyses[entry.path].camelot,
                }
                for entry in entries
            ],
            "transitions": state.transitions,
        }
        manifest_path = output.with_suffix(output.suffix + ".json")
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return manifest
    finally:
        if cleanup_context is not None:
            cleanup_context.cleanup()


def display_rankings(
    ranked: Sequence[RankedCandidate],
    *,
    top: int,
) -> None:
    print(
        f"{'#':>2}  {'Score':>6}  {'BPM':>7}  {'ΔBPM':>7}  "
        f"{'Key':>3}  {'Kdist':>5}  {'Transition':<24}  Track"
    )
    for index, item in enumerate(ranked[:top], start=1):
        print(
            f"{index:>2}  {item.score:>6.3f}  {item.bpm:>7.2f}  "
            f"{item.bpm_delta:>+7.2f}  {item.camelot:>3}  "
            f"{item.camelot_distance:>5.1f}  "
            f"{item.recommended_transition.directive:<24}  "
            f"{item.label}"
        )


def common_runtime(args: argparse.Namespace) -> tuple[
    AnalysisCache,
    TransitionModel,
    tempfile.TemporaryDirectory[str],
    Path,
]:
    require_commands(("ffmpeg", "ffprobe"))
    cache = AnalysisCache(args.cache_dir.expanduser().resolve())
    examples = load_jsonl(args.dataset)
    defaults = TransitionSpec(
        phrase_bars=args.default_phrase_bars,
        crossfade_bars=args.default_crossfade_bars,
        bass_cut_db=args.default_bass_cut_db,
        filter_sweep=args.default_filter_sweep,
        bass_handoff_fraction=args.default_bass_handoff,
        fade_shape=args.default_fade_shape,
        source="default",
    )
    model = TransitionModel(
        examples,
        neighbors=args.neighbors,
        max_neighbors_per_source=args.max_neighbors_per_source,
        defaults=defaults,
    )
    temporary = tempfile.TemporaryDirectory(prefix="djmix-analysis-")
    return cache, model, temporary, Path(temporary.name)


def command_next(args: argparse.Namespace) -> int:
    cache, model, temporary, temporary_dir = common_runtime(args)
    try:
        defaults = model.defaults
        entries = parse_m3u(args.playlist, defaults)
        current_path = args.current.expanduser().resolve()
        all_entries = [PlaylistEntry(current_path), *entries]
        analyses = analyze_entries(
            all_entries,
            cache=cache,
            temporary_dir=temporary_dir,
            sample_rate=args.analysis_sample_rate,
            context_seconds=args.context_seconds,
            silence_top_db=args.silence_top_db,
        )
        ranked = rank_candidates(
            analyses[current_path],
            (analyses[entry.path] for entry in entries),
            model,
            maximum_tempo_change=args.maximum_tempo_change,
            same_artist_penalty=args.same_artist_penalty,
        )
        display_rankings(ranked, top=args.top)

        if args.json_output is not None:
            payload = [
                {
                    **dataclasses.asdict(item),
                    "path": str(item.path),
                    "recommended_transition": dataclasses.asdict(
                        item.recommended_transition
                    ),
                }
                for item in ranked[: args.top]
            ]
            args.json_output.parent.mkdir(parents=True, exist_ok=True)
            args.json_output.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        return 0
    finally:
        temporary.cleanup()


def command_order(args: argparse.Namespace) -> int:
    cache, model, temporary, temporary_dir = common_runtime(args)
    try:
        entries = parse_m3u(args.playlist, model.defaults)
        analyses = analyze_entries(
            entries,
            cache=cache,
            temporary_dir=temporary_dir,
            sample_rate=args.analysis_sample_rate,
            context_seconds=args.context_seconds,
            silence_top_db=args.silence_top_db,
        )
        ordered = auto_order_entries(
            entries,
            analyses,
            model,
            start_path=args.start,
            maximum_tempo_change=args.maximum_tempo_change,
            same_artist_penalty=args.same_artist_penalty,
        )
        write_m3u(
            args.output,
            ordered,
            relative_to=args.output.parent.resolve(),
        )
        LOG.info("Wrote ordered playlist: %s", args.output)
        return 0
    finally:
        temporary.cleanup()


def command_render(args: argparse.Namespace) -> int:
    cache, model, temporary, temporary_dir = common_runtime(args)
    try:
        entries = entries_from_sources(args.items, model.defaults)
        analyses = analyze_entries(
            entries,
            cache=cache,
            temporary_dir=temporary_dir,
            sample_rate=args.analysis_sample_rate,
            context_seconds=args.context_seconds,
            silence_top_db=args.silence_top_db,
        )
        if args.auto_order:
            entries = auto_order_entries(
                entries,
                analyses,
                model,
                start_path=args.start,
                maximum_tempo_change=args.maximum_tempo_change,
                same_artist_penalty=args.same_artist_penalty,
            )

        manifest = render_set(
            entries,
            analyses,
            model,
            output=args.output,
            work_dir=args.work_dir.expanduser().resolve(),
            target_bpm_value=args.target_bpm,
            sample_rate=args.output_sample_rate,
            target_rms_db=args.target_rms_db,
            maximum_tempo_change=args.maximum_tempo_change,
            stretcher=args.time_stretcher,
            crossover_hz=args.crossover_hz,
            filter_start_hz=args.filter_start_hz,
            filter_end_hz=args.filter_end_hz,
            master_lufs=args.master_lufs,
            true_peak_db=args.true_peak_db,
            keep_temporary=args.keep_temporary,
        )
        print(json.dumps(manifest, indent=2, ensure_ascii=False))
        return 0
    finally:
        temporary.cleanup()


def command_inspect(args: argparse.Namespace) -> int:
    cache, _, temporary, temporary_dir = common_runtime(args)
    try:
        analysis = analyze_track(
            args.track,
            cache=cache,
            temporary_dir=temporary_dir,
            sample_rate=args.analysis_sample_rate,
            context_seconds=args.context_seconds,
            silence_top_db=args.silence_top_db,
        )
        print(json.dumps(dataclasses.asdict(analysis), indent=2))
        return 0
    finally:
        temporary.cleanup()


def add_common_analysis_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path(".djlearn/transitions.jsonl"),
        help="Learned Luminosity-style transition examples",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".djmix/analysis-cache"),
    )
    parser.add_argument("--analysis-sample-rate", type=int, default=22_050)
    parser.add_argument("--context-seconds", type=float, default=90.0)
    parser.add_argument("--silence-top-db", type=float, default=45.0)
    parser.add_argument("--neighbors", type=int, default=7)
    parser.add_argument(
        "--max-neighbors-per-source",
        type=int,
        default=2,
        help=(
            "Per-set neighbor cap once the dataset contains multiple sets; "
            "zero disables"
        ),
    )
    parser.add_argument("--maximum-tempo-change", type=float, default=0.08)
    parser.add_argument("--same-artist-penalty", type=float, default=0.04)
    parser.add_argument("--default-phrase-bars", type=int, default=32)
    parser.add_argument("--default-crossfade-bars", type=int, default=24)
    parser.add_argument("--default-bass-cut-db", type=float, default=12.0)
    parser.add_argument(
        "--default-filter-sweep",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument("--default-bass-handoff", type=float, default=0.55)
    parser.add_argument(
        "--default-fade-shape",
        choices=("early", "balanced", "late"),
        default="balanced",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Rank trance tracks and render learned, phrase-aware DJ sets."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    next_parser = subparsers.add_parser(
        "next",
        help="Rank the best next track from an M3U playlist",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    next_parser.add_argument("current", type=Path)
    next_parser.add_argument("--playlist", type=Path, required=True)
    next_parser.add_argument("--top", type=int, default=10)
    next_parser.add_argument("--json-output", type=Path)
    add_common_analysis_options(next_parser)
    next_parser.set_defaults(handler=command_next)

    order_parser = subparsers.add_parser(
        "order",
        help="Greedily order an M3U playlist by transition quality",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    order_parser.add_argument("playlist", type=Path)
    order_parser.add_argument("--start", type=Path)
    order_parser.add_argument("-o", "--output", type=Path, required=True)
    add_common_analysis_options(order_parser)
    order_parser.set_defaults(handler=command_order)

    render_parser = subparsers.add_parser(
        "render",
        help="Render an M3U playlist or explicit track sequence",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    render_parser.add_argument(
        "items",
        nargs="+",
        help="One M3U path, or tracks interspersed with -P... directives",
    )
    render_parser.add_argument("-o", "--output", type=Path, required=True)
    render_parser.add_argument(
        "--auto-order",
        action="store_true",
        help="Choose each next track from the remaining playlist",
    )
    render_parser.add_argument("--start", type=Path)
    render_parser.add_argument("--work-dir", type=Path, default=Path(".djmix"))
    render_parser.add_argument(
        "--target-bpm",
        default="median",
        help="median, first, or a numeric BPM",
    )
    render_parser.add_argument("--output-sample-rate", type=int, default=44_100)
    render_parser.add_argument("--target-rms-db", type=float, default=-17.0)
    render_parser.add_argument(
        "--time-stretcher",
        choices=("auto", "rubberband", "ffmpeg"),
        default="auto",
    )
    render_parser.add_argument("--crossover-hz", type=float, default=180.0)
    render_parser.add_argument("--filter-start-hz", type=float, default=18_000.0)
    render_parser.add_argument("--filter-end-hz", type=float, default=1_500.0)
    render_parser.add_argument(
        "--master-lufs",
        type=float,
        default=None,
        help="Optional final loudness normalization; omitted preserves dynamics",
    )
    render_parser.add_argument("--true-peak-db", type=float, default=-1.0)
    render_parser.add_argument("--keep-temporary", action="store_true")
    add_common_analysis_options(render_parser)
    render_parser.set_defaults(handler=command_render)

    inspect_parser = subparsers.add_parser(
        "inspect",
        help="Show cached or newly computed features for one track",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    inspect_parser.add_argument("track", type=Path)
    add_common_analysis_options(inspect_parser)
    inspect_parser.set_defaults(handler=command_inspect)

    return parser


def validate_args(args: argparse.Namespace) -> None:
    if hasattr(args, "maximum_tempo_change"):
        if not 0.0 <= args.maximum_tempo_change <= 0.50:
            raise DjMixError("--maximum-tempo-change must be between 0 and 0.5")
        if args.context_seconds <= 0:
            raise DjMixError("--context-seconds must be positive")
        if args.analysis_sample_rate < 8_000:
            raise DjMixError("--analysis-sample-rate is too low")
        if args.neighbors <= 0:
            raise DjMixError("--neighbors must be positive")
        if args.max_neighbors_per_source < 0:
            raise DjMixError("--max-neighbors-per-source cannot be negative")
        if not 0.0 <= args.default_bass_handoff <= 1.0:
            raise DjMixError("--default-bass-handoff must be between 0 and 1")

    if args.command == "next" and args.top <= 0:
        raise DjMixError("--top must be positive")
    if args.command == "render":
        if args.output_sample_rate < 8_000:
            raise DjMixError("--output-sample-rate is too low")
        if args.crossover_hz <= 0:
            raise DjMixError("--crossover-hz must be positive")
        if args.filter_end_hz <= 0 or args.filter_start_hz <= args.filter_end_hz:
            raise DjMixError(
                "--filter-start-hz must be greater than --filter-end-hz > 0"
            )
        if args.true_peak_db > 0:
            raise DjMixError("--true-peak-db must not exceed 0 dB")


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    processed_argv = preprocess_directive_arguments(raw_argv)
    parser = build_parser()
    args = parser.parse_args(processed_argv)
    configure_logging(args.log_level)

    try:
        validate_args(args)
        return int(args.handler(args))
    except KeyboardInterrupt:
        LOG.error("Interrupted")
        return 130
    except DjMixError as exc:
        LOG.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

