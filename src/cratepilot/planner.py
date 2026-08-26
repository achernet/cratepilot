from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .legacy.djmix import camelot_distance
from .models import SetPlanV1, TrackAnalysisV1, TransitionPlanV1

FIRST_BOOTH_CURVE = (0.35, 0.50, 0.78, 1.00, 0.84)
WEAK_TRANSITION_THRESHOLD = 0.45


class PlanningError(RuntimeError):
    pass


def _gaussian(value: float, sigma: float) -> float:
    return math.exp(-0.5 * (value / sigma) ** 2)


def _camelot_label(distance: float) -> str:
    if distance <= 0.1:
        return "same harmonic center"
    if distance <= 1.1:
        return "neighboring Camelot keys"
    if distance <= 2.1:
        return "workable harmonic movement"
    return "a deliberate harmonic jump"


def score_transition(source: TrackAnalysisV1, target: TrackAnalysisV1) -> TransitionPlanV1:
    bpm_delta = target.bpm - source.bpm
    tempo_factor = source.bpm / target.bpm if target.bpm else 1.0
    tempo_change = abs(tempo_factor - 1.0)
    tempo = _gaussian(bpm_delta, 3.5) if tempo_change <= 0.08 else 0.0
    distance = camelot_distance(source.outro.camelot, target.intro.camelot)
    harmony = math.exp(-0.72 * distance)
    rms_delta = target.intro.rms_db - source.outro.rms_db
    energy = _gaussian(rms_delta, 4.5)
    low = _gaussian(target.intro.low_ratio - source.outro.low_ratio, 0.15)
    centroid_ratio = math.log(
        max(target.intro.spectral_centroid_hz, 1.0) / max(source.outro.spectral_centroid_hz, 1.0)
    )
    timbre = _gaussian(centroid_ratio, 0.50)
    onset_ratio = math.log(
        max(target.intro.onset_strength, 1e-4) / max(source.outro.onset_strength, 1e-4)
    )
    rhythm = _gaussian(onset_ratio, 0.80)
    learned = max(0.0, min(1.0, 0.52 * harmony + 0.28 * energy + 0.20 * rhythm))
    heuristic = (
        0.28 * tempo
        + 0.24 * harmony
        + 0.15 * energy
        + 0.10 * low
        + 0.08 * timbre
        + 0.07 * rhythm
        + 0.08 * target.intro.key_confidence
    )
    same_artist = bool(source.artist and target.artist and source.artist.casefold() == target.artist.casefold())
    total = 0.62 * heuristic + 0.38 * learned - (0.04 if same_artist else 0.0)
    if tempo_change > 0.08:
        total -= 0.5 + tempo_change
    total = max(0.0, min(1.0, total))
    energy_change = target.energy - source.energy
    warning = None
    if tempo_change > 0.08:
        warning = f"Requires {tempo_change:.1%} tempo movement; keep changes within 8%."
    elif total < WEAK_TRANSITION_THRESHOLD:
        warning = "Weak transition: audition carefully or replace one of these tracks."
    explanation = (
        f"{source.bpm:.0f} → {target.bpm:.0f} BPM ({abs(bpm_delta):.1f} BPM apart)",
        f"{source.outro.camelot} → {target.intro.camelot}: {_camelot_label(distance)}",
        f"Energy {'rises' if energy_change >= 0 else 'settles'} by {abs(energy_change):.0f} points",
        "Matches the learned 32-bar transition neighborhood",
    )
    return TransitionPlanV1(
        source_track_id=source.id,
        target_track_id=target.id,
        total_score=round(total, 4),
        components={
            "tempo": round(tempo, 4),
            "harmony": round(harmony, 4),
            "energy": round(energy, 4),
            "low_end": round(low, 4),
            "timbre": round(timbre, 4),
            "rhythm": round(rhythm, 4),
            "learned": round(learned, 4),
        },
        tempo_factor=round(tempo_factor, 6),
        phrase_bars=32,
        crossfade_bars=32,
        bass_cut_db=12.0,
        bass_handoff_fraction=0.55 if energy_change >= 0 else 0.48,
        filter_sweep=energy_change < -8.0,
        fade_shape="late" if energy_change >= 12.0 else "balanced",
        explanation=explanation,
        warning=warning,
    )


def transition_seconds(transition: TransitionPlanV1, source: TrackAnalysisV1, target: TrackAnalysisV1) -> float:
    bpm = max(1.0, (source.bpm + target.bpm) / 2.0)
    return transition.crossfade_bars * 4.0 * 60.0 / bpm


def plan_duration(sequence: Sequence[TrackAnalysisV1], transitions: Sequence[TransitionPlanV1]) -> float:
    total = sum(track.duration_seconds for track in sequence)
    for index, transition in enumerate(transitions):
        total -= transition_seconds(transition, sequence[index], sequence[index + 1])
    return max(0.0, total)


def target_energy(position: float) -> float:
    position = min(1.0, max(0.0, position))
    scaled = position * (len(FIRST_BOOTH_CURVE) - 1)
    left = int(math.floor(scaled))
    right = min(len(FIRST_BOOTH_CURVE) - 1, left + 1)
    fraction = scaled - left
    return FIRST_BOOTH_CURVE[left] * (1.0 - fraction) + FIRST_BOOTH_CURVE[right] * fraction


def _sequence_score(
    sequence: Sequence[TrackAnalysisV1],
    transitions: Sequence[TransitionPlanV1],
    *,
    target_seconds: float,
) -> float:
    compatibility = sum(item.total_score for item in transitions) / max(1, len(transitions))
    curve_fit = 0.0
    for index, track in enumerate(sequence):
        position = index / max(1, len(sequence) - 1)
        curve_fit += max(0.0, 1.0 - abs(track.energy / 100.0 - target_energy(position)))
    curve_fit /= max(1, len(sequence))
    duration_fit = max(0.0, 1.0 - abs(plan_duration(sequence, transitions) - target_seconds) / target_seconds)
    artist_spacing = 1.0
    for source, target in zip(sequence, sequence[1:]):
        if source.artist and target.artist and source.artist.casefold() == target.artist.casefold():
            artist_spacing = 0.0
            break
    return 0.70 * compatibility + 0.20 * curve_fit + 0.05 * duration_fit + 0.05 * artist_spacing


@dataclass(frozen=True)
class _Beam:
    sequence: tuple[TrackAnalysisV1, ...]
    transitions: tuple[TransitionPlanV1, ...]
    score: float


def _locks_allow(sequence: Sequence[TrackAnalysisV1], locks: Mapping[int, str]) -> bool:
    for position, track_id in locks.items():
        if position < len(sequence) and sequence[position].id != track_id:
            return False
    return True


def generate_drafts(
    tracks: Iterable[TrackAnalysisV1],
    *,
    title: str = "First Booth — 45 min",
    preset: str = "first-booth-45",
    target_duration_seconds: float = 45.0 * 60.0,
    locked_positions: Mapping[int, str] | None = None,
    beam_width: int = 50,
    count: int = 3,
) -> list[SetPlanV1]:
    library = tuple(sorted(tracks, key=lambda item: item.id))
    if len(library) < 2:
        raise PlanningError("At least two analyzed tracks are required to build a set.")
    locks = dict(locked_positions or {})
    known = {item.id for item in library}
    if any(track_id not in known for track_id in locks.values()):
        raise PlanningError("A locked track is not present in the library.")
    minimum_seconds = target_duration_seconds * 0.93
    maximum_seconds = target_duration_seconds * 1.07
    beams: list[_Beam] = []
    for track in library:
        sequence = (track,)
        if _locks_allow(sequence, locks):
            beams.append(_Beam(sequence, (), _sequence_score(sequence, (), target_seconds=target_duration_seconds)))
    completed: list[_Beam] = []
    max_tracks = min(14, len(library))
    while beams:
        next_beams: list[_Beam] = []
        for beam in beams:
            duration = plan_duration(beam.sequence, beam.transitions)
            if minimum_seconds <= duration <= maximum_seconds:
                completed.append(beam)
            if len(beam.sequence) >= max_tracks or duration > maximum_seconds:
                continue
            used = {item.id for item in beam.sequence}
            for candidate in library:
                if candidate.id in used:
                    continue
                if beam.sequence[-1].artist and candidate.artist and beam.sequence[-1].artist.casefold() == candidate.artist.casefold():
                    continue
                sequence = (*beam.sequence, candidate)
                if not _locks_allow(sequence, locks):
                    continue
                transition = score_transition(beam.sequence[-1], candidate)
                transitions = (*beam.transitions, transition)
                score = _sequence_score(sequence, transitions, target_seconds=target_duration_seconds)
                next_beams.append(_Beam(sequence, transitions, score))
        next_beams.sort(key=lambda item: (-item.score, tuple(track.id for track in item.sequence)))
        beams = next_beams[:beam_width]
        if len(completed) >= count * 12 and beams and len(beams[0].sequence) > 8:
            break
    if not completed:
        completed = beams
    completed.sort(
        key=lambda item: (
            abs(plan_duration(item.sequence, item.transitions) - target_duration_seconds),
            -item.score,
            tuple(track.id for track in item.sequence),
        )
    )
    plans: list[SetPlanV1] = []
    seen: set[tuple[str, ...]] = set()
    for rank, beam in enumerate(completed):
        ids = tuple(item.id for item in beam.sequence)
        if ids in seen:
            continue
        seen.add(ids)
        duration = plan_duration(beam.sequence, beam.transitions)
        warnings = tuple(item.warning for item in beam.transitions if item.warning)
        digest = hashlib.sha256("\x1f".join(ids).encode()).hexdigest()[:16]
        plans.append(
            SetPlanV1(
                id=f"plan-{digest}",
                title=f"{title} · Draft {len(plans) + 1}",
                preset=preset,
                target_duration_seconds=target_duration_seconds,
                duration_seconds=round(duration, 3),
                track_ids=ids,
                locked_positions=locks,
                energy_curve=FIRST_BOOTH_CURVE,
                transitions=beam.transitions,
                objective_score=round(beam.score, 4),
                warnings=warnings,
            )
        )
        if len(plans) >= count:
            break
    if not plans:
        raise PlanningError("The selected library cannot satisfy the requested duration and locks.")
    return plans


def replan_sequence(sequence: Sequence[TrackAnalysisV1], *, title: str, preset: str = "manual") -> SetPlanV1:
    if len({item.id for item in sequence}) != len(sequence):
        raise PlanningError("A set cannot contain duplicate tracks.")
    transitions = tuple(score_transition(source, target) for source, target in zip(sequence, sequence[1:]))
    duration = plan_duration(sequence, transitions)
    digest = hashlib.sha256("\x1f".join(item.id for item in sequence).encode()).hexdigest()[:16]
    score = _sequence_score(sequence, transitions, target_seconds=duration or 1.0)
    return SetPlanV1(
        id=f"plan-{digest}", title=title, preset=preset, target_duration_seconds=duration,
        duration_seconds=round(duration, 3), track_ids=tuple(item.id for item in sequence),
        locked_positions={}, energy_curve=FIRST_BOOTH_CURVE, transitions=transitions,
        objective_score=round(score, 4), warnings=tuple(item.warning for item in transitions if item.warning),
    )

