import unittest

import numpy as np

from cratepilot.legacy import djmix
from cratepilot.legacy import transition_learner as learner


def audio_context() -> djmix.AudioContext:
    return djmix.AudioContext(
        bpm=138.0,
        key="A minor",
        camelot="8A",
        key_confidence=0.9,
        rms_db=-16.0,
        low_ratio=0.25,
        mid_ratio=0.55,
        high_ratio=0.20,
        spectral_centroid_hz=2400.0,
        onset_strength=1.2,
        dynamic_range_db=7.0,
    )


def learned_example(
    source_key: str,
    *,
    fade_shape: str = "balanced",
    preference_weight: float = 1.0,
    phrase_bars: int = 32,
) -> dict:
    context = audio_context()
    context_value = {
        "rms_db": context.rms_db,
        "low_ratio": context.low_ratio,
        "spectral_centroid_hz": context.spectral_centroid_hz,
        "onset_strength": context.onset_strength,
        "dynamic_range_db": context.dynamic_range_db,
    }
    return {
        "source_key": source_key,
        "bpm_delta": 0.0,
        "camelot_distance": 0.0,
        "outgoing_context": context_value,
        "incoming_context": context_value,
        "phrase_bars": phrase_bars,
        "crossfade_bars": 16,
        "estimated_bass_cut_db": 12.0,
        "bass_handoff_fraction": 0.55,
        "filter_sweep": False,
        "fade_shape": fade_shape,
        "analysis_confidence": 1.0,
        "preference_weight": preference_weight,
    }


class PublishedCueTests(unittest.TestCase):
    def test_chapter_numbers_are_removed_and_intro_boundary_is_excluded(self):
        metadata = {
            "chapters": [
                {"start_time": 0, "title": "1..Intro"},
                {"start_time": 18, "title": "2..Artist A - Track A"},
                {"start_time": 300, "title": "3..Artist B - Track B"},
                {"start_time": 600, "title": "4..Artist C - Track C"},
            ]
        }

        cues = learner.discover_published_cues(metadata, 900)
        brackets = learner.brackets_from_cues(cues, 900, 60)

        self.assertEqual(
            [cue.label for cue in cues],
            [
                "Intro",
                "Artist A - Track A",
                "Artist B - Track B",
                "Artist C - Track C",
            ],
        )
        self.assertEqual(len(brackets), 2)
        self.assertEqual(brackets[0].outgoing.artist, "Artist A")
        self.assertEqual(brackets[0].incoming.artist, "Artist B")

    def test_bracketed_description_timestamps_are_supported(self):
        metadata = {
            "description": (
                "1. [0:00] Intro\n"
                "2. [0:18] Artist A - Track A\n"
                "3. [6:03] Artist B - Track B\n"
            )
        }

        cues = learner.discover_published_cues(metadata, 600)

        self.assertEqual([cue.start_seconds for cue in cues], [0.0, 18.0, 363.0])
        self.assertEqual(cues[1].label, "Artist A - Track A")


class TransitionDirectiveTests(unittest.TestCase):
    def test_fade_shape_directive_round_trip_and_legacy_default(self):
        early = djmix.parse_transition_directive("-P32C16B12F1H0.55SE")
        legacy = djmix.parse_transition_directive("-P32C16B12F1H0.55")

        self.assertEqual(early.fade_shape, "early")
        self.assertTrue(early.directive.endswith("SE"))
        self.assertEqual(legacy.fade_shape, "balanced")

    def test_fade_shapes_move_the_equal_power_handoff(self):
        midpoint = 500
        _, incoming_early = djmix.equal_power_envelopes(1001, "early")
        _, incoming_balanced = djmix.equal_power_envelopes(1001, "balanced")
        _, incoming_late = djmix.equal_power_envelopes(1001, "late")

        self.assertGreater(incoming_early[midpoint], incoming_balanced[midpoint])
        self.assertGreater(incoming_balanced[midpoint], incoming_late[midpoint])


class TransitionModelTests(unittest.TestCase):
    def test_neighbors_are_capped_per_source_when_multiple_sets_exist(self):
        examples = [
            *[learned_example("set-a") for _ in range(4)],
            *[learned_example("set-b") for _ in range(3)],
        ]
        model = djmix.TransitionModel(
            examples,
            neighbors=7,
            max_neighbors_per_source=2,
        )

        indices, _ = model.neighbors_for(audio_context(), audio_context())
        selected_sources = [model.examples[index]["source_key"] for index in indices]

        self.assertEqual(selected_sources.count("set-a"), 2)
        self.assertEqual(selected_sources.count("set-b"), 2)

    def test_preferred_example_controls_learned_fade_shape(self):
        model = djmix.TransitionModel(
            [
                learned_example(
                    "gold-set",
                    fade_shape="early",
                    preference_weight=3.0,
                    phrase_bars=16,
                ),
                learned_example(
                    "other-set",
                    fade_shape="late",
                    preference_weight=1.0,
                    phrase_bars=64,
                ),
            ],
            neighbors=2,
        )

        recommendation = model.recommend(audio_context(), audio_context())

        self.assertEqual(recommendation.fade_shape, "early")
        self.assertEqual(recommendation.phrase_bars, 16)


if __name__ == "__main__":
    unittest.main()
