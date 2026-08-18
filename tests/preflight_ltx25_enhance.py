"""Manual live audit of the LTX 2.5 enhancement adapter against ComfyUI ``/object_info``.

Not pytest-collected (like ``preflight_h3_ultra.py``, whose rules it shares through
``tests/preflight.py``). Run from the repo root with a live, user-managed ComfyUI
(never started or stopped here):

    uv run python tests/preflight_ltx25_enhance.py [base_url] [--record]

One payload variant, because this graph has exactly one shape: the adapter takes a
source path and an output prefix and nothing else. Every sampling value the export
fixes — the sigmas, cfg 1, ``euler``, seed 0, the empty prompt, the detailer at 0.9 —
is a constant rather than a control, so there is no second combination to audit. A
variant per container extension would send the same graph with a different string
into a ``STRING`` input, which the schema treats identically.

Beyond the shared per-node validation, three claims about the *adapter* are checked:

* the model files the payload actually names — read out of the payload, never restated
  here — are present in their loaders' combo options. Four files: the LTX 2.5
  transformer, the video VAE, the Gemma CLIP and the IC-LoRA detailer;
* the container extensions the adapter refuses on equal the ones
  ``VHS_LoadVideoPath.video`` declares, so the local refusal is the node's rule rather
  than a list this project invented;
* **the dependency list is the reachable subgraph's, not the node list's.** The audited
  export carries 20 nodes and only 18 are reachable from its single ``VHS_VideoCombine``;
  the two orphans are an audio ``VAELoaderKJ`` and a ``LatentUpscaleModelLoader``
  inherited from the parent graph. Built from the node list, this audit would demand an
  audio VAE and a latent spatial upscaler that nothing in this task loads — and would
  then refuse to run on a machine perfectly capable of the work. That check is the whole
  reason this file exists separately from the H3 one, so it is asserted here rather than
  trusted to the comment above.

``--record`` merges the audited classes into ``tests/fixtures/object_info.json`` only
when the audit found zero problems, keeping every class already recorded there.

**No generation is submitted.** ``/object_info`` is the only endpoint read. Nothing here
measures a frame count, and nothing here may: what this graph does to a take's frame
count is a live measurement, not a prediction — see ``build_ltx25_enhance_payload``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from preflight import combo_options, parse_arguments, repo_src_on_path, run_audit

repo_src_on_path()

# Imported after `repo_src_on_path()` on purpose: run as a script, `src` is not
# importable until that call puts it on the path.
from music_video_producer.workflows import (
    LTX25_ENHANCE_SOURCE_EXTENSIONS,
    build_ltx25_enhance_payload,
    reachable_node_ids,
)

#: The audited evidence and the one node ComfyUI would execute backwards from.
EXPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflow_templates"
    / "reference_exports"
    / "ltx25-enhancer-user-export.json"
)
EXPORT_OUTPUT_NODE = "1994"

#: A path shaped like the take this adapter is given: an absolute file under ComfyUI's
#: output directory, in a container the node reads. Nothing opens it — `/object_info`
#: does not touch the filesystem — so it does not need to exist.
AUDIT_SOURCE = "J:/comfy/output/music-video-producer/preflight/shot-h3-reference_00001-audio.mp4"

#: Filename suffixes that make a payload string a model file. The same set
#: ``preflight_h3_ultra.model_files`` uses.
MODEL_SUFFIXES = (".safetensors", ".ckpt", ".pt", ".pth")


def audit_payloads() -> list[tuple[str, dict]]:
    """The variant under audit. One, for the reason in the module docstring."""
    return [
        (
            "ltx25-enhance",
            build_ltx25_enhance_payload(
                source_video=AUDIT_SOURCE,
                prefix="music-video-producer/preflight/shot-ltx25-enhance",
            ),
        )
    ]


def model_files(variants: list[tuple[str, dict]]) -> set[tuple[str, str, str]]:
    """Every ``(class, input, filename)`` the audited payloads actually load.

    Read out of the payloads rather than restated as a literal list, exactly as the H3
    audit does — and here it carries a second guarantee for free. A payload *is* the
    reachable subgraph, because a payload is what gets submitted; so a list derived from
    it cannot pick up an orphaned loader the way a list derived from the export's nodes
    would. ``check_dependencies_come_from_the_reachable_subgraph`` asserts that rather
    than leaving it as a property nobody checks.
    """
    return {
        (node["class_type"], name, value)
        for _, payload in variants
        for node in payload.values()
        for name, value in node["inputs"].items()
        if isinstance(value, str) and value.endswith(MODEL_SUFFIXES)
    }


def nested_model_files(value: object) -> set[str]:
    """Every model filename anywhere inside one input value.

    Recursive rather than a scan of the top level, because the export's
    ``Power Lora Loader (rgthree)`` does not put its filename in an input at all: it sits
    one level down, in the widget dict ``lora_1: {"on": true, "lora": ..., "strength":
    0.9}``. A top-level reader silently omits the detailer — the one dependency of the four
    that ComfyUI's schema cannot see either, which is precisely why this adapter substitutes
    a ``LoraLoader`` for that node.
    """
    if isinstance(value, str):
        return {value} if value.endswith(MODEL_SUFFIXES) else set()
    if isinstance(value, dict):
        return {name for item in value.values() for name in nested_model_files(item)}
    if isinstance(value, list):
        return {name for item in value for name in nested_model_files(item)}
    return set()


def export_model_files(node_ids: set[str] | None = None) -> set[str]:
    """The model filenames the audited export names, optionally within ``node_ids``."""
    export = json.loads(EXPORT_PATH.read_text(encoding="utf-8"))
    return {
        filename
        for node_id, node in export.items()
        if node_ids is None or node_id in node_ids
        for value in node["inputs"].values()
        for filename in nested_model_files(value)
    }


def check_model_files(object_info: dict) -> list[str]:
    """Each loaded model file against its loader's combo options.

    ``validate`` already rejects a value outside the options, so this repeats the check on
    purpose: the audit's headline claim is that these files are present, and it names them
    so a run that loaded none would not look the same as a run that loaded them all.
    """
    problems: list[str] = []
    files = model_files(audit_payloads())
    if not files:
        return ["models: no payload names a model file, so nothing was confirmed installed"]
    for class_type, input_name, filename in sorted(files):
        spec = (
            object_info.get(class_type, {}).get("input", {}).get("required", {}).get(input_name)
        )
        options = combo_options(spec)
        if options is None:
            problems.append(f"models: {class_type}.{input_name} publishes no combo options")
        elif filename not in options:
            problems.append(f"models: {filename} is not installed for {class_type}.{input_name}")
    return problems


def check_source_extensions(object_info: dict) -> list[str]:
    """The adapter's refusal list against the extensions the loader declares.

    Equality rather than containment: the adapter is not choosing a narrower policy here,
    it is restating the node's list so a take in an unreadable container is named locally
    instead of arriving as an opaque ``/prompt`` rejection. A restated list that stops
    matching is exactly what this audit exists to catch.
    """
    spec = (
        object_info.get("VHS_LoadVideoPath", {}).get("input", {}).get("required", {}).get("video")
    )
    declared = (
        spec[1].get("vhs_path_extensions")
        if isinstance(spec, list) and len(spec) > 1 and isinstance(spec[1], dict)
        else None
    )
    if not isinstance(declared, list):
        return [
            (
                "extensions: VHS_LoadVideoPath.video declares no vhs_path_extensions, so the "
                "adapter's list is checked against nothing"
            )
        ]
    if sorted(declared) != sorted(LTX25_ENHANCE_SOURCE_EXTENSIONS):
        return [
            (
                f"extensions: the adapter reads {sorted(LTX25_ENHANCE_SOURCE_EXTENSIONS)} but "
                f"VHS_LoadVideoPath.video declares {sorted(declared)}"
            )
        ]
    return []


def check_dependencies_come_from_the_reachable_subgraph(object_info: dict) -> list[str]:
    """The audited claim this whole file was written around.

    ``object_info`` is deliberately unused: this compares the adapter against the *export*,
    and the failure it guards is one a live schema cannot see. A dependency list built from
    the export's 20 nodes rather than its 18 reachable ones would name an audio VAE and a
    latent spatial upscaler, and ``check_model_files`` would then demand both be installed —
    turning a correct pre-flight into one that refuses a capable machine. Run as a live
    check rather than only as a unit test because it is a precondition of the file check
    above being the right file check.
    """
    export = json.loads(EXPORT_PATH.read_text(encoding="utf-8"))
    if EXPORT_OUTPUT_NODE not in export:
        return [f"reachability: the audited export has no node {EXPORT_OUTPUT_NODE} to run from"]
    reachable = reachable_node_ids(export, [EXPORT_OUTPUT_NODE])
    orphaned = set(export) - reachable
    loaded = {filename for _, _, filename in model_files(audit_payloads())}
    problems: list[str] = []
    if not orphaned:
        # The trap is gone or the reachability walk broke. Either way this check has stopped
        # proving anything, and a silently vacuous check is what the whole pre-flight opposes.
        problems.append(
            "reachability: the audited export has no orphaned nodes, so nothing here "
            "distinguishes a dependency list built from the node list"
        )
    for filename in sorted(export_model_files(orphaned)):
        if filename in loaded:
            problems.append(
                f"reachability: {filename} is loaded by an orphaned node in the export "
                f"({', '.join(sorted(orphaned))}) and the payload declares it anyway"
            )
    expected = export_model_files(reachable)
    if loaded != expected:
        problems.append(
            f"reachability: the payload loads {sorted(loaded)} but the export's reachable "
            f"subgraph loads {sorted(expected)}"
        )
    return problems


#: Every check this audit runs. Named as one tuple so a test can assert the audit wires all
#: of them: a check deleted from here is a check that still passes its own unit test while
#: the live audit stops performing it.
CHECKS = (
    check_model_files,
    check_source_extensions,
    check_dependencies_come_from_the_reachable_subgraph,
)


def main() -> None:
    base_url, record = parse_arguments(sys.argv[1:])
    run_audit(audit_payloads(), base_url=base_url, record=record, checks=CHECKS)


if __name__ == "__main__":
    main()
