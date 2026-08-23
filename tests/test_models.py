from typing import get_args

import pytest
from pydantic import ValidationError

from music_video_producer.models import (
    NO_EVIDENCED_BUNDLE,
    Asset,
    Project,
    RenderJob,
    SamplingBundle,
    SamplingProfile,
    Shot,
    Song,
)


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


def test_no_sampling_profile_may_be_named_what_no_evidenced_bundle_is_named():
    """The sentinel is a name in the same namespace as the bundles, so it must not be one.

    `sampling_bundle_cell` draws `none` for a graph that has no evidenced bundle. A profile added
    to `SamplingProfile` under that name would make a real, chosen bundle indistinguishable from
    the absence of one, in the column a Director compares takes in.
    """
    assert NO_EVIDENCED_BUNDLE not in get_args(SamplingProfile)


def test_a_recorded_bundle_must_name_itself_and_a_job_without_one_reads_as_none():
    """The two ends of the field's contract, at the model.

    `name` is required: a record that exists says which bundle it is, and there is no half-written
    state for a reader to interpret. And `sampling_bundle` is defaulted to `None`, which is what
    every manifest written before 2026-08-23 loads as — a required field here would have made
    every existing project unopenable, and a defaulted *bundle* would have invented one.
    """
    with pytest.raises(ValidationError):
        SamplingBundle()

    assert RenderJob(kind="h3").sampling_bundle is None
    legacy = RenderJob.model_validate_json('{"kind": "h3", "seed": 7}')
    assert legacy.sampling_bundle is None
    # And it round-trips as an explicit `null` rather than being dropped, so the absence is
    # visible in the manifest rather than merely implied by a missing key.
    assert '"sampling_bundle":null' in legacy.model_dump_json().replace(" ", "")
