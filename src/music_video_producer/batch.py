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
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

from pydantic import BaseModel

from .comfy import ComfyError, HistoryResult
from .models import Project, RenderJob, Shot, ShotStatus, shot_label
from .timeline import ordered_shots

#: Re-exported, not redefined. `shot_label` moved to `models.py` when `timeline.assistant_input`
#: needed it — `timeline` may not import this module — and every existing importer says
#: `from .batch import shot_label`, so the name stays here and there is still exactly one
#: implementation. See `models.shot_label` for why both halves of the name are carried.
__all__ = [
    "NEAR_DUPLICATE_OVERLAP",
    "PLACEHOLDER_PROMPT",
    "READINESS_REFUSAL",
    "TERMINAL_JOB_STATUSES",
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
    """

    shot_ids: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    reason: str = ""


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
    """

    ready: bool = False
    shot_count: int = 0
    ready_count: int = 0
    blocking: list[ReadinessNote] = field(default_factory=list)
    warnings: list[ReadinessNote] = field(default_factory=list)
    warnings_computed: bool = True
    warnings_omitted: int = 0

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
    """
    shots = ordered_shots(project)
    if not shots:
        return ReadinessReport(
            ready=False,
            shot_count=0,
            ready_count=0,
            blocking=[ReadinessNote(shot_ids=[], labels=[], reason=PLAN_WITHOUT_SHOTS)],
            warnings=[],
            warnings_computed=include_warnings,
        )

    blocking: list[ReadinessNote] = []
    prompted: list[Shot] = []
    for shot in shots:
        rejection = prompt_rejection(shot.prompt)
        if rejection:
            blocking.append(
                ReadinessNote(
                    shot_ids=[shot.id], labels=[shot_label(project, shot)], reason=rejection
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

    A job absent from both the queue and history keeps the status it has, exactly as the
    manual per-job refresh always left it: inventing an error for it would mark a prompt
    "failed" in the seconds ComfyUI takes to admit a restart, and the honest answer is that
    nothing is known yet.
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
            if job.status != in_queue:
                job.status = in_queue
                changed = True
            continue
        try:
            history = await comfy.history(job.prompt_id)
        except ComfyError:
            continue
        before = (job.status, list(job.output_files), job.error)
        apply_job_history(project, job, history)
        if (job.status, job.output_files, job.error) != before:
            changed = True
    return RenderReconciliation(changed=changed, comfy_online=True)


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


class RenderStatusReport(BaseModel):
    """AD-1's fixed poll answer: the jobs, plus the states their completions move.

    `active` is the browser's whole polling contract — poll again in two seconds if and only
    if it is true. `comfy_online` is the degraded-tick flag; the jobs and states alongside it
    are then simply the project as last known, so a ComfyUI restart never blanks a queue
    panel that was painted from real answers.
    """

    active: bool
    comfy_online: bool
    jobs: list[RenderJob]
    shots: list[ShotRenderState]
    assets: list[AssetRenderState]
    song: SongRenderState | None = None


def render_status_report(
    project: Project, *, comfy_online: bool = True
) -> RenderStatusReport:
    """The poll answer for this project as it stands. Pure — reconcile first, then report."""
    return RenderStatusReport(
        active=bool(reconcilable_jobs(project)),
        comfy_online=comfy_online,
        jobs=project.jobs,
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
