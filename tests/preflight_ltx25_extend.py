"""Manual live audit of the LTX 2.5 video-extension adapter against ComfyUI ``/object_info``.

Not pytest-collected (like ``preflight_h3_ultra.py`` and ``preflight_ltx25_enhance.py``, whose
rules it shares through ``tests/preflight.py``). Run from the repo root with a live,
user-managed ComfyUI (never started or stopped here):

    uv run python tests/preflight_ltx25_extend.py [base_url] [--record]

Two payload variants, because this adapter has exactly two shapes: ``include_audio`` decides
whether the saver is handed a track, and with it whether the audio decode, trim and concat are
built at all. Everything else varies only in the *value* of a number the schema treats
identically, so a third variant would audit nothing new.

Beyond the shared per-node validation, four claims about the *adapter* are checked:

* the model files the payload actually names — read out of the payload, never restated here —
  are present in their loaders' combo options. **Five** files: the LTX 2.5 transformer, the
  video VAE, the audio VAE, the Gemma CLIP and the latent spatial upscaler;
* the container extensions the adapter refuses on equal the ones ``VHS_LoadVideoPath.video``
  declares, so the local refusal is the node's rule rather than a list this project invented;
* every ceiling the adapter refuses above equals the one the receiving input declares, so a
  bound that moves upstream is reported here instead of turning a local refusal into a lie;
* **the dependency list is the reachable subgraph's, not the node list's** — with the twist
  that makes this graph the odd one out. In the enhancer and audio-replacer exports
  ``LatentUpscaleModelLoader`` is an *orphan* and declaring it would make the pre-flight refuse
  a capable machine. Here it is genuinely reached, and dropping it by analogy would leave the
  adapter short a model it loads. The walk decides, not the habit — and this file asserts both
  halves against live evidence: that the audited export has no orphans at all (the reason it,
  rather than its no-audio sibling, is the one reproduced) and that the sibling's eleven are
  found by the same walk.

A fifth check earns its place from the substitutions: five node classes in the export's
reachable subgraph are not in the payload, and every one of them is confirmed *registered*.
Each was replaced because its schema shape hides values from this pre-flight, never because it
was missing, and an unaudited claim of "for schema-visibility" is indistinguishable from a
workaround for an uninstalled node.

``--record`` merges the audited classes into ``tests/fixtures/object_info.json`` only when the
audit found zero problems, keeping every class already recorded there.

**No generation is submitted.** ``/object_info`` is the only endpoint read. Nothing here
measures a frame count, and nothing here may: what this graph does to a take's length is a live
measurement, not a prediction — see ``build_ltx25_extend_payload``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from preflight import combo_options, numeric_bounds, parse_arguments, repo_src_on_path, run_audit

repo_src_on_path()

# Imported after `repo_src_on_path()` on purpose: run as a script, `src` is not
# importable until that call puts it on the path.
from music_video_producer.workflows import (
    LTX25_EXTEND_MAX_DIMENSION,
    LTX25_EXTEND_MAX_FRAME_RATE,
    LTX25_EXTEND_MAX_MASK_SECONDS,
    LTX25_EXTEND_MAX_RANGE_FRAMES,
    LTX25_EXTEND_MAX_SEED,
    LTX25_EXTEND_SOURCE_EXTENSIONS,
    build_ltx25_extend_payload,
    reachable_node_ids,
)

REFERENCE_EXPORTS = Path(__file__).resolve().parents[1] / "workflow_templates" / "reference_exports"

#: The audited evidence this adapter reproduces, and the one node ComfyUI would execute
#: backwards from.
EXPORT_PATH = REFERENCE_EXPORTS / "ltx25-videoextender-user-export.json"

#: Its silent sibling, audited here only as the counter-example: it is *not* built, and the
#: reachability walk is shown discriminating against it rather than being trusted to.
NOAUDIO_EXPORT_PATH = REFERENCE_EXPORTS / "ltx25-videoextender-noaudio-user-export.json"

#: Both exports save through a node with this id, which is how they were cut from one parent.
EXPORT_OUTPUT_NODE = "1994"

#: A path shaped like the take this adapter is given: an absolute file under ComfyUI's output
#: directory, in a container the node reads. Nothing opens it — `/object_info` does not touch
#: the filesystem — so it does not need to exist.
AUDIT_SOURCE = "J:/comfy/output/music-video-producer/preflight/shot-h3-reference_00001-audio.mp4"

#: Filename suffixes that make a payload string a model file. The same set
#: ``preflight_h3_ultra.model_files`` and ``preflight_ltx25_enhance.model_files`` use.
MODEL_SUFFIXES = (".safetensors", ".ckpt", ".pt", ".pth")


def audit_payloads() -> list[tuple[str, dict]]:
    """The two variants under audit. Two, for the reason in the module docstring."""
    common = {"source_video": AUDIT_SOURCE, "prefix": "music-video-producer/preflight/shot-extend"}
    return [
        ("ltx25-extend", build_ltx25_extend_payload(**common, extend_seconds=10)),
        (
            # A fractional length as well as the silent shape, so the seconds-to-seconds
            # arithmetic is audited on a value the export never carried.
            "ltx25-extend-silent",
            build_ltx25_extend_payload(**common, extend_seconds=4.5, include_audio=False),
        ),
    ]


def model_files(variants: list[tuple[str, dict]]) -> set[tuple[str, str, str]]:
    """Every ``(class, input, filename)`` the audited payloads actually load.

    Read out of the payloads rather than restated as a literal list, exactly as the H3 and
    enhancement audits do, and here it carries the same second guarantee for free: a payload
    *is* the reachable subgraph, because a payload is what gets submitted, so a list derived
    from it cannot pick up an orphaned loader the way a list derived from an export's nodes
    would.
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

    Recursive rather than a scan of the top level, for the reason
    ``preflight_ltx25_enhance.nested_model_files`` records: ``Power Lora Loader (rgthree)``
    keeps its filename one level down in a widget dict. This export's rgthree node holds no
    row at all, which is exactly why the adapter drops it — and a reader that could not see
    inside would have no way to tell an empty one from a loaded one.
    """
    if isinstance(value, str):
        return {value} if value.endswith(MODEL_SUFFIXES) else set()
    if isinstance(value, dict):
        return {name for item in value.values() for name in nested_model_files(item)}
    if isinstance(value, list):
        return {name for item in value for name in nested_model_files(item)}
    return set()


def export_graph(path: Path = EXPORT_PATH) -> dict:
    """One audited export, read fresh. Never written: these files are immutable evidence."""
    return json.loads(path.read_text(encoding="utf-8"))


def export_model_files(node_ids: set[str] | None = None, *, path: Path = EXPORT_PATH) -> set[str]:
    """The model filenames an audited export names, optionally within ``node_ids``."""
    export = export_graph(path)
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
    purpose: the audit's headline claim is that these five files are present, and it names them
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

    Equality rather than containment, for the reason
    ``preflight_ltx25_enhance.check_source_extensions`` records: the adapter is not choosing a
    narrower policy, it is restating the node's list so a take in an unreadable container is
    named locally instead of arriving as an opaque ``/prompt`` rejection.
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
    if sorted(declared) != sorted(LTX25_EXTEND_SOURCE_EXTENSIONS):
        return [
            (
                f"extensions: the adapter reads {sorted(LTX25_EXTEND_SOURCE_EXTENSIONS)} but "
                f"VHS_LoadVideoPath.video declares {sorted(declared)}"
            )
        ]
    return []


#: Each ceiling the adapter refuses above, against the input that has to accept the value.
#: ``(constant name, value, class, section, input)``. A tuple rather than five hand-written
#: comparisons so a constant added to the adapter without a line here is visible as a
#: difference in length, which ``tests/test_workflows.py`` asserts.
DECLARED_LIMITS = (
    ("LTX25_EXTEND_MAX_FRAME_RATE", LTX25_EXTEND_MAX_FRAME_RATE, "VHS_LoadVideoPath", "required", "force_rate"),
    ("LTX25_EXTEND_MAX_RANGE_FRAMES", LTX25_EXTEND_MAX_RANGE_FRAMES, "GetImageRangeFromBatch", "required", "num_frames"),
    ("LTX25_EXTEND_MAX_MASK_SECONDS", LTX25_EXTEND_MAX_MASK_SECONDS, "LTXVAudioVideoMask", "required", "video_end_time"),
    ("LTX25_EXTEND_MAX_DIMENSION", LTX25_EXTEND_MAX_DIMENSION, "ImageResizeKJv2", "required", "width"),
    ("LTX25_EXTEND_MAX_DIMENSION", LTX25_EXTEND_MAX_DIMENSION, "ImageResizeKJv2", "required", "height"),
    ("LTX25_EXTEND_MAX_SEED", LTX25_EXTEND_MAX_SEED, "RandomNoise", "required", "noise_seed"),
)


def check_declared_limits(object_info: dict) -> list[str]:
    """Every restated ceiling against the schema that declares it.

    The adapter refuses locally so a caller sees the number rather than an opaque 502 from
    ``/prompt`` validation after the submission round-trip. That is only worth doing while the
    restated number is the node's own: a ceiling that moved upstream would turn a helpful
    refusal into a wrong one, and a ceiling that *vanished* would leave the refusal checked
    against nothing at all — so an input publishing no maximum is a problem here, not a pass.
    """
    problems: list[str] = []
    for name, value, class_type, section, input_name in DECLARED_LIMITS:
        spec = object_info.get(class_type, {}).get("input", {}).get(section, {}).get(input_name)
        _, maximum = numeric_bounds(spec)
        if maximum is None:
            problems.append(
                f"limits: {class_type}.{input_name} publishes no maximum, so {name} is "
                f"checked against nothing"
            )
        elif maximum != value:
            problems.append(
                f"limits: {name} is {value!r} but {class_type}.{input_name} declares "
                f"{maximum!r}"
            )
    return problems


def substituted_classes() -> set[str]:
    """Classes the export's reachable subgraph names that the payload does not build."""
    export = export_graph()
    reachable = reachable_node_ids(export, [EXPORT_OUTPUT_NODE])
    built = {node["class_type"] for _, payload in audit_payloads() for node in payload.values()}
    return {export[node_id]["class_type"] for node_id in reachable} - built


def check_substitutions_are_for_schema_shape_not_absence(object_info: dict) -> list[str]:
    """Every class the adapter replaced is installed on this server.

    ``build_ltx25_extend_payload`` says each substitution is about a schema shape this
    pre-flight cannot read — an autogrow group keyed by ``names``, a dynamic combo's
    sub-inputs, a client-side widget row — and never about a node that is not there. That
    distinction is invisible from the payload: a graph built around a missing node looks
    exactly like a graph built around an awkward one. So the replaced classes are derived from
    the export rather than listed here, and each is confirmed registered.
    """
    substituted = substituted_classes()
    if not substituted:
        # Either the payload became a transcription or the derivation broke. Either way this
        # check has stopped proving anything, and a silently vacuous check is what the whole
        # pre-flight opposes.
        return [
            (
                "substitutions: the payload builds every class the export's reachable subgraph "
                "names, so nothing here confirms a substitution was a choice"
            )
        ]
    return [
        f"substitutions: {class_type} is not registered, so replacing it was not a choice "
        f"about schema shape"
        for class_type in sorted(substituted)
        if class_type not in object_info
    ]


def check_dependencies_come_from_the_reachable_subgraph(object_info: dict) -> list[str]:
    """The audited claim this whole file was written around.

    ``object_info`` is deliberately unused: this compares the adapter against the *exports*,
    and the failure it guards is one a live schema cannot see.

    Three things are asserted, and the first is why this export is the one reproduced:

    * the audited export has **no** orphans. Every node is reachable from its saver, so
      nothing about which nodes to build is a judgement call;
    * the payload loads exactly the model files that reachable subgraph loads — five, and the
      fifth is ``LatentUpscaleModelLoader``, which the enhancement and audio-replacement
      adapters both correctly *refuse* to declare because it is an orphan in their exports.
      Applied as a habit rather than as a walk, that refusal would drop a model this graph
      genuinely loads;
    * the no-audio sibling *does* have orphans, and the same walk finds them. Without that,
      "the walk discriminates" would be a property nothing here demonstrates on the one pair
      of graphs where it visibly matters.
    """
    export = export_graph()
    if EXPORT_OUTPUT_NODE not in export:
        return [f"reachability: the audited export has no node {EXPORT_OUTPUT_NODE} to run from"]
    problems: list[str] = []
    reachable = reachable_node_ids(export, [EXPORT_OUTPUT_NODE])
    orphaned = set(export) - reachable
    if orphaned:
        problems.append(
            f"reachability: the audited export has grown orphans "
            f"({', '.join(sorted(orphaned))}), so the choice of this export over its no-audio "
            f"sibling no longer rests on it having none"
        )
    loaded = {filename for _, _, filename in model_files(audit_payloads())}
    expected = export_model_files(reachable)
    if loaded != expected:
        problems.append(
            f"reachability: the payload loads {sorted(loaded)} but the export's reachable "
            f"subgraph loads {sorted(expected)}"
        )
    sibling = export_graph(NOAUDIO_EXPORT_PATH)
    if EXPORT_OUTPUT_NODE not in sibling:
        problems.append(
            f"reachability: the no-audio export has no node {EXPORT_OUTPUT_NODE} to run from"
        )
        return problems
    sibling_orphans = set(sibling) - reachable_node_ids(sibling, [EXPORT_OUTPUT_NODE])
    if not sibling_orphans:
        problems.append(
            "reachability: the no-audio export has no orphaned nodes either, so nothing here "
            "distinguishes a dependency list built from a node list"
        )
    return problems


#: Every check this audit runs. Named as one tuple so a test can assert the audit wires all of
#: them: a check deleted from here is a check that still passes its own unit test while the
#: live audit stops performing it.
CHECKS = (
    check_model_files,
    check_source_extensions,
    check_declared_limits,
    check_substitutions_are_for_schema_shape_not_absence,
    check_dependencies_come_from_the_reachable_subgraph,
)


def main() -> None:
    base_url, record = parse_arguments(sys.argv[1:])
    run_audit(
        audit_payloads(),
        base_url=base_url,
        record=record,
        checks=CHECKS,
        # The classes the payload replaced. In no payload, read by a check, and therefore
        # invisible to the offline half of the suite unless they are recorded — the reasoning
        # `run_audit`'s own docstring gives for `ResolutionSelector`.
        extra_classes=sorted(substituted_classes()),
    )


if __name__ == "__main__":
    main()
