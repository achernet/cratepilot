from pathlib import Path

from test_planner import track

from cratepilot.planner import replan_sequence
from cratepilot.storage import Store


def test_schema_and_round_trip(tmp_path: Path):
    store = Store(tmp_path / "cratepilot.db")
    tracks = [track(1), track(2)]
    store.save_tracks(tracks)
    assert [item.id for item in store.tracks()] == ["track-01", "track-02"]
    plan = replan_sequence(tracks, title="Stored")
    store.save_plan(plan)
    restored = store.plan(plan.id)
    assert restored == plan
    store.update_job("job", "analysis", "complete", 1.0, "done", {"tracks": 2})
    assert store.job("job")["result"] == {"tracks": 2}

