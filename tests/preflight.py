"""Shared ``/object_info`` validation for every adapter pre-flight.

Lifted out of ``preflight_songplanner.py`` so a second adapter does not need a
second copy of the rules. Both audits — and the offline fixture checks in
``tests/test_workflows.py`` — call the functions here, so a schema shape learned
once is understood everywhere.

What an audit checks, per payload variant:

* every node class is registered on the live server;
* every input name the payload feeds exists in the class schema, after the two
  expansions below;
* every schema-required input is fed (literal or ``[node, output]`` link);
* every link points at a node that exists in the same payload, at an output index
  that node actually publishes;
* every combo-backed string value — model files included — appears in the combo
  options, read from ``input[1]["options"]`` (ComfyUI 0.33.1 V3 shape) with a
  fallback to the classic inline list at ``input[0]``;
* every numeric literal is finite, falls inside its schema ``min``/``max``, and is
  integral where the schema declares INT — and a string fed to a numeric input is
  reported rather than quietly skipped;
* optionally, that every numeric literal resolved at least one bound, so a bound
  that disappears upstream fails loudly instead of passing vacuously.

Two schema shapes need expanding before any of that is true of a graph richer
than SongPlanner's, and both were false failures until this module learned them:

``COMFY_AUTOGROW_V3`` — a *group* input such as ``MiniMaxH3ReferenceToVideo``'s
``ref_images`` publishes one template plus an index range rather than the slots
themselves. The payload feeds ``ref_images.ref_image_0``, which is not a schema
key, so a literal name comparison reports every attached reference as an input
that does not exist. ``expand_schema`` materialises the ``prefix``+index slots
the template describes and range-checks the index against its ``max``.

Format-conditional inputs — ``VHS_VideoCombine`` publishes ``crf``, ``pix_fmt``,
``save_metadata`` and ``trim_to_audio`` under ``format``'s options dict, keyed by
the selected format, because a different container takes different switches. A
name check that reads only ``required``/``optional`` reports all four as absent,
and ``crf`` resolves no bounds, so its 0–100 range goes unchecked. ``expand_schema``
merges the selected format's entries, which is also what makes the range check
real for them.

Anything this module cannot read is reported as a problem rather than skipped:
an unreadable autogrow template, a combo option it cannot resolve to a string, a
conditional selector fed as a link instead of a literal, and a conditional name
colliding with a declared input all fail the audit. Silence there is how a
validator goes inert without anyone noticing — which is the failure mode the
whole pre-flight exists to prevent.

Nothing here submits a graph: ``/object_info`` is the only endpoint touched.
"""

from __future__ import annotations

import json
import math
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, NamedTuple

#: The one recorded schema subset both audits merge into and the offline tests read.
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "object_info.json"

DEFAULT_BASE_URL = "http://127.0.0.1:8188"

#: ComfyUI's declared type for a group input that grows a numbered slot per connection.
AUTOGROW_TYPE = "COMFY_AUTOGROW_V3"

#: Declared types whose value must be a number. A string fed to one of these is not a
#: combo value the options check would catch — it is a type error ComfyUI rejects.
NUMERIC_TYPES = frozenset({"INT", "FLOAT"})


def combo_options(spec: object) -> list | None:
    """Combo options from an ``/object_info`` input spec.

    ComfyUI 0.33.1 V3 nodes carry options in ``input[1]["options"]`` (dynamic
    combos such as SaveAudioAdvanced ``format`` list dicts keyed by ``key``),
    while classic nodes still inline the option list at ``input[0]``. Reading
    only the old shape silently reports every model as missing, so prefer the
    0.33.1 shape and fall back to the classic one.

    An option this cannot resolve to a value — a dict with no ``key`` — becomes
    ``None`` in the returned list rather than raising. ``validate`` reads that as
    "the option list is unreadable" and says so, instead of blaming the payload
    for a value it could not have matched.
    """
    if not isinstance(spec, list) or not spec:
        return None
    if len(spec) > 1 and isinstance(spec[1], dict) and isinstance(spec[1].get("options"), list):
        return [
            item.get("key") if isinstance(item, dict) else item for item in spec[1]["options"]
        ]
    if isinstance(spec[0], list):
        return spec[0]
    return None


def declared_type(spec: object) -> str | None:
    """The declared type name (``"INT"``, ``"FLOAT"``, ``"STRING"``, …) or None.

    A combo input carries a list of options at ``spec[0]`` instead of a type name,
    so only a string there is a declared type. Range-checking without this cannot
    tell ``steps=1.5`` from ``steps=1``: both sit inside the schema's 1–10000, but
    the first is not an integer and the node declares INT.
    """
    if isinstance(spec, list) and spec and isinstance(spec[0], str):
        return spec[0]
    return None


def numeric_bounds(spec: object) -> tuple[float | None, float | None]:
    """``(min, max)`` from an ``/object_info`` INT/FLOAT input spec.

    ComfyUI carries numeric constraints in the options dict at ``input[1]`` —
    the same slot V3 combos use for their option list — and rejects any value
    outside them at ``/prompt`` validation time (``value_smaller_than_min`` /
    ``value_bigger_than_max``) before a single node executes. Either bound may
    be absent, in which case that side is unconstrained. ``step`` is
    deliberately not read: ComfyUI accepts off-step values, so enforcing it
    here would invent a constraint the server does not have.
    """
    if not isinstance(spec, list) or len(spec) < 2 or not isinstance(spec[1], dict):
        return (None, None)
    bounds: list[float | None] = []
    for key in ("min", "max"):
        value = spec[1].get(key)
        bounds.append(
            value
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            else None
        )
    return (bounds[0], bounds[1])


class AutogrowTemplate(NamedTuple):
    """One ``COMFY_AUTOGROW_V3`` group's slot naming and how many slots it offers."""

    prefix: str
    minimum: int
    maximum: int
    slot_spec: object


def autogrow_template(spec: object) -> AutogrowTemplate | None:
    """The group's ``(prefix, min, max, slot spec)``, or None if it is not a readable group.

    The group publishes one template — ``{"input": {"required": {<name>: <spec>}},
    "prefix": ..., "min": ..., "max": ...}`` — and the graph feeds
    ``<group>.<prefix><index>`` for each connected slot. ``max`` is the slot
    *count*, so the valid indices are ``0 … max - 1``: ``ref_images`` with
    ``max: 9`` accepts ``ref_image_0`` through ``ref_image_8``, which is exactly
    the range the audited user export uses. ``min`` is the count that must be fed;
    it is ``0`` on every group this project feeds, and reported rather than
    assumed so a group that starts demanding one cannot pass silently.

    Returning None for a group whose template cannot be read is not "not a group":
    ``expand_schema`` reports that separately, because a group whose template
    stopped parsing would otherwise turn every fed slot into a false failure.
    """
    if declared_type(spec) != AUTOGROW_TYPE:
        return None
    options = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}  # type: ignore[index]
    template = options.get("template")
    if not isinstance(template, dict):
        return None
    prefix = template.get("prefix")
    maximum = template.get("max")
    minimum = template.get("min", 0)
    slots = template.get("input", {}).get("required", {}) if isinstance(
        template.get("input"), dict
    ) else {}
    if not isinstance(prefix, str) or not prefix:
        return None
    for value in (minimum, maximum):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
    slot_spec = next(iter(slots.values()), None) if isinstance(slots, dict) else None
    return AutogrowTemplate(prefix, minimum, maximum, slot_spec)


def format_conditional_specs(spec: object, selected: object) -> dict[str, Any]:
    """The extra inputs a combo's *selected* option brings with it.

    ``VHS_VideoCombine.format`` carries ``input[1]["formats"]``, a mapping from
    each format to a list of ``[name, *input_spec]`` entries: ``video/h264-mp4``
    adds ``pix_fmt``, ``crf``, ``save_metadata`` and ``trim_to_audio``, while
    ``video/ffv1-mkv`` adds ``level``, ``coder`` and others instead. Only the
    selected format's entries are real inputs, which is why the payload's own
    value decides. ``entry[1:]`` is an ordinary input spec, so the combo, type
    and bounds helpers above read it unchanged.
    """
    if not conditional_formats(spec) or not isinstance(selected, str):
        return {}
    entries = spec[1]["formats"].get(selected)  # type: ignore[index]
    if not isinstance(entries, list):
        return {}
    return {
        entry[0]: list(entry[1:])
        for entry in entries
        if isinstance(entry, list) and entry and isinstance(entry[0], str)
    }


def conditional_formats(spec: object) -> dict | None:
    """The ``{option: [extra inputs]}`` mapping an input publishes, if it has one."""
    if not isinstance(spec, list) or len(spec) < 2 or not isinstance(spec[1], dict):
        return None
    formats = spec[1].get("formats")
    return formats if isinstance(formats, dict) else None


@dataclass
class NodeSchema:
    """What one node's payload may feed, must feed, and what could not be read.

    ``problems`` carries schema-reading failures — an unreadable autogrow template,
    a conditional selector fed as a link, a conditional name colliding with a
    declared input. They are problems rather than silent skips because each one
    turns a check off, and a check that turns itself off is indistinguishable from
    a clean audit.
    """

    specs: dict[str, Any] = field(default_factory=dict)
    required: set[str] = field(default_factory=set)
    groups: dict[str, AutogrowTemplate] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def expand_schema(info: dict, node_inputs: dict[str, Any]) -> NodeSchema:
    """One node's schema with both expandable shapes materialised.

    The required set drops the autogrow group names, which are never fed under
    their own name, and never gains a format-conditional input, which always has
    a default.
    """
    required = info.get("input", {}).get("required", {})
    optional = info.get("input", {}).get("optional", {})
    schema = NodeSchema(required=set(required))
    for name, spec in {**required, **optional}.items():
        if declared_type(spec) == AUTOGROW_TYPE:
            schema.required.discard(name)
            template = autogrow_template(spec)
            if template is None:
                schema.problems.append(
                    f"autogrow group {name!r} publishes a template this audit cannot read, "
                    f"so its slots are not checked"
                )
                continue
            schema.groups[name] = template
            if template.maximum == 0:
                schema.problems.append(f"autogrow group {name!r} offers no slots at all")
            for index in range(template.maximum):
                schema.specs[f"{name}.{template.prefix}{index}"] = template.slot_spec
            continue
        schema.specs[name] = spec
        if conditional_formats(spec) is None:
            continue
        selected = node_inputs.get(name)
        if not isinstance(selected, str):
            # A link or an omitted selector leaves every conditional input unknown, which
            # reads exactly like the false failures this expansion exists to remove.
            schema.problems.append(
                f"input {name!r} selects conditional inputs, but the payload feeds "
                f"{selected!r} rather than a literal option, so they cannot be checked"
            )
            continue
        for conditional, conditional_spec in format_conditional_specs(spec, selected).items():
            if conditional in schema.specs:
                # Dict order would otherwise decide which spec wins, silently.
                schema.problems.append(
                    f"input {conditional!r} is declared by the node *and* by {name}="
                    f"{selected!r}; the declared one is used"
                )
                continue
            schema.specs[conditional] = conditional_spec
    return schema


def autogrow_slot(name: str, groups: dict[str, AutogrowTemplate]) -> tuple[str, int, int] | None:
    """``(group, index, max)`` for an input name that reads as an out-of-range slot.

    Only reached for a name ``expand_schema`` did not materialise, so an index it
    parses is one past the template's range — the failure worth naming as such
    rather than as an input that does not exist.
    """
    group, separator, slot = name.partition(".")
    if not separator or group not in groups:
        return None
    template = groups[group]
    if not slot.startswith(template.prefix):
        return None
    index = slot[len(template.prefix):]
    if not index.isdigit():
        return None
    return group, int(index), template.maximum


def link_target(value: object) -> tuple[str, int] | None:
    """``(node id, output index)`` if this input value is a ``[node, output]`` link."""
    if (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
    ):
        return value[0], value[1]
    return None


def _link_problems(
    where: str, name: str, target: tuple[str, int], payload: dict, object_info: dict
) -> list[str]:
    """A link's destination, checked against the payload and the target's schema.

    The error class the whole pre-flight exists to catch is a graph that is wrong in a
    way ComfyUI only discovers after the submission. A link to a node id that is not in
    the payload is rejected at ``/prompt`` validation; a link to an output index the
    target does not publish is worse — where the index merely lands on the wrong output
    of the right node, ComfyUI runs the graph and the model is handed someone else's
    media, which costs the whole render to discover.
    """
    node_id, index = target
    if node_id not in payload:
        return [f"{where}: {name} links to node {node_id!r}, which is not in this payload"]
    class_type = payload[node_id].get("class_type")
    info = object_info.get(class_type)
    if info is None:
        return []  # The missing class is already reported where that node is checked.
    outputs = info.get("output")
    if not isinstance(outputs, list):
        return [f"{where}: {name} links to {class_type}, which publishes no output list"]
    if not 0 <= index < len(outputs):
        return [
            (
                f"{where}: {name} links to output {index} of {node_id} ({class_type}), "
                f"which publishes {len(outputs)} output(s)"
            )
        ]
    return []


def validate(
    label: str, payload: dict[str, dict], object_info: dict, *, graph: dict[str, dict] | None = None
) -> list[str]:
    """Every problem in ``payload``, checked against ``object_info``.

    ``graph`` is the whole submitted graph when ``payload`` is a subset of it — the
    offline fixture tests check only the nodes whose classes are recorded, and a link
    into a node they skipped is still a valid link. It defaults to ``payload``, which
    is the case every audit uses: a link out of the submitted graph is a defect.
    """
    graph = payload if graph is None else graph
    problems: list[str] = []
    for node_id, node in payload.items():
        class_type = node["class_type"]
        where = f"{label} payload, node {node_id} ({class_type})"
        info = object_info.get(class_type)
        if info is None:
            problems.append(f"{where}: class is not registered")
            continue
        schema = expand_schema(info, node["inputs"])
        problems += [f"{where}: {problem}" for problem in schema.problems]
        names = set(node["inputs"])
        for name in sorted(names - set(schema.specs)):
            slot = autogrow_slot(name, schema.groups)
            if slot is None:
                problems.append(f"{where}: input {name!r} does not exist in the schema")
                continue
            group, index, maximum = slot
            offered = f"0-{maximum - 1}" if maximum else "none"
            problems.append(
                f"{where}: input {name!r} is slot {index}, past {group}'s maximum of "
                f"{maximum} slots ({offered})"
            )
        for name in sorted(schema.required - names):
            problems.append(f"{where}: required input {name!r} is not fed")
        for group, template in sorted(schema.groups.items()):
            fed = sum(1 for name in names if name.startswith(f"{group}.{template.prefix}"))
            if fed < template.minimum:
                problems.append(
                    f"{where}: {group} requires at least {template.minimum} slot(s) but "
                    f"{fed} is fed"
                )
        for name, value in node["inputs"].items():
            spec = schema.specs.get(name)
            target = link_target(value)
            if target is not None:
                problems += _link_problems(where, name, target, graph, object_info)
                continue
            if isinstance(value, str):
                options = combo_options(spec)
                if options is not None and value not in options:
                    if None in options:
                        problems.append(
                            f"{where}: {name}={value!r} could not be checked — the combo "
                            f"options include an entry this audit cannot read"
                        )
                    else:
                        problems.append(f"{where}: {name}={value!r} not in combo options")
                elif options is None and declared_type(spec) in NUMERIC_TYPES:
                    problems.append(
                        f"{where}: {name}={value!r} is a string but the schema declares "
                        f"{declared_type(spec)}"
                    )
                continue
            # Bools are ints in Python but BOOLEAN in the schema, and anything else
            # non-numeric (a dict, a malformed list) carries no range to check.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if not math.isfinite(value):
                problems.append(f"{where}: {name}={value!r} is not a finite number")
                continue
            if declared_type(spec) == "INT" and value != int(value):
                problems.append(
                    f"{where}: {name}={value!r} is fractional but the schema declares INT"
                )
            minimum, maximum = numeric_bounds(spec)
            if minimum is not None and value < minimum:
                problems.append(f"{where}: {name}={value!r} is below the schema minimum {minimum!r}")
            if maximum is not None and value > maximum:
                problems.append(f"{where}: {name}={value!r} is above the schema maximum {maximum!r}")
    return problems


def unbounded_numeric_inputs(label: str, payload: dict[str, dict], object_info: dict) -> list[str]:
    """Numeric literals whose schema exposes neither ``min`` nor ``max``.

    ``numeric_bounds`` returning ``(None, None)`` makes the range check a silent
    no-op — and a bound that upstream removes or relocates looks exactly like a
    clean audit. Every numeric input of both SongPlanner graphs and of both H3
    graphs resolves at least one bound today, so reporting the ones that do not
    turns that failure mode loud instead of vacuous. Kept out of ``validate()``
    because an unbounded INT is a gap in this guard's coverage, not a defect in the
    payload: other adapters may legitimately feed inputs ComfyUI leaves
    unconstrained.
    """
    gaps: list[str] = []
    for node_id, node in payload.items():
        class_type = node["class_type"]
        info = object_info.get(class_type)
        if info is None:
            continue
        specs = expand_schema(info, node["inputs"]).specs
        for name, value in node["inputs"].items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if numeric_bounds(specs.get(name)) == (None, None):
                gaps.append(
                    f"{label} payload, node {node_id} ({class_type}): {name} resolved no "
                    f"min/max, so its value is not range-checked"
                )
    return gaps


class UsageError(SystemExit):
    """A bad command line. A `SystemExit` so a script exits non-zero with the message."""


def parse_arguments(argv: Sequence[str]) -> tuple[str, bool]:
    """``(base_url, record)`` from ``sys.argv[1:]``, the flag the audits share.

    A positional argument must look like a URL. Accepting anything meant a typo — a
    stray flag, a path, ``--recrod`` — became the base URL and the audit failed with
    a connection error naming a host nobody typed.
    """
    positional = [item for item in argv if item != "--record"]
    if len(positional) > 1 or (
        positional and not positional[0].startswith(("http://", "https://"))
    ):
        raise UsageError(
            f"usage: {Path(sys.argv[0]).name} [http://host:port] [--record]\n"
            f"unexpected argument(s): {' '.join(positional)}"
        )
    base_url = (
        positional[0] if positional else os.environ.get("MVP_COMFY_URL", DEFAULT_BASE_URL)
    ).rstrip("/")
    return base_url, "--record" in argv


def fetch_object_info(base_url: str) -> dict:
    """The live schema. Read-only: ``/object_info`` is the only endpoint touched."""
    with urllib.request.urlopen(f"{base_url}/object_info", timeout=60) as response:
        return json.load(response)


def read_fixture(path: Path = FIXTURE_PATH) -> dict:
    """The recorded schema subset, or ``{}`` if none is recorded yet.

    A truncated or half-written fixture fails here by name. Left to
    ``json.loads`` it surfaces as a bare ``JSONDecodeError`` from whichever test
    happened to read it first, which reads like a broken test rather than a
    broken file.
    """
    if not path.exists():
        return {}
    try:
        recorded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"recorded fixture {path} is not readable JSON: {error}") from error
    if not isinstance(recorded, dict):
        # ValueError rather than TypeError on purpose: every caller treats "this fixture is
        # unusable" as one condition, and the file being a list is a content problem, not a
        # caller passing the wrong type.
        raise ValueError(  # noqa: TRY004
            f"recorded fixture {path} is not an object of classes"
        )
    return recorded


class FixtureRecord(NamedTuple):
    """What one ``--record`` did: names added, names whose schema changed, the total."""

    added: list[str]
    changed: list[str]
    classes: list[str]


def record_fixture(
    object_info: dict, classes: Sequence[str], *, path: Path = FIXTURE_PATH
) -> FixtureRecord:
    """Merge ``classes`` into the recorded fixture, keeping every class already there.

    Merging rather than overwriting is the whole discipline: the fixture is shared
    by every adapter's offline checks, so a second audit recording its own subset
    would silently delete the first audit's coverage — and ``UNRECORDED_CLASSES``
    in ``tests/test_workflows.py``, the ledger of what is *not* covered, would go
    stale in the safe-looking direction.

    A class named here **is** rewritten from the live server, so ``changed`` is
    reported alongside ``added``: a bound that moved on an already-recorded class
    would otherwise be written in silently, and every offline test would quietly
    start agreeing with the new schema. Written through a temporary file in the
    same directory and then replaced, so an interrupted record cannot leave the
    shared fixture half-written.
    """
    existing = read_fixture(path)
    missing = [name for name in classes if name not in object_info]
    if missing:
        raise KeyError(f"not in /object_info: {', '.join(sorted(missing))}")
    merged = {**existing, **{name: object_info[name] for name in classes}}
    added = sorted(set(classes) - set(existing))
    changed = sorted(
        name for name in classes if name in existing and existing[name] != object_info[name]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(merged, indent=2, sort_keys=True) + "\n"
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    ) as temporary:
        temporary.write(serialized)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)
    return FixtureRecord(added, changed, sorted(merged))


def run_audit(
    variants: Sequence[tuple[str, dict]],
    *,
    base_url: str,
    record: bool,
    checks: Sequence[Callable[[dict], list[str]]] = (),
    extra_classes: Sequence[str] = (),
    fetch: Callable[[str], dict] | None = None,
    fixture_path: Path = FIXTURE_PATH,
) -> None:
    """Validate every variant against the live schema and, if clean, record it.

    ``checks`` are extra live-schema assertions an adapter's audit adds — a
    hardcoded limit against the schema that produced it, say. They are ordinary
    problems: a failing one fails the audit, and **a failing audit never records
    a fixture**, because a fixture recorded from a schema the payload does not
    satisfy would make every offline test agree with the defect.

    ``extra_classes`` are classes a ``check`` *reads* but no payload submits —
    ``ResolutionSelector``, whose arithmetic an adapter may reproduce locally rather
    than wire into a graph. Without them such a class is absent from the recorded
    fixture, and the offline half of the suite would find the check reporting "this
    node publishes nothing" against a fixture that simply never heard of it: a
    failure that looks like a real one and is not. Recorded like any other class, so
    a bound that moves upstream is still reported as ``changed``.

    ``fetch`` and ``fixture_path`` exist so this function is *executed by tests*
    rather than only by the two scripts. Everything below the fetch — the checks
    being folded in, the recording sitting after the failure guard — was
    unreachable from the suite until it could be driven with a stubbed server.
    """
    if not variants:
        print("FAIL: no payload variants to audit")
        raise SystemExit(1)
    try:
        object_info = (fetch or fetch_object_info)(base_url)
    except (urllib.error.URLError, OSError, ValueError) as error:
        print(f"FAIL: could not read {base_url}/object_info: {error}")
        raise SystemExit(1) from error
    problems = [
        problem for label, payload in variants for problem in validate(label, payload, object_info)
    ]
    # A bound that vanished upstream would leave the range check passing vacuously
    # against live ComfyUI, which is precisely the blind spot this audit closes.
    problems += [
        gap
        for label, payload in variants
        for gap in unbounded_numeric_inputs(label, payload, object_info)
    ]
    problems += [problem for check in checks for problem in check(object_info)]
    for problem in problems:
        print(f"FAIL {problem}")
    if problems:
        if record:
            print("Fixture NOT recorded: the audit found problems")
        raise SystemExit(1)
    classes = sorted(
        {node["class_type"] for _, payload in variants for node in payload.values()}
        | set(extra_classes)
    )
    if record:
        recorded = record_fixture(object_info, classes, path=fixture_path)
        print(
            f"Recorded {len(classes)} classes to {fixture_path} "
            f"({len(recorded.added)} new, {len(recorded.changed)} changed, "
            f"{len(recorded.classes)} in the fixture)"
        )
        for heading, names in (("new", recorded.added), ("changed", recorded.changed)):
            if names:
                print(f"  {heading}: {', '.join(names)}")
    total_nodes = sum(len(payload) for _, payload in variants)
    print(
        f"OK {total_nodes} nodes across {len(variants)} variants "
        f"({len(classes)} classes) validated against {base_url}"
    )


def repo_src_on_path() -> None:
    """Import the application from a script run out of ``tests/``."""
    source = Path(__file__).resolve().parents[1] / "src"
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
