from __future__ import annotations

import dataclasses
import logging
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .identity import canonical_identity, split_version, stable_id
from .models import (
    AcquisitionCandidateV1,
    CatalogTrackV2,
    DiscoveryEdgeV1,
    DiscoverySessionV1,
    SourceReferenceV1,
    TrackAnalysisV1,
)
from .planner import evaluate_readiness, generate_drafts
from .providers import ProviderTrack, SimilarityProvider, VideoSearchProvider
from .storage import Store
from .youtube_scorer import score_youtube_results

LOGGER = logging.getLogger(__name__)


def legal_source_links(artist: str, title: str) -> tuple[dict[str, str], ...]:
    from urllib.parse import quote_plus

    query = quote_plus(f"{artist} {title}")
    return (
        {"label": "Beatport", "url": f"https://www.beatport.com/search?q={query}"},
        {"label": "Bandcamp", "url": f"https://bandcamp.com/search?q={query}"},
        {"label": "YouTube", "url": f"https://www.youtube.com/results?search_query={query}"},
    )


class Catalog:
    def __init__(self, store: Store) -> None:
        self.store = store

    def upsert(self, track: ProviderTrack, *, verification_state: str = "unverified", analysis_id: str | None = None) -> CatalogTrackV2:
        title, version = split_version(track.title)
        identity = canonical_identity(track.artist, title, version)
        existing = self.store.catalog_track_by_identity(identity)
        reference = SourceReferenceV1(
            provider=track.provider, external_id=track.external_id, url=track.url, isrc=track.isrc,
            evidence=track.evidence or {},
        )
        if existing:
            sources = list(existing.sources)
            keys = {(item.provider, item.external_id, item.url) for item in sources}
            if (reference.provider, reference.external_id, reference.url) not in keys:
                sources.append(reference)
            identifiers = dict(existing.identifiers)
            if track.external_id:
                identifiers[track.provider] = track.external_id
            if track.isrc:
                identifiers["isrc"] = track.isrc
            merged = dataclasses.replace(
                existing, sources=tuple(sources), identifiers=identifiers,
                verification_state=verification_state if verification_state != "unverified" else existing.verification_state,
                analysis_id=analysis_id or existing.analysis_id,
            )
            self.store.save_catalog_track(merged)
            return merged
        identifiers = {track.provider: track.external_id} if track.external_id else {}
        if track.isrc:
            identifiers["isrc"] = track.isrc
        value = CatalogTrackV2(
            id=stable_id("track", identity), artist=track.artist, title=title,
            version=version, normalized_identity=identity, identifiers=identifiers,
            sources=(reference,), verification_state=verification_state, analysis_id=analysis_id,
            created_at=datetime.now(UTC).isoformat(),
        )
        self.store.save_catalog_track(value)
        return value

    def import_analyses(self, tracks: Iterable[TrackAnalysisV1]) -> list[CatalogTrackV2]:
        values = []
        for track in tracks:
            values.append(self.upsert(ProviderTrack(
                artist=track.artist, title=track.title, provider="local", external_id=track.id,
                url=Path(track.path).as_uri() if track.path else None,
                evidence={"path": track.path} if track.path else {},
            ), verification_state="verified", analysis_id=track.id))
        return values

    def merge(self, target_id: str, source_id: str) -> CatalogTrackV2:
        target, source = self.store.catalog_track(target_id), self.store.catalog_track(source_id)
        if not target or not source:
            raise KeyError("Catalog track not found")
        source_keys = {(item.provider, item.external_id, item.url) for item in target.sources}
        merged_sources = list(target.sources)
        merged_sources.extend(
            item for item in source.sources if (item.provider, item.external_id, item.url) not in source_keys
        )
        merged = dataclasses.replace(
            target,
            identifiers={**source.identifiers, **target.identifiers},
            sources=tuple(merged_sources),
            assets=tuple(dict.fromkeys(target.assets + source.assets)),
            tags=tuple(sorted(set(target.tags + source.tags))),
            analysis_id=target.analysis_id or source.analysis_id,
        )
        self.store.save_catalog_track(merged)
        self.store.delete_catalog_track(source_id)
        return merged


class DiscoveryService:
    def __init__(
        self, store: Store, *, similarity: SimilarityProvider | None = None,
        video_search: VideoSearchProvider | None = None,
    ) -> None:
        self.store = store
        self.catalog = Catalog(store)
        self.similarity = similarity
        self.video_search = video_search

    def create_session(
        self, seeds: Sequence[ProviderTrack], *, max_depth: int = 2, max_nodes: int = 150,
        review_batch_size: int = 30, readiness_target: int = 8, result_count: int = 30,
    ) -> DiscoverySessionV1:
        if not 1 <= readiness_target <= 30:
            raise ValueError("readiness_target must be between 1 and 30")
        if not 1 <= result_count <= 100:
            raise ValueError("result_count must be between 1 and 100")
        session_id = stable_id("discover", datetime.now(UTC).isoformat(), *(f"{x.artist}|{x.title}" for x in seeds))
        session = DiscoverySessionV1(
            id=session_id, seeds=tuple(dataclasses.asdict(seed) for seed in seeds), max_depth=max(0, min(5, max_depth)),
            max_nodes=max(1, min(1000, max_nodes)), review_batch_size=max(1, min(100, review_batch_size)),
            readiness_target=readiness_target, result_count=result_count, status="queued",
        )
        self.store.save_discovery_session(session)
        return session

    def run(
        self,
        session_id: str,
        *,
        progress_callback: Callable[[float, str], None] | None = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> DiscoverySessionV1:
        report = progress_callback or (lambda _progress, _message: None)
        check_cancelled = cancel_check or (lambda: None)
        session = self.store.discovery_session(session_id)
        if not session:
            raise KeyError("Discovery session not found")
        seeds = [ProviderTrack(**seed) for seed in session.seeds]
        queue = deque((seed, 0, None) for seed in seeds)
        seen: set[str] = set()
        track_ids: list[str] = []
        edge_ids: list[str] = []
        warnings: list[str] = []
        LOGGER.info("Starting discovery %s with %d seeds and a %d-node cap", session.id, len(seeds), session.max_nodes)
        while queue and len(seen) < session.max_nodes:
            check_cancelled()
            provider_track, depth, parent = queue.popleft()
            current = self.catalog.upsert(provider_track)
            if current.normalized_identity in seen:
                continue
            seen.add(current.normalized_identity)
            track_ids.append(current.id)
            report(
                min(0.62, 0.04 + 0.58 * len(seen) / max(1, session.max_nodes)),
                f"Discovered {len(seen):,} canonical tracks · graph depth {depth} · {len(queue):,} queued.",
            )
            if parent:
                edge = DiscoveryEdgeV1(
                    id=stable_id("edge", parent, current.id, provider_track.relationship), source_track_id=parent,
                    target_track_id=current.id, relationship=provider_track.relationship, provider=provider_track.provider,
                    weight=provider_track.weight, evidence=provider_track.evidence or {},
                )
                self.store.save_discovery_edge(edge)
                edge_ids.append(edge.id)
            if depth >= session.max_depth or self.similarity is None:
                continue
            try:
                related = self.similarity.related(provider_track, same_artist_limit=10, similar_limit=20)
            except Exception as exc:
                warnings.append(f"{provider_track.artist} — {provider_track.title}: discovery provider unavailable ({exc})")
                continue
            for neighbor in related:
                if len(seen) + len(queue) >= session.max_nodes:
                    break
                queue.append((neighbor, depth + 1, current.id))
        report(0.65, f"Ranking up to {session.review_batch_size} mixable source candidates.")
        candidates = self._build_candidates(
            track_ids, session.result_count, session.review_batch_size,
            progress_callback=report, cancel_check=check_cancelled,
        )
        report(0.84, "Evaluating strict draft readiness once against the analyzed library.")
        ready_plans = self._ready_plans(
            session.readiness_target, progress_callback=report, cancel_check=check_cancelled
        )
        status = "ready" if len(ready_plans) >= session.readiness_target else "limit_reached"
        if status != "ready":
            warnings.append(
                f"Found {len(ready_plans)} of {session.readiness_target} strict drafts before discovery limits were exhausted."
            )
        updated = dataclasses.replace(
            session, status=status, discovered_track_ids=tuple(track_ids), edge_ids=tuple(edge_ids),
            candidate_ids=tuple(item.id for item in candidates), ready_plan_ids=tuple(item.id for item in ready_plans),
            warnings=tuple(warnings), progress=1.0,
        )
        self.store.save_discovery_session(updated)
        LOGGER.info("Discovery %s finished with %d tracks and %d candidates", session.id, len(track_ids), len(candidates))
        return updated

    def _build_candidates(
        self,
        track_ids: Sequence[str],
        result_count: int,
        batch_size: int,
        *,
        progress_callback: Callable[[float, str], None] | None = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> list[AcquisitionCandidateV1]:
        report = progress_callback or (lambda _progress, _message: None)
        check_cancelled = cancel_check or (lambda: None)
        if self.video_search is None:
            return []
        candidates: list[AcquisitionCandidateV1] = []
        for track_index, track_id in enumerate(track_ids):
            check_cancelled()
            track = self.store.catalog_track(track_id)
            if not track or track.assets or len(candidates) >= batch_size:
                continue
            try:
                report(
                    0.65 + 0.17 * track_index / max(1, len(track_ids)),
                    f"Searching versions for {track.artist} — {track.title}.",
                )
                results = self.video_search.search(track.artist, track.title, limit=result_count)
            except Exception:
                continue
            for rank, scored in enumerate(score_youtube_results(results, artist=track.artist, title=track.title), 1):
                candidate = AcquisitionCandidateV1(
                    id=stable_id("candidate", track.id, scored.result.id), catalog_track_id=track.id,
                    provider="youtube", source_url=scored.result.url, title=scored.result.title,
                    channel=scored.result.channel, duration_seconds=scored.result.duration_seconds, rank=rank,
                    total_score=scored.total, components=scored.components, explanation=scored.explanation,
                    legal_links=legal_source_links(track.artist, track.title),
                )
                self.store.save_candidate(candidate)
                candidates.append(candidate)
                if len(candidates) >= batch_size:
                    break
        return candidates

    def _ready_plans(
        self,
        limit: int,
        *,
        progress_callback: Callable[[float, str], None] | None = None,
        cancel_check: Callable[[], None] | None = None,
    ):
        tracks = self.store.tracks()
        if len(tracks) < 2:
            return []
        try:
            drafts = generate_drafts(
                tracks,
                count=min(30, max(limit * 4, 3)),
                progress_callback=(
                    (lambda progress, message: progress_callback(0.84 + progress * 0.14, message))
                    if progress_callback else None
                ),
                cancel_check=cancel_check,
            )
        except Exception:
            return []
        accepted = []
        for draft in drafts:
            if evaluate_readiness(draft, tracks, accepted):
                self.store.save_plan(draft)
                accepted.append(draft)
                if len(accepted) >= limit:
                    break
        return accepted
