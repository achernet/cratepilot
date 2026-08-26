from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any, Iterable

from .models import CatalogTrackV2, SmartCrateV1, TrackAnalysisV1


class CrateRuleError(ValueError):
    pass


def _field(track: CatalogTrackV2, analysis: TrackAnalysisV1 | None, name: str) -> Any:
    if name in {"artist", "title", "year", "verification_state", "tags"}:
        return getattr(track, name)
    if name in {"bpm", "energy", "key", "camelot", "duration_seconds"}:
        return getattr(analysis, name) if analysis else None
    if name == "community":
        return track.identifiers.get("community")
    raise CrateRuleError(f"Unsupported smart-crate field: {name}")


def _matches(value: Any, operator: str, expected: Any) -> bool:
    if operator == "eq":
        return value == expected
    if operator == "contains":
        if isinstance(value, (tuple, list, set)):
            return str(expected).casefold() in {str(item).casefold() for item in value}
        return str(expected).casefold() in str(value or "").casefold()
    if operator == "regex":
        return bool(re.search(str(expected), str(value or ""), re.I))
    if operator == "between":
        return value is not None and float(expected[0]) <= float(value) <= float(expected[1])
    if operator == "gte":
        return value is not None and float(value) >= float(expected)
    if operator == "lte":
        return value is not None and float(value) <= float(expected)
    raise CrateRuleError(f"Unsupported smart-crate operator: {operator}")


def materialize_crate(
    crate: SmartCrateV1,
    catalog: Iterable[CatalogTrackV2],
    analyses: Iterable[TrackAnalysisV1],
) -> SmartCrateV1:
    analysis_by_id = {item.id: item for item in analyses}
    values: list[tuple[CatalogTrackV2, TrackAnalysisV1 | None]] = []
    excluded = set(crate.exclude_track_ids)
    for track in catalog:
        if track.id in excluded:
            continue
        analysis = analysis_by_id.get(track.analysis_id or "")
        if track.id in crate.include_track_ids or all(
            _matches(_field(track, analysis, str(rule["field"])), str(rule.get("operator", "eq")), rule.get("value"))
            for rule in crate.rules
        ):
            values.append((track, analysis))
    included = {track.id for track, _ in values}
    catalog_by_id = {track.id: track for track in catalog}
    for track_id in crate.include_track_ids:
        if track_id not in included and track_id in catalog_by_id and track_id not in excluded:
            track = catalog_by_id[track_id]
            values.append((track, analysis_by_id.get(track.analysis_id or "")))
    values.sort(
        key=lambda pair: (_field(pair[0], pair[1], crate.order_by) is None, _field(pair[0], pair[1], crate.order_by), pair[0].id),
        reverse=crate.descending,
    )
    return dataclasses.replace(crate, materialized_track_ids=tuple(track.id for track, _ in values))


def write_m3u8(path: Path, crate: SmartCrateV1, catalog: Iterable[CatalogTrackV2]) -> None:
    by_id = {track.id: track for track in catalog}
    lines = ["#EXTM3U"]
    for track_id in crate.materialized_track_ids:
        track = by_id.get(track_id)
        if not track:
            continue
        asset = next((item for item in track.assets if item.kind == "dj-mp3"), None)
        if not asset:
            continue
        target = Path(asset.path).resolve()
        try:
            relative = target.relative_to(path.parent.resolve())
            lines.append(relative.as_posix())
        except ValueError:
            lines.append(target.as_uri())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

