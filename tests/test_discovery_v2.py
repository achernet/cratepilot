from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

import pytest

from cratepilot.acquisition import ACKNOWLEDGEMENT_VERSION, AcquisitionError, AcquisitionService, sanitize_component
from cratepilot.crates import CrateRuleError, materialize_crate, write_m3u8
from cratepilot.discovery import Catalog, DiscoveryService
from cratepilot.identity import canonical_identity, split_version
from cratepilot.models import (
    AcquisitionCandidateV1,
    CatalogTrackV2,
    LocalAssetV1,
    SmartCrateV1,
)
from cratepilot.providers import ProviderTrack, ShazamRelatedProvider, VideoResult
from cratepilot.storage import DATABASE_VERSION, Store
from cratepilot.youtube_scorer import score_youtube_results


class RelatedFixture:
    def related(self, track, *, same_artist_limit, similar_limit):
        if track.title == "Seed":
            return [
                ProviderTrack("Artist", "Second", "shazam", relationship="same_artist"),
                ProviderTrack("Neighbor", "Third", "shazam", relationship="similar"),
                ProviderTrack("The Artist", "Seed (Official Audio)", "shazam", relationship="similar"),
            ]
        return []


class SearchFixture:
    def search(self, artist, title, *, limit=30):
        assert limit == 30
        return [
            VideoResult("bad", f"{title} live concert", "Someone Else", "https://www.youtube.com/watch?v=bad", 500, 2_000_000),
            VideoResult("good", f"{artist} - {title} (Official Audio)", artist, "https://www.youtube.com/watch?v=good", 310, 500_000),
        ]


class NeverVerifier:
    def verify(self, *args, **kwargs):
        raise AssertionError("verification must not run during approval")


def test_identity_normalizes_articles_noise_and_versions():
    assert canonical_identity("The Artist", "A Song (Official Audio)") == canonical_identity("Artist", "A Song")
    assert split_version("A Song (Extended Mix)") == ("A Song", "Extended Mix")
    assert canonical_identity("Artist", "A Song (Extended Mix)") != canonical_identity("Artist", "A Song")


def test_catalog_collapses_duplicate_sources_and_preserves_evidence(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    catalog = Catalog(store)
    first = catalog.upsert(ProviderTrack("The Artist", "Seed", "spotify", external_id="sp1", isrc="ISRC1"))
    second = catalog.upsert(ProviderTrack("Artist", "Seed (Official Audio)", "shazam", external_id="sh1"))
    assert first.id == second.id
    assert second.identifiers == {"spotify": "sp1", "isrc": "ISRC1", "shazam": "sh1"}
    assert {item.provider for item in second.sources} == {"spotify", "shazam"}


def test_discovery_obeys_depth_node_and_review_caps_and_deduplicates(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    service = DiscoveryService(store, similarity=RelatedFixture(), video_search=SearchFixture())
    session = service.create_session(
        [ProviderTrack("Artist", "Seed", "manual")], max_depth=2, max_nodes=3, review_batch_size=2,
    )
    completed = service.run(session.id)
    assert len(completed.discovered_track_ids) == 3
    assert len(store.catalog_tracks()) == 3
    assert len(completed.candidate_ids) == 2
    assert completed.status == "limit_reached"
    assert completed.progress == 1


def test_youtube_ranking_prefers_identity_and_penalizes_live_version():
    ranked = score_youtube_results(SearchFixture().search("Artist", "Seed"), artist="Artist", title="Seed")
    assert ranked[0].result.id == "good"
    assert ranked[0].components["artist"] > ranked[1].components["artist"]
    assert any("version signals" in item for item in ranked[1].explanation)


def test_shazam_page_fixture_extracts_citation_array():
    document = '<script>window.data={"citation":[{"name":"Song","byArtist":"Artist","url":"https://x"}]}</script>'
    assert ShazamRelatedProvider._citations(document)[0]["name"] == "Song"


def test_acquisition_requires_standing_acknowledgement_and_explicit_batch(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    catalog = Catalog(store).upsert(ProviderTrack("Artist", "Seed", "manual"))
    candidate = AcquisitionCandidateV1(
        "candidate", catalog.id, "youtube", "https://www.youtube.com/watch?v=x", "Artist - Seed", "Artist",
        300, 1, .9, {"title": 1.0}, ("identity match",),
    )
    store.save_candidate(candidate)
    service = AcquisitionService(store, verifier=NeverVerifier(), root=tmp_path / "private")
    with pytest.raises(AcquisitionError):
        service.approve([])
    job = service.approve([candidate.id])
    assert job.acknowledgement_version is None
    with pytest.raises(AcquisitionError):
        service.run(job)
    service.acknowledge(True)
    assert service.is_acknowledged()
    approved = service.approve([candidate.id])
    assert approved.acknowledgement_version == ACKNOWLEDGEMENT_VERSION
    service.acknowledge(False)
    assert not service.is_acknowledged()


def test_sanitize_component_removes_windows_path_characters():
    assert sanitize_component('A/B: "Track"?') == "A_B_ _Track__"


def _catalog_track(track_id: str, analysis_id: str, *, year=2020, tags=()):
    return CatalogTrackV2(track_id, "Artist", track_id, f"artist|{track_id}|", year=year, analysis_id=analysis_id, tags=tags)


def test_smart_crate_rules_manual_overrides_and_stable_m3u(tmp_path: Path):
    from test_planner import track

    analyses = [dataclasses.replace(track(1), energy=40), dataclasses.replace(track(2), energy=80)]
    asset_path = tmp_path / "music" / "second.mp3"
    asset_path.parent.mkdir()
    asset_path.write_bytes(b"audio")
    catalog = [
        _catalog_track("first", analyses[0].id, tags=("warmup",)),
        dataclasses.replace(
            _catalog_track("second", analyses[1].id),
            assets=(LocalAssetV1("dj-mp3", str(asset_path), "hash", False, "audio/mpeg"),),
        ),
    ]
    crate = SmartCrateV1(
        "crate", "Peak", rules=({"field": "energy", "operator": "gte", "value": 70},),
        include_track_ids=("first",), order_by="energy",
    )
    materialized = materialize_crate(crate, catalog, analyses)
    assert materialized.materialized_track_ids == ("first", "second")
    playlist = tmp_path / "music" / "peak.m3u8"
    write_m3u8(playlist, materialized, catalog)
    assert playlist.read_text() == "#EXTM3U\nsecond.mp3\n"
    with pytest.raises(CrateRuleError):
        materialize_crate(dataclasses.replace(crate, rules=({"field": "path", "value": "x"},)), catalog, analyses)


def test_database_migrates_v1_without_losing_tracks(tmp_path: Path):
    path = tmp_path / "old.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript("CREATE TABLE schema_meta(version INTEGER NOT NULL); INSERT INTO schema_meta VALUES(1);")
    connection.commit()
    connection.close()
    store = Store(path)
    version = store.connection.execute("SELECT version FROM schema_meta").fetchone()[0]
    assert version == DATABASE_VERSION
    assert store.catalog_tracks() == []
