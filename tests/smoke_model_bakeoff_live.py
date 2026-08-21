"""Run 5 of the live LM Studio measurements: **which model, now that the schema is fixed?**

Runs 1-3 compared prompts and models against a broken grammar.
``DirectorResult.model_json_schema()["required"]`` never listed ``shots``, so LM Studio's
constrained decoder was *correct* to accept replies with none. Run 4 measured the fix on
one model and it separated cleanly: control 2 of 3 empty, ``require=("shots",)`` 0 of 3,
``require=("shots","sections")`` 0 of 3 with **both** halves delivered 3 of 3 where the
combined ask had managed 0 of 9 across two earlier runs.

**Every model verdict taken before that fix is void.** Gemma was ruled unusable on 8 of 8
empty replies, measured entirely against the broken grammar. This run re-decides the model
on a correct foundation, across three candidates:

* ``huihui-qwythos-9b-claude-mythos-5-1m-abliterated`` -- the incumbent, named in `.env`
* ``gemma-4-26b-a4b-it-heretic-ara-v2`` -- 26B-A4B MoE, ruled out on the broken schema
* ``huihui-qwen3.6-35b-a3b-claude-4.7-opus-abliterated-mtp`` -- 35B with 3B active

Usage -- one model at a time, because a model swap costs a load::

    uv run python tests/smoke_model_bakeoff_live.py plan
    uv run python tests/smoke_model_bakeoff_live.py qwythos 0 6
    uv run python tests/smoke_model_bakeoff_live.py summary

`.env` is **not edited**. The model under test is named here, so the run cannot silently
measure whatever happens to be configured. Nothing is written into ``data/``, no route is
called, and no ComfyUI work of any kind is submitted.

Conditions are runs 2-4's, unchanged, so the numbers land in the same table: the same 60.0 s
song, ``populate_required_shots(60)`` = 12, ``PLAN_TEMPERATURE`` 0.7, the same combined
single-call ask assembled from the same shipped constants with the count-enforcement wording
left in place. The **only** variable across models is the model.

The arms:

* **fixed** -- ``director_result_schema(require=("shots", "sections"))``, which is exactly
  what `populate_timeline` now sends when it asks for sections (`app.py`). Every model runs
  this arm.
* **old** -- ``DirectorResult.model_json_schema()``, the broken grammar runs 1-3 measured.
  **Gemma only.** Gemma is the sharpest test of the fix because it failed 8 of 8 before, and
  a historical comparison across sessions is confounded by everything that changed in
  between. Its two arms are **interleaved** so session drift lands on both.

Rolls: **5 per model** on the fixed arm, 3 on Gemma's old-schema control. The pre-declared
plan was 3; rolls 4 and 5 are a declared extension, and the summary reports the first three
separately so the N=3 figure stays comparable with runs 2-4.

A **warm-up call is made and discarded** on every model -- a cold load is believed to be
what killed run 1's roll 1 -- and is recorded as ``discarded: true`` rather than merely
claimed.

Each call is appended to ``run5-calls.jsonl`` **as it lands**, so a harness that dies
halfway leaves every roll it completed. Calls are addressed by index so a model's plan can
be chunked under a tool timeout without changing what is measured.

Timeout: **900 s** per plan call, runs 2-4's budget, kept so the numbers are comparable. The
shipped `DirectorClient` timeout is 300 s.
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

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "test-artifacts" / "2026-08-20-lmstudio-live"
PREFIX = "run5-"
CALLS = ARTIFACTS / f"{PREFIX}calls.jsonl"

#: Runs 1-4's song, unchanged. `populate_required_shots(60)` is 12 shots.
SONG_SECONDS = 60.0

#: Rolls per model on the fixed arm. Three were pre-declared; 4 and 5 are the extension.
ROLLS = 5

#: Rolls in Gemma's within-session control on the old, broken schema.
CONTROL_ROLLS = 3

TIMEOUT_SECONDS = 900
VISION_TIMEOUT_SECONDS = 300

#: Confirmed against `GET /v1/models` before the run: these ids exist on this host.
MODELS: dict[str, str] = {
    "qwythos": "huihui-qwythos-9b-claude-mythos-5-1m-abliterated",
    "gemma": "gemma-4-26b-a4b-it-heretic-ara-v2",
    "qwen35b": "huihui-qwen3.6-35b-a3b-claude-4.7-opus-abliterated-mtp",
}

#: Run 3's image and run 3's deliberately **misleading** `purpose` string. The file is named
#: "multiview" and is handed to the model as a character sheet; it is in fact a five-view
#: sheet of a grey sci-fi spacecraft with a small red accent light on the forward upper hull.
#: A model producing filler echoes "character" back; a model that is looking contradicts it.
VISION_IMAGE = ROOT / "docs" / "asset_940dfee992dd-multiview_00001_.png"
VISION_PURPOSE = "character multiview consistency sheet"


def schema_for(arm: str) -> dict[str, Any]:
    """The strict schema each arm attaches to an identical request.

    Built from the shipped `director_result_schema` rather than transcribed, so an arm
    cannot measure a schema the application would not send.
    """
    if arm == "fixed":
        return director_result_schema(require=("shots", "sections"))
    if arm == "old":
        return DirectorResult.model_json_schema()
    raise ValueError(f"unknown arm {arm!r}")


def probe_project() -> Project:
    """Runs 1-4's project, byte-for-byte, so this run's conditions match theirs."""
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


def single_call_instruction(project: Project, count: int) -> str:
    """Populate's combined ask exactly as the route builds it with no sections known."""
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


def units(model_key: str) -> list[dict[str, Any]]:
    """Every call this run makes for one model, in order, addressable by index.

    Unit 0 is the discarded warm-up. Gemma's two arms are interleaved for the three rolls
    the control runs, then its extension rolls follow. Vision is last, so a model that is
    slow on the plan calls still gets its one vision measurement inside a chunk.
    """
    plan: list[dict[str, Any]] = [
        {"phase": "warmup", "arm": "fixed", "roll": 0, "discarded": True}
    ]
    if model_key == "gemma":
        for roll in range(1, CONTROL_ROLLS + 1):
            plan.append({"phase": "A", "arm": "fixed", "roll": roll, "discarded": False})
            plan.append({"phase": "B", "arm": "old", "roll": roll, "discarded": False})
        for roll in range(CONTROL_ROLLS + 1, ROLLS + 1):
            plan.append({"phase": "A", "arm": "fixed", "roll": roll, "discarded": False})
    else:
        for roll in range(1, ROLLS + 1):
            plan.append({"phase": "A", "arm": "fixed", "roll": roll, "discarded": False})
    plan.append({"phase": "E", "arm": "vision", "roll": 1, "discarded": False})
    return [{**unit, "model": model_key} for unit in plan]


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


async def one_plan_call(
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

    `plan` returns only the validated `DirectorResult`, and this run needs the raw bytes, the
    token usage, and what the *unvalidated* reply contained -- a reply can carry twelve array
    entries and still fail `PlannedShot`, and reporting that as "empty shots" would confuse a
    validation result with a grammar one.
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
    prompts = [shot.prompt for shot in shots]
    entry = {
        "index": index,
        **unit,
        "model_id": client.model,
        "required": required,
        "temperature": PLAN_TEMPERATURE,
        "elapsed_seconds": round(elapsed, 1),
        "schema_required": schema.get("required"),
        "answered": bool(raw),
        "returned": len(shots),
        "raw_shots_in_reply": raw_shots,
        "raw_sections_in_reply": raw_sections,
        "empty_shots": bool(raw) and raw_shots == 0,
        "validation_failed": bool(raw) and result is None,
        "met_required": bool(raw) and len(shots) >= required,
        "exactly_required": bool(raw) and len(shots) == required,
        "sections_returned": len(result.sections) if result else 0,
        "sections": [section.model_dump() for section in result.sections] if result else [],
        "shot_prompts": prompts,
        "distinct_prompts": len(set(prompts)),
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
        f"[{unit['model']}:{index}] {unit['phase']}/{unit['arm']} roll {unit['roll']}"
        f"{' (WARM-UP, DISCARDED)' if unit['discarded'] else ''}: "
        f"{elapsed:.0f}s  shots={len(shots)}/{required}"
        f"  raw_shots={raw_shots}  sections={entry['sections_returned']}"
        f"  distinct_prompts={entry['distinct_prompts']}"
        f"  reasoning={entry['reasoning_tokens']}"
        + ("  EMPTY SHOTS" if entry["empty_shots"] else "")
        + ("  VALIDATION FAILED" if entry["validation_failed"] else "")
        + (f"  ERROR: {parse_error[:160]}" if transport_error else "")
    )
    return entry


async def one_vision_call(
    client: DirectorClient, *, index: int, unit: dict[str, Any]
) -> dict[str, Any]:
    """The shipped `DirectorClient.inspect_image`, not a hand-rolled request.

    `config.py` carries a **single** `llm_model`: whichever model plans also does vision and
    expansion. A model that cannot see is disqualified whatever its planning numbers say.
    """
    image = VISION_IMAGE.read_bytes()
    started = time.monotonic()
    payload: dict[str, Any] | None = None
    error_text = ""
    try:
        result = await client.inspect_image(
            image=image, mime_type="image/png", purpose=VISION_PURPOSE
        )
        payload = result.model_dump()
    except Exception as error:  # noqa: BLE001 - a vision failure is a measurement
        error_text = f"{type(error).__name__}: {error}"
    elapsed = time.monotonic() - started
    entry = {
        "index": index,
        **unit,
        "model_id": client.model,
        "elapsed_seconds": round(elapsed, 1),
        "answered": payload is not None,
        "image": str(VISION_IMAGE.relative_to(ROOT)),
        "image_bytes": len(image),
        "purpose_sent": VISION_PURPOSE,
        "inspection": payload,
        "parse_error": error_text,
        "utc": datetime.now(UTC).isoformat(timespec="seconds"),
        # Columns the plan rows carry, so the summary can read one shape.
        "required": 0,
        "returned": 0,
        "raw_shots_in_reply": None,
        "raw_sections_in_reply": None,
        "empty_shots": False,
        "validation_failed": False,
        "met_required": False,
        "exactly_required": False,
        "sections_returned": 0,
        "reasoning_tokens": None,
        "json": {},
    }
    summary = (payload or {}).get("summary", "")
    print(
        f"[{unit['model']}:{index}] E/vision: {elapsed:.0f}s  "
        + (f"ok -- {summary[:200]}" if payload else f"FAILED: {error_text[:200]}")
    )
    return entry


def append(entry: dict[str, Any]) -> None:
    """One call, on disk, before the next one starts."""
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with CALLS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


async def run_range(model_key: str, start: int, end: int) -> None:
    model_id = MODELS[model_key]
    settings = Settings()
    plan = units(model_key)
    project = probe_project()
    assert project.song is not None
    required = populate_required_shots(project.song.duration)
    context = project.model_dump(mode="json", exclude=DIRECTOR_CONTEXT_EXCLUDE)
    print(
        f"RUN 5 -- three-model bake-off on the fixed schema\n"
        f"model={model_id}  (.env still says {settings.llm_model})\n"
        f"base={settings.llm_base_url}  units {start}..{min(end, len(plan)) - 1} "
        f"of {len(plan)}  song={SONG_SECONDS:.0f}s required={required} "
        f"temperature={PLAN_TEMPERATURE}"
    )
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    instructions = ARTIFACTS / f"{PREFIX}instructions.txt"
    if not instructions.exists():
        instructions.write_text(
            "=== RUN 5 ===\n\n"
            "Every model and every arm sends this identical request text. The arms differ\n"
            "only in the strict json_schema attached to it. The count-enforcement wording\n"
            "is deliberately kept, exactly as in runs 2-4.\n\n"
            f"--- COMBINED ASK, 60 s song, required={required} ---\n"
            f"{single_call_instruction(project, required)}\n\n"
            "--- SYSTEM PROMPT ---\n"
            f"{SYSTEM_PROMPT}\n\n"
            "--- SCHEMAS, per arm ---\n"
            + "\n\n".join(
                f"{arm}:\n{json.dumps(schema_for(arm), indent=2)}" for arm in ("fixed", "old")
            )
            + f"\n\n--- VISION ---\nimage: {VISION_IMAGE}\npurpose: {VISION_PURPOSE}\n",
            encoding="utf-8",
        )
    for index in range(start, min(end, len(plan))):
        unit = plan[index]
        client = DirectorClient(
            base_url=settings.llm_base_url,
            model=model_id,
            api_key=settings.llm_api_key,
            timeout=VISION_TIMEOUT_SECONDS if unit["arm"] == "vision" else TIMEOUT_SECONDS,
        )
        try:
            if unit["arm"] == "vision":
                entry = await one_vision_call(client, index=index, unit=unit)
            else:
                entry = await one_plan_call(
                    client,
                    index=index,
                    unit=unit,
                    message=single_call_instruction(project, required),
                    context=context,
                    schema=schema_for(unit["arm"]),
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
        "distinct_prompts_per_roll": [row.get("distinct_prompts") for row in subset],
        "elapsed_per_roll": [row["elapsed_seconds"] for row in subset],
        "elapsed_spread": spread([row["elapsed_seconds"] for row in subset]),
        "reasoning_per_roll": [row["reasoning_tokens"] for row in subset],
        "reasoning_spread": spread([row["reasoning_tokens"] for row in subset]),
    }


def summarise() -> None:
    """Fold `run5-calls.jsonl` into `run5-summary.json`. Warm-ups are excluded by name."""
    rows = [json.loads(line) for line in CALLS.read_text(encoding="utf-8").splitlines() if line]
    measured = [row for row in rows if not row["discarded"]]
    per_model: dict[str, Any] = {}
    for key, model_id in MODELS.items():
        mine = [row for row in measured if row["model"] == key]
        fixed = [row for row in mine if row["arm"] == "fixed"]
        old = [row for row in mine if row["arm"] == "old"]
        vision = [row for row in mine if row["arm"] == "vision"]
        if not mine:
            continue
        per_model[key] = {
            "model_id": model_id,
            "fixed_schema": arm_summary(fixed),
            "fixed_schema_first_three": arm_summary(fixed[:3]),
            "old_schema_control": arm_summary(old) if old else None,
            "vision": [
                {
                    "elapsed_seconds": row["elapsed_seconds"],
                    "answered": row["answered"],
                    "parse_error": row["parse_error"],
                    "inspection": row["inspection"],
                }
                for row in vision
            ],
        }
    plan_rows = [row for row in rows if row["arm"] != "vision"]
    summary = {
        "run": 5,
        "question": "with the schema fixed, which model should `MVP_LLM_MODEL` name?",
        "utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "plan_temperature": PLAN_TEMPERATURE,
        "song_seconds": SONG_SECONDS,
        "required_shots": populate_required_shots(SONG_SECONDS),
        "rolls_fixed_arm": ROLLS,
        "rolls_old_schema_control": CONTROL_ROLLS,
        "timeout_seconds": TIMEOUT_SECONDS,
        "shipped_director_timeout_seconds": 300,
        "warmup_calls_discarded": sum(1 for row in rows if row["discarded"]),
        "measured_calls": len(measured),
        "models": per_model,
        "D_json_ladder": {
            "calls_with_replies": sum(1 for r in plan_rows if r.get("json")),
            "clean_bare_json_loads": sum(
                1 for r in plan_rows if r.get("json", {}).get("bare_json_loads_ok")
            ),
            "ladder_rescued": sum(
                1 for r in plan_rows if r.get("json", {}).get("ladder_rescued_the_reply")
            ),
            "replies_with_fences": sum(
                1 for r in plan_rows if r.get("json", {}).get("has_code_fence")
            ),
        },
        "calls_over_shipped_300s_timeout": [
            {
                "model": r["model"],
                "index": r["index"],
                "arm": r["arm"],
                "elapsed_seconds": r["elapsed_seconds"],
            }
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
        for key in MODELS:
            for index, unit in enumerate(units(key)):
                print(key, index, unit)
        return
    if not argv or argv[0] not in MODELS:
        raise SystemExit(f"usage: {Path(__file__).name} <{'|'.join(MODELS)}|summary|plan> [start] [end]")
    model_key = argv[0]
    start = int(argv[1]) if len(argv) > 1 else 0
    end = int(argv[2]) if len(argv) > 2 else len(units(model_key))
    asyncio.run(run_range(model_key, start, end))


main()
