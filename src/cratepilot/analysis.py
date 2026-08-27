from __future__ import annotations

import logging
import tempfile
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
    pass


def scan_library(root: Path) -> list[Path]:
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
) -> list[TrackAnalysisV1]:
    paths = tuple(paths)
    report = progress_callback or (lambda _progress, _message: None)
    check_cancelled = cancel_check or (lambda: None)
    LOGGER.info("Analyzing %d audio files", len(paths))
    cache = djmix.AnalysisCache(cache_directory)
    analyses: list[TrackAnalysisV1] = []
    with tempfile.TemporaryDirectory(prefix="cratepilot-analysis-") as temporary:
        temporary_dir = Path(temporary)
        for index, path in enumerate(paths):
            check_cancelled()
            report(index / max(1, len(paths)), f"Analyzing {index + 1:,} of {len(paths):,}: {path.name}")
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
                raise AnalysisError(str(exc)) from exc
            analyses.append(public_analysis(value))
    report(0.99, f"Analyzed {len(analyses):,} tracks.")
    LOGGER.info("Finished analyzing %d audio files", len(analyses))
    return analyses
