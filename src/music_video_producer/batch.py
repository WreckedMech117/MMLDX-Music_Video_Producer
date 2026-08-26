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
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import combinations
from math import isfinite
from typing import Any

from pydantic import BaseModel, Field

from .comfy import ComfyError, HistoryResult
from .models import (
    NO_EVIDENCED_BUNDLE,
    Asset,
    Project,
    RenderJob,
    Shot,
    ShotStatus,
    now_utc,
    numbered_references,
    shot_label,
)
from .reference_map import (
    STALE_REFERENCE_MAP_CAUSE,
    STALE_REFERENCE_MAP_CONSEQUENCE,
    STALE_REFERENCE_MAP_REMEDY,
    stale_reference_map,
)
from .timeline import (
    H3_FPS,
    H3_MAX_SHOT_SECONDS,
    H3_MIN_SHOT_SECONDS,
    ordered_shots,
    over_render_frames,
    song_section,
)

#: Re-exported, not redefined. `shot_label` moved to `models.py` when `timeline.assistant_input`
#: needed it — `timeline` may not import this module — and every existing importer says
#: `from .batch import shot_label`, so the name stays here and there is still exactly one
#: implementation. See `models.shot_label` for why both halves of the name are carried.
__all__ = [
    "JOB_KIND_WITH_SAMPLING_BUNDLE",
    "NEAR_DUPLICATE_OVERLAP",
    "NOTE_KIND_PROMPT",
    "NOTE_KIND_SAMENESS",
    "NOTE_KIND_SETTING_CONFLICT",
    "NOTE_KIND_STALE_MAP",
    "NOTE_KIND_TAKE_UNCOVERED",
    "NOTE_KIND_VIDEO_SOUNDTRACK",
    "NOTE_KIND_WINDOW_LONG",
    "NOTE_KIND_WINDOW_SHORT",
    "PLACEHOLDER_PROMPT",
    "READINESS_REFUSAL",
    "SHOT_AFTER_FAILED_RENDER",
    "SHOT_SETTING_FIGHTS_SECTION",
    "SHOT_VIDEO_SOUNDTRACK_UNCONDITIONED",
    "SHOT_WITH_STALE_REFERENCE_MAP",
    "TERMINAL_JOB_STATUSES",
    "JobProgress",
    "ReadinessNote",
    "ReadinessReport",
    "RenderReconciliation",
    "RenderStatusReport",
    "apply_job_history",
    "format_duration",
    "format_lora_strength",
    "prompt_is_missing",
    "prompt_rejection",
    "queue_locations",
    "readiness_refusal",
    "readiness_report",
    "reconcilable_jobs",
    "reconcile_render_jobs",
    "render_status_report",
    "render_timing_summary",
    "sampling_bundle_cell",
    "sampling_bundle_summary",
    "setting_conflict_note",
    "shot_label",
    "shot_status_after_failed_render",
    "stamp_job_settled",
    "supersede_target_jobs",
    "take_coverage_note",
    "video_soundtrack_note",
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

# The third block, and the one the pre-flight was silent about until 2026-08-21. A shot whose
# stored expansion carries a stale reference map is **already refused at the submit route** —
# `app.STALE_REFERENCE_MAP_REFUSAL`, since 2026-08-20 — but nothing said so before the GPU cost was
# confirmed, so the Director read "33 ready", agreed to the batch, and learned about the one skipped
# shot from the report afterwards. Everything else this module reports exists precisely so that
# "22 armed, 3 skipped" is seen while a Director can still act on it.
#
# **It blocks rather than warns, and the split it lands on is the established one.** Sameness warns
# because two shots may legitimately share a prompt and a gate over a legitimate state gets worked
# around; emptiness blocks because there is no reading of an empty prompt under which the render is
# the right thing to spend. A stale map is on emptiness' side of that line: there is no state of the
# world in which conditioning a render on a map naming the wrong pictures is what the Director
# meant, and the route already agrees — it refuses. Warning here would have made the report
# *predict* a refusal while `ready` went on saying the plan may be submitted, which is the one thing
# `ReadinessReport` must never say about a shot the route will turn away. Blocking makes the two
# layers give one answer, and it costs nothing a Director can lose: a blocked shot no longer
# disables the batch button (`api.js`'s `generateAllPlan`), so this names the shot and skips it
# rather than stopping the other thirty-two.
#
# The sentence is composed from `reference_map.py`'s shared clauses — the same cause, the same
# consequence and the same remedy the refusal says — because one problem must not reach the Director
# as two explanations, and the remedy in particular has a step (`mark-draft`) that a second wording
# would be free to forget.
SHOT_WITH_STALE_REFERENCE_MAP = (
    f"This shot's {STALE_REFERENCE_MAP_CAUSE}. Submitting it would spend a full GPU pass, and "
    f"{STALE_REFERENCE_MAP_CONSEQUENCE}. {STALE_REFERENCE_MAP_REMEDY}"
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
#: The second **blocking** kind, and the reason `blocked_ids` grew a `kind` filter: a stale map and
#: an empty prompt are both refusals, and each route says its own sentence about its own one. Kept
#: apart from `NOTE_KIND_PROMPT` so no surface can print "no prompt" over a shot that has one.
NOTE_KIND_STALE_MAP = "stale_map"
NOTE_KIND_WINDOW_SHORT = "window_short"
NOTE_KIND_WINDOW_LONG = "window_long"
#: The third window state, and the Director's second yellow (2026-08-21): "if the bounds of the
#: shots window are dragged beyond where that clip covers then the shot would turn yellow to warn
#: that the bounds was gone past. The user could then readjust or regenerate to fit the newly
#: wanted shot timeframe." A *different* fact from the band — the band is about the length H3 is
#: trained for and is decidable before anything is rendered; this one is about the take that
#: exists, and can only be asked of a shot that has one — so it is its own kind, drawn in the same
#: amber and never folded into `window_long`.
NOTE_KIND_TAKE_UNCOVERED = "take_uncovered"
#: The fourth non-blocking kind, and the only one that is not about a window at all: this shot has
#: **two sources of location**. It rides in `window_warnings` because that list's real membership
#: rule is "per-shot, never blocks, drawn under its own name" — see `ReadinessReport` — and the
#: alternative was a fourth list for one sentence a Director reads exactly as they read the others.
#: It is its own kind, and never folded into another, because the fix is a different action: not a
#: drag and not a re-render, but a citation or a section prompt.
NOTE_KIND_SETTING_CONFLICT = "setting_conflict"
#: The fifth non-blocking kind, and the second that is not about a window: this shot cites a video
#: and only the video's *picture* is conditioned. It is its own kind for the setting conflict's
#: reason — the fact is different and there is no action to take, so folding it into a
#: window kind would put a remedy sentence over a statement of how the render works. A client that
#: does not know this kind yet draws it through `api.js`'s `NOTE_KIND_WINDOW_UNKNOWN` fallback,
#: which prints the sentence whole under a heading that claims nothing and gives the clip no
#: badge — which is right here, because there is nothing wrong with the shot.
NOTE_KIND_VIDEO_SOUNDTRACK = "video_soundtrack"

#: How far a window may sit outside its take before it is reported: half a frame at H3's rate.
#: Both sides of the comparison are manifest floats and one of them is a division, so an exact
#: `>` would report a shot as uncovered on the last bit of a number nobody touched. Deliberately
#: not imported from `assembly` — that module measures a *file* with ffprobe and this one reads a
#: recorded window, and the two are allowed to be judged at different moments with different
#: evidence. The number is the same because half a frame means the same thing in both places.
TAKE_COVERAGE_TOLERANCE_SECONDS = 1 / (2 * H3_FPS)

#: The window has been dragged back past the take's first frame: the cut asks for picture from
#: before the take begins. Reachable because a locked move-drag compensates `trim_nudge` and is
#: deliberately **not clamped** — the Director's ruling is that the warning must never constrain
#: the gesture ("still gives us the ability to nudge the actual position of a clip if we need
#: to"), so this is what happens instead of a stop.
#:
#: Every number the fix needs is named, and both fixes are named, because "readjust or regenerate"
#: is the Director's own pair of remedies. It says it does not block submission for the same
#: reason the band's two sentences do: a Director who reads a warning as a gate stops working.
SHOT_WINDOW_BEFORE_TAKE = (
    "This shot's window now starts {behind:.3f}s before its take does: the cut reaches back past "
    "the take's own first frame, and those seconds were never rendered. Drag the window forward "
    "over the take, ease the trim nudge forward, or render the shot again for the window it has "
    "now. This does not block submission."
)
#: And the other end: the window runs off the back of the take. The take's length is the length
#: the render *asked H3 for* — `over_render_frames` of the window recorded at submission — and is
#: named as such rather than measured, because this module never opens a video file. Assembly
#: measures the real one with ffprobe and judges it there; a note that claimed to have measured
#: would be claiming evidence it does not hold.
SHOT_WINDOW_PAST_TAKE = (
    "This shot's window runs {past:.3f}s past the end of its take. The take was rendered for "
    "{take:.3f}s of picture, and the window asks for {needed:.3f}s of it — {offset:.3f}s of "
    "offset plus a {duration:.3f}s window. Drag the window back over the take, ease the trim "
    "nudge back, or render the shot again for the window it has now. This does not block "
    "submission."
)

# --------------------------------------------------------------------------------------------
# Two sources of location. The Director's report (2026-08-23), on the live 30-shot plan: five
# shots cite `Dusk Warehouse Bed` while their section's look prompt reads "Vast empty warehouse
# floor". The section look reaches the render layered over the shot's own references —
# `app.song_audio_prose` appends it verbatim, and the document expansions are handed it — so the
# submitted text gives H3 a bed to look at and then tells it the location is an empty floor. It is
# baked into what gets rendered, and nothing said so.
#
# **It warns and never blocks**, and that is the Director's ruling in their own words: "populate
# still writes it, readiness flags it, and the Director keeps the ability to do it deliberately,
# because sometimes a contradiction is the shot you want." It lands on sameness' side of this
# module's split, and for sameness' exact reason — there is a reading of this state under which the
# render is the right thing to spend, so a gate over it is a gate that gets worked around.
#
# **The rule is structural, because readiness may not think.** This report runs on every render
# submission and must stay pure, offline and deterministic, so "contradicts" cannot be asked of a
# model here. What is decidable without understanding either string is *two sources of location*:
# the shot cites a `setting` Asset, and the section's look prompt matches a **different** setting
# this project holds better than it matches the one that was cited. The project's own asset names
# supply the whole vocabulary; nothing here knows what a warehouse is.
#
# The naive reading of the same idea — "cites a setting, and the section has a look prompt" — was
# measured on that live plan before this was built and fires on **30 of 30 shots**, which is noise,
# and noise is worse than nothing: a warning that always fires teaches a Director to skip the list
# that carries the ones that matter. The rule below was measured on the same plan at **5 of 30**,
# and they are the five the Director named. That measurement is what this ships on.
#
# The likely author of the state is `models.with_default_setting`, which attaches the project's
# declared location to any shot that named none — correct in general, and wrong for a shot sitting
# in a section whose look describes the other one.
SHOT_SETTING_FIGHTS_SECTION = (
    "This shot cites the setting {cited}, and the {label} section's look prompt matches a "
    "different setting this project holds: {rival}, on {words}. The section look reaches the "
    "render layered over this shot's own references, so H3 is handed {cited}'s picture and then "
    "told the location is somewhere else. Cite {rival} instead, reword the section's look, or "
    "leave it as it is, because a contradiction is sometimes the shot you want. This does not "
    "block submission."
)

# --------------------------------------------------------------------------------------------
# A cited video's soundtrack. The H3 loader can pair a video's own audio track with it, or route
# that track into the standalone audio group, and it does neither unless the payload says
# `has_audio` — which `generate_h3` did not send, at all, until 2026-08-26. So a Director could
# drop an mp4 into the library (`app.js` derives `video` from the MIME type), cite it, and get a
# take conditioned on the clip's picture with its sound silently discarded.
#
# The decision recorded on the route beside the payload: **it stays off, and it is now said out
# loud in both directions** — `has_audio: False` on the wire, and this sentence to the Director.
# A reference shot is performed against the *master song*, which `use_song_audio` attaches as an
# audio reference and which the whole over-render window exists to line the take up against; a
# clip's own soundtrack conditioned alongside it is unrelated music in the same sampling pass.
# Turning it on by default would change what every existing citation renders, and there is
# nothing here to decide it with — no Asset field records whether a video carries an audio
# stream, and no control offers the paired/standalone choice the node reads.
#
# It states how the render works and asks for nothing, so it never blocks and names no remedy:
# a note whose fix is "do nothing" is a note a Director learns to skip past, and the sentences
# either side of it in this list are ones that do want an action.
#
# One note per shot rather than one per cited video, and every cited video named in it: this is
# one fact about how the shot renders, and repeating it per citation would count a single
# statement several times in a list a Director reads by length.
SHOT_VIDEO_SOUNDTRACK_UNCONDITIONED = (
    "This shot cites {references} as video {reference_word}. H3 is conditioned on the picture "
    "only and no soundtrack is sent alongside, so the take is performed against the master song "
    "rather than against any audio the cited media carries. This does not block submission."
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

    `window_warnings` is every **per-shot warning that never blocks**: the shot-length band
    (`SHOT_WINDOW_BELOW_BAND`, `SHOT_WINDOW_ABOVE_BAND`); for a shot whose take carries a window
    snapshot, whether that window still sits inside the take (`take_coverage_note`); whether
    the shot is being given two locations at once (`setting_conflict_note`); and whether a cited
    video's soundtrack is being dropped (`video_soundtrack_note`). The name is the
    window notes it was built for and is kept because it is on the wire — a client reads
    `report.window_warnings` — while the membership rule was always the broader one: per shot,
    never `ready`, drawn under its own kind. It is a **third list rather than more entries in
    `warnings`** for one concrete reason: `warnings` has exactly one meaning to every reader it
    already has. `api.js` labels every note in it `READINESS_SAMENESS_LABEL` ("Near-duplicate")
    and `readinessSummary` counts its length as "N near-duplicate pairs" — so a window note
    posted into that list would reach the Director's screen under a name that is not what it
    says, and counted as a pair it is not. One list, one kind of note. It is computed
    unconditionally, unlike sameness: it is per-shot rather than pairwise, so it costs one
    comparison per shot and there is nothing for an `include_warnings=False` caller to save.

    A shot can carry **several** notes in this list — a long window over a take it has outgrown
    is two of them — which is why nothing keys this list by shot id. The client's
    `windowWarningsByShot` reduces it to the one state a clip can wear and says which wins; every
    note is still printed, in full, in the readiness list.
    """

    ready: bool = False
    shot_count: int = 0
    ready_count: int = 0
    blocking: list[ReadinessNote] = field(default_factory=list)
    warnings: list[ReadinessNote] = field(default_factory=list)
    warnings_computed: bool = True
    warnings_omitted: int = 0
    window_warnings: list[ReadinessNote] = field(default_factory=list)

    def blocked_ids(self, *, kind: str = "") -> list[str]:
        """Every Shot id that blocks, in report order. Empty for an empty plan.

        `kind` narrows to one `NOTE_KIND_*`; the default answers for every kind, which is what
        "may this plan be submitted" means and what every client-facing reader wants. The filter
        exists for the routes that raise **their own** sentence for **their own** rule: since a
        stale reference map became a block, `generate_h3`'s prompt gate has to ask for prompt
        blocks specifically, or it would answer a stale shot with `READINESS_REFUSAL` — "no prompt
        on SHOT 07" about a shot with a prompt — while the stale refusal it already has sits three
        checks further down. Narrowing the question is how the two stay one sentence each.
        """
        return [
            shot_id
            for note in self.blocking
            if not kind or note.kind == kind
            for shot_id in note.shot_ids
        ]

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


def take_coverage_note(shot: Shot) -> tuple[str, str]:
    """Whether this shot's window still sits inside the picture its take holds.

    ``(kind, sentence)``, or ``("", "")`` — `window_band_note`'s shape, and a warning on the same
    terms: it never blocks, and the sentence says so. The Director's ask of 2026-08-21, in full:

        "if the bounds of the shots window are dragged beyond where that clip covers then the
        shot would turn yellow to warn that the bounds was gone past. The user could then
        readjust or regenerate to fit the newly wanted shot timeframe. This should help prevent
        lipsync clips from being dragged away from where it actually matches up with the music."

    **Answered only when the take was snapshotted.** `latest_take_duration` is 0 for every take
    rendered before the snapshot existed (2026-08-21 — including all 33 in the Director's own
    project) and for every hand-picked clip, whose bookkeeping `select_shot_clip` clears. Those
    takes cannot be coverage-checked by anything on the manifest: the only window there is the
    *live* one, which is a fact about the plan rather than about the file, and a check run against
    it would report a shot as uncovered precisely because it had been edited — which is the state
    this is supposed to be able to tell apart. Silence, deliberately, and never a guess: the same
    ruling `RESTORE_AUDIO_UNDESCRIBED_TAKE` records for the same pair of fields.

    The take's length is `over_render_frames(latest_take_duration) / H3_FPS`: the count the
    submission asked H3 for, computed from the window it asked for it with — the one function the
    payload builders and the submission route both compute their length with, so the picture named
    here is the picture that was requested. It is not a measurement, and the sentence does not
    claim to be one; `assembly` measures the file itself and refuses there.

    The offset is `latest_take_lead + trim_nudge` — `api.effectiveOffset`'s number and assembly's,
    read off the Shot and never recomputed, because a pre-margin take and a post-margin one are
    indistinguishable by arithmetic on their lengths.
    """
    if not shot.latest_output or shot.latest_take_duration <= 0:
        return "", ""
    take_seconds = over_render_frames(shot.latest_take_duration) / H3_FPS
    offset = shot.latest_take_lead + shot.trim_nudge
    if offset < -TAKE_COVERAGE_TOLERANCE_SECONDS:
        return NOTE_KIND_TAKE_UNCOVERED, SHOT_WINDOW_BEFORE_TAKE.format(behind=-offset)
    needed = offset + shot.duration
    if needed > take_seconds + TAKE_COVERAGE_TOLERANCE_SECONDS:
        return NOTE_KIND_TAKE_UNCOVERED, SHOT_WINDOW_PAST_TAKE.format(
            past=needed - take_seconds,
            take=take_seconds,
            needed=needed,
            offset=offset,
            duration=shot.duration,
        )
    return "", ""


def _telling_words(settings: list[Asset]) -> dict[str, set[str]]:
    """Each setting Asset's name reduced to the words that tell it apart from the others.

    A word carried by **more than one** of these names distinguishes none of them and is dropped.
    On the live plan that is exactly `"warehouse"`, which both settings are named after, leaving
    `{"dusk", "bed"}` against `{"gritty", "floor"}` — the words a Director would point at.

    Self-calibrating, which is the point: it needs no stop-word list, because the words worth
    ignoring are decided by this project's own library rather than by a table someone has to keep.
    A shared article or preposition falls out of it for free, and so does a house term every
    location in the project is named with. Two settings that share a name reduce to two empty sets
    and can never disagree with anything, which is the honest answer for a pair nothing separates.
    """
    per_asset = {asset.id: _words(asset.name) for asset in settings}
    seen: Counter[str] = Counter()
    for words in per_asset.values():
        seen.update(words)
    shared = {word for word, count in seen.items() if count > 1}
    return {asset_id: words - shared for asset_id, words in per_asset.items()}


def _quoted_words(words: set[str]) -> str:
    """`"floor"`, or `"bed" and "dusk"` — the evidence, listed for a sentence.

    Sorted, so one project produces one wording: the set is a set, and a note whose text depends on
    iteration order is a note two runs of the same pure report disagree about.
    """
    listed = [f'"{word}"' for word in sorted(words)]
    if len(listed) == 1:
        return listed[0]
    return f"{', '.join(listed[:-1])} and {listed[-1]}"


def video_soundtrack_note(project: Project, shot: Shot) -> tuple[str, str]:
    """Whether this shot cites a video whose sound will not reach H3: ``(kind, sentence)``.

    `setting_conflict_note`'s shape and its terms — per shot, never blocks — and the one note in
    this list that reports a *deliberate* property of the render rather than something the
    Director might want to change. See `SHOT_VIDEO_SOUNDTRACK_UNCONDITIONED` for the decision and
    why it is stated here at all: the payload sent no `has_audio` until 2026-08-26, so the
    soundtrack was discarded in silence, and being silent about it is the one option that was
    ruled out.

    **Which citations count is `numbered_references`' answer, not a second one.** It is the same
    walk the payload appends media by, so the `<Video N>` this sentence prints is the slot the
    render actually wires — a shot citing a picture and then a video is told about `<Video 1>`,
    which is what the map and the expansion call it too. A citation whose Asset this project no
    longer holds travels as a picture there and so is silent here, correctly: nothing is known
    about it, least of all whether it has a soundtrack.

    Asked of every shot on `window_band_note`'s argument, including one whose prompt is still
    blank, because it is true of the shot either way.
    """
    cited = [entry for entry in numbered_references(project, shot) if entry.kind == "video"]
    if not cited:
        return "", ""
    return NOTE_KIND_VIDEO_SOUNDTRACK, SHOT_VIDEO_SOUNDTRACK_UNCONDITIONED.format(
        references=", ".join(
            f"{entry.tag} ({entry.asset.name})" for entry in cited if entry.asset
        ),
        reference_word="reference" if len(cited) == 1 else "references",
    )


def setting_conflict_note(project: Project, shot: Shot) -> tuple[str, str]:
    """Whether this shot has two sources of location: ``(kind, sentence)``, or ``("", "")``.

    `window_band_note`'s shape and `take_coverage_note`'s terms — it never blocks, and the sentence
    says so. See `SHOT_SETTING_FIGHTS_SECTION` for the Director's report, the ruling, and the
    measurement (5 of 30 on the live plan, against 30 of 30 for the naive form of the same idea).

    **Structural, and it never reads for meaning.** The comparison is word overlap between the
    section's look prompt and the *names of this project's own setting Assets*, through `_words` —
    the same tokeniser the sameness check already scores prompts with. It fires when some setting
    the shot did **not** cite matches that look better than the one it did. Nothing here knows what
    a warehouse is, and nothing may: this report runs on every render submission and stays pure,
    offline and deterministic.

    **Silence is the answer to every ambiguity**, deliberately, because this is a warning and a
    warning that fires on a maybe is the noise that empties the list:

    * Fewer than two settings, and there is no second location to disagree with.
    * No section, or a section with no look prompt — `song_section` returns nothing rather than
      guessing, and a blank look describes no place.
    * A look prompt that matches both settings equally, or neither. The live Outro
      ("Empty warehouse under cold moonlight") matches both on the shared word and is silent, which
      is right: it names no floor and no bed. A strictly better rival is required, not a tie.
    * A shot citing only a character, a prop or a style. Location is the whole subject here.
    * A shot that **already cites the setting the look describes**, alongside another one. Its
      remedy would be "cite the one you are already citing", and a note whose fix is a no-op is a
      note a Director learns to skip. Such a shot does have a problem — it names two places itself
      — but that is a different fact, with a different fix, and this sentence would describe
      neither of them. Reachable by citation *order* rather than only by a double citation: the
      walk reports the first cited setting that disagrees, so a shot citing the floor and then the
      bed would otherwise be told to cite the floor.

    No stemming and no synonyms: `"floors"` does not match `"floor"`, and a project that words its
    look prompts away from its asset names gets no warning rather than a guessed one. That is the
    cost of refusing a model call in here, it is stated rather than hidden, and it fails towards
    silence — which is the direction a warning is allowed to fail in.

    The **first** cited setting that disagrees is reported, and against its **best** rival, so one
    shot yields at most one note: this is one fact about the shot ("it is in two places"), and
    listing it once per pairing would count a single problem several times.

    **Two of the guards below are cost rather than behaviour**, and it is written down because a
    mutation sweep found them (2026-08-23) and the next reader would otherwise take their survival
    for a hole in the tests. Neither changes an answer:

    * the fewer-than-two-settings return — with one setting the rival loop skips it as already
      held, and with none the outer loop has nothing to walk, so no rival can be found either way;
    * the blank-look-prompt return — `_words("")` is the empty set, every `matched` is then empty,
      and firing needs `len(matched) > held` with `held >= 0`, which the empty set cannot satisfy.

    They stay because they are what makes this cheap on the plans it says nothing about, which is
    most of them, and `readiness_report` claims that cost in its own docstring. **`section is None`
    is not one of them** — without it, `section.prompt` raises on a shot in no section.
    """
    settings = [asset for asset in project.assets if asset.kind == "setting"]
    if len(settings) < 2:
        return "", ""
    section = song_section(project, shot)
    if section is None or not section.prompt.strip():
        return "", ""
    look = _words(section.prompt)
    telling = _telling_words(settings)
    # `numbered_references` rather than a second walk of `citations`: it is **the** reference
    # numbering, it drops a citation whose Asset this project no longer holds, and a warning about
    # a reference must be reading the same references the payload will wire.
    cited_settings = [
        entry.asset
        for entry in numbered_references(project, shot)
        if entry.asset is not None and entry.asset.kind == "setting"
    ]
    held_ids = {asset.id for asset in cited_settings}
    for cited in cited_settings:
        held = len(telling[cited.id] & look)
        best: tuple[Asset, set[str]] | None = None
        for other in settings:
            # `held_ids` rather than `cited.id`, so a rival this shot already holds is never the
            # remedy. It covers the self-comparison too — a setting is always in its own shot's set.
            if other.id in held_ids:
                continue
            matched = telling[other.id] & look
            if len(matched) > held and (best is None or len(matched) > len(best[1])):
                best = (other, matched)
        if best is not None:
            rival, matched = best
            return NOTE_KIND_SETTING_CONFLICT, SHOT_SETTING_FIGHTS_SECTION.format(
                cited=cited.name,
                label=section.label,
                rival=rival.name,
                words=_quoted_words(matched),
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

    A **stale reference map** blocks, alongside the empty prompt, and is the one thing here that is
    not decided in this module: `reference_map.stale_reference_map` is the submit route's own test,
    imported so the pre-flight cannot hold a second opinion. See `SHOT_WITH_STALE_REFERENCE_MAP`
    for why it blocks rather than warns.

    `ready_count` deliberately still counts **shots that have a prompt**, which is what
    `api.js`'s `readinessSummary` prints it as ("33 of 33 shots have a prompt"), and a stale shot
    has one. What that shot loses is `ready` and a line in `blocking`, so the same summary goes on
    to say "1 cannot be submitted" — which is the true statement about it, in the sentence that
    already existed for it.

    A **setting that fights its section's look** warns, in `window_warnings`, and is the one check
    here that reads the project's Assets rather than the Shot alone — see `setting_conflict_note`
    for the rule and for the hit rate it was measured at before it was built. It is offline and
    deterministic like everything else in this function; it must be, because this runs on every
    render submission.

    Still pure and still cheap: no model, no file, no ComfyUI, and nothing written back. The one
    added cost is one `models.numbered_references` walk per **expanded** shot; an unexpanded shot
    returns before walking anything. The setting check walks the references of a shot whose section
    carries a look prompt, and returns before walking anything for a project holding fewer than two
    settings — which is every project until the Director makes a second one.
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
        # The take-coverage note rides in the same list as the band's, drawn in the same amber by
        # the same reader. A second list, or a second colour, would be a second mechanism for one
        # sentence a Director reads the same way: "this window is a problem and you may carry on".
        # After the band's note for this shot, so a shot that is both long and uncovered reads in
        # the order the two facts were decided; the client's own precedence decides which one the
        # clip's single accessible name carries.
        coverage_kind, coverage_reason = take_coverage_note(shot)
        if coverage_kind:
            window_warnings.append(
                ReadinessNote(
                    shot_ids=[shot.id],
                    labels=[shot_label(project, shot)],
                    reason=coverage_reason,
                    kind=coverage_kind,
                )
            )
        # And whether this shot is being given two locations at once. Third in the list for this
        # shot, on the same terms as the two above: per-shot, never blocking, its own kind. Asked
        # of **every** shot on `window_band_note`'s argument — it is a fact about this one shot's
        # citations and its section, just as true of a shot whose prompt is still blank, and
        # hiding it until the prompt was written would make it appear only once the Director had
        # stopped thinking about where the shot is.
        conflict_kind, conflict_reason = setting_conflict_note(project, shot)
        if conflict_kind:
            window_warnings.append(
                ReadinessNote(
                    shot_ids=[shot.id],
                    labels=[shot_label(project, shot)],
                    reason=conflict_reason,
                    kind=conflict_kind,
                )
            )
        # And whether a cited video's soundtrack is being dropped. Fourth for this shot, on the
        # same terms as the three above, and last because it is the only one of them that asks
        # for nothing: the others name a thing to fix and this one states how the render works.
        soundtrack_kind, soundtrack_reason = video_soundtrack_note(project, shot)
        if soundtrack_kind:
            window_warnings.append(
                ReadinessNote(
                    shot_ids=[shot.id],
                    labels=[shot_label(project, shot)],
                    reason=soundtrack_reason,
                    kind=soundtrack_kind,
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
        # And whether the expansion this shot is holding still describes the references it cites.
        # `reference_map.stale_reference_map` is the submit route's own function, imported rather
        # than re-derived: the pre-flight and the gate answer this from one implementation or they
        # will eventually answer it differently, which is the whole failure this note exists to
        # close in the other direction.
        #
        # Asked of **every** shot, blocked ones included, on `window_band_note`'s argument: a stale
        # map is a fact about this one shot rather than a comparison with another, so it is just as
        # true of a shot whose prompt is also blank, and hiding it until the prompt was written
        # would make it appear only at the moment the other note stopped. A shot with no expansion
        # is not stale and is silent here — that function's first line — so a plan being written
        # rather than rendered reports nothing.
        if stale_reference_map(project, shot):
            blocking.append(
                ReadinessNote(
                    shot_ids=[shot.id],
                    labels=[shot_label(project, shot)],
                    reason=SHOT_WITH_STALE_REFERENCE_MAP,
                    kind=NOTE_KIND_STALE_MAP,
                )
            )

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


# --------------------------------------------------------------------------------------------
# Render timing — how long a render actually took, recorded by the application that ran it.
#
# **Why this exists.** Until 2026-08-21 this application recorded nothing about render cost.
# `RenderJob.updated_at` was set by its `default_factory` and no settle path ever wrote it
# again, so every settled job in the Director's live manifest carried `updated_at ==
# created_at` to the microsecond. The one render-cost figure this codebase acted on — a
# 221-frame window "took 2.2 HOURS", quoted into a live constant's justification and onward
# into three other files — had no primary record anywhere: it was a comment citing itself.
# When it was finally challenged it turned out to be wrong by roughly 3.4x, and settling that
# meant reading mtimes off the `.mp4` files in ComfyUI's output tree by hand. Nothing below is
# clever; it is the measurement this application should always have been taking.
# --------------------------------------------------------------------------------------------

#: `render_seconds` came from ComfyUI's own execution clock — `execution_start` to
#: `execution_success`/`execution_error` in `/history`'s `status.messages`. The render alone:
#: queue wait is excluded, so two jobs measured this way are directly comparable.
JOB_TIMING_FROM_COMFY = "comfy"

#: `render_seconds` came from this record: `created_at` (enqueue) to the settle. It therefore
#: includes any time the prompt spent **waiting** behind other prompts, and is only an upper
#: bound on the render. Every surface that shows it has to say so.
#:
#: One exception, and it is the reason `render_timing_summary` reads `prompt_id` as well as this
#: field: a job with an **empty** `prompt_id` is local work (an export; see that field's note
#: below), started the moment its record was created and never queued anywhere, so its span is
#: exact rather than an upper bound. The label still says `record`, because it names where the
#: span came from and not how much to trust it in one case.
JOB_TIMING_FROM_RECORD = "record"

#: The job settled, and no length could be measured for it. Reached one way only: the record's
#: own span ran **backwards**, which is a clock adjustment between `created_at` and the settle
#: rather than a render that finished before it started.
#:
#: A third value rather than leaving the field empty, because empty already means something
#: else and something false here — "settled before this application measured anything, and
#: nothing was ever invented for it", which is what every surface says about a job that carries
#: it. It also restores idempotence: the guard in `stamp_job_settled` keys on this field, so a
#: settle that left it empty could be stamped again and again, moving `updated_at` each time.
JOB_TIMING_UNMEASURED = "unmeasured"


def format_duration(seconds: float) -> str:
    """Seconds as the Director reads them: ``42s``, ``6m18s``, ``2h04m``.

    Compact because it sits in a table column, and never rounded up into the next unit — a
    render reported as `7m` when it took 6m59s is the kind of smoothing this whole module
    exists to stop. Negative and non-finite inputs answer ``"—"``; neither is a duration, and
    a clock adjustment can produce the first.
    """
    if not isfinite(seconds) or seconds < 0:
        return "—"
    whole = int(seconds)
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m{whole % 60:02d}s"
    return f"{whole // 3600}h{(whole % 3600) // 60:02d}m"


def stamp_job_settled(job: RenderJob, *, elapsed_seconds: float | None = None) -> bool:
    """Record that this job has stopped, and how long it was running. Returns whether it wrote.

    **Every settle path calls this** — completion and failure (`apply_job_history`), the
    `missing_ticks` death, supersession (`supersede_target_jobs`), the cancel route, the
    never-submitted settle, the orphaned-assembly heal, and both ends of an export. Enumerated
    rather than centralised in one status setter because the paths live in two modules and each
    writes a different `error`; what they share is this one call, and a settle path that forgets
    it is the defect this function exists to make findable.

    `elapsed_seconds` is ComfyUI's own execution span where the caller has one
    (`HistoryResult.elapsed_seconds`); the render alone, and preferred whenever it exists. With
    no measurement to hand, the span is taken off this record — `created_at` to now — which is
    **enqueue** to settle and therefore an upper bound rather than a render time. The two are
    not interchangeable and `render_seconds_source` is what keeps them distinguishable
    afterwards; nothing may show one as though it were the other.

    **Idempotent, and that is load-bearing.** A job that already carries a source is left
    entirely alone, `updated_at` included: terminal is terminal, so a second call can only be a
    later clock overwriting the real measurement. That reason stands alone — an earlier draft of
    this docstring also claimed the guard kept a re-read from looking like a change to the
    reconciler, which is not true of the code: `reconcile_project_jobs`'s change test reads
    `status`, `output_files` and `error` and nothing else, and it never reaches a settled job in
    the first place.

    Which is why a span that runs backwards still writes a *source* — `JOB_TIMING_UNMEASURED`.
    Leaving it empty left the job stamped but sourceless, so the guard above did not hold it and
    every later call moved `updated_at` again, while every surface described a job settled today
    as one settled before this application measured anything.

    What it does **not** do: touch `status`, `error`, or the target. Those are each caller's own
    decision and settling is not one act — see `apply_job_history` and `supersede_target_jobs`.
    """
    if job.render_seconds_source:
        return False
    job.updated_at = now_utc()
    if elapsed_seconds is not None and isfinite(elapsed_seconds) and elapsed_seconds >= 0:
        job.render_seconds = round(elapsed_seconds, 3)
        job.render_seconds_source = JOB_TIMING_FROM_COMFY
        return True
    span = (job.updated_at - job.created_at).total_seconds()
    # A negative span is a clock that moved, not a render that finished before it started. The
    # stamp still lands — the job did settle — and nothing is claimed about its length, but the
    # settle is still *recorded* as one: `JOB_TIMING_UNMEASURED` rather than the empty string,
    # which means "settled before this application measured anything" and would be a lie about a
    # job that settled just now. See that constant.
    if span >= 0:
        job.render_seconds = round(span, 3)
        job.render_seconds_source = JOB_TIMING_FROM_RECORD
    else:
        job.render_seconds_source = JOB_TIMING_UNMEASURED
    return True


def render_timing_summary(job: RenderJob) -> str:
    """One honest line about what this job cost, or ``""`` where nothing was measured.

    The whole point is that the caveat travels with the number. A `comfy`-sourced timing is a
    render time and says so plainly; a `record`-sourced one is enqueue-to-settle and says *that*
    plainly, because for anything submitted as a batch the difference is most of the number and
    a reader who is not told will read it as render cost. That misreading is precisely how a
    221-frame render came to be recorded as taking 2.2 hours.

    **The source is read before the status**, and that order is the correction of a real defect.
    A job that failed or was interrupted still has ComfyUI's execution clock on it — the span runs
    `execution_start` → `execution_error`/`execution_interrupted`, which is time on the GPU and
    nothing else — and describing it as "the time the record was open, not render time" inverted
    the one caveat this function exists to carry. A failed render is also the most useful cost
    datum here: a 226-frame window that OOMs after three minutes is exactly what a Director needs
    to know before asking for it again. So a `comfy`-sourced non-`complete` job is reported as
    time spent rendering, and only a `record`-sourced one is reported as a record span.

    A `record`-sourced non-`complete` job is never described as having rendered: a cancellation
    that stood open for forty minutes rendered for some unknown part of that, and "rendered in
    40m" would be a fabrication. It is reported as what it is — how long the record was open.

    **A finished piece of local work is not a queue.** A job with an empty `prompt_id` never went
    near ComfyUI (an export; see `PENDING_SUBMISSION_PROMPT_ID` for why the empty string is that
    marker and not "unsubmitted"), so `created_at` really is when the work started and, once it
    has run to `complete`, the record's span is the whole of it. Telling the Director that
    "ComfyUI reported no execution clock for this prompt, so the wait in the queue is included"
    about an assembly was a caveat invented out of nothing, about a component the job never
    touched, and it made an exact number look like an upper bound.

    `complete` and not merely local, because the exactness comes from both ends of the span being
    real. An export orphaned by a crash is settled by `heal_orphaned_local_jobs` at the next boot,
    so its span runs from enqueue to whenever somebody restarted the application — which can be a
    machine that was switched off overnight, and is emphatically not how long the export ran. That
    one takes the branch above and is reported as a record span, which is what it is.

    The frame count is included when there is one, because a duration without it is
    uninterpretable: 6m18s is unremarkable at 141 frames and impossible at 277. Empty string
    for an unmeasured job rather than "unknown", so a caller can simply not draw the line.

    **A slow render is never marked as one, and that is a decision rather than an omission.**
    The evidence that prompted the question (2026-08-23): of six instrumented jobs, the four at
    141 frames span **13.7x** end to end, which is far too wide to be sampler variance and is the
    signature of the card spilling out of VRAM part-way through. It was proposed that such a job
    carry a marker in the timing column so the number could be read as "slow because it ran out of
    memory" rather than as this configuration's cost.
    **The Director declined it: no marker, just show the time.** Not to be re-proposed.

    The reason it is the right call is that this function's whole discipline is that it says only
    what was measured. Nothing on a `RenderJob` records VRAM pressure — ComfyUI's execution clock
    is a duration and nothing else — so any marker would be *inferred* from the duration being
    long, which is a restatement of the number dressed as a cause. Establishing the cause would
    need a memory reading this application does not take and must not start taking; **no
    `nvidia-smi` dependency belongs anywhere in this codebase**, which is the same standing rule
    that keeps ComfyUI user-managed. A 13.7x spread is visible in the column already, to a reader
    who has the frame count beside it, and that is exactly what the column is for.
    """
    if not job.render_seconds_source:
        return ""
    if job.render_seconds_source == JOB_TIMING_UNMEASURED:
        return (
            f"{job.status}; the clock moved between this record being created and it settling, "
            f"so no length was measured"
        )
    length = format_duration(job.render_seconds)
    frames = f", {job.render_frames} frames" if job.render_frames else ""
    if job.render_seconds_source == JOB_TIMING_FROM_COMFY:
        if job.status == "complete":
            return f"rendered in {length}{frames}"
        return (
            f"{job.status} after {length} of rendering{frames} (ComfyUI's own execution clock, "
            f"so this is time on the GPU and not queue wait)"
        )
    if job.status != "complete":
        return f"{job.status} after {length}{frames} (time the record was open, not render time)"
    if not job.prompt_id:
        return (
            f"{length} start to finish{frames}; local work that never went to ComfyUI, so this "
            f"is the whole job rather than an upper bound"
        )
    queued = " — this job was submitted in a batch" if job.batch_id else ""
    return (
        f"{length} from queued to done{frames}; ComfyUI reported no execution clock for this "
        f"prompt, so the wait in the queue is included{queued}"
    )


#: The `kind` of job that submits an H3 graph, and therefore the only kind a sampling bundle can
#: describe. Read from `kind` rather than recorded on the job, because for every other kind the
#: answer needs no record: this application submits no MiniMax H3 graph for a `music`, `flux`,
#: `multiview`, `edit`, `ltx` or `post` job, and that is a fact about the routes rather than about
#: any particular render.
JOB_KIND_WITH_SAMPLING_BUNDLE = "h3"


def sampling_bundle_summary(job: RenderJob) -> str:
    """One honest line about which sampling bundle produced this take.

    `render_timing_summary`'s shape and its voice, for the same reason: the bundle became a
    per-project choice on 2026-08-23, so a project's takes are a *mixture*, and a take whose
    bundle is unnamed is as uninterpretable as a duration whose caveat was dropped. Written once
    here and mirrored in `app.js`, executed against each other under node, because a sentence
    written twice in two languages is how the two stop meaning the same thing.

    **An old job reads as unknown and is never given a value it never had.** Every job settled
    before this field existed carries no bundle, and the 49 in the Director's live manifest are
    all of them; defaulting those to `"default"` would invent a measurement, which is exactly the
    sin behind the fabricated "221 frames = 2.2 hours" figure that this application spent two days
    retiring. So the sentence says nobody recorded one, dates the instrumentation, and states
    plainly that nothing was invented — the same three things the timing fallback says.

    A record naming `NO_EVIDENCED_BUNDLE` is **not** unknown and must never read as it. It is an
    H3 submission through the first/last keyframe or text-only Director graph, both of which
    refuse a named bundle and render their own way; a Director who read "unknown" there while the
    project was set to turbo would supply the wrong answer themselves.

    Never `""`, unlike `render_timing_summary`. Every job has an answer here — a bundle, no
    evidenced bundle, no H3 graph, or no record — and a caller drawing a cell needs a sentence for
    all four rather than a fallback of its own invented at the call site.
    """
    if job.kind != JOB_KIND_WITH_SAMPLING_BUNDLE:
        return (
            f"A sampling bundle is a MiniMax H3 setting, and this is a {job.kind} job; "
            f"it submitted no H3 graph."
        )
    if job.sampling_bundle is None:
        return (
            "No sampling bundle was recorded for this job. Every H3 submission has recorded one "
            "since 2026-08-23; a job submitted before that carries none, and none was invented "
            "for it."
        )
    bundle = job.sampling_bundle
    if bundle.name == NO_EVIDENCED_BUNDLE:
        return (
            "No evidenced bundle: this shot rendered through an H3 graph that has none — the "
            "first/last keyframe and text-only Director graphs load different checkpoints and "
            "sample their own way, whatever the project is set to."
        )
    lora = (
        f"{bundle.lora} at {format_lora_strength(bundle.lora_strength)}"
        if bundle.lora
        else "no LoRA"
    )
    return (
        f"Submitted on the {bundle.name} bundle: {bundle.steps} steps, {bundle.sampler} / "
        f"{bundle.scheduler}, {lora}."
    )


def format_lora_strength(strength: float) -> str:
    """A LoRA strength as both languages write it: ``1``, ``0.7``, ``-1.5``.

    Its own function because the two renderings have to agree character for character and the
    obvious spellings do not: Python's `str(1.0)` is ``1.0`` where JavaScript's is ``1``. `%g` is
    the one format that matches `Number.prototype.toString` across the range a strength can hold
    (`H3_LORA_STRENGTH_LIMITS` is -100 to 100), and the contract test executes both over it.
    """
    if not isfinite(strength):
        return "—"
    return f"{strength:g}"


def sampling_bundle_cell(job: RenderJob) -> str:
    """The queue column's compact form: the bundle's name and the steps it actually sampled.

    `render_timing_cell`'s job one column over, and terse for its reason — the column is narrow
    and a Director comparing two takes reads it by scanning, not by hovering. The full sentence
    rides the same cell's `title`.

    The step count is in the cell rather than only in the tooltip because it is the number the
    choice is made on and the number a *name alone* would get wrong: `H3Request.steps` overrides
    the bundle's own count, so a cell reading `turbo` with nothing beside it could be four steps
    or twenty. Two takes at `default · 20` and `turbo-references2v · 8` are told apart at a
    glance, which is the whole of what this column is for.

    **Unknown is a word, not a dash.** `—` is what a *known* negative draws — a job that submitted
    no H3 graph at all, which `kind` establishes without any record — and `none` is the other one.
    A job whose bundle nobody recorded has to be distinguishable from both by reading rather than
    by hovering. That is the one requirement this column was added under.
    """
    if job.kind != JOB_KIND_WITH_SAMPLING_BUNDLE:
        return "—"
    if job.sampling_bundle is None:
        return "unknown"
    if job.sampling_bundle.name == NO_EVIDENCED_BUNDLE:
        return "none"
    return f"{job.sampling_bundle.name} · {job.sampling_bundle.steps}"


#: The history statuses adopted verbatim onto a job. Anything else ComfyUI invents — it has
#: reported bare "success" strings before `HistoryResult` normalised them — reads as "running",
#: which is the only honest reading of "there is an entry and it is not finished".
_ADOPTED_HISTORY_STATUSES = frozenset({"queued", "running", "complete", "error"})

#: How many consecutive reconcile ticks a prompt may be unknown to both ComfyUI's queue and
#: its history before the job is settled as lost. Three ticks of the browser's two-second
#: poll rides out a restart's ambiguous seconds; a prompt still unknown after that died
#: with the queue.
MISSING_TICKS_LIMIT = 3

#: What a settle that produced nothing leaves behind, said once and shared by every sentence that
#: has to say it. Before 2026-08-23 all three of them ended "Render again re-opens the shot",
#: which stopped being true the moment `shot_status_after_failed_render` started releasing a shot
#: that never got a take: such a shot is already open, and `Render again` is not drawn for it.
#: Both halves are named because both are reachable from one settle — see that function.
SHOT_AFTER_FAILED_RENDER = (
    "A shot left with no take by this goes back to ready, so the next batch picks it up; one "
    "that still holds an earlier take stays settled and offers Render again."
)

JOB_LOST_WITH_QUEUE = (
    "ComfyUI no longer knows this prompt — its queue was cleared, restarted, or crashed "
    f"before the render ran. Nothing was produced. {SHOT_AFTER_FAILED_RENDER}"
)

#: What `prompt_id` holds between a job record being saved and its graph being accepted by
#: ComfyUI. The Director's 2026-08-21 ruling put the record first — a save that loses a race
#: then refuses *before* any GPU time is spent — which means a record briefly exists for a
#: prompt that has no id yet, and something has to go in the field.
#:
#: **Not the empty string**, which this application already spends on a different meaning:
#: an empty `prompt_id` is the *local-work* marker (`heal_orphaned_local_jobs`, the assemble
#: route's busy check, `api.js`'s assembly-progress branch), and it is deliberately excluded
#: from `reconcilable_jobs` because nothing on ComfyUI can answer for it. An `h3` or `post`
#: record carrying an empty id would therefore be read as an ffmpeg job — reconciled by
#: nobody and, for `kind="post"`, healed to an assembly error at the next startup.
#:
#: A non-empty sentinel gets the opposite and correct treatment for free, with no new rule
#: anywhere: the record is reconcilable, so the poll keeps watching it and the shot reads as
#: in flight; ComfyUI's queue never contains this string and its history answers `known=False`
#: for it, so an orphan — a crash between the save and the submit — is settled by the existing
#: three-unknown-tick rule exactly as a prompt that died with the queue is. Deliberately not a
#: UUID shape, so it can never be mistaken for one ComfyUI minted, and `ComfyClient.cancel` is
#: safe against it (a delete of an id in no bucket is a no-op and no `/interrupt` follows).
PENDING_SUBMISSION_PROMPT_ID = "pending-submission"

#: What a record says once its graph was never accepted. Two callers, one sentence: the
#: submission routes settle their own record when `comfy.submit` raises, and the reconciler
#: settles one still carrying `PENDING_SUBMISSION_PROMPT_ID` after `MISSING_TICKS_LIMIT`
#: unknown ticks — the case where the process died between the save and the submit.
#:
#: Separate from `JOB_LOST_WITH_QUEUE` because that sentence claims something about ComfyUI
#: ("no longer knows this prompt") which was never true here: ComfyUI never knew it at all,
#: and there was no queue for it to be cleared from. The consequence the Director acts on is
#: identical, so the last two sentences are.
JOB_NEVER_SUBMITTED = (
    "The record for this render was saved and its graph was then never accepted by ComfyUI, "
    f"so no prompt was ever queued. Nothing was produced. {SHOT_AFTER_FAILED_RENDER}"
)


def accept_submission(job: RenderJob, prompt_id: str) -> None:
    """Write the accepted prompt id onto a record that was saved before the graph went out.

    The second half of the record-first ordering, and every field it touches is one the
    pending window could have left wrong:

    * `prompt_id` stops being the sentinel and becomes the thing ComfyUI answers for;
    * `status` returns to `queued`, and `error` and `missing_ticks` are cleared, because the
      reconciler may have reached this record while the submission was still in progress and
      settled it as never submitted. That verdict was right on the evidence it had and is
      wrong now — the graph *was* accepted — and the window is wide enough to matter: the
      pre-submission VRAM eject is allowed twenty seconds and the `/prompt` call thirty, while
      three ticks of the browser's poll is six. A record left `error` with a live prompt id is
      never reconciled again, which is the orphaned take this whole ordering exists to prevent.

    Nothing else moves. What the acceptance implies for the *target* — a Shot's status, an
    Asset's `prompt_id`, the Song being replaced — stays at each route, because each of those
    is a different promise and one of them is destructive.
    """
    job.prompt_id = prompt_id
    job.status = "queued"
    job.error = ""
    job.missing_ticks = 0


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


def shot_status_after_failed_render(shot: Shot) -> ShotStatus:
    """Where a Shot lands when its render stops without completing. The one rule, three callers.

    The Director's report (2026-08-23): a Generate All at 20 steps was cancelled from ComfyUI's own
    UI part-way through, twenty-six renders died with it, and every one of those shots came back
    reading `error` — so the inspector offered **Flag for re-render**, a control that means "this
    shot has a take I do not like". None of them had a take. Nothing had rendered. In their words:
    "thier option is 'Flag for re-render' instead of just returning to ready after the fail."

    **`error` and no take is not a failure state, it is an un-started one.** Such a shot was armed,
    it was submitted, and it never got anything back. The honest place for it is exactly where it
    stood before the submission — `ready` — from which `Generate All` picks it up again, the
    per-shot submit route accepts it (`generate_h3` gates on `status == "ready"`), and the
    inspector draws `Back to draft` rather than a re-render flag over a take that does not exist.
    Nothing has to be un-done first: `render_again` exists to *re-open* a settled shot, and a shot
    that never closed does not need re-opening.

    **A shot that still holds an earlier take keeps `error`, unchanged.** That is a real settled
    state with a real file behind it: the take on screen is the previous one, `Render again` and
    `Flag for re-render` are the right controls for it, and releasing it to `ready` would put a
    shot with a perfectly good take back in the batch's ready set to be rendered over.

    **The test is `shot_has_take`, not `status`** — that function's own three reasons, and this is
    a fourth caller of them rather than a fourth reading. Most sharply here: a shot can read
    `error` and hold a good earlier take, and a shot can read `complete` and hold nothing at all.
    Only `latest_output` separates the two cases this function has to separate.

    Pure, and returns rather than writes, because the three settle paths each guard differently on
    the way in — `cancel_job` and the missing-ticks death only touch a shot that is still in flight,
    while `apply_job_history` adopts ComfyUI's own verdict unconditionally — and folding those
    guards in here would make one of them silently apply to the other two.
    """
    return "error" if shot_has_take(shot) else "ready"


def apply_job_history(project: Project, job: RenderJob, history: HistoryResult) -> None:
    """Write one ComfyUI history answer onto the job, and onto whatever the job was producing.

    The one place a completion moves project state — the per-job route and the reconciler both
    delegate here, so `Shot.status`, an Asset's landed file and a Song's audio path cannot be
    adopted by two subtly different rules. Everything in here is a decision about data already
    fetched; nothing touches the network.

    This is also where a *completed* render's cost is recorded, and the one settle path with a
    real measurement to record: ComfyUI stamps its own `execution_start` and `execution_success`
    into the history entry this function is reading, so the duration written here is the render
    with the queue wait already excluded. See `stamp_job_settled`.
    """
    settled_before = job.status in TERMINAL_JOB_STATUSES
    job.status = (
        history.status if history.status in _ADOPTED_HISTORY_STATUSES else "running"
    )
    # On the transition into a terminal status, and only there. A history re-read of a job that
    # was already settled must not re-stamp it with a later clock, and a queued→running move is
    # not a settle at all.
    if not settled_before and job.status in TERMINAL_JOB_STATUSES:
        # `getattr` with a `None` default, on the `known` flag's precedent below: a history
        # double built before this existed reports no execution clock, and "no clock" is exactly
        # what the default means. The record's own span is then used instead, labelled as such.
        stamp_job_settled(job, elapsed_seconds=getattr(history, "elapsed_seconds", None))
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
            # `error` only where there is a take to be settled *over*; a shot this render left
            # with nothing is released back to `ready`. See `shot_status_after_failed_render`.
            # Nothing on this branch moves `latest_output` — only the `complete` branch above does
            # — so what is read here is the earlier take the shot was already holding, which is
            # exactly the thing the two cases have to be told apart by.
            shot.status = shot_status_after_failed_render(shot)


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

    That same rule is what heals a record saved before its graph went out and orphaned by a
    crash in between (the Director's 2026-08-21 ordering). No branch here exempts it: the
    sentinel is an ordinary non-empty prompt id that ComfyUI has never heard of, so it is
    looked up and counted exactly like any other. Only the sentence it settles into differs —
    see `PENDING_SUBMISSION_PROMPT_ID` and `JOB_NEVER_SUBMITTED`.
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
                # A settle, so it is stamped like one. There is no ComfyUI measurement to
                # prefer here by definition — this branch is reached precisely because ComfyUI
                # has no record of the prompt — so the span is the record's own, and
                # `render_seconds_source` says `record` so nobody reads it as a render time.
                stamp_job_settled(job)
                # A record still carrying the pre-submission sentinel is not a prompt that
                # died with the queue — ComfyUI never had it — so it is settled in the
                # sentence that says so. Same terminal state, same consequence, an
                # explanation that is true. See `PENDING_SUBMISSION_PROMPT_ID`.
                job.error = (
                    JOB_NEVER_SUBMITTED
                    if job.prompt_id == PENDING_SUBMISSION_PROMPT_ID
                    else JOB_LOST_WITH_QUEUE
                )
                shot = next(
                    (item for item in project.shots if item.id == job.target_id), None
                )
                # The externally-killed path: ComfyUI's queue was cleared, restarted or crashed
                # under a live batch, which is precisely what the Director did on 2026-08-23 and
                # what left twenty-six takeless shots reading `error`. It settles the shot through
                # the same one rule the completion path and the cancel route use, so a render
                # killed outside this application and one cancelled inside it leave the shot in
                # the same place. See `shot_status_after_failed_render`.
                if job.kind == "h3" and shot and shot.status in ("queued", "running"):
                    shot.status = shot_status_after_failed_render(shot)
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
            # Settled, therefore stamped. Not a render time and `render_timing_summary` will
            # not call it one: an older prompt may still be executing on ComfyUI as this runs,
            # so what is recorded is how long the *record* stood open.
            stamp_job_settled(job)
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


def shot_has_take(shot: Shot) -> bool:
    """Whether this shot already has a video, which is the whole of the ``empty`` scope's test.

    `latest_output` and nothing else. It is the single pointer the whole application means by
    "this shot's video" -- the take player, the Monitor, assembly and `select_shot_take` all
    resolve through it -- so asking anything else here would invent a second definition.

    Deliberately **not** `status`. The three facts that rule status out, each observed rather
    than reasoned:

    * a reconciled job writes `status = "complete"` before it looks at `output_files`, so a
      completed render that produced no file leaves a `complete` shot with no video;
    * a failed re-render writes `status = "error"` and leaves `latest_output` untouched, so an
      `error` shot may very well still have a perfectly good earlier take -- and one that never
      rendered at all has none;
    * the Director's own live plan (2026-08-23) carries three `draft` shots that already hold a
      finished take. A status test would re-render all three; this one leaves them alone.

    And the converse is what makes a deleted take come back: clearing `latest_output` -- however
    it is cleared -- is exactly what puts the shot back in scope, because the pointer *is* the
    claim. A take whose file was deleted from disk behind the application's back still has its
    pointer, and stays out of scope; that is the same reading every other take path takes (they
    refuse with `TAKE_MISSING_FILE_REFUSAL` rather than pretending there was never a take).
    """
    return bool(shot.latest_output)


def batch_targets(
    project: Project, *, scope: str = "ready", replace_existing: bool = False
) -> tuple[list[Shot], list[tuple[Shot, str]]]:
    """The shots one batch will submit, in timeline order, and the named skips.

    Three scopes, per the spec's matrix. ``ready`` is FR-4's own set -- every shot standing
    at ``ready`` -- widened by ``replace_existing`` to settled (``complete``/``error``)
    shots that nothing protects; approved and locked settled shots are skipped **by
    name**. ``flagged`` is AD-5's resubmission set: exactly the flagged shots, with the
    same two protections named.

    ``empty`` is the Director's ask of 2026-08-23 -- "generate all shots that dont already
    have a video" -- and it is the *complement* of a take rather than any status class:
    every shot `shot_has_take` says no about. Two exclusions and both are borrowed rather
    than invented:

    * in-flight (``queued``/``running``) shots have no take *yet* and are silently absent,
      exactly as they are from the Replace Existing set. They are already rendering, which
      the queue panel shows, and a second submission is the one case that does real harm;
    * approved and locked shots are named, through the same `protected` the other two
      scopes use. An approved shot with no take is not reachable in practice, but a locked
      one is, and a locked shot silently missing from both lists reads as a bug.

    Draft shots **are** in this scope, and that is the one decision here that is not
    mechanical. The alternative was tested against the case the button was asked for: a
    freshly populated plan is thirty drafts with prose and no takes, so a scope that
    excluded drafts would do nothing on precisely the plan it was built for. The gate it
    steps around is already bulk-bypassable by design -- `Mark all drafts ready` promotes
    every draft in one click -- and it is not stepped around silently here either: the
    batch route arms each draft through `mark_shot_ready` itself, so the arming refusals
    (locked, approved, in-flight, no prompt) fire per shot and land in the report by name.
    "Emptiness blocks, sameness warns" is untouched: a draft with no prompt is refused,
    not submitted.

    Deliberately only the *meaning-level* protections are decided here (approval, lock --
    the two states a submission could never be right about). Everything mechanical --
    in-flight 409s, the readiness prompt gate, adapterless modes -- is left to the
    single-shot routes the batch delegates to, so no second copy of any of those rules
    exists to drift. A draft in the *flagged* scope is still not this function's business
    and still surfaces through the single-shot path's own "must be ready" refusal, which is
    what keeps that scope byte-identical across this change.
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
        elif scope == "empty":
            if shot_has_take(shot) or shot.status in ("queued", "running"):
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
