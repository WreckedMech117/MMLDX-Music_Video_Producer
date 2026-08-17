import json

import pytest

from music_video_producer.models import Shot
from music_video_producer.timeline import TimelineError, align_h3_frames, build_director_timeline


def test_align_h3_frames_uses_17k_plus_5_grid():
    assert align_h3_frames(120) == 124
    assert align_h3_frames(124) == 124
    assert align_h3_frames(125) == 141


def test_director_timeline_sorts_shots_and_uses_frames():
    shots = [
        Shot(start=3.0, duration=2.0, prompt="close-up"),
        Shot(start=0.0, duration=3.0, prompt="wide shot"),
    ]

    result = build_director_timeline(shots, window_start=0, window_duration=5, fps=24)
    payload = json.loads(result.timeline_data)

    assert [segment["prompt"] for segment in payload["segments"]] == ["wide shot", "close-up"]
    assert payload["segments"][1]["start"] == 72
    assert payload["segments"][1]["length"] == 48
    assert result.requested_frames == 120
    assert result.aligned_frames == 124
    assert result.warnings == []


def test_director_timeline_rejects_overlapping_shots():
    shots = [
        Shot(start=0, duration=4, prompt="first"),
        Shot(start=3, duration=4, prompt="second"),
    ]

    with pytest.raises(TimelineError, match="overlap"):
        build_director_timeline(shots, window_start=0, window_duration=7)


def test_director_timeline_warns_outside_h3_training_window():
    result = build_director_timeline(
        [Shot(start=0, duration=18, prompt="long")], window_start=0, window_duration=18
    )

    assert any("4–15" in warning for warning in result.warnings)
