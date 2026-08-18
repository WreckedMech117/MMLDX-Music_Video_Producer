"""Whether a Shot Plan is fit to submit, derived on demand and never stored.

AD-5's module. Everything here is pure: it reads a `Project` and returns a report. Nothing is
written back onto the manifest, because readiness is computable from the prompts themselves and
anything computable from the manifest is computed rather than persisted — a stored `ready` flag
is a second source of truth that goes stale the moment a prompt is edited.

`timeline.py` must never import this module; this module may import `timeline.py`. The
dependency runs one way so the window math stays a pure leaf.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import combinations

from .models import Project, Shot, shot_label
from .timeline import ordered_shots

#: Re-exported, not redefined. `shot_label` moved to `models.py` when `timeline.assistant_input`
#: needed it — `timeline` may not import this module — and every existing importer says
#: `from .batch import shot_label`, so the name stays here and there is still exactly one
#: implementation. See `models.shot_label` for why both halves of the name are carried.
__all__ = [
    "NEAR_DUPLICATE_OVERLAP",
    "PLACEHOLDER_PROMPT",
    "READINESS_REFUSAL",
    "ReadinessNote",
    "ReadinessReport",
    "prompt_is_missing",
    "prompt_rejection",
    "readiness_refusal",
    "readiness_report",
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
