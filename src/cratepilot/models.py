from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class FeatureContextV1:
    bpm: float
    camelot: str
    key_confidence: float
    rms_db: float
    low_ratio: float
    mid_ratio: float
    high_ratio: float
    spectral_centroid_hz: float
    onset_strength: float
    dynamic_range_db: float


@dataclass(frozen=True)
class CueSuggestionV1:
    hot_cue_a_seconds: float
    hot_cue_b_seconds: float
    hot_cue_c_seconds: float
    mix_in_seconds: float
    mix_out_seconds: float


@dataclass(frozen=True)
class TrackAnalysisV1:
    id: str
    artist: str
    title: str
    path: str | None
    duration_seconds: float
    bpm: float
    key: str
    camelot: str
    energy: float
    audible_start_seconds: float
    audible_end_seconds: float
    cues: CueSuggestionV1
    intro: FeatureContextV1
    outro: FeatureContextV1
    schema_version: int = SCHEMA_VERSION

    @property
    def label(self) -> str:
        return f"{self.artist} - {self.title}" if self.artist else self.title


@dataclass(frozen=True)
class TransitionPlanV1:
    source_track_id: str
    target_track_id: str
    total_score: float
    components: dict[str, float]
    tempo_factor: float
    phrase_bars: int
    crossfade_bars: int
    bass_cut_db: float
    bass_handoff_fraction: float
    filter_sweep: bool
    fade_shape: str
    explanation: tuple[str, ...]
    warning: str | None = None
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class SetPlanV1:
    id: str
    title: str
    preset: str
    target_duration_seconds: float
    duration_seconds: float
    track_ids: tuple[str, ...]
    locked_positions: dict[int, str]
    energy_curve: tuple[float, ...]
    transitions: tuple[TransitionPlanV1, ...]
    objective_score: float
    warnings: tuple[str, ...] = ()
    model_version: str = "hybrid-1"
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class ExportManifestV1:
    plan_id: str
    created_at: str
    output_directory: str
    files: tuple[dict[str, Any], ...]
    duration_seconds: float
    mastering: dict[str, float | str | bool]
    rekordbox_checklist: tuple[str, ...]
    schema_version: int = SCHEMA_VERSION


def to_dict(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {item.name: to_dict(getattr(value, item.name)) for item in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(key): to_dict(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_dict(item) for item in value]
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_dict(value), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def context_from_dict(value: dict[str, Any]) -> FeatureContextV1:
    return FeatureContextV1(**value)


def track_from_dict(value: dict[str, Any]) -> TrackAnalysisV1:
    payload = dict(value)
    payload["cues"] = CueSuggestionV1(**payload["cues"])
    payload["intro"] = context_from_dict(payload["intro"])
    payload["outro"] = context_from_dict(payload["outro"])
    return TrackAnalysisV1(**payload)


def transition_from_dict(value: dict[str, Any]) -> TransitionPlanV1:
    payload = dict(value)
    payload["explanation"] = tuple(payload.get("explanation", ()))
    return TransitionPlanV1(**payload)


def plan_from_dict(value: dict[str, Any]) -> SetPlanV1:
    payload = dict(value)
    payload["track_ids"] = tuple(payload["track_ids"])
    payload["locked_positions"] = {int(key): item for key, item in payload.get("locked_positions", {}).items()}
    payload["energy_curve"] = tuple(float(item) for item in payload["energy_curve"])
    payload["transitions"] = tuple(transition_from_dict(item) for item in payload["transitions"])
    payload["warnings"] = tuple(payload.get("warnings", ()))
    return SetPlanV1(**payload)

