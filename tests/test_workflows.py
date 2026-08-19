import ast
import copy
import hashlib
import inspect
import io
import json
import math
import re
import tempfile
import tokenize
from pathlib import Path

import preflight
import preflight_audio_replace
import preflight_h3_ultra
import preflight_ltx25_enhance
import preflight_songplanner
import pytest

# The module object as well as the names, because one test below has to replace
# `song_audio_window` *where the builder looks it up* — see
# `test_the_restore_window_comes_from_song_audio_window_and_not_a_second_computation`.
from music_video_producer import workflows as workflows_module
from music_video_producer.app import SongPlannerRequest
from music_video_producer.timeline import (
    OVER_RENDER_SECONDS,
    align_h3_frames,
    over_render_frames,
)
from music_video_producer.workflows import (
    AUDIO_REPLACE_AUDIO_EXTENSIONS,
    AUDIO_REPLACE_CRF,
    AUDIO_REPLACE_FORMAT,
    AUDIO_REPLACE_PIX_FMT,
    AUDIO_REPLACE_SOURCE_FORMAT,
    AUDIO_REPLACE_VIDEO_EXTENSIONS,
    H3_ASPECT_RATIOS,
    H3_DEFAULT_ASPECT_RATIO,
    H3_DEFAULT_MEGAPIXELS,
    H3_DEFAULT_MULTIPLE,
    H3_DEFAULT_PROFILE,
    H3_DIRECTOR_DEFAULT_STEPS,
    H3_DIRECTOR_MAX_FRAMES,
    H3_DIRECTOR_MAX_SECONDS,
    H3_FRAME_RATE,
    H3_KEYFRAME_DEFAULT_STEPS,
    H3_KEYFRAME_MAX_FRAMES,
    H3_LORA_STRENGTH_LIMITS,
    H3_MEGAPIXEL_LIMITS,
    H3_MULTIPLE_LIMITS,
    H3_REFERENCE_DIMENSION_LIMITS,
    H3_REFERENCE_LIMITS,
    H3_REFERENCE_MAX_FRAMES,
    H3_REFERENCE_PROFILES,
    H3_SPLIT_OFFSETS,
    LTX25_DIVISOR,
    LTX25_ENHANCE_CFG,
    LTX25_ENHANCE_DETAILER_LORA,
    LTX25_ENHANCE_DETAILER_STRENGTH,
    LTX25_ENHANCE_LARGEST_SIZE,
    LTX25_ENHANCE_PROMPT,
    LTX25_ENHANCE_SAMPLER,
    LTX25_ENHANCE_SEED,
    LTX25_ENHANCE_SIGMAS,
    LTX25_ENHANCE_SOURCE_EXTENSIONS,
    MULTIVIEW_CFG,
    MULTIVIEW_DENOISE,
    MULTIVIEW_SAMPLER,
    MULTIVIEW_SCHEDULER,
    MULTIVIEW_STEPS,
    MUSIC3_MAX_DURATION_SECONDS,
    SONGPLANNER_DEFAULT_DURATION_HEADROOM,
    SONGPLANNER_MAX_DURATION_HEADROOM,
    H3SamplingProfile,
    WorkflowCatalog,
    audio_replace_lengths,
    build_audio_replace_payload,
    build_flux_payload,
    build_h3_director_payload,
    build_h3_keyframe_payload,
    build_h3_reference_payload,
    build_ltx25_enhance_payload,
    build_multiview_payload,
    build_music3_payload,
    build_songplanner_invented_payload,
    build_songplanner_known_lyrics_payload,
    normalize_to_divisor,
    patch_ltx25_dimension_boundary,
    reachable_node_ids,
    select_resolution,
    song_audio_window,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_EXPORTS = REPO_ROOT / "workflow_templates" / "reference_exports"


def recorded_object_info() -> dict:
    return json.loads((REPO_ROOT / "tests/fixtures/object_info.json").read_text(encoding="utf-8"))


def every_builder_payload() -> list[tuple[str, dict]]:
    """One representative payload from every builder, for cross-adapter guards."""
    template = json.loads(
        (REFERENCE_EXPORTS / "h3-ltx25-user-export.json").read_text(encoding="utf-8")
    )
    return [
        *preflight_songplanner.audit_payloads(),
        ("music3", build_music3_payload(caption="c", lyrics="l", duration=120, seed=0, prefix="p")),
        (
            "flux",
            build_flux_payload(
                prompt="p", width=1024, height=1024, steps=20, guidance=4.0, seed=0, prefix="p"
            ),
        ),
        ("multiview", build_multiview_payload(image_name="a.png", prompt="p", seed=0, prefix="p")),
        (
            "h3-director",
            build_h3_director_payload(
                timeline_data='{"segments":[{"id":"s","start":0,"length":120,"prompt":"p"}]}',
                duration=5.0,
                requested_frames=120,
                seed=0,
                width=1344,
                height=768,
                steps=20,
                prefix="p",
            ),
        ),
        (
            "h3-reference",
            build_h3_reference_payload(
                prompt="<Picture 1>",
                references=[{"kind": "picture", "file": "a.png"}],
                duration=8,
                width=1280,
                height=720,
                steps=20,
                seed=0,
                prefix="p",
            ),
        ),
        (
            # The turbo profile is a different graph — one node more, drawing the whole
            # model chain through a LoRA — so it earns its own row here rather than
            # being assumed equivalent to the default one above.
            "h3-reference-turbo",
            build_h3_reference_payload(
                prompt="<Picture 1>",
                references=[{"kind": "picture", "file": "a.png"}],
                duration=8,
                width=1280,
                height=720,
                seed=0,
                prefix="p",
                profile="turbo",
            ),
        ),
        (
            # The third profile is the same shape as the turbo one and different in every
            # value it carries — a different LoRA file at a different strength — so it gets
            # its own row too: a filename no payload here builds is a filename this range
            # check never sees.
            "h3-reference-turbo-references2v",
            build_h3_reference_payload(
                prompt="<Picture 1>",
                references=[{"kind": "picture", "file": "a.png"}],
                duration=8,
                width=1280,
                height=720,
                seed=0,
                prefix="p",
                profile="turbo-references2v",
            ),
        ),
        (
            # The keyframe adapter is a third graph on a third checkpoint, and its two
            # shapes differ in whether `last_frame` exists at all — so each earns a row.
            "h3-keyframe-first-last",
            build_h3_keyframe_payload(
                prompt="p",
                first_frame="a.png",
                last_frame="b.png",
                duration=5,
                width=1280,
                height=720,
                seed=0,
                prefix="p",
            ),
        ),
        (
            "h3-keyframe-first-only",
            build_h3_keyframe_payload(
                prompt="p",
                first_frame="a.png",
                last_frame=None,
                duration=5,
                seed=0,
                prefix="p",
            ),
        ),
        ("ltx25-patched", patch_ltx25_dimension_boundary(template)),
        (
            # The one shape this builder has: every sampling value the export fixes is a
            # constant here rather than a control, so there is no second combination.
            "ltx25-enhance",
            build_ltx25_enhance_payload(source_video="J:/comfy/output/take_00001.mp4", prefix="p"),
        ),
        (
            # Also one shape: this builder has no sampling to vary and no model to swap.
            "audio-replace",
            build_audio_replace_payload(
                source_video="J:/comfy/output/take_00001.mp4",
                source_audio="F:/data/master.mp3",
                start=12.0,
                duration=3.75,
                song_duration=154.644898,
                prefix="p",
            ),
        ),
    ]


# Node classes the recorded fixture does not cover, so nothing above range-checks
# them offline. Recorded explicitly rather than skipped silently: this list is the
# honest measure of the guard's reach, and it shrinks only when an audit's
# `--record` is extended past the classes it already covers.
#
# `preflight_h3_ultra.py --record` removed the thirteen the H3 Ultra reference graph
# uses — every `MiniMaxH3*` class except `MiniMaxH3DirectorCS`, which no audited
# payload builds, plus the sampler, guider, decode and save classes it shares with
# the Director and LTX graphs. Recording merges, so the SongPlanner classes those
# thirteen joined are still there.
#
# `LoraLoaderModelOnly` left the list when the turbo sampling profile put it in an
# audited payload. It was uncovered while only the Krea multiview and LTX graphs used
# it, and nothing audited those; now the H3 audit records it, so the Krea LoRAs are
# range-checked offline as a side effect.
#
# `preflight_ltx25_enhance.py --record` removed thirteen more, four of which were on
# this list because only the *unaudited* combined LTX export used them: `CFGGuider`,
# `CLIPTextEncode`, `LTXVConditioning` and `ManualSigmas`. The enhancement adapter
# submits all four, so they are recorded now and range-checked offline everywhere they
# appear. `LatentUpscaleModelLoader` stays here, and deliberately: the enhancement graph
# does **not** load it — it is one of the two orphans in that export — so nothing audited
# puts it in front of the live schema.
#
# `ResolutionSelector` left the list without any payload ever submitting it. It is the
# first entry recorded through `run_audit`'s `extra_classes`: `select_resolution`
# reproduces its arithmetic in Python — the H3 graph takes two integers, not a node — so
# the class appears in no payload while its option list and its two numeric ranges are
# exactly what that reproduction has to keep matching. Recording it is what lets the
# offline half of the suite check the reproduction against the schema rather than only
# against itself.
#
# `preflight_audio_replace.py --record` removed `VHS_LoadAudio`, which the restoration adapter
# submits, and recorded `LoadAudio` and `VHS_LoadVideo` through `extra_classes` — neither is in
# any payload, and both are here because the *shape* of their inputs is the whole justification
# for substituting them. `LatentUpscaleModelLoader` stays on this list for a second reason now:
# it is an orphan in the AudioReplacer export as well, so nothing audited puts it in front of
# the live schema from either graph.
UNRECORDED_CLASSES = frozenset({
    "ComfyMathExpression",
    "DualCLIPLoader", "EmptySD3LatentImage", "FluxGuidance", "FrameInterpolate",
    "FrameInterpolationModelLoader", "GetImageSize", "ImageResizeKJv2",
    "Krea2EditGroundedEncode", "Krea2EditModelPatch", "LTXVAudioVAEDecode", "LTXVAudioVAEEncode",
    "LTXVConcatAVLatent", "LTXVImgToVideoInplace", "LTXVLatentUpsampler",
    "LTXVSeparateAVLatent", "LatentUpscaleModelLoader", "LoadImage",
    "MathExpression|pysssss",
    "ModelSamplingFlux", "PrimitiveFloat",
    "PrimitiveStringMultiline", "RTXVideoSuperResolution",
    "SaveImage", "SeedVR2LoadDiTModel", "SeedVR2LoadVAEModel",
    "SeedVR2VideoUpscaler", "SetLatentNoiseMask", "SolidMask", "VAEDecodeTiled",
    "VAEEncode", "VHS_LoadImagePath", "easy cleanGpuUsed",
    "easy clearCacheAll",
})

SONGPLANNER_EXPORTS = {
    REFERENCE_EXPORTS
    / "songplanner-invented-user-export.json": "fb26b3720c47918e14b15b7d55583454b338fa24d31b54414a8b1ab9fbef1420",
    REFERENCE_EXPORTS
    / "songplanner-known-lyrics-user-export.json": "2df49d8ce9a72f4532e26f645af04e4706cc03c5cfeba3d62aa41447524a6659",
}


def test_catalog_reports_present_and_missing_workflows(tmp_path: Path):
    (tmp_path / "Flux-Image-Gen.json").write_text("{}", encoding="utf-8")
    catalog = WorkflowCatalog(tmp_path)

    entries = {entry.id: entry for entry in catalog.list()}

    assert entries["flux-image-gen"].available is True
    assert entries["music3-balanced"].available is False
    assert entries["krea-multiview"].category == "asset"


def test_flux_payload_exposes_real_generation_controls():
    payload = build_flux_payload(
        prompt="A weathered singer in sodium light",
        width=1024,
        height=1024,
        steps=20,
        guidance=4.0,
        seed=42,
        prefix="mvp/assets/character",
    )

    assert payload["11"]["inputs"]["text"].startswith("A weathered singer")
    assert payload["20"]["inputs"] == {"width": 1024, "height": 1024, "batch_size": 1}
    assert payload["19"]["inputs"]["noise_seed"] == 42
    assert payload["12"]["inputs"]["filename_prefix"] == "mvp/assets/character"


def test_music3_payload_uses_caption_lyrics_and_duration():
    payload = build_music3_payload(
        caption="dark synth rock with female vocals",
        lyrics="[Verse]\nStatic in the wires",
        duration=12.0,
        seed=7,
        prefix="mvp/songs/signal-bloom",
    )

    encoder = next(node for node in payload.values() if node["class_type"] == "MiniMaxMusic3TextEncode")
    latent = next(
        node for node in payload.values() if node["class_type"] == "EmptyMiniMaxMusic3LatentAudio"
    )
    assert encoder["inputs"]["caption"].startswith("dark synth")
    assert encoder["inputs"]["lyrics"].startswith("[Verse]")
    assert latent["inputs"]["seconds"] == 12.0


def test_songplanner_invented_payload_lands_controls_on_planner_and_music_nodes():
    payload = build_songplanner_invented_payload(
        idea="a slow-burn desert rock anthem with a female vocalist",
        genre_hint="desert rock",
        duration=90.0,
        duration_headroom=1.5,
        seed=41,
        prefix="mvp/songs/night-signal",
    )

    planner = next(node for node in payload.values() if node["class_type"] == "M3SongPlanner")
    encoder = next(
        node for node in payload.values() if node["class_type"] == "MiniMaxMusic3TextEncode"
    )
    latent = next(
        node for node in payload.values() if node["class_type"] == "EmptyMiniMaxMusic3LatentAudio"
    )
    sampler = next(node for node in payload.values() if node["class_type"] == "KSampler")
    save = next(node for node in payload.values() if node["class_type"] == "SaveAudioAdvanced")
    assert planner["inputs"]["idea"].startswith("a slow-burn desert rock")
    assert planner["inputs"]["genre_hint"] == "desert rock"
    assert planner["inputs"]["duration_seconds"] == 90.0
    assert planner["inputs"]["seed"] == 41
    assert encoder["inputs"]["caption"] == ["55", 0]
    assert encoder["inputs"]["lyrics"] == ["55", 1]
    assert encoder["inputs"]["seed"] == 41
    # The planner is asked for 90 s of song; the encoder's ceiling is 90 × 1.5. Two inputs
    # that take the same kind of number and mean different things.
    assert encoder["inputs"]["max_duration"] == 135.0
    assert latent["inputs"]["seconds"] == ["45", 1]
    assert sampler["inputs"]["seed"] == 41
    assert save["inputs"]["format"] == "flac"
    assert save["inputs"]["filename_prefix"] == "mvp/songs/night-signal"
    dropped = {"PreviewAny", "CR Text", "SeedNode", "VAEDecodeAudioTiled", "ComfySwitchNode"}
    assert not dropped & {node["class_type"] for node in payload.values()}


def test_songplanner_known_lyrics_payload_passes_lyrics_through_unchanged():
    lyrics = (
        "[Intro]\n\n[Verse 1]\nStatic in the wires tonight\n  indented line kept as-is\n\n"
        "[Chorus]\nWe are the night signal\nWe are the night signal\n\n[Outro]\nFade…"
    )
    payload = build_songplanner_known_lyrics_payload(
        idea="ballad",
        genre_hint="",
        lyrics=lyrics,
        duration=60,
        duration_headroom=1.5,
        seed=3,
        prefix="mvp/songs/known",
    )

    encoder = next(
        node for node in payload.values() if node["class_type"] == "MiniMaxMusic3TextEncode"
    )
    assert encoder["inputs"]["lyrics"] == lyrics
    assert encoder["inputs"]["caption"] == ["55", 0]


def test_songplanner_known_lyrics_payload_rejects_blank_lyrics():
    with pytest.raises(ValueError, match="lyrics"):
        build_songplanner_known_lyrics_payload(
            idea="ballad",
            genre_hint="",
            lyrics="  \n",
            duration=60,
            duration_headroom=1.5,
            seed=3,
            prefix="mvp/songs/x",
        )


def test_songplanner_known_lyrics_payload_never_degrades_to_invented():
    """A cover request with no lyric sheet must fail loudly, not silently invent lyrics."""
    for missing in (None, 0, ["[verse]"]):
        with pytest.raises(TypeError, match="string"):
            build_songplanner_known_lyrics_payload(
                idea="ballad",
                genre_hint="",
                lyrics=missing,
                duration=60,
                duration_headroom=1.5,
                seed=3,
                prefix="mvp/songs/x",
            )


def test_songplanner_builders_differ_only_in_node_45_lyric_handling():
    shared = {
        "idea": "ballad",
        "genre_hint": "rock",
        "duration": 60,
        "duration_headroom": 1.5,
        "seed": 3,
        "prefix": "mvp/songs/pair",
    }
    invented = build_songplanner_invented_payload(**shared)
    known = build_songplanner_known_lyrics_payload(lyrics="[verse]\nKnown words", **shared)

    assert invented["45"]["inputs"]["lyrics"] == ["55", 1]
    assert known["45"]["inputs"]["lyrics"] == "[verse]\nKnown words"
    invented["45"]["inputs"].pop("lyrics")
    known["45"]["inputs"].pop("lyrics")
    assert invented == known


#: SHA-256 of the canonical JSON of each SongPlanner payload as it was built *before* the
#: duration headroom existed, at commit b08df47. Recorded rather than recomputed on purpose:
#: a headroom of 1.0 has to reproduce the pre-headroom payload byte for byte, and only a
#: digest taken from the old code can prove that. The invented case deliberately passes an
#: integer duration and the known-lyrics case a float, because `duration * 1.0` would turn
#: `90` into `90.0` and change the bytes on the wire while every `==` assertion still passed.
PRE_HEADROOM_PAYLOAD_DIGESTS = {
    "invented": "b2a336c306af3bef831d454174cfc9e19d44b0b5642be9438a72f27be5ab86bc",
    "known-lyrics": "013cc12ce86290e69597753d70201ef29c13a978c19d0ca8b7bd4cc266906585",
}


def headroom_builders(duration, headroom):
    """Both variants at one duration and headroom, unbuilt.

    Callables rather than payloads so a refusal test can name which variant raised: building
    the pair eagerly would let the invented builder's `ValueError` stand in for both. Keyed as
    `PRE_HEADROOM_PAYLOAD_DIGESTS` is. The known-lyrics variant takes `float(duration)`
    deliberately — one integer target and one float target across the pair, because the
    `1.0` guarantee is about wire bytes and `90` and `90.0` are different bytes.
    """
    shared = {
        "idea": "a slow-burn desert rock anthem",
        "genre_hint": "desert rock",
        "duration_headroom": headroom,
        "seed": 41,
        "prefix": "mvp/songs/night-signal",
    }
    return {
        "invented": lambda: build_songplanner_invented_payload(duration=duration, **shared),
        "known-lyrics": lambda: build_songplanner_known_lyrics_payload(
            duration=float(duration), lyrics="[verse]\nKnown words", **shared
        ),
    }


def headroom_payload_pair(duration, headroom):
    """Both variants built, keyed as `PRE_HEADROOM_PAYLOAD_DIGESTS` is."""
    return {label: build() for label, build in headroom_builders(duration, headroom).items()}


def test_songplanner_headroom_raises_the_encoders_ceiling_and_never_the_planners_target():
    """The whole point: one number is a target to write to, the other a ceiling to stop at.

    `M3SongPlanner.duration_seconds` is how long a song to write; `max_duration` is the
    encoder's latent ceiling, which the song may finish before. Passing the target to both —
    which is what the creator's own export does — leaves a song whose lyrics run long no room
    for its ending. Asserted on both variants because a cover's supplied lyric sheet can
    overrun exactly as easily as an invented one.
    """
    for headroom, expected_ceiling in ((1.0, 60.0), (1.5, 90.0), (2.0, 120.0), (6.0, 360.0)):
        for label, payload in headroom_payload_pair(60.0, headroom).items():
            assert payload["55"]["inputs"]["duration_seconds"] == 60.0, (label, headroom)
            assert payload["45"]["inputs"]["max_duration"] == expected_ceiling, (label, headroom)


def test_songplanner_headroom_of_one_reproduces_the_pre_headroom_payload_byte_for_byte():
    """A headroom of 1.0 must be inert: identical bytes, not merely equal numbers.

    Compared as a digest of canonical JSON rather than field by field, because the failure
    this guards against is a `90` that became `90.0` — same value, different wire bytes, and
    `assert x == 90` cannot see it.
    """
    for label, payload in headroom_payload_pair(90, 1.0).items():
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        assert hashlib.sha256(blob).hexdigest() == PRE_HEADROOM_PAYLOAD_DIGESTS[label], label


def test_songplanner_refuses_a_headroom_that_pushes_the_ceiling_past_the_schema():
    """Named, not clamped: a quietly shortened ceiling is the truncation this exists to stop.

    300 s is a duration the route accepts and 1.5 is the default headroom, so the product is
    reachable from an entirely valid-looking request — which is why the refusal has to name
    both numbers and the ceiling rather than the request as a whole.
    """
    for label, build in headroom_builders(300.0, 1.5).items():
        with pytest.raises(ValueError, match="max_duration=450") as raised:
            build()
        assert "360" in str(raised.value), label
        assert "300" in str(raised.value), label
        assert "1.5" in str(raised.value), label

    # The last product the encoder accepts is exactly the ceiling, and it is not refused.
    at_ceiling = headroom_payload_pair(240.0, 1.5)
    assert at_ceiling["invented"]["45"]["inputs"]["max_duration"] == MUSIC3_MAX_DURATION_SECONDS
    assert at_ceiling["invented"]["55"]["inputs"]["duration_seconds"] == 240.0


def test_songplanner_refuses_a_headroom_below_one():
    """Below the target the ceiling can only truncate, which is the bug inverted."""
    for headroom in (0.99, 0.5, 0.0):
        for label, build in headroom_builders(60.0, headroom).items():
            with pytest.raises(ValueError, match="below the song asked for") as raised:
                build()
            assert f"{headroom:g}" in str(raised.value), (label, headroom)


def test_headroom_bounds_are_derived_from_the_recorded_node_schema():
    """Both ends of the headroom bound are node facts, not taste, so read them off the node.

    `MUSIC3_MAX_DURATION_SECONDS` is the encoder's own maximum, and the widest headroom worth
    offering is that maximum over the planner's duration floor: above it no duration this
    route accepts could produce a legal ceiling. Re-derived here so a re-recorded fixture
    cannot move the schema without moving the constants with it.
    """
    schema = recorded_object_info()
    encoder_min, encoder_max = preflight.numeric_bounds(
        schema["MiniMaxMusic3TextEncode"]["input"]["required"]["max_duration"]
    )
    planner_min, _ = preflight.numeric_bounds(
        schema["M3SongPlanner"]["input"]["required"]["duration_seconds"]
    )

    assert MUSIC3_MAX_DURATION_SECONDS == encoder_max
    assert SONGPLANNER_MAX_DURATION_HEADROOM * planner_min == MUSIC3_MAX_DURATION_SECONDS
    # The encoder's floor is far below anything the planner's 30 s minimum can reach, which is
    # why only the ceiling needs guarding.
    assert encoder_min < planner_min
    # The creator's documented rule, and the reason it is a default rather than a constant.
    assert SONGPLANNER_DEFAULT_DURATION_HEADROOM == 1.5

    field = SongPlannerRequest.model_fields["duration_headroom"]
    bounds = {item.__class__.__name__: item for item in field.metadata}
    assert field.default == SONGPLANNER_DEFAULT_DURATION_HEADROOM
    assert bounds["Ge"].ge == 1.0
    assert bounds["Le"].le == SONGPLANNER_MAX_DURATION_HEADROOM
    # Most of the value of this change is that the two inputs stop being interchangeable in
    # the reader's head, so the field has to say which one it moves and which one it does not.
    assert "max_duration" in field.description
    assert "M3SongPlanner.duration_seconds" in field.description


def test_songplanner_variants_validate_separately_against_recorded_object_info():
    object_info = json.loads(
        Path("tests/fixtures/object_info.json").read_text(encoding="utf-8")
    )

    for label, payload in preflight_songplanner.audit_payloads():
        assert preflight.validate(label, payload, object_info) == []


def test_payload_validation_rejects_numeric_values_outside_the_schema_range():
    """The guard that would have caught duration=16 offline instead of as a live 502."""
    object_info = json.loads(
        Path("tests/fixtures/object_info.json").read_text(encoding="utf-8")
    )

    below = build_songplanner_invented_payload(
        idea="too short", genre_hint="", duration=16, duration_headroom=1.0, seed=0, prefix="range"
    )
    above = build_songplanner_invented_payload(
        idea="too long", genre_hint="", duration=301, duration_headroom=1.0, seed=0, prefix="range"
    )

    low_problems = preflight.validate("below", below, object_info)
    high_problems = preflight.validate("above", above, object_info)
    assert any(
        "duration_seconds=16" in problem and "below the schema minimum 30.0" in problem
        for problem in low_problems
    ), low_problems
    assert any(
        "duration_seconds=301" in problem and "above the schema maximum 300.0" in problem
        for problem in high_problems
    ), high_problems

    # M3SongPlanner's seed is 32-bit even though the encoder and sampler seeds in
    # the same payload are 64-bit, so the narrow bound must be caught per node.
    wide_seed = build_songplanner_invented_payload(
        idea="wide seed",
        genre_hint="",
        duration=120,
        duration_headroom=1.0,
        seed=2**32,
        prefix="range",
    )
    seed_problems = preflight.validate("seed", wide_seed, object_info)
    assert [
        problem for problem in seed_problems if "M3SongPlanner" in problem
    ] == seed_problems, seed_problems
    assert any(
        "seed=4294967296" in problem and "above the schema maximum 4294967295" in problem
        for problem in seed_problems
    ), seed_problems


def test_numeric_bounds_reads_the_schema_and_ignores_step():
    object_info = json.loads(
        Path("tests/fixtures/object_info.json").read_text(encoding="utf-8")
    )
    planner = object_info["M3SongPlanner"]["input"]["required"]

    assert preflight.numeric_bounds(planner["duration_seconds"]) == (30.0, 300.0)
    # A combo spec carries no numeric bounds, and off-step values are not a problem:
    # ComfyUI only rejects min/max violations.
    assert preflight.numeric_bounds(planner["text_encoder"]) == (None, None)
    off_step = build_songplanner_invented_payload(
        idea="off step", genre_hint="", duration=37.5, duration_headroom=1.5, seed=0, prefix="range"
    )
    assert preflight.validate("off-step", off_step, object_info) == []


def test_songplanner_request_bounds_equal_the_recorded_node_schema():
    """The drift that caused the original bug: route constants versus node schema.

    `SongPlannerRequest`'s numbers exist only because `M3SongPlanner` declares them,
    so reading both and comparing means neither the hand-written constant nor a
    re-recorded fixture can move without the other.
    """
    planner = recorded_object_info()["M3SongPlanner"]["input"]["required"]

    def route_bound(field: str, kind: str):
        for item in SongPlannerRequest.model_fields[field].metadata:
            if item.__class__.__name__ == kind:
                return getattr(item, kind.lower())
        raise AssertionError(f"SongPlannerRequest.{field} has no {kind} bound")

    for field, schema_input in (("duration", "duration_seconds"), ("seed", "seed")):
        minimum, maximum = preflight.numeric_bounds(planner[schema_input])
        assert (minimum, maximum) != (None, None), schema_input
        assert route_bound(field, "Ge") == minimum, field
        assert route_bound(field, "Le") == maximum, field


def test_every_numeric_songplanner_input_resolves_a_schema_bound():
    """A bound that disappears upstream must fail loudly, not pass vacuously.

    `numeric_bounds` returning `(None, None)` silently disables the range check for
    that input, which is indistinguishable from a clean audit — so every numeric
    literal in both variants has to resolve at least one bound.
    """
    object_info = recorded_object_info()

    for label, payload in preflight_songplanner.audit_payloads():
        assert preflight.unbounded_numeric_inputs(label, payload, object_info) == []


def test_every_builder_payload_is_range_checked_against_the_fixture():
    """The guard must not be SongPlanner-only, or the closed gap is only closed here."""
    object_info = recorded_object_info()
    uncovered: set[str] = set()

    for label, payload in every_builder_payload():
        covered = {
            node_id: node
            for node_id, node in payload.items()
            if node["class_type"] in object_info
        }
        uncovered |= {
            node["class_type"] for node in payload.values() if node["class_type"] not in object_info
        }
        # `graph=payload` because `covered` is a subset: a link into a node whose class is
        # not recorded is a real link, not a dangling one.
        assert preflight.validate(label, covered, object_info, graph=payload) == [], label

    assert uncovered == UNRECORDED_CLASSES


def test_range_check_rejects_a_fractional_value_on_an_int_input():
    """`steps=1.5` sits inside 1-10000 but is not an integer, and INT says it must be."""
    object_info = recorded_object_info()
    payload = build_music3_payload(caption="c", lyrics="l", duration=120, seed=0, prefix="p")
    payload["50"]["inputs"]["steps"] = 1.5

    problems = preflight.validate("fractional", payload, object_info)

    assert any(
        "steps=1.5" in problem and "fractional but the schema declares INT" in problem
        for problem in problems
    ), problems
    # A whole-number float is what ComfyUI itself accepts for an INT, so it is not a defect.
    payload["50"]["inputs"]["steps"] = 30.0
    assert preflight.validate("integral", payload, object_info) == []


def test_songplanner_model_files_are_present_in_recorded_combos():
    object_info = json.loads(
        Path("tests/fixtures/object_info.json").read_text(encoding="utf-8")
    )
    expectations = (
        ("M3SongPlanner", "text_encoder", "gemma_3_12B_it_fp4_mixed.safetensors"),
        ("UNETLoader", "unet_name", "minimax_music3_dit_fp16.safetensors"),
        ("CLIPLoader", "clip_name", "minimax_music3_text_encoder_bf16.safetensors"),
        ("VAELoader", "vae_name", "minimax_music3_dav.safetensors"),
    )
    for class_type, input_name, filename in expectations:
        spec = object_info[class_type]["input"]["required"][input_name]
        options = preflight.combo_options(spec)
        assert filename in options, f"{class_type}.{input_name}: {filename}"


def test_songplanner_source_exports_are_not_mutated():
    for export, expected in SONGPLANNER_EXPORTS.items():
        digest = hashlib.sha256(export.read_bytes()).hexdigest()
        assert digest == expected, export


def test_multiview_payload_uses_uploaded_character_and_quadview_lora():
    payload = build_multiview_payload(
        image_name="mvp_character.png",
        prompt="Preserve this character in close-up, front, side and back views",
        seed=88,
        prefix="mvp/multiview/lead",
    )

    assert payload["182"]["inputs"]["image"] == "mvp_character.png"
    assert payload["127"]["inputs"]["lora_name"] == r"krea2\QuadView_krea2_v1.safetensors"
    assert payload["119"]["inputs"]["prompt"].startswith("Preserve this character")
    assert payload["53"]["inputs"]["seed"] == 88
    assert payload["29"]["inputs"]["filename_prefix"] == "mvp/multiview/lead"


#: The creator's saved character-sheet graph, byte-identical to the file on the ComfyUI
#: machine, kept here so the claims this suite makes about it are checkable offline.
#:
#: **Editor format, not API format.** It carries `nodes`/`links`/`groups` and a `mode` on
#: every node, which is exactly why it is the evidence for "those two samplers are
#: bypassed" — an API export has no `mode` field at all and could not answer the question.
#: It is reference evidence and must never be submitted to `/prompt`.
KREA_SHEET_EXPORT = REFERENCE_EXPORTS / "krea2-charactersheet-user-export.json"

#: ComfyUI's `mode` values, for the two this graph uses. 0 runs; 4 is bypassed — the node
#: is skipped and its input is passed through to whatever it fed.
NODE_MODE_ACTIVE = 0
NODE_MODE_BYPASSED = 4

#: KSampler's widget order in a saved graph, which is what makes the pipeline
#: documentation's "8-step euler at CFG 0.3" a misreading rather than a disagreement:
#: `widgets_values[3]` is cfg and `widgets_values[6]` is denoise, so a 0.3 read as CFG is
#: the denoise value pulled three slots early.
KSAMPLER_WIDGETS = ("seed", "control_after_generate", "steps", "cfg", "sampler_name",
                    "scheduler", "denoise")


def krea_sheet_nodes() -> dict[int, dict]:
    """Every node of the creator's saved sheet graph, keyed by its id."""
    graph = json.loads(KREA_SHEET_EXPORT.read_text(encoding="utf-8"))
    return {node["id"]: node for node in graph["nodes"]}


def ksampler_settings(node: dict) -> dict:
    """One saved KSampler's widgets, read by position into names."""
    assert node["type"] == "KSampler", node["type"]
    return dict(zip(KSAMPLER_WIDGETS, node["widgets_values"], strict=True))


def test_the_creator_sheet_export_is_not_mutated():
    """The evidence the pin below rests on, held byte-identical to the creator's file."""
    digest = hashlib.sha256(KREA_SHEET_EXPORT.read_bytes()).hexdigest()

    assert digest == "ac36130b6be2caa4b624cf2b4739a142c3150a2f7001b7dcba49ee0a99bee399"


def test_the_sheet_is_one_pass_and_the_creator_graph_says_why():
    """Read this before "fixing" the two sampling passes `Music-Video.md` says are missing.

    The pipeline documentation beside the graph says the sheet is *"Three KSamplers:
    10-step euler for the initial sheet, 8-step euler at CFG 0.3 for the refine pass,
    8-step res_multistep for the final output."* Every clause of that is contradicted by
    the file it describes, and this test asserts the contradiction against the file rather
    than restating it in prose, so the next reader who tries the change fails here and is
    told what the evidence is.

    The three passes were built on 2026-08-18 and reverted the same day. Two of the
    samplers are bypassed and are not in the sheet path at all — they generate an optional
    *character portrait* that can become the sheet's input image — and stacking them after
    the sheet would have needed a denoise value no source specifies. That is a third
    combination nobody has rendered, shipped as a default, changing every reference sheet
    in the project on the strength of a prose error. It is the same hazard that made the
    H3 sampling profiles reproduce whole evidenced bundles instead of blending them.
    """
    nodes = krea_sheet_nodes()
    samplers = {node_id: node for node_id, node in nodes.items() if node["type"] == "KSampler"}

    # Three exist. Exactly one of them runs.
    assert sorted(samplers) == [53, 146, 172]
    assert nodes[53]["mode"] == NODE_MODE_ACTIVE
    assert nodes[146]["mode"] == NODE_MODE_BYPASSED
    assert nodes[172]["mode"] == NODE_MODE_BYPASSED

    # And the bypassed pair is not merely disabled, it is somewhere else: the sheet's own
    # groups sit at positive x, and those two are a thousand units to the left, in a group
    # the creator titled "2nd Sampling" — the second pass of the *portrait* generator.
    groups = json.loads(KREA_SHEET_EXPORT.read_text(encoding="utf-8"))["groups"]
    titles = {group["title"]: group["bounding"][0] for group in groups}
    assert "2nd Sampling" in titles
    assert titles["2nd Sampling"] < 0
    for sheet_group in ("CharSheet - Sampling", "CharSheet - Condition", "CharSheet - MODELS"):
        assert titles[sheet_group] > 0, sheet_group

    # The adapter is node 53, value for value, read out of the file rather than retyped.
    sheet = ksampler_settings(nodes[53])
    payload = build_multiview_payload(image_name="a.png", prompt="p", seed=7, prefix="p")
    sampler = payload["53"]["inputs"]

    assert sampler["steps"] == sheet["steps"] == MULTIVIEW_STEPS
    assert sampler["cfg"] == sheet["cfg"] == MULTIVIEW_CFG
    assert sampler["sampler_name"] == sheet["sampler_name"] == MULTIVIEW_SAMPLER
    assert sampler["scheduler"] == sheet["scheduler"] == MULTIVIEW_SCHEDULER
    assert sampler["denoise"] == sheet["denoise"] == MULTIVIEW_DENOISE

    # One pass in the payload, because one pass is what the sheet path has.
    assert [node["class_type"] for node in payload.values()].count("KSampler") == 1

    # The prose's "CFG 0.3" is the bypassed refine's *denoise*; its cfg is 1.0 like every
    # other sampler here. Asserted so a reader who trusts the prose sees where 0.3 lives.
    refine = ksampler_settings(nodes[172])
    assert refine["cfg"] == 1.0
    assert refine["denoise"] == 0.3
    # And `res_multistep` is the *first* of the bypassed pair, not a final pass: node 172
    # takes its latent straight from node 146, so 146 runs before it.
    assert ksampler_settings(nodes[146])["sampler_name"] == "res_multistep"
    latent_link = next(item for item in nodes[172]["inputs"] if item["name"] == "latent_image")
    links = {link[0]: link for link in json.loads(KREA_SHEET_EXPORT.read_text(encoding="utf-8"))["links"]}
    assert links[latent_link["link"]][1] == 146


#: The multiview payload exactly as it shipped before objects could be promoted.
#:
#: This story added a refine and a final pass and then took them out again. A digest is the
#: cleanest statement that the revert was complete: not "three samplers became one" but
#: "every byte of the submitted graph is what it was", which also covers the node ids, the
#: LoRA names, the resize and the link topology that the revert had no business touching.
SHIPPED_MULTIVIEW_PAYLOAD_DIGEST = (
    "81a440e23e523b9eaee5fe05c45923d22e5f294618461fd57df5672cede09ca2"
)


def test_the_multiview_payload_is_byte_identical_to_what_shipped():
    """Recorded from `git show HEAD:...workflows.py` before the passes were added."""
    payload = build_multiview_payload(
        image_name="mvp_character.png", prompt="a prompt", seed=88, prefix="mvp/multiview/lead"
    )
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    assert hashlib.sha256(serialized.encode()).hexdigest() == SHIPPED_MULTIVIEW_PAYLOAD_DIGEST


#: Node classes that would only ever be in a sheet graph to take the sheet apart.
#:
#: The prohibition is on *assuming a panel count*, and the way that assumption reaches a
#: payload is a node that crops, splits or indexes the decoded sheet. Named as classes
#: rather than checked as a word, because "the builder emits no cropper" is a fact about
#: the graph that gets submitted, and a comment cannot satisfy it.
PANEL_SPLITTING_CLASSES = frozenset({
    "ImageCrop", "ImageFromBatch", "ImageBatch", "RepeatImageBatch", "LatentFromBatch",
    "LatentCrop", "LatentBatch", "ImageSplit", "ImageBatchGet", "SplitImageWithAlpha",
})


def test_multiview_graph_neither_splits_nor_batches_the_sheet():
    """A LoRA called QuadView, asked for four views, returned six.

    So the sheet's panel count is not knowable in advance and the graph must not act as
    though it were. One image in, one image out, batch size 1: nothing crops it, nothing
    indexes into a batch, and the save writes the sheet whole.
    """
    payload = build_multiview_payload(
        image_name="a.png", prompt="p", seed=0, prefix="mvp/multiview/ship"
    )
    classes = {node["class_type"] for node in payload.values()}

    assert classes & PANEL_SPLITTING_CLASSES == set()
    assert payload["135"]["inputs"]["batch_size"] == 1
    assert [node["class_type"] for node in payload.values()].count("SaveImage") == 1
    assert payload["29"]["inputs"]["images"] == ["54", 0]


def test_multiview_latent_size_matches_the_creator_file_not_its_note():
    """1536x1024, which is what the graph carries and 1.57 MP rather than the noted 1 MP.

    The creator's Resolution note says "1MP is the sweet spot"; the file's own
    EmptySD3LatentImage is 1536x1024. Pinned here so the discrepancy is a recorded
    decision — the file won — rather than something the next reader re-litigates from the
    note and silently changes under every sheet already in a manifest.
    """
    payload = build_multiview_payload(image_name="a.png", prompt="p", seed=0, prefix="p")

    assert payload["135"]["inputs"]["width"] == 1536
    assert payload["135"]["inputs"]["height"] == 1024


#: A number of panels, poses, angles or views, written as a digit or as a word.
#:
#: "four-panel", "4 panel", "six views", "three poses" — the shapes a count reaches a prompt
#: or a constant in. "three-quarter view" does not match and must not: it is an angle's name,
#: not a tally, and both templates use it.
PANEL_COUNT_PATTERN = re.compile(
    r"(?i)\b(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)[\s_-]*"
    r"(?:panel|pose|angle|view)s?\b"
)


def executable_source(path: Path) -> str:
    """`path` with its comments and docstrings removed, so only what *runs* is scanned.

    A comment is where the reasoning lives, and the reasoning here is largely *about* the
    counts that must not be asserted — the probe that asked for four and got six is worth
    writing down in every file that was changed because of it. Stripping them is what keeps
    this guard about behaviour instead of about vocabulary.
    """
    if path.suffix != ".py":
        return "\n".join(
            line for line in path.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("//")
        )
    text = path.read_text(encoding="utf-8")
    docstrings = set()
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if ast.get_docstring(node, clean=False) is None:
            continue
        docstrings.update(range(node.body[0].lineno, node.body[0].end_lineno + 1))
    return "\n".join(
        token.string
        for token in tokenize.generate_tokens(io.StringIO(text).readline)
        if token.type != tokenize.COMMENT and token.start[0] not in docstrings
    )


def test_nothing_that_runs_states_a_number_of_panels():
    """Not in code, not in a prompt, not in a test — the one thing this feature may not know.

    A LoRA named QuadView, handed a prompt asking for four views, returned six. Any code
    that had counted, cropped or indexed panels would have been wrong on the first object
    ever promoted, and the *prompt* is the place a count gets written without feeling like
    an assumption at all: "a clean four-panel character sheet" reads as direction and is
    actually a prediction about output nobody has seen.

    Scanned across the application and its tests rather than at the multiview builder,
    because the sheet is read by the reference path and the H3 adapter too, and a count
    asserted anywhere is a count this feature has to live with.
    """
    # The scanner must be able to fail. A negative existential that silently stopped
    # matching would pass forever, on a codebase that had reintroduced every count.
    #
    # The number and the noun are separate literals, joined only at runtime: this file is
    # inside the scan, and a probe written out whole would be a count in a test — the exact
    # thing being forbidden — reported against this very line.
    for number, noun in (("four", "-panel"), ("six", " views"), ("4", " panels")):
        assert PANEL_COUNT_PATTERN.search(f"a clean {number}{noun} sheet"), f"{number}{noun}"
    # And must not fire on the angle both templates name.
    assert not PANEL_COUNT_PATTERN.search("a three-quarter view of the whole object")

    scanned = sorted(
        path
        for directory in ("src", "tests")
        for suffix in ("*.py", "*.js", "*.html")
        for path in (REPO_ROOT / directory).rglob(suffix)
        if "__pycache__" not in path.parts
    )
    assert len(scanned) > 20, "the scan found almost nothing; the walk is broken"

    counts = [
        f"{path.relative_to(REPO_ROOT)}: {found.group(0)!r}"
        for path in scanned
        for found in [PANEL_COUNT_PATTERN.search(executable_source(path))]
        if found
    ]
    assert counts == [], counts


def test_h3_director_payload_is_self_contained_and_semantically_patched():
    payload = build_h3_director_payload(
        timeline_data='{"segments":[{"id":"shot_1","start":0,"length":120,"prompt":"A singer turns toward camera"}]}',
        duration=5.0,
        requested_frames=120,
        seed=42,
        width=1344,
        height=768,
        steps=20,
        prefix="music-video-producer/project/shots/shot_1",
    )

    director = payload["2343"]["inputs"]
    assert director["clip"] == ["mvp:clip", 0]
    assert director["vae"] == ["mvp:video_vae", 0]
    assert director["audio_vae"] == ["mvp:audio_vae", 0]
    assert director["custom_width"] == 1344
    assert director["custom_height"] == 768
    assert director["duration_frames"] == 120
    assert '"reference_mode":"OFF"' in director["timeline_data"]
    assert payload["2347"]["inputs"]["noise_seed"] == 42
    assert payload["2346"]["inputs"]["steps"] == 20
    assert payload["2348"]["inputs"]["filename_prefix"].endswith("shot_1")
    assert payload["2351:2174"]["inputs"]["vae"] == ["mvp:video_vae", 0]
    assert payload["2351:2175"]["inputs"]["vae"] == ["mvp:audio_vae", 0]
    assert all(node["class_type"] != "Power Lora Loader (rgthree)" for node in payload.values())


def test_h3_reference_payload_maps_multiple_subjects_environment_and_shared_audio():
    references = [
        {"kind": "picture", "file": "F:/project/lead.png", "label": "lead vocalist"},
        {"kind": "picture", "file": "F:/project/duet.png", "label": "duet vocalist"},
        {"kind": "picture", "file": "F:/project/stage.png", "label": "chorus environment"},
        {"kind": "audio", "file": "F:/project/song.flac", "label": "master song"},
    ]
    payload = build_h3_reference_payload(
        prompt=(
            "<Picture 1> and <Picture 2> perform a duet in <Picture 3>, "
            "lip synced to <Audio 1>."
        ),
        references=references,
        duration=8,
        width=1280,
        height=720,
        steps=20,
        seed=42,
        prefix="mvp/duet-chorus",
    )

    media = json.loads(payload["mvp:references"]["inputs"]["media_state"])
    conditioner = payload["mvp:condition"]["inputs"]
    assert [item["kind"] for item in media] == ["picture", "picture", "picture", "audio"]
    assert conditioner["ref_images.ref_image_0"] == ["mvp:split", 0]
    assert conditioner["ref_images.ref_image_2"] == ["mvp:split", 2]
    assert conditioner["ref_audios.ref_audio_0"] == ["mvp:split", 15]
    # 8 s plus the over-render margin: 8.5 s -> 204 frames -> 209 on the 17k+5 grid.
    assert conditioner["length"] == 209
    assert payload["mvp:scheduler"]["inputs"]["steps"] == 20
    assert payload["mvp:save"]["inputs"]["filename_prefix"] == "mvp/duet-chorus"


def h3_reference_payload(references: list[dict], **overrides) -> dict:
    """One reference payload with the boring arguments filled in.

    `steps` is left unset rather than pinned at 20, which is what it used to be: a helper
    that always sent 20 would hand the turbo profile the default profile's step count and
    quietly make every profile test below check the wrong graph. For the default profile
    the resolved value is still 20, so nothing that used this before has moved.
    """
    arguments = {
        "prompt": "p",
        "references": references,
        "duration": 8,
        "width": 1280,
        "height": 720,
        "steps": None,
        "seed": 0,
        "prefix": "p",
    }
    return build_h3_reference_payload(**{**arguments, **overrides})


def h3_references(kind: str, count: int) -> list[dict]:
    return [{"kind": kind, "file": f"F:/refs/{kind}-{index}"} for index in range(count)]


def test_h3_reference_payload_refuses_more_than_the_node_has_slots_for():
    """The per-kind limits are the node's autogrow maxima, and one past each is refused.

    Exactly at the limit must still build: a refusal that fires one reference early is
    the same defect wearing the opposite sign, and nothing else asserts the boundary.
    """
    for kind, limit in H3_REFERENCE_LIMITS.items():
        assert h3_reference_payload(h3_references(kind, limit))
        with pytest.raises(ValueError, match=f"at most {limit} {kind} references"):
            h3_reference_payload(h3_references(kind, limit + 1))
    # The message names the kind that overflowed and the number actually counted, quoting
    # the real limit rather than a hardcoded sentence.
    with pytest.raises(ValueError, match="at most 9 picture references per shot and this one has 10"):
        h3_reference_payload(h3_references("picture", 10))
    # Audio says what is being counted, because the route appends the master song as a
    # fourth audio reference: the Director attached three and needs to be told that.
    with pytest.raises(ValueError, match=r"has 4 \(the master song counts as one\)"):
        h3_reference_payload(h3_references("audio", 4))


def test_h3_reference_payload_refuses_a_reference_list_with_nothing_in_it():
    """The reference graph has no text-only mode: with no media it would sample noise."""
    with pytest.raises(ValueError, match="At least one H3 reference is required"):
        h3_reference_payload([])


def test_h3_reference_payload_refuses_a_reference_size_the_node_does_not_offer():
    for rejected in ("", "matched", "MAX", "2048"):
        with pytest.raises(ValueError, match="ref_image_size must be 'match' or 'max'"):
            h3_reference_payload(h3_references("picture", 1), ref_image_size=rejected)


def test_h3_reference_payload_refuses_a_kind_it_cannot_wire():
    """An unhandled kind must fail loudly rather than be dropped from the graph.

    Silently skipping it would submit a payload whose prompt still says `<Picture 2>`
    while only one picture is attached — a full-cost render of the wrong thing.
    """
    for unknown in ("image", "text", None):
        with pytest.raises(ValueError, match="Unsupported H3 reference kind"):
            h3_reference_payload([{"kind": unknown, "file": "F:/refs/x"}])


def test_h3_reference_payload_wires_a_video_and_its_paired_soundtrack():
    """The `video` kind, which no other test builds, and its optional paired audio."""
    paired = h3_reference_payload(
        [
            {"kind": "video", "file": "F:/refs/take.mp4", "has_audio": True},
            {"kind": "video", "file": "F:/refs/silent.mp4", "has_audio": False},
            {"kind": "video", "file": "F:/refs/own.mp4", "has_audio": True, "audio_mode": "separate"},
        ]
    )

    conditioner = paired["mvp:condition"]["inputs"]
    assert conditioner["ref_videos.ref_video_0"] == ["mvp:split", 9]
    assert conditioner["ref_videos.ref_video_1"] == ["mvp:split", 10]
    assert conditioner["ref_videos.ref_video_2"] == ["mvp:split", 11]
    # `paired` is the default reading of a video that has audio; `separate` and a silent
    # video both leave the soundtrack slot empty, so the video's own audio is not fed.
    assert conditioner["ref_video_audios.ref_video_audio_0"] == ["mvp:split", 12]
    assert "ref_video_audios.ref_video_audio_1" not in conditioner
    assert "ref_video_audios.ref_video_audio_2" not in conditioner


def test_h3_reference_payload_carries_the_reference_size_to_the_conditioner():
    for size in ("match", "max"):
        payload = h3_reference_payload(h3_references("picture", 1), ref_image_size=size)
        assert payload["mvp:condition"]["inputs"]["ref_image_size"] == size


# --- Frame selection -------------------------------------------------------------------
#
# The Director's pipeline sizes its frame with `ResolutionSelector` — megapixels, an aspect
# ratio and a multiple — and this adapter took two raw integers. The two are not the same
# control: the second can express a size the model was never tuned for, and it is how this
# project came to render everything at a number nobody chose.


def test_the_selector_reproduces_the_directors_own_measured_frame():
    """0.6 MP at 16:9 on a multiple of 32 is 1056x608, and that is measured, not derived.

    Node `115` of `h3-ltx25-user-export.json` carries those three values, and the
    2026-08-17 boundary run recorded the H3 base they produced on this machine as
    1056x608 — `33x32` by `19x32`. Pinned as a literal on purpose: the arithmetic is a
    reproduction of someone else's node, so the thing worth asserting is agreement with
    the observation, not agreement with the formula written next to it.
    """
    export = json.loads(
        (REFERENCE_EXPORTS / "h3-ltx25-user-export.json").read_text(encoding="utf-8")
    )
    selector = export["115"]
    assert selector["class_type"] == "ResolutionSelector"
    assert selector["inputs"] == {
        "aspect_ratio": H3_DEFAULT_ASPECT_RATIO,
        "megapixels": H3_DEFAULT_MEGAPIXELS,
        "multiple": H3_DEFAULT_MULTIPLE,
    }

    assert select_resolution(**selector["inputs"]) == (1056, 608)
    # And the defaults are those same three values, so a request that names no geometry
    # gets the frame the export selects rather than one that merely resembles it.
    assert select_resolution() == (1056, 608)
    # 0.64 MP and 1.737:1 — *larger* than 0.6 MP and *not* 16:9, because each axis rounds
    # independently. Asserted because the drift is the node's behaviour and a "fix" that
    # made the result closer to nominal would silently stop matching the Director's frame.
    assert 1056 * 608 == 642048
    assert (1056 / 32, 608 / 32) == (33.0, 19.0)


def test_the_selector_rounds_the_way_the_installed_node_rounds():
    """Line-for-line against `comfy_extras.nodes_resolution`, over every option.

    Reimplemented here rather than imported: the point is that two independent
    transcriptions of the same eight-line function agree, across the whole option list and
    both ends of the megapixel range, including the sizes whose half-cell lands on .5 and
    so depend on `round` being banker's rounding rather than `floor(x + 0.5)`.
    """
    for aspect_ratio, (w_ratio, h_ratio) in H3_ASPECT_RATIOS.items():
        for megapixels in (0.1, 0.6, 1.0, 2.5, 16.0):
            for multiple in (8, 32, 64):
                total_pixels = megapixels * 1024 * 1024
                scale = math.sqrt(total_pixels / (w_ratio * h_ratio))
                expected = (
                    round(w_ratio * scale / multiple) * multiple,
                    round(h_ratio * scale / multiple) * multiple,
                )
                floor, ceiling = H3_REFERENCE_DIMENSION_LIMITS
                if not all(floor <= axis <= ceiling for axis in expected):
                    # Legal for the selector, refused for H3 — 16 MP at 21:9 is 6208 px
                    # wide but 0.1 MP at 21:9 on a multiple of 64 is 0 px tall. Both are
                    # the "absurd megapixels" row, and both are refused before submission.
                    with pytest.raises(ValueError):
                        select_resolution(
                            megapixels=megapixels,
                            aspect_ratio=aspect_ratio,
                            multiple=multiple,
                        )
                    continue
                assert (
                    select_resolution(
                        megapixels=megapixels, aspect_ratio=aspect_ratio, multiple=multiple
                    )
                    == expected
                ), (aspect_ratio, megapixels, multiple)


def test_the_selector_refuses_what_the_nodes_will_not_take():
    """Every refusal happens locally, before any GPU time, naming the range it broke."""
    low, high = H3_MEGAPIXEL_LIMITS
    for megapixels in (low - 0.01, high + 0.01, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="egapixels"):
            select_resolution(megapixels=megapixels)
    # Both ends still build: a refusal that fires at the boundary is the same defect
    # wearing the opposite sign.
    assert select_resolution(megapixels=low, aspect_ratio="1:1 (Square)")
    assert select_resolution(megapixels=high, aspect_ratio="1:1 (Square)")

    minimum, maximum = H3_MULTIPLE_LIMITS
    for multiple in (minimum - 1, maximum + 1, 32.0, True):
        with pytest.raises(ValueError, match="multiple"):
            select_resolution(multiple=multiple)
    with pytest.raises(ValueError, match="Unknown aspect ratio"):
        select_resolution(aspect_ratio="16:9")


def test_the_h3_multiple_is_the_same_grid_the_ltx_boundary_repair_found():
    """32 twice, for two reasons, asserted equal rather than aliased.

    `LTX25_DIVISOR` is the LTX 2.5 VAE's spatial compression; `H3_DEFAULT_MULTIPLE` is
    what the Director's selector is set to and what `MiniMaxH3ReferenceToVideo` declares as
    its width/height step. They are the same number today and either could move alone, so
    the agreement is checked rather than assumed by sharing a name.
    """
    assert H3_DEFAULT_MULTIPLE == LTX25_DIVISOR == 32
    schema = recorded_object_info()["MiniMaxH3ReferenceToVideo"]["input"]["required"]
    assert schema["width"][1]["step"] == H3_DEFAULT_MULTIPLE
    assert schema["height"][1]["step"] == H3_DEFAULT_MULTIPLE


def test_the_reference_payload_selects_a_frame_or_is_given_one_but_never_both():
    """The four rows of the geometry matrix, at the builder.

    Explicit dimensions are byte-identical to what they were, which is what the pinned
    smoke and every existing test in this file depend on; an omission selects the
    Director's frame; and a request carrying both is refused rather than resolved by
    precedence, because a caller who sent two different intentions has not chosen either.
    """
    references = h3_references("picture", 1)
    arguments = {"prompt": "p", "references": references, "duration": 8, "seed": 0, "prefix": "p"}

    explicit = build_h3_reference_payload(**arguments, width=640, height=384)
    assert (
        explicit["mvp:condition"]["inputs"]["width"],
        explicit["mvp:condition"]["inputs"]["height"],
    ) == (640, 384)

    selected = build_h3_reference_payload(**arguments)
    assert (
        selected["mvp:condition"]["inputs"]["width"],
        selected["mvp:condition"]["inputs"]["height"],
    ) == (1056, 608)
    # Naming the selector's three inputs explicitly reaches the same frame, so the default
    # is those values rather than a hardcoded pair that happens to match them.
    named = build_h3_reference_payload(
        **arguments,
        megapixels=H3_DEFAULT_MEGAPIXELS,
        aspect_ratio=H3_DEFAULT_ASPECT_RATIO,
        multiple=H3_DEFAULT_MULTIPLE,
    )
    assert named == selected

    with pytest.raises(ValueError, match="not both"):
        build_h3_reference_payload(**arguments, width=640, height=384, megapixels=0.6)
    with pytest.raises(ValueError, match="not both"):
        build_h3_reference_payload(**arguments, width=640, height=384, multiple=32)
    # Half an explicit frame is not a frame: without this, `width=640` alone would take its
    # height from the selector and quietly render 640x608.
    with pytest.raises(ValueError, match="only width"):
        build_h3_reference_payload(**arguments, width=640)
    with pytest.raises(ValueError, match="only height"):
        build_h3_reference_payload(**arguments, height=384)
    with pytest.raises(ValueError, match="whole number of pixels"):
        build_h3_reference_payload(**arguments, width=640, height=16)


# --- The song audio window -------------------------------------------------------------
#
# `MiniMaxH3ReferenceToVideo` has no window input of any kind — it is not the Director node
# and takes no `start_second`/`end_second`. A reference audio's window is expressed in
# `MiniMaxH3MediaLoader`'s `media_state`, as `{"trim": {"start": s, "end": s}}` on the item,
# which `media_io.load_audio` slices the decoded waveform with. That is the only place a
# window can be said at all on this path.


def test_every_shot_gets_a_window_including_the_one_at_zero_seconds():
    """A shot hears its own seconds. **Every** shot, 0 s included.

    An earlier draft returned `None` at 0 s to keep that shot's payload byte-identical to the
    pre-fix one, on the recorded premise that the conditioner already trimmed a whole-file
    reference to the render window. That premise is false —
    `MiniMaxH3ReferenceToVideo._encode_ref_audio` VAE-encodes the entire waveform and never
    truncates — so silence at 0 s preserved the *defect* rather than the behaviour, and
    preserved it for the shot most likely to exist in a fresh project. Renegotiated by the
    Director on 2026-08-18; see the Spec Change Log in `spec-song-audio-window.md`.

    The loader drops a `start` of 0 (`_trim`'s `num()` keeps a value only when `v > 0`) but
    keeps the `end` beside it, and `_slice_audio` reads a missing start as `start or 0.0`, so
    `{"start": 0.0, "end": 3.75}` slices exactly `[0, 3.75]`. Sending it costs nothing and
    says what was meant.
    """
    assert song_audio_window(start=80, duration=4, song_duration=154) == {
        "start": 80,
        "end": 84,
    }
    assert song_audio_window(start=12, duration=3.75, song_duration=154) == {
        "start": 12,
        "end": 15.75,
    }
    assert song_audio_window(start=0, duration=3.75, song_duration=154) == {
        "start": 0,
        "end": 3.75,
    }
    assert song_audio_window(start=0.001, duration=1, song_duration=154) == {
        "start": 0.001,
        "end": 1.001,
    }
    # Nothing anywhere returns "no window". The 0 s shot being a special case is precisely
    # what was renegotiated away, so the absence of the special case is what is asserted --
    # not merely that 0 s happens to produce the right pair today.
    assert all(
        song_audio_window(start=start, duration=1, song_duration=154) is not None
        for start in (0, 0.0, 0.001, 1, 153)
    )


def test_a_window_past_the_end_of_the_song_is_refused_naming_both_numbers():
    """The node clamps; this refuses.

    `media_io._slice_audio` ends at `min(total, end * sample_rate)`, so a window running
    past the file is silently shortened and the render proceeds against fewer seconds than
    were asked for — with nothing anywhere recording the difference. Refusing costs
    nothing and happens before any GPU time.
    """
    with pytest.raises(ValueError) as past:
        song_audio_window(start=152, duration=3.75, song_duration=154)
    assert "155.75s" in str(past.value) and "154s" in str(past.value)

    with pytest.raises(ValueError) as longer:
        song_audio_window(start=0, duration=200, song_duration=154)
    assert "200s" in str(longer.value) and "154s" in str(longer.value)

    # Exactly at the end is not past it.
    assert song_audio_window(start=150, duration=4, song_duration=154) == {
        "start": 150,
        "end": 154,
    }
    # A Song that never recorded its length cannot be compared against, so the window is
    # still sent rather than the project being blocked over a field nobody filled in.
    assert song_audio_window(start=900, duration=4, song_duration=0) == {
        "start": 900,
        "end": 904,
    }
    for bad in (float("inf"), float("nan")):
        with pytest.raises(ValueError):
            song_audio_window(start=bad, duration=4, song_duration=154)


def test_the_reference_payload_carries_a_window_and_refuses_a_broken_one():
    """The `trim` reaches `media_state`, and a malformed one is refused rather than dropped.

    The loader's `_trim` silently discards anything it cannot read as a positive number, so
    a broken window does not fail — it renders the whole file and looks exactly like a shot
    that asked for none. That is this story's own failure mode, so it is caught where it is
    visible.
    """
    windowed = build_h3_reference_payload(
        prompt="p",
        references=[
            {"kind": "audio", "file": "F:/refs/song.mp3", "trim": {"start": 12.0, "end": 15.75}}
        ],
        duration=3.75,
        seed=0,
        prefix="p",
    )
    media = json.loads(windowed["mvp:references"]["inputs"]["media_state"])
    assert media[0]["trim"] == {"start": 12.0, "end": 15.75}

    for broken in (
        {"start": 12.0},
        {"end": 15.75},
        {"start": -1.0, "end": 15.75},
        {"start": 15.75, "end": 12.0},
        {"start": 12.0, "end": 12.0},
        {"start": "12", "end": "15.75"},
        [12.0, 15.75],
    ):
        with pytest.raises(ValueError):
            build_h3_reference_payload(
                prompt="p",
                references=[{"kind": "audio", "file": "F:/refs/song.mp3", "trim": broken}],
                duration=3.75,
                seed=0,
                prefix="p",
            )


# --- Sampling profiles -----------------------------------------------------------------
#
# Three evidenced bundles, each reproduced whole. The tests below pin the directions that
# matter: the default profile emits *exactly* what the adapter emitted before profiles
# existed, the turbo profile reproduces the Director's own H3 stage rather than borrowing
# half of it, and `turbo-references2v` reproduces the Director's canonical
# `MiniMaxH3Turbo References2V` graph — a different bundle from `turbo`, not a correction
# of it. Every profile's values are read out of the export they come from; none is typed
# in here, because a literal in a test only proves the test agrees with itself.

#: The default profile's payload for one fixed set of arguments, hashed at the commit
#: before sampling profiles existed (`7e25ad0`, the story's baseline) by running that
#: revision's `build_h3_reference_payload` and hashing
#: `json.dumps(payload, separators=(",", ":"))` — key order included, so a node inserted
#: anywhere in the graph changes it.
#:
#: A digest rather than a copied dict because the claim is *sameness with what shipped*,
#: and a literal expected payload written today would only prove the adapter agrees with
#: whatever it currently does. If this fails, the default profile's payload changed: that
#: is the thing the story promised would not happen, so re-deriving the digest is the
#: wrong fix unless the Director has renegotiated the promise.
#: Re-pinned 2026-08-19 for the over-render margin (see H3_TEXT_ONLY_PRE_KEYFRAME_DIGEST's
#: note): duration 8 s now renders 209 frames instead of 192, and nothing else moved.
H3_DEFAULT_PROFILE_DIGEST = "f87e6b97efa95e67dcbedcefef40b1cffb603b88958896193725b21f220bfe17"


def default_profile_payload(**overrides) -> dict:
    """The exact arguments `H3_DEFAULT_PROFILE_DIGEST` was taken over."""
    arguments = {
        "prompt": "<Picture 1> and <Video 1> in <Audio 1>",
        "references": [
            {"kind": "picture", "file": "F:/refs/lead.png", "label": "lead"},
            {"kind": "video", "file": "F:/refs/pan.mp4", "has_audio": True},
            {"kind": "audio", "file": "F:/refs/song.flac", "label": "song"},
        ],
        "duration": 8,
        "width": 1280,
        "height": 720,
        "steps": 20,
        "seed": 42,
        "prefix": "mvp/default-profile",
    }
    return build_h3_reference_payload(**{**arguments, **overrides})


def test_the_default_profile_emits_the_graph_the_adapter_shipped_before_profiles():
    """AC-1: a request that omits the profile builds today's payload, unchanged.

    Named and omitted must also agree, or "the default" would mean two things — the
    Director who never touches the field and the one who types `default` have to get the
    same graph.
    """
    payload = default_profile_payload()
    serialized = json.dumps(payload, separators=(",", ":"))

    assert hashlib.sha256(serialized.encode("utf-8")).hexdigest() == H3_DEFAULT_PROFILE_DIGEST
    assert default_profile_payload(profile="default") == payload
    assert default_profile_payload(profile=H3_DEFAULT_PROFILE) == payload
    # And the reason it is unchanged, said out loud: no LoRA node, and the shift node
    # still drawing straight from the loader.
    assert all(node["class_type"] != "LoraLoaderModelOnly" for node in payload.values())
    assert payload["mvp:shift"]["inputs"]["model"] == ["mvp:model", 0]


def test_the_default_profile_matches_the_audited_export_it_reproduces():
    """The evidence, read rather than restated: the export is why the default is the default.

    The export's `Power Lora Loader (rgthree)` holds a turbo LoRA switched **off**, and
    that deliberate off-switch is the whole justification for a 20-step profile with no
    LoRA. If someone ever switches it on in the file, this stops agreeing — which is the
    moment the default profile's evidence changed and the constant should be revisited.
    """
    export = json.loads(
        (REFERENCE_EXPORTS / "h3-ultra-references-user-export.json").read_text(encoding="utf-8")
    )
    scheduler = next(
        node for node in export.values() if node["class_type"] == "BasicScheduler"
    )["inputs"]
    sampler = next(
        node for node in export.values() if node["class_type"] == "KSamplerSelect"
    )["inputs"]
    power_lora = next(
        node for node in export.values() if node["class_type"] == "Power Lora Loader (rgthree)"
    )["inputs"]
    profile = H3_REFERENCE_PROFILES["default"]

    assert (profile.scheduler, profile.steps) == (scheduler["scheduler"], scheduler["steps"])
    assert profile.sampler == sampler["sampler_name"]
    assert profile.lora is None and profile.lora_strength is None
    lora_entries = [value for key, value in power_lora.items() if key.startswith("lora_")]
    assert lora_entries, power_lora
    assert all(entry["on"] is False for entry in lora_entries), lora_entries


def test_the_turbo_profile_matches_the_export_it_reproduces():
    """The turbo bundle read out of the audited export, exactly as the default one is.

    The Director's H3 stage is *in this repo*: `h3-ltx25-user-export.json` is the audited
    copy of the same saved workflow, and it carries the whole chain — node `127` loading
    `ref2va`, node `5959` `LoraLoaderModelOnly` at 0.7 taking that loader's output, and
    `BasicScheduler`/`KSamplerSelect` downstream of the shift.

    This exists because the first version of this test asserted the turbo values against
    literals typed into it while the default profile was checked against its export. That
    is the standard held for the profile carrying no LoRA and dropped for the one carrying
    a LoRA into a GPU job — the wrong way round. Nothing here is restated: the LoRA is
    located by *following the wiring* from the loader this adapter uses, so a second LoRA
    elsewhere in that 65-node graph cannot be mistaken for it.
    """
    export = json.loads(
        (REFERENCE_EXPORTS / "h3-ltx25-user-export.json").read_text(encoding="utf-8")
    )
    def only(candidates: list[str], what: str) -> str:
        """One node id, or a failure naming what was ambiguous rather than picking."""
        assert len(candidates) == 1, f"{what}: {candidates}"
        return candidates[0]

    def fed_by(node_id: str, socket: str = "model") -> list[str]:
        return [
            other_id
            for other_id, other in export.items()
            if other["inputs"].get(socket) == [node_id, 0]
        ]

    loader = only(
        [
            node_id
            for node_id, node in export.items()
            if node["class_type"] == "UNETLoader"
            and node["inputs"].get("unet_name")
            == "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
        ],
        "ref2va loaders",
    )
    lora = only(
        [node_id for node_id in fed_by(loader) if export[node_id]["class_type"] == "LoraLoaderModelOnly"],
        "LoRAs on the ref2va loader",
    )
    # Walk the model chain forward from the LoRA, so the scheduler and sampler come from
    # the branch this LoRA actually feeds. The export holds several of each — its LTX
    # stages sample too, and they are `euler` as well — so picking one by class alone
    # would be a coincidence dressed as evidence.
    reached: set[str] = set()
    frontier = fed_by(lora)
    while frontier:
        node_id = frontier.pop()
        if node_id in reached:
            continue
        reached.add(node_id)
        frontier += fed_by(node_id)
    scheduler = only(
        [node_id for node_id in reached if export[node_id]["class_type"] == "BasicScheduler"],
        "schedulers downstream of the LoRA",
    )
    sampler = only(
        [
            export[node_id]["inputs"]["sampler"][0]
            for node_id in fed_by(scheduler, socket="sigmas")
            if export[node_id]["class_type"] == "SamplerCustomAdvanced"
        ],
        "samplers on that scheduler's sigmas",
    )
    profile = H3_REFERENCE_PROFILES["turbo"]

    assert profile.lora == export[lora]["inputs"]["lora_name"]
    assert profile.lora_strength == export[lora]["inputs"]["strength_model"]
    assert profile.scheduler == export[scheduler]["inputs"]["scheduler"]
    assert profile.steps == export[scheduler]["inputs"]["steps"]
    assert profile.sampler == export[sampler]["inputs"]["sampler_name"]


def test_the_turbo_profile_reproduces_the_directors_stage_and_rewires_everything_after_it():
    """AC-2, and the wiring half of it.

    The values themselves are checked against the audited export by the test above; this
    one is about what the *builder* emits from them.

    The downstream assertion is written as "nothing but the LoRA reads the loader" rather
    than as "the shift node reads the LoRA", because a node added later that quietly kept
    reading `mvp:model` would satisfy the second and defeat the point: half the graph
    would sample the unpatched checkpoint.
    """
    payload = h3_reference_payload(h3_references("picture", 1), profile="turbo")

    lora = payload["mvp:lora"]
    assert lora["class_type"] == "LoraLoaderModelOnly"
    assert lora["inputs"]["lora_name"] == "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"
    assert lora["inputs"]["strength_model"] == 0.7
    assert lora["inputs"]["model"] == ["mvp:model", 0]
    assert payload["mvp:shift"]["inputs"]["model"] == ["mvp:lora", 0]
    assert payload["mvp:scheduler"]["inputs"]["scheduler"] == "beta"
    assert payload["mvp:scheduler"]["inputs"]["steps"] == 4
    assert payload["mvp:sampler"]["inputs"]["sampler_name"] == "euler"

    readers = {
        node_id
        for node_id, node in payload.items()
        for value in node["inputs"].values()
        if value == ["mvp:model", 0]
    }
    assert readers == {"mvp:lora"}, readers
    # The turbo graph is the default graph plus exactly one node; nothing else moved.
    assert set(payload) - set(h3_reference_payload(h3_references("picture", 1))) == {"mvp:lora"}


def test_the_turbo_references2v_profile_matches_the_export_it_reproduces():
    """The canonical `MiniMaxH3Turbo References2V` bundle, read out of the audited export.

    Nothing here is a literal: the export is walked from the UNET loader *the adapter
    itself names* — so a bundle sitting on some other checkpoint could not be mistaken for
    this one — through its `Power Lora Loader (rgthree)`, forward along the model chain to
    the one `BasicScheduler` it reaches, and on to the sampler feeding that scheduler's
    sigmas. The shipped `turbo` profile's first test typed its four values in while the
    default profile was checked against its file; that asymmetry was corrected, and this
    profile is written to the corrected standard from the start.

    The adapter's fixed sigma shift and sage-attention values are checked against this
    export too, because they are part of what makes it *this* graph rather than a graph
    with the same sampler.
    """
    export = json.loads(
        (REFERENCE_EXPORTS / "h3-turbo-references2v-user-export.json").read_text(
            encoding="utf-8"
        )
    )
    payload = h3_reference_payload(h3_references("picture", 1), profile="turbo-references2v")

    def only(candidates: list[str], what: str) -> str:
        """One node id, or a failure naming what was ambiguous rather than picking."""
        assert len(candidates) == 1, f"{what}: {candidates}"
        return candidates[0]

    def fed_by(node_id: str, socket: str = "model") -> list[str]:
        return [
            other_id
            for other_id, other in export.items()
            if other["inputs"].get(socket) == [node_id, 0]
        ]

    loader = only(
        [
            node_id
            for node_id, node in export.items()
            if node["class_type"] == "UNETLoader"
            and node["inputs"].get("unet_name") == payload["mvp:model"]["inputs"]["unet_name"]
        ],
        "loaders of the checkpoint this adapter loads",
    )
    power_lora = only(
        [
            node_id
            for node_id in fed_by(loader)
            if export[node_id]["class_type"] == "Power Lora Loader (rgthree)"
        ],
        "LoRA loaders on that checkpoint",
    )
    # One row, switched on. The default profile's export holds the same class with every
    # row switched *off*, which is why "enabled" is what is counted rather than "present":
    # the two exports differ in that flag and in nothing else about this node's shape.
    rows = [
        entry
        for key, entry in export[power_lora]["inputs"].items()
        if key.startswith("lora_") and isinstance(entry, dict)
    ]
    enabled = [entry for entry in rows if entry.get("on") is True]
    assert rows, export[power_lora]["inputs"]
    assert len(enabled) == 1, rows

    reached: set[str] = set()
    frontier = fed_by(power_lora)
    while frontier:
        node_id = frontier.pop()
        if node_id in reached:
            continue
        reached.add(node_id)
        frontier += fed_by(node_id)
    scheduler = only(
        [node_id for node_id in reached if export[node_id]["class_type"] == "BasicScheduler"],
        "schedulers downstream of the LoRA",
    )
    sampler = only(
        [
            export[node_id]["inputs"]["sampler"][0]
            for node_id in fed_by(scheduler, socket="sigmas")
            if export[node_id]["class_type"] == "SamplerCustomAdvanced"
        ],
        "samplers on that scheduler's sigmas",
    )
    profile = H3_REFERENCE_PROFILES["turbo-references2v"]

    assert profile.lora == enabled[0]["lora"]
    assert profile.lora_strength == enabled[0]["strength"]
    assert profile.scheduler == export[scheduler]["inputs"]["scheduler"]
    assert profile.steps == export[scheduler]["inputs"]["steps"]
    assert profile.sampler == export[sampler]["inputs"]["sampler_name"]

    # The two nodes the adapter emits on every profile, against this export's own numbers.
    shift = only(fed_by(power_lora), "nodes reading the LoRA loader")
    attention = only(fed_by(shift), "nodes reading the sigma shift")
    assert export[shift]["class_type"] == "MiniMaxH3SigmaShift"
    assert export[attention]["class_type"] == "PathchSageAttentionKJ"
    for name in ("shift_video", "shift_audio"):
        assert payload["mvp:shift"]["inputs"][name] == export[shift]["inputs"][name], name
    assert (
        payload["mvp:attention"]["inputs"]["sage_attention"]
        == export[attention]["inputs"]["sage_attention"]
    )


def test_the_turbo_references2v_profile_carries_its_bundle_through_the_adapters_lora_node():
    """AC: the export's values reach the graph — through this adapter's node, deliberately.

    The export carries its LoRA in a `Power Lora Loader (rgthree)`. This adapter does not
    reproduce that class, and the choice is asserted rather than left to be discovered:
    live `/object_info` declares no `lora_*` inputs on it at all — its required map is
    empty and only `model`/`clip` are optional — so a faithful copy would put the filename
    and strength where the pre-flight cannot check either, and the audit's "this LoRA is
    installed" claim would quietly become unverifiable. The bundle is therefore evidenced
    in its values and not in its wiring, which is a real cost and is stated in the
    builder's docstring.
    """
    payload = h3_reference_payload(h3_references("picture", 1), profile="turbo-references2v")
    profile = H3_REFERENCE_PROFILES["turbo-references2v"]

    lora = payload["mvp:lora"]
    assert lora["class_type"] == "LoraLoaderModelOnly"
    assert lora["inputs"]["lora_name"] == profile.lora
    assert lora["inputs"]["strength_model"] == profile.lora_strength
    assert lora["inputs"]["model"] == ["mvp:model", 0]
    assert payload["mvp:shift"]["inputs"]["model"] == ["mvp:lora", 0]
    assert payload["mvp:scheduler"]["inputs"]["scheduler"] == profile.scheduler
    assert payload["mvp:scheduler"]["inputs"]["steps"] == profile.steps
    assert payload["mvp:sampler"]["inputs"]["sampler_name"] == profile.sampler
    # Nothing but the LoRA still reads the raw loader, so no node samples the unpatched
    # checkpoint; and the graph is the default graph plus exactly that one node.
    readers = {
        node_id
        for node_id, node in payload.items()
        for value in node["inputs"].values()
        if value == ["mvp:model", 0]
    }
    assert readers == {"mvp:lora"}, readers
    assert set(payload) - set(h3_reference_payload(h3_references("picture", 1))) == {"mvp:lora"}
    assert all(node["class_type"] != "Power Lora Loader (rgthree)" for node in payload.values())


def test_no_two_sampling_profiles_are_the_same_bundle():
    """Three names over two configurations would be a rename presented as a third option.

    `turbo` and `turbo-references2v` are both the Director's and both turbo, and the whole
    reason both ship is that they are *different* bundles from different sources. If they
    ever collapse onto the same five values, one of them has been edited into the other and
    the Director is choosing between two labels for one graph.
    """
    bundles = {
        (profile.lora, profile.lora_strength, profile.scheduler, profile.sampler, profile.steps)
        for profile in H3_REFERENCE_PROFILES.values()
    }

    assert len(bundles) == len(H3_REFERENCE_PROFILES), sorted(H3_REFERENCE_PROFILES)
    # Each LoRA-carrying profile names its own file: two profiles sharing a LoRA would make
    # the pre-flight's per-file confirmation weaker than the number of variants suggests.
    loras = [profile.lora for profile in H3_REFERENCE_PROFILES.values() if profile.lora]
    assert len(set(loras)) == len(loras), loras


@pytest.mark.parametrize("name", sorted(H3_REFERENCE_PROFILES))
def test_each_profile_emits_its_own_lora_scheduler_sampler_and_step_default(name: str):
    """Every field of every profile reaches the payload, for whatever profiles exist.

    Parametrized over the mapping rather than over the two names known today: a third
    profile added as data would otherwise be data nothing checks, which is exactly how a
    configuration ends up looking evidenced while emitting something else.
    """
    profile = H3_REFERENCE_PROFILES[name]
    payload = h3_reference_payload(h3_references("picture", 1), profile=name)
    scheduler = payload["mvp:scheduler"]["inputs"]

    assert scheduler["scheduler"] == profile.scheduler
    assert scheduler["steps"] == profile.steps
    assert payload["mvp:sampler"]["inputs"]["sampler_name"] == profile.sampler
    if profile.lora is None:
        assert "mvp:lora" not in payload
        assert payload["mvp:shift"]["inputs"]["model"] == ["mvp:model", 0]
    else:
        assert payload["mvp:lora"]["inputs"]["lora_name"] == profile.lora
        assert payload["mvp:lora"]["inputs"]["strength_model"] == profile.lora_strength
        assert payload["mvp:shift"]["inputs"]["model"] == ["mvp:lora", 0]


@pytest.mark.parametrize("name", sorted(H3_REFERENCE_PROFILES))
def test_an_explicit_step_count_overrides_the_profiles_own(name: str):
    """The profile chooses the graph; the Director chooses the effort.

    Both directions matter: an explicit count must win over the profile's, and it must
    not change anything else the profile decided — a step count is not a licence to drop
    the LoRA or swap the sampler.
    """
    profile = H3_REFERENCE_PROFILES[name]
    explicit = h3_reference_payload(h3_references("picture", 1), profile=name, steps=37)
    implicit = h3_reference_payload(h3_references("picture", 1), profile=name)

    assert explicit["mvp:scheduler"]["inputs"]["steps"] == 37
    assert implicit["mvp:scheduler"]["inputs"]["steps"] == profile.steps
    # `steps=None` is what the route sends for an omitted count, and it must mean the
    # same thing as omitting the argument altogether.
    assert h3_reference_payload(h3_references("picture", 1), profile=name, steps=None) == implicit
    explicit["mvp:scheduler"]["inputs"]["steps"] = profile.steps
    assert explicit == implicit


def test_the_builder_refuses_a_profile_it_has_no_evidence_for():
    """An unknown profile is refused before a payload exists, not silently defaulted.

    Falling back to `default` would be the worst outcome available: a Director asking for
    turbo would get a 20-step no-LoRA render and a bill for it, with nothing saying so.
    """
    for unknown in ("turbo ", "TURBO", "fast", "", None, 4, 0.7):
        with pytest.raises(ValueError, match="Unknown H3 sampling profile"):
            h3_reference_payload(h3_references("picture", 1), profile=unknown)
    # Unhashable values are the ones that used to escape: `profile in {...}` raises
    # `TypeError`, which the route's `except ValueError` does not translate, so a caller
    # that is not the route got a 500 where every other bad input here gets a 422.
    for unhashable in (["turbo"], {"profile": "turbo"}, {"turbo"}):
        with pytest.raises(ValueError, match="Unknown H3 sampling profile"):
            h3_reference_payload(h3_references("picture", 1), profile=unhashable)
    # Refused *before* the payload is built: an unknown profile on an otherwise invalid
    # request still reports the profile, because nothing downstream ran.
    with pytest.raises(ValueError, match="Unknown H3 sampling profile"):
        h3_reference_payload([], profile="fast")


def test_a_profile_validates_its_own_fields_where_they_are_declared():
    """A profile that cannot produce a submittable graph fails on the line that wrote it.

    Every value below builds a payload ComfyUI rejects at `/prompt` validation, which the
    Director sees as an opaque 502 after the submission round-trip — at the end of the one
    path where the profile is meant to be the part nobody has to check. The whole point of
    profiles is that the configuration is trustworthy; a profile that can be nonsense is
    not one.
    """
    def profile(**overrides) -> H3SamplingProfile:
        fields = {
            "lora": "some.safetensors",
            "lora_strength": 0.7,
            "scheduler": "beta",
            "sampler": "euler",
            "steps": 4,
        }
        return H3SamplingProfile(**{**fields, **overrides})

    assert profile()  # the shape everything below breaks one field of

    for lora, strength in ((None, 0.7), ("some.safetensors", None)):
        with pytest.raises(ValueError, match="name a LoRA and its strength together"):
            profile(lora=lora, lora_strength=strength)
    # `True` is an `int` in Python and would sample exactly once, silently.
    for steps in (0, -1, 1.5, True, None, "4"):
        with pytest.raises(ValueError, match="at least one step"):
            profile(steps=steps)
    for blank in ("", "   ", None, 4):
        with pytest.raises(ValueError, match="scheduler must be named"):
            profile(scheduler=blank)
        with pytest.raises(ValueError, match="sampler must be named"):
            profile(sampler=blank)
    for empty in ("", "   ", 4):
        with pytest.raises(ValueError, match="LoRA must be a filename"):
            profile(lora=empty)
    minimum, maximum = H3_LORA_STRENGTH_LIMITS
    for strength in (minimum - 0.1, maximum + 0.1, float("inf"), float("nan"), True, "0.7"):
        with pytest.raises(ValueError, match="LoRA strength must be between"):
            profile(lora_strength=strength)
    # The bounds themselves are inclusive, and 0 is a real value — a LoRA loaded at no
    # strength is odd but it is the node's own range, not this adapter's opinion.
    for strength in (minimum, maximum, 0, 1):
        assert profile(lora_strength=strength).lora_strength == strength


def test_the_shipped_profiles_satisfy_their_own_validation():
    """Constructing them at import already proves it; saying so keeps it a guarantee.

    `H3_REFERENCE_PROFILES` is built at module scope, so a profile that violated the rules
    above would fail every import of `workflows.py` rather than one test — but nothing
    would then say *which* rule, and a future profile added with the validation weakened
    would slip past silently.
    """
    for name, profile in H3_REFERENCE_PROFILES.items():
        assert profile.steps >= 1, name
        assert profile.scheduler.strip() and profile.sampler.strip(), name
        if profile.lora is not None:
            minimum, maximum = H3_LORA_STRENGTH_LIMITS
            assert profile.lora.strip(), name
            assert minimum <= profile.lora_strength <= maximum, name


def test_the_text_only_director_path_is_offered_no_profile_and_keeps_its_step_default():
    """The Ask-First boundary, pinned.

    `MiniMaxH3DirectorCS` loads a different checkpoint pair, the installed generic H3
    turbo LoRAs are not the `ref2v` one, and nothing has been rendered live that way — so
    the Director graph gains no LoRA, and an omitted step count still means the 20 it
    always meant rather than any profile's number.
    """
    payload = build_h3_director_payload(
        timeline_data='{"segments":[{"id":"s","start":0,"length":120,"prompt":"p"}]}',
        duration=5.0,
        requested_frames=120,
        seed=0,
        width=1344,
        height=768,
        prefix="p",
    )

    assert H3_DIRECTOR_DEFAULT_STEPS == 20
    assert payload["2346"]["inputs"]["steps"] == H3_DIRECTOR_DEFAULT_STEPS
    assert payload["2346"]["inputs"]["scheduler"] == "simple"
    assert payload["2345"]["inputs"]["sampler_name"] == "res_multistep"
    assert all(node["class_type"] != "LoraLoaderModelOnly" for node in payload.values())


# --- The keyframe adapter against its evidence ---------------------------------------
#
# `build_h3_keyframe_payload` is derived from `h3-first-last-user-export.json`, and every
# bundle value below is *read out of that export by walking its wiring* rather than
# retyped — the standard the turbo profile's first test had to be corrected to. The
# export's one orphan is the inherited `ref2va` loader, which is exactly the checkpoint
# this adapter must never load: swapping it in would cost a ~20 GB model swap per render
# and produce a combination nobody has rendered.


KEYFRAME_EXPORT = "h3-first-last-user-export.json"


def keyframe_payload(**overrides) -> dict:
    arguments = {
        "prompt": "The wolf turns from the window to the door.",
        "first_frame": "F:/refs/first.png",
        "last_frame": "F:/refs/last.png",
        "duration": 5,
        "seed": 11,
        "prefix": "mvp/keyframe",
    }
    return build_h3_keyframe_payload(**{**arguments, **overrides})


def keyframe_export() -> dict:
    return json.loads((REFERENCE_EXPORTS / KEYFRAME_EXPORT).read_text(encoding="utf-8"))


def test_the_keyframe_export_has_one_orphan_and_it_is_the_ref2va_loader():
    """The reachability fact the whole adapter stands on, derived rather than trusted.

    28 nodes, 27 reachable from the single save node, and the orphan is the inherited
    `ref2va` `UNETLoader` — so the reachable checkpoint, the one the adapter loads, is the
    dedicated first/last `fl2va` model. If this ever fails, the evidence changed and the
    adapter's checkpoint claim must be re-derived, not patched.
    """
    export = keyframe_export()
    savers = [
        node_id for node_id, node in export.items()
        if node["class_type"] == "VHS_VideoCombine"
    ]
    assert len(savers) == 1, savers
    reachable = reachable_node_ids(export, savers)

    assert len(export) == 28
    assert len(reachable) == 27
    (orphan,) = set(export) - reachable
    assert export[orphan]["class_type"] == "UNETLoader"
    assert export[orphan]["inputs"]["unet_name"] == (
        "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
    )


def test_the_keyframe_adapter_matches_the_export_it_reproduces():
    """Every bundle value chain-walked out of the export and compared, none retyped.

    The walk starts at the one *reachable* `UNETLoader` — so the orphaned `ref2va` loader
    cannot be mistaken for the checkpoint — and follows the model chain forward to the
    scheduler, then across the sigmas link to the sampler. The conditioner, the VAEs, the
    CLIP and the saver are located the same way, by wiring.
    """
    export = keyframe_export()
    payload = keyframe_payload()
    savers = [
        node_id for node_id, node in export.items()
        if node["class_type"] == "VHS_VideoCombine"
    ]
    reachable = reachable_node_ids(export, savers)

    def only(candidates: list[str], what: str) -> str:
        assert len(candidates) == 1, f"{what}: {candidates}"
        return candidates[0]

    def fed_by(node_id: str, socket: str = "model") -> list[str]:
        return [
            other_id
            for other_id, other in export.items()
            if other["inputs"].get(socket) == [node_id, 0]
        ]

    # The checkpoint: the export's one reachable UNET loader, whatever it names.
    loader = only(
        [
            node_id for node_id in reachable
            if export[node_id]["class_type"] == "UNETLoader"
        ],
        "reachable UNET loaders",
    )
    assert (
        payload["mvp:model"]["inputs"]["unet_name"]
        == export[loader]["inputs"]["unet_name"]
    )

    # The model chain forward from the loader: Power Lora (empty) -> sigma shift -> sage
    # attention -> Spectrum -> preview. Each value the adapter emits is compared against
    # the node the walk found, and the two nodes the adapter deliberately does not emit
    # are asserted below rather than skipped silently.
    power_lora = only(fed_by(loader), "nodes reading the UNET loader")
    assert export[power_lora]["class_type"] == "Power Lora Loader (rgthree)"
    lora_rows = [
        entry
        for key, entry in export[power_lora]["inputs"].items()
        if key.startswith("lora_") and isinstance(entry, dict) and "on" in entry
    ]
    # No LoRA at all — not even an off-switched row — which is why the adapter applies
    # none and drops the empty pass-through loader for the reference adapter's recorded
    # schema-visibility reason.
    assert lora_rows == [], lora_rows
    shift = only(fed_by(power_lora), "nodes reading the empty LoRA loader")
    assert export[shift]["class_type"] == "MiniMaxH3SigmaShift"
    for name in ("shift_video", "shift_audio"):
        assert payload["mvp:shift"]["inputs"][name] == export[shift]["inputs"][name], name
    attention = only(fed_by(shift), "nodes reading the sigma shift")
    assert export[attention]["class_type"] == "PathchSageAttentionKJ"
    assert (
        payload["mvp:attention"]["inputs"]["sage_attention"]
        == export[attention]["inputs"]["sage_attention"]
    )
    # `SpectrumApplyMiniMaxH3` sits **enabled** in this export's chain, exactly as it sits
    # enabled in both reference exports — and the shipped reference adapter omits it. This
    # adapter mirrors that decision deliberately: two H3 adapters silently diverging on one
    # node is the drift the profile work exists to prevent, and Spectrum's parameters are
    # Ask First and have not been asked. Asserted in both directions so the mirroring is a
    # checked fact rather than a comment.
    spectrum = only(fed_by(attention), "nodes reading the sage attention")
    assert export[spectrum]["class_type"] == "SpectrumApplyMiniMaxH3"
    assert export[spectrum]["inputs"]["enabled"] is True
    for built in (payload, h3_reference_payload(h3_references("picture", 1))):
        assert all(
            node["class_type"] != "SpectrumApplyMiniMaxH3" for node in built.values()
        )
    preview = only(fed_by(spectrum), "nodes reading Spectrum")
    assert export[preview]["class_type"] == "ModelPreviewOverrideKJ"
    for name in (
        "max_resolution", "jpeg_quality", "suppress_default_preview",
        "preview_frames", "preview_fps",
    ):
        assert payload["mvp:preview"]["inputs"][name] == export[preview]["inputs"][name], name

    # The sampling bundle, downstream of the preview node.
    scheduler = only(
        [
            node_id for node_id in fed_by(preview)
            if export[node_id]["class_type"] == "BasicScheduler"
        ],
        "schedulers on the preview node",
    )
    for name in ("scheduler", "denoise"):
        assert (
            payload["mvp:scheduler"]["inputs"][name] == export[scheduler]["inputs"][name]
        ), name
    assert H3_KEYFRAME_DEFAULT_STEPS == export[scheduler]["inputs"]["steps"]
    assert payload["mvp:scheduler"]["inputs"]["steps"] == export[scheduler]["inputs"]["steps"]
    sampler = only(
        [
            export[node_id]["inputs"]["sampler"][0]
            for node_id in fed_by(scheduler, socket="sigmas")
            if export[node_id]["class_type"] == "SamplerCustomAdvanced"
        ],
        "samplers on that scheduler's sigmas",
    )
    assert (
        payload["mvp:sampler"]["inputs"]["sampler_name"]
        == export[sampler]["inputs"]["sampler_name"]
    )

    # The conditioner and its frame wiring: first from splitter output 0, last from
    # output 1, which are `picture_1` and `picture_2` in the recorded schema.
    conditioner = only(
        [
            node_id for node_id, node in export.items()
            if node["class_type"] == "MiniMaxH3ImageToVideo"
        ],
        "keyframe conditioners",
    )
    splitter_id, first_index = export[conditioner]["inputs"]["first_frame"]
    assert export[splitter_id]["class_type"] == "MiniMaxH3ReferenceSplitter"
    assert payload["mvp:condition"]["inputs"]["first_frame"] == ["mvp:split", first_index]
    _, last_index = export[conditioner]["inputs"]["last_frame"]
    assert payload["mvp:condition"]["inputs"]["last_frame"] == ["mvp:split", last_index]
    assert first_index == H3_SPLIT_OFFSETS["picture"]
    outputs = recorded_object_info()["MiniMaxH3ReferenceSplitter"]["output_name"]
    assert outputs[first_index] == "picture_1" and outputs[last_index] == "picture_2"

    # The CLIP and both VAEs, located through the conditioner's and decoders' own links.
    clip_id = export[conditioner]["inputs"]["clip"][0]
    assert (
        payload["mvp:clip"]["inputs"]["clip_name"] == export[clip_id]["inputs"]["clip_name"]
    )
    video_vae_id = export[conditioner]["inputs"]["vae"][0]
    assert (
        payload["mvp:video_vae"]["inputs"]["vae_name"]
        == export[video_vae_id]["inputs"]["vae_name"]
    )
    audio_decoder = only(
        [
            node_id for node_id, node in export.items()
            if node["class_type"] == "VAEDecodeAudio"
        ],
        "audio decoders",
    )
    audio_vae_id = export[audio_decoder]["inputs"]["vae"][0]
    assert (
        payload["mvp:audio_vae"]["inputs"]["vae_name"]
        == export[audio_vae_id]["inputs"]["vae_name"]
    )

    # The saver: every scalar the export fixes, compared by name. The links and the
    # filename prefix are this application's; everything else is the evidence's.
    saver = export[savers[0]]["inputs"]
    for name, value in saver.items():
        if isinstance(value, list):
            continue
        if name == "filename_prefix":
            continue
        assert payload["mvp:save"]["inputs"][name] == value, name


def test_the_keyframe_length_is_the_exports_own_expression_on_the_shared_grid():
    """The frame count the export computes in-graph, computed server-side to the digit.

    The export's `ComfyMathExpression` carries the 17k+5 snap as a string; evaluating that
    string — the export's own arithmetic, not a local restatement — must agree with what
    the builder sends and with `timeline.align_h3_frames`, for every window shape that
    matters: the floor, exact grid points, and either side of one.
    """
    export = keyframe_export()
    expression_node = next(
        node for node in export.values() if node["class_type"] == "ComfyMathExpression"
    )
    expression = expression_node["inputs"]["expression"]

    for duration in (5 / 24, 0.5, 3.75, 5, 5.1, 8, 12.34, 149.0):
        # The audited export's own arithmetic, evaluated with no builtins and no inputs
        # but `a` — restating the formula locally is exactly what this test must not do.
        # Evaluated at `duration + OVER_RENDER_SECONDS`: the builder deliberately feeds
        # the grid the over-rendered length (the Director's margin ruling), and the
        # export's snap arithmetic must agree with it *at that input*.
        expected = eval(
            expression,
            {"__builtins__": {}},
            {"a": duration + OVER_RENDER_SECONDS, "max": max, "round": round},
        )
        payload = keyframe_payload(duration=duration)
        assert payload["mvp:condition"]["inputs"]["length"] == expected, duration
        assert over_render_frames(duration) == expected, duration


def test_the_keyframe_payload_is_the_reachable_subgraph_minus_the_stated_drops():
    """What is not reproduced is a named decision, never an accident.

    The reachable subgraph's classes minus the payload's classes must be exactly the
    editor-side plumbing the adapter resolves server-side (geometry and the frame
    expression), the empty LoRA pass-through, and Spectrum — each dropped for a reason
    the builder's docstring states. A class leaving or joining this set is a change of
    decision and must fail here.
    """
    export = keyframe_export()
    savers = [
        node_id for node_id, node in export.items()
        if node["class_type"] == "VHS_VideoCombine"
    ]
    reachable_classes = {
        export[node_id]["class_type"]
        for node_id in reachable_node_ids(export, savers)
    }
    payload_classes = {node["class_type"] for node in keyframe_payload().values()}

    assert reachable_classes - payload_classes == {
        # Geometry: derives an unmeasured ~1 MP frame from the first image, with a 0.9 MP
        # selector fallback — resolved server-side through `select_resolution` instead,
        # sharing the reference path's measured 0.6 MP default.
        "ImageScaleToTotalPixels",
        "GetImageSize",
        "Any Switch (rgthree)",
        "ResolutionSelector",
        # The frame count: computed server-side, pinned against the export's expression
        # in the test above.
        "ComfyMathExpression",
        "PrimitiveFloat",
        # Carries no LoRA row at all; live schema declares no lora_* inputs on it.
        "Power Lora Loader (rgthree)",
        # Enabled in the export, omitted here — mirroring the reference adapter's
        # treatment of the same node in its own evidence, deliberately.
        "SpectrumApplyMiniMaxH3",
    }
    assert payload_classes <= reachable_classes


def test_the_keyframe_last_frame_is_omitted_entirely_for_a_first_only_shot():
    """The `image_to_video` shape: absent means absent.

    The schema declares `last_frame` optional; the honest way to not send one is to omit
    the input, not to send a null or an empty wiring. The media loader likewise carries
    one picture, so the splitter's second output is never read.
    """
    payload = keyframe_payload(last_frame=None)

    assert "last_frame" not in payload["mvp:condition"]["inputs"]
    media = json.loads(payload["mvp:frames"]["inputs"]["media_state"])
    assert [item["kind"] for item in media] == ["picture"]
    assert media[0]["file"] == "F:/refs/first.png"
    assert payload["mvp:condition"]["inputs"]["first_frame"] == ["mvp:split", 0]

    both = keyframe_payload()
    media = json.loads(both["mvp:frames"]["inputs"]["media_state"])
    assert [(item["file"], item["label"]) for item in media] == [
        ("F:/refs/first.png", "first frame"),
        ("F:/refs/last.png", "last frame"),
    ]
    assert all(item["enabled"] is True for item in media)


def test_the_keyframe_builder_refuses_frames_that_are_not_paths():
    """`None` for the last frame is a shape; an empty string is a lost path.

    Treating `""` as "no frame" would silently reroute a first/last shot to the
    first-only graph, which is the kind of silent downgrade a refusal exists to stop.
    """
    for blank in ("", "   "):
        with pytest.raises(ValueError, match="first frame must be a file path"):
            keyframe_payload(first_frame=blank)
        with pytest.raises(ValueError, match="last frame must be a file path"):
            keyframe_payload(last_frame=blank)
    for wrong in (None, 4, ["a.png"]):
        with pytest.raises(ValueError, match="first frame|needs a first frame"):
            keyframe_payload(first_frame=wrong)


def test_the_keyframe_builder_shares_the_reference_geometry_contract():
    """One `_resolve_frame` behind both adapters, observed from outside.

    The default is the measured 0.6 MP selection — the same frame the reference builder
    defaults to, asserted by comparing the two rather than by restating 1056x608 — and
    the two ways of describing a frame are mutually exclusive here exactly as they are
    there.
    """
    keyframe = keyframe_payload()["mvp:condition"]["inputs"]
    reference = h3_reference_payload(
        h3_references("picture", 1), width=None, height=None
    )["mvp:condition"]["inputs"]
    assert (keyframe["width"], keyframe["height"]) == (reference["width"], reference["height"])
    assert (keyframe["width"], keyframe["height"]) == select_resolution()

    explicit = keyframe_payload(width=640, height=384)["mvp:condition"]["inputs"]
    assert (explicit["width"], explicit["height"]) == (640, 384)
    selected = keyframe_payload(megapixels=0.9, aspect_ratio="16:9 (Widescreen)", multiple=32)
    assert (
        selected["mvp:condition"]["inputs"]["width"],
        selected["mvp:condition"]["inputs"]["height"],
    ) == select_resolution(megapixels=0.9, aspect_ratio="16:9 (Widescreen)", multiple=32)

    with pytest.raises(ValueError, match="not both"):
        keyframe_payload(width=640, height=384, megapixels=0.6)
    with pytest.raises(ValueError, match="only width"):
        keyframe_payload(width=640)


def test_the_keyframe_builder_refuses_a_window_past_its_own_nodes_ceiling():
    """The refusal quotes this node's ceiling, and the ceiling is the schema's."""
    with pytest.raises(ValueError, match="H3 keyframe node's 3600-frame maximum"):
        keyframe_payload(duration=151)
    # 3592 frames is the last grid point at or below the ceiling; the longest buildable
    # window is that minus the over-render margin, which every take now carries.
    assert keyframe_payload(duration=3592 / H3_FRAME_RATE - OVER_RENDER_SECONDS)
    schema = recorded_object_info()["MiniMaxH3ImageToVideo"]["input"]["required"]
    assert preflight.numeric_bounds(schema["length"])[1] == H3_KEYFRAME_MAX_FRAMES


def test_the_keyframe_path_is_offered_no_profile_and_keeps_the_exports_step_default():
    """The Ask-First boundary, pinned the way the text-only path pins its own.

    No turbo evidence exists for the `fl2va` checkpoint — the turbo exports are T2V,
    Director and References2V graphs — so the builder takes no `profile` parameter at
    all, and an omitted step count means the export's 20.
    """
    assert "profile" not in inspect.signature(build_h3_keyframe_payload).parameters
    assert keyframe_payload()["mvp:scheduler"]["inputs"]["steps"] == H3_KEYFRAME_DEFAULT_STEPS
    assert keyframe_payload(steps=8)["mvp:scheduler"]["inputs"]["steps"] == 8


def test_the_keyframe_graph_offers_no_audio_reference_and_generates_its_own_track():
    """The lip-sync answer, as payload facts.

    `MiniMaxH3ImageToVideo` declares no reference-audio input — the recorded schema is
    checked for that here and the live one in the pre-flight — so no keyframe payload can
    carry the master song, and the audio that reaches the saver is the sampler's own
    latent decoded through the audio VAE, exactly as the text-only path's is.
    """
    schema = recorded_object_info()["MiniMaxH3ImageToVideo"]["input"]
    every_input = {**schema.get("required", {}), **schema.get("optional", {})}
    assert not [name for name in every_input if "audio" in name.lower()]
    assert set(schema.get("optional", {})) >= {"first_frame", "last_frame"}

    payload = keyframe_payload()
    condition = payload["mvp:condition"]["inputs"]
    assert not [name for name in condition if "audio" in name.lower()]
    assert payload["mvp:save"]["inputs"]["audio"] == ["mvp:audio", 0]
    assert payload["mvp:audio"]["inputs"]["samples"] == ["mvp:sample", 0]


#: The two pre-existing shapes the keyframe story was forbidden to move, hashed at commit
#: `899b85f` — the commit before this adapter existed — by running that revision's builders
#: over these exact arguments and hashing `json.dumps(payload, separators=(",", ":"))`.
#: The references shape is `H3_DEFAULT_PROFILE_DIGEST` above, taken the same way at its own
#: baseline and still asserted by its own test. If one of these fails, a pre-existing
#: mode's payload changed; re-deriving the digest is the wrong fix unless the Director has
#: renegotiated that promise.
#: Re-pinned 2026-08-19 for the over-render margin — a renegotiation, not a drift: the
#: Director ruled every take renders longer than its window ("do not generate a clip to
#: exact or lesser length than the time it was given"), which changes `length` in every H3
#: payload family at once. The text-only digest is unchanged (its builder takes frames
#: from the caller); the reference digest moved 90 -> 107 frames and nothing else.
H3_TEXT_ONLY_PRE_KEYFRAME_DIGEST = (
    "c454de1948ad7185112d1a9a492a129b7f3225f8218cc4f758640e0d792984c8"
)
H3_SONG_AUDIO_PRE_KEYFRAME_DIGEST = (
    "fd3863312ce3c3c2e65cfd68791d0bad2a41b878b8f71719fa198db8a6b388b0"
)


def test_every_pre_existing_h3_shape_is_byte_identical_across_the_keyframe_change():
    """AC-3: the text-only shape and the song-audio reference shape, unchanged bytes.

    Together with `test_the_default_profile_emits_the_graph_the_adapter_shipped_before_profiles`
    — which pins the references shape to its own pre-profiles digest — this covers every
    pre-existing mode's payload family across the change that extracted `_resolve_frame`
    and added the keyframe branch.
    """
    text_only = build_h3_director_payload(
        timeline_data='{"segments":[{"id":"s","start":0,"length":120,"prompt":"A singer turns"}]}',
        duration=5.0,
        requested_frames=124,
        seed=17,
        width=1344,
        height=768,
        prefix="mvp/text-only",
        start=3.0,
    )
    serialized = json.dumps(text_only, separators=(",", ":")).encode("utf-8")
    assert hashlib.sha256(serialized).hexdigest() == H3_TEXT_ONLY_PRE_KEYFRAME_DIGEST

    song_audio = build_h3_reference_payload(
        prompt=(
            "Reference map: <Audio 1> is the master song for synchronization. "
            "The chorus lands."
        ),
        references=[
            {
                "kind": "audio",
                "file": "F:/refs/master.flac",
                "label": "master song",
                "trim": {"start": 12.0, "end": 15.75},
            }
        ],
        duration=3.75,
        seed=7,
        prefix="mvp/song-audio",
    )
    serialized = json.dumps(song_audio, separators=(",", ":")).encode("utf-8")
    assert hashlib.sha256(serialized).hexdigest() == H3_SONG_AUDIO_PRE_KEYFRAME_DIGEST


def test_h3_reference_slots_are_numbered_per_kind_in_attachment_order():
    """FR-19's determinism at the payload boundary: order in, order out.

    The route numbers its `<Picture N>` tags off the same walk, so this is the half of
    that promise the builder owns — the Nth picture lands in the Nth picture slot no
    matter what videos or audios are interleaved with it, and reordering the attachments
    reorders the slots rather than shuffling media between kinds.
    """
    mixed = [
        {"kind": "picture", "file": "F:/refs/lead.png"},
        {"kind": "audio", "file": "F:/refs/song.flac"},
        {"kind": "video", "file": "F:/refs/pan.mp4"},
        {"kind": "picture", "file": "F:/refs/stage.png"},
        {"kind": "audio", "file": "F:/refs/room.flac"},
    ]

    conditioner = h3_reference_payload(mixed)["mvp:condition"]["inputs"]
    media = json.loads(h3_reference_payload(mixed)["mvp:references"]["inputs"]["media_state"])

    assert [item["file"] for item in media] == [item["file"] for item in mixed]
    assert conditioner["ref_images.ref_image_0"] == ["mvp:split", 0]
    assert conditioner["ref_images.ref_image_1"] == ["mvp:split", 1]
    assert conditioner["ref_videos.ref_video_0"] == ["mvp:split", 9]
    assert conditioner["ref_audios.ref_audio_0"] == ["mvp:split", 15]
    assert conditioner["ref_audios.ref_audio_1"] == ["mvp:split", 16]

    reordered = [mixed[3], mixed[4], mixed[2], mixed[0], mixed[1]]
    swapped = json.loads(h3_reference_payload(reordered)["mvp:references"]["inputs"]["media_state"])
    assert [item["file"] for item in swapped] == [item["file"] for item in reordered]
    # Slot 0 now holds what was attached second-to-last, which is what makes the
    # numbering the Director sees a consequence of the attachment order.
    assert swapped[0]["file"] == "F:/refs/stage.png"


def test_h3_reference_payload_refuses_a_window_past_the_node_frame_ceiling():
    """Refused here rather than by ComfyUI, which rejects the prompt after the round-trip.

    `length` is capped at 3600 in the live schema; over it, `/prompt` validation returns
    `value_bigger_than_max` and the route translates that into an opaque 502.
    """
    # 3592 = 17·211 + 5 is the last grid point at or below the ceiling; the longest
    # buildable *window* is that minus the over-render margin every take now carries.
    longest = 3592 / 24 - OVER_RENDER_SECONDS
    assert h3_reference_payload(h3_references("picture", 1), duration=longest)[
        "mvp:condition"
    ]["inputs"]["length"] == H3_REFERENCE_MAX_FRAMES - 8

    with pytest.raises(ValueError, match=f"{H3_REFERENCE_MAX_FRAMES}-frame maximum"):
        h3_reference_payload(h3_references("picture", 1), duration=longest + 1)


def test_h3_director_payload_refuses_a_window_past_the_nodes_own_maxima():
    """The text-only path had no ceiling at all, and it is the one with live render evidence.

    `MiniMaxH3DirectorCS` caps `duration_frames`, `start_frame` and `end_frame` at 10000 and
    `start_second`/`end_second` at 1000 s. At 24 fps the frame cap binds first — about 416 s —
    which is why the refusal names the value it would have sent rather than a single limit.
    """
    def director(**overrides):
        arguments = {
            "timeline_data": '{"segments":[]}',
            "duration": 5.0,
            "requested_frames": 120,
            "seed": 0,
            "width": 1344,
            "height": 768,
            "steps": 20,
            "prefix": "p",
        }
        return build_h3_director_payload(**{**arguments, **overrides})

    at_limit = director(duration=415.0, requested_frames=align_h3_frames(round(415 * H3_FRAME_RATE)))
    assert at_limit["2343"]["inputs"]["end_frame"] == 9960

    with pytest.raises(ValueError, match=f"duration_frames={H3_DIRECTOR_MAX_FRAMES + 1}"):
        director(requested_frames=H3_DIRECTOR_MAX_FRAMES + 1)
    with pytest.raises(ValueError, match="end_frame=24000"):
        director(duration=1000.0, requested_frames=120)
    with pytest.raises(ValueError, match="start_frame"):
        director(start=500.0, duration=1.0)
    assert H3_DIRECTOR_MAX_SECONDS == 1000.0


def test_neither_h3_builder_accepts_a_window_that_is_not_a_finite_number():
    """`inf` reaches the frame arithmetic as `OverflowError`, which no route translates.

    The request models bound width, height and steps but leave `Shot.duration` open above,
    so this is reachable from a stored plan — and it arrives as a 500 rather than a refusal.
    """
    for value in (float("inf"), float("nan")):
        with pytest.raises(ValueError, match="finite number of seconds"):
            h3_reference_payload(h3_references("picture", 1), duration=value)
        with pytest.raises(ValueError, match="finite number of seconds"):
            build_h3_director_payload(
                timeline_data='{"segments":[]}',
                duration=value,
                requested_frames=120,
                seed=0,
                width=1344,
                height=768,
                steps=20,
                prefix="p",
            )


def test_h3_reference_frame_alignment_agrees_with_the_timeline_helper():
    """Two implementations of H3's 17k+5 grid, asserted to agree instead of assumed to.

    `build_h3_reference_payload` aligns inline while `timeline.align_h3_frames` is what the
    Director path uses; they are separate arithmetic that must produce the same frame count,
    because a reference render and a text render of the same Shot landing on different
    lengths is an assembly-time defect nothing else would catch.

    The frame rate comes from `H3_FRAME_RATE`, the constant the adapter itself converts
    with, so this compares two *alignments* rather than quietly re-deriving the conversion
    beside it — a rate change moves the adapter and this test together, and the grid
    properties below still hold it to something.
    """
    checked = 0
    for eighths in range(1, 1200):
        duration = eighths / 8  # 0.125 s to 149.875 s, every eighth of a second
        requested = max(5, round((duration + OVER_RENDER_SECONDS) * H3_FRAME_RATE))
        expected = align_h3_frames(requested)
        if expected > H3_REFERENCE_MAX_FRAMES:
            continue
        payload = h3_reference_payload(h3_references("picture", 1), duration=duration)
        length = payload["mvp:condition"]["inputs"]["length"]
        assert length == expected, duration
        assert length == over_render_frames(duration), duration
        # Independent of both implementations: on the grid, never more than one grid step
        # past what the margin needs — and the ruling itself, in frames: **never exact or
        # lesser than the window**, always at least the margin longer (less the half-frame
        # rounding can shave).
        assert length >= 5 and (length - 5) % 17 == 0, duration
        assert requested <= length < requested + 17, duration
        assert length / H3_FRAME_RATE > duration, duration
        assert length >= (duration + OVER_RENDER_SECONDS) * H3_FRAME_RATE - 0.5, duration
        checked += 1
    assert checked > 1100, checked


def test_h3_reference_limits_and_wiring_match_the_recorded_node_schema():
    """The adapter's constants against the schema that produced them, offline.

    The live audit makes the same comparison against the running server; this is the
    half that runs in CI, so a re-recorded fixture cannot move a bound without the
    adapter's number moving with it.
    """
    object_info = recorded_object_info()
    conditioner = object_info["MiniMaxH3ReferenceToVideo"]["input"]
    groups = conditioner["optional"]
    outputs = object_info["MiniMaxH3ReferenceSplitter"]["output_name"]

    filled = h3_reference_payload(
        [*h3_references("picture", 9), *h3_references("video", 3), *h3_references("audio", 3)]
    )["mvp:condition"]["inputs"]
    for kind, group in (
        ("picture", "ref_images"), ("video", "ref_videos"), ("audio", "ref_audios")
    ):
        template = preflight.autogrow_template(groups[group])
        prefix, maximum = template.prefix, template.maximum
        assert maximum == H3_REFERENCE_LIMITS[kind], group
        # At the limit the adapter fills every slot the schema offers, under the schema's
        # own names — which pins the count and the naming to the node rather than to a guess.
        assert {f"{group}.{prefix}{index}" for index in range(maximum)} <= set(filled), group

    assert preflight.numeric_bounds(conditioner["required"]["length"])[1] == H3_REFERENCE_MAX_FRAMES
    assert outputs[H3_SPLIT_OFFSETS["picture"]] == "picture_1"
    assert outputs[H3_SPLIT_OFFSETS["video"]] == "video_1"
    assert outputs[H3_SPLIT_OFFSETS["video_audio"]] == "video_audio_1"
    assert outputs[H3_SPLIT_OFFSETS["audio"]] == "audio_1"
    assert len(outputs) == H3_SPLIT_OFFSETS["audio"] + H3_REFERENCE_LIMITS["audio"]


def test_recording_a_fixture_keeps_the_classes_another_audit_recorded(tmp_path: Path):
    """The discipline that keeps `UNRECORDED_CLASSES` honest: recording merges.

    Two audits share one fixture. The recorder used to write a subset derived from its own
    payloads, so the second audit to run would delete the first's coverage — and the ledger
    of what is *not* covered would go stale in the direction that looks safe, because the
    offline checks would simply stop range-checking the dropped classes.
    """
    fixture = tmp_path / "object_info.json"
    fixture.write_text(json.dumps({"M3SongPlanner": {"input": {}}}), encoding="utf-8")
    live = {"M3SongPlanner": {"input": {"required": {}}}, "MiniMaxH3MediaLoader": {"input": {}}}

    recorded_names = preflight.record_fixture(live, ["MiniMaxH3MediaLoader"], path=fixture)

    recorded = json.loads(fixture.read_text(encoding="utf-8"))
    assert recorded_names.added == ["MiniMaxH3MediaLoader"]
    assert recorded_names.changed == []
    assert recorded_names.classes == ["M3SongPlanner", "MiniMaxH3MediaLoader"]
    assert set(recorded) == {"M3SongPlanner", "MiniMaxH3MediaLoader"}
    # A class the audit did not name keeps the entry it already had rather than being
    # refreshed from a live server the audit never validated it against.
    assert recorded["M3SongPlanner"] == {"input": {}}
    with pytest.raises(KeyError, match="not in /object_info"):
        preflight.record_fixture(live, ["NotInstalled"], path=fixture)


def test_every_h3_audit_variant_validates_against_the_recorded_object_info():
    """Every variant the live audit builds, checked offline against the recorded schema."""
    object_info = recorded_object_info()

    for label, payload in preflight_h3_ultra.audit_payloads():
        assert preflight.validate(label, payload, object_info) == [], label
        assert preflight.unbounded_numeric_inputs(label, payload, object_info) == [], label


def test_autogrow_slots_are_understood_and_an_index_past_the_group_is_named():
    """The first of the two shapes that made this validator lie about a correct graph.

    `ref_images.ref_image_0` is not a schema key — the schema publishes the `ref_images`
    group's template and index range instead — so a literal name check reported every
    attached reference as an input that does not exist. It must accept the slots the
    template describes and reject only an index past its maximum.
    """
    object_info = recorded_object_info()
    payload = h3_reference_payload(h3_references("picture", 9))

    assert preflight.validate("full", payload, object_info) == []

    conditioner = payload["mvp:condition"]["inputs"]
    conditioner["ref_images.ref_image_9"] = ["mvp:split", 9]
    conditioner["ref_images.ref_image_x"] = ["mvp:split", 0]
    problems = preflight.validate("overflow", payload, object_info)

    assert any(
        "'ref_images.ref_image_9' is slot 9, past ref_images's maximum of 9 slots (0-8)" in problem
        for problem in problems
    ), problems
    assert any(
        "'ref_images.ref_image_x' does not exist in the schema" in problem for problem in problems
    ), problems
    # The group name itself is never fed, so demanding it would be a third false failure.
    assert not any("required input 'ref_" in problem for problem in problems), problems


def test_format_conditional_inputs_are_read_from_the_selected_format():
    """The second shape: `VHS_VideoCombine` publishes `crf` and friends under `format`.

    They live in `format`'s options dict keyed by the selected format, so a name check
    reading only `required`/`optional` reported four inputs of a correct save node as
    absent — and `crf` resolved no bounds, leaving its 0-100 range unchecked. Selecting
    a different format has to change which inputs exist, or the merge is not reading the
    selection at all.
    """
    object_info = recorded_object_info()
    payload = h3_reference_payload(h3_references("picture", 1))
    save = payload["mvp:save"]["inputs"]

    assert preflight.validate("h264", payload, object_info) == []

    save["crf"] = 101
    assert any(
        "crf=101" in problem and "above the schema maximum 100" in problem
        for problem in preflight.validate("crf", payload, object_info)
    )
    save["crf"] = 19
    save["pix_fmt"] = "yuv420p12le"
    assert any(
        "pix_fmt='yuv420p12le' not in combo options" in problem
        for problem in preflight.validate("pix_fmt", payload, object_info)
    )

    save["pix_fmt"] = "yuv420p"
    save["format"] = "video/ffv1-mkv"
    problems = preflight.validate("ffv1", payload, object_info)
    assert any(
        "input 'crf' does not exist in the schema" in problem for problem in problems
    ), problems
    # `pix_fmt` survives the format change because ffv1-mkv declares one too; `crf` does not.
    assert not any("pix_fmt" in problem for problem in problems), problems


# --- The audit's own logic, driven directly -------------------------------------------
#
# Everything below exists because the audit could be gutted while the suite stayed green:
# `unbounded_numeric_inputs` could return `[]`, `run_audit` could record a *failing* audit
# over the shared fixture, and each `check_*` could be dropped from the wired tuple, with
# nothing failing. A pre-flight nothing tests is a pre-flight whose verdict means nothing.


def tiny_schema(**inputs) -> dict:
    """One registered class with the given required inputs and one output."""
    return {"Tiny": {"input": {"required": inputs}, "output": ["IMAGE"]}}


def tiny_payload(**values) -> dict:
    return {"n1": {"class_type": "Tiny", "inputs": values}}


def test_the_unbounded_check_names_an_input_whose_bounds_disappeared():
    """The positive direction. The check's whole purpose is to fail when a bound vanishes.

    Asserting only that it returns `[]` for today's payloads passes just as well for a
    function that returns `[]` for everything — which is exactly what it was replaced with
    to prove the gap, against a fully green suite.
    """
    object_info = recorded_object_info()
    payload = build_music3_payload(caption="c", lyrics="l", duration=120, seed=0, prefix="p")

    assert preflight.unbounded_numeric_inputs("bounded", payload, object_info) == []

    stripped = copy.deepcopy(object_info)
    del stripped["KSampler"]["input"]["required"]["steps"][1]["min"]
    del stripped["KSampler"]["input"]["required"]["steps"][1]["max"]
    gaps = preflight.unbounded_numeric_inputs("stripped", payload, stripped)

    assert [gap for gap in gaps if "steps resolved no min/max" in gap], gaps
    assert all("KSampler" in gap for gap in gaps), gaps
    # And the range check really is a no-op for it now, which is what makes the gap a gap.
    unbounded = build_music3_payload(caption="c", lyrics="l", duration=120, seed=0, prefix="p")
    unbounded["50"]["inputs"]["steps"] = 10**9
    assert preflight.validate("stripped", unbounded, stripped) == []


def test_a_combo_option_the_audit_cannot_read_is_reported_rather_than_crashing():
    """A V3 option dict with no `key` used to raise `KeyError` and abort the whole audit."""
    object_info = tiny_schema(model=["COMBO", {"options": [{"key": "a.safetensors"}, {}]}])

    assert preflight.combo_options(
        object_info["Tiny"]["input"]["required"]["model"]
    ) == ["a.safetensors", None]
    assert preflight.validate("readable", tiny_payload(model="a.safetensors"), object_info) == []
    problems = preflight.validate("unreadable", tiny_payload(model="b.safetensors"), object_info)
    assert any("could not be checked" in problem for problem in problems), problems


def test_an_unreadable_or_empty_autogrow_group_is_reported_rather_than_assumed():
    """A template that stopped parsing would otherwise turn every fed slot into a failure."""
    broken = tiny_schema(group=["COMFY_AUTOGROW_V3", {"template": {"min": 0, "max": 3}}])
    problems = preflight.validate("broken", tiny_payload(**{"group.slot_0": 1}), broken)
    assert any("cannot read" in problem for problem in problems), problems

    empty = tiny_schema(
        group=["COMFY_AUTOGROW_V3", {"template": {"prefix": "slot_", "min": 0, "max": 0}}]
    )
    problems = preflight.validate("empty", tiny_payload(**{"group.slot_0": 1}), empty)
    assert any("offers no slots at all" in problem for problem in problems), problems
    # The out-of-range message must not offer "0--1" as the acceptable range.
    assert any("(none)" in problem for problem in problems), problems


def test_an_autogrow_group_that_demands_slots_reports_a_payload_feeding_too_few():
    """`min` is a slot count the node requires, and it was parsed and thrown away."""
    schema = tiny_schema(
        group=["COMFY_AUTOGROW_V3", {"template": {"prefix": "slot_", "min": 2, "max": 3}}]
    )

    assert preflight.validate("enough", tiny_payload(**{"group.slot_0": 1, "group.slot_1": 2}), schema) == []
    problems = preflight.validate("too-few", tiny_payload(**{"group.slot_0": 1}), schema)
    assert any("requires at least 2 slot(s) but 1 is fed" in problem for problem in problems), problems


def test_a_conditional_selector_that_is_not_a_literal_is_reported():
    """A linked or omitted `format` silently reinstates the false failures it removed.

    With nothing selected there are no conditional inputs to merge, so `crf` and friends
    read as inputs that do not exist — the exact eight-failure state, arrived at quietly.
    """
    schema = tiny_schema(
        format=[["mp4"], {"formats": {"mp4": [["crf", "INT", {"min": 0, "max": 100}]]}}]
    )

    assert preflight.validate("literal", tiny_payload(format="mp4", crf=19), schema) == []
    linked = {"n1": {"class_type": "Tiny", "inputs": {"format": ["n0", 0], "crf": 19}}}
    problems = preflight.validate("linked", linked, {**schema, "Src": {"output": ["STRING"]}})
    assert any("rather than a literal option" in problem for problem in problems), problems


def test_a_conditional_input_colliding_with_a_declared_one_is_reported():
    """Dict order would otherwise decide which spec is enforced, silently."""
    schema = tiny_schema(
        crf=["INT", {"min": 0, "max": 10}],
        format=[["mp4"], {"formats": {"mp4": [["crf", "INT", {"min": 0, "max": 100}]]}}],
    )

    problems = preflight.validate("collision", tiny_payload(format="mp4", crf=50), schema)

    assert any("declared by the node *and* by format='mp4'" in problem for problem in problems)
    # The declared spec is the one enforced, and it says 50 is too big.
    assert any("above the schema maximum 10" in problem for problem in problems), problems


def test_a_non_finite_or_non_numeric_literal_is_reported_rather_than_raising():
    """`inf` used to raise `OverflowError` inside the INT check and abort the audit."""
    schema = tiny_schema(steps=["INT", {"min": 1, "max": 100}])

    for value in (float("inf"), float("-inf"), float("nan")):
        problems = preflight.validate("non-finite", tiny_payload(steps=value), schema)
        assert any("is not a finite number" in problem for problem in problems), value

    problems = preflight.validate("string", tiny_payload(steps="20"), schema)
    assert any("is a string but the schema declares INT" in problem for problem in problems), problems


def test_links_must_reach_a_node_in_the_payload_at_an_output_it_publishes():
    """The error class the pre-flight exists to catch, and it checked none of it.

    A payload wiring `["ghost", 0]` validated clean, and so did an index past the target's
    output list — the second being the one that costs a whole render to discover, because
    ComfyUI runs the graph and hands the model someone else's media.
    """
    object_info = recorded_object_info()
    payload = h3_reference_payload(h3_references("picture", 1))

    assert preflight.validate("wired", payload, object_info) == []

    payload["mvp:condition"]["inputs"]["clip"] = ["ghost", 0]
    payload["mvp:condition"]["inputs"]["vae"] = ["mvp:video_vae", 7]
    problems = preflight.validate("dangling", payload, object_info)

    assert any(
        "clip links to node 'ghost', which is not in this payload" in problem
        for problem in problems
    ), problems
    assert any(
        "vae links to output 7 of mvp:video_vae (VAELoader), which publishes 1 output(s)" in problem
        for problem in problems
    ), problems


def test_the_h3_split_indices_are_checked_against_the_splitters_real_output_count():
    """The wrong-slot risk `H3_SPLIT_OFFSETS` names, caught generically rather than per node."""
    object_info = recorded_object_info()
    payload = h3_reference_payload(h3_references("audio", 1))

    payload["mvp:condition"]["inputs"]["ref_audios.ref_audio_0"] = ["mvp:split", 18]

    assert any(
        "links to output 18 of mvp:split" in problem
        for problem in preflight.validate("past-the-end", payload, object_info)
    )


def test_recording_a_fixture_reports_a_changed_entry_and_not_only_a_new_one():
    """A moved bound on an already-recorded class is written in, so it must be reported.

    Only reporting *added* names is how a re-record silently changes what every offline
    test asserts: the class was already there, so nothing in the output changes, while the
    schema the tests validate against just moved.
    """
    fixture = REPO_ROOT / "tests/fixtures/object_info.json"
    recorded = json.loads(fixture.read_text(encoding="utf-8"))
    moved = copy.deepcopy(recorded)
    moved["M3SongPlanner"]["input"]["required"]["duration_seconds"][1]["max"] = 600.0
    target = Path(tempfile.mkdtemp()) / "object_info.json"
    target.write_text(json.dumps(recorded, indent=2, sort_keys=True), encoding="utf-8")

    result = preflight.record_fixture(moved, ["M3SongPlanner", "KSampler"], path=target)

    assert result.added == []
    assert result.changed == ["M3SongPlanner"]
    assert json.loads(target.read_text(encoding="utf-8"))["M3SongPlanner"] == moved["M3SongPlanner"]


def test_a_corrupt_fixture_fails_by_name_instead_of_as_a_json_error(tmp_path: Path):
    """Half-written or hand-edited, it must say which file and why."""
    broken = tmp_path / "object_info.json"
    broken.write_text('{"M3SongPlanner": {"input": ', encoding="utf-8")

    with pytest.raises(ValueError, match="is not readable JSON"):
        preflight.read_fixture(broken)
    with pytest.raises(ValueError, match="is not readable JSON"):
        preflight.record_fixture({"Tiny": {}}, ["Tiny"], path=broken)

    broken.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="not an object of classes"):
        preflight.read_fixture(broken)


def test_parse_arguments_refuses_an_argument_that_is_not_a_base_url(monkeypatch):
    """A typo used to become the base URL, so `--recrod` failed as a connection error."""
    monkeypatch.delenv("MVP_COMFY_URL", raising=False)

    assert preflight.parse_arguments([]) == (preflight.DEFAULT_BASE_URL, False)
    assert preflight.parse_arguments(["--record"]) == (preflight.DEFAULT_BASE_URL, True)
    assert preflight.parse_arguments(["http://host:9/"]) == ("http://host:9", False)
    monkeypatch.setenv("MVP_COMFY_URL", "http://elsewhere:1")
    assert preflight.parse_arguments([]) == ("http://elsewhere:1", False)

    for argv in (["--recrod"], ["127.0.0.1:8188"], ["http://a", "http://b"]):
        with pytest.raises(SystemExit) as refused:
            preflight.parse_arguments(argv)
        assert "usage:" in str(refused.value), argv


def stub_server(object_info: dict):
    def fetch(base_url: str) -> dict:
        assert base_url
        return object_info

    return fetch


def test_run_audit_records_only_after_the_audit_passes(tmp_path: Path, capsys):
    """`run_audit` was executed by nothing but the two scripts.

    Moving the record block above the failure guard — which makes a *failing* audit
    overwrite the shared fixture, the outcome the docstring and OPERATIONS.md name as the
    reason for the ordering — kept the whole suite green. So did deleting the line that
    folds each adapter's `checks` into the problem list, which makes the H3 audit print
    `OK` while performing none of its comparisons.
    """
    object_info = recorded_object_info()
    fixture = tmp_path / "object_info.json"
    fixture.write_text('{"Existing": {"input": {}}}\n', encoding="utf-8")
    before = fixture.read_bytes()
    clean = [("clean", h3_reference_payload(h3_references("picture", 1)))]
    broken = copy.deepcopy(clean[0][1])
    broken["mvp:condition"]["inputs"]["width"] = 10**9

    with pytest.raises(SystemExit) as failed:
        preflight.run_audit(
            [("broken", broken)],
            base_url="http://stub",
            record=True,
            fetch=stub_server(object_info),
            fixture_path=fixture,
        )
    assert failed.value.code == 1
    assert "Fixture NOT recorded" in capsys.readouterr().out
    assert fixture.read_bytes() == before

    # A failing adapter check must fail the audit exactly like a payload problem does.
    with pytest.raises(SystemExit):
        preflight.run_audit(
            clean,
            base_url="http://stub",
            record=True,
            checks=(lambda info: ["the adapter disagrees with the schema"],),
            fetch=stub_server(object_info),
            fixture_path=fixture,
        )
    assert "the adapter disagrees" in capsys.readouterr().out
    assert fixture.read_bytes() == before

    preflight.run_audit(
        clean,
        base_url="http://stub",
        record=True,
        checks=(lambda info: [],),
        fetch=stub_server(object_info),
        fixture_path=fixture,
    )
    output = capsys.readouterr().out
    recorded = json.loads(fixture.read_text(encoding="utf-8"))

    assert "OK 18 nodes across 1 variants" in output
    assert "Existing" in recorded, "recording merged rather than overwrote"
    assert "MiniMaxH3ReferenceToVideo" in recorded


def test_run_audit_refuses_an_empty_variant_list_and_an_unreachable_server(capsys):
    """"OK 0 nodes across 0 variants" is not a pass — it is an audit that ran nothing."""
    with pytest.raises(SystemExit) as empty:
        preflight.run_audit([], base_url="http://stub", record=False, fetch=stub_server({}))
    assert empty.value.code == 1
    assert "no payload variants" in capsys.readouterr().out

    def unreachable(base_url: str) -> dict:
        raise OSError("connection refused")

    with pytest.raises(SystemExit):
        preflight.run_audit(
            [("v", h3_reference_payload(h3_references("picture", 1)))],
            base_url="http://stub",
            record=False,
            fetch=unreachable,
        )
    assert "could not read http://stub/object_info" in capsys.readouterr().out


def test_the_h3_audit_wires_every_check_it_defines():
    """A check deleted from the tuple keeps passing its own test while the audit stops running it."""
    assert set(preflight_h3_ultra.CHECKS) == {
        preflight_h3_ultra.check_reference_limits,
        preflight_h3_ultra.check_split_offsets,
        preflight_h3_ultra.check_frame_ceilings,
        preflight_h3_ultra.check_lora_strength_range,
        preflight_h3_ultra.check_model_files,
        preflight_h3_ultra.check_request_bounds,
        preflight_h3_ultra.check_aspect_ratios,
        preflight_h3_ultra.check_default_geometry,
        preflight_h3_ultra.check_keyframe_schema_claims,
    }
    assert len(preflight_h3_ultra.CHECKS) == 9
    # And the class those last two read is named for recording, or they would check the live
    # schema and nothing else: absent from the fixture, both report "publishes nothing" in the
    # offline half of the suite, which reads as a real failure and is not.
    assert "ResolutionSelector" in preflight_h3_ultra.EXTRA_CLASSES
    assert "ResolutionSelector" in recorded_object_info()


def test_each_h3_check_passes_the_real_schema_and_names_a_moved_one():
    """Both directions for every check, against a schema mutated one bound at a time.

    The nearest thing before this re-implemented one comparison inline against the
    *fixture*, which only changes when someone re-records — so it pinned the adapter to a
    file rather than to the server, and the check functions themselves ran nowhere.
    """
    schema = recorded_object_info()
    for check in preflight_h3_ultra.CHECKS:
        assert check(schema) == [], check.__name__

    limits = copy.deepcopy(schema)
    limits["MiniMaxH3ReferenceToVideo"]["input"]["optional"]["ref_images"][1]["template"]["max"] = 8
    assert any(
        "ref_images offers 8 slots" in problem
        for problem in preflight_h3_ultra.check_reference_limits(limits)
    )
    # A group promoted from `optional` to `required` upstream is a schema change to notice,
    # not one that should make the check report a false failure.
    promoted = copy.deepcopy(schema)
    inputs = promoted["MiniMaxH3ReferenceToVideo"]["input"]
    inputs["required"]["ref_images"] = inputs["optional"].pop("ref_images")
    assert preflight_h3_ultra.check_reference_limits(promoted) == []

    outputs = copy.deepcopy(schema)
    names = outputs["MiniMaxH3ReferenceSplitter"]["output_name"]
    names[9], names[15] = names[15], names[9]
    assert any(
        "rather than 'video_1'" in problem
        for problem in preflight_h3_ultra.check_split_offsets(outputs)
    )

    ceiling = copy.deepcopy(schema)
    ceiling["MiniMaxH3DirectorCS"]["input"]["required"]["end_frame"][1]["max"] = 5000
    assert any(
        "MiniMaxH3DirectorCS.end_frame declares a maximum of 5000" in problem
        for problem in preflight_h3_ultra.check_frame_ceilings(ceiling)
    )

    strength = copy.deepcopy(schema)
    strength["LoraLoaderModelOnly"]["input"]["required"]["strength_model"][1]["max"] = 2.0
    assert any(
        "strength_model declares (-100.0, 2.0)" in problem
        for problem in preflight_h3_ultra.check_lora_strength_range(strength)
    )

    models = copy.deepcopy(schema)
    # `VAELoader` is a classic node: its options are the inline list at spec[0].
    models["VAELoader"]["input"]["required"]["vae_name"][0] = [
        option
        for option in models["VAELoader"]["input"]["required"]["vae_name"][0]
        if "minimax_h3_video_vae" not in str(option)
    ]
    assert any(
        "minimax_h3_video_vae_fp16.safetensors is not installed" in problem
        for problem in preflight_h3_ultra.check_model_files(models)
    )

    request = copy.deepcopy(schema)
    request["BasicScheduler"]["input"]["required"]["steps"][1]["max"] = 10
    assert any(
        "H3Request.steps accepts 100" in problem
        for problem in preflight_h3_ultra.check_request_bounds(request)
    )
    # The selector's own two ranges, restated on `H3Request` and checked the same way.
    megapixels = copy.deepcopy(schema)
    megapixels["ResolutionSelector"]["input"]["required"]["megapixels"][1]["max"] = 4.0
    assert any(
        "H3Request.megapixels accepts 16.0" in problem
        for problem in preflight_h3_ultra.check_request_bounds(megapixels)
    )

    ratios = copy.deepcopy(schema)
    options = ratios["ResolutionSelector"]["input"]["required"]["aspect_ratio"][1]["options"]
    options[options.index("16:9 (Widescreen)")] = "16:9 (Wide)"
    assert any(
        "16:9 (Wide)" in problem
        for problem in preflight_h3_ultra.check_aspect_ratios(ratios)
    )

    # The one geometry error ComfyUI would *not* catch: `step` is declared and not validated,
    # so an off-grid default reaches the GPU rather than the validator.
    grid = copy.deepcopy(schema)
    grid["MiniMaxH3ReferenceToVideo"]["input"]["required"]["width"][1]["step"] = 64
    assert any(
        "width 1056 is off" in problem and "step of 64" in problem
        for problem in preflight_h3_ultra.check_default_geometry(grid)
    )
    ceilinged = copy.deepcopy(schema)
    ceilinged["MiniMaxH3ReferenceToVideo"]["input"]["required"]["height"][1]["max"] = 512
    assert any(
        "height 608 is above" in problem
        for problem in preflight_h3_ultra.check_default_geometry(ceilinged)
    )

    # The keyframe mode table's two schema facts, each moved one at a time. A frame
    # promoted to required breaks the first-only shape; an audio input appearing would
    # falsify "keyframe shots cannot take the master song" — both must be named, never
    # absorbed.
    promoted_frame = copy.deepcopy(schema)
    keyframe_inputs = promoted_frame["MiniMaxH3ImageToVideo"]["input"]
    keyframe_inputs["required"]["last_frame"] = keyframe_inputs["optional"].pop("last_frame")
    assert any(
        "last_frame is required, not optional" in problem
        for problem in preflight_h3_ultra.check_keyframe_schema_claims(promoted_frame)
    )
    grown_audio = copy.deepcopy(schema)
    grown_audio["MiniMaxH3ImageToVideo"]["input"]["optional"]["ref_audios"] = [
        "COMFY_AUTOGROW_V3", {"template": {"prefix": "ref_audio_", "min": 0, "max": 3}}
    ]
    assert any(
        "audio-shaped inputs" in problem and "ref_audios" in problem
        for problem in preflight_h3_ultra.check_keyframe_schema_claims(grown_audio)
    )


def test_the_h3_audit_covers_every_h3_graph():
    """The text-only Director graph is the one H3 path with live render evidence.

    It was audited by nothing: `MiniMaxH3DirectorCS` was in no audited payload, so its
    literals were range-checked neither live nor offline, and the H3 graphs share every
    loader and sampler class — a model file renamed under one is renamed under all. The
    keyframe conditioner joined the same audit when its adapter landed.
    """
    classes = {
        node["class_type"]
        for _, payload in preflight_h3_ultra.audit_payloads()
        for node in payload.values()
    }

    assert {
        "MiniMaxH3DirectorCS", "MiniMaxH3ReferenceToVideo", "MiniMaxH3ImageToVideo"
    } <= classes
    assert classes <= set(recorded_object_info()), classes - set(recorded_object_info())


def test_the_h3_audit_exercises_both_keyframe_shapes():
    """One variant with `last_frame`, one without — the audit's optionality coverage.

    An input every variant sends is an optionality nothing tests: if the first-only
    variant vanished, the audit would go on passing while the one shape `image_to_video`
    actually submits was validated by nothing. Derived from what the payloads carry, so a
    variant renamed or rebuilt still counts by its shape.
    """
    keyframe_conditioners = [
        node["inputs"]
        for _, payload in preflight_h3_ultra.audit_payloads()
        for node in payload.values()
        if node["class_type"] == "MiniMaxH3ImageToVideo"
    ]

    assert len(keyframe_conditioners) >= 2, keyframe_conditioners
    shapes = {"last_frame" in inputs for inputs in keyframe_conditioners}
    assert shapes == {True, False}, shapes
    # And every one of them draws its frames from the splitter, first at picture_1.
    for inputs in keyframe_conditioners:
        assert inputs["first_frame"][1] == H3_SPLIT_OFFSETS["picture"]


def test_the_h3_audit_reads_its_model_files_out_of_the_payloads():
    """Restating the filenames would leave a renamed loader confirming the old file.

    The four are asserted by name here — that is the audit's headline claim — but the
    *audit* derives them from what the payloads actually load, so a repointed loader
    changes what is checked instead of being checked against a stale literal.
    """
    files = preflight_h3_ultra.model_files(preflight_h3_ultra.audit_payloads())

    assert {filename for _, _, filename in files} == {
        "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
        "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
        "qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
        "minimax_h3_video_vae_fp16.safetensors",
        "minimax_h3_audio_vae_fp32.safetensors",
        # Each turbo profile's LoRA is in this set because a payload *loads* it, not
        # because it is named here — which is what makes the live audit's "installed"
        # claim about the files the app would actually submit. Two different files,
        # because the two turbo bundles come from two different graphs.
        "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors",
        "minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors",
    }
    assert {class_type for class_type, _, _ in files} == {
        "UNETLoader", "CLIPLoader", "VAELoader", "LoraLoaderModelOnly",
    }


def test_the_reference_smoke_names_a_profile_and_sends_no_step_count():
    """The first live render's mistake, made unrepeatable.

    `tests/smoke_h3_reference_app.py` hardcoded `steps: 4` against the default profile —
    a 20-step graph with no LoRA to compensate — and the frame it produced said nothing
    about the path's picture quality. The body it sends is a module constant precisely so
    this can be checked without a socket, a running app, or a GPU.
    """
    import smoke_h3_reference_app as smoke

    assert "steps" not in smoke.RENDER_REQUEST, smoke.RENDER_REQUEST
    assert smoke.RENDER_REQUEST["profile"] == smoke.RENDER_PROFILE
    assert smoke.RENDER_PROFILE in H3_REFERENCE_PROFILES
    # What the printed record calls the effort is the profile's own number, so the record
    # and the render cannot disagree about what was sampled.
    assert smoke.RENDER_STEPS == H3_REFERENCE_PROFILES[smoke.RENDER_PROFILE].steps


def test_the_reference_smoke_covers_the_two_other_fixtures_that_hid_a_defect():
    """The same guard, for the same reason, over the start and the frame.

    The step count was the first cost-saving fixture mistaken for a property of the system.
    `start = 0.0` was the second: it is the one start where a missing audio offset and a
    correct one produce identical bytes, so a smoke pinned there could not fail however
    wrong the code was. 640x384 was the third: 0.25 MP against the Director's 0.6, chosen
    to save GPU minutes, and the resolution every recorded quality judgement was made at.

    All three are now unpinnable from the request body, and this checks that rather than
    trusting the docstring that says so.
    """
    import smoke_h3_reference_app as smoke

    # No geometry in the body: the run renders at whatever the application selects, which
    # is the number a Director actually gets.
    assert "width" not in smoke.RENDER_REQUEST, smoke.RENDER_REQUEST
    assert "height" not in smoke.RENDER_REQUEST, smoke.RENDER_REQUEST
    assert "megapixels" not in smoke.RENDER_REQUEST, smoke.RENDER_REQUEST
    # And what it asserts afterwards is that same selection, read from the adapter rather
    # than typed, so the pin cannot outlive the default it describes.
    assert (smoke.RENDER_WIDTH, smoke.RENDER_HEIGHT) == select_resolution() == (1056, 608)
    # Past the intro of the master track, and emphatically not the blind spot. Asserted on
    # the constant rather than on the window: every shot carries a window now, 0 s included,
    # so a run at 0 s would send one and still prove nothing -- 0 s is the start whose window
    # covers the same seconds a missing offset would have.
    assert smoke.SHOT_START_SECONDS >= 8.0
    assert song_audio_window(
        start=smoke.SHOT_START_SECONDS, duration=smoke.SHOT_DURATION_SECONDS, song_duration=0
    ) == {"start": 12.0, "end": 15.75}


def test_the_reference_smoke_reads_what_was_sampled_out_of_comfyuis_own_record():
    """The record has to say what the *server* built, not what the constants mean.

    A profile accepted and then silently dropped produces a run that completes, measures
    correctly, and prints `turbo` over a render that used none — the record would be a
    confident lie about a GPU job. The smoke reads the submitted graph back from
    `/history` and compares it with the profile, so the two pure functions that do the
    reading are driven here against the real shape ComfyUI 0.33.1 returns.
    """
    import smoke_h3_reference_app as smoke

    payload = h3_reference_payload(h3_references("picture", 1), profile="turbo")
    entry = {"prompt": [13, "abc", payload, {"create_time": 0}, ["mvp:save"]], "outputs": {}}

    graph, why = smoke.graph_from_history(entry)

    assert (graph, why) == (payload, "")
    assert smoke.submitted_sampling(graph) == smoke.profile_declares("turbo")
    # The default profile carries no LoRA, and its two empty fields are what say so —
    # rather than the summary quietly reporting the turbo LoRA it never loaded.
    default = h3_reference_payload(h3_references("picture", 1))
    assert smoke.submitted_sampling(default) == smoke.profile_declares("default")
    assert smoke.submitted_sampling(default)["lora"] == ""

    # The frame and the song window are read back the same way and for the same reason: a
    # `trim` the loader cannot read is dropped silently, so a run that completes and measures
    # correctly is not evidence the model heard the right seconds -- only the recorded graph
    # is. Driven here against a real payload rather than only on a live run.
    windowed = build_h3_reference_payload(
        prompt="p",
        references=[
            {"kind": "picture", "file": "F:/refs/lead.png"},
            {
                "kind": "audio",
                "file": "F:/refs/master.mp3",
                "label": "master song",
                "trim": {"start": 12.0, "end": 15.75},
            },
        ],
        duration=3.75,
        seed=0,
        prefix="p",
    )
    assert smoke.submitted_geometry(windowed) == {
        "width": 1056,
        "height": 608,
        # 3.75 s plus the over-render margin: 4.25 s -> 102 frames -> 107 on the grid.
        "length": 107,
    }
    assert smoke.submitted_song_window(windowed) == {
        "file": "F:/refs/master.mp3",
        "trim": {"start": 12.0, "end": 15.75},
    }
    # A song reference carrying no window reports `trim: None` rather than looking absent,
    # which at a non-zero start is exactly the defect the live run must be able to see.
    unwindowed = build_h3_reference_payload(
        prompt="p",
        references=[{"kind": "audio", "file": "F:/refs/master.mp3", "label": "master song"}],
        duration=3.75,
        seed=0,
        prefix="p",
    )
    assert smoke.submitted_song_window(unwindowed) == {
        "file": "F:/refs/master.mp3",
        "trim": None,
    }
    # And a graph with no master song at all is an empty mapping, never a wrong answer.
    assert smoke.submitted_song_window(default) == {}
    assert smoke.submitted_song_window({}) == {}

    # A shape it cannot read is a named gap, never a wrong answer. The index is not
    # trusted either: the graph is found by shape, so a reordered tuple still resolves.
    for unreadable, expected in (
        (None, "not an object"),
        ({}, "carries no prompt list"),
        ({"prompt": [13, "abc", {"create_time": 0}]}, "carries no node graph"),
    ):
        found, reason = smoke.graph_from_history(unreadable)
        assert found == {} and expected in reason, (unreadable, reason)
    reordered = {"prompt": [payload, 13, "abc"]}
    assert smoke.graph_from_history(reordered) == (payload, "")
    # Two schedulers is a graph this summary does not understand, and it must say nothing
    # rather than pick one: reporting the first would be a guess printed as a measurement.
    doubled = {**payload, "extra:scheduler": copy.deepcopy(payload["mvp:scheduler"])}
    assert smoke.submitted_sampling(doubled)["steps"] is None


def test_the_h3_audit_variants_take_their_steps_from_the_profiles():
    """Each profile's own step default is a number the audit actually sends.

    The audit's shared arguments used to carry a literal `steps=20`. That is the default
    profile's count *today*, so it looked equivalent — but it is a copy, and a copy stops
    describing what the application submits the moment the profile moves. The variants now
    name no count at all, so this holds by construction; it is asserted because "by
    construction" is precisely the kind of claim that stops being true quietly.
    """
    audited = {
        node["inputs"]["steps"]
        for _, payload in preflight_h3_ultra.audit_payloads()
        for node in payload.values()
        if node["class_type"] == "BasicScheduler"
    }

    for name, profile in H3_REFERENCE_PROFILES.items():
        assert profile.steps in audited, (name, sorted(audited))


def audited_reference_variants() -> list[tuple[str, dict]]:
    """The audit's reference-graph variants; the Director-graph ones sample no profile."""
    return [
        (label, payload)
        for label, payload in preflight_h3_ultra.audit_payloads()
        if any(node["class_type"] == "MiniMaxH3ReferenceToVideo" for node in payload.values())
    ]


def audited_bundle(payload: dict) -> tuple:
    """One audited payload reduced to the bundle it samples with, minus the step count.

    The count is left out here because two variants deliberately send the request model's
    extreme values rather than any profile's own number. It is not dropped from the claim:
    the test below still requires, per profile, a variant carrying both the bundle and the
    profile's own count.
    """
    lora = payload.get("mvp:lora", {}).get("inputs", {})
    return (
        lora.get("lora_name"),
        lora.get("strength_model"),
        payload["mvp:scheduler"]["inputs"]["scheduler"],
        payload["mvp:sampler"]["inputs"]["sampler_name"],
    )


def declared_bundle(profile: H3SamplingProfile) -> tuple:
    return (profile.lora, profile.lora_strength, profile.scheduler, profile.sampler)


@pytest.mark.parametrize("name", sorted(H3_REFERENCE_PROFILES))
def test_the_h3_audit_covers_every_sampling_profile(name: str):
    """AC-3, held for every profile there is rather than for the two there were.

    Parametrized over the table, so a profile added to `H3_REFERENCE_PROFILES` and not
    given its own audit variant fails here — which is the whole difference between a
    configuration that was checked against the live server before a GPU job and one that
    merely looks as though it was. Asserting "some variant carries a LoRA" would pass on an
    audit that had quietly dropped a profile and duplicated another.
    """
    profile = H3_REFERENCE_PROFILES[name]
    variants = audited_reference_variants()
    matching = [
        label for label, payload in variants if audited_bundle(payload) == declared_bundle(profile)
    ]
    complete = [
        label
        for label, payload in variants
        if audited_bundle(payload) == declared_bundle(profile)
        and payload["mvp:scheduler"]["inputs"]["steps"] == profile.steps
    ]

    assert matching, (name, [label for label, _ in variants])
    # And one of them sends the profile's own count, taken from the profile rather than
    # restated in the audit, so a moved default moves what is audited.
    assert complete, (name, matching)


def test_the_h3_audit_samples_no_bundle_a_profile_does_not_declare():
    """The other direction: the audit must not bless a combination nothing ships.

    A variant assembled by hand — this LoRA with that scheduler — would be audited, would
    pass, and would read afterwards as if the combination had been validated as a
    configuration. Only shipped bundles are audited, and every shipped LoRA is loaded by
    one of them.
    """
    declared = {declared_bundle(profile) for profile in H3_REFERENCE_PROFILES.values()}

    for label, payload in audited_reference_variants():
        assert audited_bundle(payload) in declared, (label, audited_bundle(payload))

    loaded = {
        node["inputs"]["lora_name"]
        for _, payload in preflight_h3_ultra.audit_payloads()
        for node in payload.values()
        if node["class_type"] == "LoraLoaderModelOnly"
    }
    assert loaded == {
        profile.lora for profile in H3_REFERENCE_PROFILES.values() if profile.lora is not None
    }


def test_the_h3_audit_variants_reach_both_ends_of_every_request_bound():
    """A single safe midpoint satisfies the schema wherever the bound moves.

    All five original variants sent 1280x720x20 steps, so the range check on those inputs
    would have passed unchanged if the node had halved its maximum — the check existed and
    proved nothing.
    """
    conditioners = [
        node["inputs"]
        for _, payload in preflight_h3_ultra.audit_payloads()
        for node in payload.values()
        if node["class_type"] in {"MiniMaxH3ReferenceToVideo", "MiniMaxH3DirectorCS"}
    ]
    widths = {inputs.get("width", inputs.get("custom_width")) for inputs in conditioners}
    schedulers = {
        node["inputs"]["steps"]
        for _, payload in preflight_h3_ultra.audit_payloads()
        for node in payload.values()
        if node["class_type"] == "BasicScheduler"
    }

    for field, seen in (("width", widths), ("steps", schedulers)):
        assert preflight_h3_ultra.request_bound(field, "Ge") in seen, field
        assert preflight_h3_ultra.request_bound(field, "Le") in seen, field


def test_ltx25_reference_patch_normalizes_seedvr2_frames_before_vae():
    template = json.loads(
        Path("workflow_templates/reference_exports/h3-ltx25-user-export.json").read_text(
            encoding="utf-8"
        )
    )

    payload = patch_ltx25_dimension_boundary(template)

    normalizer = payload["mvp:ltx-size-normalize"]
    assert normalizer["class_type"] == "ImageResizeKJv2"
    assert normalizer["inputs"]["image"] == ["6112", 0]
    assert normalizer["inputs"]["divisible_by"] == 32
    assert payload["6116:6070"]["inputs"]["pixels"] == ["mvp:ltx-size-normalize", 0]
    assert payload["6116:4970"]["inputs"]["image"] == ["mvp:ltx-size-normalize", 0]
    assert payload["6116:6073"]["inputs"]["image"] == ["mvp:ltx-size-normalize", 0]
    assert template["6116:6070"]["inputs"]["pixels"] == ["6112", 0]


def test_ltx25_normalizer_preserves_geometry_by_cropping_not_stretching():
    """The Director's ruling: preserve geometry, pay for it in trimmed pixels.

    ``keep_proportion="crop"`` centre-crops to the target aspect and then resamples,
    so the retained content keeps its shape. ``"resize"`` -- what this used to be --
    resampled straight to the target and squashed 1250x720 into 1248x704, a 2.07%
    anamorphic stretch. Both produce the same 1248x704 output, so the difference is
    invisible in dimensions and only this configuration pins it.

    What this test can honestly prove is the configuration contract. How the node
    actually rounds and crops was verified out of band by executing the installed
    ``ImageResizeKJv2`` on a synthetic 1250x720 frame: crop mode retained source
    rows ~6-710 (15 rows trimmed, split centre) at 0.02% residual distortion,
    against resize mode's full-height 2.07% squash. See docs/WORKFLOW-MAP.md.
    """
    template = json.loads(
        (REFERENCE_EXPORTS / "h3-ltx25-user-export.json").read_text(encoding="utf-8")
    )

    inputs = patch_ltx25_dimension_boundary(template)["mvp:ltx-size-normalize"]["inputs"]

    assert inputs["keep_proportion"] == "crop"
    assert inputs["crop_position"] == "center"
    assert inputs["upscale_method"] == "lanczos"
    assert inputs["device"] == "cpu"
    # Deriving the target from the incoming frame is what keeps the patch free of a
    # hardcoded resolution; crop mode supports it exactly as resize mode did.
    assert inputs["width"] == 0
    assert inputs["height"] == 0
    assert inputs["divisible_by"] == LTX25_DIVISOR


def test_ltx25_divisor_makes_the_observed_boundary_size_exact():
    """The observed SeedVR2 boundary size must divide exactly by the LTX 2.5 VAE's
    total spatial compression of 32 (4-pixel patchify plus three stride-2 stages).
    1250x720 must land on 1248x704 -- not the 1248x720 that 16 gives, where
    720/32 = 22.5 pushes a half cell through the conv stack.

    This routes through ``normalize_to_divisor``, the function the patch itself uses
    for the known-size path, rather than recomputing the flooring inline: an inline
    recomputation would assert its own arithmetic and prove nothing about the code.
    """
    template = json.loads(
        (REFERENCE_EXPORTS / "h3-ltx25-user-export.json").read_text(encoding="utf-8")
    )

    assert normalize_to_divisor(1250) == 1248
    assert normalize_to_divisor(720) == 704
    for axis in (1248, 704):
        assert axis % 32 == 0
    # 16 would have cleared patchify and left 720 -- the defect this replaced.
    assert normalize_to_divisor(720, divisor=16) == 720

    inputs = patch_ltx25_dimension_boundary(template)["mvp:ltx-size-normalize"]["inputs"]
    assert inputs["divisible_by"] == LTX25_DIVISOR == 32


def test_normalize_to_divisor_never_falls_below_one_divisor_cell():
    """Flooring alone sends a sub-divisor axis to 0 -- a zero-sized resize, not a small one."""
    assert normalize_to_divisor(1250) == 1248
    assert normalize_to_divisor(720) == 704
    assert normalize_to_divisor(LTX25_DIVISOR) == LTX25_DIVISOR
    for axis in (1, 7, LTX25_DIVISOR - 1):
        assert normalize_to_divisor(axis) == LTX25_DIVISOR, axis
    for invalid in (0, -8):
        with pytest.raises(ValueError, match="at least 1 pixel"):
            normalize_to_divisor(invalid)
    with pytest.raises(ValueError, match="divisor"):
        normalize_to_divisor(64, divisor=0)


def test_ltx25_patch_applies_the_divisor_floor_when_the_source_size_is_known():
    """Given the real frame size the patch normalizes it here, so no axis can reach 0."""
    template = json.loads(
        (REFERENCE_EXPORTS / "h3-ltx25-user-export.json").read_text(encoding="utf-8")
    )

    normal = patch_ltx25_dimension_boundary(template, source_size=(1250, 720))
    tiny = patch_ltx25_dimension_boundary(template, source_size=(1250, 12))

    assert normal["mvp:ltx-size-normalize"]["inputs"]["width"] == 1248
    assert normal["mvp:ltx-size-normalize"]["inputs"]["height"] == 704
    assert tiny["mvp:ltx-size-normalize"]["inputs"]["height"] == LTX25_DIVISOR
    for payload in (normal, tiny):
        inputs = payload["mvp:ltx-size-normalize"]["inputs"]
        assert inputs["width"] % LTX25_DIVISOR == 0
        assert inputs["height"] % LTX25_DIVISOR == 0
        assert inputs["width"] >= LTX25_DIVISOR
        assert inputs["height"] >= LTX25_DIVISOR
    # The divisor the floor uses and the one the node is handed are the same number.
    assert normal["mvp:ltx-size-normalize"]["inputs"]["divisible_by"] == LTX25_DIVISOR


# The audited evidence for the LTX 2.5 enhancement adapter, its single output node, and the
# digest that says the file has not moved under the tests that read it.
LTX25_ENHANCER_EXPORT = REFERENCE_EXPORTS / "ltx25-enhancer-user-export.json"
LTX25_ENHANCER_EXPORT_SHA256 = (
    "6f6c70102b8b8e244847518cdbe4a90881dc4782cfbd6b3e2b03eaca4b66fd37"
)
LTX25_ENHANCER_OUTPUT_NODE = "1994"

# The two node classes the adapter substitutes, and nothing else. Declared as data so the
# comparison below can hold every other class to equality: a substitution that grew a third
# entry would otherwise disappear into a looser assertion. Each is justified in
# `build_ltx25_enhance_payload`; both keep the same outputs in the same order, which is why
# the wiring comparison further down is a like-for-like one.
LTX25_ENHANCE_SUBSTITUTIONS = {
    "VHS_LoadVideo": "VHS_LoadVideoPath",
    "Power Lora Loader (rgthree)": "LoraLoader",
}


def ltx25_enhancer_export() -> dict:
    return json.loads(LTX25_ENHANCER_EXPORT.read_text(encoding="utf-8"))


def ltx25_enhance_payload(**overrides) -> dict:
    arguments = {
        "source_video": "J:/comfy/output/music-video-producer/p/shots/s-h3-reference_00001.mp4",
        "prefix": "music-video-producer/p/shots/s-ltx25-enhance",
    }
    return build_ltx25_enhance_payload(**{**arguments, **overrides})


def test_the_ltx25_enhancer_export_is_not_mutated():
    """The evidence every test below reads, pinned. It is immutable per AGENTS.md."""
    digest = hashlib.sha256(LTX25_ENHANCER_EXPORT.read_bytes()).hexdigest()

    assert digest == LTX25_ENHANCER_EXPORT_SHA256


def test_the_enhancer_export_carries_orphaned_loaders_the_adapter_must_not_inherit():
    """The trap, measured off the file rather than restated from the spec.

    20 nodes, 18 reachable from the single `VHS_VideoCombine`. The two that are not are an
    audio `VAELoaderKJ` and a `LatentUpscaleModelLoader`, inherited from whatever graph this
    export was cut out of. Both name a real model file, and both files would end up in a
    dependency list built from `export.values()` — which is the failure the whole adapter is
    shaped around, so it is asserted here before anything asserts the adapter avoids it.

    `LatentUpscaleModelLoader` earns its own line: despite the name, no latent upscale runs
    in the executed path at all.
    """
    export = ltx25_enhancer_export()

    reachable = reachable_node_ids(export, [LTX25_ENHANCER_OUTPUT_NODE])
    orphaned = set(export) - reachable

    assert len(export) == 20
    assert len(reachable) == 18
    assert {export[node_id]["class_type"] for node_id in orphaned} == {
        "VAELoaderKJ",
        "LatentUpscaleModelLoader",
    }
    # The audio VAE is the orphaned `VAELoaderKJ`; the *video* one of the same class is
    # reachable, so the class is a dependency even though that file is not. A dependency
    # rule written per class rather than per node would get this exactly wrong.
    assert {export[node_id]["inputs"].get("vae_name") for node_id in orphaned} == {
        "ltx-2.5-audio-vae-bf16.safetensors",
        None,
    }
    assert "VAELoaderKJ" in {export[node_id]["class_type"] for node_id in reachable}
    # No latent upscaler anywhere ComfyUI would execute.
    assert "LatentUpscaleModelLoader" not in {
        export[node_id]["class_type"] for node_id in reachable
    }


def test_the_enhance_payload_is_the_reachable_subgraph_and_nothing_else():
    """AC: the adapter is built from the 18, not from the 20.

    Compared as a class multiset against the export's own reachable nodes, with the two
    substitutions applied from `LTX25_ENHANCE_SUBSTITUTIONS` — so a node quietly added or
    dropped fails here, and so does a third substitution nobody declared.
    """
    export = ltx25_enhancer_export()
    reachable = reachable_node_ids(export, [LTX25_ENHANCER_OUTPUT_NODE])
    expected = sorted(
        LTX25_ENHANCE_SUBSTITUTIONS.get(
            export[node_id]["class_type"], export[node_id]["class_type"]
        )
        for node_id in reachable
    )

    payload = ltx25_enhance_payload()

    assert sorted(node["class_type"] for node in payload.values()) == expected
    # And the payload is itself fully reachable from its own output: a node built here that
    # nothing downstream reads would be dead weight submitted to a GPU.
    assert reachable_node_ids(payload, ["mvp:save"]) == set(payload)


def test_the_enhance_payload_declares_only_the_models_the_reachable_subgraph_loads():
    """The specific harm the orphans would do, stated as the four files and the two absences.

    Derived on both sides: the expectation is read out of the export's reachable nodes, and
    the payload's list is read out of the payload. Neither is a list typed into this test.
    """
    export = ltx25_enhancer_export()
    reachable = reachable_node_ids(export, [LTX25_ENHANCER_OUTPUT_NODE])
    orphaned = set(export) - reachable

    def files(node_ids) -> set[str]:
        return {
            filename
            for node_id in node_ids
            for value in export[node_id]["inputs"].values()
            for filename in preflight_ltx25_enhance.nested_model_files(value)
        }

    payload = ltx25_enhance_payload()
    loaded = {
        value
        for node in payload.values()
        for value in node["inputs"].values()
        if isinstance(value, str) and value.endswith(".safetensors")
    }

    assert loaded == files(reachable)
    assert len(loaded) == 4
    # Named, because "equal to the reachable set" would still pass if the reachable set had
    # somehow acquired them. These are the two a node-list dependency scan would add, and a
    # pre-flight demanding either refuses a machine that can do this work.
    assert files(orphaned) == {
        "ltx-2.5-audio-vae-bf16.safetensors",
        "ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors",
    }
    assert loaded.isdisjoint(files(orphaned))


def test_the_enhance_payload_carries_the_sources_audio_straight_to_the_saver():
    """The audio guarantee, as wiring: the only audio in the graph is the source's own.

    Three assertions rather than one, because "the saver reads the loader" alone would still
    hold in a graph that had grown an audio VAE feeding something else. The source's third
    output is `audio` in the live schema, and it is checked by index because that is what the
    payload actually wires.
    """
    payload = ltx25_enhance_payload()
    source = next(
        node_id for node_id, node in payload.items() if node["class_type"] == "VHS_LoadVideoPath"
    )

    assert payload["mvp:save"]["inputs"]["audio"] == [source, 2]
    # Nothing synthesises, decodes or re-encodes audio anywhere in this graph.
    assert not any(
        "audio" in node["class_type"].lower() for node in payload.values()
    ), sorted(node["class_type"] for node in payload.values())
    assert all(
        node["inputs"].get("vae_name") != "ltx-2.5-audio-vae-bf16.safetensors"
        for node in payload.values()
    )


def test_the_source_take_is_the_only_input_and_nothing_regenerates_it():
    """The frozen "Always": the take is an input, and nothing here may re-run H3."""
    source = "J:/comfy/output/music-video-producer/p/shots/s-h3-reference_00003-audio.mp4"

    payload = ltx25_enhance_payload(source_video=source)

    assert payload["mvp:source"]["inputs"]["video"] == source
    # The frames entering the VAE encode are the source's, through the lanczos scale and
    # nothing else.
    assert payload["mvp:scale"]["inputs"]["image"] == ["mvp:source", 0]
    assert payload["mvp:encode"]["inputs"]["pixels"] == ["mvp:scale", 0]
    # No H3 stage, and no empty-latent node that could stand in for one: every latent in the
    # graph descends from the encoded source.
    assert not any("MiniMaxH3" in node["class_type"] for node in payload.values())
    assert payload["mvp:sample"]["inputs"]["latents"] == ["mvp:encode", 0]
    assert payload["mvp:sample"]["inputs"]["optional_guiding_latents"] == ["mvp:encode", 0]


def test_the_enhance_payload_reproduces_the_exports_fixed_sampling():
    """Every Ask First value read out of the export rather than retyped here.

    The spec marks the sigmas, the detailer strength and the prompt as Ask First, and nothing
    has been asked — so the test that they are reproduced has to compare against the file,
    not against literals that would agree with a typo in the builder.
    """
    export = ltx25_enhancer_export()

    def only(class_type: str, node_ids: set[str]) -> dict:
        matches = [
            node_id for node_id in node_ids if export[node_id]["class_type"] == class_type
        ]
        assert len(matches) == 1, f"{class_type}: {matches}"
        return export[matches[0]]["inputs"]

    reachable = reachable_node_ids(export, [LTX25_ENHANCER_OUTPUT_NODE])
    payload = ltx25_enhance_payload()

    assert LTX25_ENHANCE_SIGMAS == only("ManualSigmas", reachable)["sigmas"]
    assert LTX25_ENHANCE_SIGMAS.count(",") == 3, "three steps plus the terminating zero"
    assert LTX25_ENHANCE_CFG == only("CFGGuider", reachable)["cfg"]
    assert LTX25_ENHANCE_SAMPLER == only("KSamplerSelect", reachable)["sampler_name"]
    assert LTX25_ENHANCE_SEED == only("RandomNoise", reachable)["noise_seed"]
    assert LTX25_ENHANCE_PROMPT == only("CLIPTextEncode", reachable)["text"] == ""
    assert LTX25_ENHANCE_LARGEST_SIZE == only("INTConstant", reachable)["value"]
    # The detailer sits one level down, inside the rgthree loader's widget dict — which is
    # exactly why the adapter substitutes a node whose schema can see it.
    rgthree = only("Power Lora Loader (rgthree)", reachable)
    rows = [value for key, value in rgthree.items() if key.startswith("lora_")]
    assert len(rows) == 1 and rows[0]["on"] is True, rgthree
    assert LTX25_ENHANCE_DETAILER_LORA == rows[0]["lora"]
    assert LTX25_ENHANCE_DETAILER_STRENGTH == rows[0]["strength"]
    # And the values reach the payload. One rgthree row with a single strength patches the
    # model and the CLIP alike, which is what the substituted `LoraLoader` reproduces.
    detailer = payload["mvp:detailer"]["inputs"]
    assert detailer["lora_name"] == LTX25_ENHANCE_DETAILER_LORA
    assert detailer["strength_model"] == detailer["strength_clip"] == LTX25_ENHANCE_DETAILER_STRENGTH
    assert payload["mvp:prompt"]["inputs"]["text"] == ""


def test_the_enhance_payload_reproduces_the_exports_tiling_and_scaling():
    """The rest of the executed path, value for value, against the export's reachable nodes.

    Includes the temporal tiling on purpose. It is not exposed and it is not tuned here, but
    it is the thing most likely to change the output's frame count — so the numbers being the
    export's is the only reason the live measurement will describe the audited graph.
    """
    export = ltx25_enhancer_export()
    reachable = reachable_node_ids(export, [LTX25_ENHANCER_OUTPUT_NODE])
    payload = ltx25_enhance_payload()

    def exported(class_type: str) -> dict:
        matches = [
            node_id for node_id in reachable if export[node_id]["class_type"] == class_type
        ]
        assert len(matches) == 1, f"{class_type}: {matches}"
        return export[matches[0]]["inputs"]

    def built(class_type: str) -> dict:
        matches = [
            node_id for node_id, node in payload.items() if node["class_type"] == class_type
        ]
        assert len(matches) == 1, f"{class_type}: {matches}"
        return payload[matches[0]]["inputs"]

    for class_type in (
        "ImageScaleToMaxDimension",
        "VAEEncodeTiled",
        "LTXVLoopingSampler",
        "LTXVSpatioTemporalTiledVAEDecode",
    ):
        # Literal values only: the links differ because the node ids do, and they are
        # compared structurally by the reachability test above.
        source_values = {
            name: value
            for name, value in exported(class_type).items()
            if not isinstance(value, list)
        }
        assert {
            name: value for name, value in built(class_type).items() if name in source_values
        } == source_values, class_type
    assert built("LTXVLoopingSampler")["temporal_tile_size"] == 56
    assert built("LTXVLoopingSampler")["temporal_overlap"] == 24


def test_the_enhance_payload_takes_its_frame_rate_from_the_source_rather_than_a_literal():
    """A number this adapter invented would silently retime the enhanced take.

    `VHS_VideoInfo`'s output 0 is `source_fps`, and both the saver and the LTX conditioning
    read it — as the export does. Nothing here asserts anything about frame *count*: that is
    a live measurement, and this is about rate.
    """
    payload = ltx25_enhance_payload()

    assert payload["mvp:source_info"]["inputs"]["video_info"] == ["mvp:source", 3]
    assert payload["mvp:save"]["inputs"]["frame_rate"] == ["mvp:source_info", 0]
    assert payload["mvp:conditioning"]["inputs"]["frame_rate"] == ["mvp:source_info", 0]


@pytest.mark.parametrize(
    "source",
    [
        "",
        "   ",
        "J:/comfy/output/take_00001.png",
        "J:/comfy/output/take_00001",
        '"J:/comfy/output/take_00001.mp4"',
        " J:/comfy/output/take_00001.mp4 ",
    ],
)
def test_the_builder_refuses_a_source_the_loader_cannot_open_as_given(source: str):
    """Refused locally so the reason is legible, rather than as a `/prompt` rejection.

    The quoted and padded cases are not pedantry: VHS strips both off the path before opening
    it, so a value like that names one file to whoever checked it exists and a different one
    to the node.
    """
    with pytest.raises(ValueError):
        ltx25_enhance_payload(source_video=source)


def test_the_builder_accepts_every_container_the_node_declares():
    """The other side of the refusal: the adapter's list is not narrower than the node's."""
    for extension in LTX25_ENHANCE_SOURCE_EXTENSIONS:
        payload = ltx25_enhance_payload(source_video=f"J:/comfy/output/take_00001.{extension}")
        assert payload["mvp:source"]["inputs"]["video"].endswith(extension)
    # Case is the file system's business, not the node's.
    assert ltx25_enhance_payload(source_video="J:/comfy/output/TAKE.MP4")


def test_the_enhance_payload_validates_against_the_recorded_object_info():
    """The offline half of the pre-flight, so a schema drift fails in CI rather than live."""
    object_info = recorded_object_info()
    label, payload = preflight_ltx25_enhance.audit_payloads()[0]

    assert preflight.validate(label, payload, object_info) == []
    assert preflight.unbounded_numeric_inputs(label, payload, object_info) == []


def test_the_enhance_audit_wires_every_check_it_defines():
    """A check dropped from `CHECKS` still passes its own test while the audit stops running it."""
    defined = {
        name
        for name in dir(preflight_ltx25_enhance)
        if name.startswith("check_") and callable(getattr(preflight_ltx25_enhance, name))
    }

    assert {check.__name__ for check in preflight_ltx25_enhance.CHECKS} == defined


def test_each_enhance_check_passes_the_recorded_schema_and_names_a_moved_one():
    """Every check is exercised in both directions: clean, and against a schema that moved."""
    object_info = recorded_object_info()

    for check in preflight_ltx25_enhance.CHECKS:
        assert check(object_info) == [], check.__name__

    moved = copy.deepcopy(object_info)
    moved["UNETLoader"]["input"]["required"]["unet_name"][0] = ["something-else.safetensors"]
    assert any(
        "ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors" in problem
        for problem in preflight_ltx25_enhance.check_model_files(moved)
    )

    moved = copy.deepcopy(object_info)
    moved["VHS_LoadVideoPath"]["input"]["required"]["video"][1]["vhs_path_extensions"] = ["mp4"]
    assert preflight_ltx25_enhance.check_source_extensions(moved) != []


def test_the_enhance_audit_refuses_a_dependency_list_built_from_the_node_list(monkeypatch):
    """The mutation this whole design exists to survive, driven through the audit itself.

    A payload that also built the export's orphaned loaders — the shape a node-list scan
    produces — must fail the reachability check by name, and must fail it for both files.
    """
    honest = preflight_ltx25_enhance.audit_payloads()

    def from_the_node_list() -> list[tuple[str, dict]]:
        label, payload = honest[0]
        payload = {
            **copy.deepcopy(payload),
            "mvp:audio_vae": {
                "class_type": "VAELoaderKJ",
                "inputs": {
                    "vae_name": "ltx-2.5-audio-vae-bf16.safetensors",
                    "device": "cpu",
                    "weight_dtype": "bf16",
                },
            },
            "mvp:latent_upscaler": {
                "class_type": "LatentUpscaleModelLoader",
                "inputs": {
                    "model_name": "ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors"
                },
            },
        }
        return [(label, payload)]

    monkeypatch.setattr(preflight_ltx25_enhance, "audit_payloads", from_the_node_list)

    problems = preflight_ltx25_enhance.check_dependencies_come_from_the_reachable_subgraph(
        recorded_object_info()
    )

    assert len(problems) >= 2
    for filename in (
        "ltx-2.5-audio-vae-bf16.safetensors",
        "ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors",
    ):
        assert any(filename in problem and "orphaned" in problem for problem in problems), problems


def test_nothing_in_the_enhancement_path_claims_the_frame_count_is_preserved():
    """The frozen "Never", enforced against the source rather than trusted.

    The reference chain's LTX stage turned 192 frames into 185, this graph tiles temporally,
    and what it does to a frame count is unknown until it is measured. So no builder, route,
    pre-flight or test on this path may assert that an output frame count equals an input's —
    including this file. The check is a read of the source text because the failure mode is a
    line of code nobody wrote yet.
    """
    sources = [
        REPO_ROOT / "src/music_video_producer/workflows.py",
        REPO_ROOT / "src/music_video_producer/app.py",
        REPO_ROOT / "tests/preflight_ltx25_enhance.py",
        REPO_ROOT / "tests/test_api.py",
        Path(__file__),
    ]
    # `frame_count` is the loader's second output name, and an assertion *about* it is what
    # this forbids. The needle is spliced from two pieces so this line does not match itself:
    # written whole, the guard's only finding would be the guard.
    pattern = re.compile(r"assert[^\n]*" + "frame" + "_count")

    for source in sources:
        text = source.read_text(encoding="utf-8")
        assert not pattern.search(text), source
    # And the adapter never caps or resamples the loaded frames, which would make the count a
    # thing this application chose rather than a thing it measured.
    payload = ltx25_enhance_payload()
    assert payload["mvp:source"]["inputs"]["frame_load_cap"] == 0
    assert payload["mvp:source"]["inputs"]["select_every_nth"] == 1
    assert payload["mvp:source"]["inputs"]["force_rate"] == 0


# --- Song-audio restoration ----------------------------------------------------------------
#
# The audited evidence for the restoration adapter, its single output node, and the digest that
# says the file has not moved under the tests that read it.
AUDIOREPLACER_EXPORT = REFERENCE_EXPORTS / "ltx25-audioreplacer-user-export.json"
AUDIOREPLACER_EXPORT_SHA256 = (
    "9cf755fe83588b6f15fc8c363bf363150fb1f6f5051df2adb482e22e4d4ebb5d"
)
AUDIOREPLACER_OUTPUT_NODE = "4"

# The two node classes the adapter substitutes and the one it adds, declared as data so the
# comparison below can hold every other class to equality: a fourth node arriving quietly would
# otherwise disappear into a looser assertion. Each is justified in
# `build_audio_replace_payload`. Both substitutes keep the same outputs in the same order as the
# loaders they replace, which is why the wiring comparison further down is a like-for-like one.
AUDIO_REPLACE_SUBSTITUTIONS = {
    "VHS_LoadVideo": "VHS_LoadVideoPath",
    "LoadAudio": "VHS_LoadAudio",
}
# Not a substitution: an addition, and the only one. The export hardcodes `frame_rate: 25` on
# its saver; this reads the take's own rate instead. Declared here so the class comparison can
# allow exactly this one extra node and no other.
AUDIO_REPLACE_ADDITIONS = ("VHS_VideoInfo",)


def audioreplacer_export() -> dict:
    return json.loads(AUDIOREPLACER_EXPORT.read_text(encoding="utf-8"))


def audio_replace_payload(**overrides) -> dict:
    arguments = {
        "source_video": "J:/comfy/output/music-video-producer/p/shots/s-h3-reference_00001.mp4",
        "source_audio": "F:/MusicVideoProducer/data/projects/p/media/songs/master.mp3",
        "start": 12.0,
        "duration": 3.75,
        "song_duration": 154.644898,
        "prefix": "music-video-producer/p/shots/s-song-audio",
    }
    return build_audio_replace_payload(**{**arguments, **overrides})


def test_the_audioreplacer_export_is_not_mutated():
    """The evidence every test below reads, pinned. It is immutable per AGENTS.md."""
    digest = hashlib.sha256(AUDIOREPLACER_EXPORT.read_bytes()).hexdigest()

    assert digest == AUDIOREPLACER_EXPORT_SHA256


def test_the_audioreplacer_export_carries_five_orphaned_loaders_and_reaches_three_nodes():
    """The trap, measured off the file rather than restated from the spec.

    8 nodes, 3 reachable from the single `VHS_VideoCombine`. The five that are not are a
    `UNETLoader`, two `VAELoaderKJ`, a `CLIPLoader` and a `LatentUpscaleModelLoader`, inherited
    from the parent LTX graph this export was cut out of. All five name a real model file, and
    all five files would end up in a dependency list built from `export.values()` — which for
    this graph is not a near miss but the whole list, because **the reachable subgraph loads
    nothing at all**. Asserted here before anything asserts the adapter avoids it.
    """
    export = audioreplacer_export()

    reachable = reachable_node_ids(export, [AUDIOREPLACER_OUTPUT_NODE])
    orphaned = set(export) - reachable

    assert len(export) == 8
    assert len(reachable) == 3
    assert {export[node_id]["class_type"] for node_id in reachable} == {
        "LoadAudio",
        "VHS_LoadVideo",
        "VHS_VideoCombine",
    }
    assert {export[node_id]["class_type"] for node_id in orphaned} == {
        "UNETLoader",
        "VAELoaderKJ",
        "CLIPLoader",
        "LatentUpscaleModelLoader",
    }
    # The five files a node-list dependency scan would demand. Named rather than counted,
    # because "some files" and "a 22B transformer plus two VAEs plus a Gemma text encoder plus
    # a spatial upscaler" are different sentences to a Director whose machine was just refused.
    assert preflight_audio_replace.export_model_files(orphaned) == {
        "ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors",
        "ltx-2.5-video-vae-conv-bf16.safetensors",
        "ltx-2.5-audio-vae-bf16.safetensors",
        "gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors",
        "ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors",
    }
    # And the executed path loads none of them, which is the fact the adapter is built on.
    assert preflight_audio_replace.export_model_files(reachable) == set()


def test_the_audio_replace_payload_is_the_reachable_subgraph_and_one_declared_addition():
    """AC: the adapter is built from the 3, not from the 8.

    Compared as a class multiset against the export's own reachable nodes, with the two
    substitutions and the single declared addition applied from the constants above — so a node
    quietly added or dropped fails here, and so does a third substitution nobody declared.
    """
    export = audioreplacer_export()
    reachable = reachable_node_ids(export, [AUDIOREPLACER_OUTPUT_NODE])
    expected = sorted(
        [
            AUDIO_REPLACE_SUBSTITUTIONS.get(
                export[node_id]["class_type"], export[node_id]["class_type"]
            )
            for node_id in reachable
        ]
        + list(AUDIO_REPLACE_ADDITIONS)
    )

    payload = audio_replace_payload()

    assert sorted(node["class_type"] for node in payload.values()) == expected
    # And the payload is itself fully reachable from its own output: a node built here that
    # nothing downstream reads would be dead weight submitted to ComfyUI.
    assert reachable_node_ids(payload, ["mvp:save"]) == set(payload)


def test_the_audio_replace_payload_names_no_model_file_at_all():
    """The specific harm the orphans would do, stated as the absence that is the whole point.

    Derived on both sides: the expectation is read out of the export's reachable nodes and the
    payload's list is read out of the payload. Neither is a list typed into this test. This
    graph decodes a video, slices an audio file and muxes them; it has no UNET, no VAE, no CLIP
    and no LoRA, so a pre-flight that demanded any model here would refuse a machine that needs
    none of them.
    """
    export = audioreplacer_export()
    reachable = reachable_node_ids(export, [AUDIOREPLACER_OUTPUT_NODE])
    orphaned = set(export) - reachable

    loaded = preflight_audio_replace.payload_model_files([("t", audio_replace_payload())])

    assert loaded == preflight_audio_replace.export_model_files(reachable) == set()
    # Named, because "equal to the reachable set" alone would still pass if the reachable set
    # had somehow acquired them.
    assert len(preflight_audio_replace.export_model_files(orphaned)) == 5
    # No sampler, no guider, no conditioning: nothing that could regenerate a frame.
    classes = {node["class_type"] for node in audio_replace_payload().values()}
    assert not any(
        marker in name
        for name in classes
        for marker in ("Sampler", "Guider", "VAE", "CLIP", "UNET", "Lora", "MiniMax", "LTXV")
    ), classes


def test_the_restore_window_comes_from_song_audio_window_and_not_a_second_computation(
    monkeypatch,
):
    """The single correctness argument of this whole stage, driven as a mutation.

    `song_audio_window` is replaced with one that returns a window nothing else could produce.
    If the builder computed the window itself — even with arithmetic that agrees today — the
    payload would carry 12 to 15.75 and this would fail. It carries the replacement's numbers
    instead, which is only possible if the shared function is the single source of the window.

    That is the failure this guards: two independent computations of "which seconds is this
    shot" drift, and the symptom is a subtle desync rather than an error.
    """
    calls: list[dict] = []

    def only_this_function_could_have_produced_it(*, start, duration, song_duration):
        calls.append({"start": start, "duration": duration, "song_duration": song_duration})
        return {"start": 101.5, "end": 108.25}

    monkeypatch.setattr(
        workflows_module, "song_audio_window", only_this_function_could_have_produced_it
    )

    payload = audio_replace_payload(start=12.0, duration=3.75, song_duration=154.644898)

    # Called with the render's own three numbers, unmodified on the way.
    assert calls == [{"start": 12.0, "duration": 3.75, "song_duration": 154.644898}]
    # And the payload is that function's answer, not the builder's own arithmetic.
    window = payload["mvp:song"]["inputs"]
    assert window["seek_seconds"] == 101.5
    assert window["duration"] == pytest.approx(6.75)


def test_the_restore_builder_offers_no_window_parameter_for_a_caller_to_disagree_through():
    """The structural half of the argument above: there is nothing to pass a window as.

    The mutation test proves the builder *uses* the shared function. This proves a caller
    cannot route around it — the signature takes the three numbers the render was given and
    accepts no window, no start second, no end second and no trim. A parameter added here
    would be the seam a second computation arrives through, so the signature is pinned.
    """
    parameters = inspect.signature(build_audio_replace_payload).parameters

    assert set(parameters) == {
        "source_video",
        "source_audio",
        "start",
        "duration",
        "song_duration",
        "prefix",
    }
    # Keyword-only throughout, so no positional call can transpose start and duration.
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY for parameter in parameters.values()
    )


def test_a_shot_running_past_the_end_of_the_song_is_refused_in_the_renders_own_words():
    """The matrix's window-past-the-end row: inherited, never restated.

    Compared against `song_audio_window`'s own message rather than against a string typed here,
    because a second refusal written in this file would be exactly the duplicated rule the spec
    forbids — and would drift from the render's wording the first time either changed.
    """
    with pytest.raises(ValueError) as shared:
        song_audio_window(start=152.0, duration=5.0, song_duration=154.644898)
    with pytest.raises(ValueError) as inherited:
        audio_replace_payload(start=152.0, duration=5.0, song_duration=154.644898)

    assert str(inherited.value) == str(shared.value)
    # A song whose length was never recorded compares against an unknown, so it is not refused —
    # the shared function's rule, inherited whole rather than tightened here.
    assert audio_replace_payload(start=152.0, duration=5.0, song_duration=0)


def test_the_restored_file_carries_the_master_and_never_the_takes_generated_audio():
    """The wiring that is the entire feature, and the diagnostic it deliberately leaves behind.

    The saver's audio is the song loader's first output. The take's *generated* audio is the
    source loader's third output, and it is wired nowhere: replacing it is the point. It stays
    in the take, which this graph only ever reads — hearing what H3 actually produced is what
    found a real conditioning bug on 2026-08-18.
    """
    payload = audio_replace_payload()

    assert payload["mvp:save"]["inputs"]["audio"] == ["mvp:song", 0]
    assert payload["mvp:save"]["inputs"]["images"] == ["mvp:source", 0]
    assert payload["mvp:song"]["class_type"] == "VHS_LoadAudio"
    # The take's own audio output goes nowhere at all.
    links = [
        value
        for node in payload.values()
        for value in node["inputs"].values()
        if isinstance(value, list)
    ]
    assert ["mvp:source", 2] not in links
    # And nothing in this graph can synthesise a track: there is no audio decode of any kind.
    assert not any("Decode" in node["class_type"] for node in payload.values())


def test_the_restore_payload_does_not_conform_the_takes_frames_the_way_the_export_would():
    """The adapter's one substantive departure from the export, and why it is not optional.

    The export loads its video with `format: "LTXV"`, which live `/object_info` defines as a
    24 fps target, a 32-pixel dimension floor and an `8n+1` frame rule. An H3 take lands on the
    17k+5 grid — 90 frames for a 3.75 s shot — so inheriting that would cut the picture to 89
    and leave the restored song running one frame long against it. `"None"` conforms nothing.
    """
    export = audioreplacer_export()
    loader = next(node for node in export.values() if node["class_type"] == "VHS_LoadVideo")

    assert loader["inputs"]["format"] == "LTXV"
    assert AUDIO_REPLACE_SOURCE_FORMAT == "None"

    source = audio_replace_payload()["mvp:source"]["inputs"]

    assert source["format"] == AUDIO_REPLACE_SOURCE_FORMAT
    # Nothing else resamples, caps or skips either.
    assert source["force_rate"] == 0
    assert source["frame_load_cap"] == 0
    assert source["skip_first_frames"] == 0
    assert source["select_every_nth"] == 1
    assert source["custom_width"] == 0
    assert source["custom_height"] == 0
    # 90 frames is what a 3.75 s H3 shot is, and 8n+1 does not contain it — the arithmetic the
    # paragraph above rests on, computed rather than asserted from memory.
    frames = align_h3_frames(max(5, round(3.75 * H3_FRAME_RATE)))
    assert frames == 90
    assert (frames - 1) % 8 != 0


def test_the_restore_payload_takes_its_frame_rate_from_the_take_rather_than_the_exports_25():
    """A number this adapter invented would silently retime the picture.

    The export's saver hardcodes 25 fps, which is the creator's own footage. An H3 take is 24,
    and writing its frames out at 25 plays the picture 4% fast under a soundtrack that is not
    sped up at all — a desync produced by the one stage whose purpose is to remove one.
    """
    export = audioreplacer_export()
    saver = next(node for node in export.values() if node["class_type"] == "VHS_VideoCombine")

    assert saver["inputs"]["frame_rate"] == 25

    payload = audio_replace_payload()

    assert payload["mvp:save"]["inputs"]["frame_rate"] == ["mvp:source_info", 0]
    assert payload["mvp:source_info"]["inputs"]["video_info"] == ["mvp:source", 3]
    # And no literal frame rate survives anywhere in the payload.
    assert not any(
        isinstance(node["inputs"].get("frame_rate"), (int, float)) for node in payload.values()
    )


def test_the_restore_payload_never_pads_or_cuts_the_picture_to_the_audio():
    """The matrix's length-mismatch row, as the saver setting that makes the report honest."""
    payload = audio_replace_payload()

    assert payload["mvp:save"]["inputs"]["trim_to_audio"] is False
    assert payload["mvp:save"]["inputs"]["pingpong"] is False
    assert payload["mvp:save"]["inputs"]["loop_count"] == 0
    # The container the export writes, reproduced.
    assert payload["mvp:save"]["inputs"]["format"] == AUDIO_REPLACE_FORMAT == "video/h264-mp4"
    assert payload["mvp:save"]["inputs"]["pix_fmt"] == AUDIO_REPLACE_PIX_FMT
    assert payload["mvp:save"]["inputs"]["crf"] == AUDIO_REPLACE_CRF


def test_restore_lengths_report_the_grid_rounding_rather_than_asserting_agreement():
    """The two numbers, and the case where they differ.

    A 3.75 s shot is 90 frames, which is 3.75 s exactly. A 5 s shot is 124, which is 5.1667 s —
    a sixth of a second of picture past the end of its own audio window. That mismatch is real,
    computable before anything is submitted, and reported rather than corrected.
    """
    exact = audio_replace_lengths(duration=3.75)

    assert exact["audio_seconds"] == 3.75
    assert exact["requested_picture_seconds"] == 3.75
    assert exact["requested_frames"] == 90

    rounded = audio_replace_lengths(duration=5.0)

    assert rounded["audio_seconds"] == 5.0
    assert rounded["requested_frames"] == 124
    assert rounded["requested_picture_seconds"] == pytest.approx(124 / 24)
    assert rounded["requested_picture_seconds"] > rounded["audio_seconds"]
    # Read off `align_h3_frames` rather than a copy of the grid, so a shot that rendered
    # through that function is measured against that function.
    for duration in (0.1, 3.75, 5.0, 8.0, 12.5, 15.0):
        assert audio_replace_lengths(duration=duration)["requested_frames"] == align_h3_frames(
            max(5, round(duration * H3_FRAME_RATE))
        )


def test_the_restore_builder_refuses_paths_the_loaders_could_not_open():
    """Both extension lists and both VHS path-rewriting refusals, per loader."""
    for extension in AUDIO_REPLACE_VIDEO_EXTENSIONS:
        assert audio_replace_payload(source_video=f"J:/o/take_00001.{extension}")
    for extension in AUDIO_REPLACE_AUDIO_EXTENSIONS:
        assert audio_replace_payload(source_audio=f"F:/d/master.{extension}")
    # Case is the caller's, not a reason to refuse.
    assert audio_replace_payload(source_video="J:/o/TAKE.MP4", source_audio="F:/d/M.FLAC")

    with pytest.raises(ValueError, match="webm, mp4, mkv, gif, mov"):
        audio_replace_payload(source_video="J:/o/take_00001.wav")
    with pytest.raises(ValueError, match="wav, mp3, ogg, m4a, flac"):
        audio_replace_payload(source_audio="F:/d/master.mp4")
    # VHS strips whitespace and one surrounding quote before opening, on both loaders, so a
    # path it would rewrite is refused rather than silently repointed.
    for arguments in (
        {"source_video": ' "J:/o/take_00001.mp4" '},
        {"source_audio": ' "F:/d/master.mp3" '},
    ):
        with pytest.raises(ValueError, match="quoted or padded"):
            audio_replace_payload(**arguments)
    for arguments in ({"source_video": "  "}, {"source_audio": ""}, {"prefix": " "}):
        with pytest.raises(ValueError):
            audio_replace_payload(**arguments)


def test_the_audio_replace_payload_validates_against_the_recorded_object_info():
    """The offline half of the pre-flight, so a schema drift fails in CI rather than live."""
    object_info = recorded_object_info()
    label, payload = preflight_audio_replace.audit_payloads()[0]

    assert preflight.validate(label, payload, object_info) == []
    assert preflight.unbounded_numeric_inputs(label, payload, object_info) == []


def test_the_audio_replace_audit_wires_every_check_it_defines():
    """A check dropped from `CHECKS` still passes its own test while the audit stops running it."""
    defined = {
        name
        for name in dir(preflight_audio_replace)
        if name.startswith("check_") and callable(getattr(preflight_audio_replace, name))
    }

    assert {check.__name__ for check in preflight_audio_replace.CHECKS} == defined


def test_each_audio_replace_check_passes_the_recorded_schema_and_names_a_moved_one():
    """Every check is exercised in both directions: clean, and against a schema that moved."""
    object_info = recorded_object_info()

    for check in preflight_audio_replace.CHECKS:
        assert check(object_info) == [], check.__name__

    moved = copy.deepcopy(object_info)
    moved["VHS_LoadAudio"]["input"]["required"]["audio_file"][1]["vhs_path_extensions"] = ["mp3"]
    assert preflight_audio_replace.check_path_extensions(moved) != []

    # The window inputs disappearing is the failure that would otherwise be silent: ComfyUI
    # drops an input it does not know, so the whole song would play over the shot.
    moved = copy.deepcopy(object_info)
    del moved["VHS_LoadAudio"]["input"]["optional"]["seek_seconds"]
    assert any(
        "seek_seconds" in problem
        for problem in preflight_audio_replace.check_the_window_inputs_are_the_nodes_own(moved)
    )

    # A substitution that stopped being possible.
    moved = copy.deepcopy(object_info)
    moved["VHS_LoadAudio"]["input"]["required"]["audio_file"][0] = "COMBO"
    assert (
        preflight_audio_replace.check_the_substituted_loaders_are_the_only_reachable_ones(moved)
        != []
    )

    # And the format departure losing its justification in either direction.
    moved = copy.deepcopy(object_info)
    moved["VHS_LoadVideoPath"]["input"]["optional"]["format"][1]["formats"]["None"] = {
        "frames": [8, 1]
    }
    assert preflight_audio_replace.check_the_source_format_conforms_nothing(moved) != []

    moved = copy.deepcopy(object_info)
    moved["VHS_LoadVideoPath"]["input"]["optional"]["format"][1]["formats"]["LTXV"] = {}
    assert preflight_audio_replace.check_the_source_format_conforms_nothing(moved) != []


def test_the_audio_replace_audit_refuses_a_dependency_list_built_from_the_node_list(monkeypatch):
    """The mutation this whole design exists to survive, driven through the audit itself.

    A payload that also built the export's five orphaned loaders — the shape a node-list scan
    produces — must fail the reachability check, and must name every one of the five files.
    """
    honest = preflight_audio_replace.audit_payloads()
    export = audioreplacer_export()
    orphaned = set(export) - reachable_node_ids(export, [AUDIOREPLACER_OUTPUT_NODE])

    def from_the_node_list() -> list[tuple[str, dict]]:
        label, payload = honest[0]
        inherited = {
            f"mvp:orphan_{node_id}": copy.deepcopy(export[node_id]) for node_id in orphaned
        }
        return [(label, {**copy.deepcopy(payload), **inherited})]

    monkeypatch.setattr(preflight_audio_replace, "audit_payloads", from_the_node_list)

    problems = preflight_audio_replace.check_dependencies_come_from_the_reachable_subgraph(
        recorded_object_info()
    )

    assert problems
    for filename in preflight_audio_replace.export_model_files(orphaned):
        assert any(filename in problem for problem in problems), problems


def test_nothing_on_the_restore_path_claims_the_frame_count_is_preserved():
    """The matrix's frame-count row, enforced against the source rather than trusted.

    What the restored file contains is an `ffprobe` reading of two files. No builder, route,
    pre-flight or test on this path may assert that an output frame count equals an input's —
    including this file. The check is a read of the source text because the failure mode is a
    line of code nobody wrote yet.
    """
    sources = [
        REPO_ROOT / "src/music_video_producer/workflows.py",
        REPO_ROOT / "src/music_video_producer/app.py",
        REPO_ROOT / "tests/preflight_audio_replace.py",
        REPO_ROOT / "tests/test_api.py",
        Path(__file__),
    ]
    # Spliced from two pieces for the reason the enhancement guard states: written whole, the
    # guard's only finding would be the guard.
    pattern = re.compile(r"assert[^\n]*" + "frame" + "_count")

    for source in sources:
        text = source.read_text(encoding="utf-8")
        assert not pattern.search(text), source
    # And what the route reports is named as a *request*, never as a measurement.
    assert "requested_picture_seconds" in audio_replace_lengths(duration=3.75)
    assert "picture_seconds" not in audio_replace_lengths(duration=3.75)
