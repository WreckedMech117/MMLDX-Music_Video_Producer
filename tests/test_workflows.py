import ast
import copy
import hashlib
import importlib
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
import preflight_ltx25_extend
import preflight_songplanner
import pytest

# The module object as well as the names, because one test below has to replace
# `song_audio_window` *where the builder looks it up* — see
# `test_the_restore_window_comes_from_song_audio_window_and_not_a_second_computation`.
from music_video_producer import workflows as workflows_module
from music_video_producer.app import SongPlannerRequest
from music_video_producer.timeline import (
    H3_MIN_RENDER_FRAMES,
    OVER_RENDER_SECONDS,
    align_h3_frames,
    margin_frames,
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
    H3_ATTENTION_CHAIN_POSITION,
    H3_ATTENTION_NODE_INPUTS,
    H3_ATTENTION_PROFILES,
    H3_DEFAULT_ASPECT_RATIO,
    H3_DEFAULT_ATTENTION,
    H3_DEFAULT_MEGAPIXELS,
    H3_DEFAULT_MULTIPLE,
    H3_DEFAULT_PROFILE,
    H3_DIRECTOR_DEFAULT_STEPS,
    H3_DIRECTOR_MAX_FRAMES,
    H3_DIRECTOR_MAX_SECONDS,
    H3_FRAME_RATE,
    H3_IMAGE_EDIT_PROFILES,
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
    H3AttentionProfile,
    H3SamplingProfile,
    WorkflowCatalog,
    audio_replace_lengths,
    build_audio_replace_payload,
    build_flux_payload,
    build_h3_director_payload,
    build_h3_image_edit_payload,
    build_h3_keyframe_payload,
    build_h3_reference_payload,
    build_ltx25_enhance_payload,
    build_ltx25_extend_payload,
    build_multiview_payload,
    build_music3_payload,
    build_songplanner_invented_payload,
    build_songplanner_known_lyrics_payload,
    normalize_to_divisor,
    partition_h3_references,
    patch_ltx25_dimension_boundary,
    reachable_node_ids,
    resolve_h3_attention,
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
        # Both shapes of the extension adapter: `include_audio` decides whether the audio
        # decode, trim and concat are built at all, so the two are different graphs rather
        # than one graph with a different number in it.
        *preflight_ltx25_extend.audit_payloads(),
        (
            # Also one shape: this builder has no sampling to vary and no model to swap.
            "audio-replace",
            build_audio_replace_payload(
                source_video="J:/comfy/output/take_00001.mp4",
                source_audio="F:/data/master.mp3",
                start=12.0,
                duration=3.75,
                song_duration=154.644898,
                take_lead=0.25,
                prefix="p",
            ),
        ),
        (
            # AI Mod's two evidenced bundles, each its own graph (the turbo one draws the
            # chain through a LoRA), multi-picture on the default so the raw-splitter
            # slots are range-checked too.
            "h3-image-edit",
            build_h3_image_edit_payload(
                prompt="subject_definitions:\n<Picture 1> is the base image being edited.",
                pictures=[{"file": "a.png"}, {"file": "b.png"}],
                seed=0,
                prefix="p",
            ),
        ),
        (
            "h3-image-edit-turbo",
            build_h3_image_edit_payload(
                prompt="subject_definitions:\n<Picture 1> is the base image being edited.",
                pictures=[{"file": "a.png"}],
                seed=0,
                profile="turbo",
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
#
# `preflight_ltx25_extend.py --record` removed ten, and the tenth is the one worth naming:
# `LatentUpscaleModelLoader` is off this list at last. It stayed on it through two adapters
# because it is an *orphan* in both of their exports — and it is genuinely reached in the
# video-extension export, where it feeds `LTXVLatentUpsampler`. The same reachability walk that
# kept it out of those payloads is what puts it into this one, which is the whole argument for
# deriving dependencies rather than pattern-matching a class name. The other nine —
# `ImageResizeKJv2`, `PrimitiveFloat`, `VAEDecodeTiled` and the six `LTXV*` audio and latent
# classes — were uncovered only because the *unaudited* combined LTX export used them; the
# extension adapter submits all nine, so they are range-checked offline everywhere they appear.
# Five more classes joined the fixture through `extra_classes` without any payload submitting
# them: `SimpleCalculatorKJ`, `ResizeImageMaskNode`, `ImpactImageInfo`, `CM_IntToFloat` and
# `Power Lora Loader (rgthree)` are the nodes that adapter *replaced*, recorded because the
# claim it makes — that each was replaced for its schema shape and not for being missing — is
# checked against their schemas.
UNRECORDED_CLASSES = frozenset({
    "ComfyMathExpression",
    "DualCLIPLoader", "EmptySD3LatentImage", "FluxGuidance", "FrameInterpolate",
    "FrameInterpolationModelLoader", "GetImageSize",
    "Krea2EditGroundedEncode", "Krea2EditModelPatch",
    "LoadImage",
    "MathExpression|pysssss",
    "ModelSamplingFlux",
    "PrimitiveStringMultiline", "RTXVideoSuperResolution",
    "SeedVR2LoadDiTModel", "SeedVR2LoadVAEModel",
    "SeedVR2VideoUpscaler", "SetLatentNoiseMask", "SolidMask",
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
    """The `video` kind, which no other test builds, and its optional paired audio.

    **`"separate"` is not evidence about `audio_mode`.** The loader node branches on `"paired"`
    and `"standalone"` only, so this third spelling falls through both tests and leaves the
    soundtrack unsent — it agreed with the builder by coincidence rather than by rule. Kept as
    written because the *outcome* it asserts is right and pinning it costs nothing; the two
    branches the node actually has are pinned by
    `test_h3_partition_sends_no_soundtrack_for_an_audio_mode_the_node_does_not_know` and
    `test_h3_partition_gives_a_standalone_videos_track_an_audio_slot_of_its_own`.
    """
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


# --- The loader node's partition, mirrored ---------------------------------------------
#
# `partition_h3_references` models `MiniMaxH3MediaLoader._partition` in the GPL extension
# `ComfyUI-Fantastic-MiniMaxH3-PromptBuilder`. That node cannot be imported — outside this
# project, not a dependency — so these tests are the pin: each one states a rule of the node's,
# read off its source, as its expectation. If the node changes, the mirror is wrong and these
# say which rule it now disagrees with. Everything here is offline; no render proves or is
# needed to prove any of it.
#
# The rules being pinned, in the node's own order of application:
#   1. an item whose `enabled` is exactly `False` is skipped and consumes no slot;
#   2. a picture joins the picture group in list order;
#   3. a video always joins the video group and always adds one entry to the paired-audio
#      group, so the two are positional — video N's soundtrack is paired-audio N;
#   4. that entry is the video's own track only when it has audio and `audio_mode` reads
#      `paired` (the default when the key is absent), and is empty otherwise;
#   5. a video with audio in `standalone` mode *also* joins the audio group, where the video
#      sits in the list, displacing every `audio` reference after it;
#   6. an `audio` joins the audio group in list order.


def slot_tuples(references: list[dict]) -> list[tuple[str, int, int]]:
    """The partition as `(group, slot, source index)`, which is all these tests read."""
    return [(entry.group, entry.slot, entry.index) for entry in partition_h3_references(references)]


def wired_slots(payload: dict) -> dict[str, list]:
    """Only the four media groups' wiring out of a built conditioner."""
    prefixes = ("ref_images.", "ref_videos.", "ref_video_audios.", "ref_audios.")
    return {
        name: value
        for name, value in payload["mvp:condition"]["inputs"].items()
        if name.startswith(prefixes)
    }


def test_h3_partition_closes_the_numbering_up_around_a_switched_off_reference():
    """Rule 1, on its own: a disabled item takes no slot and shifts nothing after it.

    The defect this replaced wired the third picture to split output 2 while the loader had put
    it at output 1, so the conditioner read a slot the node never filled and the slot it did
    fill went nowhere. Both halves are asserted: the numbering, and the payload built from it.
    """
    references = [
        {"kind": "picture", "file": "F:/refs/a.png"},
        {"kind": "picture", "file": "F:/refs/b.png", "enabled": False},
        {"kind": "picture", "file": "F:/refs/c.png"},
    ]
    assert slot_tuples(references) == [("picture", 0, 0), ("picture", 1, 2)]
    assert wired_slots(h3_reference_payload(references)) == {
        "ref_images.ref_image_0": ["mvp:split", 0],
        "ref_images.ref_image_1": ["mvp:split", 1],
    }
    # And with several off in a row, across two kinds, so the closing-up is not accidentally
    # right for a single gap only.
    several = [
        {"kind": "picture", "file": "F:/refs/a.png"},
        {"kind": "picture", "file": "F:/refs/b.png", "enabled": False},
        {"kind": "picture", "file": "F:/refs/c.png", "enabled": False},
        {"kind": "audio", "file": "F:/refs/x.flac", "enabled": False},
        {"kind": "picture", "file": "F:/refs/d.png"},
        {"kind": "audio", "file": "F:/refs/y.flac"},
    ]
    assert slot_tuples(several) == [("picture", 0, 0), ("picture", 1, 4), ("audio", 0, 5)]
    assert wired_slots(h3_reference_payload(several)) == {
        "ref_images.ref_image_0": ["mvp:split", 0],
        "ref_images.ref_image_1": ["mvp:split", 1],
        "ref_audios.ref_audio_0": ["mvp:split", 15],
    }


def test_h3_partition_reads_enabled_by_identity_the_way_the_node_does():
    """The node's test is `is False`, not falsiness, and the two differ for real payloads.

    `enabled: 0` and `enabled: null` survive a JSON round trip into `media_state` and are *not*
    off to the node. A mirror written as `not item.get("enabled", True)` would drop them, and
    every later slot of that kind would then be wired one place below where the loader put it —
    the original defect, reintroduced from the other side.
    """
    for kept in (0, None, "", "false"):
        references = [
            {"kind": "picture", "file": "F:/refs/a.png", "enabled": kept},
            {"kind": "picture", "file": "F:/refs/b.png"},
        ]
        assert slot_tuples(references) == [("picture", 0, 0), ("picture", 1, 1)], kept


def test_h3_partition_keeps_a_disabled_video_from_shifting_the_soundtracks():
    """Rules 1 and 3 together: the paired group stays positional against the video group."""
    references = [
        {"kind": "video", "file": "F:/refs/v1.mp4", "has_audio": True},
        {"kind": "video", "file": "F:/refs/v2.mp4", "has_audio": True, "enabled": False},
        {"kind": "video", "file": "F:/refs/v3.mp4", "has_audio": True},
    ]
    assert slot_tuples(references) == [
        ("video", 0, 0), ("video_audio", 0, 0), ("video", 1, 2), ("video_audio", 1, 2),
    ]
    assert wired_slots(h3_reference_payload(references)) == {
        "ref_videos.ref_video_0": ["mvp:split", 9],
        "ref_video_audios.ref_video_audio_0": ["mvp:split", 12],
        "ref_videos.ref_video_1": ["mvp:split", 10],
        "ref_video_audios.ref_video_audio_1": ["mvp:split", 13],
    }


def test_h3_partition_holds_a_silent_videos_place_in_the_paired_group():
    """Rules 3 and 4: a video with no audio still occupies its paired slot, unwired.

    The third video's soundtrack must be `video_audio_2`, not `video_audio_0`. Counting only
    the videos that *have* audio is the shape this test exists to fail.
    """
    references = [
        {"kind": "video", "file": "F:/refs/silent.mp4"},
        {"kind": "video", "file": "F:/refs/quiet.mp4", "has_audio": False},
        {"kind": "video", "file": "F:/refs/loud.mp4", "has_audio": True},
    ]
    assert slot_tuples(references) == [
        ("video", 0, 0), ("video", 1, 1), ("video", 2, 2), ("video_audio", 2, 2),
    ]
    assert wired_slots(h3_reference_payload(references)) == {
        "ref_videos.ref_video_0": ["mvp:split", 9],
        "ref_videos.ref_video_1": ["mvp:split", 10],
        "ref_videos.ref_video_2": ["mvp:split", 11],
        "ref_video_audios.ref_video_audio_2": ["mvp:split", 14],
    }


def test_h3_partition_gives_a_standalone_videos_track_an_audio_slot_of_its_own():
    """Rule 5, and the reason it matters: it displaces every `audio` reference after it.

    The node appends the extracted track to the `audios` group where the *video* sits in the
    list. So the master song that follows it is `audio_2`, and a builder that numbered the
    cited audios by themselves would wire the song to `audio_1` — the slot holding the video's
    soundtrack. The take would then be performed against a clip's audio while the payload said
    it was performed against the song.
    """
    after = [
        {"kind": "video", "file": "F:/refs/v.mp4", "has_audio": True, "audio_mode": "standalone"},
        {"kind": "audio", "file": "F:/refs/song.flac"},
    ]
    assert slot_tuples(after) == [("video", 0, 0), ("audio", 0, 0), ("audio", 1, 1)]
    assert wired_slots(h3_reference_payload(after)) == {
        "ref_videos.ref_video_0": ["mvp:split", 9],
        "ref_audios.ref_audio_0": ["mvp:split", 15],
        "ref_audios.ref_audio_1": ["mvp:split", 16],
    }
    # And an audio *before* the video keeps slot 0: the group fills in list order, so only what
    # follows the video moves. Asserted because "standalone audio always wins slot 0" would
    # pass the case above and be wrong here.
    before = [
        {"kind": "audio", "file": "F:/refs/song.flac"},
        {"kind": "video", "file": "F:/refs/v.mp4", "has_audio": True, "audio_mode": "standalone"},
    ]
    assert slot_tuples(before) == [("audio", 0, 0), ("video", 0, 1), ("audio", 1, 1)]


def test_h3_partition_sends_no_soundtrack_for_an_audio_mode_the_node_does_not_know():
    """Rule 4's edge, on values the node *actually* branches on and one it does not.

    The node compares `audio_mode` against `"paired"` and `"standalone"` and nothing else, so
    any third spelling falls through both and the track goes nowhere. `test_h3_reference_
    payload_wires_a_video_and_its_paired_soundtrack` passes `"separate"` — one of those third
    spellings — which is why it is not evidence about either branch: it agreed with the old
    builder by accident. Both real branches are pinned here.
    """
    absent = [{"kind": "video", "file": "F:/refs/v.mp4", "has_audio": True}]
    named = [{"kind": "video", "file": "F:/refs/v.mp4", "has_audio": True, "audio_mode": "paired"}]
    assert slot_tuples(absent) == slot_tuples(named) == [("video", 0, 0), ("video_audio", 0, 0)]
    for unknown in ("separate", "", "PAIRED", "Standalone", None):
        stray = [
            {"kind": "video", "file": "F:/refs/v.mp4", "has_audio": True, "audio_mode": unknown},
            {"kind": "audio", "file": "F:/refs/song.flac"},
        ]
        # No paired slot and no extra audio: the song keeps `audio_0`.
        assert slot_tuples(stray) == [("video", 0, 0), ("audio", 0, 1)], unknown


def test_h3_reference_payload_refuses_a_plan_whose_every_reference_is_switched_off():
    """Nothing overflows, nothing is unknown, and nothing would reach the model.

    Distinct from the empty-list refusal because the cause is distinct: references were
    attached and all of them are off. Before the partition was shared this built a payload
    wired to four slots the loader leaves empty, which renders at full cost on no media.
    """
    references = [
        {"kind": "picture", "file": "F:/refs/a.png", "enabled": False},
        {"kind": "audio", "file": "F:/refs/y.flac", "enabled": False},
    ]
    with pytest.raises(ValueError, match="All 2 H3 references are switched off"):
        h3_reference_payload(references)


def test_h3_reference_ceilings_satisfy_both_of_the_nodes_own_two_counts():
    """The loader counts its media twice and the two disagree, so the ceiling is the larger.

    `VALIDATE_INPUTS` counts every item of a kind in `media_state`, switched-off ones included,
    and refuses a tenth picture before the graph runs — so a payload that let a disabled tenth
    through would be refused *after* submission, as an opaque 502 rather than as this sentence.
    `_partition` skips the disabled ones and then truncates each group, and a `standalone`
    video's soundtrack joins the audio group where nothing counted it, so three attached audios
    plus one such video is four — and the fourth would be wired to split output 18, which the
    splitter does not have. Neither count alone catches both; the larger catches each.
    """
    off = [*h3_references("picture", 9), {"kind": "picture", "file": "F:/refs/x", "enabled": False}]
    # Refused on the attached count even though only nine would be wired, because that is the
    # count the node's own validator makes and the number it would quote back.
    with pytest.raises(ValueError, match=r"at most 9 picture references .* has 10"):
        h3_reference_payload(off)
    assert h3_reference_payload(off[:-1])

    over = [
        {"kind": "video", "file": "F:/refs/v.mp4", "has_audio": True, "audio_mode": "standalone"},
        *h3_references("audio", 3),
    ]
    with pytest.raises(ValueError, match=r"at most 3 audio references .* has 4"):
        h3_reference_payload(over)
    # Exactly at the ceiling with the video's track counted still builds, so the check has not
    # simply moved the off-by-one somewhere else.
    assert h3_reference_payload(over[:-1])


def test_h3_reference_payload_still_names_a_kind_it_cannot_wire_when_that_kind_is_off():
    """The mirror's one deliberate divergence, and the direction it fails in.

    The node skips a disabled item before it ever looks at `kind`, so an unwirable one is
    silently ignored there. This builder refuses it anyway: a caller that sent `kind: "image"`
    has a defect, and it is the same defect whether or not the item happens to be switched off
    today. The divergence only ever refuses — it can never wire a slot differently from the
    node — which is the only direction a mirror is allowed to differ in.
    """
    with pytest.raises(ValueError, match="Unsupported H3 reference kind: image"):
        h3_reference_payload(
            [
                {"kind": "picture", "file": "F:/refs/a.png"},
                {"kind": "image", "file": "F:/refs/b.png", "enabled": False},
            ]
        )


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


# --------------------------------------------------------------------------------------------
# Attention backend profiles — spec-h3-attention-backend.
#
# The whole point of this block is the first three tests: the three H3 payload families that
# carry an attention node must be byte-identical on the default profile. Everything after them
# describes what the *other* profiles do, and none of that would matter if the default moved.
# --------------------------------------------------------------------------------------------

#: The keyframe and image-edit payloads for one fixed set of arguments each, hashed at commit
#: `c4c0722` — the commit before attention profiles existed — by running that revision's
#: builders over exactly the arguments below and hashing
#: `json.dumps(payload, separators=(",", ":"))`, key order included, the same way
#: `H3_DEFAULT_PROFILE_DIGEST` is taken.
#:
#: These two families had no payload digest before today. The reference family had two
#: (`H3_DEFAULT_PROFILE_DIGEST` and `H3_SONG_AUDIO_PRE_KEYFRAME_DIGEST`) and the text-only
#: family one (`H3_TEXT_ONLY_PRE_KEYFRAME_DIGEST`, whose builder emits no attention node at
#: all), so with these the digest cover is complete: **every** builder that emits a
#: `PathchSageAttentionKJ` now has its default pinned.
#:
#: If one of these fails, a pre-existing mode's payload changed. Re-deriving the digest is the
#: wrong fix unless the Director has renegotiated the promise.
H3_KEYFRAME_PRE_ATTENTION_DIGEST = (
    "0f3bc5fbfa509b813c8fb2f2cd8041f4ae2a1fc99a1336b3aeffa741a4946231"
)
H3_IMAGE_EDIT_PRE_ATTENTION_DIGEST = (
    "2228c5fb799523afd2e3024112f55925863d4f64288e7bfa2acddbf3e38db9fe"
)


def keyframe_attention_payload(**overrides) -> dict:
    """The exact arguments `H3_KEYFRAME_PRE_ATTENTION_DIGEST` was taken over."""
    arguments = {
        "prompt": "A singer turns to the window",
        "first_frame": "F:/refs/first.png",
        "last_frame": "F:/refs/last.png",
        "duration": 4.0,
        "seed": 99,
        "prefix": "mvp/keyframe-attention",
        "width": 1280,
        "height": 720,
        "steps": 20,
    }
    return build_h3_keyframe_payload(**{**arguments, **overrides})


def image_edit_attention_payload(**overrides) -> dict:
    """The exact arguments `H3_IMAGE_EDIT_PRE_ATTENTION_DIGEST` was taken over."""
    arguments = {
        "prompt": "Give the jacket a red collar",
        "pictures": [{"file": "F:/refs/base.png"}, {"file": "F:/refs/swatch.png"}],
        "seed": 11,
        "width": 1024,
        "height": 1024,
        "steps": 20,
        "prefix": "mvp/image-edit-attention",
    }
    return build_h3_image_edit_payload(**{**arguments, **overrides})


def test_the_default_attention_leaves_every_h3_payload_byte_identical():
    """AC-1, and the reason everything else in this block is allowed to exist.

    Three digests, one per payload family that carries an attention node. The reference
    family's own pre-attention digests are asserted by
    `test_the_default_profile_emits_the_graph_the_adapter_shipped_before_profiles` and
    `test_every_pre_existing_h3_shape_is_byte_identical_across_the_keyframe_change`, which
    were taken at earlier baselines and still pass unchanged — so this test covers the two
    families that had no digest, and re-covers the reference one through the arguments that
    exercise the LoRA branch as well.

    Named and omitted must also agree, or "the default" would mean two things.
    """
    keyframe = keyframe_attention_payload()
    serialized = json.dumps(keyframe, separators=(",", ":")).encode("utf-8")
    assert hashlib.sha256(serialized).hexdigest() == H3_KEYFRAME_PRE_ATTENTION_DIGEST

    edit = image_edit_attention_payload()
    serialized = json.dumps(edit, separators=(",", ":")).encode("utf-8")
    assert hashlib.sha256(serialized).hexdigest() == H3_IMAGE_EDIT_PRE_ATTENTION_DIGEST

    assert keyframe_attention_payload(attention="default") == keyframe
    assert keyframe_attention_payload(attention=H3_DEFAULT_ATTENTION) == keyframe
    assert image_edit_attention_payload(attention="default") == edit
    assert default_profile_payload(attention="default") == default_profile_payload()
    # The LoRA-carrying sampling profiles run through the same patch-chain helper, so they
    # get the same promise: a `turbo` graph is what it was too.
    for profile in sorted(H3_REFERENCE_PROFILES):
        named = default_profile_payload(profile=profile, attention="default")
        assert named == default_profile_payload(profile=profile), profile


def test_the_default_attention_profile_is_the_audited_export_s_own_node():
    """The evidence, read rather than restated — the same standard the sampling profiles hold.

    `h3-ultra-references-user-export.json` carries the node this adapter reproduces, with the
    values it reproduces, in the chain position it reproduces: the sage node draws from the
    sigma shift, not the other way round. If someone ever switches that export's
    `sage_attention` on, this stops agreeing — which is the moment the default profile's
    evidence changed and the constant should be revisited rather than the test.
    """
    export = json.loads(
        (REFERENCE_EXPORTS / "h3-ultra-references-user-export.json").read_text(encoding="utf-8")
    )
    sage_id, sage = next(
        (node_id, node)
        for node_id, node in export.items()
        if node["class_type"] == "PathchSageAttentionKJ"
    )
    shift_id, _ = next(
        (node_id, node)
        for node_id, node in export.items()
        if node["class_type"] == "MiniMaxH3SigmaShift"
    )
    profile = H3_ATTENTION_PROFILES[H3_DEFAULT_ATTENTION]

    assert profile.class_type == sage["class_type"]
    assert dict(profile.inputs) == {
        "sage_attention": sage["inputs"]["sage_attention"],
        "allow_compile": sage["inputs"]["allow_compile"],
    }
    # The position, chain-walked: the export's sage node is fed by its sigma shift.
    assert sage["inputs"]["model"] == [shift_id, 0], sage_id
    assert profile.position == "after-shift"


@pytest.mark.parametrize(
    "name, class_type, values",
    [
        ("default", "PathchSageAttentionKJ", {"sage_attention": "disabled", "allow_compile": False}),
        ("pytorch", "ModelAttentionBackend", {"attention": "pytorch attention"}),
        ("comfy-kitchen", "ModelAttentionBackend", {"attention": "comfy kitchen attention"}),
        ("sage-auto", "PathchSageAttentionKJ", {"sage_attention": "auto", "allow_compile": False}),
        (
            "sage-fp8-cuda++",
            "PathchSageAttentionKJ",
            {"sage_attention": "sageattn_qk_int8_pv_fp8_cuda++", "allow_compile": False},
        ),
    ],
)
def test_each_attention_profile_emits_the_node_and_values_it_claims(name, class_type, values):
    """Every profile, in every builder that takes one: the class and the values it names.

    Parametrized over the registry's whole contents rather than a sample, and asserted
    against the table above rather than against the registry, so a value edited in
    `workflows.py` has to be edited here too — a test that reads the same dict it is checking
    proves only that the dict equals itself.
    """
    reference = default_profile_payload(attention=name)["mvp:attention"]
    keyframe = keyframe_attention_payload(attention=name)["mvp:attention"]
    edit = image_edit_attention_payload(attention=name)["mvp:attention"]

    for node in (reference, keyframe, edit):
        assert node["class_type"] == class_type
        assert {k: v for k, v in node["inputs"].items() if k != "model"} == values
        # Input order is fixed, because a digest pins it and the schema publishes it in
        # this order. `model` always first, as every other node in these builders has it.
        assert list(node["inputs"]) == ["model", *values]


@pytest.mark.parametrize(
    "name, position",
    [
        ("default", "after-shift"),
        ("pytorch", "before-shift"),
        ("comfy-kitchen", "before-shift"),
        ("sage-auto", "after-shift"),
        ("sage-fp8-cuda++", "after-shift"),
    ],
)
def test_each_attention_profile_wires_its_node_where_its_evidence_puts_it(name, position):
    """The chain position, asserted as wiring rather than as a label.

    `PathchSageAttentionKJ` after the sigma shift — chain-walked in
    `h3-ultra-references-user-export.json` by
    `test_the_default_attention_profile_is_the_audited_export_s_own_node`.
    `ModelAttentionBackend` before it — chain-walked on 2026-08-21 in the Director's Comfy
    Kitchen graph, `J:/Hermes-Remote/comfyui/workflowsbackup/ComfyKitchen/MiniMax-H3 TXT2VID
    IMG2VID (Full)- 20260818.json`, where node 6095 feeds sigma shift 6007 which feeds guider
    214. That file is the Director's and lives outside this repo, so the position is written
    out here as a literal rather than read from `H3_ATTENTION_CHAIN_POSITION` — an earlier
    version of this test read the table it was checking, and a mutation sweep walked straight
    past a profile moved to the wrong side of the shift.

    Whichever way round, exactly one attention node exists and everything downstream reads
    the *last* patch — the two classes write the same `optimized_attention_override`, so a
    graph carrying both would silently discard one.
    """
    assert H3_ATTENTION_PROFILES[name].position == position
    for payload in (default_profile_payload(attention=name), keyframe_attention_payload(attention=name)):
        attention = payload["mvp:attention"]["inputs"]["model"]
        shift = payload["mvp:shift"]["inputs"]["model"]
        downstream = payload["mvp:preview"]["inputs"]["model"]

        if position == "after-shift":
            assert attention == ["mvp:shift", 0]
            assert shift != ["mvp:attention", 0]
            assert downstream == ["mvp:attention", 0]
        else:
            assert shift == ["mvp:attention", 0]
            assert attention != ["mvp:shift", 0]
            assert downstream == ["mvp:shift", 0]

        classes = [node["class_type"] for node in payload.values()]
        assert sum(classes.count(each) for each in H3_ATTENTION_NODE_INPUTS) == 1, classes
        # The scheduler and the guider read the preview, which reads the end of the patch
        # chain, so the whole graph samples the patched model rather than a half-patched one.
        assert payload["mvp:scheduler"]["inputs"]["model"] == ["mvp:preview", 0]
        assert payload["mvp:guider"]["inputs"]["model"] == ["mvp:preview", 0]

    # The image-edit graph has no sigma shift to be ordered against, so every profile lands in
    # the one slot and the scheduler and guider read it directly.
    edit = image_edit_attention_payload(attention=name)
    assert "mvp:shift" not in edit
    assert edit["mvp:scheduler"]["inputs"]["model"] == ["mvp:attention", 0]
    assert edit["mvp:guider"]["inputs"]["model"] == ["mvp:attention", 0]


@pytest.mark.parametrize(
    "builder",
    [default_profile_payload, keyframe_attention_payload, image_edit_attention_payload],
)
@pytest.mark.parametrize("unknown", ["", "sage", "sageattn3", "PYTORCH", None, 7, ["default"]])
def test_an_unknown_attention_profile_is_refused_rather_than_defaulted(builder, unknown):
    """Every builder refuses, and refuses with a `ValueError` the routes translate to a 422.

    A silent fall back to the default is the failure this experiment cannot survive: a
    measurement run that mistyped its backend would report the baseline's numbers under the
    candidate's name and nobody would see it. `sageattn3` is in the parameters on purpose —
    it is a real option of the live node and deliberately *not* a profile here, so asking for
    it must fail rather than quietly render on something else. Unhashable and non-string
    values are covered because a `TypeError` escapes a route's `except ValueError` as a 500.
    """
    with pytest.raises(ValueError) as raised:
        builder(attention=unknown)

    assert "attention profile" in str(raised.value)
    assert "comfy-kitchen" in str(raised.value)


def test_resolve_h3_attention_returns_the_registry_s_own_profile():
    """The shared lookup all three builders use, checked once where it lives."""
    for name, profile in H3_ATTENTION_PROFILES.items():
        assert resolve_h3_attention(name) is profile
    with pytest.raises(ValueError):
        resolve_h3_attention("nope")


@pytest.mark.parametrize(
    "kwargs, complaint",
    [
        ({"class_type": "SomeAttention", "inputs": ()}, "known attention node"),
        ({"class_type": "ModelAttentionBackend", "inputs": (("mode", "pytorch attention"),)}, "takes"),
        (
            {"class_type": "PathchSageAttentionKJ", "inputs": (("allow_compile", False), ("sage_attention", "auto"))},
            "takes",
        ),
        ({"class_type": "ModelAttentionBackend", "inputs": (("attention", True),)}, "takes a str"),
        (
            {"class_type": "PathchSageAttentionKJ", "inputs": (("sage_attention", "auto"), ("allow_compile", "False"))},
            "takes a bool",
        ),
        (
            {"class_type": "PathchSageAttentionKJ", "inputs": (("sage_attention", "  "), ("allow_compile", False))},
            "must be named",
        ),
    ],
)
def test_an_attention_profile_that_could_not_validate_fails_at_import(kwargs, complaint):
    """Every way a new profile can be wrong, refused on the line that wrote it.

    A node input ComfyUI does not declare, or a value of the wrong type, reaches `/prompt`
    validation as an opaque 502 after the submission round-trip — at the end of the one path
    where the profile was supposed to be the trustworthy part. The `allow_compile="False"`
    case is the one worth the line: it is truthy everywhere it is read, so without a type
    check it would silently enable `torch.compile` on a timing run.
    """
    with pytest.raises(ValueError) as raised:
        H3AttentionProfile(**kwargs)

    assert complaint in str(raised.value)


def test_the_attention_registry_names_no_two_profiles_the_same_graph():
    """Two names for one node is a measurement that compares a thing with itself.

    Also pins the registry's own shape: every profile's class is one this module knows how to
    place and how to check, which is what `tests/preflight_h3_ultra.py` then audits live.
    """
    emitted = [
        (profile.class_type, profile.inputs) for profile in H3_ATTENTION_PROFILES.values()
    ]
    assert len(set(emitted)) == len(emitted), emitted
    for profile in H3_ATTENTION_PROFILES.values():
        assert profile.class_type in H3_ATTENTION_NODE_INPUTS
        assert profile.class_type in H3_ATTENTION_CHAIN_POSITION
    assert H3_DEFAULT_ATTENTION in H3_ATTENTION_PROFILES


def test_the_attention_harness_refuses_to_render_without_its_gpu_argument():
    """The gate, checked the only way it can be: by trying to get past it.

    `--confirm-gpu` is the whole safety of a script whose ordinary run costs hours of the
    Director's card. The refusal has to land *before* any network call, so this test needs no
    ComfyUI and no ffmpeg — if it ever starts needing one, the gate has moved behind
    something that already touched the machine.
    """
    harness = importlib.import_module("measure_h3_attention")
    argv = ["--project", "project_nonexistent", "--shot", "shot_nonexistent"]

    with pytest.raises(SystemExit) as raised:
        harness.parse_and_gate(argv)

    assert raised.value.code == 2
    # And the same command *with* the flag gets past the gate rather than being refused for
    # some unrelated reason — otherwise the test above would pass on a broken parser.
    assert harness.parse_and_gate([*argv, "--confirm-gpu"]).confirm_gpu is True


def test_the_attention_harness_measures_the_frame_count_the_cliff_sits_at():
    """226 frames, on H3's own grid, reached the way the application reaches a frame count.

    The harness takes seconds and lets `over_render_frames` derive frames, because that is
    the project's rule and a harness that hardcoded 226 into a payload would be measuring a
    graph the application cannot produce.
    """
    harness = importlib.import_module("measure_h3_attention")

    assert harness.MEASURED_FRAMES == 226
    assert (harness.MEASURED_FRAMES - 5) % 17 == 0
    assert over_render_frames(harness.measured_duration()) == harness.MEASURED_FRAMES
    assert over_render_frames(harness.warmup_duration()) == harness.WARMUP_FRAMES
    # The enhancer investigation's own indices are kept so the two are comparable, and every
    # index has to exist in a 226-frame clip or it silently samples nothing.
    assert {20, 44, 70} <= set(harness.SAMPLE_FRAMES)
    assert max(harness.SAMPLE_FRAMES) < harness.MEASURED_FRAMES


def test_the_attention_harness_crosses_sampling_bundles_with_backends():
    """An arm is a pair, because the cost table answered neither question on its own.

    Every number in the recorded render-cost table is a `default`-profile, 20-step
    measurement — the batch route's `BatchRequest.profile` defaults there and the frontend
    sends no profile with a batch, while single-shot "Render Again" hardcodes `turbo`. So the
    bundle is at least as live a variable as the backend, and the harness has to be able to
    hold either one still.
    """
    harness = importlib.import_module("measure_h3_attention")
    base = ["--project", "p", "--shot", "s", "--confirm-gpu"]

    # An arm is a *triple*: frame count, sampling bundle, attention backend. All three are
    # live variables in this experiment and each must be holdable still while the others move.
    backends = harness.parse_and_gate([*base, "--sampling", "default", "--profiles", "default,pytorch"])
    assert backends.arms == [
        (226, "default", "default", None), (226, "default", "pytorch", None)
    ]

    bundles = harness.parse_and_gate(
        [*base, "--sampling", "turbo-references2v,turbo", "--profiles", "default"]
    )
    assert bundles.arms == [
        (226, "turbo-references2v", "default", None), (226, "turbo", "default", None)
    ]
    # Every list at once is the cross product, and the cost line has to say so.
    both = harness.parse_and_gate([*base, "--sampling", "default,turbo", "--profiles", "default,pytorch"])
    assert len(both.arms) == 4

    # The band sweep: one bundle, one backend, four lengths — one experiment in one command,
    # because running it as four invocations is four chances to vary something else.
    band = harness.parse_and_gate(
        [*base, "--sampling", "turbo-references2v", "--profiles", "default",
         "--frames", "158,175,192,209"]
    )
    assert [count for count, _, _, _ in band.arms] == [158, 175, 192, 209]
    # Bound first and checked on the next line, deliberately. This module is scanned by
    # `test_nothing_in_the_enhancement_path_claims_the_frame_count_is_preserved`, a coarse
    # text guard against ever claiming an output frame count equals an input's on the LTX
    # enhance/restore path: it rejects an assertion naming that attribute on one line. This
    # check is about parsed CLI arguments and has nothing to do with that path, but a text
    # guard is worth more than one tidy line. Do not rejoin these two statements — and note
    # that the first version of this very comment tripped the scan by quoting the pattern.
    parsed = band.frame_counts
    assert parsed == [158, 175, 192, 209]
    # Off-grid counts and duplicates are refused before anything renders.
    with pytest.raises(SystemExit):
        harness.parse_and_gate([*base, "--profiles", "default,pytorch", "--frames", "200"])
    with pytest.raises(SystemExit):
        harness.parse_and_gate([*base, "--profiles", "default,pytorch", "--frames", "158,158"])
    with pytest.raises(SystemExit):
        harness.parse_and_gate([*base, "--profiles", "default,pytorch", "--frames", "abc"])

    # An unknown *sampling* bundle is refused the way an unknown backend is — the sampling
    # names come from a different registry and would otherwise reach
    # `build_h3_reference_payload` unchecked, after the gate. Paired with a known bundle on
    # purpose: with only the bad one named this would produce a single arm and abort for
    # *that* reason instead, and the test would pass with the validation deleted.
    with pytest.raises(SystemExit):
        harness.parse_and_gate([*base, "--sampling", "turbo,ultra-turbo", "--profiles", "default"])
    with pytest.raises(SystemExit):
        harness.parse_and_gate([*base, "--sampling", "turbo,turbo", "--profiles", "default"])
    # One arm is not an A/B whichever axis it came from.
    with pytest.raises(SystemExit):
        harness.parse_and_gate([*base, "--sampling", "turbo", "--profiles", "default"])


def test_the_attention_harness_refuses_to_start_a_second_run_at_midnight(monkeypatch, tmp_path):
    """The bug this test exists for actually happened, mid-experiment, at 00:23.

    The evidence directory is date-stamped and a five-arm experiment takes hours, so a
    resumed run crosses midnight. Taking `today()` unconditionally made a second, empty
    directory, found no records in it, and began adopting an in-flight render as the *first*
    arm of a fresh experiment — filing a `pytorch` render under the `default` label. That is
    the mislabelled-arm failure the harness exists to prevent, arriving through the door
    nobody was watching. Two defensible answers means refuse and name them, never pick one.
    """
    harness = importlib.import_module("measure_h3_attention")
    monkeypatch.setattr(harness, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(harness, "today", lambda: "2026-08-22")

    # Nothing anywhere: today's directory, and no complaint. A genuinely new run.
    assert harness.resolve_run_dir(None) == tmp_path / "2026-08-22-h3-attention"

    yesterday = tmp_path / "2026-08-21-h3-attention" / "records"
    yesterday.mkdir(parents=True)
    (yesterday / "default+default-r1.json").write_text("{}", encoding="utf-8")

    # Yesterday holds arms and today does not: refuse, and name the directory to pass.
    with pytest.raises(SystemExit):
        harness.resolve_run_dir(None)
    # Explicitly named, either way, is always honoured — that is how the refusal is answered.
    assert harness.resolve_run_dir("2026-08-21-h3-attention") == tmp_path / "2026-08-21-h3-attention"
    assert harness.resolve_run_dir("2026-08-22-h3-attention") == tmp_path / "2026-08-22-h3-attention"

    # Once today's directory holds arms of its own it is unambiguous again, even though
    # yesterday's still exists — otherwise every later invocation of a new run would refuse.
    todays = tmp_path / "2026-08-22-h3-attention" / "records"
    todays.mkdir(parents=True)
    (todays / "default+pytorch-r1.json").write_text("{}", encoding="utf-8")
    assert harness.resolve_run_dir(None) == tmp_path / "2026-08-22-h3-attention"


def test_the_attention_harness_leaves_preview_frames_alone_by_default():
    """The measurement knob must patch nothing unless asked, or every digest moves.

    `ModelPreviewOverrideKJ` and its `preview_frames: 12` come from the Director's audited
    export (`h3-ultra-references-user-export.json` node 2376), so the builders reproduce them
    and are not changed. `--preview-frames` exists only to measure what that costs, and it
    patches the submitted payload rather than the builder — so with the flag absent, what goes
    to ComfyUI is byte-identical to what the builder emitted.
    """
    harness = importlib.import_module("measure_h3_attention")
    base = ["--project", "p", "--shot", "s", "--confirm-gpu", "--profiles", "default,pytorch"]

    assert harness.parse_and_gate(base).preview_list == [None]
    assert harness.parse_and_gate([*base, "--preview-frames", "1"]).preview_list == [1]
    # As an arm axis, so 12 and 1 interleave in one run rather than four invocations — any
    # residual drift then falls across both conditions instead of on one of them.
    paired = harness.parse_and_gate([*base, "--preview-frames", "12,1", "--repeats", "2"])
    assert [preview for _, _, _, preview in paired.arms] == [12, 1, 12, 1, 12, 1, 12, 1][:4]
    with pytest.raises(SystemExit):
        harness.parse_and_gate([*base, "--preview-frames", "0"])
    with pytest.raises(SystemExit):
        harness.parse_and_gate([*base, "--preview-frames", "12,12"])

    # The builders still emit the export's own value, whatever the flag says — the flag
    # cannot reach them.
    payload = default_profile_payload()
    assert payload["mvp:preview"]["inputs"]["preview_frames"] == 12
    # And the export really is where that number comes from, so nobody "tidies" it later.
    export = json.loads(
        (REFERENCE_EXPORTS / "h3-ultra-references-user-export.json").read_text(encoding="utf-8")
    )
    exported = next(
        node for node in export.values() if node["class_type"] == "ModelPreviewOverrideKJ"
    )
    assert exported["inputs"]["preview_frames"] == 12
    assert exported["inputs"]["max_resolution"] == 1024


def test_the_attention_harness_patches_no_payload_unless_asked():
    """`None` must patch nothing, and the patch must land on the preview node only.

    Both halves survived the suite as inline code — a mutation that patched unconditionally
    and one that patched `PathchSageAttentionKJ` instead were invisible. Either would alter
    every submitted payload while the builders, and therefore every digest, went on agreeing
    with themselves.
    """
    harness = importlib.import_module("measure_h3_attention")

    payload = default_profile_payload()
    before = copy.deepcopy(payload)
    assert harness.apply_preview_override(payload, None) == 0
    assert payload == before

    assert harness.apply_preview_override(payload, 1) == 1
    assert payload["mvp:preview"]["inputs"]["preview_frames"] == 1
    # Only the preview node moved; the attention node is untouched.
    assert payload["mvp:attention"] == before["mvp:attention"]
    assert {k: v for k, v in payload.items() if k != "mvp:preview"} == {
        k: v for k, v in before.items() if k != "mvp:preview"
    }


def test_the_attention_harness_labels_which_clock_a_cost_came_from():
    """Sampling when known, the whole render as a labelled fallback, never silently mixed.

    A 62 s cold load once read as a 24% backend difference because cost was the whole render
    and nothing said so. The basis travels with the number now.
    """
    harness = importlib.import_module("measure_h3_attention")

    sampled = harness.cost_fields(sampling_span=181, execution=255.35, wall=256.39, frames=107)
    assert sampled == {"seconds_per_frame": 1.692, "seconds_per_frame_basis": "sampling"}

    fallback = harness.cost_fields(sampling_span=None, execution=255.35, wall=256.39, frames=107)
    assert fallback["seconds_per_frame_basis"] == "whole-render"
    assert fallback["seconds_per_frame"] == 2.386
    # And with no ComfyUI clock either, the stopwatch — still labelled as the whole render.
    only_wall = harness.cost_fields(sampling_span=None, execution=None, wall=256.39, frames=107)
    assert only_wall["seconds_per_frame_basis"] == "whole-render"
    assert only_wall["seconds_per_frame"] == 2.396


def test_the_attention_harness_reads_the_free_endpoint_s_actual_contract():
    """`POST /free` answers 200 with a ZERO-LENGTH body, and that broke the first version.

    The call was made through the harness's JSON helper, which parses the reply — so a call
    that had genuinely succeeded was recorded as `called: False`. That is the "do not assume
    it worked" trap entered from the other side: assuming it *failed*, and it would have been
    written up as "/free is unavailable on this build". The endpoint's contract has to be read
    (`server.py:1192` returns `web.Response(status=200)`), not its wrapper's.

    It is also asynchronous — `set_flag` notifies the queue worker, which acts on the flags on
    its next tick (`main.py:398-410`) — so nothing is freed when the 200 arrives, and the
    settle exists for that.
    """
    harness = importlib.import_module("measure_h3_attention")

    assert harness.FREE_SETTLE_SECONDS >= 5
    # Unreachable server: reported, never raised, and never silently counted as success.
    outcome = harness.free_memory("http://127.0.0.1:9")
    assert outcome["called"] is False
    assert outcome.get("error")
    assert "before" in outcome
    # And it must not route through the JSON helper, whose parse is what broke it. Matched on
    # the *call* rather than the name, because the function's own comment explains why it does
    # not use that helper — and an earlier version of this assertion tripped on that comment.
    body = inspect.getsource(harness.free_memory)
    assert "post_json(" not in body, body
    assert "urlopen" in body


def test_the_attention_harness_waits_for_free_and_checks_its_status(monkeypatch):
    """A non-200 is not success, and the deltas are read after a settle rather than instantly.

    Both of these survived a mutation sweep as untested live-path code: reporting `called:
    True` regardless of status, and skipping the settle entirely, were invisible. The settle
    matters because `/free` only sets queue flags — the unload happens on the worker's next
    tick — so reading state immediately measures nothing and would report "freed 0" for a call
    that worked.
    """
    harness = importlib.import_module("measure_h3_attention")
    slept: list[float] = []
    monkeypatch.setattr(harness.time, "sleep", slept.append)
    monkeypatch.setattr(harness, "gpu_state", lambda url: {"host_ram_free_gib": 1.0})

    class Reply:
        def __init__(self, status):
            self.status = status
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False

    monkeypatch.setattr(harness.urllib.request, "urlopen", lambda *a, **k: Reply(200))
    good = harness.free_memory("http://example.invalid")
    assert good["called"] is True and good["status"] == 200
    assert slept == [harness.FREE_SETTLE_SECONDS], slept

    slept.clear()
    monkeypatch.setattr(harness.urllib.request, "urlopen", lambda *a, **k: Reply(503))
    bad = harness.free_memory("http://example.invalid")
    assert bad["called"] is False and bad["status"] == 503


def test_the_attention_harness_cuts_only_the_memory_bound_signature():
    """The standing authorisation to cut an arm, and every way it must decline to.

    Measured on 2026-08-22: plain PyTorch attention spent 97 minutes on a 226-frame sequence
    without reaching `loaded completely` or emitting one sampling step, pinned at 100%
    utilisation drawing 159 W on a card that pulls 400-575 W under compute. Re-confirming that
    costs hours of GPU for nothing, so it is cut automatically — but *only* on all four
    signals, because each alone has an innocent reading.
    """
    harness = importlib.import_module("measure_h3_attention")
    stalled = {
        "elapsed": 5820.0, "baseline": 1753.3, "loaded_completely": False,
        "progress_frames": 0, "watts": 159.0,
    }

    assert harness.should_cut(**stalled) is True
    # Each signal alone is a reason not to cut.
    assert harness.should_cut(**{**stalled, "loaded_completely": True}) is False
    assert harness.should_cut(**{**stalled, "progress_frames": 1}) is False
    assert harness.should_cut(**{**stalled, "watts": 480.0}) is False
    # An unreadable power meter is not evidence of thrashing.
    assert harness.should_cut(**{**stalled, "watts": None}) is False
    # Merely slow is allowed to be slow: below the multiple, nothing is cut.
    assert harness.should_cut(**{**stalled, "elapsed": 1753.3 * 1.4}) is False
    assert harness.should_cut(**{**stalled, "elapsed": 1753.3 * 1.6}) is True
    # And the first arm of a run, with nothing completed to be slow against, is never cut —
    # this rule stops re-confirming a known pattern, it does not police a fresh experiment.
    assert harness.should_cut(**{**stalled, "baseline": None}) is False

    # The two constants the watchdog's IO depends on, pinned because nothing else can reach
    # them and a sweep proved it: sentinel mutations to both survived the whole suite.
    #
    # `LOADED_LINE` must be the string ComfyUI actually prints, or the check reads "never
    # loaded" for every arm and the first signal is permanently true — the cut would then rest
    # on three signals, not four. Verified against this machine's own log, where the completed
    # 226-frame arm printed "loaded completely; 8571.52 MB usable, 4966.19 MB loaded".
    assert harness.LOADED_LINE == "loaded completely"
    # The listen has to outlast the longest single sampling step, or "no frames seen" means
    # "did not look long enough". The measured baseline was ~88 s per step at 226 frames.
    assert harness.PROGRESS_LISTEN_SECONDS >= 180
    assert harness.DID_NOT_COMPLETE_POWER_WATTS == 200
    assert harness.DID_NOT_COMPLETE_BASELINE_MULTIPLE == 1.5


def test_the_attention_harness_screens_cheaply_before_the_cliff_point():
    """226 frames is the question; 107 frames is what asks it cheaply first.

    97 minutes went into discovering that a backend does not fit 226 frames. Five minutes at
    107 would have suggested it, so a backend is screened before it is promoted — and the
    sampled frame indices have to follow the frame count, or a screening take would be asked
    for frames it does not have.
    """
    harness = importlib.import_module("measure_h3_attention")

    assert harness.SCREEN_FRAMES == 107
    assert over_render_frames(harness.duration_for_frames(harness.SCREEN_FRAMES)) == 107
    assert over_render_frames(harness.duration_for_frames(226)) == 226
    # **The fixture bug that voided a whole screen.** `over_render_frames` floors every window
    # below ~3.271 s at `H3_MIN_RENDER_FRAMES` = 107, so a search that walks up from one frame
    # answers 0.0417 s for 107 — and a render built from it is conditioned on 42 ms of song.
    # The duration must come from the *un-floored* arithmetic, which is `margin_frames`.
    for count in (107, 141, 158, 175, 192, 209, 226):
        chosen = harness.duration_for_frames(count)
        assert margin_frames(chosen) == count, (count, chosen)
        assert chosen > 3.0, (count, chosen)
    # Specifically: the floored answer is available and must not be the one returned.
    assert over_render_frames(0.0417) == 107
    assert margin_frames(0.0417) != 107
    assert harness.duration_for_frames(107) > 3.29
    # Indices past the end are dropped, never clamped: three arms sharing a clamped index
    # would look aligned while all three showed their last frame, which is agreement about
    # nothing. So the count must *shrink* and the survivors must stay distinct — asserting
    # only "max is in range" passes on a clamped tuple, which a mutation proved.
    screened = harness.sample_indices(harness.SCREEN_FRAMES)
    assert max(screened) < harness.SCREEN_FRAMES
    assert {20, 44, 70} <= set(screened)
    assert len(set(screened)) == len(screened), screened
    assert len(screened) < len(harness.SAMPLE_FRAMES)
    assert set(screened) == {i for i in harness.SAMPLE_FRAMES if i < harness.SCREEN_FRAMES}
    assert set(harness.sample_indices(226)) == set(harness.SAMPLE_FRAMES)
    # A frame count off H3's 17k+5 grid has no duration that produces it, and rendering a
    # neighbouring length silently is exactly what the grid rule exists to prevent.
    with pytest.raises(SystemExit):
        harness.duration_for_frames(200)


def test_the_attention_harness_costs_sampling_not_the_model_load():
    """The error this exists to prevent was made, reported twice, and retracted.

    On 2026-08-22 a 107-frame screen ran with the checkpoint cold for its first arm and
    resident for the rest. That arm paid a 62 s load (CLIP 25.9 GB + UNET 20.0 GB) the others
    did not, and a table built on *total execution* reported it as the attention backend being
    24% slower. ComfyUI's own per-step rates were 9.08, 9.18 and 9.22 s/it — indistinguishable.

    So cost is sampling time, parsed from ComfyUI's tqdm summary, and the load is reported
    beside it instead of inside it. Total execution measures whatever the machine happened to
    be doing; this measures the thing under test.
    """
    harness = importlib.import_module("measure_h3_attention")

    # The real line, from the run that caused this. tqdm prints its summary twice; the last
    # complete one wins.
    window = (
        "[2026-08-22 01:41:46.703] 100%|xxxx| 20/20 [03:01<00:00,  9.08s/it]"
        "100%|xxxx| 20/20 [03:01<00:00,  9.14s/it]"
    )
    assert harness.sampling_seconds(window) == (181, 9.14)

    # A partial bar is not a sampling time. An arm cut mid-render has none, and reporting the
    # elapsed-so-far as its cost is exactly the confusion this function removes.
    assert harness.sampling_seconds("[x] 3/20 [00:30<02:00,  9.08s/it]") == (None, None)
    assert harness.sampling_seconds("no bar here at all") == (None, None)

    # And the arithmetic that made the error visible: at 107 frames the three arms differ by
    # ~2% on sampling while their totals differ by 24%.
    assert round(181 / 107, 3) == 1.692
    assert round(183 / 107, 3) == 1.710
    assert abs((255.35 - 193.06) - 62.29) < 0.01


def test_the_attention_harness_never_compares_takes_of_different_lengths(tmp_path):
    """Frame 44 of a 4.5 s take is not frame 44 of an 8.25 s take.

    A run directory accumulates arms across invocations, so once a 107-frame screen and a
    226-frame arm share one it becomes possible to hstack two *different instants* of the
    performance and present them as the same moment. That is worse than producing no sheet:
    it manufactures an apparent lip-sync difference out of arithmetic, in the one artefact a
    person is asked to judge by eye. Sheets and audio windows are therefore grouped by frame
    count, and the grouping is what this pins.
    """
    harness = importlib.import_module("measure_h3_attention")

    # The windows really are different, which is the whole reason grouping matters.
    assert harness.duration_for_frames(107) != harness.duration_for_frames(226)
    # Indices only exist where the take is long enough, so a mixed comparison could not even
    # be made honestly for the back half.
    assert 113 in harness.sample_indices(226)
    assert 113 not in harness.sample_indices(107)
    # Sheet names carry the length, so two invocations at different counts cannot overwrite
    # each other's evidence — the earlier scheme keyed only on the frame index.
    short, long_ = harness.sample_indices(107), harness.sample_indices(226)
    assert f"mouth_compare-f107-i{short[0]:04d}.png" != f"mouth_compare-f226-i{long_[0]:04d}.png"
    # And an arm's own frame count is what it is grouped by, not the invocation's.
    records = [{"frames": 107}, {"frames": 226}, {"frames": 107}]
    grouped = sorted({record["frames"] for record in records})
    assert grouped == [107, 226]


def test_the_attention_harness_records_each_arm_s_own_sampling_bundle():
    """An arm that reported the wrong step count would read as an attention result.

    The bundles differ five-fold in steps, so a record that took `default`'s 20 while
    rendering `turbo`'s 4 would attribute the whole difference to the backend. Asserted
    against the registry's values here — this is the one place the two are allowed to be
    compared, because the point is that the harness reads the *arm's* bundle rather than a
    fixed one.
    """
    harness = importlib.import_module("measure_h3_attention")

    for name, profile in H3_REFERENCE_PROFILES.items():
        facts = harness.bundle_facts(name)
        assert facts["steps"] == profile.steps
        assert facts["lora"] == profile.lora
        assert facts["scheduler"] == profile.scheduler
        assert facts["sampler"] == profile.sampler
    # And the bundles really are distinguishable by what is recorded, or the check above
    # would pass on a harness that always answered `default`.
    assert len({harness.bundle_facts(name)["steps"] for name in H3_REFERENCE_PROFILES}) > 1
    assert harness.bundle_facts("turbo")["steps"] != harness.bundle_facts("default")["steps"]


def test_the_attention_harness_measures_audio_against_the_window_it_was_given():
    """The third verdict, and the reason it is a comparison rather than a score.

    H3 *generates* its audio conditioned on the reference, so a correlation well below 1 is
    the model working as designed — the Director's "a bit of a mutation of what I assume was
    the input audio" describes that, not a defect. What is worth knowing is whether one arm
    departs further, or lands later, than another at the same seed. The lag is the half that
    does not need a person, so it is the half worth pinning: a signal delayed by a known
    number of samples must report that delay and not zero.
    """
    harness = importlib.import_module("measure_h3_attention")
    numpy = pytest.importorskip("numpy")

    # Pinned as a literal, not read from the module. An earlier version built its fixture at
    # `harness.AUDIO_COMPARE_RATE` and therefore agreed with any value the module chose — a
    # sentinel mutation moving it to 8 kHz passed, which means the whole test was measuring
    # the module against itself.
    assert harness.AUDIO_COMPARE_RATE == 16000
    assert harness.AUDIO_LAG_LIMIT_SECONDS == 1.0
    rate = 16000
    # Broadband *with syllable structure*, because that is what the measure is built for and
    # neither simpler fixture worked. A pure 220 Hz tone is periodic, so its correlation peaks
    # at every multiple of its period and a 100 ms delay read as 50 ms — eleven periods out.
    # Flat white noise then broke the other half: its amplitude envelope is nearly constant,
    # so envelope correlation had nothing to lock onto and a 100 ms delay read as 43.8 ms.
    # Speech is neither — it is broadband carrier under a strong ~4 Hz syllable envelope — so
    # the fixture is noise modulated at 4 Hz, and both halves of the measure can be checked.
    noise = numpy.random.default_rng(20260822).normal(0, 6000, rate)
    syllables = 0.5 + 0.5 * numpy.sin(2 * numpy.pi * 4 * numpy.arange(rate) / rate) ** 2
    tone = noise * syllables

    def write(path, samples):
        import wave
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(samples.astype("<i2").tobytes())
        return path

    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        reference = write(root / "reference.wav", tone)
        same = write(root / "same.wav", tone)
        # 100 ms late, which is roughly two frames at 24 fps and far more than a mouth can
        # hide: a comparison that reported 0 here would be blind to exactly what it is for.
        delay = rate // 10
        late = write(root / "late.wav", numpy.concatenate((numpy.zeros(delay), tone[:-delay])))

        aligned = harness.audio_comparison(same, reference)
        assert aligned["correlation"] > 0.99
        assert abs(aligned["lag_ms"]) < 1

        shifted = harness.audio_comparison(late, reference)
        assert abs(abs(shifted["lag_ms"]) - 100) < 2, shifted
        # Both halves must find it: the envelope is what survives H3 regenerating the audio,
        # and the waveform pair is kept only so the first run's numbers stay explainable.
        assert abs(abs(shifted["waveform_lag_ms"]) - 100) < 2, shifted
        assert shifted["basis"].startswith("amplitude envelope")
        # A peak on the edge of the search window is flagged rather than reported as a delay —
        # the first live run produced exactly that (-873 ms against a +/-1000 ms window) and
        # reading it as "the audio is 873 ms late" would have invented an offset.
        edge = harness.audio_comparison(
            write(root / "unrelated.wav",
                  numpy.random.default_rng(7).normal(0, 6000, rate)),
            reference,
        )
        if abs(edge["lag_ms"]) >= 900:
            assert "search window" in edge.get("note", "")

        # A DC offset is not a performance difference. Some encoders leave one, and without
        # centring it dominates the correlation and buries the shape being compared.
        offset = write(root / "offset.wav", numpy.clip(tone + 4000, -32768, 32767))
        biased = harness.audio_comparison(offset, reference)
        assert biased["correlation"] > 0.95, biased

        # Silence on one side is reported, never scored — a decode that produced nothing must
        # not read as "perfectly different", which is the most alarming thing this table can
        # say and would send the Director hunting a defect in the wrong place.
        silent = write(root / "silent.wav", numpy.zeros(rate))
        dead = harness.audio_comparison(silent, reference)
        assert dead["correlation"] is None and dead["lag_ms"] is None
        assert "silence" in dead["note"]


def test_the_attention_harness_keeps_the_muxed_take_not_the_silent_companion(tmp_path):
    """`VHS_VideoCombine` can write two files; only one of them has a voice in it.

    The whole secondary measure is whether the mouth still matches the song, so preserving
    the silent companion would leave every contact sheet technically produced and evidentially
    worthless. Chosen between rather than refused, because by the time this runs the GPU
    minutes are already spent.
    """
    harness = importlib.import_module("measure_h3_attention")
    for name in ("attention-default_00001.mp4", "attention-default_00001-audio.mp4"):
        (tmp_path / name).write_bytes(b"not really a video")
    entry = {
        "outputs": {
            "mvp:save": {
                "gifs": [
                    {"filename": "attention-default_00001.mp4", "subfolder": ""},
                    {"filename": "attention-default_00001-audio.mp4", "subfolder": ""},
                ]
            }
        }
    }

    assert harness.output_video(tmp_path, entry).name == "attention-default_00001-audio.mp4"
    # A render that produced nothing on disk is a failure, not a choice.
    with pytest.raises(SystemExit):
        harness.output_video(tmp_path / "empty", entry)


def test_the_attention_harness_calls_a_substituted_backend_inconclusive():
    """The false-null guard, and the reason this harness reads a log at all.

    `ModelAttentionBackend.VALIDATE_INPUTS` returns `True` for any string, so a payload
    naming an unavailable backend validates, renders, and produces a perfectly good video —
    on PyTorch attention, with one warning line as the only trace. "Comfy kitchen came out
    identical to baseline" is exactly what a successful-but-inert run looks like, so the
    harness has to be able to tell them apart before either is written down.
    """
    harness = importlib.import_module("measure_h3_attention")
    fell_back = (
        "[2026-08-21 23:00:00] Attention backend 'comfy kitchen attention' is unavailable; "
        "using PyTorch attention."
    )

    verdict, evidence = harness.engagement("comfy-kitchen", fell_back)
    assert verdict == "fell-back"
    assert evidence

    # A clean window on the same arm is `registered`, not `confirmed`: the node emits no
    # positive line, so the evidence is the option list plus the absence of the fallback.
    assert harness.engagement("comfy-kitchen", "[..] Prompt executed")[0] == "registered"
    # A sage arm has a positive line and is held to it — including the *mode*, because a
    # kernel that quietly resolved to a different one is the same trap wearing other clothes.
    asked = "sageattn_qk_int8_pv_fp8_cuda++"
    assert harness.engagement("sage-fp8-cuda++", f"Using sage attention mode: {asked}")[0] == "confirmed"
    assert harness.engagement("sage-fp8-cuda++", "Using sage attention mode: auto")[0] == "wrong-mode"
    assert harness.engagement("sage-fp8-cuda++", "[..] Prompt executed")[0] == "unconfirmed"
    # And the default profile patches nothing, so there is nothing to confirm — it inherits
    # ComfyUI's launch flag, which the report records rather than infers.
    assert harness.engagement("default", "[..] Prompt executed")[0] == "inherited"


def test_the_attention_harness_refuses_arms_that_differ_by_anything_else():
    """One controlled variable, checked before the GPU rather than discovered after it.

    The real profiles must pass — otherwise the harness could never run — and a payload
    tampered with anywhere outside the attention node must not.
    """
    harness = importlib.import_module("measure_h3_attention")
    payloads = {
        name: default_profile_payload(attention=name) for name in H3_ATTENTION_PROFILES
    }

    harness.only_the_attention_node_differs(payloads)

    tampered = {name: copy.deepcopy(payload) for name, payload in payloads.items()}
    tampered["comfy-kitchen"]["mvp:noise"]["inputs"]["noise_seed"] = 1234
    with pytest.raises(SystemExit):
        harness.only_the_attention_node_differs(tampered)


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


def test_the_resolver_the_builder_uses_is_the_resolver_the_record_uses():
    """One resolution, so a take's recorded provenance cannot name a bundle other than the one
    its graph was built from.

    The route records what a submission sampled on by calling `resolved_h3_sampling`, and the
    builder builds the graph by calling the same function with the same two arguments. This
    executes it directly and reads the result off the payload, so a second implementation
    appearing on either side — the shape that made `resolved_sampling_profile` necessary one
    level up — fails here rather than in a queue row six weeks later.

    The step substitution is the case a name-only record gets wrong and is asserted for every
    bundle: "the profile chooses the graph, the Director chooses the effort".
    """
    from music_video_producer.workflows import resolved_h3_sampling

    for name, profile in H3_REFERENCE_PROFILES.items():
        assert resolved_h3_sampling(name) is profile, name
        overridden = resolved_h3_sampling(name, 12)
        assert overridden.steps == 12
        # And nothing else moved: an override of one number, never a fourth combination.
        assert (overridden.lora, overridden.lora_strength) == (profile.lora, profile.lora_strength)
        assert (overridden.sampler, overridden.scheduler) == (profile.sampler, profile.scheduler)
        # `None` means "the bundle's own count", which is what the route sends for an omission.
        assert resolved_h3_sampling(name, None) is profile, name

        payload = h3_reference_payload(h3_references("picture", 1), profile=name, steps=12)
        assert payload["mvp:scheduler"]["inputs"]["steps"] == overridden.steps
        assert payload["mvp:scheduler"]["inputs"]["scheduler"] == overridden.scheduler
        assert payload["mvp:sampler"]["inputs"]["sampler_name"] == overridden.sampler

    # The builder's refusals are this function's refusals, in the same words, because they are
    # now literally the same lines.
    for unknown in ("turbo ", "TURBO", "fast", "", None, 4, ["turbo"], {"turbo"}):
        with pytest.raises(ValueError, match="Unknown H3 sampling profile"):
            resolved_h3_sampling(unknown)
    # A step count a profile could not sample is refused on the line that supplied it. The route
    # bounds the field at 1..100 so this is unreachable from a request; a script can reach it,
    # and a zero-step graph samples nothing while looking like a render that was asked for.
    for impossible in (0, -1, True, 2.5):
        with pytest.raises(ValueError, match="at least one step"):
            resolved_h3_sampling("turbo", impossible)


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

    The export's expression is the **grid snap** and has never carried a shot-length policy,
    so since the Director's 2026-08-20 minimum-length floor the two agree at the grid and the
    builder takes the larger of that and `H3_MIN_RENDER_FRAMES`. That is asserted as such —
    the snap still has to be the export's, digit for digit, and the floor is named as the one
    thing on top of it — rather than by dropping the short durations, which would stop
    checking the two arithmetics exactly where they are easiest to get wrong.
    """
    export = keyframe_export()
    expression_node = next(
        node for node in export.values() if node["class_type"] == "ComfyMathExpression"
    )
    expression = expression_node["inputs"]["expression"]

    floored = 0
    for duration in (5 / 24, 0.5, 3.75, 5, 5.1, 8, 12.34, 149.0):
        # The audited export's own arithmetic, evaluated with no builtins and no inputs
        # but `a` — restating the formula locally is exactly what this test must not do.
        # Evaluated at `duration + OVER_RENDER_SECONDS`: the builder deliberately feeds
        # the grid the over-rendered length (the Director's margin ruling), and the
        # export's snap arithmetic must agree with it *at that input*.
        snapped = eval(
            expression,
            {"__builtins__": {}},
            {"a": duration + OVER_RENDER_SECONDS, "max": max, "round": round},
        )
        expected = max(H3_MIN_RENDER_FRAMES, snapped)
        floored += expected != snapped
        payload = keyframe_payload(duration=duration)
        assert payload["mvp:condition"]["inputs"]["length"] == expected, duration
        assert over_render_frames(duration) == expected, duration
        # The grid itself, unfloored, is still the export's own number.
        assert align_h3_frames(max(5, round((duration + OVER_RENDER_SECONDS) * 24))) == snapped
    # Both sides of the floor are covered by the durations above: 5/24 s and 0.5 s reach it,
    # 3.75 s and up do not. A change that made every case one or the other would leave half
    # this test asserting nothing.
    assert floored == 2


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
    short = 0
    for eighths in range(1, 1200):
        duration = eighths / 8  # 0.125 s to 149.875 s, every eighth of a second
        requested = max(5, round((duration + OVER_RENDER_SECONDS) * H3_FRAME_RATE))
        snapped = align_h3_frames(requested)
        # The Director's 2026-08-20 minimum-length floor sits on top of the grid: a window
        # too short for H3's trained minimum is rendered at that minimum anyway and exposes
        # a slice of the take. The grid arithmetic below is unchanged and still checked.
        expected = max(H3_MIN_RENDER_FRAMES, snapped)
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
        if snapped < H3_MIN_RENDER_FRAMES:
            # The floored branch: the take is H3's minimum and is strictly longer than the
            # margin alone asked for. That surplus is the invisible buffer.
            assert length == H3_MIN_RENDER_FRAMES > requested, duration
            short += 1
        else:
            assert requested <= length < requested + 17, duration
        assert length / H3_FRAME_RATE > duration, duration
        assert length >= (duration + OVER_RENDER_SECONDS) * H3_FRAME_RATE - 0.5, duration
        checked += 1
    assert checked > 1100, checked
    # Every eighth-second window up to 3.25 s is floored and 3.375 s upward is not, so the
    # branch above is exercised on both sides rather than only on the one this loop happens
    # to start in.
    assert short == 26, short


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
        preflight_h3_ultra.check_attention_profiles,
    }
    assert len(preflight_h3_ultra.CHECKS) == 10
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

    # The attention registry, moved three ways — the three ways a profile stops describing
    # the live node. Each has to be named rather than absorbed, because a profile the audit
    # silently passes is the unproven combination the whole registry exists to prevent.
    dropped_kernel = copy.deepcopy(schema)
    sage = dropped_kernel["PathchSageAttentionKJ"]["input"]["required"]["sage_attention"]
    sage[0] = [option for option in sage[0] if "fp8_cuda" not in str(option)]
    assert any(
        "sageattn_qk_int8_pv_fp8_cuda++" in problem and "not one of" in problem
        for problem in preflight_h3_ultra.check_attention_profiles(dropped_kernel)
    )
    dropped_backend = copy.deepcopy(schema)
    backend = dropped_backend["ModelAttentionBackend"]["input"]["required"]["attention"]
    backend[0] = [option for option in backend[0] if "kitchen" not in str(option)]
    assert any(
        "comfy kitchen attention" in problem
        for problem in preflight_h3_ultra.check_attention_profiles(dropped_backend)
    )
    renamed_input = copy.deepcopy(schema)
    inputs = renamed_input["ModelAttentionBackend"]["input"]["required"]
    inputs["backend"] = inputs.pop("attention")
    problems = preflight_h3_ultra.check_attention_profiles(renamed_input)
    assert any("H3_ATTENTION_NODE_INPUTS" in problem for problem in problems)
    assert any("the live node does not declare" in problem for problem in problems)
    # And a node that vanished from the server entirely — a KJNodes rename would take the
    # *default* profile with it, so the audit has to say so rather than find nothing to check.
    gone = copy.deepcopy(schema)
    gone.pop("PathchSageAttentionKJ")
    assert any(
        "publishes no input map at all" in problem
        for problem in preflight_h3_ultra.check_attention_profiles(gone)
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
        # The image-edit variants load the dedicated image VAE — AI Mod's distinctive
        # model file, in the set because a payload loads it (2026-08-19).
        "minimax_h3_t1_image_vae_step1597.safetensors",
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
    declared = {
        declared_bundle(profile)
        for table in (H3_REFERENCE_PROFILES, H3_IMAGE_EDIT_PROFILES)
        for profile in table.values()
    }

    for label, payload in audited_reference_variants():
        assert audited_bundle(payload) in declared, (label, audited_bundle(payload))

    loaded = {
        node["inputs"]["lora_name"]
        for _, payload in preflight_h3_ultra.audit_payloads()
        for node in payload.values()
        if node["class_type"] == "LoraLoaderModelOnly"
    }
    assert loaded == {
        profile.lora
        for table in (H3_REFERENCE_PROFILES, H3_IMAGE_EDIT_PROFILES)
        for profile in table.values()
        if profile.lora is not None
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
        # Widened 2026-08-26: this named `app.py` alone, and `enhance_with_ltx25` /
        # `restore_song_audio` moved to `routes/shots.py` in `4a1a4f6` — so the guard
        # was reading a file its subject had left. The rule is about the package.
        *sorted((REPO_ROOT / "src/music_video_producer").rglob("*.py")),
        REPO_ROOT / "tests/preflight_ltx25_enhance.py",
        # The extension path inherits the same "Never": its output is *longer* than its input
        # and by how much is an ffprobe reading, so no file on either path may assert a frame
        # count relationship. `workflows.py` and this file are already on the list and carry
        # both adapters.
        REPO_ROOT / "tests/preflight_ltx25_extend.py",
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
        # The lead a submission of this shot records: a normal-length window at 12 s takes the
        # quarter second. Every case below is a real take's four numbers, never three of them
        # and a default.
        "take_lead": 0.25,
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


def test_the_restore_window_comes_from_over_render_window_and_not_a_second_computation(
    monkeypatch,
):
    """The single correctness argument of this whole stage, driven as a mutation.

    `timeline.over_render_window` — the function that answers "which seconds of the song is this
    take", and the one `generate_h3` conditions with — is replaced with one that returns a window
    nothing else could produce. If the builder computed the window itself, even with arithmetic
    that agrees today, the payload would carry the real 11.75 s and this would fail. It carries
    the replacement's numbers instead, which is only possible if the shared function is the
    single source of the window.

    That is the failure this guards, and it is the one that actually happened: until 2026-08-21
    this stage windowed by `shot.start`/`shot.duration` while the render windowed by the take's
    own lead and length, and the symptom of two computations drifting is a subtle desync rather
    than an error.
    """
    calls: list[dict] = []

    def only_this_function_could_have_produced_it(
        *, start, lead, picture_seconds, song_duration
    ):
        calls.append(
            {
                "start": start,
                "lead": lead,
                "picture_seconds": picture_seconds,
                "song_duration": song_duration,
            }
        )
        return 101.5, 108.25

    monkeypatch.setattr(
        workflows_module, "over_render_window", only_this_function_could_have_produced_it
    )

    payload = audio_replace_payload(
        start=12.0, duration=3.75, song_duration=154.644898, take_lead=0.25
    )

    # Called with the take's own four numbers: the shot's start and the *recorded* lead,
    # unmodified on the way, and the render's own frame count as seconds.
    assert calls == [
        {
            "start": 12.0,
            "lead": 0.25,
            "picture_seconds": over_render_frames(3.75) / 24,
            "song_duration": 154.644898,
        }
    ]
    # And the payload is that function's answer, not the builder's own arithmetic.
    window = payload["mvp:song"]["inputs"]
    assert window["seek_seconds"] == 101.5
    assert window["duration"] == pytest.approx(6.75)


def test_the_restore_stage_still_inherits_the_renders_own_legality_check(monkeypatch):
    """`song_audio_window` is still called, and still with the *shot's* three numbers.

    It answers a different question from the window above — is this shot legal at all — and its
    refusal is the one the render gives. Mutated to prove the call is real: a builder that
    dropped it would stop refusing a shot that runs past the end of the song, which is the
    refusal `test_a_shot_running_past_the_end_of_the_song_is_refused_in_the_renders_own_words`
    depends on.
    """
    calls: list[dict] = []

    def recording(*, start, duration, song_duration):
        calls.append({"start": start, "duration": duration, "song_duration": song_duration})
        return {"start": start, "end": start + duration}

    monkeypatch.setattr(workflows_module, "song_audio_window", recording)

    audio_replace_payload(start=12.0, duration=3.75, song_duration=154.644898)

    assert calls == [{"start": 12.0, "duration": 3.75, "song_duration": 154.644898}]


def test_the_restore_builder_offers_no_window_parameter_for_a_caller_to_disagree_through():
    """The structural half of the argument above: there is nothing to pass a window as.

    The mutation test proves the builder *uses* the shared functions. This proves a caller
    cannot route around them — the signature takes the four numbers the render was given or
    recorded and accepts no window, no start second, no end second and no trim. A parameter
    added here would be the seam a second computation arrives through, so the signature is
    pinned. `take_lead` is not that seam: it is a *recorded* number, not a derived one, and it
    is the one fact about a take that cannot be recomputed from the manifest.
    """
    parameters = inspect.signature(build_audio_replace_payload).parameters

    assert set(parameters) == {
        "source_video",
        "source_audio",
        "start",
        "duration",
        "song_duration",
        "take_lead",
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


def test_a_lead_that_cannot_describe_this_shot_is_refused_rather_than_repaired():
    """`take_lead` is a *recorded* number, so a value this shot could not have produced means
    the record does not belong to this shot — and windowing on it would put the sound ahead of
    or behind the mouth by exactly that much.

    `over_render_lead` answers `min(ideal, extra, start)`, so a lead is never negative and never
    longer than the shot's own start; a take at 3 s cannot begin 5 s before its window, because
    that is a second and a half before the song. Refused rather than clamped to something
    plausible: a clamp here would produce a graph that renders happily and is out of sync, which
    is the failure this whole stage exists to remove.
    """
    with pytest.raises(ValueError, match="cannot begin"):
        audio_replace_payload(start=3.0, duration=3.75, take_lead=5.0)
    with pytest.raises(ValueError, match="cannot begin"):
        audio_replace_payload(take_lead=-0.25)
    # Non-finite, on the same footing as every other number this module takes.
    with pytest.raises(ValueError, match="Take lead"):
        audio_replace_payload(take_lead=float("nan"))
    # A lead of exactly the shot's start is legal: the take begins at the song's first sample,
    # which is what a shot at 0 s and a shot whose whole buffer is lead both produce.
    assert audio_replace_payload(start=0.25, duration=3.75, take_lead=0.25)
    assert audio_replace_payload(start=0.0, duration=3.75, take_lead=0.0)


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
    # 107 frames is what a 3.75 s H3 shot is rendered as since the over-render margin, and 8n+1
    # does not contain it — the arithmetic the paragraph above rests on, computed rather than
    # asserted from memory, and computed through the function the render itself calls.
    frames = over_render_frames(3.75)
    assert frames == 107
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


def test_restore_lengths_report_the_take_the_render_asked_for(monkeypatch):
    """The two numbers, what they mean since the over-render margin, and the case left where
    they differ.

    Until 2026-08-21 `requested_picture_seconds` was `align_h3_frames(round(duration * 24))` —
    the formula from before the margin existed — so a 3.75 s shot was reported as 90 frames when
    the render had asked H3 for 107, and a 2.083 s micro-cut as 50 against the same 107. Nothing
    rendered from that number; it is the sentence the Director reads, and it disagreed with what
    the take is.

    Both sides now come from the take: the picture from `over_render_frames`, the audio from the
    window `build_audio_replace_payload` actually sends. They agree by construction, and the one
    way left to differ is the song ending before the picture does.
    """
    exact = audio_replace_lengths(
        start=12.0, duration=3.75, song_duration=154.644898, take_lead=0.25
    )

    assert exact["requested_frames"] == 107
    assert exact["requested_picture_seconds"] == pytest.approx(107 / 24)
    assert exact["audio_seconds"] == pytest.approx(107 / 24)

    micro = audio_replace_lengths(
        start=12.0, duration=2.083, song_duration=154.644898, take_lead=1.2083333333333333
    )

    # The floor: a micro-cut's take is the same 107 frames, not 50.
    assert micro["requested_frames"] == 107
    assert micro["audio_seconds"] == pytest.approx(107 / 24)

    # The song's end. For a legal shot with its own recorded lead the clamp cannot fire —
    # `over_render_lead`'s overflow branch always has the room, because a shot ending inside the
    # song leaves at least `overflow` of buffer to spend — so this is the state where the lead no
    # longer matches the window: a take whose shot was moved afterwards. The tail is clamped to
    # the song, the audio comes up short of the picture, and the pair says so rather than
    # refusing or padding. `trim_to_audio` is off, so the take keeps its unbacked frames.
    end = audio_replace_lengths(
        start=150.0, duration=4.0, song_duration=154.0, take_lead=0.25
    )

    assert end["audio_seconds"] == pytest.approx(4.25)
    assert end["requested_picture_seconds"] > end["audio_seconds"]

    # Read off `over_render_frames` rather than a copy of the grid, so a shot that rendered
    # through that function is measured against that function — including the micro-cut band,
    # where a copy of the *old* formula is exactly what was wrong.
    for duration in (0.5, 1.75, 2.083, 3.75, 5.0, 8.0, 12.5, 15.0):
        assert audio_replace_lengths(
            start=20.0, duration=duration, song_duration=154.644898, take_lead=0.25
        )["requested_frames"] == over_render_frames(duration)

    # Driven as a mutation, because "agrees with the function" and "calls the function" are
    # different claims and the pre-margin bug passed the first.
    monkeypatch.setattr(workflows_module, "over_render_frames", lambda duration: 217)
    mutated = audio_replace_lengths(
        start=12.0, duration=3.75, song_duration=154.644898, take_lead=0.25
    )
    assert mutated["requested_frames"] == 217


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
        # Widened 2026-08-26: this named `app.py` alone, and `enhance_with_ltx25` /
        # `restore_song_audio` moved to `routes/shots.py` in `4a1a4f6` — so the guard
        # was reading a file its subject had left. The rule is about the package.
        *sorted((REPO_ROOT / "src/music_video_producer").rglob("*.py")),
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
    reported = audio_replace_lengths(
        start=12.0, duration=3.75, song_duration=154.644898, take_lead=0.25
    )
    assert "requested_picture_seconds" in reported
    assert "picture_seconds" not in reported


# --- LTX 2.5 video extension ---------------------------------------------------------------
#
# The audited evidence for the extension adapter, its single output node, and the digests that
# say neither file has moved under the tests that read them. Both, because the *sibling* is
# load-bearing here in a way no other adapter's is: the no-audio export is the counter-example
# the reachability argument is made against, so it has to be as immutable as the one built.
LTX25_EXTENDER_EXPORT = REFERENCE_EXPORTS / "ltx25-videoextender-user-export.json"
LTX25_EXTENDER_EXPORT_SHA256 = (
    "d73e8713c40d46473dfb29928dd471f0f30cc33d078ad38d5375d54020a1b218"
)
LTX25_EXTENDER_NOAUDIO_EXPORT = REFERENCE_EXPORTS / "ltx25-videoextender-noaudio-user-export.json"
LTX25_EXTENDER_NOAUDIO_EXPORT_SHA256 = (
    "22f3463edce14acacb35ada20b752f655e73a9e4877790967860b019190d5c17"
)
LTX25_EXTENDER_OUTPUT_NODE = "1994"

# The node classes the adapter does not build, and what stands in for each. `None` means the
# class is dropped outright rather than replaced. Declared as data so the census comparison
# below can hold every other class to equality: a substitution that grew an entry would
# otherwise disappear into a looser assertion. Each is justified in
# `build_ltx25_extend_payload`.
LTX25_EXTEND_SUBSTITUTIONS = {
    "VHS_LoadVideo": "VHS_LoadVideoPath",
    "SimpleCalculatorKJ": "CM_IntBinaryOperation",
    "ResizeImageMaskNode": "ImageScaleBy",
    # Dropped rather than replaced: the rgthree loader carries no enabled row and applies
    # nothing, `ImpactImageInfo` measures a batch `GetImageSizeAndCount` already measures, and
    # `CM_IntToFloat` only widened integers the adapter now folds.
    "ImpactImageInfo": None,
    "Power Lora Loader (rgthree)": None,
    "CM_IntToFloat": None,
}

# Classes the payload builds that the export never names. `CM_FloatBinaryOperation` is the
# float half of the calculator substitution and `PrimitiveFloat` carries the one value whose
# consuming input publishes no range; both are justified in `build_ltx25_extend_payload`.
LTX25_EXTEND_ADDITIONS = {"CM_FloatBinaryOperation", "PrimitiveFloat"}

# The four expressions the export computes in `SimpleCalculatorKJ` nodes that the adapter folds
# into Python. Mapped rather than evaluated, so an expression the export changes to something
# this test never modelled fails by name instead of being silently re-evaluated.
LTX25_EXTEND_EXPRESSIONS = {
    "(a*b)+1": lambda a, b: a * b + 1,
    "a/b": lambda a, b: a / b,
    "a+b": lambda a, b: a + b,
    "a-b": lambda a, b: a - b,
}


def ltx25_extender_export() -> dict:
    return json.loads(LTX25_EXTENDER_EXPORT.read_text(encoding="utf-8"))


def ltx25_extend_payload(**overrides) -> dict:
    arguments = {
        "source_video": "J:/comfy/output/music-video-producer/p/shots/s-h3-reference_00001.mp4",
        "prefix": "music-video-producer/p/shots/s-ltx25-extend",
        "extend_seconds": 10,
    }
    return build_ltx25_extend_payload(**{**arguments, **overrides})


def test_the_videoextender_exports_are_not_mutated():
    """Both files are immutable audited evidence; every claim below reads them."""
    for path, digest in (
        (LTX25_EXTENDER_EXPORT, LTX25_EXTENDER_EXPORT_SHA256),
        (LTX25_EXTENDER_NOAUDIO_EXPORT, LTX25_EXTENDER_NOAUDIO_EXPORT_SHA256),
    ):
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, path


def test_the_audio_extender_export_is_wholly_reachable_and_its_sibling_is_not():
    """The reason this export is the one reproduced, asserted rather than asserted *about*.

    61 nodes, 61 reachable: nothing about which nodes to build is a judgement call. The no-audio
    sibling carries 65 and reaches 54, and its eleven orphans are its abandoned audio tail —
    an edit in progress. An adapter derived from that file would have to decide what its author
    meant, which is exactly the decision this project refuses to make on the Director's behalf.
    """
    export = ltx25_extender_export()
    sibling = json.loads(LTX25_EXTENDER_NOAUDIO_EXPORT.read_text(encoding="utf-8"))

    reachable = reachable_node_ids(export, [LTX25_EXTENDER_OUTPUT_NODE])
    sibling_reachable = reachable_node_ids(sibling, [LTX25_EXTENDER_OUTPUT_NODE])

    assert len(export) == 61
    assert reachable == set(export)
    assert len(sibling) == 65
    assert len(sibling_reachable) == 54
    orphans = set(sibling) - sibling_reachable
    assert len(orphans) == 11
    # Every orphan is on the audio side, which is what makes them a half-finished edit rather
    # than an unrelated leftover.
    assert {sibling[node_id]["class_type"] for node_id in orphans} <= {
        "AudioConcat",
        "CM_IntToFloat",
        "LTXVAudioVAEDecode",
        "LTXVEmptyLatentAudio",
        "SimpleCalculatorKJ",
        "TrimAudioDuration",
        "VHS_VideoInfo",
    }


def test_the_latent_upscaler_is_reached_here_and_orphaned_in_the_other_two_ltx_exports():
    """The finding that makes reachability a walk rather than a remembered fact.

    `LatentUpscaleModelLoader` sits in the enhancer export, the audio-replacer export and this
    one. In the first two it is an orphan, and both adapters are right to leave its model file
    out of their dependency lists. In this one it feeds `LTXVLatentUpsampler`, and leaving it
    out by analogy would drop a model the graph loads. Same class name, opposite answers, and
    only the walk can tell them apart.
    """
    reached = {}
    for path, output_node in (
        (LTX25_EXTENDER_EXPORT, LTX25_EXTENDER_OUTPUT_NODE),
        (LTX25_ENHANCER_EXPORT, LTX25_ENHANCER_OUTPUT_NODE),
        (AUDIOREPLACER_EXPORT, AUDIOREPLACER_OUTPUT_NODE),
    ):
        export = json.loads(path.read_text(encoding="utf-8"))
        reachable = reachable_node_ids(export, [output_node])
        classes = {export[node_id]["class_type"] for node_id in reachable}
        assert "LatentUpscaleModelLoader" in {
            node["class_type"] for node in export.values()
        }, path
        reached[path.name] = "LatentUpscaleModelLoader" in classes

    assert reached[LTX25_EXTENDER_EXPORT.name] is True
    assert reached[LTX25_ENHANCER_EXPORT.name] is False
    assert reached[AUDIOREPLACER_EXPORT.name] is False
    # And the adapters agree with their own graphs.
    assert "LatentUpscaleModelLoader" in {
        node["class_type"] for node in ltx25_extend_payload().values()
    }
    assert "LatentUpscaleModelLoader" not in {
        node["class_type"] for node in ltx25_enhance_payload().values()
    }


def test_the_extend_payload_builds_the_reachable_classes_it_does_not_substitute():
    """The payload is the reachable subgraph, substitutions declared and nothing else changed.

    Held to equality on both sides: a class the export reaches and the payload neither builds
    nor declares a substitution for is a dependency quietly dropped, and a class the payload
    builds that the export never names is a graph this project invented.
    """
    export = ltx25_extender_export()
    reachable = reachable_node_ids(export, [LTX25_EXTENDER_OUTPUT_NODE])
    export_classes = {export[node_id]["class_type"] for node_id in reachable}
    payload_classes = {node["class_type"] for node in ltx25_extend_payload().values()}

    substituted = set(LTX25_EXTEND_SUBSTITUTIONS)
    assert substituted <= export_classes, sorted(substituted - export_classes)
    # Everything the export reaches is either built under its own name or declared substituted,
    # and nothing the payload builds is unaccounted for on the other side.
    assert export_classes - payload_classes == substituted
    replacements = {
        stand_in for stand_in in LTX25_EXTEND_SUBSTITUTIONS.values() if stand_in is not None
    }
    assert payload_classes - export_classes == replacements | LTX25_EXTEND_ADDITIONS
    # And the audit derives the same set from the same two files rather than from this table.
    assert preflight_ltx25_extend.substituted_classes() == substituted


def test_the_extend_payload_declares_only_the_models_the_reachable_subgraph_loads():
    """Five files, derived from the export rather than retyped, and the audio VAE among them.

    The audio VAE is a dependency whichever way `include_audio` goes: the conditioning tail is
    encoded through it before any decision about the saver is made.
    """
    export = ltx25_extender_export()
    reachable = reachable_node_ids(export, [LTX25_EXTENDER_OUTPUT_NODE])
    expected = {
        filename
        for node_id in reachable
        for value in export[node_id]["inputs"].values()
        for filename in preflight_ltx25_extend.nested_model_files(value)
    }

    for payload in (ltx25_extend_payload(), ltx25_extend_payload(include_audio=False)):
        loaded = {
            value
            for node in payload.values()
            for value in node["inputs"].values()
            if isinstance(value, str) and value.endswith(preflight_ltx25_extend.MODEL_SUFFIXES)
        }
        assert loaded == expected
        assert len(loaded) == 5
        assert "ltx-2.5-audio-vae-bf16.safetensors" in loaded
        assert "ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors" in loaded


def test_the_extend_payload_reproduces_the_exports_fixed_sampling():
    """Both schedules, the cfg, the sampler, the refine seed and the negative prompt.

    Read out of the export by chain-walk rather than compared against numbers retyped in this
    test: a constant that drifts in the adapter has to fail against the file, not against a
    copy of itself. The sigma strings are compared verbatim — spacing included — because the
    export's `0.7250` and `0.4219` are what the evidence carries.
    """
    export = ltx25_extender_export()
    payload = ltx25_extend_payload()

    sigmas = {
        node["inputs"]["sigmas"]
        for node in export.values()
        if node["class_type"] == "ManualSigmas"
    }
    assert sigmas == {
        payload["mvp:base_sigmas"]["inputs"]["sigmas"],
        payload["mvp:refine_sigmas"]["inputs"]["sigmas"],
    }
    # The base pass starts from full noise and the refine pass from 0.85 -- a characteristic of
    # the graph, recorded so a quiet retune is visible.
    assert payload["mvp:base_sigmas"]["inputs"]["sigmas"].startswith("1.0,")
    assert payload["mvp:refine_sigmas"]["inputs"]["sigmas"].startswith("0.85,")

    assert {
        node["inputs"]["cfg"] for node in export.values() if node["class_type"] == "CFGGuider"
    } == {payload["mvp:guider"]["inputs"]["cfg"]}
    assert {
        node["inputs"]["sampler_name"]
        for node in export.values()
        if node["class_type"] == "KSamplerSelect"
    } == {payload["mvp:sampler"]["inputs"]["sampler_name"]}
    # Two `RandomNoise` nodes in the export; the refine pass's is reproduced as a constant and
    # the base pass's is the caller's, so only the first has to match.
    seeds = {
        node["inputs"]["noise_seed"]
        for node in export.values()
        if node["class_type"] == "RandomNoise"
    }
    assert payload["mvp:refine_noise"]["inputs"]["noise_seed"] in seeds
    assert ltx25_extend_payload(seed=7)["mvp:base_noise"]["inputs"]["noise_seed"] == 7

    negatives = {
        node["inputs"]["text"]
        for node in export.values()
        if node["class_type"] == "CLIPTextEncode"
    }
    assert payload["mvp:negative"]["inputs"]["text"] in negatives
    assert payload["mvp:prompt"]["inputs"]["text"] == ""


def resolve_export_number(export: dict, node_id: str, output: int = 0):
    """One number out of the export's own constant-and-calculator chain.

    Walks `INTConstant`, `SimpleCalculatorKJ` and `VHS_VideoInfo` exactly as ComfyUI would,
    with the one substitution the adapter's folding rests on: the loaded frame rate a
    `VHS_VideoInfo` reports is whatever `force_rate` was set to, and `force_rate` is the
    framerate constant. `SimpleCalculatorKJ` output 1 is its INT.
    """
    node = export[node_id]
    class_type = node["class_type"]
    if class_type == "INTConstant":
        return node["inputs"]["value"]
    if class_type == "CM_IntToFloat":
        return float(resolve_export_number(export, *node["inputs"]["a"]))
    if class_type == "VHS_VideoInfo":
        # Output 5 is `loaded_fps`, and only that one is resolvable without the file.
        assert output == 5, f"{node_id} output {output} needs the source video"
        rate = next(
            other["inputs"]["value"]
            for other in export.values()
            if other["class_type"] == "INTConstant"
            and other.get("_meta", {}).get("title") == "Framerate"
        )
        return float(rate)
    assert class_type == "SimpleCalculatorKJ", f"{node_id} is a {class_type}"
    expression = node["inputs"]["expression"].replace(" ", "")
    assert expression in LTX25_EXTEND_EXPRESSIONS, expression
    operands = [
        resolve_export_number(export, *node["inputs"][f"variables.{name}"]) for name in "ab"
    ]
    value = LTX25_EXTEND_EXPRESSIONS[expression](*operands)
    return int(value) if output == 1 else value


def test_the_extend_payloads_folded_arithmetic_matches_the_exports_own_expressions():
    """The four numbers the adapter computes in Python instead of in graph nodes.

    `build_ltx25_extend_payload` folds them because `force_rate` pins the loaded rate to the
    framerate argument, which makes each a function of what the caller already passed. This
    re-derives all four from the export's expressions and constants and compares — so the
    folding is checked against the evidence rather than against a comment claiming it is safe.
    """
    export = ltx25_extender_export()
    reference_seconds = export["2000"]["inputs"]["value"]
    extend_seconds = export["2001"]["inputs"]["value"]
    frame_rate = export["1997"]["inputs"]["value"]
    assert (reference_seconds, extend_seconds, frame_rate) == (3, 10, 24)

    reference_frames = resolve_export_number(export, "1993:1946", 1)
    reference_length = resolve_export_number(export, "1993:1948")
    overlap = resolve_export_number(export, "1993:1940")
    end_time = resolve_export_number(export, "1993:1937")
    assert reference_frames == 73
    # The export computes the same length twice, once against the constant rate and once
    # against the rate the loader reports. Folding them into one number is only correct while
    # they agree, which is what `force_rate` guarantees and this asserts.
    assert reference_length == overlap

    payload = ltx25_extend_payload(
        reference_seconds=reference_seconds,
        extend_seconds=extend_seconds,
        frame_rate=frame_rate,
    )
    assert payload["mvp:reference_frames"]["inputs"]["value"] == reference_frames
    assert payload["mvp:reference_length"]["inputs"]["value"] == reference_length
    assert payload["mvp:mask"]["inputs"]["video_start_time"] == reference_length
    assert payload["mvp:mask"]["inputs"]["audio_start_time"] == reference_length
    assert payload["mvp:mask"]["inputs"]["video_end_time"] == end_time
    assert payload["mvp:mask"]["inputs"]["audio_end_time"] == end_time
    assert payload["mvp:reference_audio"]["inputs"]["duration"] == overlap
    assert payload["mvp:generated_audio"]["inputs"]["start_index"] == overlap
    assert payload["mvp:generated_audio"]["inputs"]["duration"] == float(extend_seconds)
    # And the loader really does force the rate, without which none of the above holds.
    assert payload["mvp:source"]["inputs"]["force_rate"] == float(frame_rate)

    # The same derivation at settings the export never carried, by editing the export's own
    # constants and re-running its own expressions. Checked at a different frame rate on
    # purpose: at 24 fps a rate the adapter hardcoded and a rate it read from the argument
    # produce identical numbers, so the export's own values cannot tell the two apart.
    for reference, extend, rate in ((3, 10, 30), (2, 7.5, 16), (5, 1, 48)):
        variant = copy.deepcopy(export)
        variant["2000"]["inputs"]["value"] = reference
        variant["2001"]["inputs"]["value"] = extend
        variant["1997"]["inputs"]["value"] = rate
        built = ltx25_extend_payload(
            reference_seconds=reference, extend_seconds=extend, frame_rate=rate
        )
        assert built["mvp:reference_frames"]["inputs"]["value"] == resolve_export_number(
            variant, "1993:1946", 1
        ), (reference, rate)
        assert built["mvp:reference_length"]["inputs"]["value"] == resolve_export_number(
            variant, "1993:1948"
        ), (reference, rate)
        assert built["mvp:reference_length"]["inputs"]["value"] == resolve_export_number(
            variant, "1993:1940"
        ), (reference, rate)
        assert built["mvp:mask"]["inputs"]["video_end_time"] == resolve_export_number(
            variant, "1993:1937"
        ), (reference, extend, rate)
        assert built["mvp:source"]["inputs"]["force_rate"] == float(rate)
        assert built["mvp:mask"]["inputs"]["video_fps"] == float(rate)
        assert built["mvp:conditioning"]["inputs"]["frame_rate"] == float(rate)
        assert built["mvp:save"]["inputs"]["frame_rate"] == float(rate)


def test_the_extend_payload_returns_the_head_untouched_and_the_tail_regenerated():
    """The split that decides what survives the extension, wired the way the export wires it.

    The head is `count - reference_frames` frames straight off the loader; the tail is the last
    `reference_frames` and goes through the model. So the seam second comes back out of the
    sampler rather than being spliced -- the property `LTX25_EXTEND_BASE_SIGMAS` describes,
    checked here rather than left as prose.
    """
    payload = ltx25_extend_payload()

    assert payload["mvp:head"]["inputs"]["start_index"] == 0
    assert payload["mvp:head"]["inputs"]["num_frames"] == ["mvp:head_frames", 0]
    assert payload["mvp:head_frames"]["inputs"] == {
        "op": "Sub",
        "a": ["mvp:measure", 3],
        "b": ["mvp:reference_frames", 0],
    }
    assert payload["mvp:tail"]["inputs"]["start_index"] == -1
    assert payload["mvp:tail"]["inputs"]["num_frames"] == ["mvp:reference_frames", 0]
    # The head comes from the loader's own frames; the second half of the batch comes from the
    # decode. Nothing splices the tail back in.
    assert payload["mvp:batch"]["inputs"]["image_1"] == ["mvp:head", 0]
    assert payload["mvp:batch"]["inputs"]["image_2"] == ["mvp:generated", 0]
    assert payload["mvp:generated"]["inputs"]["image"] == ["mvp:decode", 0]
    assert payload["mvp:decode"]["inputs"]["samples"] == ["mvp:refine_split", 0]
    # The mask keeps the reference seconds and pads out to hold the new ones.
    assert payload["mvp:mask"]["inputs"]["max_length"] == "pad"
    # And nothing caps or decimates the loaded frames, which would make the head's length a
    # thing this application chose rather than a thing it measured. `force_rate` is the one
    # deliberate exception -- the export forces it, and the folded arithmetic depends on it.
    assert payload["mvp:source"]["inputs"]["frame_load_cap"] == 0
    assert payload["mvp:source"]["inputs"]["select_every_nth"] == 1
    assert payload["mvp:source"]["inputs"]["skip_first_frames"] == 0


def test_include_audio_drops_only_the_output_chain_and_never_the_conditioning():
    """The one control that changes the graph's shape, held to exactly what it claims.

    False removes the decode/trim/concat and the saver's optional link. It does **not** touch
    the audio *conditioning*, and it does not turn this into the no-audio export, which feeds
    an empty latent instead -- a claim `build_ltx25_extend_payload` makes and this pins.
    """
    with_audio = ltx25_extend_payload()
    without = ltx25_extend_payload(include_audio=False)

    assert set(with_audio) - set(without) == {
        "mvp:audio",
        "mvp:decode_audio",
        "mvp:generated_audio",
    }
    assert set(without) - set(with_audio) == set()
    assert with_audio["mvp:save"]["inputs"]["audio"] == ["mvp:audio", 0]
    assert "audio" not in without["mvp:save"]["inputs"]
    # The conditioning is identical on both, audio VAE included.
    for name in (
        "mvp:loudness",
        "mvp:reference_audio",
        "mvp:reference_audio_start",
        "mvp:encode_reference_audio",
        "mvp:audio_vae",
        "mvp:mask",
    ):
        assert without[name] == with_audio[name], name
    assert "LTXVEmptyLatentAudio" not in {node["class_type"] for node in without.values()}
    # Neither shape leaves an unreachable node behind, which is the defect this project spends
    # a reachability walk to keep out of other people's exports.
    for payload in (with_audio, without):
        assert reachable_node_ids(payload, ["mvp:save"]) == set(payload)


def test_the_extend_payload_refuses_what_the_schema_would_reject():
    """Every restated ceiling and every shape refusal, each naming its own number.

    A value outside a declared range is rejected by `/prompt` validation before a node runs,
    and reaches the Director as an opaque 502 after the submission round-trip. Refusing here
    costs nothing and says which number was wrong.
    """
    for message, overrides in (
        ("source video path", {"source_video": ""}),
        ("quoted or padded", {"source_video": '"J:/comfy/output/take_00001.mp4"'}),
        ("is not one of those", {"source_video": "J:/comfy/output/take_00001.avi"}),
        ("filename prefix", {"prefix": "  "}),
        ("prompt must be text", {"prompt": 3}),
        ("seed must be a whole number", {"seed": 1.5}),
        ("RandomNoise.noise_seed", {"seed": -1}),
        ("RandomNoise.noise_seed", {"seed": preflight_ltx25_extend.LTX25_EXTEND_MAX_SEED + 1}),
        ("frame rate", {"frame_rate": 0}),
        ("force_rate tops out", {"frame_rate": 61}),
        ("width", {"width": 0}),
        ("ImageResizeKJv2.height tops out", {"height": 16385}),
        ("reference window", {"reference_seconds": 0}),
        ("num_frames tops out", {"reference_seconds": 200}),
        ("more than zero seconds", {"extend_seconds": 0}),
        ("must be a finite number", {"extend_seconds": float("nan")}),
        ("LTXVAudioVideoMask spans at most", {"extend_seconds": 10_001}),
    ):
        with pytest.raises(ValueError, match=re.escape(message)):
            ltx25_extend_payload(**overrides)
    # A capitalised extension is the same container.
    assert ltx25_extend_payload(source_video="J:/comfy/output/TAKE.MP4")


def test_the_extend_payloads_validate_against_the_recorded_object_info():
    """Both audited variants, whole, against the recorded schema."""
    object_info = recorded_object_info()

    for label, payload in preflight_ltx25_extend.audit_payloads():
        assert preflight.validate(label, payload, object_info) == [], label
        # Every numeric literal resolves a bound, so no range check is passing vacuously.
        assert preflight.unbounded_numeric_inputs(label, payload, object_info) == [], label


def test_the_extend_audit_wires_every_check_it_defines():
    """A check defined and not wired still passes its unit test while auditing nothing."""
    defined = {
        name
        for name in dir(preflight_ltx25_extend)
        if name.startswith("check_") and callable(getattr(preflight_ltx25_extend, name))
    }

    assert {check.__name__ for check in preflight_ltx25_extend.CHECKS} == defined


def test_every_restated_extend_ceiling_has_a_row_in_the_audit():
    """A constant added to the adapter without a row here is a ceiling checked against nothing."""
    restated = {
        name
        for name in dir(workflows_module)
        if name.startswith("LTX25_EXTEND_MAX_")
    }

    assert {name for name, *_ in preflight_ltx25_extend.DECLARED_LIMITS} == restated


def test_each_extend_check_passes_the_recorded_schema_and_names_a_moved_one():
    """Every check is clean against the fixture, and every check *finds* its own mutation.

    The second half is the point. These audits were shown to be gutable while still printing
    OK, so each check is driven against a schema mutated in exactly the way it exists to catch
    -- a blanked check would pass the first half and fail here.
    """
    object_info = recorded_object_info()
    for check in preflight_ltx25_extend.CHECKS:
        assert check(object_info) == [], check.__name__

    # A model file that left its loader's options.
    moved = copy.deepcopy(object_info)
    moved["VAELoaderKJ"]["input"]["required"]["vae_name"][0] = ["something-else.safetensors"]
    assert any(
        "ltx-2.5-audio-vae-bf16.safetensors" in problem
        for problem in preflight_ltx25_extend.check_model_files(moved)
    )

    # A container list that stopped matching the node's.
    moved = copy.deepcopy(object_info)
    moved["VHS_LoadVideoPath"]["input"]["required"]["video"][1]["vhs_path_extensions"] = ["mp4"]
    assert preflight_ltx25_extend.check_source_extensions(moved) != []
    # And one that vanished entirely, which would otherwise check the adapter against nothing.
    moved["VHS_LoadVideoPath"]["input"]["required"]["video"][1].pop("vhs_path_extensions")
    assert preflight_ltx25_extend.check_source_extensions(moved) != []

    # A ceiling that moved, and a ceiling that disappeared.
    for mutate in (
        lambda spec: spec[1].__setitem__("max", 30),
        lambda spec: spec[1].pop("max"),
    ):
        moved = copy.deepcopy(object_info)
        mutate(moved["VHS_LoadVideoPath"]["input"]["required"]["force_rate"])
        problems = preflight_ltx25_extend.check_declared_limits(moved)
        assert any("LTX25_EXTEND_MAX_FRAME_RATE" in problem for problem in problems), problems

    # A substituted class that is not installed after all.
    moved = copy.deepcopy(object_info)
    del moved["SimpleCalculatorKJ"]
    problems = preflight_ltx25_extend.check_substitutions_are_for_schema_shape_not_absence(moved)
    assert any("SimpleCalculatorKJ" in problem for problem in problems), problems


def test_the_extend_audit_refuses_a_dependency_list_built_from_the_node_list(monkeypatch):
    """The mutation this design exists to survive, driven through the audit itself.

    Two directions, because this graph is the one where both are live: a payload that dropped
    `LatentUpscaleModelLoader` -- the habit the other two LTX adapters correctly follow -- and
    a payload that added a model the reachable subgraph never loads.
    """
    honest = preflight_ltx25_extend.audit_payloads()

    def without_the_upscaler() -> list[tuple[str, dict]]:
        label, payload = honest[0]
        payload = copy.deepcopy(payload)
        del payload["mvp:upscaler"]
        return [(label, payload)]

    def with_an_invented_model() -> list[tuple[str, dict]]:
        label, payload = honest[0]
        payload = copy.deepcopy(payload)
        payload["mvp:extra"] = {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": "not-in-this-graph.safetensors", "weight_dtype": "default"},
        }
        return [(label, payload)]

    for build in (without_the_upscaler, with_an_invented_model):
        monkeypatch.setattr(preflight_ltx25_extend, "audit_payloads", build)
        problems = preflight_ltx25_extend.check_dependencies_come_from_the_reachable_subgraph(
            recorded_object_info()
        )
        assert any("reachable subgraph loads" in problem for problem in problems), build.__name__


def test_a_blanked_extend_check_fails_this_suite_rather_than_printing_ok(monkeypatch, tmp_path):
    """The audit's failure path, driven end to end against a stubbed server.

    `run_audit` is what the script calls, so this is where "a failing check fails the audit"
    has to be true -- and where "a failing audit records no fixture" has to be true with it.
    Driven through `fetch` rather than a live server, so the failure path is exercised by the
    suite instead of by a person remembering to break something.
    """
    object_info = recorded_object_info()
    fixture = tmp_path / "object_info.json"

    def stub(base_url: str) -> dict:
        return object_info

    # Clean: the audit passes and writes the fixture it was asked for.
    preflight.run_audit(
        preflight_ltx25_extend.audit_payloads(),
        base_url="http://stub",
        record=True,
        checks=preflight_ltx25_extend.CHECKS,
        fetch=stub,
        fixture_path=fixture,
    )
    assert fixture.exists()
    fixture.unlink()

    # One check reporting a problem must fail the whole audit and record nothing.
    def reports(_object_info: dict) -> list[str]:
        return ["limits: a ceiling moved"]

    with pytest.raises(SystemExit) as failure:
        preflight.run_audit(
            preflight_ltx25_extend.audit_payloads(),
            base_url="http://stub",
            record=True,
            checks=(*preflight_ltx25_extend.CHECKS, reports),
            fetch=stub,
            fixture_path=fixture,
        )
    assert failure.value.code == 1
    assert not fixture.exists()

    # The mutation the brief calls for, one check at a time: replace a check with a blank that
    # always returns [], hand the audit a schema that check would have rejected, and the audit
    # must go from failing to printing OK. That transition is what proves the check is doing
    # the work -- and it is why the per-check mutations above are the ones that must stay red.
    # Each mutation is one only its own check can see. A schema break that `validate` also
    # catches would keep the audit red with the check blanked, and would prove nothing about
    # the check.
    breakage = {
        # An option list that stopped being readable. `validate` skips an unreadable COMBO
        # rather than blaming the payload for it; naming the file is this check's whole job.
        "check_model_files": lambda info: info["VAELoaderKJ"]["input"]["required"][
            "vae_name"
        ].__setitem__(0, "COMBO"),
        "check_source_extensions": lambda info: info["VHS_LoadVideoPath"]["input"]["required"][
            "video"
        ][1].__setitem__("vhs_path_extensions", ["mp4"]),
        "check_declared_limits": lambda info: info["VHS_LoadVideoPath"]["input"]["required"][
            "force_rate"
        ][1].__setitem__("max", 30),
        "check_substitutions_are_for_schema_shape_not_absence": lambda info: info.pop(
            "SimpleCalculatorKJ"
        ),
    }
    assert set(breakage) <= {check.__name__ for check in preflight_ltx25_extend.CHECKS}

    for name, break_it in breakage.items():
        broken = copy.deepcopy(object_info)
        break_it(broken)

        def broken_stub(base_url: str, schema: dict = broken) -> dict:
            return schema

        arguments = {
            "base_url": "http://stub",
            "record": False,
            "fetch": broken_stub,
            "fixture_path": fixture,
        }
        with pytest.raises(SystemExit):
            preflight.run_audit(
                preflight_ltx25_extend.audit_payloads(),
                checks=preflight_ltx25_extend.CHECKS,
                **arguments,
            )
        # Blank that one check and the same broken schema audits clean, which is exactly the
        # gutted-but-green failure this suite exists to make impossible.
        blanked = tuple(
            (lambda _info: []) if check.__name__ == name else check
            for check in preflight_ltx25_extend.CHECKS
        )
        preflight.run_audit(
            preflight_ltx25_extend.audit_payloads(), checks=blanked, **arguments
        )
