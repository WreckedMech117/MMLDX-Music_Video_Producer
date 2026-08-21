"""Run 4 of the live LM Studio measurements: **was the empty-`shots` failure the schema?**

Runs 1–3 chased populate's `shots: []` failure through prompt wording, count enforcement,
a two-stage split and a second model, and measured no wording that fixed it: run 2's
single-call control returned `shots: []` on 1 of 3 rolls and delivered both halves on 0 of
9 rolls across two runs; run 3 (Gemma) returned zero shots on 8 of 8.

The cause was underneath all of it. `DirectorResult.model_json_schema()["required"]` is
``["message", "treatment", "style_bible"]`` — `shots` and `sections` both carry
``default_factory=list``, so Pydantic never marks them required — and that schema is what
rides ``response_format: json_schema strict`` into LM Studio's **constrained decoder**. A
reply with no `shots` was legal under the grammar it was decoded with. Every prompt in
front of it was asking for a field the schema said was optional.

This run measures whether promoting `shots` into `required` stops it.

    uv run python tests/smoke_populate_schema_live.py 0 3     # units 0-2
    uv run python tests/smoke_populate_schema_live.py summary

Not collected by pytest: `smoke_*.py` does not match `python_files`, exactly as the six
existing live smokes avoid collection. **Nothing is written into `data/`, no route is
called, and no ComfyUI work of any kind is submitted.** A throwaway `Project` is built in
memory, the shipped constants and the shipped `director_result_schema` are imported rather
than transcribed, and the only I/O besides the model calls is the evidence dropped under
`test-artifacts/2026-08-20-lmstudio-live/` with a `run4-` prefix.

Conditions are **run 2's and run 3's**, so the numbers land in the same table: the same
60.0 s song, the same `populate_required_shots(60)` = 12, the same `PLAN_TEMPERATURE` of
0.7, the same combined single-call ask assembled from the same shipped constants, N=3 per
arm, arms **interleaved** so session drift lands on all of them rather than on whichever
went first. The **count-enforcement wording is left in place on every arm** — one variable
at a time, and this run is changing the schema.

The arms, all four sending the identical request text and differing only in the strict
schema attached to it:

* **control** — `DirectorResult.model_json_schema()`, byte-for-byte what runs 1–3 sent and
  what the chat route still sends. Without it a clean treatment arm is indistinguishable
  from a lucky session, which is exactly what made run 2's null result trustworthy.
* **require_shots** — the shipped fix: `director_result_schema(require=("shots",))`.
* **require_both** — `require=("shots", "sections")`. Not shipped. It measures the *other*
  half of the recorded failure — both halves delivered on 0 of 9 rolls — and whether a
  grammar can force a model to answer two asks it has always chosen between.
* **min_items** — `require=("shots",)` plus ``minItems`` equal to the required count. A
  standalone probe already established that LM Studio's decoder **honours `minItems`**: a
  request asking in words for two shots against ``minItems: 12`` returned twelve. The same
  probe showed it does *not* enforce numeric bounds, so the forced entries arrived with
  ``duration: 0`` and ``duration: 1200`` and the whole reply failed `PlannedShot`
  validation. This arm asks the question that matters for shipping it: when the prompt and
  the floor agree on the number, is the answer still parseable?

A **warm-up call is made and discarded** before any measured unit, and is recorded in the
evidence as `discarded: true` so it is visible rather than merely claimed.

Each unit is one model call, appended to `run4-calls.jsonl` **as it lands**, so a harness
that dies halfway leaves every roll it completed. Units are addressed by index so the run
can be chunked under a tool timeout without changing what is measured.

Timeout: **900 s** per call, run 2's budget, kept so the numbers are comparable. The
shipped `DirectorClient` timeout is 300 s and that gap is a finding to hand back rather
than a thing to fix from here.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from music_video_producer.app import (
    DIRECTOR_CONTEXT_EXCLUDE,
    POPULATE_FINAL_CHECK,
    POPULATE_INSTRUCTION,
    POPULATE_SECTIONS_ASK,
    POPULATE_SECTIONS_CONSTRAINT,
    populate_required_shots,
)
from music_video_producer.config import Settings
from music_video_producer.director import (
    PLAN_TEMPERATURE,
    SYSTEM_PROMPT,
    DirectorClient,
    DirectorResult,
    director_result_schema,
    extract_json,
)
from music_video_producer.models import Asset, Project, Song

ARTIFACTS = Path(__file__).resolve().parents[1] / "test-artifacts" / "2026-08-20-lmstudio-live"
PREFIX = "run4-"
CALLS = ARTIFACTS / f"{PREFIX}calls.jsonl"

#: Runs 1–3's song, unchanged. `populate_required_shots(60)` is 12 shots.
SONG_SECONDS = 60.0

#: The scale probe's song. `populate_required_shots(180)` is 35 shots.
SCALE_SECONDS = 180.0

#: Rolls per arm in A. Three. Three settles little and the report has to say so.
ROLLS = 3

TIMEOUT_SECONDS = 900
SCALE_TIMEOUT_SECONDS = 1800

#: The arms of A, in the order each roll walks them.
ARMS = ("control", "require_shots", "require_both", "min_items")

#: The scale probe's arms. `require_both` is dropped there for budget, not for doubt.
SCALE_ARMS = ("control", "require_shots", "min_items")


def schema_for(arm: str, required: int) -> dict[str, Any]:
    """The strict schema each arm attaches to an identical request.

    Built from the shipped `director_result_schema` rather than transcribed, so an arm
    cannot measure a schema the application would not send.
    """
    if arm == "control":
        return DirectorResult.model_json_schema()
    if arm == "require_shots":
        return director_result_schema(require=("shots",))
    if arm == "require_both":
        return director_result_schema(require=("shots", "sections"))
    if arm == "min_items":
        return director_result_schema(require=("shots",), min_shots=required)
    raise ValueError(f"unknown arm {arm!r}")


def probe_project() -> Project:
    """Runs 1–3's project, byte-for-byte, so arm A's conditions match theirs."""
    project = Project(name="Calliope live smoke")
    project.creative_brief = "A night drive that opens out into wilderness."
    project.treatment = (
        "Three movements. The corridor: sodium-lit underpass, the singer alone at the wheel. "
        "The threshold: the car stops where the tarmac ends. The forest: she walks in and the "
        "city light drops away behind her."
    )
    project.style_bible = "Sodium amber, hard backlight, 35mm grain, handheld, shallow depth."
    project.song = Song(
        title="Signal Bloom",
        source="imported",
        duration=SONG_SECONDS,
        caption="Slow synth rock, 84 bpm",
        lyrics=(
            "[Intro]\n"
            "\n"
            "[Verse]\n"
            "Headlights bloom on the underpass wall\n"
            "I keep the radio low\n"
            "\n"
            "[Chorus]\n"
            "Signal bloom, carry me out\n"
            "Past where the streetlights go\n"
            "\n"
            "[Bridge]\n"
            "The tarmac ends and the dark begins\n"
            "\n"
            "[Outro]\n"
        ),
    )
    project.assets = [
        Asset(id="asset_lead", name="Mia", kind="character", path="media/mia.png"),
        Asset(id="asset_car", name="The grey estate car", kind="prop", path="media/car.png"),
        Asset(id="asset_underpass", name="Sodium underpass", kind="setting", path="media/u.png"),
        Asset(id="asset_forest", name="Pine forest at dusk", kind="setting", path="media/f.png"),
    ]
    return project


def scale_project() -> Project:
    """Run 2's scale project: the same video at real length — 35 shots, eight [Tag] blocks."""
    project = probe_project()
    project.song = Song(
        title="Signal Bloom (full)",
        source="imported",
        duration=SCALE_SECONDS,
        caption="Slow synth rock, 84 bpm",
        lyrics=(
            "[Intro]\n"
            "\n"
            "[Verse 1]\n"
            "Headlights bloom on the underpass wall\n"
            "I keep the radio low\n"
            "The wipers keep a time I never chose\n"
            "\n"
            "[Chorus]\n"
            "Signal bloom, carry me out\n"
            "Past where the streetlights go\n"
            "\n"
            "[Verse 2]\n"
            "The lane markers stutter and thin\n"
            "There is nobody behind me now\n"
            "\n"
            "[Chorus]\n"
            "Signal bloom, carry me out\n"
            "Past where the streetlights go\n"
            "\n"
            "[Bridge]\n"
            "The tarmac ends and the dark begins\n"
            "I leave the engine running for a while\n"
            "\n"
            "[Final Chorus]\n"
            "Signal bloom, carry me out\n"
            "I am already gone\n"
            "\n"
            "[Outro]\n"
        ),
    )
    return project


def single_call_instruction(project: Project, count: int) -> str:
    """Populate's combined ask exactly as the route builds it with no sections known.

    Identical to run 1's `shipped_instruction` and run 2's control, count-enforcement
    wording included — this run changes the schema and nothing else.
    """
    assert project.song is not None
    assets = "; ".join(f"{asset.name} ({asset.kind})" for asset in project.assets)
    return (
        POPULATE_INSTRUCTION.format(
            duration=project.song.duration,
            count=count,
            assets=assets,
            sections_ask=POPULATE_SECTIONS_ASK,
            sections_constraint=POPULATE_SECTIONS_CONSTRAINT,
        )
        + POPULATE_FINAL_CHECK.format(count=count)
    )


def units() -> list[dict[str, Any]]:
    """Every call this run makes, in order, addressable by index.

    Unit 0 is the discarded warm-up. Arm A follows, **interleaved**: every arm gets roll 1
    before any arm gets roll 2. The scale probe is last.
    """
    plan: list[dict[str, Any]] = [
        {"phase": "warmup", "arm": "control", "roll": 0, "discarded": True}
    ]
    for roll in range(1, ROLLS + 1):
        for arm in ARMS:
            plan.append({"phase": "A", "arm": arm, "roll": roll, "discarded": False})
    for arm in SCALE_ARMS:
        plan.append({"phase": "C", "arm": arm, "roll": 1, "discarded": False})
    return plan


def json_observations(raw: str) -> dict[str, Any]:
    """Run 1's measurement D, carried forward: did rung 1 of the ladder suffice."""
    stripped = raw.strip()
    try:
        json.loads(stripped)
        bare_ok = True
    except (ValueError, TypeError):
        bare_ok = False
    try:
        extract_json(raw)
        ladder_ok = True
    except (ValueError, TypeError):
        ladder_ok = False
    return {
        "bare_json_loads_ok": bare_ok,
        "extract_json_ok": ladder_ok,
        "ladder_rescued_the_reply": ladder_ok and not bare_ok,
        "has_code_fence": "```" in stripped,
        "raw_chars": len(stripped),
    }


def reasoning_tokens(usage: dict[str, Any]) -> int | None:
    details = usage.get("completion_tokens_details") or {}
    value = details.get("reasoning_tokens")
    return value if isinstance(value, int) else None


async def one_call(
    client: DirectorClient,
    *,
    index: int,
    unit: dict[str, Any],
    message: str,
    context: dict[str, Any],
    schema: dict[str, Any],
    required: int,
) -> dict[str, Any]:
    """One plan-shaped call through `DirectorClient`'s own transport, recorded whole.

    `plan` returns only the validated `DirectorResult`, and this run needs the raw bytes,
    the token usage, and — the point of the whole run — what the *unvalidated* reply
    contained: an arm can force twelve array entries and still fail `PlannedShot`, and
    reporting that as "empty shots" would confuse a grammar result with a wording one. So
    the raw `shots` array is counted before validation as well as after.
    """
    body = {
        "model": client.model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {"request": message, "project": context}, ensure_ascii=False
                ),
            },
        ],
        "temperature": PLAN_TEMPERATURE,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "director_result", "strict": True, "schema": schema},
        },
    }
    started = time.monotonic()
    raw, usage, transport_error, provider_body = "", {}, "", ""
    try:
        response = await client._completion(body=body, headers=client._headers())
        # Kept before `raise_for_status` gets to it: a provider refusal's *text* is the
        # only thing that says why, and a run that reports "400" without it hands back a
        # mystery instead of a measurement.
        if response.status_code != 200:
            provider_body = response.text[:2000]
        payload = response.json()
        raw = client._content(response)
        usage = payload.get("usage") or {}
    except Exception as error:  # noqa: BLE001 - a transport failure is a measurement
        transport_error = f"{type(error).__name__}: {error}"
    elapsed = time.monotonic() - started

    raw_shots: int | None = None
    raw_sections: int | None = None
    result: DirectorResult | None = None
    parse_error = transport_error
    if raw:
        try:
            decoded = extract_json(raw)
            if isinstance(decoded, dict):
                raw_shots = len(decoded.get("shots") or [])
                raw_sections = len(decoded.get("sections") or [])
        except (ValueError, TypeError):
            pass
        try:
            result = DirectorResult.model_validate(extract_json(raw))
        except Exception as error:  # noqa: BLE001 - the failure text is the measurement
            parse_error = f"{type(error).__name__}: {error}"
    shots = [shot for shot in (result.shots if result else []) if shot.prompt.strip()]
    entry = {
        "index": index,
        **unit,
        "required": required,
        "temperature": PLAN_TEMPERATURE,
        "elapsed_seconds": round(elapsed, 1),
        "schema_required": schema.get("required"),
        "schema_min_items": schema.get("properties", {}).get("shots", {}).get("minItems"),
        # A call that never returned is not an empty plan; it is no plan. Folding a
        # transport failure into the empty-`shots` column would report a timeout as a
        # schema result, which is the confusion this run exists to avoid.
        "answered": bool(raw),
        "returned": len(shots),
        "raw_shots_in_reply": raw_shots,
        "raw_sections_in_reply": raw_sections,
        "empty_shots": bool(raw) and raw_shots == 0,
        "validation_failed": bool(raw) and result is None,
        "met_required": bool(raw) and len(shots) >= required,
        "exactly_required": bool(raw) and len(shots) == required,
        "sections_returned": len(result.sections) if result else 0,
        "reasoning_tokens": reasoning_tokens(usage),
        "parse_error": parse_error,
        "provider_refusal_body": provider_body,
        "usage": usage,
        "json": json_observations(raw) if raw else {},
        "request": message,
        "raw": raw,
        "utc": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    print(
        f"[{index}] {unit['phase']}/{unit['arm']} roll {unit['roll']}"
        f"{' (WARM-UP, DISCARDED)' if unit['discarded'] else ''}: "
        f"{elapsed:.0f}s  shots={len(shots)}/{required}"
        f"  raw_shots={raw_shots}  sections={entry['sections_returned']}"
        f"  reasoning={entry['reasoning_tokens']}"
        + ("  EMPTY SHOTS" if entry["empty_shots"] else "")
        + ("  VALIDATION FAILED" if entry["validation_failed"] else "")
        + (f"  ERROR: {parse_error[:120]}" if transport_error else "")
    )
    return entry


def append(entry: dict[str, Any]) -> None:
    """One roll, on disk, before the next one starts."""
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with CALLS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


async def run_range(start: int, end: int) -> None:
    settings = Settings()
    plan = units()
    small = probe_project()
    big = scale_project()
    assert small.song is not None and big.song is not None
    small_required = populate_required_shots(small.song.duration)
    big_required = populate_required_shots(big.song.duration)
    small_context = small.model_dump(mode="json", exclude=DIRECTOR_CONTEXT_EXCLUDE)
    big_context = big.model_dump(mode="json", exclude=DIRECTOR_CONTEXT_EXCLUDE)
    print(
        f"RUN 4 — schema `required` vs the empty-shots failure\n"
        f"model={settings.llm_model}  base={settings.llm_base_url}\n"
        f"units {start}..{end - 1} of {len(plan)}  song={SONG_SECONDS:.0f}s "
        f"required={small_required}  temperature={PLAN_TEMPERATURE}"
    )
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / f"{PREFIX}instructions.txt").write_text(
        "=== RUN 4 ===\n\n"
        "Every arm sends this identical request text and differs only in the strict\n"
        "json_schema attached to it. The count-enforcement wording is deliberately kept.\n\n"
        f"--- COMBINED ASK, 60 s song, required={small_required} ---\n"
        f"{single_call_instruction(small, small_required)}\n\n"
        f"--- COMBINED ASK, 180 s song, required={big_required} ---\n"
        f"{single_call_instruction(big, big_required)}\n\n"
        "--- SCHEMAS, per arm (60 s song) ---\n"
        + "\n\n".join(
            f"{arm}:\n{json.dumps(schema_for(arm, small_required), indent=2)}"
            for arm in ARMS
        )
        + "\n",
        encoding="utf-8",
    )
    for index in range(start, min(end, len(plan))):
        unit = plan[index]
        scale = unit["phase"] == "C"
        project = big if scale else small
        required = big_required if scale else small_required
        client = DirectorClient(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            timeout=SCALE_TIMEOUT_SECONDS if scale else TIMEOUT_SECONDS,
        )
        try:
            entry = await one_call(
                client,
                index=index,
                unit=unit,
                message=single_call_instruction(project, required),
                context=big_context if scale else small_context,
                schema=schema_for(unit["arm"], required),
                required=required,
            )
        finally:
            await client.close()
        append(entry)


def spread(values: list[float | int | None]) -> dict[str, Any]:
    numbers = [float(value) for value in values if value is not None]
    if not numbers:
        return {"n": 0}
    return {
        "n": len(numbers),
        "min": round(min(numbers), 1),
        "median": round(statistics.median(numbers), 1),
        "max": round(max(numbers), 1),
        "mean": round(statistics.fmean(numbers), 1),
        "max_over_min": round(max(numbers) / min(numbers), 1) if min(numbers) > 0 else None,
    }


def summarise() -> None:
    """Fold `run4-calls.jsonl` into `run4-summary.json`. The warm-up is excluded by name."""
    rows = [json.loads(line) for line in CALLS.read_text(encoding="utf-8").splitlines() if line]
    measured = [row for row in rows if not row["discarded"]]

    def arm_summary(subset: list[dict[str, Any]]) -> dict[str, Any]:
        answered = [row for row in subset if row["answered"]]
        return {
            "rolls": len(subset),
            "answered": len(answered),
            "empty_shots": sum(1 for row in subset if row["empty_shots"]),
            "empty_shots_rate": (
                f"{sum(1 for row in subset if row['empty_shots'])} of "
                f"{len(answered)} answered rolls"
            ),
            "validation_failed": sum(1 for row in subset if row["validation_failed"]),
            "met_required": sum(1 for row in subset if row["met_required"]),
            "exactly_required": sum(1 for row in subset if row["exactly_required"]),
            "shots_per_roll": [row["returned"] for row in subset],
            "raw_shots_per_roll": [row["raw_shots_in_reply"] for row in subset],
            "sections_per_roll": [row["sections_returned"] for row in subset],
            "both_halves": sum(
                1 for row in subset if row["returned"] > 0 and row["sections_returned"] > 0
            ),
            "elapsed_per_roll": [row["elapsed_seconds"] for row in subset],
            "elapsed_spread": spread([row["elapsed_seconds"] for row in subset]),
            "reasoning_per_roll": [row["reasoning_tokens"] for row in subset],
            "reasoning_spread": spread([row["reasoning_tokens"] for row in subset]),
        }

    summary = {
        "run": 4,
        "question": "does promoting `shots` into the strict schema's `required` stop `shots: []`?",
        "utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "plan_temperature": PLAN_TEMPERATURE,
        "song_seconds": SONG_SECONDS,
        "required_shots": populate_required_shots(SONG_SECONDS),
        "rolls_per_arm": ROLLS,
        "timeout_seconds_arms": TIMEOUT_SECONDS,
        "timeout_seconds_scale": SCALE_TIMEOUT_SECONDS,
        "shipped_director_timeout_seconds": 300,
        "warmup_calls_discarded": sum(1 for row in rows if row["discarded"]),
        "measured_calls": len(measured),
        "A": {
            arm: arm_summary([r for r in measured if r["phase"] == "A" and r["arm"] == arm])
            for arm in ARMS
        },
        "C_scale_probe": {
            "song_seconds": SCALE_SECONDS,
            "required_shots": populate_required_shots(SCALE_SECONDS),
            "rows": [
                {
                    key: row[key]
                    for key in (
                        "arm",
                        "elapsed_seconds",
                        "returned",
                        "raw_shots_in_reply",
                        "sections_returned",
                        "required",
                        "empty_shots",
                        "validation_failed",
                        "met_required",
                        "exactly_required",
                        "reasoning_tokens",
                        "parse_error",
                    )
                }
                for row in measured
                if row["phase"] == "C"
            ],
        },
        "D_json_ladder": {
            "calls_with_replies": sum(1 for r in rows if r.get("json")),
            "clean_bare_json_loads": sum(
                1 for r in rows if r.get("json", {}).get("bare_json_loads_ok")
            ),
            "ladder_rescued": sum(
                1 for r in rows if r.get("json", {}).get("ladder_rescued_the_reply")
            ),
            "replies_with_fences": sum(
                1 for r in rows if r.get("json", {}).get("has_code_fence")
            ),
        },
        "calls_over_shipped_300s_timeout": [
            {"index": r["index"], "arm": r["arm"], "elapsed_seconds": r["elapsed_seconds"]}
            for r in rows
            if r["elapsed_seconds"] > 300
        ],
    }
    (ARTIFACTS / f"{PREFIX}summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nevidence: {ARTIFACTS}")


def main() -> None:
    # The prompts and replies carry em dashes and typographic quotes; this console is
    # cp1252. Without this the run dies on a print rather than on a measurement.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    argv = sys.argv[1:]
    if argv and argv[0] == "summary":
        summarise()
        return
    if argv and argv[0] == "plan":
        for index, unit in enumerate(units()):
            print(index, unit)
        return
    start = int(argv[0]) if argv else 0
    end = int(argv[1]) if len(argv) > 1 else len(units())
    asyncio.run(run_range(start, end))


main()
