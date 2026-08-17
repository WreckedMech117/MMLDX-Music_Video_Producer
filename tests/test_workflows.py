import hashlib
import json
from pathlib import Path

import preflight_songplanner
import pytest

from music_video_producer.app import SongPlannerRequest
from music_video_producer.workflows import (
    LTX25_DIVISOR,
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
        ("ltx25-patched", patch_ltx25_dimension_boundary(template)),
    ]


# Node classes the recorded fixture does not cover, so nothing above range-checks
# them offline. Recorded explicitly rather than skipped silently: this list is the
# honest measure of the guard's reach, and it shrinks only when
# `preflight_songplanner.py --record` is extended past the SongPlanner classes.
UNRECORDED_CLASSES = frozenset({
    "BasicGuider", "BasicScheduler", "CFGGuider", "CLIPTextEncode", "ComfyMathExpression",
    "DualCLIPLoader", "EmptySD3LatentImage", "FluxGuidance", "FrameInterpolate",
    "FrameInterpolationModelLoader", "GetImageSize", "ImageResizeKJv2", "KSamplerSelect",
    "Krea2EditGroundedEncode", "Krea2EditModelPatch", "LTXVAudioVAEDecode", "LTXVAudioVAEEncode",
    "LTXVConcatAVLatent", "LTXVConditioning", "LTXVImgToVideoInplace", "LTXVLatentUpsampler",
    "LTXVSeparateAVLatent", "LatentUpscaleModelLoader", "LoadImage", "LoraLoaderModelOnly",
    "ManualSigmas", "MathExpression|pysssss", "MiniMaxH3DirectorCS", "MiniMaxH3MediaLoader",
    "MiniMaxH3ReferenceSplitter", "MiniMaxH3ReferenceToVideo", "MiniMaxH3SigmaShift",
    "ModelPreviewOverrideKJ", "ModelSamplingFlux", "PathchSageAttentionKJ", "PrimitiveFloat",
    "PrimitiveStringMultiline", "RTXVideoSuperResolution", "RandomNoise", "ResolutionSelector",
    "SamplerCustomAdvanced", "SaveImage", "SeedVR2LoadDiTModel", "SeedVR2LoadVAEModel",
    "SeedVR2VideoUpscaler", "SetLatentNoiseMask", "SolidMask", "VAEDecode", "VAEDecodeTiled",
    "VAEEncode", "VHS_LoadAudio", "VHS_LoadImagePath", "VHS_VideoCombine", "easy cleanGpuUsed",
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
        assert preflight_songplanner.validate(label, payload, object_info) == []


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

    low_problems = preflight_songplanner.validate("below", below, object_info)
    high_problems = preflight_songplanner.validate("above", above, object_info)
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
    seed_problems = preflight_songplanner.validate("seed", wide_seed, object_info)
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

    assert preflight_songplanner.numeric_bounds(planner["duration_seconds"]) == (30.0, 300.0)
    # A combo spec carries no numeric bounds, and off-step values are not a problem:
    # ComfyUI only rejects min/max violations.
    assert preflight_songplanner.numeric_bounds(planner["text_encoder"]) == (None, None)
    off_step = build_songplanner_invented_payload(
        idea="off step", genre_hint="", duration=37.5, seed=0, prefix="range"
    )
    assert preflight_songplanner.validate("off-step", off_step, object_info) == []


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
        minimum, maximum = preflight_songplanner.numeric_bounds(planner[schema_input])
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
        assert preflight_songplanner.unbounded_numeric_inputs(label, payload, object_info) == []


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
        assert preflight_songplanner.validate(label, covered, object_info) == [], label

    assert uncovered == UNRECORDED_CLASSES


def test_range_check_rejects_a_fractional_value_on_an_int_input():
    """`steps=1.5` sits inside 1-10000 but is not an integer, and INT says it must be."""
    object_info = recorded_object_info()
    payload = build_music3_payload(caption="c", lyrics="l", duration=120, seed=0, prefix="p")
    payload["50"]["inputs"]["steps"] = 1.5

    problems = preflight_songplanner.validate("fractional", payload, object_info)

    assert any(
        "steps=1.5" in problem and "fractional but the schema declares INT" in problem
        for problem in problems
    ), problems
    # A whole-number float is what ComfyUI itself accepts for an INT, so it is not a defect.
    payload["50"]["inputs"]["steps"] = 30.0
    assert preflight_songplanner.validate("integral", payload, object_info) == []


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
        options = preflight_songplanner.combo_options(spec)
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
