"""The H3 image-edit adapter (AI Mod): builder pinned to its export, and the prompt form.

Evidence: `h3-image-edit-user-export.json` / `h3-turbo-image-edit-user-export.json`,
imported 2026-08-19 and SHA-pinned in the MANIFEST. Route behaviour lives in
`test_api.py` beside the multiview promotion it mirrors.
"""

import json
from pathlib import Path

import pytest

from music_video_producer.workflows import (
    H3_IMAGE_EDIT_PROFILES,
    build_h3_image_edit_payload,
    image_edit_prompt,
    reachable_node_ids,
)

REFERENCE_EXPORTS = Path("workflow_templates/reference_exports")
IMAGE_EDIT_EXPORT = "h3-image-edit-user-export.json"
TURBO_IMAGE_EDIT_EXPORT = "h3-turbo-image-edit-user-export.json"


def image_edit_export(name: str = IMAGE_EDIT_EXPORT) -> dict:
    return json.loads((REFERENCE_EXPORTS / name).read_text(encoding="utf-8"))


def image_edit_payload(**overrides) -> dict:
    arguments = {
        "prompt": "subject_definitions:\n<Picture 1> is the base image being edited.",
        "pictures": [{"file": "F:/assets/base.png", "label": "base"}],
        "seed": 7,
        "prefix": "mvp/edit",
    }
    return build_h3_image_edit_payload(**{**arguments, **overrides})


def test_the_image_edit_export_orphans_are_the_mirror_of_the_keyframe_exports():
    """The reachability fact the adapter stands on, derived rather than trusted: 27 nodes,
    24 reachable from the single SaveImage, and the orphans are the Image Comparer, the
    video VAE loader and the `fl2va` UNET — so the reachable checkpoint is `ref2va`,
    exactly the mirror of the keyframe export where `ref2va` was the orphan."""
    export = image_edit_export()
    savers = [nid for nid, node in export.items() if node["class_type"] == "SaveImage"]
    assert len(savers) == 1
    reachable = reachable_node_ids(export, savers)
    assert len(export) == 27 and len(reachable) == 24
    orphans = sorted(export[nid]["class_type"] for nid in export if nid not in reachable)
    assert orphans == ["Image Comparer (rgthree)", "UNETLoader", "VAELoader"]
    orphan_unet = next(
        nid for nid in export
        if nid not in reachable and export[nid]["class_type"] == "UNETLoader"
    )
    assert "fl2va" in export[orphan_unet]["inputs"]["unet_name"]
    reachable_unet = next(
        nid for nid in reachable if export[nid]["class_type"] == "UNETLoader"
    )
    assert "ref2va" in export[reachable_unet]["inputs"]["unet_name"]


def test_the_image_edit_adapter_matches_the_export_it_reproduces():
    """The distinctive wiring, chain-walked out of the export and compared, none retyped:
    the image VAE in the conditioner's `vae` seat AND the decoder; the 2 MP scale between
    the splitter's first output and `ref_image_0`; the sampler's latent from the
    empty-image node, not the conditioner; length 5; ref_image_size max; and neither a
    sigma shift nor a preview node anywhere — this export has none, and emitting the
    video graphs' habit would be invention."""
    export = image_edit_export()
    payload = image_edit_payload()

    cond_id = next(
        nid for nid, n in export.items() if n["class_type"] == "MiniMaxH3ReferenceToVideo"
    )
    cond = export[cond_id]["inputs"]
    ours = payload["mvp:condition"]["inputs"]
    assert ours["length"] == cond["length"] == 5
    assert ours["ref_image_size"] == cond["ref_image_size"] == "max"

    # The image VAE: the export wires one VAELoader into both the conditioner's vae seat
    # and the VAEDecode; the adapter must use the same file in the same two seats.
    vae_ref = cond["vae"]
    vae_name = export[vae_ref[0]]["inputs"]["vae_name"]
    assert "t1_image_vae" in vae_name
    decode_id = next(nid for nid, n in export.items() if n["class_type"] == "VAEDecode")
    assert export[decode_id]["inputs"]["vae"] == vae_ref
    assert payload["mvp:image_vae"]["inputs"]["vae_name"] == vae_name
    assert ours["vae"] == ["mvp:image_vae", 0]
    assert payload["mvp:image"]["inputs"]["vae"] == ["mvp:image_vae", 0]

    # The scale node: 2 MP, x32, crop, lanczos, feeding ref_image_0.
    scale_id = next(
        nid for nid, n in export.items() if n["class_type"] == "ImageScaleToTotalPixelsX"
    )
    scale = export[scale_id]["inputs"]
    for key in ("megapixels", "multiple_of", "resize_mode", "upscale_method"):
        assert payload["mvp:scale"]["inputs"][key] == scale[key], key
    assert cond["ref_images.ref_image_0"] == [scale_id, 0]
    assert ours["ref_images.ref_image_0"] == ["mvp:scale", 0]

    # The latent: EmptyMiniMaxH3ImageLatentAV into the sampler, batch 1.
    latent_id = next(
        nid for nid, n in export.items() if n["class_type"] == "EmptyMiniMaxH3ImageLatentAV"
    )
    sampler_id = next(
        nid for nid, n in export.items() if n["class_type"] == "SamplerCustomAdvanced"
    )
    assert export[sampler_id]["inputs"]["latent_image"] == [latent_id, 0]
    assert payload["mvp:sample"]["inputs"]["latent_image"] == ["mvp:latent", 0]
    assert (
        payload["mvp:latent"]["inputs"]["batch_size"]
        == export[latent_id]["inputs"]["batch_size"]
        == 1
    )

    # No shift, no preview — matching the export, asserted in both directions.
    for cls in ("MiniMaxH3SigmaShift", "ModelPreviewOverrideKJ"):
        assert all(n["class_type"] != cls for n in export.values()), cls
        assert all(n["class_type"] != cls for n in payload.values()), cls
    # Spectrum enabled in the export, omitted here — both prior H3 adapters' decision.
    assert any(n["class_type"] == "SpectrumApplyMiniMaxH3" for n in export.values())
    assert all(n["class_type"] != "SpectrumApplyMiniMaxH3" for n in payload.values())

    # The reachable class set minus the stated drops is exactly what the adapter emits.
    savers = [nid for nid, n in export.items() if n["class_type"] == "SaveImage"]
    reachable = {export[nid]["class_type"] for nid in reachable_node_ids(export, savers)}
    dropped = {
        "SpectrumApplyMiniMaxH3",       # recorded omission, both prior H3 adapters
        "Power Lora Loader (rgthree)",  # empty here; profiles use LoraLoaderModelOnly
        "INTConstant",                  # the Any Switch constants, resolved server-side
        "Any Switch (rgthree)",         # ditto
        "PrimitiveStringMultiline",     # the prompt travels as a literal, as everywhere
    }
    emitted = {n["class_type"] for n in image_edit_payload().values()}
    assert reachable - dropped == emitted


def test_the_image_edit_profiles_are_the_two_exports_bundles_exactly():
    """Default from the plain export, turbo from its twin — chain-walked, not retyped.
    The turbo scheduler is `beta`, which differs from turbo-references2v's `simple` with
    the same LoRA: bundles are evidenced per export, never borrowed across."""
    plain = image_edit_export()
    sched = next(n for n in plain.values() if n["class_type"] == "BasicScheduler")["inputs"]
    sampler = next(n for n in plain.values() if n["class_type"] == "KSamplerSelect")["inputs"]
    default = image_edit_payload()
    assert default["mvp:scheduler"]["inputs"]["scheduler"] == sched["scheduler"] == "simple"
    assert default["mvp:scheduler"]["inputs"]["steps"] == sched["steps"] == 20
    assert (
        default["mvp:sampler"]["inputs"]["sampler_name"]
        == sampler["sampler_name"]
        == "res_multistep"
    )
    assert "mvp:lora" not in default
    assert default["mvp:attention"]["inputs"]["model"] == ["mvp:model", 0]

    turbo_export = image_edit_export(TURBO_IMAGE_EDIT_EXPORT)
    t_sched = next(
        n for n in turbo_export.values() if n["class_type"] == "BasicScheduler"
    )["inputs"]
    t_sampler = next(
        n for n in turbo_export.values() if n["class_type"] == "KSamplerSelect"
    )["inputs"]
    t_lora = next(
        n for n in turbo_export.values()
        if n["class_type"] == "Power Lora Loader (rgthree)"
    )["inputs"]["lora_1"]
    turbo = image_edit_payload(profile="turbo")
    assert turbo["mvp:scheduler"]["inputs"]["scheduler"] == t_sched["scheduler"] == "beta"
    assert turbo["mvp:scheduler"]["inputs"]["steps"] == t_sched["steps"] == 8
    assert turbo["mvp:sampler"]["inputs"]["sampler_name"] == t_sampler["sampler_name"] == "euler"
    assert t_lora["on"] is True
    assert turbo["mvp:lora"]["inputs"]["lora_name"] == t_lora["lora"]
    assert turbo["mvp:lora"]["inputs"]["strength_model"] == t_lora["strength"] == 1
    assert turbo["mvp:attention"]["inputs"]["model"] == ["mvp:lora", 0]

    assert set(H3_IMAGE_EDIT_PROFILES) == {"default", "turbo"}
    with pytest.raises(ValueError, match="Unknown H3 image-edit profile"):
        image_edit_payload(profile="warp")


def test_the_image_edit_builder_refuses_what_it_cannot_build():
    with pytest.raises(ValueError, match="requires a prompt"):
        image_edit_payload(prompt="   ")
    with pytest.raises(ValueError, match="at least the base picture"):
        image_edit_payload(pictures=[])
    ten = [{"file": f"F:/assets/{index}.png"} for index in range(10)]
    with pytest.raises(ValueError, match="at most 9 pictures"):
        image_edit_payload(pictures=ten)
    with pytest.raises(ValueError, match="file path"):
        image_edit_payload(pictures=[{"file": ""}])
    with pytest.raises(ValueError, match="steps must be a positive integer"):
        image_edit_payload(steps=0)

    # Multi-picture wiring: extras ride the splitter raw from slot 1 up; the base is the
    # scaled slot 0. The ceiling — the Director's storyboard/characters-into-scenes note —
    # is the same nine slots the reference path has.
    three = image_edit_payload(
        pictures=[
            {"file": "F:/assets/base.png", "label": "base"},
            {"file": "F:/assets/outfit.png", "label": "outfit"},
            {"file": "F:/assets/scene.png", "label": "scene"},
        ]
    )
    cond = three["mvp:condition"]["inputs"]
    assert cond["ref_images.ref_image_0"] == ["mvp:scale", 0]
    assert cond["ref_images.ref_image_1"] == ["mvp:split", 1]
    assert cond["ref_images.ref_image_2"] == ["mvp:split", 2]
    media = json.loads(three["mvp:references"]["inputs"]["media_state"])
    assert [item["kind"] for item in media] == ["picture"] * 3

    # Geometry: the export's resolved canvas by default, explicit override honoured.
    assert image_edit_payload()["mvp:latent"]["inputs"] == {
        "width": 1920, "height": 1080, "batch_size": 1,
    }
    wide = image_edit_payload(width=1056, height=608)
    assert wide["mvp:latent"]["inputs"]["width"] == 1056
    assert wide["mvp:condition"]["inputs"]["height"] == 608


def test_the_image_edit_prompt_follows_the_workflows_own_guide():
    """The bundled guide's shape, and the one deliberate divergence: clothing and pose are
    NOT in the always-preserve preamble, because "change the outfit" is the Director's own
    worked example and a preamble that pre-forbids the edit would fight the instruction."""
    wrapped = image_edit_prompt(
        "Change her boots to bright red leather boots.",
        source_kind="character",
        source_label="HarderFaster",
    )
    assert wrapped.startswith("subject_definitions:")
    assert "<Picture 1> is the base image being edited." in wrapped
    assert "<Subject 1> is the character from <Picture 1> (HarderFaster)" in wrapped
    assert (
        "[reference generation] The target image is an edited version of <Picture 1>."
        in wrapped
    )
    assert "<Picture 1>: attribute_transfer" in wrapped
    assert "<Subject 1>: fully_preserved" in wrapped
    assert "Change her boots to bright red leather boots." in wrapped
    assert "overall_soundscape:\nSilence" in wrapped
    assert "non_diegetic_music:\nN/A" in wrapped
    preserve = wrapped.split("detailed_description:")[1].split("overall_soundscape:")[0]
    assert "identity" in preserve
    assert "clothing" not in preserve and "pose" not in preserve

    # A non-character source has no identity to preserve — the plain form, no <Subject 1>.
    plain = image_edit_prompt(
        "Add rain streaking the windows.", source_kind="setting", source_label="Warehouse"
    )
    assert "<Subject 1>" not in plain
    assert "(Warehouse)" in plain
    assert "attribute_transfer" in plain

    # Extra pictures number from 2 in both the definitions and the retention analysis.
    extras = image_edit_prompt(
        "Put the character in the scene.",
        source_kind="character",
        source_label="Lucy",
        extra_labels=("the warehouse stage",),
    )
    assert "<Picture 2> is the warehouse stage." in extras
    assert "<Picture 2>: reference - additional visual reference." in extras

    # A hand-written full form travels verbatim, never double-wrapped.
    structured = "subject_definitions:\n<Picture 1> is x.\n\ndetailed_description:\nY."
    assert (
        image_edit_prompt(structured, source_kind="character", source_label="L")
        == structured
    )
