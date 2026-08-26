from __future__ import annotations

import pytest

from cratepilot.models import CueSuggestionV1, FeatureContextV1, TrackAnalysisV1
from cratepilot.planner import (
    evaluate_readiness,
    generate_drafts,
    plan_duration,
    replan_sequence,
    score_transition,
    target_energy,
)


def context(bpm: float, camelot: str, energy: float) -> FeatureContextV1:
    return FeatureContextV1(
        bpm=bpm,
        camelot=camelot,
        key_confidence=0.9,
        rms_db=-22.0 + energy / 10.0,
        low_ratio=0.2 + energy / 500.0,
        mid_ratio=0.55,
        high_ratio=0.2,
        spectral_centroid_hz=1700.0 + energy * 10.0,
        onset_strength=0.5 + energy / 100.0,
        dynamic_range_db=8.0,
    )


def track(index: int, *, artist: str | None = None, duration: float = 390.0) -> TrackAnalysisV1:
    bpm = 130.0 + index
    energy = 35.0 + index * 6.0
    camelot = f"{(index % 12) + 1}A"
    ctx = context(bpm, camelot, energy)
    return TrackAnalysisV1(
        id=f"track-{index:02d}", artist=artist or f"Artist {index}", title=f"Track {index}", path=f"/music/{index}.flac",
        duration_seconds=duration, bpm=bpm, key="A minor", camelot=camelot, energy=energy,
        audible_start_seconds=0.0, audible_end_seconds=duration,
        cues=CueSuggestionV1(8.0, 64.0, duration - 16.0, 8.0, duration - 16.0), intro=ctx, outro=ctx,
    )


def test_transition_is_explainable_and_bounded():
    transition = score_transition(track(1), track(2))
    assert 0.0 <= transition.total_score <= 1.0
    assert transition.phrase_bars == transition.crossfade_bars == 32
    assert len(transition.explanation) == 4
    assert set(transition.components) == {"tempo", "harmony", "energy", "low_end", "timbre", "rhythm", "learned"}


def test_first_booth_curve_builds_to_a_late_peak():
    values = [target_energy(index / 20) for index in range(21)]
    assert values[0] == pytest.approx(0.35)
    assert max(values) == pytest.approx(1.0)
    assert values[-1] == pytest.approx(0.84)
    assert values.index(max(values)) >= 15


def test_beam_search_returns_three_deterministic_duration_bounded_drafts():
    library = [track(index) for index in range(10)]
    first = generate_drafts(library)
    second = generate_drafts(reversed(library))
    assert len(first) == 3
    assert [plan.track_ids for plan in first] == [plan.track_ids for plan in second]
    assert all(42 * 60 <= plan.duration_seconds <= 48 * 60 for plan in first)
    assert len({plan.track_ids for plan in first}) == 3


def test_locked_position_is_a_hard_constraint():
    drafts = generate_drafts([track(index) for index in range(10)], locked_positions={2: "track-06"})
    assert all(plan.track_ids[2] == "track-06" for plan in drafts)


def test_adjacent_artist_repeats_are_not_generated():
    library = [track(index, artist="Same" if index < 3 else None) for index in range(10)]
    drafts = generate_drafts(library)
    by_id = {item.id: item for item in library}
    for draft in drafts:
        artists = [by_id[item].artist for item in draft.track_ids]
        assert all(left != right for left, right in zip(artists, artists[1:]))


def test_manual_reorder_recomputes_only_the_sequence_contract():
    sequence = [track(0), track(2), track(1)]
    plan = replan_sequence(sequence, title="Edited")
    assert plan.track_ids == tuple(item.id for item in sequence)
    assert len(plan.transitions) == 2
    assert plan.duration_seconds == pytest.approx(plan_duration(sequence, plan.transitions), abs=0.001)


def test_draft_count_is_configurable_and_bounded():
    assert len(generate_drafts([track(index) for index in range(10)], count=5)) == 5
    with pytest.raises(Exception, match="between 1 and 30"):
        generate_drafts([track(1), track(2)], count=31)


def test_strict_readiness_checks_transition_energy_and_diversity():
    import dataclasses

    sequence = [
        dataclasses.replace(track(index), energy=target_energy(index / 4) * 100)
        for index in range(5)
    ]
    plan = replan_sequence(sequence, title="Strict")
    transitions = tuple(dataclasses.replace(item, total_score=.75, warning=None) for item in plan.transitions)
    plan = dataclasses.replace(plan, duration_seconds=45 * 60, transitions=transitions, warnings=())
    assert evaluate_readiness(plan, sequence)
    assert not evaluate_readiness(dataclasses.replace(plan, warnings=("weak",)), sequence)
    near_duplicate = dataclasses.replace(plan, id="other")
    assert not evaluate_readiness(near_duplicate, sequence, [plan])
