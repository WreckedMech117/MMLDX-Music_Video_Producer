import hashlib
import json
from pathlib import Path

import preflight_songplanner
import pytest

from music_video_producer.workflows import (
    WorkflowCatalog,
    build_flux_payload,
    build_h3_director_payload,
    build_h3_reference_payload,
    build_multiview_payload,
    build_music3_payload,
    build_songplanner_invented_payload,
    build_songplanner_known_lyrics_payload,
    patch_ltx25_dimension_boundary,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_EXPORTS = REPO_ROOT / "workflow_templates" / "reference_exports"

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
    assert normalizer["inputs"]["divisible_by"] == 16
    assert payload["6116:6070"]["inputs"]["pixels"] == ["mvp:ltx-size-normalize", 0]
    assert payload["6116:4970"]["inputs"]["image"] == ["mvp:ltx-size-normalize", 0]
    assert payload["6116:6073"]["inputs"]["image"] == ["mvp:ltx-size-normalize", 0]
    assert template["6116:6070"]["inputs"]["pixels"] == ["6112", 0]
