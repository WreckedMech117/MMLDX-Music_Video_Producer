"""Manual live audit of the SongPlanner adapter against ComfyUI ``/object_info``.

Not pytest-collected (like ``smoke_h3_app.py``), but its validation helpers are
imported by ``tests/test_workflows.py`` for offline fixture checks. Run from the
repo root with a live, user-managed ComfyUI (never started or stopped here):

    uv run python tests/preflight_songplanner.py [base_url] [--record]

Each payload variant is validated separately: every node class must be
registered, every payload input name must exist in the class schema, every
schema-required input must be fed (literal or ``[node, output]`` link), every
combo-backed string value (model files included) must appear in the combo
options — read from ``input[1]["options"]`` (ComfyUI 0.33.1 V3 shape) with a
fallback to the classic inline list at ``input[0]`` — every numeric literal must
fall inside its schema's ``min``/``max`` and be integral where the schema declares
INT, and every numeric literal must resolve at least one bound so a bound that
disappears upstream fails loudly instead of passing vacuously. ``--record``
writes the audited ``/object_info`` subset to
``tests/fixtures/object_info.json`` only when the audit found zero problems.
No generation is submitted.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from music_video_producer.workflows import (
    build_songplanner_invented_payload,
    build_songplanner_known_lyrics_payload,
)

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "object_info.json"


def audit_payloads() -> list[tuple[str, dict]]:
    """The graphs under audit, validated separately so neither masks the other."""
    invented = build_songplanner_invented_payload(
        idea="preflight idea", genre_hint="", duration=120, seed=0, prefix="preflight"
    )
    known = build_songplanner_known_lyrics_payload(
        idea="preflight idea",
        genre_hint="",
        lyrics="[verse]\npreflight",
        duration=120,
        seed=0,
        prefix="preflight",
    )
    return [("invented", invented), ("known-lyrics", known)]


def combo_options(spec: object) -> list | None:
    """Combo options from an ``/object_info`` input spec.

    ComfyUI 0.33.1 V3 nodes carry options in ``input[1]["options"]`` (dynamic
    combos such as SaveAudioAdvanced ``format`` list dicts keyed by ``key``),
    while classic nodes still inline the option list at ``input[0]``. Reading
    only the old shape silently reports every model as missing, so prefer the
    0.33.1 shape and fall back to the classic one.
    """
    if not isinstance(spec, list) or not spec:
        return None
    if len(spec) > 1 and isinstance(spec[1], dict) and isinstance(spec[1].get("options"), list):
        return [
            item["key"] if isinstance(item, dict) else item for item in spec[1]["options"]
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
            value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        )
    return (bounds[0], bounds[1])


def validate(label: str, payload: dict[str, dict], object_info: dict) -> list[str]:
    problems: list[str] = []
    for node_id, node in payload.items():
        class_type = node["class_type"]
        where = f"{label} payload, node {node_id} ({class_type})"
        info = object_info.get(class_type)
        if info is None:
            problems.append(f"{where}: class is not registered")
            continue
        required = info.get("input", {}).get("required", {})
        optional = info.get("input", {}).get("optional", {})
        input_specs = {**required, **optional}
        names = set(node["inputs"])
        for name in sorted(names - set(input_specs)):
            problems.append(f"{where}: input {name!r} does not exist in the schema")
        for name in sorted(set(required) - names):
            problems.append(f"{where}: required input {name!r} is not fed")
        for name, value in node["inputs"].items():
            spec = input_specs.get(name)
            if isinstance(value, str):
                options = combo_options(spec)
                if options is not None and value not in options:
                    problems.append(f"{where}: {name}={value!r} not in combo options")
                continue
            # ``[node, output]`` links carry no literal to check, and bools are
            # ints in Python but BOOLEAN in the schema — neither is a range.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
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
    clean audit. Every numeric input of both SongPlanner graphs resolves at least
    one bound today, so reporting the ones that do not turns that failure mode
    loud instead of vacuous. Kept out of ``validate()`` because an unbounded INT
    is a gap in this guard's coverage, not a defect in the payload: other adapters
    may legitimately feed inputs ComfyUI leaves unconstrained.
    """
    gaps: list[str] = []
    for node_id, node in payload.items():
        class_type = node["class_type"]
        info = object_info.get(class_type)
        if info is None:
            continue
        specs = {
            **info.get("input", {}).get("required", {}),
            **info.get("input", {}).get("optional", {}),
        }
        for name, value in node["inputs"].items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if numeric_bounds(specs.get(name)) == (None, None):
                gaps.append(
                    f"{label} payload, node {node_id} ({class_type}): {name} resolved no "
                    f"min/max, so its value is not range-checked"
                )
    return gaps


def main() -> None:
    arguments = [item for item in sys.argv[1:] if item != "--record"]
    record = "--record" in sys.argv[1:]
    base_url = (
        arguments[0] if arguments else os.environ.get("MVP_COMFY_URL", "http://127.0.0.1:8188")
    ).rstrip("/")
    try:
        with urllib.request.urlopen(f"{base_url}/object_info", timeout=60) as response:
            object_info = json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as error:
        print(f"FAIL: could not read {base_url}/object_info: {error}")
        raise SystemExit(1) from error
    variants = audit_payloads()
    problems = [
        problem
        for label, payload in variants
        for problem in validate(label, payload, object_info)
    ]
    # A bound that vanished upstream would leave the range check passing vacuously
    # against live ComfyUI, which is precisely the blind spot this audit closes.
    problems += [
        gap
        for label, payload in variants
        for gap in unbounded_numeric_inputs(label, payload, object_info)
    ]
    for problem in problems:
        print(f"FAIL {problem}")
    if problems:
        if record:
            print("Fixture NOT recorded: the audit found problems")
        raise SystemExit(1)
    classes = sorted(
        {node["class_type"] for _, payload in variants for node in payload.values()}
    )
    if record:
        subset = {name: object_info[name] for name in classes}
        FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE_PATH.write_text(
            json.dumps(subset, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"Recorded {len(subset)} classes to {FIXTURE_PATH}")
    total = sum(len(payload) for _, payload in variants)
    print(
        f"OK {total} nodes across {len(variants)} variants "
        f"({len(classes)} classes) validated against {base_url}"
    )


if __name__ == "__main__":
    main()
