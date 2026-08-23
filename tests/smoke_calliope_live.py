"""Live LM Studio evidence for the three prompt-wording changes that landed 2026-08-20.

Three of that day's changes are *prompt text* — populate's count enforcement, its guided
retry, and `APPEARANCE_ANCHOR_RULES` — and none of them had ever been handed to a real
model. The suite verifies the strings are assembled and sent; only a live roll can say
whether the loaded model obeys them. A fourth change, `extract_json`'s ladder, is
insurance whose necessity is unmeasured, so every call here also records what the raw
reply actually looked like.

`smoke_assistant_lmstudio.py`'s shape, for four questions instead of one. **Nothing is
written into `data/` and no route is called**: throwaway `Project` objects are built in
memory, the shipped constants are imported rather than transcribed, and the only I/O
besides the model calls is the evidence dropped under
`test-artifacts/2026-08-20-lmstudio-live/`. No ComfyUI work of any kind.

    uv run python tests/smoke_calliope_live.py

Not collected by pytest: `smoke_*.py` does not match `python_files`, exactly as the four
existing live smokes avoid collection.

What it measures, and the honest caveat on each:

A. **Count enforcement.** Three rolls of the shipped, count-enforced populate ask against
   three rolls of a control with the count enforcement surgically removed. N=3 per arm
   settles nothing on its own; it is the first evidence there has ever been.
B. **The guided retry.** Any short roll from A buys one real `POPULATE_RETRY_PREFIX`
   retry at `POPULATE_RETRY_TEMPERATURE`, exactly as the route spends it. If no roll comes
   back short, one deliberately oversized ask is used to provoke a shortfall — and "it
   never came back short" is itself reported, because that is the headline.
C. **The appearance anchor.** One expansion with an anchor stored on a cited Asset and
   `APPEARANCE_ANCHOR_RULES` active, one control with the anchor cleared and the rules off.
   The failure the rule exists to stop — inventing hair, clothing, age, build or face for a
   subject that has no anchor — is checked for in both.
D. **The JSON ladder.** For every call: was the raw content clean JSON, did it carry
   reasoning chatter or a code fence, and did `extract_json` recover something a bare
   `json.loads` refused.

Roughly a dozen model calls, each of which can take minutes: the loaded model reasons
before answering (2026-08-19 regression — `enable_thinking: false` stopped suppressing it)
and `DirectorClient`'s timeout is 300 s. Progress is printed as it goes.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from music_video_producer.app import (
    DIRECTOR_CONTEXT_EXCLUDE,
    POPULATE_FINAL_CHECK,
    POPULATE_INSTRUCTION,
    POPULATE_MAX_WINDOW_SECONDS,
    POPULATE_RETRY_PREFIX,
    POPULATE_RETRY_TEMPERATURE,
    POPULATE_SECTIONS_ASK,
    POPULATE_SECTIONS_CONSTRAINT,
    POPULATE_SHORT_COUNT_PROBLEM,
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
from music_video_producer.h3_expansion_prompt import APPEARANCE_ANCHOR_RULES, system_prompt
from music_video_producer.models import Asset, AssetCitation, Project, Shot, Song
from music_video_producer.timeline import H3_MIN_SHOT_SECONDS, shot_expansion_input

#: Where the raw evidence lands, matching the dated-directory convention already in
#: `test-artifacts/`. Written to, and nothing else on disk is.
ARTIFACTS = Path(__file__).resolve().parents[1] / "test-artifacts" / "2026-08-20-lmstudio-live"

#: Deliberately modest. `populate_required_shots(60)` is 12 shots — inside the band where
#: the roadmap's run-2 under-delivery was recorded, and a fraction of the cost of rerolling
#: the 32-shot layout that produced it.
SONG_SECONDS = 60.0

#: How many rolls per arm of measurement A. Three, and three is a small sample; the report
#: is required to say so rather than to round a 2-of-3 up into a fact.
ROLLS = 3

#: The count asked for when no roll of A comes back short on its own and the guided retry
#: therefore has nothing to recover. Large enough to reproduce the recorded failure mode.
PROVOCATION_COUNT = 32

#: **Deliberately larger than the shipped 300 s, and the departure is a measurement in its
#: own right.** The first attempt at this run used `DirectorClient`'s own default and the
#: very first count-enforced 12-shot roll died on `httpx.ReadTimeout` at 300 s. Measured on
#: this machine the same minute: the loaded model generates at **10.2 tokens/second**
#: (800 completion tokens in 78.6 s), and it reasons before answering — the 2026-08-19
#: regression — so a reply carrying a reasoning phase plus twelve shots and five sections
#: is several thousand tokens and therefore several hundred seconds. A probe that keeps the
#: shipped budget measures the budget and nothing else; this one raises it so the *wording*
#: can be measured, and reports the timeout separately. That timeout is a finding to hand
#: back, not a thing to fix from here.
TIMEOUT_SECONDS = 900


def probe_project() -> Project:
    """A project in the state populate is *for*: brief, treatment, style bible, lyric sheet,
    a small asset library, and no shots yet. In memory only — never saved, never a manifest."""
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


def shipped_instruction(project: Project, count: int) -> str:
    """The populate ask **as the route builds it**, assembled from the shipped constants.

    Mirrors `populate_timeline`'s assembly for the no-sections-known case: the instruction,
    the sections ask and constraint (this project has no marked sections), and
    `POPULATE_FINAL_CHECK` appended last so the count is the final thing read. The route's
    two-stage structure pass is not run — this probe is about the shots call.
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


def control_instruction(project: Project, count: int) -> str:
    """The same ask with the **count enforcement surgically removed**, and nothing else changed.

    Built by string surgery on the shipped text rather than by pasting the pre-change
    constant, deliberately: the historical pre-change wording (commit `0a03afc`) also used a
    4–15 s window band, asked for no `sections`, and had no numbered HARD CONSTRAINTS block
    at all, so a comparison against it would confound four differences into one number. What
    is removed here is exactly the three count-enforcement sites the 2026-08-20 entry claims
    credit for, replaced by the old soft phrasing:

    - the opening `Return EXACTLY {count} shots.` becomes the pre-change `about {count} shots
      in total` — the *same number*, stated as guidance rather than as a demand;
    - HARD CONSTRAINT 1 (`shots` must contain EXACTLY N entries) is dropped and the rest
      renumbered;
    - `POPULATE_FINAL_CHECK` is not appended.

    Everything else — the coverage sentence, the window band, the length-mixing sentence, the
    asset roster, the camera-variation rules, the sections ask, the surviving constraints —
    is byte-identical to the shipped arm. This is a reconstruction, not a historical text,
    and the report has to say so.
    """
    shipped = shipped_instruction(project, count)
    text = shipped.replace(f"Return EXACTLY {count} shots. ", "", 1)
    # The band's own numbers, taken from the shipped constants rather than spelled again: this
    # anchor was `"each between 4 and 6 seconds."` and silently stopped matching when the
    # ceiling moved to 6.8, which turns a reconstruction into a copy of the shipped arm without
    # failing. Asserted below rather than trusted.
    band = (
        f"each between {H3_MIN_SHOT_SECONDS:g} and {POPULATE_MAX_WINDOW_SECONDS:g} seconds."
    )
    assert band in text, "the shipped window-band sentence moved"
    text = text.replace(band, f"{band[:-1]}, about {count} shots in total.", 1)
    hard_constraint_one = (
        f"1. `shots` must contain EXACTLY {count} entries. Not fewer. A shorter list is a "
        "failed answer, however good the individual shots are.\n"
    )
    assert hard_constraint_one in text, "the shipped HARD CONSTRAINT 1 wording moved"
    text = text.replace(hard_constraint_one, "", 1)
    assert POPULATE_FINAL_CHECK.format(count=count) in text, "the shipped FINAL CHECK moved"
    text = text.replace(POPULATE_FINAL_CHECK.format(count=count), "", 1)
    # Renumber what is left so the control does not read as a list with a hole in it.
    for old, new in (("\n2. ", "\n1. "), ("\n3. ", "\n2. "), ("\n4. ", "\n3. ")):
        text = text.replace(old, new, 1)
    return text


class Recorder:
    """Every call this run makes, with the timing and the raw reply kept.

    The point is measurement D and the reporting discipline together: the raw content is
    what says whether the JSON ladder was needed, and it is also the evidence the report is
    required to quote. Nothing is summarised away before it is written down.
    """

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
    """What measurement D needs from one raw reply, computed without touching the model.

    `bare_json_loads_ok` is the whole question: it is true exactly when rung 1 of the ladder
    would have sufficed and the other rungs are insurance. When it is false and
    `extract_json_ok` is true, the ladder recovered a reply the pre-change code would have
    turned into a `DirectorError`.
    """
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
    leading = stripped[: max(0, stripped.find("{"))] if "{" in stripped else stripped
    return {
        "bare_json_loads_ok": bare_ok,
        "extract_json_ok": ladder_ok,
        "ladder_rescued_the_reply": ladder_ok and not bare_ok,
        "has_code_fence": "```" in stripped,
        "chars_before_first_brace": len(leading),
        "leading_chatter": leading[:400],
        "raw_chars": len(stripped),
    }


async def plan_capturing_raw(
    client: DirectorClient,
    *,
    message: str,
    project_context: dict[str, Any],
    temperature: float,
) -> tuple[str, DirectorResult | None, str, dict[str, Any]]:
    """`DirectorClient.plan`'s request, sent through `DirectorClient`'s own transport, with
    the raw content and the token usage handed back as well as the parsed result.

    `plan` returns only the validated `DirectorResult`, and measurement D needs the bytes
    that came back. So the body is assembled here exactly as `plan` assembles it — the same
    imported `SYSTEM_PROMPT`, the same user-turn JSON envelope, the same strict
    `response_format` built from the same `DirectorResult` schema — and posted through the
    shipped `_completion`, so the unloaded-model retry and the schema-free fallback are the
    real ones rather than a second implementation of them. Only the return shape differs.

    `usage` is kept because `reasoning_tokens` is the number that explains the wall clock:
    at the 10.2 tok/s measured on this machine, a reply's cost is its *total* tokens and the
    reasoning phase is most of them. A transport failure — the 300 s `ReadTimeout` this run
    hit on its first attempt — is returned as an ordinary result rather than raised, so one
    slow roll cannot take the other eleven measurements down with it.
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


async def roll(
    client: DirectorClient,
    recorder: Recorder,
    *,
    label: str,
    message: str,
    context: dict[str, Any],
    temperature: float,
    required: int,
) -> dict[str, Any]:
    """One populate-shaped call, timed, counted and written down."""
    print(f"  [{len(recorder.calls) + 1}] {label} ... ", end="", flush=True)
    started = time.monotonic()
    raw, result, parse_error, usage = await plan_capturing_raw(
        client, message=message, project_context=context, temperature=temperature
    )
    elapsed = time.monotonic() - started
    shots = [shot for shot in (result.shots if result else []) if shot.prompt.strip()]
    entry = recorder.record(
        label=label,
        temperature=temperature,
        elapsed_seconds=round(elapsed, 1),
        required=required,
        returned=len(shots),
        # A call that never returned is not a short plan; it is no plan, and rolling it
        # into the count column would report a transport failure as a wording result.
        answered=bool(raw),
        met_required=bool(raw) and len(shots) >= required,
        sections_returned=len(result.sections) if result else 0,
        performance_true=sum(1 for shot in shots if getattr(shot, "performance", False)),
        parse_error=parse_error,
        usage=usage,
        json=json_observations(raw),
        request=message,
        raw=raw,
    )
    print(
        f"{elapsed:.0f}s  shots={len(shots)}/{required}  sections={entry['sections_returned']}"
        f"  performance={entry['performance_true']}"
        f"  clean_json={entry['json']['bare_json_loads_ok']}"
        + (f"  PARSE FAIL: {parse_error}" if parse_error else "")
    )
    return entry


async def expansion(
    client: DirectorClient,
    recorder: Recorder,
    *,
    label: str,
    project: Project,
    shot: Shot,
    anchors: bool,
) -> dict[str, Any]:
    """One H3 expansion, through the shipped `expand_shot` and the shipped system prompt.

    `system_prompt(appearance_anchors=...)` and `shot_expansion_input` are the route's own
    two halves: the route turns `appearance_anchors` on exactly when the payload actually
    carries an `anchor` key, so passing the same boolean here keeps the rule and the data
    agreeing the way the route makes them agree.
    """
    payload = shot_expansion_input(project, shot)
    prompt = system_prompt(appearance_anchors=anchors)
    print(f"  [{len(recorder.calls) + 1}] {label} ... ", end="", flush=True)
    started = time.monotonic()
    error = ""
    text = ""
    try:
        text = await client.expand_shot(shot_input=payload, system_prompt=prompt)
    except Exception as failure:  # noqa: BLE001 - a failed expansion is a result
        error = f"{type(failure).__name__}: {failure}"
    elapsed = time.monotonic() - started
    entry = recorder.record(
        label=label,
        temperature=0.6,
        elapsed_seconds=round(elapsed, 1),
        anchor_rules_sent=APPEARANCE_ANCHOR_RULES in prompt,
        anchor_in_payload=any("anchor" in ref for ref in payload["shot"].get("references", []))
        if isinstance(payload.get("shot"), dict)
        else "anchor" in json.dumps(payload),
        error=error,
        request=json.dumps(payload, ensure_ascii=False, indent=2),
        raw=text,
    )
    print(f"{elapsed:.0f}s  chars={len(text)}" + (f"  ERROR: {error}" if error else ""))
    return entry


#: The anchor the Director would have typed. Distinctive on purpose: "chestnut" and "oxblood"
#: are words the model has no reason to produce unless it was handed them, so their presence
#: in the written prompt is evidence of use rather than of coincidence.
ANCHOR = "a woman in her late twenties with a chestnut undercut and an oxblood leather jacket"

#: Appearance words the anchor does not license. Counted in the *control* reply, where no
#: anchor exists and the rule says to describe no appearance at all — any hit there is the
#: invention the rule was written to stop.
APPEARANCE_WORDS = (
    "hair", "haired", "blonde", "brunette", "redhead", "ponytail", "beard", "eyes",
    "young", "teenage", "middle-aged", "slender", "slim", "muscular", "tall", "petite",
    "dress", "jacket", "shirt", "jeans", "coat", "hoodie", "skirt", "boots",
)


def anchor_findings(text: str) -> dict[str, Any]:
    body = text.lower()
    return {
        "anchor_verbatim": ANCHOR.lower() in body,
        "anchor_key_words": [word for word in ("chestnut", "undercut", "oxblood") if word in body],
        "appearance_words_present": sorted({word for word in APPEARANCE_WORDS if word in body}),
        "first_mia_mention": (
            text[max(0, body.find("mia")) : max(0, body.find("mia")) + 240]
            if "mia" in body
            else ""
        ),
    }


async def main() -> None:
    # The prompts and the replies are full of em dashes and typographic quotes, and this
    # console is cp1252. Without this the run dies on a print rather than on a measurement.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    settings = Settings()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    project = probe_project()
    assert project.song is not None
    required = populate_required_shots(project.song.duration)
    # The route's own dump, exclusion included, so the model sees what populate shows it —
    # not a wider view that would make this probe test a context nothing sends.
    context = project.model_dump(mode="json", exclude=DIRECTOR_CONTEXT_EXCLUDE)
    recorder = Recorder(settings.llm_model)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    print(f"model={settings.llm_model}  base={settings.llm_base_url}  utc={stamp}")
    print(f"song={project.song.duration:.0f}s  required shots={required}  rolls per arm={ROLLS}")

    client = DirectorClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        timeout=TIMEOUT_SECONDS,
    )
    shipped = shipped_instruction(project, required)
    control = control_instruction(project, required)
    (ARTIFACTS / "instructions.txt").write_text(
        f"=== SHIPPED (count-enforced), required={required} ===\n{shipped}\n\n"
        f"=== CONTROL (count enforcement removed), required={required} ===\n{control}\n",
        encoding="utf-8",
    )

    try:
        print("\nA. count enforcement — shipped wording")
        shipped_rolls = [
            await roll(
                client,
                recorder,
                label=f"A/shipped roll {index}",
                message=shipped,
                context=context,
                temperature=PLAN_TEMPERATURE,
                required=required,
            )
            for index in range(1, ROLLS + 1)
        ]

        print("\nA. control — same ask, count enforcement removed")
        for index in range(1, ROLLS + 1):
            await roll(
                client,
                recorder,
                label=f"A/control roll {index}",
                message=control,
                context=context,
                temperature=PLAN_TEMPERATURE,
                required=required,
            )

        print("\nB. guided retry")
        # A roll that never answered is not a short plan and must not buy the retry: the
        # retry names a shortfall in numbers, and there is no number to name.
        short = [
            entry for entry in shipped_rolls if entry["answered"] and not entry["met_required"]
        ]
        if short:
            # The route's own retry, verbatim: the shortfall named in numbers ahead of the
            # unchanged instruction, at the lower retry temperature. One retry, as the route
            # spends exactly one.
            worst = min(short, key=lambda entry: entry["returned"])
            problems = POPULATE_SHORT_COUNT_PROBLEM.format(
                returned=worst["returned"], required=required
            )
            await roll(
                client,
                recorder,
                label="B/retry after a natural shortfall",
                message=POPULATE_RETRY_PREFIX.format(problems=problems) + shipped,
                context=context,
                temperature=POPULATE_RETRY_TEMPERATURE,
                required=required,
            )
        else:
            print("  no shipped roll came back short — provoking one with a larger ask")
            provoked = await roll(
                client,
                recorder,
                label=f"B/provocation at {PROVOCATION_COUNT}",
                message=shipped_instruction(project, PROVOCATION_COUNT),
                context=context,
                temperature=PLAN_TEMPERATURE,
                required=PROVOCATION_COUNT,
            )
            if not provoked["met_required"]:
                problems = POPULATE_SHORT_COUNT_PROBLEM.format(
                    returned=provoked["returned"], required=PROVOCATION_COUNT
                )
                await roll(
                    client,
                    recorder,
                    label="B/retry after the provoked shortfall",
                    message=POPULATE_RETRY_PREFIX.format(problems=problems)
                    + shipped_instruction(project, PROVOCATION_COUNT),
                    context=context,
                    temperature=POPULATE_RETRY_TEMPERATURE,
                    required=PROVOCATION_COUNT,
                )

        print("\nC. appearance anchor")
        anchored = probe_project()
        anchored.assets[0].consistency_prompt = ANCHOR
        anchored.shots = [
            Shot(
                id="shot_anchor",
                start=0,
                duration=5,
                prompt="Mia leans against the grey estate car under the sodium underpass, "
                "singing the chorus straight down the lens as the camera pushes in.",
                mode="references",
                singing="singing",
                citations=[
                    AssetCitation(asset_id="asset_lead"),
                    AssetCitation(asset_id="asset_underpass"),
                ],
            )
        ]
        with_anchor = await expansion(
            client,
            recorder,
            label="C/expansion WITH anchor",
            project=anchored,
            shot=anchored.shots[0],
            anchors=True,
        )
        bare = probe_project()
        bare.shots = [shot.model_copy(deep=True) for shot in anchored.shots]
        without_anchor = await expansion(
            client,
            recorder,
            label="C/expansion WITHOUT anchor (control)",
            project=bare,
            shot=bare.shots[0],
            anchors=False,
        )

        summary = {
            "model": settings.llm_model,
            "base_url": settings.llm_base_url,
            "utc": stamp,
            "song_seconds": project.song.duration,
            "required_shots": required,
            "rolls_per_arm": ROLLS,
            "total_calls": len(recorder.calls),
            "total_elapsed_seconds": round(recorder.elapsed, 1),
            "A_shipped": [
                {key: entry[key] for key in ("returned", "required", "answered",
                                             "met_required", "sections_returned",
                                             "performance_true", "elapsed_seconds",
                                             "parse_error", "usage")}
                for entry in recorder.calls
                if entry["label"].startswith("A/shipped")
            ],
            "A_control": [
                {key: entry[key] for key in ("returned", "required", "answered",
                                             "met_required", "sections_returned",
                                             "performance_true", "elapsed_seconds",
                                             "parse_error", "usage")}
                for entry in recorder.calls
                if entry["label"].startswith("A/control")
            ],
            "B": [
                {key: entry[key] for key in ("label", "returned", "required", "met_required")}
                for entry in recorder.calls
                if entry["label"].startswith("B/")
            ],
            "C_with_anchor": anchor_findings(with_anchor["raw"]),
            "C_without_anchor": anchor_findings(without_anchor["raw"]),
            "D_json": {
                "calls_with_json_replies": sum(
                    1 for entry in recorder.calls if "json" in entry
                ),
                "clean_bare_json_loads": sum(
                    1 for entry in recorder.calls if entry.get("json", {}).get("bare_json_loads_ok")
                ),
                "ladder_rescued": sum(
                    1
                    for entry in recorder.calls
                    if entry.get("json", {}).get("ladder_rescued_the_reply")
                ),
                "replies_with_fences": sum(
                    1 for entry in recorder.calls if entry.get("json", {}).get("has_code_fence")
                ),
            },
        }
        (ARTIFACTS / "summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (ARTIFACTS / "calls.json").write_text(
            json.dumps(recorder.calls, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print("\n" + json.dumps(summary, indent=2, ensure_ascii=False))
        print(f"\nevidence: {ARTIFACTS}")
    finally:
        await client.close()


asyncio.run(main())
