"""Manual live audit of the SongPlanner adapter against ComfyUI ``/object_info``.

Not pytest-collected (like ``smoke_h3_app.py``), but its validation helpers are
imported by ``tests/test_workflows.py`` for offline fixture checks. Run from the
repo root with a live, user-managed ComfyUI (never started or stopped here):

    uv run python tests/preflight_songplanner.py [base_url] [--record]

Each payload variant is validated separately: every node class must be
registered, every payload input name must exist in the class schema, every
schema-required input must be fed (literal or ``[node, output]`` link), and
every combo-backed string value (model files included) must appear in the
combo options — read from ``input[1]["options"]`` (ComfyUI 0.33.1 V3 shape)
with a fallback to the classic inline list at ``input[0]``. ``--record``
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
    _build_songplanner_core,
    build_songplanner_invented_payload,
)

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "object_info.json"


def audit_payloads() -> list[tuple[str, dict]]:
    """The graphs under audit, validated separately so neither masks the other."""
    invented = build_songplanner_invented_payload(
        idea="preflight idea", genre_hint="", duration=120, seed=0, prefix="preflight"
    )
    known = _build_songplanner_core(
        idea="preflight idea",
        genre_hint="",
        duration=120,
        seed=0,
        prefix="preflight",
        lyrics="[verse]\npreflight",
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
            if not isinstance(value, str):
                continue
            options = combo_options(input_specs.get(name))
            if options is not None and value not in options:
                problems.append(f"{where}: {name}={value!r} not in combo options")
    return problems


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
