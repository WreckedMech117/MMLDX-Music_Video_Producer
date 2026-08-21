"""Whether a Shot Plan is fit to submit, and what its renders have done since — AD-5's module.

The readiness half is pure: it reads a `Project` and returns a report. Nothing is written back
onto the manifest, because readiness is computable from the prompts themselves and anything
computable from the manifest is computed rather than persisted — a stored `ready` flag is a
second source of truth that goes stale the moment a prompt is edited.

The reconciliation half below it is AD-1's: one implementation of "what did ComfyUI do to this
project's jobs", used by the polling endpoint and delegated to by the per-job route, so
`Shot.status` has exactly one writer on the completion path. Its decisions
(`queue_locations`, `apply_job_history`, `render_status_report`) are pure; only
`reconcile_render_jobs` touches the network, through the client it is handed.

`timeline.py` must never import this module; this module may import `timeline.py`. The
dependency runs one way so the window math stays a pure leaf.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

from pydantic import BaseModel, Field

from .comfy import ComfyError, HistoryResult
from .models import Project, RenderJob, Shot, ShotStatus, shot_label
from .timeline import (
    H3_FPS,
    H3_MAX_SHOT_SECONDS,
    H3_MIN_SHOT_SECONDS,
    ordered_shots,
    over_render_frames,
)

#: Re-exported, not redefined. `shot_label` moved to `models.py` when `timeline.assistant_input`
#: needed it — `timeline` may not import this module — and every existing importer says
#: `from .batch import shot_label`, so the name stays here and there is still exactly one
#: implementation. See `models.shot_label` for why both halves of the name are carried.
__all__ = [
    "NEAR_DUPLICATE_OVERLAP",
    "NOTE_KIND_PROMPT",
    "NOTE_KIND_SAMENESS",
    "NOTE_KIND_WINDOW_LONG",
    "NOTE_KIND_WINDOW_SHORT",
    "PLACEHOLDER_PROMPT",
    "READINESS_REFUSAL",
    "TERMINAL_JOB_STATUSES",
    "JobProgress",
    "ReadinessNote",
    "ReadinessReport",
    "RenderReconciliation",
    "RenderStatusReport",
    "apply_job_history",
    "prompt_is_missing",
    "prompt_rejection",
    "queue_locations",
    "readiness_refusal",
    "readiness_report",
    "reconcilable_jobs",
    "reconcile_render_jobs",
    "render_status_report",
    "shot_label",
    "supersede_target_jobs",
    "window_band_note",
]

#: Token overlap above which two prompts are reported as lacking variance. Jaccard —
#: shared tokens over the tokens of the pair — rather than intersection-over-smallest,
#: which scores a one-line prompt fully contained in a paragraph as a perfect duplicate.
#: The comparison is strictly greater than this: exactly 0.9 is not a warning.
NEAR_DUPLICATE_OVERLAP = 0.9

#: The prompt `app.js` writes onto every Shot it creates, and that duplicating a Shot copies.
#: It is not a prompt anyone wrote, and it is the *most common* unrenderable state in the
#: application — far more common than `""`, because reaching `""` takes a deliberate deletion
#: while this arrives by default on every new Shot. Treated exactly as blank; see
#: `prompt_rejection`.
PLACEHOLDER_PROMPT = "New shot"

#: How many Shots one refusal names before it stops listing and counts the rest. A batch over
#: twenty blocked Shots would otherwise render as an unreadable wall of ids in a toast.
REFUSAL_NAME_LIMIT = 5

#: How many variance warnings one report carries. Sameness is pairwise, so N Shots sharing one
#: prompt produce C(N,2) notes — twenty produce 190 — and a report nobody can read is a report
#: nobody reads. The overflow is counted rather than dropped silently; see `warnings_omitted`.
VARIANCE_WARNING_LIMIT = 12

#: Words, for the overlap comparison. Splitting on non-word characters rather than on spaces is
#: what makes `"A singer turns."` and `"A singer turns"` compare as the same prompt; splitting on
#: whitespace leaves the full stop attached and scores that pair at 0.75, below the threshold.
_WORD = re.compile(r"\w+", re.UNICODE)

# Why one Shot blocks. Named per Shot, because "the plan is not ready" is not something a
# Director can act on and "shot_x has no prompt" is. Two sentences rather than one, because the
# placeholder and a genuinely blank prompt are different situations to be in: one means nobody
# has written this Shot yet, the other means its text was cleared.
SHOT_WITHOUT_PROMPT = (
    "This shot has no prompt. Submitting it would spend a full GPU pass and return noise."
)
SHOT_WITH_PLACEHOLDER_PROMPT = (
    f'This shot still carries the "{PLACEHOLDER_PROMPT}" placeholder every new shot is created '
    "with, which is not a prompt anyone wrote. Submitting it would spend a full GPU pass on it."
)

# The one case that names no Shot, because there are none to name. An empty plan is not ready
# for the opposite reason to a blocked one: nothing is wrong with any Shot, there is no Shot.
PLAN_WITHOUT_SHOTS = (
    "This project has no shots, so there is nothing to submit. Add shots to the timeline first."
)

# Sameness warns and never blocks. Two Shots may legitimately carry one prompt — a deliberate
# repeated beat, or a pair the Director is about to differentiate — so this is information,
# not a gate. Split in two because "identical" and "similar" are different facts about the
# plan: differing case or spacing is *identical*, and reporting it as merely similar would
# invite a hunt for a difference that is not there.
SHOTS_SHARE_ONE_PROMPT = (
    "These shots carry the same prompt once case and spacing are ignored, so they will render "
    "as two takes of one shot. This does not block submission."
)
SHOTS_LACK_VARIANCE = (
    "These shots share more than "
    f"{NEAR_DUPLICATE_OVERLAP:.0%}"
    " of their prompt words, so they are unlikely to read as different shots. This does not "
    "block submission."
)

# --------------------------------------------------------------------------------------------
# Windows outside H3's trained band. **Neither of these blocks**, and they are reported here so
# the Director reads them while planning rather than discovering them at a submission.
#
# Before this, a window outside 4-15 s reached exactly one surface: a `warnings` string from
# `timeline.build_director_timeline`, which only the Compile dry run prints. The Director's own
# project carried five hand-timed sub-4-second windows and `readiness_report` said `ready: True`
# and nothing else about any of them.
#
# The two directions are genuinely different situations, so they are two sentences:
#
# * **Short is handled.** The Director's ruling of 2026-08-20 made a short window legitimate
#   music-video editing — micro-cuts are an editing practice, not a mistake — and
#   `timeline.over_render_frames` now floors every render at H3's minimum while
#   `timeline.over_render_lead` centres the take on the window. The only cost is render time,
#   and naming it is the whole content of the note.
# * **Long has no such trick.** A 20 s window has to be *generated* as 20 s: there is no buffer
#   to hide, and H3's motion and lipsync are trained for windows up to `H3_MAX_SHOT_SECONDS`.
SHOT_WINDOW_BELOW_BAND = (
    "This shot's window is {duration:.3f}s, under the {minimum:g}s MiniMax H3 is trained for. "
    "It renders anyway: the take is generated at H3's minimum ({frames} frames, "
    "{rendered:.3f}s) centred on this window, and the {buffer:.3f}s around it is invisible "
    "buffer that the trim discards, so the exposed cut is still exactly this window. It costs "
    "a full-length render for a short cut, and nothing else. This does not block submission."
)
#: **Reported and never enforced** — the Director's ruling of 2026-08-20: "I dont anticipate a
#: shot being requested over 15 seconds, when dragging a clip past that it should turn yellow
#: but we arent dead yet." A warning state, not a refusal.
#:
#: Three reasons it could not be a block anyway, and the first settles it: `generate_h3` accepts
#: this window. The route refuses only above the *node's* 3600-frame ceiling (150 s), so a block
#: here would make the pre-flight stricter than the gate it exists to mirror — and this module's
#: own docstring names that as the failure it prevents, in the other direction ("three chances
#: for the pre-flight to say yes to something the route then refuses"). A Director would simply
#: submit shot by shot and get a 202 each time. Second, the band is a *training* range and not a
#: hard limit: a long take degrades, and degraded is not the noise an empty prompt returns, which
#: is what the blocking half of this report means. Third, shot length is a creative decision made
#: elsewhere, which is exactly the argument `ReadinessReport` already records for letting
#: sameness warn rather than gate.
#:
#: **The extender is named as unreachable, and the state is stated exactly.** The Director's
#: ruling continues "we should have a video extender workflow already which may work if fed the
#: last bit of the video we are extending", and the honest answer is *half*: the graph is built
#: — `workflows.build_ltx25_extend_payload` reproduces the audited
#: `ltx25-videoextender-user-export.json` node for node — but **nothing in the application calls
#: it.** No route submits it, and `models.SHOT_MODE_SPECS["extend"]` carries `adapter=""`, so an
#: over-long shot cannot be rendered by extension today no matter what a Director does in the
#: interface. Saying "not built" would be as wrong as saying it works; the sentence says which
#: half exists, because `README.md`'s honest-status convention is about exactly this distance
#: between an adapter and a feature.
SHOT_WINDOW_ABOVE_BAND = (
    "This shot's window is {duration:.3f}s, past the {maximum:g}s MiniMax H3 is trained for. "
    "A short window is covered by rendering H3's minimum and hiding the buffer around it; "
    "there is no matching trick for a long one, because the whole {duration:.3f}s has to be "
    "generated. It will still submit and render — expect motion and lipsync to drift late in "
    "the take. Rendering a shorter take and extending it is the intended fix and is not "
    "reachable: the LTX 2.5 extension graph is built and audited, but no route submits it and "
    "the extend shot mode carries no adapter. Split the shot, or accept the drift. This does "
    "not block submission."
)

#: What kind of thing a `ReadinessNote` is, for a reader that has to *draw* it rather than print
#: it. The Director asked for a clip dragged past `H3_MAX_SHOT_SECONDS` to "turn yellow", and a
#: surface cannot colour from a sentence without matching on its words — which is how a browser
#: starts describing a rule the server no longer has. The two window kinds are separate values
#: because the two states are separate: one is handled and one is not.
NOTE_KIND_PROMPT = "prompt"
NOTE_KIND_SAMENESS = "sameness"
NOTE_KIND_WINDOW_SHORT = "window_short"
NOTE_KIND_WINDOW_LONG = "window_long"

# The one refusal wording, used by every path that can submit a Shot: the single-Shot route and
# the whole-batch client check. `api.js`'s READINESS_REFUSAL is the frontend half and a contract
# test asserts the two templates are identical — two hand-written refusals for one rule is how
# the browser starts describing a gate the server no longer has.
#
# Written to read for one Shot and for many, because a batch refusal names every blocking Shot
# and a single submission names one, and the Director must not be reading two different
# sentences about the same rule. "No prompt" is true of the placeholder too: it is text the
# application wrote, not text a Director wrote, and the timeline flags it the same way.
#
# Deliberately ASCII. The frontend half is compared to this one by reading api.js through node,
# whose stdout is decoded with the platform encoding on Windows; a typographic dash would come
# back mangled and the test that holds the two wordings together would fail for the wrong reason.
READINESS_REFUSAL = (
    "Not submitted: no prompt on {shots}. An empty prompt spends a full GPU pass and returns "
    "noise, so nothing was sent to ComfyUI. Write a prompt in the shot inspector, or run the "
    "Director's shot expansion, then submit again."
)


def readiness_refusal(names: list[str]) -> str:
    """The refusal naming the Shots that block, from the one wording above.

    `names` are display names — `shot_label`'s output at the route, raw ids from a client that
    has only ids. Naming is deliberately not done in here: this formats one sentence, and the
    caller decides what a Shot is called, so the browser and the server can share the template
    without also sharing a labelling scheme that needs a whole `Project` to compute.

    An empty list is a real input, not a caller error: the empty-plan note carries no ids, so
    every id-extractor returns `[]` for it. It renders the empty-plan sentence rather than
    "no prompt on ." — the one thing that state actually means.
    """
    if not names:
        return PLAN_WITHOUT_SHOTS
    listed = ", ".join(names[:REFUSAL_NAME_LIMIT])
    remaining = len(names) - REFUSAL_NAME_LIMIT
    if remaining > 0:
        listed = f"{listed} and {remaining} more"
    return READINESS_REFUSAL.format(shots=listed)


@dataclass(slots=True)
class ReadinessNote:
    """One thing the report has to say, and the Shots it is about.

    `shot_ids` carries one id for a block, two for a sameness pair, and **none** for the empty
    plan — which is the whole reason blocks and warnings share a shape rather than being a flat
    list of ids. A plan-level problem that had to name a Shot would have to invent one.

    `labels` is the same Shots under the names the Director sees, positionally aligned with
    `shot_ids`. It is carried rather than recomputed client-side so that the browser and the
    server name a Shot identically without the browser reimplementing `shot_label`.

    `kind` is the note's category as a value — one of the `NOTE_KIND_*` constants — for the
    surfaces that have to *draw* a note rather than print it. `reason` is the sentence and
    stays the only thing anyone displays; `kind` is what a clip colours from. It defaults to
    `""` so that a note built positionally by an older caller is unchanged, and every note this
    module builds sets it.
    """

    shot_ids: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    reason: str = ""
    kind: str = ""


@dataclass(slots=True)
class ReadinessReport:
    """Whether this plan may be submitted, and everything the Director needs to fix it.

    Derived on every call and never stored — see the module docstring. `ready` is exactly
    "nothing blocks": warnings never move it, because sameness is a judgement the Director is
    allowed to make and a gate that refuses a deliberately repeated beat is a gate that gets
    worked around.

    `warnings_omitted` is how many variance pairs were found beyond `VARIANCE_WARNING_LIMIT` and
    not listed, and is 0 when warnings were computed and none overflowed. `warnings_computed`
    says whether the pairwise pass ran at all: a caller that only needs the blocking answer skips
    it, and an empty `warnings` list must not then be read as "this plan has no duplicates".

    `window_warnings` is the shot-length band (`SHOT_WINDOW_BELOW_BAND`,
    `SHOT_WINDOW_ABOVE_BAND`), and it is a **third list rather than more entries in
    `warnings`** for one concrete reason: `warnings` has exactly one meaning to every reader it
    already has. `api.js` labels every note in it `READINESS_SAMENESS_LABEL` ("Near-duplicate")
    and `readinessSummary` counts its length as "N near-duplicate pairs" — so a window note
    posted into that list would reach the Director's screen under a name that is not what it
    says, and counted as a pair it is not. One list, one kind of note. It is computed
    unconditionally, unlike sameness: it is per-shot rather than pairwise, so it costs one
    comparison per shot and there is nothing for an `include_warnings=False` caller to save.
    """

    ready: bool = False
    shot_count: int = 0
    ready_count: int = 0
    blocking: list[ReadinessNote] = field(default_factory=list)
    warnings: list[ReadinessNote] = field(default_factory=list)
    warnings_computed: bool = True
    warnings_omitted: int = 0
    window_warnings: list[ReadinessNote] = field(default_factory=list)

    def blocked_ids(self) -> list[str]:
        """Every Shot id that blocks, in report order. Empty for an empty plan."""
        return [shot_id for note in self.blocking for shot_id in note.shot_ids]

    def blocked_labels(self) -> list[str]:
        """The same Shots under the names the Director sees. Empty for an empty plan."""
        return [label for note in self.blocking for label in note.labels]


def prompt_rejection(prompt: str) -> str:
    """Why this prompt cannot be submitted, or "" when it can.

    Shaped like `app.expansion_rejection`, and for the same reason: "not allowed" and "here is
    why" are one answer, and splitting them invites a caller that checks one and reports the
    other.

    Whitespace-only counts as empty, matching `expansion_rejection`'s `if not prompt.strip()`:
    a prompt of three spaces is not a prompt, and ComfyUI has no way to tell the difference.

    The `"New shot"` placeholder counts as empty too. It is not a prompt a Director wrote — it is
    the string `app.js` stamps onto every Shot it creates, and that duplicating a Shot copies —
    so a plan full of them is exactly as unrenderable as a plan full of blanks, and far more
    likely, because `""` requires a deliberate deletion. Compared after case and whitespace
    collapse so a copy that picked up stray spacing is caught with it.
    """
    collapsed = _collapsed(prompt)
    if not collapsed:
        return SHOT_WITHOUT_PROMPT
    if collapsed == _collapsed(PLACEHOLDER_PROMPT):
        return SHOT_WITH_PLACEHOLDER_PROMPT
    return ""


def prompt_is_missing(shot: Shot) -> bool:
    """True when this Shot has nothing a Director wrote in its prompt. See `prompt_rejection`.

    Deliberately reads `prompt` and nothing else. `status` is not a proxy — nothing in the
    shipped UI ever writes `ready`, so a status-keyed gate would be unreachable — and the
    reference branch of `generate_h3` interpolates the prompt into `f"Reference map: …"`, which
    turns `""` into a populated string, so any check downstream of that point passes on exactly
    the Shots this one exists to catch.
    """
    return bool(prompt_rejection(shot.prompt))


def window_band_note(shot: Shot) -> tuple[str, str]:
    """What this shot's window costs H3: ``(kind, sentence)``, or ``("", "")`` inside the band.

    Shaped like `prompt_rejection` — "outside the band" and "here is what that means" are one
    answer — and, unlike it, never a rejection: both sentences end by saying so. The band's ends
    are asymmetric and so are the sentences; see the two constants for the argument. The kind
    rides with the sentence for the same reason the two are one return value: a caller that got
    the words without the category would have to re-derive the category from the words.

    The short sentence carries the *numbers of this shot's own render* rather than a general
    remark, because "it renders anyway" is a claim a Director should be able to check: the frame
    count comes from `timeline.over_render_frames`, the one function the payload builders and the
    submission route compute their length with, so the buffer named here is the buffer that will
    exist. Reading the count off that function rather than restating the floor is what keeps this
    note honest if the floor ever moves.
    """
    if shot.duration > H3_MAX_SHOT_SECONDS:
        return NOTE_KIND_WINDOW_LONG, SHOT_WINDOW_ABOVE_BAND.format(
            duration=shot.duration, maximum=H3_MAX_SHOT_SECONDS
        )
    if shot.duration < H3_MIN_SHOT_SECONDS:
        frames = over_render_frames(shot.duration)
        rendered = frames / H3_FPS
        return NOTE_KIND_WINDOW_SHORT, SHOT_WINDOW_BELOW_BAND.format(
            duration=shot.duration,
            minimum=H3_MIN_SHOT_SECONDS,
            frames=frames,
            rendered=rendered,
            buffer=rendered - shot.duration,
        )
    return "", ""


def _words(prompt: str) -> set[str]:
    """The prompt's distinct words, lowercased, for the overlap comparison.

    Split on word boundaries rather than on whitespace. Splitting on spaces leaves punctuation
    attached, so `"turns."` and `"turns"` are different words and two prompts that differ only by
    a full stop score 0.75 — under the threshold, and reported as nothing at all. It also
    produced a phantom `""` token for a blank prompt, which then joined every intersection and
    union it took part in.

    A **set**, so repetition is deliberately ignored: `"red red red door"` and `"red door"`
    describe the same shot, and emphasis is not variance. The cost is stated rather than hidden —
    a prompt cannot make itself distinct by repeating a word — and it is the behaviour worth
    having, because the alternative flags a pair as different on the strength of a stutter.
    """
    return set(_WORD.findall(prompt.lower()))


def _collapsed(prompt: str) -> str:
    """The prompt with case and whitespace differences removed.

    `" ".join(value.split())` is the codebase's whitespace-collapse idiom (`app._short`); the
    idiom is reused rather than the function, which truncates. Punctuation is deliberately
    *kept*: this decides "identical", and the frozen definition of identical is equality after
    lowercasing and whitespace collapse. Punctuation-only differences are caught by `_words` and
    reported as similar, which is the honest label for them.
    """
    return " ".join(prompt.lower().split())


def _overlap(first: set[str], second: set[str]) -> float:
    """Jaccard overlap of two word sets: shared words over the words of the pair.

    Two empty sets have no overlap to speak of and would divide by zero, so the union is checked
    rather than each side — one empty side against a populated one is genuinely 0.0 and needs no
    special case.
    """
    union = first | second
    return len(first & second) / len(union) if union else 0.0


def readiness_report(project: Project, *, include_warnings: bool = True) -> ReadinessReport:
    """Whether this plan may be submitted. Pure, derived, and the only implementation.

    One function rather than one per caller: the single-Shot route, the browser's whole-batch
    check and Epic 4's batch submission all ask the same question, and three implementations of
    "is this plan fit to render" is three chances for the pre-flight to say yes to something the
    route then refuses.

    `include_warnings=False` skips the pairwise pass and answers only "what blocks". The
    submission route is the reason it exists: sameness cannot change its answer, and a queue of N
    Shots would otherwise run N pairwise comparisons over the whole plan — O(N³) across the
    batch — to build warnings each call discards. The report says which mode produced it
    (`warnings_computed`), so an empty `warnings` list can never be misread as "no duplicates".

    Ordered by `ordered_shots` — song order — so two calls over one project produce the same
    report and the notices read in the order the Director sees the Shots on the timeline.

    Sameness is compared only between Shots that are **not** blocked. Two empty prompts are
    trivially identical, and reporting that on top of two blocks would bury the one entry that
    has to be acted on under a warning that resolves itself the moment it is.

    The shot-length band (`window_band_note`) is reported for **every** shot, blocked ones
    included, and never changes `ready`. Unlike sameness it is a fact about one shot rather than
    a comparison between two, so a blocked shot's window is just as true as a prompted one's and
    hiding it would make the note appear only after the prompt was written — which is the
    late-discovery this exists to end.
    """
    shots = ordered_shots(project)
    if not shots:
        return ReadinessReport(
            ready=False,
            shot_count=0,
            ready_count=0,
            blocking=[
                ReadinessNote(
                    shot_ids=[], labels=[], reason=PLAN_WITHOUT_SHOTS, kind=NOTE_KIND_PROMPT
                )
            ],
            warnings=[],
            warnings_computed=include_warnings,
        )

    blocking: list[ReadinessNote] = []
    prompted: list[Shot] = []
    window_warnings: list[ReadinessNote] = []
    for shot in shots:
        band_kind, band_reason = window_band_note(shot)
        if band_kind:
            window_warnings.append(
                ReadinessNote(
                    shot_ids=[shot.id],
                    labels=[shot_label(project, shot)],
                    reason=band_reason,
                    kind=band_kind,
                )
            )
        rejection = prompt_rejection(shot.prompt)
        if rejection:
            blocking.append(
                ReadinessNote(
                    shot_ids=[shot.id],
                    labels=[shot_label(project, shot)],
                    reason=rejection,
                    kind=NOTE_KIND_PROMPT,
                )
            )
        else:
            prompted.append(shot)

    warnings: list[ReadinessNote] = []
    omitted = 0
    if include_warnings:
        for first, second in combinations(prompted, 2):
            if _collapsed(first.prompt) == _collapsed(second.prompt):
                reason = SHOTS_SHARE_ONE_PROMPT
            elif _overlap(_words(first.prompt), _words(second.prompt)) > NEAR_DUPLICATE_OVERLAP:
                reason = SHOTS_LACK_VARIANCE
            else:
                continue
            if len(warnings) >= VARIANCE_WARNING_LIMIT:
                omitted += 1
                continue
            warnings.append(
                ReadinessNote(
                    shot_ids=[first.id, second.id],
                    labels=[shot_label(project, first), shot_label(project, second)],
                    reason=reason,
                    kind=NOTE_KIND_SAMENESS,
                )
            )

    return ReadinessReport(
        ready=not blocking,
        shot_count=len(shots),
        ready_count=len(prompted),
        blocking=blocking,
        warnings=warnings,
        warnings_computed=include_warnings,
        warnings_omitted=omitted,
        window_warnings=window_warnings,
    )


# --------------------------------------------------------------------------------------------
# Render reconciliation — AD-1's one implementation of "what did ComfyUI do to these jobs".
# --------------------------------------------------------------------------------------------

#: The statuses a job never leaves. Everything else is a job whose answer still lives on
#: ComfyUI, and the set is what both halves of the transport decision key on: the backend
#: reconciles exactly the jobs outside it, and the browser polls exactly while one exists.
#: `api.js`'s TERMINAL_JOB_STATUSES is the frontend half; a contract test holds the two
#: together, because a client that polls for a status the server calls settled polls forever.
TERMINAL_JOB_STATUSES: frozenset[str] = frozenset({"complete", "error", "cancelled"})

#: The history statuses adopted verbatim onto a job. Anything else ComfyUI invents — it has
#: reported bare "success" strings before `HistoryResult` normalised them — reads as "running",
#: which is the only honest reading of "there is an entry and it is not finished".
_ADOPTED_HISTORY_STATUSES = frozenset({"queued", "running", "complete", "error"})

#: How many consecutive reconcile ticks a prompt may be unknown to both ComfyUI's queue and
#: its history before the job is settled as lost. Three ticks of the browser's two-second
#: poll rides out a restart's ambiguous seconds; a prompt still unknown after that died
#: with the queue.
MISSING_TICKS_LIMIT = 3

JOB_LOST_WITH_QUEUE = (
    "ComfyUI no longer knows this prompt — its queue was cleared, restarted, or crashed "
    "before the render ran. Nothing was produced. Render again re-opens the shot."
)


def reconcilable_jobs(project: Project) -> list[RenderJob]:
    """The jobs whose answer still lives on ComfyUI, in manifest order.

    Non-terminal **and** carrying a prompt id: a job with no prompt id has nothing to look up,
    so counting it would keep a client polling for an answer no tick can ever deliver. That
    also makes this the definition of "this project has renders in flight" — the poll endpoint
    reports `active` from it, and the browser's `hasActiveRenderJobs` mirrors it exactly.
    """
    return [
        job
        for job in project.jobs
        if job.prompt_id and job.status not in TERMINAL_JOB_STATUSES
    ]


def queue_locations(payload: dict[str, Any]) -> dict[str, str]:
    """Every prompt in ComfyUI's ``/queue`` answer, mapped to ``"running"`` or ``"queued"``.

    The same reading `ComfyClient.queue_state` makes for one prompt — each bucket holds
    ``[number, prompt_id, …]`` lists, so membership is "any element equals the id" — taken
    over the whole payload once, because taking it per job is the fan-out AD-1 exists to
    prevent: forty jobs made forty ``/queue`` calls that all parsed the same answer.
    """
    located: dict[str, str] = {}
    for key, state in (("queue_running", "running"), ("queue_pending", "queued")):
        for item in payload.get(key, []):
            if not isinstance(item, list):
                continue
            for part in item:
                if isinstance(part, str):
                    located[part] = state
    return located


def apply_job_history(project: Project, job: RenderJob, history: HistoryResult) -> None:
    """Write one ComfyUI history answer onto the job, and onto whatever the job was producing.

    The one place a completion moves project state — the per-job route and the reconciler both
    delegate here, so `Shot.status`, an Asset's landed file and a Song's audio path cannot be
    adopted by two subtly different rules. Everything in here is a decision about data already
    fetched; nothing touches the network.
    """
    job.status = (
        history.status if history.status in _ADOPTED_HISTORY_STATUSES else "running"
    )
    job.output_files = [
        "/".join(
            part.replace("\\", "/").strip("/")
            for part in (item.get("subfolder", ""), item.get("filename", ""))
            if part
        )
        for item in history.outputs
    ]
    job.error = history.error
    if job.status == "complete":
        if job.kind in {"flux", "multiview", "edit"}:
            asset = next((item for item in project.assets if item.id == job.target_id), None)
            if asset and job.output_files:
                asset.path = job.output_files[0]
        # Only the Song this job actually produced may adopt its output. `target_id`
        # is the constant string "song" for every music job, so the prompt id is the
        # only thing tying a completion to a particular Song. Without this check a
        # job that finished after the Song was removed re-attached its audio to
        # whatever Song was there — and in the other order it overwrote an *imported*
        # song's `path` with a generated file while `source` still said "imported".
        # A mismatched output is not lost: it stays listed on the job's
        # `output_files`, which is where an orphaned take is recovered from.
        elif (
            job.kind == "music"
            and project.song
            and project.song.prompt_id == job.prompt_id
            and job.output_files
        ):
            project.song.path = job.output_files[0]
        elif job.kind == "h3":
            shot = next((item for item in project.shots if item.id == job.target_id), None)
            if shot:
                shot.status = "complete"
                if job.output_files:
                    # The pointer moves; the file it used to name does not. ComfyUI
                    # numbers its outputs from the filename prefix, so a re-render of
                    # one shot writes `…_00002` beside `…_00001` — and this job's own
                    # `output_files` goes on naming whichever it produced. That is the
                    # whole of what "the previous take is not silently lost" means:
                    # nothing here is a take list, and nothing should be read as one.
                    #
                    # `latest_review` is dropped when, and only when, the take it
                    # describes stops being the latest one. It is a vision inspection of
                    # a *specific* file; carrying it across a new take would leave the
                    # inspector reporting on the previous render under the new take's
                    # name, which is worse than showing nothing — and it is now reachable
                    # from the interface, because a shot can be re-opened and rendered
                    # again. Re-run "Inspect latest take" for the new one.
                    if job.output_files[0] != shot.latest_output:
                        shot.latest_review = None
                    shot.latest_output = job.output_files[0]
    elif job.kind == "h3" and job.status == "error":
        shot = next((item for item in project.shots if item.id == job.target_id), None)
        if shot:
            shot.status = "error"


@dataclass(slots=True)
class RenderReconciliation:
    """What one reconciliation tick did.

    `changed` is whether anything on the project moved, which is exactly "does this need
    saving" — a tick that learned nothing must not rewrite the manifest every two seconds.
    `comfy_online` is whether ComfyUI answered the one `/queue` call; `False` means the tick
    degraded to a no-op, which is a fact the report carries rather than an error anybody
    raises. The Director's server keeps answering 200 while ComfyUI restarts.
    """

    changed: bool = False
    comfy_online: bool = True


async def reconcile_render_jobs(project: Project, comfy: Any) -> RenderReconciliation:
    """One tick of AD-1's reconciliation: ``/queue`` once, ``/history`` only where needed.

    `comfy` is the app's `ComfyClient` (or a test double). The order is the decision:

    * nothing to reconcile means **no request at all** — an idle project costs ComfyUI nothing;
    * ``/queue`` is fetched exactly once per tick, however many jobs are open, and a job found
      in it is settled from that answer alone — history is empty until a prompt finishes, so
      asking would learn nothing;
    * ``/history/{id}`` is fetched only for open jobs absent from the queue, which is the one
      state where history is the thing that knows;
    * a dead ComfyUI fails the tick quietly (`comfy_online=False`, nothing touched) and one
      job's failed history lookup skips that job and keeps going — the next tick asks again.

    A job absent from both the queue and history keeps the status it has for a few ticks,
    exactly as the manual per-job refresh always left it: inventing an error on the first
    absence would mark a prompt "failed" in the seconds ComfyUI takes to admit a restart,
    and the honest answer there is that nothing is known yet. But absence that *persists*
    is an answer — a crash, a restart, or a hand-cleared queue took the prompt with it, and
    a job left "queued" forever pins render-status at "active" and blocks the shot's
    re-open. `MISSING_TICKS_LIMIT` consecutive unknown ticks settle it as that error, with
    the counter reset whenever ComfyUI answers (met three times live on 2026-08-19/20).
    """
    open_jobs = reconcilable_jobs(project)
    if not open_jobs:
        return RenderReconciliation(changed=False, comfy_online=True)
    try:
        located = queue_locations(await comfy.queue())
    except ComfyError:
        return RenderReconciliation(changed=False, comfy_online=False)
    changed = False
    for job in open_jobs:
        in_queue = located.get(job.prompt_id)
        if in_queue is not None:
            if job.status != in_queue or job.missing_ticks:
                job.status = in_queue
                job.missing_ticks = 0
                changed = True
            continue
        try:
            history = await comfy.history(job.prompt_id)
        except ComfyError:
            continue
        # `getattr` with a True default: a test double built before `known` existed is a
        # history that answered, and answering is exactly what the default means.
        if not getattr(history, "known", True):
            job.missing_ticks += 1
            changed = True
            if job.missing_ticks >= MISSING_TICKS_LIMIT:
                job.status = "error"
                job.error = JOB_LOST_WITH_QUEUE
                shot = next(
                    (item for item in project.shots if item.id == job.target_id), None
                )
                if job.kind == "h3" and shot and shot.status in ("queued", "running"):
                    shot.status = "error"
            continue
        before = (job.status, list(job.output_files), job.error)
        apply_job_history(project, job, history)
        job.missing_ticks = 0
        if (job.status, job.output_files, job.error) != before:
            changed = True
    return RenderReconciliation(changed=changed, comfy_online=True)


#: What a leftover job record says once a newer render has taken its target. Written as what
#: happened to the *record*, not as a claim about ComfyUI: nothing here interrupts a prompt —
#: ComfyUI is user-managed — so an older prompt already executing goes on executing, and its
#: file lands beside the new one under the same prefix. The record keeps its `prompt_id` so
#: that file is still traceable; what it loses is the ability to write itself onto the target.
JOB_SUPERSEDED = (
    "Superseded by a newer render for the same target. This record was still open when the "
    "newer job was accepted, so nothing would ever have settled it — and a late answer to it "
    "would have overwritten the newer render's result. Watch the newer job instead."
)


def supersede_target_jobs(
    project: Project, *, kinds: frozenset[str] | set[str], target_id: str, keep_job_id: str
) -> list[RenderJob]:
    """Settle every unsettled job of these kinds already pointing at ``target_id``.

    Job-record hygiene for the states that get *past* the routes' in-flight refusals, and
    deliberately not a second opinion about whether a submission is allowed: every 409 stays
    exactly where it is, and this runs only after one has already been passed and a new job
    accepted. Today `generate_h3` is the one caller, because it is the one submission route
    whose per-target guard is a *Shot status* — which a whole-manifest write can walk
    backwards underneath a live job — rather than a read of the job records themselves.

    Two live records for one target is not a cosmetic untidiness. The older one is
    non-terminal, so `reconcilable_jobs` keeps reporting the project active and every gate
    that counts open renders — assembly, asset fill — keeps refusing; and if its prompt does
    answer later, `apply_job_history` adopts that answer onto the same target, moving
    `latest_output` back to the older take and dropping the newer take's `latest_review`
    with it. Settling the record closes both.

    ``cancelled`` rather than a status of its own: it is already terminal on both sides of the
    transport (`TERMINAL_JOB_STATUSES`, and `api.js`'s mirror of it), so the poll releases and
    the queue panel renders it with no client change at all. `superseded_by` is what keeps it
    distinguishable from a hand cancellation afterwards. `missing_ticks` is zeroed because the
    counter only ever meant "how close is this to being settled", and it is settled.

    **The stated cost.** A settled record is never reconciled again, so if its prompt was
    still executing on ComfyUI its `output_files` stays empty: the file lands on disk under
    the shot's own prefix but is not listed on the record, and the takes strip — which reads
    `output_files` — will not show it. That is why this is not applied to the music routes,
    where an older job's `output_files` is the only place an orphaned take is recoverable
    from and where the newer result is already protected by `Song.prompt_id`. Nothing here
    interrupts ComfyUI to make the cost go away; ComfyUI is user-managed.

    Returns the records it changed, so a caller can report or log them; the caller saves.
    """
    superseded: list[RenderJob] = []
    for job in project.jobs:
        if (
            job.id != keep_job_id
            and job.kind in kinds
            and job.target_id == target_id
            and job.status not in TERMINAL_JOB_STATUSES
        ):
            job.status = "cancelled"
            job.error = JOB_SUPERSEDED
            job.superseded_by = keep_job_id
            job.missing_ticks = 0
            superseded.append(job)
    return superseded


class ShotRenderState(BaseModel):
    """One Shot's render-facing facts — the fields a completion moves, and nothing else.

    Deliberately not the whole Shot. The browser patches these onto the copy it is holding
    while the Director may be mid-keystroke in that Shot's prompt box, so the report must not
    carry a single field the shot inspector edits — carrying `prompt` here would hand the
    poll loop the means to overwrite typing with a two-second-old snapshot.
    """

    shot_id: str
    status: ShotStatus
    latest_output: str = ""


class AssetRenderState(BaseModel):
    """One Asset's landed file. `path` is empty while its render is still in flight."""

    asset_id: str
    path: str = ""


class SongRenderState(BaseModel):
    """The Song's audio path, keyed by the prompt id that produced it.

    `prompt_id` is carried for the browser's version of the adoption guard in
    `apply_job_history`: a stale report naming a previous Song's audio must not be patched
    onto whatever Song is loaded now, and the prompt id is the only thing that ties the two.
    """

    path: str = ""
    prompt_id: str = ""


class JobProgress(BaseModel):
    """How far one open ComfyUI render has got, as a percentage nobody stored.

    A row exists **only** when ComfyUI has actually said something about that prompt on its
    WebSocket. Absence is the answer for "unknown" — a socket that never connected, a prompt
    still waiting its turn in the queue, a build whose messages `comfy.progress_from_message`
    does not recognise — and `percent: 0` is the different, real answer "this render has
    started and no step of it is done yet". Neither is ever invented: an interpolated number
    on a render that is actually stuck is worse than no number at all, which is the same
    reason `timeline.song_section` returns nothing rather than guessing a section.

    `prompt_id` rides along because it is the *only* thing attribution is done by; `job_id` is
    what the browser joins on, since that is what its own job list is keyed by.
    """

    job_id: str
    prompt_id: str
    percent: int = Field(ge=0, le=100)


class RenderStatusReport(BaseModel):
    """AD-1's fixed poll answer: the jobs, plus the states their completions move.

    `active` is the browser's whole polling contract — poll again in two seconds if and only
    if it is true. `comfy_online` is the degraded-tick flag; the jobs and states alongside it
    are then simply the project as last known, so a ComfyUI restart never blanks a queue
    panel that was painted from real answers.

    `progress` is the one field here that is not read off the manifest, because it is the one
    fact that must never be written to it: see `JobProgress` and `comfy.ProgressTracker`. It
    rides this existing poll rather than a route or a socket of its own — the browser already
    asks this question every two seconds while, and only while, a render is open, so live
    percentages cost exactly zero additional requests and an idle project still makes none.
    """

    active: bool
    comfy_online: bool
    jobs: list[RenderJob]
    shots: list[ShotRenderState]
    assets: list[AssetRenderState]
    song: SongRenderState | None = None
    progress: list[JobProgress] = Field(default_factory=list)


def render_status_report(
    project: Project, *, comfy_online: bool = True, progress: Mapping[str, int] | None = None
) -> RenderStatusReport:
    """The poll answer for this project as it stands. Pure — reconcile first, then report.

    `progress` is the live `prompt_id → percent` map the WebSocket listener holds in memory,
    passed in rather than read from a module global so this stays a pure function of its
    arguments. Only *open* jobs are reported: a settled job's leftover percentage would
    contradict its own terminal status, and a percentage for a prompt this project never
    submitted belongs to somebody else's render.
    """
    live = progress or {}
    reported: list[JobProgress] = []
    for job in reconcilable_jobs(project):
        percent = live.get(job.prompt_id)
        if job.prompt_id and percent is not None:
            reported.append(
                JobProgress(
                    job_id=job.id, prompt_id=job.prompt_id, percent=max(0, min(100, percent))
                )
            )
    return RenderStatusReport(
        active=bool(reconcilable_jobs(project)),
        comfy_online=comfy_online,
        jobs=project.jobs,
        progress=reported,
        shots=[
            ShotRenderState(
                shot_id=shot.id, status=shot.status, latest_output=shot.latest_output
            )
            for shot in project.shots
        ],
        assets=[
            AssetRenderState(asset_id=asset.id, path=asset.path) for asset in project.assets
        ],
        song=SongRenderState(path=project.song.path, prompt_id=project.song.prompt_id)
        if project.song
        else None,
    )


# --------------------------------------------------------------------------------------------
# Generate All -- AD-5's batch selection (spec-generate-all). The submission itself rides the
# single-shot routes; this half decides, purely, which shots a batch names and which it skips
# with a sentence.
# --------------------------------------------------------------------------------------------

#: Why a settled shot is skipped rather than re-opened under Replace Existing (and why a
#: flagged shot cannot resubmit). Named, never silent: a Director who ticked Replace and
#: sees an old take again deserves the reason in the report, not a mystery.
GENERATE_BATCH_APPROVED_SKIP = (
    "{shot} carries an approved take. The approval pins that exact file; un-approve it "
    "to re-render."
)
GENERATE_BATCH_LOCKED_SKIP = "{shot} is locked."


def batch_targets(
    project: Project, *, scope: str = "ready", replace_existing: bool = False
) -> tuple[list[Shot], list[tuple[Shot, str]]]:
    """The shots one batch will submit, in timeline order, and the named skips.

    Two scopes, per the spec's matrix. ``ready`` is FR-4's own set -- every shot standing
    at ``ready`` -- widened by ``replace_existing`` to settled (``complete``/``error``)
    shots that nothing protects; approved and locked settled shots are skipped **by
    name**. ``flagged`` is AD-5's resubmission set: exactly the flagged shots, with the
    same two protections named.

    Deliberately only the *meaning-level* protections are decided here (approval, lock --
    the two states a submission could never be right about). Everything mechanical --
    in-flight 409s, the readiness prompt gate, adapterless modes -- is left to the
    single-shot routes the batch delegates to, so no second copy of any of those rules
    exists to drift. Draft shots are not this route's act at all: arming is arm-a-plan's
    lane, and a draft in the flagged scope surfaces through the single-shot path's own
    "must be ready" refusal rather than a rule here.
    """
    targets: list[Shot] = []
    skipped: list[tuple[Shot, str]] = []

    def protected(shot: Shot) -> str | None:
        if shot.approved_output or shot.status == "approved":
            return GENERATE_BATCH_APPROVED_SKIP.format(shot=shot_label(project, shot))
        if shot.locked:
            return GENERATE_BATCH_LOCKED_SKIP.format(shot=shot_label(project, shot))
        return None

    for shot in ordered_shots(project):
        if scope == "flagged":
            if not shot.flagged:
                continue
            reason = protected(shot)
            if reason:
                skipped.append((shot, reason))
            else:
                targets.append(shot)
        else:
            if shot.status == "ready":
                targets.append(shot)
            elif replace_existing and (
                shot.status in ("complete", "error", "approved") or shot.approved_output
            ):
                # `approved` joins the settled set so the protection is *named*: a
                # Director who ticked Replace and sees an old take again deserves the
                # reason, and an approved shot silently absent from both lists reads as
                # a bug. In-flight (`queued`/`running`) shots are deliberately absent
                # instead — they are already rendering, which the queue panel shows.
                reason = protected(shot)
                if reason:
                    skipped.append((shot, reason))
                else:
                    targets.append(shot)
    return targets, skipped
