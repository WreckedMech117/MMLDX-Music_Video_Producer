"""Manual live audit of both MiniMax H3 adapters against ComfyUI ``/object_info``.

Not pytest-collected (like ``preflight_songplanner.py``, whose rules it shares
through ``tests/preflight.py``). This is the gate the H3 paths never had: it has
cost GPU minutes to discover a missing custom node or a renamed model file, and
every one of those is visible for free in the live schema. Run from the repo root
with a live, user-managed ComfyUI (never started or stopped here):

    uv run python tests/preflight_h3_ultra.py [base_url] [--record]

Ten payload variants are audited separately, chosen to reach every input either
adapter can emit: one picture; the full 9 pictures + 3 videos + 3 audios, which
fills every autogrow slot the graph offers; a video carrying its paired
soundtrack; ``ref_image_size="max"``; the longest window that still fits the
node's frame ceiling; and **both ends of every range the H3 request model
allows**, because a variant that only ever sends one safe midpoint would pass the
range check no matter where the bound moved. **Every sampling profile** is here:
eight default-profile variants and one variant per LoRA-carrying profile
(``turbo`` and ``turbo-references2v``). Those two are the only ones carrying a
``LoraLoaderModelOnly``, and each names a *different* LoRA file — so both files
being installed, and the class being registered, are confirmed before any GPU
time rather than discovered by a submission that fails. The text-only Director
graph is audited here too: it is the one H3 path with live render evidence, and
it was covered by nothing.

Beyond the shared per-node validation, six claims about the *adapters* are
checked against the live schema:

* the reference adapter's hardcoded per-kind limits equal the autogrow ``max`` of
  the groups they govern — a limit that says 9 while the node offers 8 refuses
  nothing and submits a reference into a slot that does not exist;
* every ``mvp:split`` output index it wires lands on the splitter output the
  schema names for that kind, so a picture cannot be fed where an audio belongs;
* both adapters' frame ceilings equal the maxima their nodes declare;
* the strength range a sampling profile may declare equals the one
  ``LoraLoaderModelOnly.strength_model`` declares, so the refusal a profile gets
  at definition time is the node's rule rather than a number this project invented;
* the model files the payloads actually name — read out of the payloads, never
  restated here, so each profile's LoRA joins the list by being *loaded* rather
  than by being mentioned — are present in their loaders' combo options;
* every bound ``H3Request`` enforces sits inside the bound the node enforces, so
  a request the route accepts can never be refused by ComfyUI.

``--record`` merges the audited classes into ``tests/fixtures/object_info.json``
only when the audit found zero problems, keeping every class already recorded
there. No generation is submitted: ``/object_info`` is the only endpoint read.
"""

from __future__ import annotations

import sys

from preflight import combo_options, numeric_bounds, parse_arguments, repo_src_on_path, run_audit

repo_src_on_path()

# Imported after `repo_src_on_path()` on purpose: run as a script, `src` is not
# importable until that call puts it on the path.
from music_video_producer.app import H3Request
from music_video_producer.timeline import align_h3_frames
from music_video_producer.workflows import (
    H3_ASPECT_RATIOS,
    H3_DEFAULT_ASPECT_RATIO,
    H3_DEFAULT_MEGAPIXELS,
    H3_DEFAULT_MULTIPLE,
    H3_DIRECTOR_MAX_FRAMES,
    H3_DIRECTOR_MAX_SECONDS,
    H3_FRAME_RATE,
    H3_LORA_STRENGTH_LIMITS,
    H3_REFERENCE_LIMITS,
    H3_REFERENCE_MAX_FRAMES,
    H3_SPLIT_OFFSETS,
    build_h3_director_payload,
    build_h3_reference_payload,
    select_resolution,
)

#: The autogrow group each per-kind limit governs, and the splitter output prefix each kind's
#: wiring must land on. Both are the live schema's own names.
AUTOGROW_GROUPS = {"picture": "ref_images", "video": "ref_videos", "audio": "ref_audios"}
SPLIT_OUTPUT_PREFIXES = {
    "picture": "picture_",
    "video": "video_",
    "video_audio": "video_audio_",
    "audio": "audio_",
}

#: Each adapter's frame ceiling and the schema input that declares it.
FRAME_CEILINGS = (
    ("MiniMaxH3ReferenceToVideo", "length", H3_REFERENCE_MAX_FRAMES),
    ("MiniMaxH3DirectorCS", "duration_frames", H3_DIRECTOR_MAX_FRAMES),
    ("MiniMaxH3DirectorCS", "end_frame", H3_DIRECTOR_MAX_FRAMES),
    ("MiniMaxH3DirectorCS", "end_second", H3_DIRECTOR_MAX_SECONDS),
    ("MiniMaxH3DirectorCS", "start_second", H3_DIRECTOR_MAX_SECONDS),
)

#: Every ``H3Request`` bound and the node input that must be at least as permissive.
#: The route is deliberately *narrower* than the node — 2048 px against the node's 16384 —
#: so containment is the claim, not equality: a request the route accepts must never be one
#: ComfyUI then refuses.
REQUEST_BOUNDS = (
    ("width", "MiniMaxH3ReferenceToVideo", "width"),
    ("height", "MiniMaxH3ReferenceToVideo", "height"),
    ("steps", "BasicScheduler", "steps"),
    # The selector's own two numeric inputs. `H3Request` restates their ranges so an
    # out-of-range figure is a 422 rather than a ComfyUI validation failure seen as an opaque
    # 502; restated numbers are exactly what this audit exists to keep honest. `ResolutionSelector`
    # is never submitted — `select_resolution` reproduces its arithmetic locally — but its
    # declared ranges are still the ones the reproduction has to respect.
    ("megapixels", "ResolutionSelector", "megapixels"),
    ("multiple", "ResolutionSelector", "multiple"),
)

#: The longest window whose aligned frame count still fits `H3_REFERENCE_MAX_FRAMES`:
#: 3592 frames is 17 x 211 + 5, the last point on H3's grid at or below 3600.
LONGEST_WINDOW_SECONDS = 3592 / H3_FRAME_RATE


def request_bound(field: str, kind: str) -> float:
    """One `H3Request` numeric bound, read off the model rather than restated."""
    for item in H3Request.model_fields[field].metadata:
        if item.__class__.__name__ == kind:
            return getattr(item, kind.lower())
    raise AssertionError(f"H3Request.{field} has no {kind} bound")


def audit_payloads() -> list[tuple[str, dict]]:
    """The variants under audit, validated separately so none masks another."""
    pictures = [{"kind": "picture", "file": f"F:/refs/picture-{index}.png"} for index in range(9)]
    videos = [{"kind": "video", "file": f"F:/refs/video-{index}.mp4"} for index in range(3)]
    audios = [{"kind": "audio", "file": f"F:/refs/audio-{index}.flac"} for index in range(3)]
    paired = [
        {"kind": "video", "file": "F:/refs/paired.mp4", "has_audio": True, "audio_mode": "paired"}
    ]
    # No `steps`, on purpose. Every reference variant below takes its step count from the
    # profile it names, which is what the application submits when the Director does not
    # override it. A literal 20 here — the number this used to carry — would keep passing
    # if either profile's count moved, and the audit would quietly stop describing what is
    # actually sent. The two request-bound variants below *do* name a count, because their
    # subject is the bound rather than the profile.
    shared = {"width": 1280, "height": 720, "seed": 0, "prefix": "preflight"}
    # Both ends of every range the route can send. A single midpoint would satisfy the
    # schema's min/max whichever way either bound moved, which is a check in name only.
    smallest = {
        "width": int(request_bound("width", "Ge")),
        "height": int(request_bound("height", "Ge")),
        "steps": int(request_bound("steps", "Ge")),
        "seed": 0,
        "prefix": "preflight",
    }
    largest = {
        "width": int(request_bound("width", "Le")),
        "height": int(request_bound("height", "Le")),
        "steps": int(request_bound("steps", "Le")),
        "seed": 0,
        "prefix": "preflight",
    }
    director = {
        "timeline_data": '{"segments":[{"id":"s","start":0,"length":120,"prompt":"p"}]}',
        "seed": 0,
        "prefix": "preflight",
    }
    return [
        (
            "single-picture",
            build_h3_reference_payload(
                prompt="<Picture 1>", references=pictures[:1], duration=8, **shared
            ),
        ),
        (
            "every-slot",
            build_h3_reference_payload(
                prompt="<Picture 1> <Video 1> <Audio 1>",
                references=[*pictures, *videos, *audios],
                duration=8,
                **shared,
            ),
        ),
        (
            "paired-video-audio",
            build_h3_reference_payload(
                prompt="<Video 1>", references=paired, duration=8, **shared
            ),
        ),
        (
            "max-reference-size",
            build_h3_reference_payload(
                prompt="<Picture 1>",
                references=pictures[:1],
                duration=8,
                ref_image_size="max",
                **shared,
            ),
        ),
        (
            # The turbo bundle exactly as the Director renders it: the LoRA at its
            # strength, `beta`/`euler`, and no step count, so the profile supplies its
            # own 4. This is one of the two variants carrying a `LoraLoaderModelOnly`, and
            # it is what puts that class and this LoRA's filename in front of the live
            # schema.
            "turbo-profile",
            build_h3_reference_payload(
                prompt="<Picture 1>",
                references=pictures[:1],
                duration=8,
                profile="turbo",
                **shared,
            ),
        ),
        (
            # The canonical `MiniMaxH3Turbo References2V` bundle: a *different* LoRA file
            # at a different strength with a different scheduler and step count, so it
            # earns its own variant rather than being assumed covered by the one above —
            # a filename nothing loads is a filename nothing confirms installed. Nothing
            # has been rendered on this profile; auditing it is not evidence of a frame.
            "turbo-references2v-profile",
            build_h3_reference_payload(
                prompt="<Picture 1>",
                references=pictures[:1],
                duration=8,
                profile="turbo-references2v",
                **shared,
            ),
        ),
        (
            # What the application actually submits when the Director touches no geometry
            # field: `select_resolution`'s 0.6 MP / 16:9 / 32 frame, 1056x608. Named as its
            # own variant because every other variant here pins a size explicitly, and the
            # default is the one this project renders at — the size that was 640x384 in the
            # smoke and 1344x768 on the route, neither of which anybody chose.
            "default-geometry",
            build_h3_reference_payload(
                prompt="<Picture 1> <Audio 1>",
                # With the shot's own window on the song, which is the other thing this
                # project submits by default now. A `trim` the loader cannot read is dropped
                # silently, so putting one in front of the live schema is the only way the
                # audit sees it at all.
                references=[
                    *pictures[:1],
                    {
                        "kind": "audio",
                        "file": "F:/refs/master.mp3",
                        "label": "master song",
                        "trim": {"start": 12.0, "end": 15.75},
                    },
                ],
                duration=3.75,
                seed=0,
                prefix="preflight",
            ),
        ),
        (
            "smallest-request",
            build_h3_reference_payload(
                prompt="<Picture 1>",
                references=pictures[:1],
                duration=5 / H3_FRAME_RATE,
                **smallest,
            ),
        ),
        (
            "largest-request",
            build_h3_reference_payload(
                prompt="<Picture 1>",
                references=pictures[:1],
                duration=LONGEST_WINDOW_SECONDS,
                **largest,
            ),
        ),
        (
            "director-text-only",
            build_h3_director_payload(
                duration=5.0,
                requested_frames=align_h3_frames(5 * H3_FRAME_RATE),
                start=3.0,
                **{**largest, **director},
            ),
        ),
        (
            # The longest Director window the node's own caps allow: `end_frame` is
            # `end x 24`, so the 10000-frame ceiling binds at about 416 s, well before the
            # 1000 s one on `end_second`.
            "director-longest",
            build_h3_director_payload(
                duration=415.0,
                requested_frames=align_h3_frames(round(415 * H3_FRAME_RATE)),
                start=0.0,
                **{**smallest, **director},
            ),
        ),
    ]


def check_reference_limits(object_info: dict) -> list[str]:
    """The adapter's 9/3/3 against the autogrow maxima that produced them."""
    problems: list[str] = []
    schema = object_info.get("MiniMaxH3ReferenceToVideo", {}).get("input", {})
    # Read both halves: a group promoted from `optional` to `required` upstream is a schema
    # change this should notice, not one that should make it report a false failure.
    groups = {**schema.get("required", {}), **schema.get("optional", {})}
    for kind, limit in H3_REFERENCE_LIMITS.items():
        group = AUTOGROW_GROUPS[kind]
        template = groups.get(group, [None, {}])
        maximum = (
            template[1].get("template", {}).get("max")
            if len(template) > 1 and isinstance(template[1], dict)
            else None
        )
        if maximum != limit:
            problems.append(
                f"limits: the adapter accepts {limit} {kind}(s) but {group} offers "
                f"{maximum!r} slots"
            )
    # The paired-soundtrack group is not one the adapter counts separately — it is fed one entry
    # per reference video — so it has to offer at least as many slots as there are videos.
    paired = groups.get("ref_video_audios", [None, {}])
    paired_max = (
        paired[1].get("template", {}).get("max")
        if len(paired) > 1 and isinstance(paired[1], dict)
        else None
    )
    if paired_max != H3_REFERENCE_LIMITS["video"]:
        problems.append(
            f"limits: {H3_REFERENCE_LIMITS['video']} videos may each carry a soundtrack but "
            f"ref_video_audios offers {paired_max!r} slots"
        )
    return problems


def check_split_offsets(object_info: dict) -> list[str]:
    """Every wired splitter index against the output the schema names for that kind."""
    outputs = object_info.get("MiniMaxH3ReferenceSplitter", {}).get("output_name")
    if not isinstance(outputs, list):
        return ["splitter: MiniMaxH3ReferenceSplitter publishes no output_name list"]
    problems: list[str] = []
    for kind, offset in H3_SPLIT_OFFSETS.items():
        slots = H3_REFERENCE_LIMITS["video" if kind == "video_audio" else kind]
        for index in range(slots):
            expected = f"{SPLIT_OUTPUT_PREFIXES[kind]}{index + 1}"
            actual = outputs[offset + index] if offset + index < len(outputs) else None
            if actual != expected:
                problems.append(
                    f"splitter: the adapter wires {kind} {index} to output {offset + index}, "
                    f"which the schema names {actual!r} rather than {expected!r}"
                )
    return problems


def check_frame_ceilings(object_info: dict) -> list[str]:
    """Each adapter's refusal threshold against the maximum its node declares."""
    problems: list[str] = []
    for class_type, input_name, ceiling in FRAME_CEILINGS:
        spec = (
            object_info.get(class_type, {})
            .get("input", {})
            .get("required", {})
            .get(input_name)
        )
        maximum = numeric_bounds(spec)[1]
        if maximum != ceiling:
            problems.append(
                f"ceilings: the adapter refuses above {ceiling} but {class_type}.{input_name} "
                f"declares a maximum of {maximum!r}"
            )
    return problems


def model_files(variants: list[tuple[str, dict]]) -> set[tuple[str, str, str]]:
    """Every ``(class, input, filename)`` the audited payloads actually load.

    Read out of the payloads rather than restated as a literal list: a loader that
    was renamed, dropped or repointed would otherwise leave this audit cheerfully
    confirming that a file nothing loads is still installed.
    """
    return {
        (node["class_type"], name, value)
        for _, payload in variants
        for node in payload.values()
        for name, value in node["inputs"].items()
        if isinstance(value, str) and value.endswith((".safetensors", ".ckpt", ".pt", ".pth"))
    }


def check_lora_strength_range(object_info: dict) -> list[str]:
    """The strength range a profile may declare against the range the node declares.

    ``H3SamplingProfile`` refuses a strength outside ``H3_LORA_STRENGTH_LIMITS`` when the
    profile is *defined*, which is only worth anything while those limits are the node's
    own. Written as equality rather than containment because the adapter is not choosing a
    narrower policy here — it is restating the node's range, and a restated number that
    stops matching is exactly what this audit exists to catch.
    """
    spec = (
        object_info.get("LoraLoaderModelOnly", {})
        .get("input", {})
        .get("required", {})
        .get("strength_model")
    )
    declared = numeric_bounds(spec)
    if declared != H3_LORA_STRENGTH_LIMITS:
        return [
            (
                f"lora strength: a profile may declare {H3_LORA_STRENGTH_LIMITS} but "
                f"LoraLoaderModelOnly.strength_model declares {declared}"
            )
        ]
    return []


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


def check_request_bounds(object_info: dict) -> list[str]:
    """Every `H3Request` bound against the node bound it must sit inside."""
    problems: list[str] = []
    for field, class_type, input_name in REQUEST_BOUNDS:
        spec = (
            object_info.get(class_type, {}).get("input", {}).get("required", {}).get(input_name)
        )
        node_minimum, node_maximum = numeric_bounds(spec)
        if (node_minimum, node_maximum) == (None, None):
            problems.append(
                f"request bounds: {class_type}.{input_name} declares no min/max, so "
                f"H3Request.{field} is checked against nothing"
            )
            continue
        route_minimum = request_bound(field, "Ge")
        route_maximum = request_bound(field, "Le")
        if node_minimum is not None and route_minimum < node_minimum:
            problems.append(
                f"request bounds: H3Request.{field} accepts {route_minimum}, below "
                f"{class_type}.{input_name}'s minimum of {node_minimum}"
            )
        if node_maximum is not None and route_maximum > node_maximum:
            problems.append(
                f"request bounds: H3Request.{field} accepts {route_maximum}, above "
                f"{class_type}.{input_name}'s maximum of {node_maximum}"
            )
    return problems


def check_aspect_ratios(object_info: dict) -> list[str]:
    """The adapter's aspect table against the combo the live selector publishes.

    ``select_resolution`` reproduces ``ResolutionSelector``'s arithmetic locally rather than
    submitting the node, so nothing at ``/prompt`` would ever catch a ratio this table gets
    wrong — a renamed option or a transposed pair would simply render a differently shaped
    frame than the Director's graph does, forever, and correctly according to itself.

    Equality rather than containment: the adapter is restating the node's own option list,
    not choosing a subset of it.
    """
    spec = (
        object_info.get("ResolutionSelector", {})
        .get("input", {})
        .get("required", {})
        .get("aspect_ratio")
    )
    options = combo_options(spec)
    if options is None:
        return ["aspect ratios: ResolutionSelector.aspect_ratio publishes no combo options"]
    if set(options) != set(H3_ASPECT_RATIOS):
        missing = sorted(set(options) - set(H3_ASPECT_RATIOS))
        extra = sorted(set(H3_ASPECT_RATIOS) - set(options))
        return [
            (
                f"aspect ratios: the adapter's table and ResolutionSelector's options differ "
                f"— missing {missing}, unknown {extra}"
            )
        ]
    return []


def check_default_geometry(object_info: dict) -> list[str]:
    """The default frame against the node that has to accept it.

    Two claims, and the second is the one worth having. First, that 0.6 MP at 16:9 on a
    multiple of 32 still selects the 1056x608 the 2026-08-17 boundary run measured — a
    number this project reproduces rather than derives, so a change in the arithmetic must
    be seen rather than absorbed. Second, that the selected frame sits inside
    ``MiniMaxH3ReferenceToVideo``'s declared width/height range *and* on its declared step,
    because ComfyUI validates the min and max but not the step, so an off-grid frame is the
    one geometry error that would reach the GPU rather than the validator.
    """
    problems: list[str] = []
    width, height = select_resolution(
        megapixels=H3_DEFAULT_MEGAPIXELS,
        aspect_ratio=H3_DEFAULT_ASPECT_RATIO,
        multiple=H3_DEFAULT_MULTIPLE,
    )
    if (width, height) != (1056, 608):
        problems.append(
            f"default geometry: {H3_DEFAULT_MEGAPIXELS:g} MP at {H3_DEFAULT_ASPECT_RATIO} on "
            f"a multiple of {H3_DEFAULT_MULTIPLE} now selects {width}x{height}, not the "
            f"1056x608 measured from the Director's own pipeline"
        )
    for axis, value in (("width", width), ("height", height)):
        spec = (
            object_info.get("MiniMaxH3ReferenceToVideo", {})
            .get("input", {})
            .get("required", {})
            .get(axis)
        )
        minimum, maximum = numeric_bounds(spec)
        step = (spec[1] or {}).get("step") if isinstance(spec, list) and len(spec) > 1 else None
        if minimum is not None and value < minimum:
            problems.append(
                f"default geometry: {axis} {value} is below "
                f"MiniMaxH3ReferenceToVideo.{axis}'s minimum of {minimum}"
            )
        if maximum is not None and value > maximum:
            problems.append(
                f"default geometry: {axis} {value} is above "
                f"MiniMaxH3ReferenceToVideo.{axis}'s maximum of {maximum}"
            )
        if step and value % step:
            problems.append(
                f"default geometry: {axis} {value} is off "
                f"MiniMaxH3ReferenceToVideo.{axis}'s step of {step}, which ComfyUI does not "
                f"validate — this reaches the GPU rather than the validator"
            )
    return problems


#: Every check this audit runs. Named as one tuple so a test can assert the audit wires all
#: of them: a check deleted from here is a check that still passes its own unit test while
#: the live audit stops performing it.
CHECKS = (
    check_reference_limits,
    check_split_offsets,
    check_frame_ceilings,
    check_lora_strength_range,
    check_model_files,
    check_request_bounds,
    check_aspect_ratios,
    check_default_geometry,
)


#: Classes this audit *reads* but never submits. ``select_resolution`` reproduces
#: ``ResolutionSelector``'s arithmetic in Python rather than wiring the node into the graph —
#: the graph takes two integers — so the class appears in no payload, and without naming it
#: here its options and bounds would be checked live and nowhere else.
EXTRA_CLASSES = ("ResolutionSelector",)


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
