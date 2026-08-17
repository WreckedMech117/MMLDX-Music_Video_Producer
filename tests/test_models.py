import pytest
from pydantic import ValidationError

from music_video_producer.models import Asset, Project, Shot, Song


def test_project_serializes_song_assets_and_shots():
    project = Project(name="Signal Bloom")
    project.song = Song(title="Signal Bloom", source="generated", duration=214.5)
    project.assets.append(Asset(name="Lead", kind="character", path="assets/lead.png"))
    project.shots.append(Shot(start=0, duration=5.0, prompt="A slow push through fog"))

    restored = Project.model_validate_json(project.model_dump_json())

    assert restored.name == "Signal Bloom"
    assert restored.song.source == "generated"
    assert restored.assets[0].kind == "character"
    assert restored.shots[0].end == 5.0
    assert restored.created_at.tzinfo is not None


def test_shot_rejects_non_positive_duration():
    with pytest.raises(ValidationError):
        Shot(start=0, duration=0, prompt="invalid")


def test_asset_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        Asset(name="Mystery", kind="unknown", path="x")
