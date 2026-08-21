"""Manual live audit of the song-audio restoration adapter against ComfyUI ``/object_info``.

Not pytest-collected (like ``preflight_ltx25_enhance.py``, whose rules and helpers it shares
through ``tests/preflight.py``). Run from the repo root with a live, user-managed ComfyUI
(never started or stopped here):

    uv run python tests/preflight_audio_replace.py [base_url] [--record]

One payload variant, because this graph has exactly one shape: the adapter takes a take, a
song, three numbers and an output prefix, and there is nothing to configure. It has no
sampling, no prompt and no model.

Beyond the shared per-node validation, five claims about the *adapter* are checked, and the
first is the one that makes this file different from every other pre-flight here:

* **the payload names no model file at all.** The audited export carries 8 nodes of which
  only 3 are reachable from its ``VHS_VideoCombine``; the five orphans are a ``UNETLoader``,
  two ``VAELoaderKJ``, a ``CLIPLoader`` and a ``LatentUpscaleModelLoader`` inherited from the
  parent LTX graph. Built from ``export.values()``, this audit would demand a 22B transformer,
  two VAEs, a Gemma text encoder and a spatial upscaler for a task that decodes a video,
  slices an audio file and muxes them — and would then refuse to run on a machine perfectly
  capable of the work. The correct dependency list here is **empty**, so the check is inverted
  from the enhancer's: a single model filename in this payload is a failure;
* the container extensions the adapter refuses on equal the ones ``VHS_LoadVideoPath.video``
  and ``VHS_LoadAudio.audio_file`` declare, so both local refusals are the nodes' rules rather
  than lists this project invented;
* **both substitutions are necessary rather than convenient.** ``VHS_LoadVideo.video`` and
  ``LoadAudio.audio`` are combos of ComfyUI's *input* directory, and the take and the master
  song live outside it. Asserted against the live schema rather than believed from the
  enhancer's note, because the spec asked for it to be verified here and not assumed;
* the window inputs the adapter sets — ``VHS_LoadAudio.seek_seconds`` and ``duration`` — are
  declared by that node and are numeric. An input ComfyUI does not know is the failure mode
  that matters most on this path: it would be dropped, the whole song would play over a 3.75 s
  shot, and nothing would report an error;
* **``format: "None"`` conforms nothing, and the export's ``"LTXV"`` conforms three things.**
  This is the adapter's one substantive departure from the audited export and it is checked
  against the schema that justifies it, because if "None" ever grew a frame rule the picture
  would start being trimmed under a soundtrack that is not.

``--record`` merges the audited classes into ``tests/fixtures/object_info.json`` only when the
audit found zero problems, keeping every class already recorded there.

**No generation is submitted.** ``/object_info`` is the only endpoint read. Nothing here
measures a frame count, and nothing here may: what the restored file contains is an ``ffprobe``
reading of two files, not a prediction — see ``workflows.audio_replace_lengths``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from preflight import combo_options, declared_type, parse_arguments, repo_src_on_path, run_audit

repo_src_on_path()

# Imported after `repo_src_on_path()` on purpose: run as a script, `src` is not importable
# until that call puts it on the path.
from music_video_producer.workflows import (
    AUDIO_REPLACE_AUDIO_EXTENSIONS,
    AUDIO_REPLACE_SOURCE_FORMAT,
    AUDIO_REPLACE_VIDEO_EXTENSIONS,
    build_audio_replace_payload,
    reachable_node_ids,
)

#: The audited evidence and the one node ComfyUI would execute backwards from.
EXPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflow_templates"
    / "reference_exports"
    / "ltx25-audioreplacer-user-export.json"
)
EXPORT_OUTPUT_NODE = "4"
#: What the export's own video loader is set to, and what this adapter refuses to inherit.
EXPORT_SOURCE_FORMAT = "LTXV"

#: Paths shaped like the two files this adapter is given: an absolute take under ComfyUI's
#: output directory and an absolute song under this application's data root — deliberately on a
#: different drive, because that is the whole reason `LoadAudio` cannot be used. Nothing opens
#: either one; `/object_info` does not touch the filesystem, so neither needs to exist.
AUDIT_SOURCE = "J:/comfy/output/music-video-producer/preflight/shot-h3-reference_00001-audio.mp4"
AUDIT_SONG = "F:/MusicVideoProducer/data/projects/preflight/media/songs/master.mp3"

#: The shot the audit builds a window for: 12 s to 15.75 s of a 154 s master, which is the
#: worked example the spec states and the shape of the take the live check will use.
AUDIT_START = 12.0
AUDIT_DURATION = 3.75
AUDIT_SONG_DURATION = 154.644898
#: The lead such a take records at submission (`Shot.latest_take_lead`): a normal-length window
#: mid-song takes the quarter second. The audited window is therefore the *take's* 11.75 s to
#: 16.2083 s and not the shot's 12 s to 15.75 s — the take is 107 frames of picture beginning a
#: quarter second before the window, and the audio has to cover it (2026-08-21).
AUDIT_TAKE_LEAD = 0.25

#: Filename suffixes that make a payload string a model file. The same set
#: ``preflight_h3_ultra.model_files`` and ``preflight_ltx25_enhance`` use.
MODEL_SUFFIXES = (".safetensors", ".ckpt", ".pt", ".pth")


def audit_payloads() -> list[tuple[str, dict]]:
    """The variant under audit. One, for the reason in the module docstring."""
    return [
        (
            "audio-replace",
            build_audio_replace_payload(
                source_video=AUDIT_SOURCE,
                source_audio=AUDIT_SONG,
                start=AUDIT_START,
                duration=AUDIT_DURATION,
                song_duration=AUDIT_SONG_DURATION,
                take_lead=AUDIT_TAKE_LEAD,
                prefix="music-video-producer/preflight/shot-song-audio",
            ),
        )
    ]


def payload_model_files(variants: list[tuple[str, dict]]) -> set[str]:
    """Every model filename the audited payloads actually load. Expected to be empty."""
    return {
        value
        for _, payload in variants
        for node in payload.values()
        for value in node["inputs"].values()
        if isinstance(value, str) and value.endswith(MODEL_SUFFIXES)
    }


def export_model_files(node_ids: set[str] | None = None) -> set[str]:
    """The model filenames the audited export names, optionally within ``node_ids``."""
    export = json.loads(EXPORT_PATH.read_text(encoding="utf-8"))
    return {
        value
        for node_id, node in export.items()
        if node_ids is None or node_id in node_ids
        for value in node["inputs"].values()
        if isinstance(value, str) and value.endswith(MODEL_SUFFIXES)
    }


def check_dependencies_come_from_the_reachable_subgraph(object_info: dict) -> list[str]:
    """The audited claim this whole file was written around, in its strongest possible form.

    ``object_info`` is deliberately unused: this compares the adapter against the *export*, and
    the failure it guards is one a live schema cannot see. The reachable subgraph loads
    **nothing**, so the correct dependency list is empty and any model filename in the payload
    is a file inherited from an orphan. Stated as three assertions rather than one, because
    "empty" alone would keep passing if the export's orphans ever vanished and the check
    quietly stopped distinguishing anything.
    """
    export = json.loads(EXPORT_PATH.read_text(encoding="utf-8"))
    if EXPORT_OUTPUT_NODE not in export:
        return [f"reachability: the audited export has no node {EXPORT_OUTPUT_NODE} to run from"]
    reachable = reachable_node_ids(export, [EXPORT_OUTPUT_NODE])
    orphaned = set(export) - reachable
    problems: list[str] = []
    if not orphaned:
        # The trap is gone or the reachability walk broke. Either way this check has stopped
        # proving anything, and a silently vacuous check is what the whole pre-flight opposes.
        problems.append(
            "reachability: the audited export has no orphaned nodes, so nothing here "
            "distinguishes a dependency list built from the node list"
        )
    if not export_model_files(orphaned):
        problems.append(
            "reachability: the export's orphaned nodes name no model file, so this audit no "
            "longer demonstrates what a node-list dependency scan would have demanded"
        )
    if export_model_files(reachable):
        problems.append(
            f"reachability: the export's reachable subgraph names model files "
            f"{sorted(export_model_files(reachable))}, which contradicts an adapter that "
            f"loads none"
        )
    loaded = payload_model_files(audit_payloads())
    if loaded:
        problems.append(
            f"reachability: the payload names model files {sorted(loaded)}; this graph loads "
            f"no model at all, so every one of those came from an orphaned loader"
        )
    return problems


def check_path_extensions(object_info: dict) -> list[str]:
    """Both adapter refusal lists against the extensions the two loaders declare.

    Equality rather than containment, for ``preflight_ltx25_enhance.check_source_extensions``'s
    reason: the adapter is restating each node's list so an unreadable file is named locally
    instead of arriving as an opaque ``/prompt`` rejection, and a restated list that stops
    matching is exactly what this audit exists to catch.
    """
    problems: list[str] = []
    for class_type, input_name, declared_here in (
        ("VHS_LoadVideoPath", "video", AUDIO_REPLACE_VIDEO_EXTENSIONS),
        ("VHS_LoadAudio", "audio_file", AUDIO_REPLACE_AUDIO_EXTENSIONS),
    ):
        spec = (
            object_info.get(class_type, {}).get("input", {}).get("required", {}).get(input_name)
        )
        declared = (
            spec[1].get("vhs_path_extensions")
            if isinstance(spec, list) and len(spec) > 1 and isinstance(spec[1], dict)
            else None
        )
        if not isinstance(declared, list):
            problems.append(
                f"extensions: {class_type}.{input_name} declares no vhs_path_extensions, so "
                f"the adapter's list is checked against nothing"
            )
        elif sorted(declared) != sorted(declared_here):
            problems.append(
                f"extensions: the adapter reads {sorted(declared_here)} but "
                f"{class_type}.{input_name} declares {sorted(declared)}"
            )
    return problems


def check_the_substituted_loaders_are_the_only_reachable_ones(object_info: dict) -> list[str]:
    """Why the export's two loaders cannot be used, verified rather than inherited as a note.

    The enhancer recorded that ``VHS_LoadVideo.video`` enumerates ComfyUI's *input* directory
    while a take lives under *output*. The spec asked for the mirror question about
    ``LoadAudio`` to be **verified rather than assumed**, so both are asked here, of the live
    schema, every run:

    * each export loader publishes a COMBO — a fixed list of filenames — and therefore cannot
      be handed an absolute path at all;
    * each substitute publishes a ``STRING``, which is what an absolute path needs.

    A combo that grew a path option would not make the export's loaders usable, so the check is
    on the *shape* rather than on the list being empty. The take lives under ComfyUI's output
    directory and the master song lives under this application's data root, and neither is the
    input directory those combos enumerate.
    """
    problems: list[str] = []
    for combo_class, combo_input, path_class, path_input, subject in (
        ("VHS_LoadVideo", "video", "VHS_LoadVideoPath", "video", "a rendered take"),
        ("LoadAudio", "audio", "VHS_LoadAudio", "audio_file", "the master song"),
    ):
        combo_spec = (
            object_info.get(combo_class, {}).get("input", {}).get("required", {}).get(combo_input)
        )
        if combo_spec is None:
            problems.append(
                f"substitution: {combo_class}.{combo_input} is absent from the live schema, so "
                f"nothing here shows why it was substituted"
            )
        elif combo_options(combo_spec) is None:
            problems.append(
                f"substitution: {combo_class}.{combo_input} no longer publishes combo options, "
                f"so the reason {path_class} replaced it may no longer hold"
            )
        path_spec = (
            object_info.get(path_class, {}).get("input", {}).get("required", {}).get(path_input)
        )
        if declared_type(path_spec) != "STRING":
            problems.append(
                f"substitution: {path_class}.{path_input} is not a STRING, so {subject} cannot "
                f"be given to it as a path"
            )
    return problems


def check_the_window_inputs_are_the_nodes_own(object_info: dict) -> list[str]:
    """``seek_seconds`` and ``duration`` are declared by ``VHS_LoadAudio`` and take numbers.

    The single most dangerous failure on this path, and the reason it gets its own check
    rather than riding on the generic validation: an input ComfyUI does not know is *dropped*,
    not rejected. The window would vanish, the whole 154 s master would be laid over a 3.75 s
    shot, and nothing anywhere would report an error. That is the silent desync this stage
    exists to prevent, arriving through the node instead of through the arithmetic.
    """
    optional = object_info.get("VHS_LoadAudio", {}).get("input", {}).get("optional", {})
    required = object_info.get("VHS_LoadAudio", {}).get("input", {}).get("required", {})
    problems: list[str] = []
    for name in ("seek_seconds", "duration"):
        spec = optional.get(name, required.get(name))
        if spec is None:
            problems.append(
                f"window: VHS_LoadAudio declares no {name!r}, so the window this adapter sets "
                f"would be dropped and the whole song would play over the shot"
            )
        elif declared_type(spec) not in {"FLOAT", "INT"}:
            problems.append(
                f"window: VHS_LoadAudio.{name} is {declared_type(spec)!r} rather than a number"
            )
    return problems


def check_the_source_format_conforms_nothing(object_info: dict) -> list[str]:
    """The adapter's one substantive departure from the export, checked against its reason.

    ``VHS_LoadVideoPath.format`` publishes what each option does. The export's ``"LTXV"`` is
    ``{"target_rate": 24, "dim": [32, 0, 768, 512], "frames": [8, 1]}`` — force the rate,
    floor the dimensions, conform the frame count to ``8n+1``. An H3 take lands on the
    **17k+5** grid, so a 90-frame take would be cut to 89 and the restored song would run one
    frame long against a picture one frame short.

    Both halves are asserted. That ``"None"`` conforms nothing is what makes the adapter
    correct; that ``"LTXV"`` conforms something is what makes the departure necessary rather
    than a preference, and without it this check would keep passing if every option became a
    no-op and the substitution stopped mattering.
    """
    # Optional first: `format` is declared optional on this node and required on nothing, but
    # both are read so a schema that promotes it stays audited rather than silently unchecked.
    inputs = object_info.get("VHS_LoadVideoPath", {}).get("input", {})
    spec = inputs.get("optional", {}).get("format", inputs.get("required", {}).get("format"))
    formats = (
        spec[1].get("formats")
        if isinstance(spec, list) and len(spec) > 1 and isinstance(spec[1], dict)
        else None
    )
    if not isinstance(formats, dict):
        return [
            (
                "format: VHS_LoadVideoPath.format publishes no formats mapping, so neither "
                "the adapter's choice nor the export's is checked against anything"
            )
        ]
    problems: list[str] = []
    chosen = formats.get(AUDIO_REPLACE_SOURCE_FORMAT)
    if chosen is None:
        problems.append(
            f"format: {AUDIO_REPLACE_SOURCE_FORMAT!r} is not an option "
            f"VHS_LoadVideoPath.format offers"
        )
    elif chosen:
        problems.append(
            f"format: {AUDIO_REPLACE_SOURCE_FORMAT!r} now conforms {sorted(chosen)}, so the "
            f"adapter is no longer copying the take's own frames through untouched"
        )
    inherited = formats.get(EXPORT_SOURCE_FORMAT)
    if not inherited:
        problems.append(
            f"format: {EXPORT_SOURCE_FORMAT!r} conforms nothing, so this check no longer shows "
            f"why the adapter departs from the export's loader setting"
        )
    elif "frames" not in inherited:
        problems.append(
            f"format: {EXPORT_SOURCE_FORMAT!r} no longer carries a frame rule, so the frame "
            f"loss this adapter avoids is no longer demonstrated"
        )
    return problems


#: Every check this audit runs. Named as one tuple so a test can assert the audit wires all of
#: them: a check deleted from here is a check that still passes its own unit test while the
#: live audit stops performing it.
CHECKS = (
    check_dependencies_come_from_the_reachable_subgraph,
    check_path_extensions,
    check_the_substituted_loaders_are_the_only_reachable_ones,
    check_the_window_inputs_are_the_nodes_own,
    check_the_source_format_conforms_nothing,
)

#: Classes the checks *read* but no payload submits — the two export loaders whose combo shape
#: is the reason for each substitution. Recorded like any other class so the offline half of
#: the suite is not left auditing a fixture that never heard of them.
EXTRA_CLASSES = ("VHS_LoadVideo", "LoadAudio")


def main() -> None:
    base_url, record = parse_arguments(sys.argv[1:])
    run_audit(
        audit_payloads(),
        base_url=base_url,
        record=record,
        checks=CHECKS,
        extra_classes=EXTRA_CLASSES,
    )


if __name__ == "__main__":
    main()
