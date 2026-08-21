"""Run 3 of the live LM Studio measurements: **is the instability the model?**

Runs 1 and 2 measured `huihui-qwythos-9b-claude-mythos-5-1m-abliterated` — a Qwen model
merged with Claude Mythos, a lineage known for overthinking. Both runs found reasoning
tokens dominating every reply and swinging wildly across *identical* prompts: **26×** across
run 1's six single-call rolls, **4.9×** across run 2's three. The Director's hypothesis is
that the swing is the model rather than the wording, and that
`gemma-4-26b-a4b-it-heretic-ara-v2` — an MoE with ~4B active parameters, and vision — may be
both steadier and faster.

This file is **run 2's harness with the model swapped**, deliberately. A redesigned
experiment would make the comparison worthless, so `tests/smoke_populate_two_stage_live.py`
is reproduced here with only the deltas the model swap actually needs:

* the model is named explicitly (`MVP_RUN3_MODEL`, defaulting to the Gemma id) instead of
  read from `Settings.llm_model`, so `.env` is never edited and the run cannot silently
  measure whatever happens to be configured;
* a **warm-up call is made and discarded** before anything is measured. Run 1's single
  timeout is now believed to have been a cold model load landing on roll 1; a swapped-in
  model is cold by definition and roll 1 must not pay for that again;
* every call and every populate is **appended to disk the moment it lands**
  (`run3-calls.jsonl`, `run3-progress.jsonl`), so a tool timeout costs the remaining rolls
  and not the finished ones;
* measurement **E** (the JSON ladder) additionally records the *shape* of the reply — which
  keys the assistant message carried — because 19 of 19 replies parsing on rung 1 across
  runs 1 and 2 was a property of that build putting reasoning in a separate
  `reasoning_content` field, and a different model may not;
* measurement **D** calls the shipped `DirectorClient.inspect_image` against a real image in
  this repo. `config.py`'s single `llm_model` means the same model does planning, expansion
  **and** vision, so a planning win that costs vision is not a win.

Everything else — the 60 s song, `populate_required_shots(60)` = 12, `PLAN_TEMPERATURE`
0.7 on every call in both arms and both stages, the 900 s arm timeout, the 1800 s scale
probe timeout, N=3 per arm, **interleaved** arms, the 180 s / 35-shot scale probe, the
shipped constants, the shipped `repair_sections` — is run 2's, unchanged, so the two runs'
numbers sit in one table without re-derivation.

    uv run python tests/smoke_model_swap_gemma_live.py

Not collected by pytest: `smoke_*.py` does not match `python_files`. **Nothing is written
into `data/`, no route is called, and no ComfyUI work of any kind is submitted.** Throwaway
`Project` objects are built in memory and the only I/O besides the model calls is the
evidence dropped under `test-artifacts/2026-08-20-lmstudio-live/` with a `run3-` prefix,
beside runs 1 and 2.
"""

from __future__ import annotations

import asyncio
import json
import os
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

#: Beside runs 1 and 2, in the same dated directory, under a `run3-` prefix.
ARTIFACTS = Path(__file__).resolve().parents[1] / "test-artifacts" / "2026-08-20-lmstudio-live"
PREFIX = "run3-"

#: The model under test. Named here rather than read from `Settings` so this run cannot
#: quietly measure whatever `.env` happens to hold, and so `.env` never has to be edited to
#: run it. Override with `MVP_RUN3_MODEL` to point the same harness at a third model.
MODEL = os.environ.get("MVP_RUN3_MODEL", "gemma-4-26b-a4b-it-heretic-ara-v2")

#: Run 2's song, unchanged. `populate_required_shots(60)` is 12 shots.
SONG_SECONDS = 60.0

#: The scale probe's song. `populate_required_shots(180)` is 35 shots.
SCALE_SECONDS = 180.0

#: Rolls per arm in A. Three. Three is a small sample and the report must say so.
ROLLS = 3

#: Run 2's budget, kept so the numbers are comparable.
TIMEOUT_SECONDS = 900

#: The scale probe's budget. Generous so that "slow" and "hung" are distinguishable.
SCALE_TIMEOUT_SECONDS = 1800

#: Measurement D. A four-view character sheet already in the repo — the exact thing
#: `inspect_image`'s `purpose` argument describes, so a generic reply has no excuse.
VISION_IMAGE = Path(__file__).resolve().parents[1] / "docs" / "asset_940dfee992dd-multiview_00001_.png"


def probe_project() -> Project:
    """Run 2's project, byte-for-byte, so arm A's conditions match runs 1 and 2."""
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
    """Run 2's scale project, byte-for-byte: the same video at three minutes."""
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
    """The **control**: populate's ask exactly as the route builds it with `two_stage` off."""
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


def two_stage_shots_instruction(project: Project, count: int, sections: list[SongSection]) -> str:
    """Stage two of the two-stage populate, assembled as `populate_timeline` assembles it."""
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
    """Every call, with its timing, its token usage and its raw reply kept whole.

    Run 2's recorder plus one thing: each entry is **appended to disk as it lands**. A run
    that dies at roll 5 of 6 should cost roll 6, not rolls 1 to 5.
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self.calls: list[dict[str, Any]] = []
        self.started = time.monotonic()
        self.stream = ARTIFACTS / f"{PREFIX}calls.jsonl"
        self.stream.write_text("", encoding="utf-8")

    def record(self, **fields: Any) -> dict[str, Any]:
        entry = {"n": len(self.calls) + 1, **fields}
        self.calls.append(entry)
        with self.stream.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


def append_progress(row: dict[str, Any]) -> None:
    """One finished populate, on disk before the next one starts."""
    with (ARTIFACTS / f"{PREFIX}progress.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def json_observations(raw: str) -> dict[str, Any]:
    """Measurement E: did rung 1 of the ladder suffice, or did `extract_json` rescue it."""
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
) -> tuple[str, DirectorResult | None, str, dict[str, Any], list[str]]:
    """`DirectorClient.plan`'s request through `DirectorClient`'s own transport.

    Run 2's function with the payload read *before* the content is extracted, so a reply
    whose `content` is not a string still yields its usage and its message keys instead of
    being indistinguishable from a dead socket. The measured semantics are unchanged: `raw`
    stays empty on any failure, and `answered` is still `bool(raw)`.
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
    except Exception as error:  # noqa: BLE001 - a transport failure is a measurement
        return "", None, f"{type(error).__name__}: {error}", {}, []
    usage = payload.get("usage") or {}
    try:
        choice = payload["choices"][0]["message"]
        keys = sorted(k for k, v in choice.items() if v not in (None, "", [], {}))
    except (KeyError, IndexError, TypeError, AttributeError):
        keys = []
    try:
        raw = client._content(response)
    except Exception as error:  # noqa: BLE001 - a null/absent content is a measurement
        return "", None, f"{type(error).__name__}: {error}", usage, keys
    try:
        return raw, DirectorResult.model_validate(extract_json(raw)), "", usage, keys
    except Exception as error:  # noqa: BLE001 - the failure text is the measurement
        return raw, None, f"{type(error).__name__}: {error}", usage, keys


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
    raw, result, parse_error, usage, keys = await plan_capturing_raw(
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
        answered=bool(raw),
        empty_shots=bool(raw) and not shots,
        met_required=bool(raw) and len(shots) >= required,
        sections_returned=len(result.sections) if result else 0,
        reasoning_tokens=reasoning_tokens(usage),
        message_keys=keys,
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
    """One whole two-stage populate: the structure call, `repair_sections`, the shots call."""
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
    row = {
        "tag": tag,
        "arm": "two_stage",
        "required": required,
        "stage_one_sections_raw": len(raw_sections),
        "stage_one_sections_after_repair": len(staged),
        "stage_one_shots_emitted": stage_one["returned"],
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
    append_progress(row)
    return row


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
    row = {
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
    append_progress(row)
    return row


def spread(values: list[float | int | None]) -> dict[str, Any]:
    """Min, median, max and the ratio — the shape run 1's 26× reasoning swing demands."""
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


async def warm_up(client: DirectorClient, project: Project, context: dict[str, Any]) -> dict[str, Any]:
    """One discarded call, so roll 1 does not pay for a cold model.

    Run 1's single 300 s death is now believed to have been a first-touch model load rather
    than a slow generation. A model that was loaded seconds ago is cold by definition, so the
    first call is spent deliberately and thrown away. Its numbers are reported as *discarded*
    — they are evidence about cold starts, not about the arms.
    """
    print("  warm-up (DISCARDED, not counted in any arm) ... ", end="", flush=True)
    started = time.monotonic()
    raw, result, parse_error, usage, keys = await plan_capturing_raw(
        client,
        message=single_call_instruction(project, 12),
        project_context=context,
        temperature=PLAN_TEMPERATURE,
    )
    elapsed = time.monotonic() - started
    shots = len([s for s in (result.shots if result else []) if s.prompt.strip()])
    print(f"{elapsed:.0f}s  shots={shots}  reasoning={reasoning_tokens(usage)}"
          + (f"  FAILED: {parse_error}" if parse_error else ""))
    return {
        "discarded": True,
        "elapsed_seconds": round(elapsed, 1),
        "shots": shots,
        "sections": len(result.sections) if result else 0,
        "reasoning_tokens": reasoning_tokens(usage),
        "message_keys": keys,
        "parse_error": parse_error,
        "usage": usage,
        "json": json_observations(raw) if raw else {},
        "raw": raw,
    }


async def vision_probe(settings: Settings) -> dict[str, Any]:
    """Measurement D: does the *same* model that plans also inspect an image?

    `config.py` carries a single `llm_model`, so planning, expansion and vision inspection
    all run on whatever is configured. A model that plans well and cannot see would trade one
    working feature for another, which is a decision the numbers in A–C cannot speak to. The
    shipped `DirectorClient.inspect_image` is called rather than a hand-rolled request, so
    what is measured is the code path the application actually uses.
    """
    print(f"\nD. vision — {VISION_IMAGE.name}")
    if not VISION_IMAGE.is_file():
        print("   image missing; skipped")
        return {"ran": False, "detail": f"{VISION_IMAGE} does not exist"}
    client = DirectorClient(
        base_url=settings.llm_base_url,
        model=MODEL,
        api_key=settings.llm_api_key,
        timeout=TIMEOUT_SECONDS,
    )
    started = time.monotonic()
    try:
        result = await client.inspect_image(
            image=VISION_IMAGE.read_bytes(),
            mime_type="image/png",
            purpose="character multiview consistency sheet",
        )
        elapsed = time.monotonic() - started
        payload = result.model_dump()
        print(f"   {elapsed:.0f}s  ok — summary: {payload['summary'][:160]}")
        return {
            "ran": True,
            "ok": True,
            "elapsed_seconds": round(elapsed, 1),
            "image": str(VISION_IMAGE),
            "image_bytes": VISION_IMAGE.stat().st_size,
            "inspection": payload,
        }
    except Exception as error:  # noqa: BLE001 - a vision failure is the measurement
        elapsed = time.monotonic() - started
        print(f"   {elapsed:.0f}s  FAILED: {type(error).__name__}: {error}")
        return {
            "ran": True,
            "ok": False,
            "elapsed_seconds": round(elapsed, 1),
            "image": str(VISION_IMAGE),
            "error": f"{type(error).__name__}: {error}",
        }
    finally:
        await client.close()


async def addendum() -> None:
    """The two-stage stage-two ask, with sections **supplied**. Run 3's main pass never
    reached it.

    Gemma failed stage one on all four rolls — it emitted `shots` in answer to a prompt whose
    HARD CONSTRAINT 3 says "leave `shots` empty", and emitted no `sections` at all — so
    `repair_sections` had nothing to keep and every two-stage roll **fell back to the
    combined ask**. The arm labelled "two-stage" in the main pass is therefore three more
    single-call rolls, and the actual question two-stage exists to answer — *does dropping
    the sections ask bring the shots back?* — went unmeasured on this model.

    This closes that hole with three calls and nothing else changed: the stage-two
    instruction exactly as `populate_timeline` builds it, the sections handed in rather than
    asked for, the same temperature, the same timeout. The section map is the one **Gemma
    itself produced** on the main pass's first combined call, passed through the shipped
    `repair_sections` — precisely what stage two would have received had stage one behaved.

        uv run python tests/smoke_model_swap_gemma_live.py addendum

    N=3, an addendum to a run that already settles nothing.
    """
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    settings = Settings()
    project = probe_project()
    assert project.song is not None
    required = populate_required_shots(project.song.duration)
    context = project.model_dump(mode="json", exclude=DIRECTOR_CONTEXT_EXCLUDE)

    source = json.loads((ARTIFACTS / f"{PREFIX}calls.json").read_text(encoding="utf-8"))
    donor = next(entry for entry in source if entry["stage"] == "combined")
    parsed = DirectorResult.model_validate(extract_json(donor["raw"]))
    staged = [
        SongSection(label=label, start=start, duration=length, prompt=prompt)
        for label, start, length, prompt in repair_sections(
            [(item.label, item.start, item.duration, item.prompt) for item in parsed.sections],
            project.song.duration,
        )
    ]
    print("RUN 3 ADDENDUM — two-stage stage 2 with sections SUPPLIED")
    print(f"model={MODEL}  sections from run 3 call {donor['n']}: "
          f"{[s.label for s in staged]}")
    if not staged:
        print("no usable donor sections; nothing to measure")
        return

    client = DirectorClient(
        base_url=settings.llm_base_url,
        model=MODEL,
        api_key=settings.llm_api_key,
        timeout=TIMEOUT_SECONDS,
    )
    rows: list[dict[str, Any]] = []
    try:
        for index in range(1, ROLLS + 1):
            message = two_stage_shots_instruction(project, required, staged)
            print(f"  [{index}] addendum stage 2 (sections supplied) ... ", end="", flush=True)
            started = time.monotonic()
            raw, result, parse_error, usage, keys = await plan_capturing_raw(
                client, message=message, project_context=context, temperature=PLAN_TEMPERATURE
            )
            elapsed = time.monotonic() - started
            shots = [s for s in (result.shots if result else []) if s.prompt.strip()]
            row = {
                "roll": index,
                "elapsed_seconds": round(elapsed, 1),
                "required": required,
                "shots": len(shots),
                "sections_returned": len(result.sections) if result else 0,
                "answered": bool(raw),
                "empty_shots": bool(raw) and not shots,
                "reasoning_tokens": reasoning_tokens(usage),
                "message_keys": keys,
                "parse_error": parse_error,
                "usage": usage,
                "json": json_observations(raw) if raw else {},
                "request": message,
                "raw": raw,
            }
            rows.append(row)
            print(f"{elapsed:.0f}s  shots={len(shots)}/{required}"
                  f"  sections={row['sections_returned']}"
                  f"  reasoning={row['reasoning_tokens']}"
                  + ("  EMPTY SHOTS" if row["empty_shots"] else ""))
            (ARTIFACTS / f"{PREFIX}addendum.json").write_text(
                json.dumps(
                    {
                        "model": MODEL,
                        "sections_supplied": [s.model_dump(mode="json") for s in staged],
                        "rolls": rows,
                        "empty_shots": sum(1 for r in rows if r["empty_shots"]),
                        "shots_per_roll": [r["shots"] for r in rows],
                        "reasoning_spread": spread([r["reasoning_tokens"] for r in rows]),
                        "elapsed_spread": spread([r["elapsed_seconds"] for r in rows]),
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
    finally:
        await client.close()
    print(f"\nempty shots: {sum(1 for r in rows if r['empty_shots'])} of {len(rows)}")
    print(f"evidence: {ARTIFACTS / (PREFIX + 'addendum.json')}")


#: Stage one's ask with **one** thing changed: the numbered HARD CONSTRAINTS are reordered so
#: the `shots` mention is first and the `sections` mention last. Every other word, including
#: the FINAL CHECK, is `POPULATE_SECTIONS_INSTRUCTION`'s own. Nothing shipped is edited — this
#: string exists only inside this smoke, to test one hypothesis about one model's replies.
SECTIONS_INSTRUCTION_REORDERED = """Divide this song into its structural sections, and return \
ONLY that structure. The song is {duration:.1f} seconds long. Work from the lyric sheet's own \
[Tag] blocks in order — Intro, Verse, Chorus, Bridge, Outro, and whatever else it names — one \
section per block, in order, each with `start` and `duration` in seconds, together covering 0 to \
{duration:.1f}, and each with a one-sentence shared visual prompt saying how that part of the \
song looks. Return them in `sections`.
HARD CONSTRAINTS:
1. Leave `shots` empty. This call is about structure only — the shots are asked for separately, \
afterwards.
2. The sections must run in order and must not overlap.
3. `sections` must not be empty.
FINAL CHECK before responding: is `sections` non-empty and in song order? If it is not, fix it, \
and return only the corrected structure."""


async def order_probe() -> None:
    """Why does Gemma answer the *other* half? One cheap, falsifiable test.

    Across run 3's 16 measured calls Gemma emitted exactly one of `shots` and `sections`, never
    both — and which one it emitted tracked, without exception, the **last key named in the
    numbered HARD CONSTRAINTS**, not what the prompt asked for:

    * structure-only ask, last constraint names `shots` ("Leave `shots` empty") → 4 of 4 replies
      carried `shots` and no `sections`, the exact opposite of the ask;
    * combined ask, last constraint names `sections` → 9 of 9 replies carried `sections` and no
      `shots`;
    * shots-only ask (the addendum), last constraint names `shots` → 3 of 3 carried `shots`.

    That is a pattern spotted after the fact in data that was not collected to test it, which is
    the weakest kind of evidence there is. So it gets one controlled manipulation: stage one's
    ask with the numbered constraints **reordered** so the `sections` mention is last and the
    `shots` mention first, and not one other word changed. If the replies flip to `sections`,
    the ordering hypothesis survives a test it could have failed. If they do not, it is dead and
    the real cause is something else — most likely `SYSTEM_PROMPT`, which names
    `message, treatment, style_bible, shots` and never mentions `sections` at all.

        uv run python tests/smoke_model_swap_gemma_live.py order

    Either way this is a **probe reported to whoever owns the prompts**, not a change to them.
    N=3. Nothing shipped is touched.
    """
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    settings = Settings()
    project = probe_project()
    assert project.song is not None
    context = project.model_dump(mode="json", exclude=DIRECTOR_CONTEXT_EXCLUDE)
    message = SECTIONS_INSTRUCTION_REORDERED.format(duration=project.song.duration)
    print("RUN 3 ORDER PROBE — stage one's ask, HARD CONSTRAINTS reordered")
    print(f"model={MODEL}  (baseline: 4 of 4 stage-one replies carried shots, 0 sections)")

    client = DirectorClient(
        base_url=settings.llm_base_url,
        model=MODEL,
        api_key=settings.llm_api_key,
        timeout=TIMEOUT_SECONDS,
    )
    rows: list[dict[str, Any]] = []
    try:
        for index in range(1, ROLLS + 1):
            print(f"  [{index}] reordered structure ask ... ", end="", flush=True)
            started = time.monotonic()
            raw, result, parse_error, usage, keys = await plan_capturing_raw(
                client, message=message, project_context=context, temperature=PLAN_TEMPERATURE
            )
            elapsed = time.monotonic() - started
            shots = [s for s in (result.shots if result else []) if s.prompt.strip()]
            repaired = repair_sections(
                [(i.label, i.start, i.duration, i.prompt) for i in (result.sections if result else [])],
                project.song.duration,
            )
            row = {
                "roll": index,
                "elapsed_seconds": round(elapsed, 1),
                "shots_returned": len(shots),
                "sections_returned": len(result.sections) if result else 0,
                "sections_after_repair": len(repaired),
                "reasoning_tokens": reasoning_tokens(usage),
                "message_keys": keys,
                "parse_error": parse_error,
                "usage": usage,
                "json": json_observations(raw) if raw else {},
                "request": message,
                "raw": raw,
            }
            rows.append(row)
            print(f"{elapsed:.0f}s  shots={len(shots)}  sections={row['sections_returned']}"
                  f"  after repair={len(repaired)}  reasoning={row['reasoning_tokens']}")
            (ARTIFACTS / f"{PREFIX}order-probe.json").write_text(
                json.dumps(
                    {
                        "model": MODEL,
                        "hypothesis": (
                            "the reply carries whichever of `shots`/`sections` is named last in "
                            "the numbered HARD CONSTRAINTS, regardless of what is asked for"
                        ),
                        "baseline_stage_one": "4 of 4 replies carried shots, 0 sections",
                        "rolls": rows,
                        "rolls_returning_sections": sum(
                            1 for r in rows if r["sections_returned"]
                        ),
                        "rolls_returning_shots": sum(1 for r in rows if r["shots_returned"]),
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
    finally:
        await client.close()
    print(f"\nrolls returning sections: {sum(1 for r in rows if r['sections_returned'])} of "
          f"{len(rows)}  (baseline was 0 of 4)")
    print(f"evidence: {ARTIFACTS / (PREFIX + 'order-probe.json')}")


async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    settings = Settings()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / f"{PREFIX}progress.jsonl").write_text("", encoding="utf-8")
    project = probe_project()
    assert project.song is not None
    required = populate_required_shots(project.song.duration)
    context = project.model_dump(mode="json", exclude=DIRECTOR_CONTEXT_EXCLUDE)
    recorder = Recorder(MODEL)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    print("RUN 3 — the same measurement on a different model")
    print(f"model={MODEL}  (configured model in .env: {settings.llm_model})")
    print(f"base={settings.llm_base_url}  utc={stamp}")
    print(
        f"song={project.song.duration:.0f}s  required shots={required}  rolls per arm={ROLLS}"
        f"  temperature={PLAN_TEMPERATURE}  timeout={TIMEOUT_SECONDS}s"
    )

    client = DirectorClient(
        base_url=settings.llm_base_url,
        model=MODEL,
        api_key=settings.llm_api_key,
        timeout=TIMEOUT_SECONDS,
    )
    (ARTIFACTS / f"{PREFIX}instructions.txt").write_text(
        f"=== RUN 3 === model: {MODEL}\n\n"
        f"--- SINGLE CALL (control, shipped default), required={required} ---\n"
        f"{single_call_instruction(project, required)}\n\n"
        f"--- TWO-STAGE, STAGE 1 (structure only) ---\n"
        f"{POPULATE_SECTIONS_INSTRUCTION.format(duration=project.song.duration)}\n\n"
        "--- TWO-STAGE, STAGE 2 (shots) — assembled per roll from stage one's sections; "
        "the exact text of every stage-two call is in run3-calls.jsonl ---\n",
        encoding="utf-8",
    )

    populates: list[dict[str, Any]] = []
    scale: list[dict[str, Any]] = []
    warm: dict[str, Any] = {}
    vision: dict[str, Any] = {}
    try:
        print("\n0. warm-up")
        warm = await warm_up(client, project, context)
        (ARTIFACTS / f"{PREFIX}warmup.json").write_text(
            json.dumps(warm, indent=2, ensure_ascii=False), encoding="utf-8"
        )

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
            model=MODEL,
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

        vision = await vision_probe(settings)
        (ARTIFACTS / f"{PREFIX}vision.json").write_text(
            json.dumps(vision, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    finally:
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
                "longest_single_call_seconds": [row["longest_single_call_seconds"] for row in rows],
                "total_seconds_spread": spread([row["total_seconds"] for row in rows]),
                "longest_call_spread": spread(
                    [row["longest_single_call_seconds"] for row in rows]
                ),
                "reasoning_tokens_stage_one": [row["reasoning_tokens_stage_one"] for row in rows],
                "reasoning_tokens_stage_two": [row["reasoning_tokens_stage_two"] for row in rows],
                "reasoning_spread_stage_one": spread(
                    [row["reasoning_tokens_stage_one"] for row in rows]
                ),
                "reasoning_spread_stage_two": spread(
                    [row["reasoning_tokens_stage_two"] for row in rows]
                ),
                "reasoning_tokens_spread_all_calls": spread(
                    [row["reasoning_tokens_stage_one"] for row in rows]
                    + [row["reasoning_tokens_stage_two"] for row in rows]
                ),
            }

        summary = {
            "run": 3,
            "question": "is the reasoning instability the model? gemma vs qwythos",
            "model": MODEL,
            "model_configured_in_env": settings.llm_model,
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
            "warm_up_discarded": warm,
            "total_measured_calls": len(recorder.calls),
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
            "D_vision": vision,
            "E_json": {
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
                "message_keys_seen": sorted(
                    {key for e in recorder.calls for key in e.get("message_keys", [])}
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
        print("\n" + json.dumps(
            {k: v for k, v in summary.items() if k not in ("A_rows", "warm_up_discarded")},
            indent=2, ensure_ascii=False))
        print(f"\nevidence: {ARTIFACTS}")
        await client.close()


MODES = {"addendum": addendum, "order": order_probe}
asyncio.run(MODES.get(sys.argv[1] if sys.argv[1:2] else "", main)())
