from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
CATALOG_SCHEMA_VERSION = 2


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


@dataclass(frozen=True)
class SourceReferenceV1:
    provider: str
    external_id: str | None = None
    url: str | None = None
    isrc: str | None = None
    evidence: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclass(frozen=True)
class LocalAssetV1:
    kind: str
    path: str
    sha256: str
    immutable: bool = True
    mime_type: str | None = None


@dataclass(frozen=True)
class CatalogTrackV2:
    id: str
    artist: str
    title: str
    normalized_identity: str
    version: str | None = None
    year: int | None = None
    year_source: str | None = None
    year_confidence: float | None = None
    identifiers: dict[str, str] = dataclasses.field(default_factory=dict)
    sources: tuple[SourceReferenceV1, ...] = ()
    assets: tuple[LocalAssetV1, ...] = ()
    analysis_id: str | None = None
    verification_state: str = "unverified"
    tags: tuple[str, ...] = ()
    created_at: str | None = None
    schema_version: int = CATALOG_SCHEMA_VERSION


@dataclass(frozen=True)
class DiscoveryEdgeV1:
    id: str
    source_track_id: str
    target_track_id: str
    relationship: str
    provider: str
    weight: float
    evidence: dict[str, Any] = dataclasses.field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class DiscoverySessionV1:
    id: str
    seeds: tuple[dict[str, Any], ...]
    max_depth: int = 2
    max_nodes: int = 150
    review_batch_size: int = 30
    readiness_target: int = 8
    result_count: int = 30
    status: str = "draft"
    discovered_track_ids: tuple[str, ...] = ()
    edge_ids: tuple[str, ...] = ()
    candidate_ids: tuple[str, ...] = ()
    ready_plan_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    progress: float = 0.0
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class AcquisitionCandidateV1:
    id: str
    catalog_track_id: str
    provider: str
    source_url: str
    title: str
    channel: str
    duration_seconds: float | None
    rank: int
    total_score: float
    components: dict[str, float]
    explanation: tuple[str, ...]
    legal_links: tuple[dict[str, str], ...] = ()
    existing_asset: bool = False
    review_state: str = "pending"
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class AcquisitionJobV1:
    id: str
    candidate_ids: tuple[str, ...]
    approved_at: str
    acknowledgement_version: str | None
    status: str = "approved"
    attempts: tuple[dict[str, Any], ...] = ()
    generated_assets: tuple[LocalAssetV1, ...] = ()
    quarantine_paths: tuple[str, ...] = ()
    message: str = ""
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class SmartCrateV1:
    id: str
    name: str
    rules: tuple[dict[str, Any], ...] = ()
    include_track_ids: tuple[str, ...] = ()
    exclude_track_ids: tuple[str, ...] = ()
    order_by: str = "energy"
    descending: bool = False
    materialized_track_ids: tuple[str, ...] = ()
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


def source_from_dict(value: dict[str, Any]) -> SourceReferenceV1:
    return SourceReferenceV1(**value)


def asset_from_dict(value: dict[str, Any]) -> LocalAssetV1:
    return LocalAssetV1(**value)


def catalog_track_from_dict(value: dict[str, Any]) -> CatalogTrackV2:
    payload = dict(value)
    payload["sources"] = tuple(source_from_dict(item) for item in payload.get("sources", ()))
    payload["assets"] = tuple(asset_from_dict(item) for item in payload.get("assets", ()))
    payload["tags"] = tuple(payload.get("tags", ()))
    return CatalogTrackV2(**payload)


def discovery_edge_from_dict(value: dict[str, Any]) -> DiscoveryEdgeV1:
    return DiscoveryEdgeV1(**value)


def discovery_session_from_dict(value: dict[str, Any]) -> DiscoverySessionV1:
    payload = dict(value)
    for key in ("seeds", "discovered_track_ids", "edge_ids", "candidate_ids", "ready_plan_ids", "warnings"):
        payload[key] = tuple(payload.get(key, ()))
    return DiscoverySessionV1(**payload)


def acquisition_candidate_from_dict(value: dict[str, Any]) -> AcquisitionCandidateV1:
    payload = dict(value)
    payload["explanation"] = tuple(payload.get("explanation", ()))
    payload["legal_links"] = tuple(payload.get("legal_links", ()))
    return AcquisitionCandidateV1(**payload)


def acquisition_job_from_dict(value: dict[str, Any]) -> AcquisitionJobV1:
    payload = dict(value)
    payload["candidate_ids"] = tuple(payload.get("candidate_ids", ()))
    payload["attempts"] = tuple(payload.get("attempts", ()))
    payload["generated_assets"] = tuple(asset_from_dict(item) for item in payload.get("generated_assets", ()))
    payload["quarantine_paths"] = tuple(payload.get("quarantine_paths", ()))
    return AcquisitionJobV1(**payload)


def smart_crate_from_dict(value: dict[str, Any]) -> SmartCrateV1:
    payload = dict(value)
    for key in ("rules", "include_track_ids", "exclude_track_ids", "materialized_track_ids"):
        payload[key] = tuple(payload.get(key, ()))
    return SmartCrateV1(**payload)
