import copy
import hashlib
import json
import tempfile
from pathlib import Path

import preflight
import preflight_h3_ultra
import preflight_songplanner
import pytest

from music_video_producer.app import SongPlannerRequest
from music_video_producer.timeline import align_h3_frames
from music_video_producer.workflows import (
    H3_DEFAULT_PROFILE,
    H3_DIRECTOR_DEFAULT_STEPS,
    H3_DIRECTOR_MAX_FRAMES,
    H3_DIRECTOR_MAX_SECONDS,
    H3_FRAME_RATE,
    H3_LORA_STRENGTH_LIMITS,
    H3_REFERENCE_LIMITS,
    H3_REFERENCE_MAX_FRAMES,
    H3_REFERENCE_PROFILES,
    H3_SPLIT_OFFSETS,
    LTX25_DIVISOR,
    H3SamplingProfile,
    WorkflowCatalog,
    build_flux_payload,
    build_h3_director_payload,
    build_h3_reference_payload,
    build_multiview_payload,
    build_music3_payload,
    build_songplanner_invented_payload,
    build_songplanner_known_lyrics_payload,
    normalize_to_divisor,
    patch_ltx25_dimension_boundary,
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
        ("ltx25-patched", patch_ltx25_dimension_boundary(template)),
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
UNRECORDED_CLASSES = frozenset({
    "CFGGuider", "CLIPTextEncode", "ComfyMathExpression",
    "DualCLIPLoader", "EmptySD3LatentImage", "FluxGuidance", "FrameInterpolate",
    "FrameInterpolationModelLoader", "GetImageSize", "ImageResizeKJv2",
    "Krea2EditGroundedEncode", "Krea2EditModelPatch", "LTXVAudioVAEDecode", "LTXVAudioVAEEncode",
    "LTXVConcatAVLatent", "LTXVConditioning", "LTXVImgToVideoInplace", "LTXVLatentUpsampler",
    "LTXVSeparateAVLatent", "LatentUpscaleModelLoader", "LoadImage",
    "ManualSigmas", "MathExpression|pysssss",
    "ModelSamplingFlux", "PrimitiveFloat",
    "PrimitiveStringMultiline", "RTXVideoSuperResolution", "ResolutionSelector",
    "SaveImage", "SeedVR2LoadDiTModel", "SeedVR2LoadVAEModel",
    "SeedVR2VideoUpscaler", "SetLatentNoiseMask", "SolidMask", "VAEDecodeTiled",
    "VAEEncode", "VHS_LoadAudio", "VHS_LoadImagePath", "easy cleanGpuUsed",
    "easy clearCacheAll",
})

SONGPLANNER_EXPORTS = {
    REFERENCE_EXPORTS
    / "songplanner-invented-user-export.json": "8c313fda7665ccb79a9aeb02734f3d5c04f7f92821af3d0dbff764bc718ec28a",
    REFERENCE_EXPORTS
    / "songplanner-known-lyrics-user-export.json": "24485cf273bf1be1be798c50be65081f5737264f8c0dc6ffb1004389682523b2",
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
    assert encoder["inputs"]["max_duration"] == 90.0
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
            idea="ballad", genre_hint="", lyrics="  \n", duration=60, seed=3, prefix="mvp/songs/x"
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
                seed=3,
                prefix="mvp/songs/x",
            )


def test_songplanner_builders_differ_only_in_node_45_lyric_handling():
    shared = {
        "idea": "ballad",
        "genre_hint": "rock",
        "duration": 60,
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
        idea="too short", genre_hint="", duration=16, seed=0, prefix="range"
    )
    above = build_songplanner_invented_payload(
        idea="too long", genre_hint="", duration=301, seed=0, prefix="range"
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
        idea="wide seed", genre_hint="", duration=120, seed=2**32, prefix="range"
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
        idea="off step", genre_hint="", duration=37.5, seed=0, prefix="range"
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
    assert conditioner["length"] == 192
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


# --- Sampling profiles -----------------------------------------------------------------
#
# Two evidenced bundles, each reproduced whole. The tests below pin the two directions
# that matter: the default profile emits *exactly* what the adapter emitted before
# profiles existed, and the turbo profile reproduces the Director's own H3 stage rather
# than borrowing half of it.

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
H3_DEFAULT_PROFILE_DIGEST = "57beec67787e9f744770b58d4c52532840634c8ea2333112e31b8711823c00db"


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
    longest = 3592 / 24  # 17 * 211 + 5, the last grid point at or below the ceiling
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
        requested = max(5, round(duration * H3_FRAME_RATE))
        expected = align_h3_frames(requested)
        if expected > H3_REFERENCE_MAX_FRAMES:
            continue
        payload = h3_reference_payload(h3_references("picture", 1), duration=duration)
        length = payload["mvp:condition"]["inputs"]["length"]
        assert length == expected, duration
        # Independent of both implementations: on the grid, never short of the window, and
        # never more than one grid step longer than it needs to be.
        assert length >= 5 and (length - 5) % 17 == 0, duration
        assert requested <= length < requested + 17, duration
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
    }
    assert len(preflight_h3_ultra.CHECKS) == 6


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


def test_the_h3_audit_covers_both_h3_graphs():
    """The text-only Director graph is the one H3 path with live render evidence.

    It was audited by nothing: `MiniMaxH3DirectorCS` was in no audited payload, so its
    literals were range-checked neither live nor offline, and the two graphs share every
    loader and sampler class — a model file renamed under one is renamed under both.
    """
    classes = {
        node["class_type"]
        for _, payload in preflight_h3_ultra.audit_payloads()
        for node in payload.values()
    }

    assert {"MiniMaxH3DirectorCS", "MiniMaxH3ReferenceToVideo"} <= classes
    assert classes <= set(recorded_object_info()), classes - set(recorded_object_info())


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
        # The turbo profile's LoRA is in this set because a payload *loads* it, not
        # because it is named here — which is what makes the live audit's "installed"
        # claim about the file the app would actually submit.
        "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors",
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


def test_the_h3_audit_covers_both_sampling_profiles():
    """AC-3: both profiles reach the live schema, and only one of them carries the LoRA.

    Asserting "some variant has a LoRA" alone would pass on an audit that dropped every
    default-profile variant, and vice versa. Both halves are named, because the audit's
    whole claim is that each shipped configuration was validated — not that one of them
    was, twice.
    """
    variants = preflight_h3_ultra.audit_payloads()
    with_lora = {
        label
        for label, payload in variants
        if any(node["class_type"] == "LoraLoaderModelOnly" for node in payload.values())
    }
    reference = {
        label
        for label, payload in variants
        if any(node["class_type"] == "MiniMaxH3ReferenceToVideo" for node in payload.values())
    }

    # Exactly one, not merely at least one: `next(iter(...))` over a set of two would pick
    # an arbitrary variant, and the assertions below would then pass while saying nothing
    # about the other. Which one is checked has to be determined, not incidental.
    assert len(with_lora) == 1, sorted(with_lora)
    assert reference - with_lora, [label for label, _ in variants]
    turbo = dict(variants)[with_lora.pop()]
    profile = H3_REFERENCE_PROFILES["turbo"]
    assert turbo["mvp:lora"]["inputs"]["lora_name"] == profile.lora
    assert turbo["mvp:lora"]["inputs"]["strength_model"] == profile.lora_strength
    # Its step count comes from the profile rather than from a number restated in the
    # audit, so a moved profile default moves what is audited.
    assert turbo["mvp:scheduler"]["inputs"]["steps"] == profile.steps


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
