"""Run 6 of the live LM Studio measurements: **which model writes the H3 prompt?**

Run 5 measured *populate* on the fixed schema across three models and recommended
``gemma-4-26b-a4b-it-heretic-ara-v2``. It measured **expansion on nothing** — run 5's own
confound list says so in as many words ("Expansion was not exercised at all"). Expansion is
a different shape of task and it is the longest-running LLM work in the application: it
writes the actual H3 document that a render is conditioned on, one call per shot, with up to
`EXPANSION_ATTEMPTS` corrective retries behind each answer. Populate's ranking may not
transfer, so the Director ruled: measure expansion on all three, then switch `.env`.

Usage -- one model at a time, because a model swap costs a load::

    uv run python tests/smoke_expansion_bakeoff_live.py plan
    uv run python tests/smoke_expansion_bakeoff_live.py qwythos
    uv run python tests/smoke_expansion_bakeoff_live.py summary

`.env` is **not edited by this file**. The model under test is named here, so the run cannot
silently measure whatever happens to be configured. Nothing is written into ``data/``, no
route is called, no project on disk is read or modified, and no ComfyUI work of any kind is
submitted.

What is measured, and how it differs from run 5
-----------------------------------------------

Expansion output is a **document with a grammar**, and `h3_prompt.check` is the shipped,
objective decision procedure for it. Run 5 had to grade populate's prose by eye. This run
does not: every answer goes through the same checker `app.attempt_expansion` uses, with the
same arguments, and "well_formed" is the headline number.

The production loop is used rather than reimplemented. `app.attempt_expansion` is called
directly -- its retry budget, its corrective-turn shape, its checker arguments, its
`song_audio_prose` short circuit. The only additions are observers:

* `RecordingClient` subclasses `DirectorClient` and overrides `_completion` to time each
  HTTP call and keep its `usage` block and its raw content. It changes no request byte.
* `app.h3_check` is wrapped with a recorder for the duration of the run, so the *exact*
  `ParsedPrompt` the loop decided on -- not a re-derived one -- is what gets reported.

The shot set
------------

Five shots, **identical for every model**, modelled on the Third Video project's shape
(read-only) but built in memory. They are chosen to span the axes the brief names:

===== ===================== ============ ================= =========== ====================
Shot  Mode                  References   Anchor on a ref?  Singing     What it tests
===== ===================== ============ ================= =========== ====================
S1    ``first_last``        2 pics+song  yes (Picture 1)   singing     instruction line,
                                                                       keyframe anchors,
                                                                       forbid_dialogue,
                                                                       audio tag, anchor
S2    ``references``        2 pictures   yes (Picture 1)   not_singing anchor, multi-ref
S3    ``references``        3 pictures   yes (Picture 1),  not_singing does it INVENT an
                                         **no** anchor on              appearance for the
                                         Picture 2                     un-anchored person?
S4    ``references``        2 pictures   **none**          unknown     control: the anchor
                                                                       rules are ABSENT
S5    ``text_to_video``     none         n/a               unknown     no tags at all
===== ===================== ============ ================= =========== ====================

S4 is the control for `APPEARANCE_ANCHOR_RULES` itself: `attempt_expansion` appends that
block only when the payload actually carries an ``anchor`` key, so S4's system prompt is
byte-for-byte the one this application sent before the rules existed. S3 is the sharp test --
the rules *are* present and one subject in the shot has no anchor, which is the case the
rules' second bullet exists for.

**Song-audio references shots are deliberately not in the set.** `attempt_expansion`
short-circuits those to `song_audio_prose`, a deterministic string with no model call, so
measuring one would be measuring the application rather than the model. S1 rides the master
song but is a keyframe mode, which keeps the document path -- that is the real shape a
singing shot reaches the model in.

Sample size
-----------

**N=5 per model: five shots, one roll each.** Five settles little. Note also that unlike run
5 -- five rolls of one identical prompt -- the spread here is measured across five
*different* payloads, so wall-clock and reasoning variance mixes sampling noise with genuine
task difficulty. Where that matters it is said again in the report.

A **warm-up call is made and discarded** on every model, recorded as ``discarded: true``
rather than merely claimed. Each shot's result is appended to ``run6-calls.jsonl`` as it
lands, so a harness that dies halfway leaves every shot it completed.
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

import httpx

from music_video_producer import app as app_module
from music_video_producer.app import EXPANSION_ATTEMPTS, attempt_expansion
from music_video_producer.config import Settings
from music_video_producer.director import DirectorClient, extract_json
from music_video_producer.h3_expansion_prompt import (
    APPEARANCE_ANCHOR_RULES,
    KEYFRAME_REFERENCE_RULES,
)
from music_video_producer.h3_expansion_prompt import system_prompt as h3_system_prompt
from music_video_producer.models import Asset, AssetCitation, Project, Shot, Song, SongSection
from music_video_producer.timeline import shot_expansion_input

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "test-artifacts" / "2026-08-20-lmstudio-live"
PREFIX = "run6-"
CALLS = ARTIFACTS / f"{PREFIX}calls.jsonl"

#: The shipped `DirectorClient` timeout. Kept at run 5's 900 s for the measurement so a slow
#: model is *recorded* as slow rather than truncated; calls over 300 s are listed separately.
TIMEOUT_SECONDS = 900
SHIPPED_TIMEOUT_SECONDS = 300

#: Confirmed against `GET /v1/models` on this host before the run.
MODELS: dict[str, str] = {
    "qwythos": "huihui-qwythos-9b-claude-mythos-5-1m-abliterated",
    "gemma": "gemma-4-26b-a4b-it-heretic-ara-v2",
    "qwen35b": "huihui-qwen3.6-35b-a3b-claude-4.7-opus-abliterated-mtp",
}

#: The Director's own words for what the lead looks like, as `Asset.consistency_prompt` would
#: hold them. Distinctive on purpose: every clause here is checkable in the output, and none of
#: it is inferable from the brief, so "the anchor survived" is a string test rather than a
#: judgement. No project on disk carries an anchor yet, which is why this is written here.
ANCHOR_TEXT = (
    "a woman in her late twenties with waist-length copper hair, a black leather corset "
    "and a silver studded belt"
)
#: Phrases from the anchor that a model carrying it must reproduce. Checked individually so a
#: partial carry is reported as partial rather than as a failure.
ANCHOR_PHRASES: tuple[str, ...] = (
    "copper hair",
    "black leather corset",
    "silver studded belt",
    "late twenties",
)

#: Appearance vocabulary a model must NOT produce for a subject it was given no anchor for.
#: Deliberately a word list rather than a judgement: it is a screen, and every hit is printed
#: in context in the evidence so a reader can overrule it.
APPEARANCE_WORDS: tuple[str, ...] = (
    "blonde", "brunette", "redhead", "ponytail", "braid", "buzzcut", "dreadlock",
    "beard", "stubble", "moustache", "mustache", "freckle", "tattoo", "piercing",
    "blue eyes", "green eyes", "brown eyes", "hazel", "dark eyes", "pale skin",
    "tanned", "olive skin", "teenage", "middle-aged", "elderly", "young man",
    "young woman", "twenties", "thirties", "forties", "slender", "muscular",
    "stocky", "petite", "burly", "lanky", "t-shirt", "hoodie", "denim", "jeans",
    "leather jacket", "flannel", "tank top", "miniskirt", "boots", "sneakers",
    "bandana", "baseball cap",
)


def probe_project() -> Project:
    """The fixture, built in memory. Nothing under `data/` is read or written.

    Modelled on `project_59f14d19ff10` ("Harder Faster — Third Video"): its brief, its song
    caption and duration, its section map (taken from the Second Video's seven marked
    sections, which is the same song). The assets are that project's three, plus one extra
    un-anchored character so S3 can hold an anchored and an un-anchored subject at once.
    """
    project = Project(name="Run 6 expansion bake-off (in memory)")
    project.creative_brief = (
        "Create a rock music video where it alternates between our female character singing "
        "at a standing microphone during the verses with action shots and laying on a bed in "
        "the back during the chorus with glamour camera angles and occasional posed singing "
        "shots on the bed mixed in. Set in a darker moonlit empty warehouse with a black "
        "draped canopy bed in the center."
    )
    project.treatment = (
        "Two spaces, one room. The mic stand downstage in a cone of moonlight, where the "
        "verses are performed straight to camera. The canopy bed upstage under black drapery, "
        "where the choruses are held in long glamour takes. A single bare bulb hangs over the "
        "mic stand; the bed is lit only by moonlight through the high warehouse windows."
    )
    project.style_bible = (
        "Moonlit near-monochrome with electric-blue highlights, hard rim light, deep "
        "shadow, anamorphic flare, 35mm grain, shallow depth."
    )
    project.song = Song(
        title="Harder Faster (Female Cover)",
        source="imported",
        duration=154.644898,
        caption=(
            "Hard rock, powerful female lead vocal, driving electric guitars and drums, raw "
            "energetic delivery, 80s metal cover energy."
        ),
    )
    project.sections = [
        SongSection(id="sec_intro", label="Intro", start=0.0, duration=11.0,
                    prompt="Empty room, no performer yet; the space breathes."),
        SongSection(id="sec_v1", label="Verse", start=11.0, duration=21.54,
                    prompt="At the mic stand, downstage, hard and direct."),
        SongSection(id="sec_c1", label="Chorus", start=32.54, duration=23.82,
                    prompt="On the bed, upstage, long glamour takes."),
        SongSection(id="sec_v2", label="Verse 2", start=56.36, duration=20.84,
                    prompt="Back at the mic, faster cutting, more movement."),
        SongSection(id="sec_c2", label="Chorus 2", start=77.2, duration=26.0,
                    prompt="On the bed again, wider, the drapery moving."),
        SongSection(id="sec_bridge", label="Bridge", start=103.2, duration=20.9,
                    prompt="Neither space; details, hardware, the room itself."),
        SongSection(id="sec_outro", label="Outro", start=124.1, duration=30.54,
                    prompt="The room empties out; the bulb is the last thing lit."),
    ]
    project.assets = [
        # The anchored lead. This is the only asset in the fixture carrying an anchor.
        Asset(
            id="asset_lead",
            name="HarderFaster",
            kind="character",
            path="media/lead.png",
            consistency_prompt=ANCHOR_TEXT,
        ),
        Asset(id="asset_bed", name="Dusk Warehouse Bed", kind="setting", path="media/bed.png"),
        Asset(id="asset_mic", name="Chrome standing microphone", kind="prop",
              path="media/mic.png"),
        # Un-anchored on purpose: S3's second subject, and the case
        # `APPEARANCE_ANCHOR_RULES`' second bullet exists for.
        Asset(id="asset_guitarist", name="The guitarist", kind="character",
              path="media/guitarist.png"),
    ]
    project.shots = [WARMUP_SHOT, *MEASURED_SHOTS]
    return project


def _shot(**kwargs: Any) -> Shot:
    kwargs.setdefault("asset_ids", [])
    kwargs.setdefault("citations", [])
    kwargs.setdefault("reference_labels", {})
    return Shot(**kwargs)


#: Discarded. A cold load is believed to be what killed run 1's roll 1, so every model pays
#: for one call before anything is counted. Deliberately the simplest shot in the set.
WARMUP_SHOT = _shot(
    id="shot_warmup",
    start=0.0,
    duration=4.0,
    mode="text_to_video",
    singing="unknown",
    prompt=(
        "INTRO — STATIC WIDE of the empty moonlit warehouse; dust hanging in the light from "
        "the high windows, nobody in frame yet."
    ),
)

MEASURED_SHOTS: list[Shot] = [
    # S1 -- keyframe mode, master song, singing, anchored first frame.
    _shot(
        id="shot_s1_keyframe_anchored_singing",
        start=11.0,
        duration=5.665,
        mode="first_last",
        singing="singing",
        use_song_audio=True,
        asset_ids=["asset_lead", "asset_bed"],
        citations=[
            AssetCitation(asset_id="asset_lead", role="first", order=0),
            AssetCitation(asset_id="asset_bed", role="last", order=1),
        ],
        prompt=(
            "VERSE 1 — HANDHELD PUSH-IN that starts tight on HarderFaster at the mic stand "
            "and ends wide on the canopy bed upstage as she turns away from the lens."
        ),
    ),
    # S2 -- references, no song audio, not singing, anchored lead plus a prop.
    _shot(
        id="shot_s2_references_anchored_notsinging",
        start=103.2,
        duration=4.0,
        mode="references",
        singing="not_singing",
        asset_ids=["asset_lead", "asset_mic"],
        citations=[
            AssetCitation(asset_id="asset_lead", role="reference", order=0),
            AssetCitation(asset_id="asset_mic", role="reference", order=1),
        ],
        prompt=(
            "BRIDGE — STATIC CLOSE on HarderFaster's hands wrapping the chrome mic stand; "
            "she does not sing here, she just grips it and lets the room ring."
        ),
    ),
    # S3 -- references, three pictures, one anchored subject and one un-anchored subject.
    _shot(
        id="shot_s3_mixed_anchor_notsinging",
        start=108.0,
        duration=6.0,
        mode="references",
        singing="not_singing",
        asset_ids=["asset_lead", "asset_guitarist", "asset_bed"],
        citations=[
            AssetCitation(asset_id="asset_lead", role="reference", order=0),
            AssetCitation(asset_id="asset_guitarist", role="reference", order=1),
            AssetCitation(asset_id="asset_bed", role="reference", order=2),
        ],
        prompt=(
            "BRIDGE — SLOW ARC around HarderFaster and the guitarist standing back to back "
            "in front of the canopy bed; neither of them sings, the guitarist plays and she "
            "listens."
        ),
    ),
    # S4 -- control: no anchor on any reference, so APPEARANCE_ANCHOR_RULES is not appended.
    _shot(
        id="shot_s4_unanchored_control",
        start=124.1,
        duration=4.5,
        mode="references",
        singing="unknown",
        asset_ids=["asset_bed", "asset_mic"],
        citations=[
            AssetCitation(asset_id="asset_bed", role="reference", order=0),
            AssetCitation(asset_id="asset_mic", role="reference", order=1),
        ],
        prompt=(
            "OUTRO — LOW STATIC WIDE of the canopy bed with the chrome mic stand abandoned "
            "in the foreground; a figure crosses the far end of the room and is gone."
        ),
    ),
    # S5 -- text to video. No reference media of any kind, so no tag is legal anywhere.
    _shot(
        id="shot_s5_text_to_video",
        start=129.0,
        duration=5.0,
        mode="text_to_video",
        singing="unknown",
        prompt=(
            "OUTRO — the bare bulb over the empty mic stand swinging to a stop, moonlight "
            "raking the concrete, the warehouse settling into silence."
        ),
    ),
]


class RecordingClient(DirectorClient):
    """`DirectorClient`, plus a log of every HTTP completion it made.

    `_completion` is the single choke point every director call goes through, so overriding
    it observes the production path without touching a request byte: the body handed up is
    the body `expand_shot` built, and the response handed back is the one it would have got.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[dict[str, Any]] = []

    async def _completion(
        self, *, body: dict[str, Any], headers: dict[str, str]
    ) -> httpx.Response:
        started = time.monotonic()
        record: dict[str, Any] = {
            "elapsed_seconds": None,
            "usage": {},
            "reasoning_tokens": None,
            "raw": "",
            "reasoning_chars": 0,
            "status_code": None,
            "transport_error": "",
            "is_retry": any(message["role"] == "assistant" for message in body["messages"]),
            "temperature": body.get("temperature"),
            "max_tokens": body.get("max_tokens"),
        }
        self.calls.append(record)
        try:
            response = await super()._completion(body=body, headers=headers)
        except Exception as error:  # a transport failure is a measurement
            record["elapsed_seconds"] = round(time.monotonic() - started, 1)
            record["transport_error"] = f"{type(error).__name__}: {error}"
            raise
        record["elapsed_seconds"] = round(time.monotonic() - started, 1)
        record["status_code"] = response.status_code
        try:
            payload = response.json()
            usage = payload.get("usage") or {}
            record["usage"] = usage
            details = usage.get("completion_tokens_details") or {}
            value = details.get("reasoning_tokens")
            record["reasoning_tokens"] = value if isinstance(value, int) else None
            message = payload["choices"][0]["message"]
            record["raw"] = (message.get("content") or "").strip()
            record["reasoning_chars"] = len((message.get("reasoning_content") or "").strip())
        except Exception as error:  # noqa: BLE001 - an unreadable body is a measurement too
            record["transport_error"] = f"reading the body: {type(error).__name__}: {error}"
        return response


def json_observations(raw: str) -> dict[str, Any]:
    """Measurement F, carried forward from runs 1-5: is `extract_json`'s ladder ever used.

    Expansion returns a **document, not JSON**, so `bare_json_loads_ok` is expected to be
    false on every call and is not the question. The question is whether any expansion reply
    arrives fenced or wrapped -- the same packaging failure the ladder exists for -- so the
    fence and preamble counters are the ones that carry meaning here.
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
    return {
        "bare_json_loads_ok": bare_ok,
        "extract_json_ok": ladder_ok,
        "has_code_fence": "```" in stripped,
        "raw_chars": len(stripped),
    }


def anchor_observations(text: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Measurement C: did the stored anchor survive, and was any appearance invented.

    ``anchored`` is read off the payload the model was actually handed rather than off the
    project, exactly as `attempt_expansion` decides whether to append the rules -- so "the
    rules were present" and "the anchor was measured" cannot disagree.
    """
    references = payload["shot"].get("references", [])
    anchored = [reference for reference in references if "anchor" in reference]
    lowered = text.lower()
    hits = sorted({word for word in APPEARANCE_WORDS if word in lowered})
    # Where each invented-appearance word landed, so a reader can overrule the screen.
    contexts = []
    for word in hits:
        index = lowered.find(word)
        contexts.append(text[max(0, index - 90): index + len(word) + 90].replace("\n", " "))
    phrases = {phrase: (phrase.lower() in lowered) for phrase in ANCHOR_PHRASES}
    # The anchor's exact stored bytes, and the first-mention rule. `first_mention_carries` is
    # true when the anchor text appears within 200 characters of the subject's first naming --
    # the rule says apposition, and apposition is adjacency.
    verbatim = ANCHOR_TEXT.lower() in lowered
    name_index = lowered.find("harderfaster")
    anchor_index = lowered.find(ANCHOR_TEXT.lower())
    return {
        "rules_appended": bool(anchored),
        "anchored_tags": [reference["tag"] for reference in anchored],
        "anchor_verbatim_present": verbatim,
        "anchor_phrases_present": phrases,
        "anchor_phrases_hit": sum(1 for present in phrases.values() if present),
        "subject_named": name_index >= 0,
        "first_mention_carries_anchor": (
            name_index >= 0 and anchor_index >= 0 and abs(anchor_index - name_index) <= 200
        ),
        "appearance_words_found": hits,
        "appearance_word_contexts": contexts,
    }


def append(entry: dict[str, Any]) -> None:
    """One shot's outcome, on disk, before the next one starts."""
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with CALLS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


async def run_model(model_key: str, start: int = 0, end: int | None = None) -> None:
    """One model's shots, addressable by index so a slow model can be chunked.

    Index 0 is the discarded warm-up. Chunking changes nothing that is measured: each shot is
    an independent `attempt_expansion` against a fresh client, and every result is already
    appended as it lands.
    """
    model_id = MODELS[model_key]
    settings = Settings()
    project = probe_project()
    print(
        f"RUN 6 -- expansion bake-off\n"
        f"model={model_id}  (.env says {settings.llm_model})\n"
        f"base={settings.llm_base_url}  shots={len(MEASURED_SHOTS)} + 1 discarded warm-up  "
        f"EXPANSION_ATTEMPTS={EXPANSION_ATTEMPTS}  timeout={TIMEOUT_SECONDS}s"
    )
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    write_instructions(project)

    checks: list[Any] = []
    real_check = app_module.h3_check

    def recording_check(*args: Any, **kwargs: Any):
        result = real_check(*args, **kwargs)
        checks.append((kwargs, result))
        return result

    app_module.h3_check = recording_check  # observer only; restored in the finally below
    try:
        for order, shot in enumerate(project.shots):
            if order < start or (end is not None and order >= end):
                continue
            discarded = shot.id == WARMUP_SHOT.id
            client = RecordingClient(
                base_url=settings.llm_base_url,
                model=model_id,
                api_key=settings.llm_api_key,
                timeout=TIMEOUT_SECONDS,
            )
            payload = shot_expansion_input(project, shot)
            system = h3_system_prompt(
                expect_instruction=shot.mode in app_module.H3_KEYFRAME_MODES,
                keyframe_references=(
                    app_module.resolve_shot_mode(shot) == "references"
                    and any(c.role in ("first", "last") for c in shot.citations)
                ),
                appearance_anchors=any(
                    "anchor" in reference
                    for reference in payload["shot"].get("references", [])
                ),
            )
            checks.clear()
            started = time.monotonic()
            try:
                outcome = await attempt_expansion(project, shot, director=client)
            finally:
                await client.close()
            wall = time.monotonic() - started

            attempts = [
                {
                    **call,
                    "check": describe(checks[index][1]) if index < len(checks) else None,
                    "json": json_observations(call["raw"]) if call["raw"] else {},
                }
                for index, call in enumerate(client.calls)
            ]
            entry = {
                "model": model_key,
                "model_id": model_id,
                "order": order,
                "discarded": discarded,
                "shot_id": shot.id,
                "shot_mode": app_module.resolve_shot_mode(shot),
                "shot_duration": shot.duration,
                "shot_singing": shot.singing,
                "shot_use_song_audio": shot.use_song_audio,
                "reference_slots": app_module.reference_slot_counts(project, shot),
                "system_prompt_chars": len(system),
                "anchor_rules_appended": APPEARANCE_ANCHOR_RULES in system,
                "keyframe_rules_appended": KEYFRAME_REFERENCE_RULES in system,
                "kind": outcome.kind,
                "well_formed": outcome.kind == "expanded",
                "model_calls": len(client.calls),
                "attempts_reported": outcome.attempts,
                "wall_seconds": round(wall, 1),
                "elapsed_per_call": [call["elapsed_seconds"] for call in attempts],
                "reasoning_per_call": [call["reasoning_tokens"] for call in attempts],
                "final_problems": list(outcome.problems),
                "detail": outcome.detail,
                "text": outcome.text,
                "anchor": anchor_observations(outcome.text, payload) if outcome.text else {},
                "attempt_records": attempts,
                "payload": payload,
                "utc": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            print(
                f"[{model_key}] {shot.id}"
                f"{' (WARM-UP, DISCARDED)' if discarded else ''}: "
                f"{wall:.0f}s  kind={outcome.kind}  calls={len(client.calls)}  "
                f"reasoning={entry['reasoning_per_call']}  "
                f"anchor_rules={entry['anchor_rules_appended']}  "
                f"problems={len(outcome.problems)}"
            )
            for problem in outcome.problems:
                print(f"    ! {problem[:170]}")
            append(entry)
    finally:
        app_module.h3_check = real_check


def describe(parsed: Any) -> dict[str, Any]:
    """One `ParsedPrompt` as data. `kind` buckets the problem by which check produced it."""
    return {
        "well_formed": parsed.well_formed,
        "fields_found": sorted(parsed.fields),
        "instruction": parsed.instruction[:400],
        "problems": [
            {
                "field": problem.field,
                "fatal": problem.fatal,
                "kind": classify(problem),
                "message": problem.message,
            }
            for problem in parsed.problems
        ],
    }


#: Problem kinds, mapped from the checker's own message text. The checker reports a `field`
#: and a sentence; the sentence is what names the rule, so bucketing reads it. Any message the
#: table does not recognise is reported as `other` **with its text**, so a new kind of failure
#: shows up as an unbucketed sentence rather than being silently folded into a neighbour.
def classify(problem: Any) -> str:
    message = problem.message
    if "must be numbered in order" in message or "th marker" in message:
        return "shot_numbering"
    if "must not carry a timestamp" in message:
        return "shot1_timestamp"
    if "has no cut time" in message:
        return "missing_cut_time"
    if "does not advance" in message:
        return "cut_times_not_monotonic"
    # Before the shot-marker bound below, which shares the "at or beyond the" wording. A stray
    # time past the clip's end is a different failure from a *marked* cut past it — the model
    # lost the clip length either way, but only one of them is a cut at all — and folding the
    # two would hide which one a model actually makes.
    if "no such moment in the clip" in message:
        return "stray_cut_past_duration"
    if "at or beyond the" in message:
        return "cut_time_past_duration"
    if "No [Shot 1] opening" in message:
        return "no_shot_1"
    if "has no [Shot N] in front of it" in message:
        return "orphan_cut"
    if "is a cut time written into" in message:
        return "cut_time_in_sound_field"
    if "Dialogue tags are unbalanced" in message:
        return "unbalanced_dialogue"
    if "no language tag" in message:
        return "dialogue_language_tag"
    if "may contain no" in message and "<d>" in message:
        return "dialogue_on_song_audio_shot"
    if "sentences; the guide asks for" in message:
        return "sound_field_sentence_bounds"
    if "is missing." in message:
        return "field_missing"
    if "is empty." in message:
        return "field_empty"
    if "appears mid-line" in message:
        return "field_mid_line"
    if "appears more than once" in message:
        return "field_duplicated"
    if "out of order" in message:
        return "fields_out_of_order"
    if "No core fields found" in message:
        return "no_core_fields"
    if "retention_analysis contains a speaker id" in message:
        return "retention_speaker_id"
    if "reference slots are numbered from" in message:
        return "reference_tag_zero"
    if "conditions the render on a slot nothing fills" in message:
        return "reference_tag_over_bounds"
    if "but the prompt never mentions it" in message:
        return "reference_tag_unmentioned"
    if "requires an instruction line" in message:
        return "instruction_missing"
    if "takes no instruction line" in message:
        return "instruction_unexpected"
    return "other"


def write_instructions(project: Project) -> None:
    """The exact payload and system prompt every model receives, written once."""
    path = ARTIFACTS / f"{PREFIX}instructions.txt"
    if path.exists():
        return
    blocks = ["=== RUN 6 — EXPANSION ===\n"]
    for shot in project.shots:
        payload = shot_expansion_input(project, shot)
        system = h3_system_prompt(
            expect_instruction=shot.mode in app_module.H3_KEYFRAME_MODES,
            keyframe_references=(
                app_module.resolve_shot_mode(shot) == "references"
                and any(c.role in ("first", "last") for c in shot.citations)
            ),
            appearance_anchors=any(
                "anchor" in reference for reference in payload["shot"].get("references", [])
            ),
        )
        blocks.append(
            f"--- {shot.id} "
            f"({'WARM-UP, DISCARDED' if shot.id == WARMUP_SHOT.id else 'measured'}) ---\n"
            f"mode={app_module.resolve_shot_mode(shot)} duration={shot.duration} "
            f"singing={shot.singing} use_song_audio={shot.use_song_audio}\n"
            f"reference_slots={app_module.reference_slot_counts(project, shot)}\n\n"
            f"USER PAYLOAD (verbatim, as `timeline.shot_expansion_input` built it):\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
            f"SYSTEM PROMPT ({len(system)} chars):\n{system}\n"
        )
    path.write_text("\n\n".join(blocks), encoding="utf-8")


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
    """Fold `run6-calls.jsonl` into `run6-summary.json`. Warm-ups are excluded by name."""
    rows = [json.loads(line) for line in CALLS.read_text(encoding="utf-8").splitlines() if line]
    measured = [row for row in rows if not row["discarded"]]
    per_model: dict[str, Any] = {}
    for key, model_id in MODELS.items():
        mine = [row for row in measured if row["model"] == key]
        if not mine:
            continue
        kinds: dict[str, int] = {}
        for row in mine:
            for record in row["attempt_records"]:
                for problem in (record.get("check") or {}).get("problems", []):
                    if not problem["fatal"]:
                        continue
                    kinds[problem["kind"]] = kinds.get(problem["kind"], 0) + 1
        first_try = [row for row in mine if row["well_formed"] and row["model_calls"] == 1]
        never = [row for row in mine if not row["well_formed"]]
        anchored = [row for row in mine if row.get("anchor", {}).get("rules_appended")]
        per_model[key] = {
            "model_id": model_id,
            "shots": len(mine),
            "well_formed": sum(1 for row in mine if row["well_formed"]),
            "well_formed_first_attempt": len(first_try),
            "never_well_formed": [row["shot_id"] for row in never],
            "model_calls_total": sum(row["model_calls"] for row in mine),
            "calls_per_shot": {row["shot_id"]: row["model_calls"] for row in mine},
            "kind_per_shot": {row["shot_id"]: row["kind"] for row in mine},
            "fatal_problem_kinds": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
            "wall_seconds_per_shot": {row["shot_id"]: row["wall_seconds"] for row in mine},
            "call_elapsed": [
                value for row in mine for value in row["elapsed_per_call"] if value is not None
            ],
            "call_elapsed_spread": spread(
                [value for row in mine for value in row["elapsed_per_call"]]
            ),
            "reasoning": [
                value for row in mine for value in row["reasoning_per_call"] if value is not None
            ],
            "reasoning_spread": spread(
                [value for row in mine for value in row["reasoning_per_call"]]
            ),
            "anchor": {
                "shots_with_rules": [row["shot_id"] for row in anchored],
                "verbatim_carry": {
                    row["shot_id"]: row["anchor"]["anchor_verbatim_present"] for row in anchored
                },
                "phrases_hit": {
                    row["shot_id"]: row["anchor"]["anchor_phrases_hit"] for row in anchored
                },
                "first_mention_carries": {
                    row["shot_id"]: row["anchor"]["first_mention_carries_anchor"]
                    for row in anchored
                },
                "appearance_words_found": {
                    row["shot_id"]: row["anchor"].get("appearance_words_found", [])
                    for row in mine
                    if row.get("anchor")
                },
                # The screen minus the anchor's own vocabulary. `ANCHOR_TEXT` itself contains
                # "late twenties", so a model that carried the anchor correctly trips the raw
                # screen on a word it was *told* to write. Subtracting the anchor's own words
                # is what turns the screen into a measure of **invention**; the raw list is
                # kept above so the subtraction can be checked rather than trusted.
                "invented_appearance_words": {
                    row["shot_id"]: [
                        word
                        for word in row["anchor"].get("appearance_words_found", [])
                        if word not in ANCHOR_TEXT.lower()
                    ]
                    for row in mine
                    if row.get("anchor")
                },
            },
        }
    every_call = [
        record for row in rows for record in row["attempt_records"] if record.get("raw")
    ]
    summary = {
        "run": 6,
        "question": "which model should `MVP_LLM_MODEL` name, measured on H3 expansion?",
        "utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "expansion_attempts_ceiling": EXPANSION_ATTEMPTS,
        "shots_per_model": len(MEASURED_SHOTS),
        "shot_ids": [shot.id for shot in MEASURED_SHOTS],
        "timeout_seconds": TIMEOUT_SECONDS,
        "shipped_director_timeout_seconds": SHIPPED_TIMEOUT_SECONDS,
        "warmups_discarded": sum(1 for row in rows if row["discarded"]),
        "measured_shots": len(measured),
        "measured_model_calls": sum(row["model_calls"] for row in measured),
        "total_model_calls_including_warmups": sum(row["model_calls"] for row in rows),
        "models": per_model,
        "F_json_ladder": {
            "replies_with_content": len(every_call),
            "replies_with_code_fence": sum(
                1 for record in every_call if record["json"].get("has_code_fence")
            ),
            "replies_that_are_bare_json": sum(
                1 for record in every_call if record["json"].get("bare_json_loads_ok")
            ),
            "note": (
                "Expansion returns a document, not JSON, so `extract_json` is never called on "
                "these replies by the production path -- `expand_shot` returns the text "
                "unparsed. The counter that carries meaning is the code fence, which is the "
                "packaging failure the ladder exists for."
            ),
        },
        "calls_over_shipped_300s_timeout": [
            {"model": row["model"], "shot_id": row["shot_id"], "elapsed_seconds": value}
            for row in rows
            for value in row["elapsed_per_call"]
            if value is not None and value > SHIPPED_TIMEOUT_SECONDS
        ],
    }
    (ARTIFACTS / f"{PREFIX}summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nevidence: {ARTIFACTS}")


def main() -> None:
    # The prompts and replies carry em dashes and typographic quotes; this console is cp1252.
    # Without this the run dies on a print rather than on a measurement.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    argv = sys.argv[1:]
    if argv and argv[0] == "summary":
        summarise()
        return
    if argv and argv[0] == "plan":
        project = probe_project()
        write_instructions(project)
        for shot in project.shots:
            payload = shot_expansion_input(project, shot)
            references = payload["shot"].get("references", [])
            print(
                shot.id,
                app_module.resolve_shot_mode(shot),
                f"dur={shot.duration}",
                f"singing={shot.singing}",
                f"song_audio={shot.use_song_audio}",
                f"slots={app_module.reference_slot_counts(project, shot)}",
                f"refs={[(r['tag'], r['role'], 'anchor' in r) for r in references]}",
            )
        return
    if not argv or argv[0] not in MODELS:
        raise SystemExit(
            f"usage: {Path(__file__).name} <{'|'.join(MODELS)}|summary|plan> [start] [end]"
        )
    start = int(argv[1]) if len(argv) > 1 else 0
    end = int(argv[2]) if len(argv) > 2 else None
    asyncio.run(run_model(argv[0], start, end))


main()
