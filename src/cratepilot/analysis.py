"""Scan local audio libraries and turn tracks into planner-facing features.

The expensive per-file DSP work is independent, so :func:`analyze_paths` runs
several analyses concurrently while preserving the caller's input order.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable

from .legacy import djmix
from .models import CueSuggestionV1, FeatureContextV1, TrackAnalysisV1

SUPPORTED_EXTENSIONS = {
    ".aac", ".ac3", ".aif", ".aiff", ".alac", ".amr", ".ape", ".au", ".caf",
    ".dts", ".eac3", ".flac", ".m4a", ".m4b", ".mp2", ".mp3", ".mp4", ".mpc",
    ".oga", ".ogg", ".opus", ".ra", ".tak", ".tta", ".wav", ".wave", ".webm",
    ".wma", ".wv",
}
LOGGER = logging.getLogger(__name__)


class AnalysisError(RuntimeError):
    """Raised when a library path or audio track cannot be analyzed."""

    pass


class _ThreadSafeAnalysisCache:
    """Serialize JSON cache reads/writes without serializing expensive DSP."""

    def __init__(self, directory: Path) -> None:
        self._cache = djmix.AnalysisCache(directory)
        self._lock = threading.Lock()

    def get(self, fingerprint: str):
        with self._lock:
            return self._cache.get(fingerprint)

    def put(self, analysis: djmix.TrackAnalysis) -> None:
        # The legacy cache uses a deterministic .tmp filename, so concurrent
        # promotions of identical content must not race each other.
        with self._lock:
            self._cache.put(analysis)


def scan_library(root: Path) -> list[Path]:
    """Return supported audio files below ``root`` in deterministic order."""

    root = root.expanduser().resolve()
    if not root.is_dir():
        raise AnalysisError(f"Music library does not exist or is not a folder: {root}")
    matches = sorted(
        (path for path in root.rglob("*") if path.is_file() and path.suffix.casefold() in SUPPORTED_EXTENSIONS),
        key=lambda path: str(path).casefold(),
    )
    LOGGER.info("Found %d supported audio files under %s", len(matches), root)
    return matches


def _context(value: djmix.AudioContext) -> FeatureContextV1:
    return FeatureContextV1(
        bpm=value.bpm,
        camelot=value.camelot,
        key_confidence=value.key_confidence,
        rms_db=value.rms_db,
        low_ratio=value.low_ratio,
        mid_ratio=value.mid_ratio,
        high_ratio=value.high_ratio,
        spectral_centroid_hz=value.spectral_centroid_hz,
        onset_strength=value.onset_strength,
        dynamic_range_db=value.dynamic_range_db,
    )


def energy_score(analysis: djmix.TrackAnalysis) -> float:
    """Map loudness, rhythm, spectrum, and dynamics to a 0–100 DJ energy score."""

    context = analysis.intro
    raw = (
        50.0
        + 3.6 * (context.rms_db + 17.0)
        + 7.0 * (context.onset_strength - 1.0)
        + 20.0 * (context.low_ratio - 0.25)
        + 0.003 * (context.spectral_centroid_hz - 2200.0)
        - 0.35 * max(0.0, context.dynamic_range_db - 9.0)
    )
    return round(min(100.0, max(0.0, raw)), 2)


def public_analysis(value: djmix.TrackAnalysis, *, include_path: bool = True) -> TrackAnalysisV1:
    """Convert the legacy analyzer result to CratePilot's stable public schema."""

    phrase_seconds = (60.0 / max(value.bpm, 1.0)) * 4.0 * 16.0
    hot_b = min(value.mix_out_seconds, value.mix_in_seconds + phrase_seconds)
    return TrackAnalysisV1(
        id=value.fingerprint,
        artist=value.artist,
        title=value.title,
        path=value.path if include_path else None,
        duration_seconds=round(value.duration_seconds, 3),
        bpm=round(value.bpm, 3),
        key=value.key,
        camelot=value.camelot,
        energy=energy_score(value),
        audible_start_seconds=round(value.audible_start_seconds, 3),
        audible_end_seconds=round(value.audible_end_seconds, 3),
        cues=CueSuggestionV1(
            hot_cue_a_seconds=round(value.mix_in_seconds, 3),
            hot_cue_b_seconds=round(hot_b, 3),
            hot_cue_c_seconds=round(value.mix_out_seconds, 3),
            mix_in_seconds=round(value.mix_in_seconds, 3),
            mix_out_seconds=round(value.mix_out_seconds, 3),
        ),
        intro=_context(value.intro),
        outro=_context(value.outro),
    )


def analyze_paths(
    paths: Iterable[Path],
    *,
    cache_directory: Path,
    sample_rate: int = 22_050,
    context_seconds: float = 90.0,
    progress_callback: Callable[[float, str], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
    max_workers: int | None = None,
) -> list[TrackAnalysisV1]:
    """Analyze audio paths concurrently and return results in input order.

    ``max_workers`` is primarily a resource-control and testing hook. By
    default CratePilot uses at most four workers because each analysis already
    performs CPU- and memory-intensive decoding and DSP. Progress callbacks are
    invoked only by the coordinating thread, never by worker threads.
    """

    paths = tuple(paths)
    report = progress_callback or (lambda _progress, _message: None)
    check_cancelled = cancel_check or (lambda: None)
    if not paths:
        report(0.99, "No audio files to analyze.")
        LOGGER.info("No audio files were supplied for analysis")
        return []
    worker_count = max_workers if max_workers is not None else min(4, os.cpu_count() or 1, len(paths))
    if worker_count < 1:
        raise ValueError("max_workers must be at least 1")
    worker_count = min(worker_count, len(paths))
    LOGGER.info("Analyzing %d audio files with %d parallel workers", len(paths), worker_count)
    cache = _ThreadSafeAnalysisCache(cache_directory)
    analyses: list[TrackAnalysisV1 | None] = [None] * len(paths)

    def analyze_one(index: int, path: Path, temporary_root: Path) -> TrackAnalysisV1:
        # Separate scratch directories prevent decoder filename collisions when
        # the same content appears under more than one library path.
        temporary_dir = temporary_root / f"worker-{index:06d}"
        temporary_dir.mkdir()
        try:
            value = djmix.analyze_track(
                path,
                cache=cache,
                temporary_dir=temporary_dir,
                sample_rate=sample_rate,
                context_seconds=context_seconds,
                silence_top_db=45.0,
            )
        except (djmix.DjMixError, OSError, ValueError) as exc:
            raise AnalysisError(f"{path}: {exc}") from exc
        return public_analysis(value)

    with tempfile.TemporaryDirectory(prefix="cratepilot-analysis-") as temporary:
        temporary_root = Path(temporary)
        executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="cratepilot-analysis")
        futures: dict[Future[TrackAnalysisV1], tuple[int, Path]] = {}
        try:
            for index, path in enumerate(paths):
                check_cancelled()
                futures[executor.submit(analyze_one, index, path, temporary_root)] = (index, path)
            completed = 0
            for future in as_completed(futures):
                check_cancelled()
                index, path = futures[future]
                analyses[index] = future.result()
                completed += 1
                report(
                    completed / len(paths),
                    f"Analyzed {completed:,} of {len(paths):,}: {path.name} "
                    f"({worker_count} parallel workers)",
                )
        finally:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
    result = [analysis for analysis in analyses if analysis is not None]
    report(0.99, f"Analyzed {len(result):,} tracks with {worker_count} parallel workers.")
    LOGGER.info("Finished analyzing %d audio files with %d parallel workers", len(result), worker_count)
    return result
