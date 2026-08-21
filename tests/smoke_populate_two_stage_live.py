"""Run 2 of the live LM Studio measurements: **does two-stage populate fix `shots: []`?**

Run 1 (`smoke_calliope_live.py`, evidence under `test-artifacts/2026-08-20-lmstudio-live/`)
found that populate's real failure on this model is *not* under-delivery. It is `shots: []`
with `sections` filled instead — the model answers the other half of a combined ask. That
happened on the first roll of both arms and once more in a probe, and it is why the count
enforcement measured no effect: a count constraint has nothing to grip on an answer with no
shots.

`two_stage` (shipped 2026-08-20, `PopulateTimelineRequest.two_stage`, **off by default**)
argues that this model will not emit `sections` beside a large shot layout in one call, so
each half should be asked for on its own. Run 1's finding is the sharpest test of that
premise there has been: the model is currently *choosing between* the two halves. This run
measures whether splitting the ask stops the dropping.

    uv run python tests/smoke_populate_two_stage_live.py

Not collected by pytest: `smoke_*.py` does not match `python_files`, exactly as the five
existing live smokes avoid collection. **Nothing is written into `data/`, no route is
called, and no ComfyUI work of any kind is submitted.** Throwaway `Project` objects are
built in memory, the shipped constants and the shipped `repair_sections` are imported
rather than transcribed, and the only I/O besides the model calls is the evidence dropped
under `test-artifacts/2026-08-20-lmstudio-live/` with a `run2-` prefix, beside run 1's.

What it measures:

A. **Two-stage vs single-call**, same conditions as run 1 so the numbers are comparable:
   the same 60 s song, the same 12-shot requirement (`populate_required_shots`), the same
   `PLAN_TEMPERATURE`. N=3 per arm, which settles nothing on its own and the report has to
   say so. The primary metric is the **empty-`shots` rate** per arm — that is the failure
   being targeted. The single-call arm is run fresh rather than reused from run 1: a
   different session and model state is a confound.

   The two arms are **interleaved** (two-stage 1, single 1, two-stage 2, …) rather than run
   in blocks, so any warm-up or drift across the session lands on both arms instead of on
   whichever went first. Run 1 ran its arms in blocks and both of its first-roll failures
   were first rolls of a block; interleaving is the cheapest way to stop that pattern
   recurring as an artefact.

B. **Timeout exposure.** Elapsed is recorded per call, and for two-stage also summed per
   populate. Two smaller calls may or may not beat one large one on total wall clock; the
   point is to measure it rather than assume it. `DirectorClient`'s shipped timeout is
   300 s and run 1 died on it once, so the exposure question is which arm's *longest single
   call* comes closest to that budget.

C. **Scale probe**, one roll per arm at a realistic full-song count: a 180 s song, which
   `populate_required_shots` turns into 35 shots. N=1 per arm — a probe, not a measurement.
   A timeout is a result and is reported as one.

Both stages of the two-stage arm are built the way `populate_timeline` builds them: stage
one is `POPULATE_SECTIONS_INSTRUCTION` alone, its reply passed through the shipped
`repair_sections`; stage two is `POPULATE_INSTRUCTION` with the sections ask and constraint
**dropped**, the section map appended in the route's own words, and `POPULATE_FINAL_CHECK`
last. Stage one gets exactly one call and no retry, as the route gives it.

Timeouts, stated because run 1 showed they are load-bearing: **900 s** for the A arms (run
1's budget, so the numbers are comparable) and **1800 s** for the scale probe, so a slow
success is distinguishable from a hang. Both are deliberately larger than the shipped 300 s;
that gap is a finding to hand back, not a thing to fix from here.
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
    POPULATE_SECTIONS_INSTRUCTION,
    populate_required_shots,
)
from music_video_producer.config import Settings
from music_video_producer.director import (
    PLAN_TEMPERATURE,
    SYSTEM_PROMPT,
    DirectorClient,
    DirectorResult,
    extract_json,
)
from music_video_producer.models import Asset, Project, Song, SongSection
from music_video_producer.timeline import repair_sections

#: Beside run 1's evidence, in the same dated directory, under a `run2-` prefix so the two
#: runs are never confused for one another.
ARTIFACTS = Path(__file__).resolve().parents[1] / "test-artifacts" / "2026-08-20-lmstudio-live"
PREFIX = "run2-"

#: Run 1's song, unchanged, so arm A is comparable to run 1's numbers.
#: `populate_required_shots(60)` is 12 shots.
SONG_SECONDS = 60.0

#: The scale probe's song. `populate_required_shots(180)` is 35 shots — the realistic
#: full-song ask the 12-shot measurement cannot speak for.
SCALE_SECONDS = 180.0

#: Rolls per arm in A. Three. Three is a small sample and the report must say so.
ROLLS = 3

#: Run 1's budget, kept so the A numbers are comparable to run 1's. Larger than the shipped
#: 300 s on purpose — a probe that keeps the shipped budget measures the budget.
TIMEOUT_SECONDS = 900

#: The scale probe's budget. Generous so that "slow" and "hung" are distinguishable.
SCALE_TIMEOUT_SECONDS = 1800


def probe_project() -> Project:
    """Run 1's project, byte-for-byte, so arm A's conditions match run 1's."""
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
    """The same video at real length: a three-minute song with a full lyric sheet.

    Everything except the song is identical to `probe_project`, so the only thing the scale
    probe changes is the size of the ask — 35 shots instead of 12, and eight `[Tag]` blocks
    instead of five.
    """
    project = probe_project()
    assert project.song is not None
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
    """The **control**: populate's ask exactly as the route builds it with `two_stage` off
    and no sections known — the instruction, the sections ask and constraint, and
    `POPULATE_FINAL_CHECK` appended last. Identical to run 1's `shipped_instruction`.
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


def two_stage_shots_instruction(
    project: Project, count: int, sections: list[SongSection]
) -> str:
    """Stage two of the two-stage populate, assembled as `populate_timeline` assembles it.

    The differences from the control are the route's own and only the route's: the sections
    ask and the sections constraint are **dropped** (`wants_sections` is false once stage one
    has filled the boxes), the section map is appended in the route's wording with its
    "just laid out in the structure pass" origin, and `POPULATE_FINAL_CHECK` still goes last
    so the count is the final thing read.
    """
    assert project.song is not None
    assets = "; ".join(f"{asset.name} ({asset.kind})" for asset in project.assets)
    instruction = POPULATE_INSTRUCTION.format(
        duration=project.song.duration,
        count=count,
        assets=assets,
        sections_ask="",
        sections_constraint="",
    )
    if sections:
        section_map = "; ".join(
            f"{section.label} {section.start:.1f}-{section.end:.1f}s"
            + (f" ({section.prompt})" if section.prompt else "")
            for section in sections
        )
        instruction += (
            f" The song's sections, just laid out in the structure pass, are: "
            f"{section_map}. Shots must respect these boundaries — every shot sits "
            "inside one section and takes that section's character."
        )
    return instruction + POPULATE_FINAL_CHECK.format(count=count)


class Recorder:
    """Every call, with its timing, its token usage and its raw reply kept whole."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.calls: list[dict[str, Any]] = []
        self.started = time.monotonic()

    def record(self, **fields: Any) -> dict[str, Any]:
        entry = {"n": len(self.calls) + 1, **fields}
        self.calls.append(entry)
        return entry

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


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


async def plan_capturing_raw(
    client: DirectorClient,
    *,
    message: str,
    project_context: dict[str, Any],
    temperature: float,
) -> tuple[str, DirectorResult | None, str, dict[str, Any]]:
    """`DirectorClient.plan`'s request through `DirectorClient`'s own transport, handing
    back the raw content and the token usage as well as the parsed result.

    `plan` returns only the validated `DirectorResult`, and this run needs the bytes and
    `usage.completion_tokens_details.reasoning_tokens` — the number that explains the wall
    clock, and the number run 1 measured swinging 26× across identical rolls. The body is
    assembled exactly as `plan` assembles it (same imported `SYSTEM_PROMPT`, same user-turn
    envelope, same strict `response_format` from the same schema) and posted through the
    shipped `_completion`, so the unloaded-model retry is the real one. A transport failure
    — a timeout — is returned as an ordinary result rather than raised, so one dead call
    cannot take the rest of the run down with it.
    """
    body = {
        "model": client.model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {"request": message, "project": project_context}, ensure_ascii=False
                ),
            },
        ],
        "temperature": temperature,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "director_result",
                "strict": True,
                "schema": DirectorResult.model_json_schema(),
            },
        },
    }
    try:
        response = await client._completion(body=body, headers=client._headers())
        payload = response.json()
        raw = client._content(response)
    except Exception as error:  # noqa: BLE001 - a transport failure is a measurement
        return "", None, f"{type(error).__name__}: {error}", {}
    usage = payload.get("usage") or {}
    try:
        return raw, DirectorResult.model_validate(extract_json(raw)), "", usage
    except Exception as error:  # noqa: BLE001 - the failure text is the measurement
        return raw, None, f"{type(error).__name__}: {error}", usage


def reasoning_tokens(usage: dict[str, Any]) -> int | None:
    details = usage.get("completion_tokens_details") or {}
    value = details.get("reasoning_tokens")
    return value if isinstance(value, int) else None


async def call(
    client: DirectorClient,
    recorder: Recorder,
    *,
    label: str,
    stage: str,
    arm: str,
    message: str,
    context: dict[str, Any],
    temperature: float,
    required: int,
) -> dict[str, Any]:
    """One plan-shaped call, timed, counted and written down whole."""
    print(f"  [{len(recorder.calls) + 1}] {label} ... ", end="", flush=True)
    started = time.monotonic()
    raw, result, parse_error, usage = await plan_capturing_raw(
        client, message=message, project_context=context, temperature=temperature
    )
    elapsed = time.monotonic() - started
    shots = [shot for shot in (result.shots if result else []) if shot.prompt.strip()]
    entry = recorder.record(
        label=label,
        arm=arm,
        stage=stage,
        temperature=temperature,
        elapsed_seconds=round(elapsed, 1),
        required=required,
        returned=len(shots),
        # A call that never returned is not an empty plan; it is no plan. Folding a
        # transport failure into the empty-`shots` column would report a timeout as a
        # wording result, which is exactly the confusion this run exists to avoid.
        answered=bool(raw),
        empty_shots=bool(raw) and not shots,
        met_required=bool(raw) and len(shots) >= required,
        sections_returned=len(result.sections) if result else 0,
        reasoning_tokens=reasoning_tokens(usage),
        parse_error=parse_error,
        usage=usage,
        json=json_observations(raw) if raw else {},
        request=message,
        raw=raw,
    )
    print(
        f"{elapsed:.0f}s  shots={len(shots)}/{required}"
        f"  sections={entry['sections_returned']}"
        f"  reasoning={entry['reasoning_tokens']}"
        + ("  EMPTY SHOTS" if entry["empty_shots"] else "")
        + (f"  FAILED: {parse_error}" if parse_error else "")
    )
    return entry


async def two_stage_populate(
    client: DirectorClient,
    recorder: Recorder,
    *,
    tag: str,
    project: Project,
    context: dict[str, Any],
    required: int,
) -> dict[str, Any]:
    """One whole two-stage populate: the structure call, `repair_sections`, the shots call.

    The route's own sequence, including its concession: stage one gets one call and no
    retry, and if it comes back with nothing then `wants_sections` stays true and the shots
    call asks for the structure the way the single-call arm does. That fallback is recorded
    rather than hidden, because a two-stage populate that silently degrades into a
    single-call one is not evidence for the split.
    """
    assert project.song is not None
    duration = project.song.duration
    stage_one = await call(
        client,
        recorder,
        label=f"{tag} stage 1/2 (structure only)",
        stage="structure",
        arm="two_stage",
        message=POPULATE_SECTIONS_INSTRUCTION.format(duration=duration),
        context=context,
        temperature=PLAN_TEMPERATURE,
        # Stage one is not asked for shots at all; `required` is carried only so the
        # recorded row has the same shape as every other row.
        required=0,
    )
    raw_sections = []
    if stage_one["raw"]:
        try:
            parsed = DirectorResult.model_validate(extract_json(stage_one["raw"]))
            raw_sections = [
                (item.label, item.start, item.duration, item.prompt) for item in parsed.sections
            ]
        except Exception:  # noqa: BLE001 - an unparseable stage one is an empty stage one
            raw_sections = []
    staged = [
        SongSection(label=label, start=start, duration=length, prompt=prompt)
        for label, start, length, prompt in repair_sections(raw_sections, duration)
    ]
    fell_back = not staged
    if fell_back:
        print("      stage 1 produced no usable sections — stage 2 falls back to asking")
        message = single_call_instruction(project, required)
    else:
        message = two_stage_shots_instruction(project, required, staged)
    stage_two = await call(
        client,
        recorder,
        label=f"{tag} stage 2/2 (shots{'' if staged else ', FELL BACK to combined ask'})",
        stage="shots",
        arm="two_stage",
        message=message,
        context=context,
        temperature=PLAN_TEMPERATURE,
        required=required,
    )
    return {
        "tag": tag,
        "arm": "two_stage",
        "required": required,
        "stage_one_sections_raw": len(raw_sections),
        "stage_one_sections_after_repair": len(staged),
        "stage_one_fell_back": fell_back,
        "stage_one_seconds": stage_one["elapsed_seconds"],
        "stage_two_seconds": stage_two["elapsed_seconds"],
        "total_seconds": round(stage_one["elapsed_seconds"] + stage_two["elapsed_seconds"], 1),
        "longest_single_call_seconds": max(
            stage_one["elapsed_seconds"], stage_two["elapsed_seconds"]
        ),
        "shots": stage_two["returned"],
        "answered": stage_two["answered"],
        "empty_shots": stage_two["empty_shots"],
        "met_required": stage_two["met_required"],
        "sections_delivered": len(staged),
        "reasoning_tokens_stage_one": stage_one["reasoning_tokens"],
        "reasoning_tokens_stage_two": stage_two["reasoning_tokens"],
        "parse_error_stage_one": stage_one["parse_error"],
        "parse_error_stage_two": stage_two["parse_error"],
    }


async def single_populate(
    client: DirectorClient,
    recorder: Recorder,
    *,
    tag: str,
    project: Project,
    context: dict[str, Any],
    required: int,
) -> dict[str, Any]:
    """One single-call populate — the control, and the shipped default."""
    entry = await call(
        client,
        recorder,
        label=f"{tag} single call",
        stage="combined",
        arm="single",
        message=single_call_instruction(project, required),
        context=context,
        temperature=PLAN_TEMPERATURE,
        required=required,
    )
    return {
        "tag": tag,
        "arm": "single",
        "required": required,
        "stage_one_seconds": None,
        "stage_two_seconds": entry["elapsed_seconds"],
        "total_seconds": entry["elapsed_seconds"],
        "longest_single_call_seconds": entry["elapsed_seconds"],
        "shots": entry["returned"],
        "answered": entry["answered"],
        "empty_shots": entry["empty_shots"],
        "met_required": entry["met_required"],
        "sections_delivered": entry["sections_returned"],
        "reasoning_tokens_stage_one": None,
        "reasoning_tokens_stage_two": entry["reasoning_tokens"],
        "parse_error_stage_one": "",
        "parse_error_stage_two": entry["parse_error"],
    }


def spread(values: list[float | int | None]) -> dict[str, Any]:
    """Min, median, max and the ratio — the shape run 1's 26× reasoning swing demands.

    A mean alone would hide exactly the variance that makes the timeout question real, so
    nothing here reports a mean without the range beside it.
    """
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


async def main() -> None:
    # The prompts and replies carry em dashes and typographic quotes; this console is
    # cp1252. Without this the run dies on a print rather than on a measurement.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    settings = Settings()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    project = probe_project()
    assert project.song is not None
    required = populate_required_shots(project.song.duration)
    context = project.model_dump(mode="json", exclude=DIRECTOR_CONTEXT_EXCLUDE)
    recorder = Recorder(settings.llm_model)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    print("RUN 2 — two-stage populate vs single call")
    print(f"model={settings.llm_model}  base={settings.llm_base_url}  utc={stamp}")
    print(
        f"song={project.song.duration:.0f}s  required shots={required}  rolls per arm={ROLLS}"
        f"  temperature={PLAN_TEMPERATURE}  timeout={TIMEOUT_SECONDS}s"
    )

    client = DirectorClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        timeout=TIMEOUT_SECONDS,
    )
    (ARTIFACTS / f"{PREFIX}instructions.txt").write_text(
        "=== RUN 2 ===\n\n"
        f"--- SINGLE CALL (control, shipped default), required={required} ---\n"
        f"{single_call_instruction(project, required)}\n\n"
        f"--- TWO-STAGE, STAGE 1 (structure only) ---\n"
        f"{POPULATE_SECTIONS_INSTRUCTION.format(duration=project.song.duration)}\n\n"
        "--- TWO-STAGE, STAGE 2 (shots) — assembled per roll from stage one's sections; "
        "the exact text of every stage-two call is in run2-calls.json ---\n",
        encoding="utf-8",
    )

    populates: list[dict[str, Any]] = []
    scale: list[dict[str, Any]] = []
    try:
        print("\nA. two-stage vs single call, interleaved")
        for index in range(1, ROLLS + 1):
            populates.append(
                await two_stage_populate(
                    client,
                    recorder,
                    tag=f"A/two-stage {index}",
                    project=project,
                    context=context,
                    required=required,
                )
            )
            populates.append(
                await single_populate(
                    client,
                    recorder,
                    tag=f"A/single {index}",
                    project=project,
                    context=context,
                    required=required,
                )
            )

        print(f"\nC. scale probe — {SCALE_SECONDS:.0f}s song, N=1 per arm, "
              f"timeout={SCALE_TIMEOUT_SECONDS}s")
        big = scale_project()
        assert big.song is not None
        big_required = populate_required_shots(big.song.duration)
        big_context = big.model_dump(mode="json", exclude=DIRECTOR_CONTEXT_EXCLUDE)
        print(f"   required shots={big_required}")
        scale_client = DirectorClient(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            timeout=SCALE_TIMEOUT_SECONDS,
        )
        try:
            scale.append(
                await two_stage_populate(
                    scale_client,
                    recorder,
                    tag="C/two-stage",
                    project=big,
                    context=big_context,
                    required=big_required,
                )
            )
            scale.append(
                await single_populate(
                    scale_client,
                    recorder,
                    tag="C/single",
                    project=big,
                    context=big_context,
                    required=big_required,
                )
            )
        finally:
            await scale_client.close()

        two = [row for row in populates if row["arm"] == "two_stage"]
        one = [row for row in populates if row["arm"] == "single"]

        def arm_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
            answered = [row for row in rows if row["answered"]]
            return {
                "rolls": len(rows),
                "answered": len(answered),
                "empty_shots": sum(1 for row in rows if row["empty_shots"]),
                "empty_shots_rate": (
                    f"{sum(1 for row in rows if row['empty_shots'])} of {len(answered)} "
                    "answered rolls"
                ),
                "met_required": sum(1 for row in rows if row["met_required"]),
                "shots_per_roll": [row["shots"] for row in rows],
                "sections_per_roll": [row["sections_delivered"] for row in rows],
                "total_seconds_per_populate": [row["total_seconds"] for row in rows],
                "longest_single_call_seconds": [
                    row["longest_single_call_seconds"] for row in rows
                ],
                "total_seconds_spread": spread([row["total_seconds"] for row in rows]),
                "longest_call_spread": spread(
                    [row["longest_single_call_seconds"] for row in rows]
                ),
                "reasoning_tokens_spread": spread(
                    [row["reasoning_tokens_stage_one"] for row in rows]
                    + [row["reasoning_tokens_stage_two"] for row in rows]
                ),
            }

        summary = {
            "run": 2,
            "question": "does two-stage populate fix the empty-shots failure?",
            "model": settings.llm_model,
            "base_url": settings.llm_base_url,
            "utc_started": stamp,
            "utc_finished": datetime.now(UTC).isoformat(timespec="seconds"),
            "plan_temperature": PLAN_TEMPERATURE,
            "timeout_seconds_arms": TIMEOUT_SECONDS,
            "timeout_seconds_scale_probe": SCALE_TIMEOUT_SECONDS,
            "shipped_director_timeout_seconds": 300,
            "song_seconds": project.song.duration,
            "required_shots": required,
            "rolls_per_arm": ROLLS,
            "total_calls": len(recorder.calls),
            "total_elapsed_seconds": round(recorder.elapsed, 1),
            "A_two_stage": arm_summary(two),
            "A_single": arm_summary(one),
            "A_rows": populates,
            "C_scale_probe": {
                "song_seconds": SCALE_SECONDS,
                "required_shots": populate_required_shots(SCALE_SECONDS),
                "timeout_seconds": SCALE_TIMEOUT_SECONDS,
                "rows": scale,
            },
            "D_json": {
                "calls_with_replies": sum(1 for e in recorder.calls if e.get("json")),
                "clean_bare_json_loads": sum(
                    1 for e in recorder.calls if e.get("json", {}).get("bare_json_loads_ok")
                ),
                "ladder_rescued": sum(
                    1 for e in recorder.calls if e.get("json", {}).get("ladder_rescued_the_reply")
                ),
                "replies_with_fences": sum(
                    1 for e in recorder.calls if e.get("json", {}).get("has_code_fence")
                ),
            },
            "calls_over_shipped_300s_timeout": [
                {"label": e["label"], "elapsed_seconds": e["elapsed_seconds"]}
                for e in recorder.calls
                if e["elapsed_seconds"] > 300
            ],
        }
        (ARTIFACTS / f"{PREFIX}summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (ARTIFACTS / f"{PREFIX}calls.json").write_text(
            json.dumps(recorder.calls, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print("\n" + json.dumps({k: v for k, v in summary.items() if k != "A_rows"},
                                indent=2, ensure_ascii=False))
        print(f"\nevidence: {ARTIFACTS}")
    finally:
        await client.close()


asyncio.run(main())
