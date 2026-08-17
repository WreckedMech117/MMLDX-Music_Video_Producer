from __future__ import annotations

import math
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, BeforeValidator, Field, StringConstraints

from .batch import ReadinessReport, readiness_refusal, readiness_report, shot_label
from .comfy import ComfyClient, ComfyError
from .config import Settings
from .director import (
    DirectorClient,
    DirectorError,
    DirectorUnavailable,
    document_rejection,
)
from .models import (
    Asset,
    MessageNotice,
    Project,
    RenderJob,
    Shot,
    Song,
    TreatmentMessage,
    VisionInspectionRecord,
)
from .store import ProjectNotFound, ProjectStore
from .timeline import (
    H3_MAX_SHOT_SECONDS,
    H3_MIN_SHOT_SECONDS,
    TimelineError,
    build_director_timeline,
    expansion_input,
    ordered_shots,
)
from .workflows import (
    WorkflowCatalog,
    build_flux_payload,
    build_h3_director_payload,
    build_h3_reference_payload,
    build_multiview_payload,
    build_music3_payload,
    build_songplanner_invented_payload,
    build_songplanner_known_lyrics_payload,
)

# The one wording for what a Song change costs, shared by every route that changes or
# removes a project's Song. The Song is the timing spine: `Shot.start`/`Shot.duration`
# are absolute seconds against it, playback sync and Assembly derive from it, and
# `use_song_audio` shots reference its audio. Nothing here deletes shot data and nothing
# moves a shot to fit a new song, so the refusal has to say both — the Director needs to
# know what silently stops lining up, not to fear losing work.
#
# `api.js`'s SONG_CHANGE_CONSEQUENCE is the frontend half of this sentence; both name
# shot windows and Assembly synchronization, asserted by tests.
SONG_REPLACEMENT_CONSEQUENCE = (
    "This project already has shots that depend on the current song: shot windows are "
    "absolute seconds against it, and Assembly synchronization derives from it. "
    "Replacing or removing the song deletes no shot data and adjusts no shot window, so "
    "every existing shot keeps the timing it has now. "
    "Send confirm_song_replacement=true to proceed."
)


# The creative documents a Director reply can replace, keyed by field name. One mapping,
# and everything else about them is derived from it: the field names the guard loop reaches
# by interpolation, the slots kept out of the model's context, and the labels used on
# screen. Adding a third document must not require finding four other places, because the
# one that gets missed silently leaks a document's kept copy back into every prompt.
# `api.js`'s DOCUMENT_LABELS is the frontend half; tests assert both sides, the
# `DocumentName` literal, and `Project`'s actual fields all agree.
DOCUMENT_LABELS = {"treatment": "Treatment", "style_bible": "Style bible"}
DocumentName = Literal["treatment", "style_bible"]

# What the Director's project dump leaves out. The recovery slots are *derived* from the
# mapping rather than listed, so a document added to it cannot have its kept copy echoed
# into the prompt by omission. See `director_chat` for why that matters.
DIRECTOR_CONTEXT_EXCLUDE: dict[str, Any] = {
    "jobs": True,
    # `notices` goes out whole, and that is the invariant Story 2.2 established extended to this
    # route. A notice's `raw` field holds the degraded output a refusal is about, and this dump is
    # what the *next* Director call is handed — so leaving it in would make the guard that catches
    # "JSON in context begets JSON" the thing supplying it. The list is dropped rather than the
    # `raw` field inside it for two reasons: every notice's sentence is already in `content`, so
    # keeping the structured copy would echo a second copy of each one into the prompt; and a
    # nested path stops covering a field that is later renamed or added beside it, silently.
    "messages": {"__all__": {"id", "created_at", "notices"}},
    **{f"{field}_previous": True for field in DOCUMENT_LABELS},
}

# The one wording for *what changed*. `document_rejection` has always told the Director
# what was not applied; nothing told them what was, which is exactly how a plausible
# unrequested rewrite became permanent and invisible. This says which documents moved and
# that the previous version is recoverable, because a change nobody is told about cannot
# be reviewed.
DOCUMENT_CHANGE_NOTICE = (
    "Replaced by this reply: {documents}. The version each one had before this reply is "
    "kept and can be restored from the Treatment workspace, which discards nothing else."
)

# Filling a blank document is not a replacement, and must not be described as one. The
# guard deliberately accepts any first draft into an empty target, so the recovery slot it
# captures is empty too and a restore would refuse — promising recovery here would be a
# promise the restore route breaks.
DOCUMENT_FIRST_DRAFT_NOTICE = (
    "Written for the first time by this reply: {documents}. Each was empty beforehand, so "
    "there is no previous version to restore; one is kept the next time a reply replaces it."
)

# The one wording for a locked document. Emitted only when the candidate would genuinely
# have changed something, or a project with a locked Treatment would carry this paragraph
# on every reply forever — including replies where the model simply echoed the current text
# back. It also states the scope of the lock: the Director is stopped, the human is not.
DOCUMENT_LOCK_NOTICE = (
    "{document} is locked, so the replacement this reply proposed was not applied and no "
    "previous version was recorded. A lock only stops the Director: you can still edit the "
    "document yourself, restore a kept version, or unlock it in the Treatment workspace."
)

# What the chat composer's per-turn consent control is called, quoted by the notice below so
# the Director is told exactly what to tick. `api.js`'s APPLY_DOCUMENTS_LABEL and the label in
# `index.html` are the other two copies, and a contract test asserts all three agree: a notice
# naming a control that no longer exists is worse than no notice at all.
APPLY_DOCUMENTS_LABEL = "Apply document changes"

# The one wording for a document replacement the Director did not ask for. `apply_documents`
# is off by default, so an ordinary question — "what do you think of this idea?" — must not
# rewrite the Treatment; this says which documents the reply wanted to change instead.
#
# Emitted only when the candidate would genuinely have changed something and would genuinely
# have been applied, exactly as DOCUMENT_LOCK_NOTICE is: a reply that echoed the current text
# back proposed nothing, and a candidate the guard would have refused anyway would not have
# landed even with consent — telling the Director to tick the box and ask again would then be
# a false instruction.
#
# It also says the proposed text is not kept, because it is not: nothing new is persisted and
# there is no proposal slot, exactly as a declined shot list has none.
DOCUMENT_NOT_REQUESTED_NOTICE = (
    "Proposed but not applied: {documents}. Replacing a document is opt-in per turn, so "
    "nothing was written and no previous version was recorded. Tick "
    f'"{APPLY_DOCUMENTS_LABEL}" '
    "beside the composer and ask again to apply it; the text proposed here is not kept."
)

# The one wording for a restore, and for refusing one. `api.js`'s DOCUMENT_RESTORE_NOTICE
# and DOCUMENT_RESTORE_REFUSAL_MARKER are the frontend halves, so the toast the Director
# reads and the message stored in the thread cannot drift apart.
#
# A restore is a *swap*, not a pop: the text being replaced moves into the recovery slot,
# so a restore is normally its own inverse and a mis-click costs nothing. Saying so is the
# whole point — single-slot recovery the Director is afraid to use is not recovery.
DOCUMENT_RESTORE_NOTICE = (
    "{document} was restored to the version kept before the last applied replacement. "
    "No Director call was made. The text that was replaced is now the kept version, so "
    "restoring again swaps back."
)
# ...except when the text being displaced is empty. An empty slot has to refuse, so that
# restore is one-way, and claiming reversibility exactly where the recovered text matters
# most would be the one lie this feature cannot afford.
DOCUMENT_RESTORE_ONE_WAY_NOTICE = (
    "{document} was restored to the version kept before the last applied replacement. "
    "No Director call was made. The document it replaced was empty, so nothing recoverable "
    "was displaced and there is nothing to swap back to: this restore is one-way."
)
DOCUMENT_RESTORE_REFUSAL = (
    "No previous version of {document} was kept, so there is nothing to restore. A version "
    "is only kept when a Director reply actually replaces the document."
)


# The one separator between a reply's own prose and the notices attached to it, and the one way
# the notices are joined to each other. Both routes wrote these out for themselves, byte-identical
# and unshared. `api.js`'s NOTICE_SEPARATOR and NOTICE_JOIN are the frontend halves and a contract
# test asserts they are identical, because the browser strips exactly this tail rather than
# *searching* the message for `---`: the Director's own prose can contain that sequence, and so can
# the raw model output a notice is about, and a search would split one notice into two blocks with
# the model's degraded text presented as a protective refusal.
NOTICE_SEPARATOR = "\n\n---\n"
NOTICE_JOIN = "\n\n"

# What a reply says when the model returned no sentence of its own. `DirectorResult.message` has
# no `min_length` and deliberately keeps none — an empty sentence is not a reason to fail a turn
# that legitimately replaced a document — but without a fallback the stored reply begins with a
# bare separator, and that reply is context for the next call. The expansion route has had this
# guard since Story 2.2; the chat route never did.
CHAT_EMPTY_MESSAGE = "The Director returned no message with this reply."

# The refusal that keeps a document, and until now the only notice with no wording of its own: an
# inline f-string that pasted 400 characters of the model's own degraded output into `content` —
# into the thread `director_chat` hands straight back to the model as context on the next turn.
# The output now travels in the notice's `raw` field, which that dump excludes, so it can be read
# without being fed back.
DOCUMENT_REJECTED_NOTICE = (
    "{document} was NOT replaced: {reason}. The document you have is unchanged. The text the "
    "model returned is kept beside this notice for inspection and is left out of the next "
    "Director call's context."
)
# ...and the same refusal when there is nothing behind the disclosure. A blank or whitespace-only
# candidate is refused by the ratio floor like any other, and `MessageNotice` stores blank as
# blank, so the sentence above would offer an inspection of an empty box. That is the same class
# of false sentence this story rewrote `EXPANSION_REJECTED_NOTICE` to remove, and writing it here
# while removing it there would be no improvement at all.
DOCUMENT_REJECTED_EMPTY_NOTICE = (
    "{document} was NOT replaced: {reason}. The document you have is unchanged. The model "
    "returned no text for it, so there is nothing to inspect."
)

# Consent was given for a shot plan and none arrived. Says the timeline is untouched, because
# "no shot plan was applied" on its own reads like something might have been removed.
SHOT_PLAN_EMPTY_NOTICE = (
    "No shot plan was applied: the model returned an empty shot list. "
    "Existing shots are unchanged."
)

# FR-15's last clause, and the half of the recorded 2026-08-16 defect nothing ever reported: the
# reply's prose described a four-beat sequence while `shots` came back empty, so the Director was
# told work had been done that was never applied and no shot plan existed to review.
#
# Ungated on `apply_shots` deliberately. The browser hardcodes that flag to `false`, so gating this
# on it would put the notice exactly where no Director can reach it — and the mismatch is a fact
# about the reply contradicting itself, which is true whether or not shots would have been applied.
#
# Two wordings, chosen on what the project actually holds. "The existing shots are unchanged" is
# reassurance about work that exists; said to a project with no shots at all it is both false and
# the opposite of the point, which is that the plan the Director has just been told about does not
# exist anywhere.
SHOT_CLAIM_MISMATCH_NOTICE = (
    "The reply describes a shot plan but returned an empty shot list, so nothing was written to "
    "the timeline and the existing shots are unchanged. Ask again if you want the plan itself."
)
SHOT_CLAIM_WITHOUT_ANY_SHOTS_NOTICE = (
    "The reply describes a shot plan but returned an empty shot list, and this project still has "
    "no shots at all. Nothing was written to the timeline. Ask again if you want the plan itself."
)

# A Shot outside the renderable window. Reported, never corrected: the Shot is still applied and
# the Director decides whether to split it, trim it, or leave it.
SHOT_WINDOW_NOTICE = (
    "Proposed {duration:g}s shot at {start:g}s falls outside MiniMax H3's reliable "
    "{minimum:g}-{maximum:g}s window; split or trim it before rendering."
)

# Whether a reply's prose claims to have *produced* a shot plan. Every part of this is narrowing,
# because the first cut matched `\bshots?\b` anywhere and therefore fired on "I did not add any
# shots" and "the shots you have are fine" — a notice that appears on ordinary conversation about
# an existing timeline is one the Director learns to scroll past, which is the failure this whole
# story is about.
#
# Three rules, each earning its place:
#
# * **A claim of authorship, not a mention.** One of these verbs has to appear before the noun,
#   within one sentence, so describing shots is not the same as claiming to have written them.
# * **A denial anywhere in the sentence skips it.** "I have not written any shots" contains a
#   perfectly good claim as a substring, and the safe reading of a negated sentence is silence.
# * **Anything other than the word "shot" must be a counted structure *and* name a plan.** The
#   recorded defect said "Splitting your vision into a four-beat sequence" and never used the word
#   shot, so beats and cuts have to count — but "written the treatment in two parts" is ordinary
#   document vocabulary and must not, which is what the plan word after the count decides.
_CLAIM_VERB = (
    r"(?:add(?:s|ed|ing)?|writ(?:e|es|ten|ing)|wrote|split(?:s|ting)?|break(?:s|ing)?|broke|"
    r"broken|cut(?:s|ting)?|creat(?:e|es|ed|ing)|draft(?:s|ed|ing)?|plan(?:s|ned|ning)?|"
    r"build(?:s|ing)?|built|lay(?:s|ing)?|laid|map(?:s|ped|ping)?|storyboard(?:s|ed|ing)?|"
    r"here (?:are|is))"
)
_COUNT = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_STRUCTURE_NOUN = r"(?:beat|part|section|segment|scene|cut|chapter|block)"
_PLAN_NOUN = r"(?:sequence|plan|breakdown|storyboard|montage|edit|shot list|shots?)"
#: A sentence containing any of these is read as saying what did *not* happen, and skipped.
_NOTICE_DENIAL = re.compile(
    r"\b(?:no|not|never|nothing|none|without|cannot|unchanged|untouched|kept|left alone|"
    r"\w+n't)\b",
    re.IGNORECASE,
)
_SHOT_CLAIM_PATTERNS = (
    re.compile(rf"\b{_CLAIM_VERB}\b[^.!?]{{0,60}}?\bshots?\b", re.IGNORECASE),
    re.compile(
        rf"\b{_CLAIM_VERB}\b[^.!?]{{0,60}}?\b{_COUNT}[\s-]{_STRUCTURE_NOUN}s?\b"
        rf"[^.!?]{{0,40}}?\b{_PLAN_NOUN}\b",
        re.IGNORECASE,
    ),
)
#: Sentence boundaries, so a denial in one sentence cannot silence a claim in another and a claim
#: cannot be assembled out of two unrelated ones.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n+")


# Why an empty plan is refused instead of answered with an empty expansion. Expansion writes a
# prompt onto Shots that already exist — it never creates, retimes or removes one — so with no
# Shots there is nothing for a result to be keyed to, and a model call would spend the
# Director's time to report that nothing happened. Refused before any call, like every other
# guard in this module.
EXPANSION_WITHOUT_SHOTS = (
    "This project has no shots to expand. Expansion writes a prompt onto each existing shot "
    "and never creates, retimes, or removes one, so add shots to the timeline first."
)

# What one expansion did, in the sentences it can need. Every one names Shots the Director can
# go and look at, **by shot id**, because a prompt is free text: a result merged onto the wrong
# Shot reads as a plausible prompt forever and nothing downstream would fail. `api.js`'s
# SHOT_EXPANSION_TOAST is the frontend half of the summary; the detail lives here, in the one
# place that knows which Shots were involved.
EXPANSION_WRITTEN_NOTICE = "Prompts written for {count} shot(s): {shots}."
EXPANSION_LOCKED_NOTICE = (
    "Left unchanged because they are locked: {shots}. A lock only stops the Director — you can "
    "still write those prompts yourself in the shot inspector, or unlock the shot."
)
# A Shot whose prompt something already depends on. Rewriting it is provenance loss of the same
# class the document recovery slots exist to prevent: an approved take would silently stop
# matching the prompt it was produced from, and an in-flight render's prompt would diverge from
# what was actually submitted to ComfyUI. Reported like a lock, and for the same reason —
# "nothing happened to this Shot" has to say why.
EXPANSION_RENDERED_NOTICE = (
    "Left unchanged because a render or a take already depends on the prompt they have: "
    "{shots}. Rewriting one would leave its take claiming a prompt that never produced it. You "
    "can still edit those prompts yourself in the shot inspector."
)
# An unknown id is reported rather than guessed at and never created as a new Shot. Creating one
# would invent a window this expansion has no business choosing, and matching it positionally is
# the exact silent misassignment keying by id exists to prevent.
EXPANSION_UNKNOWN_NOTICE = (
    "Discarded: the model returned prompts addressed to {count} id(s) that no shot in this "
    "project has ({shots}). Nothing was written for them and no shot was created."
)
# An omission is reported rather than retried. A retry loop would spend GPU-free but real
# Director time on a model that already declined once, and the existing prompt is not lost.
EXPANSION_OMITTED_NOTICE = (
    "Omitted by the model and therefore unchanged: {shots}. Each kept the prompt it already "
    "had; run expansion again if you want them written."
)
# The model answering for one Shot twice. First answer wins, because two contradictory prompts
# for one Shot is a self-contradiction the Director has to see rather than a preference for
# whichever arrived last — and last-write-wins could report the same Shot as both refused and
# written in one reply.
EXPANSION_DUPLICATE_NOTICE = (
    "The model answered more than once for {shots}. The first prompt it gave for each was "
    "applied and the later ones were ignored, because a shot cannot have two prompts."
)
# The refusal, with the refused text carried out of band. Writing the raw output into the reply
# would put the degraded JSON in the chat thread, which `director_chat` ships back to the model as
# context on the next turn — so the guard that catches "JSON in context begets JSON" would be the
# thing feeding it. That is why the text was dropped entirely when this notice was written, and
# why it no longer has to be: it now travels in the notice's `raw` field, which
# `DIRECTOR_CONTEXT_EXCLUDE` keeps out of that dump, so the Director can read what was refused
# without the model ever seeing it again.
EXPANSION_REJECTED_NOTICE = (
    "NOT applied to {shot}: {reason}. The returned text is kept beside this notice for "
    "inspection and is left out of the context of the next Director call, because repeating "
    "degraded output into it is what produces more of it."
)
# ...and the same refusal when the model returned nothing to keep. `expansion_rejection` refuses a
# blank prompt in exactly those words, so promising an inspection of it would offer a disclosure
# onto an empty box — the false sentence the wording above was rewritten to stop making.
EXPANSION_REJECTED_EMPTY_NOTICE = "NOT applied to {shot}: {reason}. There is no returned text."
# What the reply says when the model returned no message of its own. `ShotExpansion.message`
# deliberately has no `min_length`: an empty sentence is not a reason to discard a whole set of
# good prompts with a 502, but it must not leave the reply as a bare separator either.
EXPANSION_EMPTY_MESSAGE = "The Director returned no summary of this expansion."


def document_change_notice(labels: list[str]) -> str:
    """State which documents this reply replaced, from the one wording above.

    This one has no JavaScript half and needs none: it is written into the chat thread the
    browser renders verbatim, so mirroring it client-side would be an unused second copy
    of a sentence — the drift this pattern exists to prevent.
    """
    return DOCUMENT_CHANGE_NOTICE.format(documents=", ".join(labels))


def document_first_draft_notice(labels: list[str]) -> str:
    """State which documents this reply filled from blank. See DOCUMENT_FIRST_DRAFT_NOTICE."""
    return DOCUMENT_FIRST_DRAFT_NOTICE.format(documents=", ".join(labels))


def document_not_requested_notice(labels: list[str]) -> str:
    """Name the documents a declined reply proposed. See DOCUMENT_NOT_REQUESTED_NOTICE."""
    return DOCUMENT_NOT_REQUESTED_NOTICE.format(documents=", ".join(labels))


def document_restore_notice(document: DocumentName, *, reversible: bool = True) -> str:
    """Confirm a restore, from the one wording above. Mirrored by `documentRestoreNotice`.

    `reversible` defaults to the ordinary case — a non-empty document displaced into the
    recovery slot, so restoring again swaps back — which is the sentence the frontend
    mirrors. The route passes it explicitly, because a restore over an empty document is
    one-way and must not claim otherwise.
    """
    template = DOCUMENT_RESTORE_NOTICE if reversible else DOCUMENT_RESTORE_ONE_WAY_NOTICE
    return template.format(document=DOCUMENT_LABELS[document])


def shot_render_provenance(shot: Shot) -> bool:
    """True when something already depends on this Shot's prompt being what it is.

    A submitted render, a take on disk, an editorial approval, or any status past `draft` all
    mean the prompt is no longer just an intention — it is the record of what produced, or is
    producing, a specific piece of media. Rewriting it in place is provenance loss: nothing
    fails, and afterwards the take and the prompt beside it simply disagree.

    Deliberately *not* sent to the model. The expansion input is trimmed of exactly these
    fields, and a derived flag would reintroduce the production state that trimming exists to
    keep out; the cost is that the model may spend a slot on a Shot whose prompt is then
    discarded, which the reply reports.
    """
    return bool(
        shot.prompt_id or shot.latest_output or shot.approved_output or shot.status != "draft"
    )


def prose_claims_shots(message: str) -> bool:
    """True when a reply's prose claims to have produced a shot plan. See `_SHOT_CLAIM_PATTERNS`.

    Sentence by sentence, because both narrowing rules are about sentences: a denial silences the
    one it is in and no other, and a claim may not be assembled out of a verb in one sentence and
    a noun in the next.

    Read only when the structured list came back empty, so a reply that *did* return shots is
    never inspected for what it says about them.
    """
    for sentence in _SENTENCE_BREAK.split(message or ""):
        if _NOTICE_DENIAL.search(sentence):
            continue
        if any(pattern.search(sentence) for pattern in _SHOT_CLAIM_PATTERNS):
            return True
    return False


def shot_claim_mismatch_notice(existing_shots: int) -> MessageNotice:
    """Report a reply that describes a plan it did not return, in the words that are true.

    A project that already has Shots is a different situation from one that has none: the first
    can be reassured that its timeline is untouched, and telling the second the same thing would
    describe shots it does not have, in the one reply that has just led the Director to believe
    they exist.
    """
    wording = (
        SHOT_CLAIM_MISMATCH_NOTICE if existing_shots else SHOT_CLAIM_WITHOUT_ANY_SHOTS_NOTICE
    )
    return MessageNotice(kind="flag", text=wording)


def rejection_notice(kept: str, empty: str, *, raw: str, **fields: object) -> MessageNotice:
    """A refusal, worded for whether there is genuinely anything behind its disclosure.

    `MessageNotice` stores a blank or whitespace-only `raw` as `""`, so this reads the stored
    value rather than the candidate: the sentence offered to the Director and the disclosure the
    client renders are then decided by the same fact.
    """
    notice = MessageNotice(kind="refusal", text=kept.format(**fields), raw=raw)
    if notice.raw:
        return notice
    return MessageNotice(kind="refusal", text=empty.format(**fields))


def assistant_reply(prose: str, notices: list[MessageNotice]) -> TreatmentMessage:
    """The one way either Director route turns prose plus notices into a stored reply.

    `content` keeps carrying the joined text: it is what every saved project already holds and
    what `api.js`'s marker-scanning helpers read, so changing it would break both for no gain.
    The notices ride alongside it as data, which is what lets the renderer split the two apart
    without searching the message for a separator that its own text may contain.
    """
    content = prose
    if notices:
        content = prose + NOTICE_SEPARATOR + NOTICE_JOIN.join(notice.text for notice in notices)
    return TreatmentMessage(role="assistant", content=content, notices=notices)


def _short(value: str, limit: int = 60) -> str:
    """Collapse and cap model-controlled text before it is stored in the chat thread.

    A `shot_id` that matched nothing is whatever the model emitted — it can be a paragraph, or
    carry newlines that break the notice apart. The thread is persisted and is context for the
    next call, so nothing model-controlled goes into it at unbounded length.
    """
    collapsed = " ".join(str(value).split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[:limit]}…"


def expansion_shot_label(index: int, shot: Shot) -> str:
    """Name a Shot by the one number the model was given: its `index` in `expansion_input`.

    The two orderings differ. `expansion_input` orders by `start`, because that is the Shot's
    position in the song; the timeline draws clips in manifest order. Numbering notices by the
    manifest would describe a different Shot than the `index` the model answered about, for
    every plan whose manifest order is not its time order — so this uses the input's index, and
    carries the start time and the id, which are unambiguous under either ordering.
    """
    return f"shot index {index} at {shot.start:g}s ({shot.id})"


def expansion_rejection(prompt: str) -> str:
    """Why a returned prompt must not be written onto a Shot, or "" when it may.

    Only the JSON-as-prose half of `document_rejection` is meaningful for a prompt, so "" is
    passed as the existing text to reach exactly that check and nothing else. The 40% ratio
    floor compares against the *current* prompt, which is `""` on an unexpanded Shot and the
    "New shot" placeholder on one added in the UI — toothless where it matters, and liable to
    refuse a legitimate first prompt on a Shot the Director had already written by hand.

    A blank prompt is refused separately. `ExpandedShot` already requires a non-empty string on
    the wire, but this route must not be the thing that blanks a Shot's prompt if anything ever
    reaches it with one.
    """
    if not prompt.strip():
        return "the model returned an empty prompt"
    return document_rejection(prompt, "")


def _require_song_replacement_confirmation(project: Project, confirmed: bool) -> None:
    """Refuse an unacknowledged Song change once the project has shots.

    A first import, and any project with no shots, stays frictionless: there is nothing
    whose timing the change can invalidate. Callers must invoke this *before* doing any
    work — writing the uploaded file or submitting to ComfyUI — or the refusal comes too
    late to be a refusal.
    """
    if confirmed or project.song is None or not project.shots:
        return
    raise HTTPException(status_code=409, detail=SONG_REPLACEMENT_CONSEQUENCE)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class MusicRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    caption: str = Field(min_length=1)
    lyrics: str = ""
    duration: float = Field(default=120, ge=4, le=360)
    # MiniMaxMusic3TextEncode.seed and KSampler.seed are 64-bit; no planner is
    # involved, so this is genuinely wider than the SongPlanner route's 32-bit
    # ceiling. Unbounded is still wrong: ComfyUI refuses anything past 64-bit
    # at /prompt validation, which reaches the Director as an opaque 502.
    seed: int = Field(default=0, ge=0, le=0xFFFFFFFFFFFFFFFF)
    # Acknowledgement of SONG_REPLACEMENT_CONSEQUENCE, not stored state: both generate
    # routes assign `project.song` at submit time, so the replacement happens here.
    confirm_song_replacement: bool = False


class SongPlannerRequest(BaseModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
    idea: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    genre_hint: str = Field(default="", max_length=160)
    lyrics: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
        | None
    ) = None
    # M3SongPlanner.duration_seconds accepts 30–300 s and MiniMaxMusic3TextEncode
    # .max_duration 0.04–360 s; the intersection is the route's bound. Taken from
    # the recorded /object_info schema, not from the reference export's literals —
    # anything outside it is rejected by ComfyUI before a node runs.
    duration: float = Field(default=120, ge=30, le=300)
    # M3SongPlanner.seed is 32-bit (max 4294967295) even though the encoder and
    # KSampler seeds it shares a payload with are 64-bit, so the planner governs
    # here too. Direct Music 3 never touches the planner and keeps its own range.
    seed: int = Field(default=0, ge=0, le=0xFFFFFFFF)
    # See MusicRequest.confirm_song_replacement.
    confirm_song_replacement: bool = False


class FluxRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    kind: Literal["character", "setting", "prop", "style", "image"] = "image"
    prompt: str = Field(min_length=1)
    width: int = Field(default=1024, ge=256, le=2048, multiple_of=16)
    height: int = Field(default=1024, ge=256, le=2048, multiple_of=16)
    steps: int = Field(default=20, ge=1, le=100)
    guidance: float = Field(default=4, ge=0, le=20)
    # RandomNoise.noise_seed is 64-bit; see MusicRequest.seed on why unbounded is wrong.
    seed: int = Field(default=0, ge=0, le=0xFFFFFFFFFFFFFFFF)


class MultiviewRequest(BaseModel):
    prompt: str = Field(min_length=1)
    # KSampler.seed is 64-bit; see MusicRequest.seed on why unbounded is wrong.
    seed: int = Field(default=0, ge=0, le=0xFFFFFFFFFFFFFFFF)


class TimelineRequest(BaseModel):
    window_start: float = Field(default=0, ge=0)
    window_duration: float = Field(gt=0)
    fps: int = Field(default=24, ge=1, le=120)


class TimelineCompileResponse(BaseModel):
    """What the dry run returns, declared rather than assembled as a bare dict.

    `readiness` is on here because a field with no schema is a field no client can discover and
    no test can pin: the compile route had no `response_model` at all, so the readiness block it
    reports would have been invisible in `/openapi.json` and indistinguishable from an accident.
    It is reported and never enforced — see `compile_timeline`.
    """

    timeline_data: str
    requested_frames: int
    aligned_frames: int
    warnings: list[str]
    readiness: ReadinessReport


class H3Request(BaseModel):
    width: int = Field(default=1344, ge=256, le=2048, multiple_of=32)
    height: int = Field(default=768, ge=256, le=2048, multiple_of=32)
    steps: int = Field(default=20, ge=1, le=100)
    ref_image_size: Literal["match", "max"] = "match"


# A flag a client omits and a flag a client sends as `null` mean the same thing — "I am not
# asking for this" — but Pydantic reads the second as a type error and 422s the whole turn, so
# the Director's message is lost over a field whose *absence* is already the safe default. Both
# consent flags read `null` as the decline instead. Nothing here loosens anything: the only
# value whose meaning changes is one that was rejected outright, and it lands on `False`.
DeclinedIfNull = Annotated[bool, BeforeValidator(lambda value: False if value is None else value)]


class DirectorRequest(BaseModel):
    message: str = Field(min_length=1)
    apply_shots: DeclinedIfNull = False
    # Per-turn consent to replace the creative documents, mirroring `apply_shots` exactly —
    # same shape, same default, and independent of it. Off by default because consent has to be
    # explicit for the turn being sent: asking "what do you think of this idea?" must not
    # rewrite the Treatment, which is what every reply did before this field existed. It is
    # deliberately not stored on `Project`, so it is neither remembered across turns nor
    # inherited by another project, and a client that omits or nulls it is a decline.
    apply_documents: DeclinedIfNull = False


class ShotListRequest(BaseModel):
    shots: list[Shot]


class ProjectDocumentsRequest(BaseModel):
    creative_brief: str = ""
    treatment: str = ""
    style_bible: str = ""
    # Locks are tri-state on the wire: `None` means "leave the stored lock as it is". Every
    # other field here defaults to "", which is why an omitted one blanks its document —
    # a lock defaulting to False the same way would silently unlock both documents on every
    # ordinary save, and the save path would quietly defeat the feature.
    #
    # The recovery slots are deliberately absent from this model. Only an applied Director
    # replacement writes them; a save cannot forge, clear, or advance a kept version, and
    # because the route mutates the *stored* project they survive untouched.
    treatment_locked: bool | None = None
    style_bible_locked: bool | None = None


def _safe_filename(value: str) -> str:
    stem = Path(value).name
    clean = re.sub(r"[^A-Za-z0-9._ -]+", "_", stem).strip(" .")
    return clean or "media"


def _copy_upload(file: UploadFile, target: Path, max_bytes: int) -> None:
    written = 0
    try:
        with target.open("wb") as output:
            while chunk := file.file.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(status_code=413, detail="Upload exceeds configured size limit")
                output.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise


def _media_duration(path: Path) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=15,
        )
        return max(0.0, float(result.stdout.strip()))
    except (FileNotFoundError, subprocess.SubprocessError, ValueError):
        return 0.0


# The longest imported song length that is a measurement rather than a mistake. Twenty-four
# hours is far past any real master and still finite, which is the point: the ceiling exists
# to reject nonsense, not to legislate song length.
MAX_IMPORTED_SONG_SECONDS = 86_400.0


def _browser_reported_duration(duration: float) -> float:
    """The browser's measurement, or exactly 0 when it is not a usable number.

    `upload_song` only reaches for ffprobe when this is 0, so every "unknown length" shape
    has to arrive as exactly that. `float` accepts `inf` and `nan` from a form post, and
    `Song.duration` only constrains `ge=0`, so without this an `inf` or `1e18` would be
    persisted untouched as the timing spine every Shot window, playback sync and Assembly
    derives from — and a wrong spine is worse than a missing one.
    """
    if not math.isfinite(duration) or duration <= 0 or duration > MAX_IMPORTED_SONG_SECONDS:
        return 0.0
    return duration


def _vision_media(path: Path) -> tuple[bytes, str]:
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        mime = {".png": "image/png", ".webp": "image/webp"}.get(suffix, "image/jpeg")
        return path.read_bytes(), mime
    if suffix not in {".mp4", ".mov", ".webm", ".mkv"}:
        raise ValueError("Vision inspection supports images and videos")
    duration = max(_media_duration(path), 1.0)
    rate = 4.0 / duration
    with tempfile.TemporaryDirectory(prefix="mvp-vision-") as directory:
        contact = Path(directory) / "contact.jpg"
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error", "-i", str(path),
                    "-vf", f"fps={rate},scale=512:-1,tile=2x2:padding=8:margin=8",
                    "-frames:v", "1", str(contact),
                ],
                capture_output=True,
                check=True,
                timeout=60,
            )
        except (FileNotFoundError, subprocess.SubprocessError) as error:
            raise ValueError(f"Could not extract video review frames: {error}") from error
        return contact.read_bytes(), "image/jpeg"


def create_app(
    *,
    settings: Settings | None = None,
    store: ProjectStore | None = None,
    comfy: ComfyClient | Any | None = None,
    director: DirectorClient | Any | None = None,
) -> FastAPI:
    settings = settings or Settings()
    store = store or ProjectStore(settings.data_root)
    comfy = comfy or ComfyClient(settings.comfy_url, timeout=settings.request_timeout)
    director = director or DirectorClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
    )
    catalog = WorkflowCatalog(settings.workflow_root)

    app = FastAPI(
        title="Music Video Producer",
        version="0.1.0",
        description="Standalone local-first music and music-video production studio.",
    )
    app.state.settings = settings
    app.state.store = store
    app.state.comfy = comfy

    def get_project(project_id: str) -> Project:
        try:
            return store.get(project_id)
        except ProjectNotFound as error:
            raise HTTPException(status_code=404, detail="Project not found") from error

    def resolve_asset_path(project_id: str, asset: Asset) -> Path:
        root = (
            store.media_dir(project_id).resolve()
            if asset.source == "upload"
            else (settings.comfy_root / "output").resolve()
        )
        target = (
            (store.project_dir(project_id) / asset.path).resolve()
            if asset.source == "upload"
            else (root / Path(asset.path)).resolve()
        )
        if root not in target.parents or not target.is_file():
            raise HTTPException(status_code=404, detail=f"Asset media was not found: {asset.name}")
        return target

    def resolve_song_path(project_id: str, song: Song) -> Path:
        root = (
            store.media_dir(project_id).resolve()
            if song.source == "imported"
            else (settings.comfy_root / "output").resolve()
        )
        target = (
            (store.project_dir(project_id) / song.path).resolve()
            if song.source == "imported"
            else (root / Path(song.path)).resolve()
        )
        if root not in target.parents or not target.is_file():
            raise HTTPException(status_code=404, detail="Song media was not found")
        return target

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "app": "Music Video Producer",
            "version": "0.1.0",
            "comfy": await comfy.health(),
            "llm": {
                "configured": bool(settings.llm_base_url and settings.llm_model),
                "model": settings.llm_model,
            },
        }

    @app.get("/api/projects", response_model=list[Project])
    def list_projects() -> list[Project]:
        return store.list()

    @app.post("/api/projects", response_model=Project, status_code=status.HTTP_201_CREATED)
    def create_project(request: ProjectCreate) -> Project:
        return store.create(Project(name=request.name.strip()))

    @app.get("/api/projects/{project_id}", response_model=Project)
    def read_project(project_id: str) -> Project:
        return get_project(project_id)

    @app.put("/api/projects/{project_id}", response_model=Project)
    def replace_project(
        project_id: str, project: Project, confirm_song_replacement: bool = False
    ) -> Project:
        current = get_project(project_id)
        if project.id != project_id:
            raise HTTPException(status_code=422, detail="Project ID cannot be changed")
        if project.updated_at != current.updated_at:
            raise HTTPException(
                status_code=409,
                detail="Project changed since it was loaded; refresh before replacing it",
            )
        # This is the normal save path for every edit in the UI, so it cannot be gated on
        # carrying a Song — that would refuse ordinary saves. It is gated on *changing* one:
        # a body whose Song differs from the stored Song is a replacement or a removal
        # however it arrived, and without this the guard was one HTTP call wide of true.
        # `Song` has no timestamps, so an untouched Song round-trips equal and passes here;
        # both being None is equal too, and adding a first Song to a Song-less project is
        # not a replacement.
        if project.song != current.song:
            _require_song_replacement_confirmation(current, confirm_song_replacement)
        # The recovery slots and the document locks are server-owned, and this route binds a
        # whole client-supplied `Project` whose every field is defaulted. A body that simply
        # omits them — which is what any client written before they existed sends — arrives
        # as ""/False, so trusting it lets one ordinary save clear both kept versions and
        # unlock both documents: exactly what AD-14 and the lock exist to prevent. Worse, a
        # body that *invents* a slot would be planting text that the restore route then swaps
        # into the live document as "the version you had before". Only an applied Director
        # replacement writes a slot, and only `PUT /documents` sets a lock.
        for field in DOCUMENT_LABELS:
            for owned in (f"{field}_previous", f"{field}_locked"):
                setattr(project, owned, getattr(current, owned))
        # The thread is server-owned for the same reason and by the same argument. Nothing in
        # this application posts a message: the chat route, the expansion route and the restore
        # route are the only writers, and each appends exactly what it did. A client body is
        # therefore never the authority on it — and since a message now carries structured
        # notices, trusting one would let an ordinary save invent a refusal that never happened,
        # reword the reason a real one gave, or simply omit the field and revert every notice in
        # the project to undifferentiated prose. The recovery slots were the first case of this;
        # a body that merely *omits* what it does not know about is the shape of all of them.
        project.messages = current.messages
        return store.save(project)

    @app.put("/api/projects/{project_id}/shots", response_model=Project)
    def replace_shots(project_id: str, request: ShotListRequest) -> Project:
        project = get_project(project_id)
        project.shots = request.shots
        return store.save(project)

    @app.put("/api/projects/{project_id}/documents", response_model=Project)
    def replace_documents(project_id: str, request: ProjectDocumentsRequest) -> Project:
        project = get_project(project_id)
        project.creative_brief = request.creative_brief
        project.treatment = request.treatment
        project.style_bible = request.style_bible
        # A lock stops the *Director* from replacing a document; it does not stop the human
        # who set it from typing in the textarea, so the text above is assigned either way.
        # Refusing an edit here would leave the Director unable to fix a locked document
        # without unlocking, saving, editing, and locking again.
        if request.treatment_locked is not None:
            project.treatment_locked = request.treatment_locked
        if request.style_bible_locked is not None:
            project.style_bible_locked = request.style_bible_locked
        return store.save(project)

    @app.post("/api/projects/{project_id}/documents/{document}/restore", response_model=Project)
    def restore_document(project_id: str, document: DocumentName) -> Project:
        """Swap a document with its single kept previous version. No Director call.

        Recovery has to be reachable without the model: the failure it exists for is the
        Director returning something unwanted, and asking that same Director to undo it
        risks a second unwanted rewrite. This route reads and writes stored text only.

        The swap is normally symmetric, so the operation is its own inverse and a mis-click
        is recoverable — but not when the document being displaced is empty, because an
        empty slot has to refuse. That case is real and is the one where the recovered text
        matters most, so it is reported as one-way rather than promised reversible.

        A locked document may still be restored: a lock stops the Director, not the human
        who set it, exactly as `PUT /documents` still accepts hand edits to a locked
        document. `DOCUMENT_LOCK_NOTICE` states that scope, and a route test pins it.

        An empty slot refuses with 409 rather than silently blanking the live document with
        "" — the exact data loss AD-14 exists to stop.
        """
        project = get_project(project_id)
        previous = getattr(project, f"{document}_previous")
        if not previous.strip():
            raise HTTPException(
                status_code=409,
                detail=DOCUMENT_RESTORE_REFUSAL.format(document=DOCUMENT_LABELS[document]),
            )
        displaced = getattr(project, document)
        setattr(project, f"{document}_previous", displaced)
        setattr(project, document, previous)
        # Recorded in the thread, not only toasted: the chat is the audit trail of what
        # happened to these documents, and a restore is as much a change as a replacement.
        project.messages.append(
            TreatmentMessage(
                role="system",
                content=document_restore_notice(document, reversible=bool(displaced.strip())),
            )
        )
        return store.save(project)

    @app.post("/api/projects/{project_id}/songs/upload", response_model=Project)
    async def upload_song(
        project_id: str,
        file: Annotated[UploadFile, File()],
        title: Annotated[str, Form()],
        duration: Annotated[float, Form()] = 0,
        confirm_song_replacement: Annotated[bool, Form()] = False,
    ) -> Project:
        project = get_project(project_id)
        # Before `_copy_upload`: a refusal must not have written anything, or it is not a
        # refusal. (The write itself no longer overwrites — see the index prefix below.)
        _require_song_replacement_confirmation(project, confirm_song_replacement)
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in {".wav", ".mp3", ".flac"}:
            raise HTTPException(status_code=415, detail="Song must be WAV, MP3, or FLAC")
        songs_dir = store.media_dir(project_id) / "songs"
        songs_dir.mkdir(parents=True, exist_ok=True)
        filename = _safe_filename(file.filename or f"song{suffix}")
        # Songs used to be written under their own name, so a confirmed replacement whose
        # filename matched the previous song destroyed the very audio that makes "re-import
        # the same file" an undo — the promise `remove_song` documents. Assets avoid this
        # with an index prefix; songs now do too. The index advances past whatever name is
        # already taken rather than being derived from a count, so a file deleted by hand
        # cannot make a later import land on a name that still exists.
        index = 0
        target = songs_dir / f"{index:03d}-{filename}"
        while target.exists():
            index += 1
            target = songs_dir / f"{index:03d}-{filename}"
        _copy_upload(file, target, settings.max_upload_bytes)
        reported = _browser_reported_duration(duration)
        resolved_duration = reported if reported > 0 else _media_duration(target)
        project.song = Song(
            title=title.strip() or target.stem,
            source="imported",
            path=target.relative_to(store.project_dir(project_id)).as_posix(),
            duration=resolved_duration,
        )
        return store.save(project)

    @app.delete("/api/projects/{project_id}/song", response_model=Project)
    def remove_song(project_id: str, confirm_song_replacement: bool = False) -> Project:
        """Detach the project's Song. Removal is not destruction.

        Shots are left exactly as they are — a shot whose window no longer has a song
        behind it is still the Director's work — and no media is deleted. What "undo" means
        differs by source, so state it exactly rather than over-promising: an imported song's
        file stays under `media/songs/` and re-importing it restores the Song, while a
        generated song's audio lives in ComfyUI's output and stays listed on its render job's
        `output_files`, which is the only record tying that take to this project once the
        Song reference is gone.
        """
        project = get_project(project_id)
        if project.song is None:
            raise HTTPException(status_code=404, detail="This project has no song to remove")
        _require_song_replacement_confirmation(project, confirm_song_replacement)
        project.song = None
        return store.save(project)

    @app.post("/api/projects/{project_id}/assets/upload", response_model=Project)
    async def upload_asset(
        project_id: str,
        file: Annotated[UploadFile, File()],
        name: Annotated[str, Form()],
        kind: Annotated[Literal["character", "setting", "prop", "style", "image", "audio", "video"], Form()] = "image",
    ) -> Project:
        project = get_project(project_id)
        suffix = Path(file.filename or "").suffix.lower()
        allowed_extensions = {
            "character": {".png", ".jpg", ".jpeg", ".webp"},
            "setting": {".png", ".jpg", ".jpeg", ".webp"},
            "prop": {".png", ".jpg", ".jpeg", ".webp"},
            "style": {".png", ".jpg", ".jpeg", ".webp"},
            "image": {".png", ".jpg", ".jpeg", ".webp"},
            "audio": {".wav", ".mp3", ".flac"},
            "video": {".mp4", ".mov", ".webm", ".mkv"},
        }
        if suffix not in allowed_extensions[kind]:
            raise HTTPException(status_code=415, detail=f"Unsupported {kind} asset file type")
        assets_dir = store.media_dir(project_id) / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        filename = _safe_filename(file.filename or "asset")
        target = assets_dir / f"{len(project.assets):03d}-{filename}"
        _copy_upload(file, target, settings.max_upload_bytes)
        project.assets.append(
            Asset(
                name=name.strip() or target.stem,
                kind=kind,
                path=target.relative_to(store.project_dir(project_id)).as_posix(),
            )
        )
        return store.save(project)

    @app.get("/api/projects/{project_id}/media/{media_path:path}")
    def read_project_media(project_id: str, media_path: str) -> FileResponse:
        get_project(project_id)
        media_root = store.media_dir(project_id).resolve()
        target = (media_root / media_path).resolve()
        if media_root not in target.parents or not target.is_file():
            raise HTTPException(status_code=404, detail="Media not found")
        return FileResponse(target)

    @app.get("/api/workflows")
    def workflows() -> list[dict[str, Any]]:
        return [
            {
                "id": entry.id,
                "name": entry.name,
                "category": entry.category,
                "relative_path": entry.relative_path,
                "description": entry.description,
                "available": entry.available,
            }
            for entry in catalog.list()
        ]

    @app.post(
        "/api/projects/{project_id}/generate/music",
        response_model=RenderJob,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def generate_music(project_id: str, request: MusicRequest) -> RenderJob:
        project = get_project(project_id)
        # Before submission: the refusal must cost no GPU time.
        _require_song_replacement_confirmation(project, request.confirm_song_replacement)
        prefix = f"music-video-producer/{project_id}/songs/{_safe_filename(request.title)}"
        payload = build_music3_payload(
            caption=request.caption,
            lyrics=request.lyrics,
            duration=request.duration,
            seed=request.seed,
            prefix=prefix,
        )
        try:
            submission = await comfy.submit(payload)
        except ComfyError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        project.song = Song(
            title=request.title,
            source="generated",
            duration=request.duration,
            lyrics=request.lyrics,
            caption=request.caption,
            prompt_id=submission.prompt_id,
        )
        job = RenderJob(
            kind="music",
            prompt_id=submission.prompt_id,
            target_id="song",
            seed=request.seed,
        )
        project.jobs.append(job)
        store.save(project)
        return job

    @app.post(
        "/api/projects/{project_id}/generate/songplanner",
        response_model=RenderJob,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def generate_songplanner(project_id: str, request: SongPlannerRequest) -> RenderJob:
        project = get_project(project_id)
        # Before submission: the refusal must cost no GPU time.
        _require_song_replacement_confirmation(project, request.confirm_song_replacement)
        prefix = f"music-video-producer/{project_id}/songs/{_safe_filename(request.title)}"
        if request.lyrics is not None:
            payload = build_songplanner_known_lyrics_payload(
                idea=request.idea,
                genre_hint=request.genre_hint,
                lyrics=request.lyrics,
                duration=request.duration,
                seed=request.seed,
                prefix=prefix,
            )
        else:
            payload = build_songplanner_invented_payload(
                idea=request.idea,
                genre_hint=request.genre_hint,
                duration=request.duration,
                seed=request.seed,
                prefix=prefix,
            )
        try:
            submission = await comfy.submit(payload)
        except ComfyError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        project.song = Song(
            title=request.title,
            source="generated",
            duration=request.duration,
            lyrics=request.lyrics or "",
            caption=request.idea,
            prompt_id=submission.prompt_id,
        )
        job = RenderJob(
            kind="music",
            prompt_id=submission.prompt_id,
            target_id="song",
            seed=request.seed,
        )
        project.jobs.append(job)
        store.save(project)
        return job

    @app.post(
        "/api/projects/{project_id}/generate/flux",
        response_model=RenderJob,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def generate_flux(project_id: str, request: FluxRequest) -> RenderJob:
        project = get_project(project_id)
        asset = Asset(
            name=request.name,
            kind=request.kind,
            path="",
            source="flux-image-gen",
            prompt=request.prompt,
        )
        prefix = f"music-video-producer/{project_id}/assets/{asset.id}"
        payload = build_flux_payload(
            prompt=request.prompt,
            width=request.width,
            height=request.height,
            steps=request.steps,
            guidance=request.guidance,
            seed=request.seed,
            prefix=prefix,
        )
        try:
            submission = await comfy.submit(payload)
        except ComfyError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        asset.prompt_id = submission.prompt_id
        project.assets.append(asset)
        job = RenderJob(
            kind="flux",
            prompt_id=submission.prompt_id,
            target_id=asset.id,
            seed=request.seed,
        )
        project.jobs.append(job)
        store.save(project)
        return job

    @app.post(
        "/api/projects/{project_id}/assets/{asset_id}/multiview",
        response_model=RenderJob,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def generate_multiview(
        project_id: str, asset_id: str, request: MultiviewRequest
    ) -> RenderJob:
        project = get_project(project_id)
        source = next((item for item in project.assets if item.id == asset_id), None)
        if not source:
            raise HTTPException(status_code=404, detail="Asset not found")
        if source.kind != "character" or not source.path:
            raise HTTPException(
                status_code=422,
                detail="A completed character image is required for multiview generation",
            )
        source_root = (
            store.media_dir(project_id).resolve()
            if source.source == "upload"
            else (settings.comfy_root / "output").resolve()
        )
        source_path = (
            (store.project_dir(project_id) / source.path).resolve()
            if source.source == "upload"
            else (source_root / Path(source.path)).resolve()
        )
        if source_root not in source_path.parents or not source_path.is_file():
            raise HTTPException(status_code=404, detail="Character source image was not found")
        upload_name = f"mvp_{project_id}_{source.id}{source_path.suffix.lower()}"
        content_type = "image/png" if source_path.suffix.lower() == ".png" else "image/jpeg"
        try:
            uploaded = await comfy.upload(upload_name, source_path.read_bytes(), content_type)
            image_name = "/".join(
                part for part in (uploaded.get("subfolder", ""), uploaded["name"]) if part
            )
            child = Asset(
                name=f"{source.name} · multiview",
                kind="character",
                path="",
                source="krea-multiview",
                parent_id=source.id,
                prompt=request.prompt,
            )
            payload = build_multiview_payload(
                image_name=image_name,
                prompt=request.prompt,
                seed=request.seed,
                prefix=f"music-video-producer/{project_id}/assets/{child.id}-multiview",
            )
            submission = await comfy.submit(payload)
        except ComfyError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        child.prompt_id = submission.prompt_id
        project.assets.append(child)
        job = RenderJob(
            kind="multiview",
            prompt_id=submission.prompt_id,
            target_id=child.id,
            seed=request.seed,
        )
        project.jobs.append(job)
        store.save(project)
        return job

    @app.post("/api/projects/{project_id}/assets/{asset_id}/analyze", response_model=Project)
    async def analyze_asset(project_id: str, asset_id: str) -> Project:
        project = get_project(project_id)
        asset = next((item for item in project.assets if item.id == asset_id), None)
        if not asset:
            raise HTTPException(status_code=404, detail="Asset not found")
        if asset.kind not in {"character", "setting", "prop", "style", "image", "video"}:
            raise HTTPException(status_code=422, detail="Vision inspection requires image or video media")
        source_path = resolve_asset_path(project_id, asset)
        try:
            image, mime_type = _vision_media(source_path)
            result = await director.inspect_image(
                image=image,
                mime_type=mime_type,
                purpose=f"{asset.kind} reference named {asset.name}",
            )
        except DirectorUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except (DirectorError, ValueError) as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        asset.vision = VisionInspectionRecord(model=settings.llm_model, **result.model_dump())
        return store.save(project)

    @app.post(
        "/api/projects/{project_id}/shots/{shot_id}/analyze-latest",
        response_model=Project,
    )
    async def analyze_latest_take(project_id: str, shot_id: str) -> Project:
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        output_root = (settings.comfy_root / "output").resolve()
        output = (output_root / Path(shot.latest_output)).resolve()
        if output_root not in output.parents or not output.is_file():
            raise HTTPException(status_code=404, detail="Latest take was not found")
        try:
            image, mime_type = _vision_media(output)
            result = await director.inspect_image(
                image=image,
                mime_type=mime_type,
                purpose=f"generated take for shot {shot.id}; check continuity and reference fidelity",
            )
        except DirectorUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except (DirectorError, ValueError) as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        shot.latest_review = VisionInspectionRecord(model=settings.llm_model, **result.model_dump())
        return store.save(project)

    @app.post(
        "/api/projects/{project_id}/timeline/compile", response_model=TimelineCompileResponse
    )
    def compile_timeline(project_id: str, request: TimelineRequest) -> TimelineCompileResponse:
        project = get_project(project_id)
        try:
            result = build_director_timeline(
                project.shots,
                window_start=request.window_start,
                window_duration=request.window_duration,
                fps=request.fps,
            )
        except TimelineError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return TimelineCompileResponse(
            timeline_data=result.timeline_data,
            requested_frames=result.requested_frames,
            aligned_frames=result.aligned_frames,
            warnings=result.warnings,
            # Reported, never enforced. This is the dry run: it queues nothing and costs no GPU
            # time, so refusing it would block the one cheap way to look at what a plan would
            # serialise — and it serialises `"prompt": ""` silently today, which is exactly the
            # thing worth seeing before the expensive call. The gate lives on the paths that
            # actually submit. The full report, warnings included: this is the one readiness
            # caller that is not on a hot path and the one whose whole job is to be looked at.
            readiness=readiness_report(project),
        )

    @app.get("/api/projects/{project_id}/readiness", response_model=ReadinessReport)
    def read_readiness(project_id: str) -> ReadinessReport:
        """The plan's readiness, derived on demand. A thin delegator over `batch`.

        A GET with no body and no side effects, because readiness is not state: it is recomputed
        from the prompts every time it is asked for, so a client that caches it is the only thing
        that can hold a stale answer.
        """
        return readiness_report(get_project(project_id))

    @app.post(
        "/api/projects/{project_id}/shots/{shot_id}/generate/h3",
        response_model=RenderJob,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def generate_h3(
        project_id: str, shot_id: str, request: H3Request
    ) -> RenderJob:
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        # Before the status check, before either payload branch, and before anything reaches
        # ComfyUI. Three reasons for that position, and each one is load-bearing:
        #
        # Before the *payload*, because the reference branch below interpolates the prompt into
        # `f"Reference map: {tags}. {shot.prompt}"` — an empty prompt arrives downstream as a
        # populated string, so every truthiness check past that line passes on exactly the Shots
        # this refuses. The guard has to read `shot.prompt` itself, here.
        #
        # Before the *status* check, because status is not a proxy for readiness. Nothing in the
        # shipped UI ever writes `ready`, so "must be ready" is reachable only from a hand-rolled
        # client; a draft Shot with no prompt would otherwise be told to change its status, which
        # is neither the real problem nor something the Director can act on.
        #
        # Via `readiness_report` rather than an inline `shot.prompt.strip()`, because AD-5 has one
        # implementation of "fit to submit" — the browser's pre-batch check and Epic 4's batch
        # submission ask the same function, and a second copy here is how a pre-flight starts
        # saying yes to something this route then refuses.
        #
        # `include_warnings=False` because sameness cannot change this answer and the batch loop
        # calls this route once per Shot: computing the pairwise pass here would run it N times
        # over the whole plan and discard the result every time.
        if shot.id in readiness_report(project, include_warnings=False).blocked_ids():
            raise HTTPException(
                # Named as the timeline names it. A raw `shot_a1b2c3d4e5f6` appears nowhere in
                # the interface, so a refusal carrying only that asks the Director to find a Shot
                # by a string they have never seen.
                status_code=422,
                detail=readiness_refusal([shot_label(project, shot)]),
            )
        if shot.status != "ready":
            raise HTTPException(status_code=422, detail="Shot must be ready before H3 submission")
        if shot.asset_ids or shot.use_song_audio:
            references: list[dict[str, Any]] = []
            tags: list[str] = []
            numbers = {"picture": 0, "video": 0, "audio": 0}
            for asset_id in shot.asset_ids:
                asset = next((item for item in project.assets if item.id == asset_id), None)
                if not asset:
                    raise HTTPException(status_code=422, detail=f"Unknown reference asset: {asset_id}")
                kind = (
                    "video"
                    if asset.kind == "video"
                    else "audio"
                    if asset.kind == "audio"
                    else "picture"
                )
                label = shot.reference_labels.get(asset.id, asset.name)
                references.append(
                    {"kind": kind, "file": str(resolve_asset_path(project_id, asset)), "label": label}
                )
                numbers[kind] += 1
                tag_name = {"picture": "Picture", "video": "Video", "audio": "Audio"}[kind]
                tags.append(f"<{tag_name} {numbers[kind]}> is {label}")
            if shot.use_song_audio:
                if not project.song or not project.song.path:
                    raise HTTPException(status_code=422, detail="A completed project song is required")
                references.append(
                    {
                        "kind": "audio",
                        "file": str(resolve_song_path(project_id, project.song)),
                        "label": "master song",
                    }
                )
                numbers["audio"] += 1
                tags.append(f"<Audio {numbers['audio']}> is the master song for synchronization")
            try:
                payload = build_h3_reference_payload(
                    prompt=f"Reference map: {'; '.join(tags)}. {shot.prompt}",
                    references=references,
                    duration=shot.duration,
                    seed=shot.seed,
                    width=request.width,
                    height=request.height,
                    steps=request.steps,
                    ref_image_size=request.ref_image_size,
                    prefix=f"music-video-producer/{project_id}/shots/{shot.id}-h3-reference",
                )
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
        else:
            try:
                timeline = build_director_timeline(
                    [shot], window_start=shot.start, window_duration=shot.duration, fps=24
                )
            except TimelineError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            try:
                payload = build_h3_director_payload(
                    timeline_data=timeline.timeline_data,
                    duration=shot.duration,
                    requested_frames=timeline.aligned_frames,
                    seed=shot.seed,
                    width=request.width,
                    height=request.height,
                    steps=request.steps,
                    start=shot.start,
                    prefix=f"music-video-producer/{project_id}/shots/{shot.id}-h3",
                )
            # The same translation the reference branch has. Without it the Director node's
            # own ceilings — 10000 frames, a 1000 s timeline — and a non-finite window reach
            # the client as a 500 instead of a refusal naming the limit.
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
        try:
            submission = await comfy.submit(payload)
        except ComfyError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        shot.status = "queued"
        shot.prompt_id = submission.prompt_id
        job = RenderJob(
            kind="h3",
            prompt_id=submission.prompt_id,
            target_id=shot.id,
            seed=shot.seed,
        )
        project.jobs.append(job)
        store.save(project)
        return job

    @app.post("/api/projects/{project_id}/director/chat", response_model=Project)
    async def director_chat(project_id: str, request: DirectorRequest) -> Project:
        # This snapshot is only ever used to build the prompt. It carries the user's message
        # so the model sees the turn it is answering, and it is then thrown away — see the
        # re-read after the await.
        snapshot = get_project(project_id)
        snapshot.messages.append(TreatmentMessage(role="user", content=request.message))
        # The recovery slots are excluded, and that is not an optimisation. This dump is the
        # whole project, so leaving them in would echo a second full copy of both documents
        # into every prompt — and the recorded root cause of the original document
        # corruption was degradation under rich context (JSON in context begets JSON), the
        # very failure `document_rejection` was written to catch. The locks stay: they are
        # two booleans, and knowing a document is off-limits is useful direction.
        context = snapshot.model_dump(mode="json", exclude=DIRECTOR_CONTEXT_EXCLUDE)
        try:
            result = await director.plan(message=request.message, project_context=context)
        except DirectorUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except DirectorError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        # Re-read after the await. A local model can hold this call open for many seconds,
        # and anything committed in that window — a lock set, a restore applied, a document
        # hand-edited — would otherwise be silently reverted by the stale snapshot on save.
        # Every decision below reads the fresh state: the lock that says do not touch this,
        # the existing text the guard compares against, and the slot being spent.
        project = get_project(project_id)
        project.messages.append(TreatmentMessage(role="user", content=request.message))
        notices: list[MessageNotice] = []
        replaced: list[str] = []
        first_drafts: list[str] = []
        not_requested: list[str] = []
        for field, label in DOCUMENT_LABELS.items():
            candidate = getattr(result, field)
            existing = getattr(project, field)
            # A candidate identical to the stored text is not a replacement, whatever the
            # guard says about it — `document_rejection` returns "" for an echo. Spending
            # the single recovery slot on it would annihilate the genuinely recoverable
            # version with a copy of the live one, and announcing it would be a change the
            # Director cannot find. Nothing captured, nothing assigned, nothing claimed.
            if candidate.strip() == existing.strip():
                continue
            reason = document_rejection(candidate, existing)
            # The lock is checked after the comparisons but before anything is written, so
            # nothing is assigned and nothing is captured — the lock must not spend the
            # recovery slot on a replacement it refused to make. It is *reported* only when
            # the candidate would genuinely have changed something, or a project with a
            # locked Treatment would carry the same paragraph on every reply forever.
            if getattr(project, f"{field}_locked"):
                if not reason:
                    notices.append(
                        MessageNotice(
                            kind="refusal", text=DOCUMENT_LOCK_NOTICE.format(document=label)
                        )
                    )
                continue
            # Consent is the second "do not write, and say why" gate, and it sits *after* the
            # lock deliberately: a lock is durable state the Director set and a flag is one
            # turn, so when both apply "locked" is the sentence worth reading — and it must
            # keep saying locked rather than merely unrequested, or unticking the box would
            # quietly relabel a protection as an oversight.
            #
            # It carries the lock's silence rule for the same reason: a candidate the guard
            # would have refused anyway would not have landed with consent either, so
            # reporting it as merely unrequested would invite a retry that also refuses.
            if not request.apply_documents:
                if not reason:
                    not_requested.append(label)
                continue
            if reason:
                # The candidate travels in `raw`, never in the sentence. It used to be pasted
                # into `content` — the one place in this module guaranteed to be handed back
                # to the model on the next turn. `MessageNotice` is what bounds it.
                notices.append(
                    rejection_notice(
                        DOCUMENT_REJECTED_NOTICE,
                        DOCUMENT_REJECTED_EMPTY_NOTICE,
                        raw=candidate,
                        document=label,
                        reason=reason,
                    )
                )
                continue
            # Capture on apply, never on attempt. Writing the recovery slot before the
            # guard ran would let a rejected candidate overwrite the only copy of the good
            # document — turning a protective refusal into the data loss it prevents.
            setattr(project, f"{field}_previous", existing)
            setattr(project, field, candidate)
            # A blank target accepts any first draft, by design, so the slot it captures is
            # empty and a restore would refuse. Reported separately: describing that as a
            # replacement whose previous version "can be restored" is a promise broken by
            # the very next click.
            (first_drafts if not existing.strip() else replaced).append(label)
        # Both statements go ahead of the "was NOT replaced" notices: what did change is what
        # the Director has to review, and it is the thing this reply used to never mention.
        if first_drafts:
            notices.insert(
                0, MessageNotice(kind="change", text=document_first_draft_notice(first_drafts))
            )
        if replaced:
            notices.insert(
                0, MessageNotice(kind="change", text=document_change_notice(replaced))
            )
        # One grouped statement rather than one per document: a declined turn wrote nothing, so
        # the Director needs the list and the reason once, not the same paragraph twice.
        if not_requested:
            notices.append(
                MessageNotice(kind="refusal", text=document_not_requested_notice(not_requested))
            )
        # A model that returned no sentence of its own must not leave the reply as a bare
        # separator followed by notices — the expansion route's guard, which this one lacked.
        message = result.message.strip() or CHAT_EMPTY_MESSAGE
        # The two empty-list notices are independent, and both can fire on one reply. They answer
        # different questions: this one says the reply contradicts itself, and the next says the
        # consent the Director gave produced nothing. Suppressing either would leave one of those
        # facts unsaid in exactly the turn it is about.
        if not result.shots and prose_claims_shots(message):
            notices.append(shot_claim_mismatch_notice(len(project.shots)))
        if request.apply_shots and not result.shots:
            notices.append(MessageNotice(kind="flag", text=SHOT_PLAN_EMPTY_NOTICE))
        for item in result.shots:
            if item.duration < H3_MIN_SHOT_SECONDS or item.duration > H3_MAX_SHOT_SECONDS:
                notices.append(
                    MessageNotice(
                        kind="flag",
                        text=SHOT_WINDOW_NOTICE.format(
                            duration=item.duration,
                            start=item.start,
                            minimum=H3_MIN_SHOT_SECONDS,
                            maximum=H3_MAX_SHOT_SECONDS,
                        ),
                    )
                )
        project.messages.append(assistant_reply(message, notices))
        if request.apply_shots and result.shots:
            merged_shots: list[Shot] = []
            for index, item in enumerate(result.shots):
                if index < len(project.shots):
                    shot = project.shots[index]
                    if not shot.locked:
                        shot.start = item.start
                        shot.duration = item.duration
                        shot.prompt = item.prompt
                    merged_shots.append(shot)
                else:
                    merged_shots.append(
                        Shot(start=item.start, duration=item.duration, prompt=item.prompt)
                    )
            merged_shots.extend(project.shots[len(result.shots) :])
            project.shots = merged_shots
        return store.save(project)

    @app.post("/api/projects/{project_id}/director/expand", response_model=Project)
    async def expand_shot_prompts(project_id: str) -> Project:
        """Turn the Treatment, Style Bible and timed Shot windows into a prompt per Shot (FR-26).

        A thin delegator over two pure things: `expansion_input` builds what the model sees, and
        `expansion_rejection` decides what may be written. Nothing here computes either, so both
        are testable without a route and the route can be asserted to pass the builder's output
        verbatim.

        Four properties are load-bearing:

        * **Keyed by shot id, never by position.** The chat route's positional merge is safe
          enough for start/duration, where a wrong assignment shows up as a visibly wrong
          window; a prompt is free text, so the same mistake after a concurrent add, delete or
          split would read as a plausible prompt forever and nothing downstream would fail.
        * **Nothing is rendered.** Expansion never touches `comfy`, never sets a Shot's
          `status`, and never queues a job. The prompt lands in the shot inspector, where the
          Director edits it and then decides about GPU time.
        * **No retiming.** `start`, `duration` and every window are untouched; only `prompt` is
          assigned.
        * **Only draft, unlocked Shots are written.** A lock is the Director's decision; render
          provenance is a fact about media that already exists. Both are reported rather than
          silently skipped, because "nothing happened to this Shot" has to say why.

        The empty-plan refusal, the re-read after the await, the 503/502 mapping and the single
        terminal `store.save` all follow `director_chat`.
        """
        # Built from the pre-await snapshot, exactly as the chat prompt is: this is what the
        # model sees, and it is then thrown away in favour of the re-read below.
        snapshot = get_project(project_id)
        if not snapshot.shots:
            raise HTTPException(status_code=422, detail=EXPANSION_WITHOUT_SHOTS)
        try:
            result = await director.expand(expansion_input=expansion_input(snapshot))
        except DirectorUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except DirectorError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        # Re-read after the await, for the reason `director_chat` documents — and here it is
        # also what makes id-keying meaningful: a Shot added, deleted or split while the model
        # was thinking is in this project and not in the snapshot the result answers.
        project = get_project(project_id)
        # Re-checked, not assumed from the snapshot: every Shot can be deleted while the model
        # is thinking, and saving a reply about a plan the pre-call guard would have refused
        # would leave the thread asserting an expansion of nothing.
        if not project.shots:
            raise HTTPException(status_code=422, detail=EXPANSION_WITHOUT_SHOTS)
        shots_by_id = {shot.id: shot for shot in project.shots}
        # Labelled from `ordered_shots`, the same call `expansion_input` numbers by, so the
        # notice and the model are talking about the same Shot under the same number.
        labels = {
            shot.id: expansion_shot_label(index, shot)
            for index, shot in enumerate(ordered_shots(project))
        }
        written: list[str] = []
        locked: list[str] = []
        rendered: list[str] = []
        unknown: list[str] = []
        duplicated: list[str] = []
        rejected: list[MessageNotice] = []
        answered: set[str] = set()
        for item in result.shots:
            shot = shots_by_id.get(item.shot_id)
            if shot is None:
                # Reported, not created and not guessed at. See EXPANSION_UNKNOWN_NOTICE. The
                # list is deduplicated because a model looping on one bad id would otherwise
                # repeat it through the whole notice.
                if item.shot_id not in unknown:
                    unknown.append(item.shot_id)
                continue
            # First answer wins, before any other check. Last-write-wins would let one Shot be
            # reported as refused *and* written in the same reply, and there is no reason to
            # prefer whichever contradiction arrived last.
            if shot.id in answered:
                if shot.id not in duplicated:
                    duplicated.append(shot.id)
                continue
            # Answered before the lock, provenance and rejection checks: the model did address
            # this Shot, so it is not an omission whatever happens to the prompt it sent.
            answered.add(shot.id)
            if shot.locked:
                locked.append(shot.id)
                continue
            # After the lock: both mean "not written", but a lock is a decision the Director
            # made and provenance is a fact about media, so when both apply the lock is the
            # sentence worth reading — the precedence `director_chat` uses for lock vs consent.
            if shot_render_provenance(shot):
                rendered.append(shot.id)
                continue
            reason = expansion_rejection(item.prompt)
            if reason:
                # The refused prompt is restored here, in `raw`. Story 2.2 dropped it because it
                # had nowhere to live that the next Director call would not read; the notice's
                # excluded field is that place, so the Director can now see what was refused.
                # `ExpandedShot.prompt` has no upper bound, so `MessageNotice` is what stops an
                # unbounded prompt reaching the manifest.
                rejected.append(
                    rejection_notice(
                        EXPANSION_REJECTED_NOTICE,
                        EXPANSION_REJECTED_EMPTY_NOTICE,
                        raw=item.prompt,
                        shot=labels[shot.id],
                        reason=reason,
                    )
                )
                continue
            shot.prompt = item.prompt
            written.append(shot.id)
        # A locked or already-rendered Shot the model never answered for is not an omission:
        # nothing was going to be written for it either way, and telling the Director to "run
        # expansion again if you want them written" would be advice that can never work.
        omitted = [
            shot.id
            for shot in project.shots
            if shot.id not in answered and not shot.locked and not shot_render_provenance(shot)
        ]
        notices: list[MessageNotice] = []
        # What changed goes first, as it does in the chat reply: it is the thing the Director has
        # to review, and everything below it is an explanation of something that did not happen.
        if written:
            notices.append(
                MessageNotice(
                    # The confirmation, and the one notice on this route that is good news:
                    # "Prompts written for 4 shot(s)" is the thing the Director pressed the
                    # button for, and dressing it as caution is how caution stops being read.
                    kind="change",
                    text=EXPANSION_WRITTEN_NOTICE.format(
                        count=len(written),
                        shots=", ".join(labels[shot_id] for shot_id in written),
                    ),
                )
            )
        # A lock and existing render provenance are decisions to *not* write; an omission and a
        # contradiction are the model behaving oddly about Shots nothing refused.
        for reported, wording, kind in (
            (locked, EXPANSION_LOCKED_NOTICE, "refusal"),
            (rendered, EXPANSION_RENDERED_NOTICE, "refusal"),
            (omitted, EXPANSION_OMITTED_NOTICE, "flag"),
            (duplicated, EXPANSION_DUPLICATE_NOTICE, "flag"),
        ):
            if reported:
                notices.append(
                    MessageNotice(
                        kind=kind,
                        text=wording.format(
                            shots=", ".join(labels[shot_id] for shot_id in reported)
                        ),
                    )
                )
        if unknown:
            notices.append(
                MessageNotice(
                    # Discarded rather than guessed at: a refusal to invent a Shot.
                    kind="refusal",
                    text=EXPANSION_UNKNOWN_NOTICE.format(
                        count=len(unknown),
                        shots=", ".join(_short(shot_id) for shot_id in unknown),
                    ),
                )
            )
        notices.extend(rejected)
        # A model that returned no sentence of its own must not leave the reply as a bare
        # separator followed by notices.
        message = result.message.strip() or EXPANSION_EMPTY_MESSAGE
        project.messages.append(assistant_reply(message, notices))
        return store.save(project)

    @app.get("/api/projects/{project_id}/jobs/{job_id}", response_model=RenderJob)
    async def read_job(project_id: str, job_id: str) -> RenderJob:
        project = get_project(project_id)
        job = next((item for item in project.jobs if item.id == job_id), None)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.prompt_id and job.status not in {"complete", "error", "cancelled"}:
            try:
                history = await comfy.history(job.prompt_id)
            except ComfyError as error:
                raise HTTPException(status_code=502, detail=str(error)) from error
            job.status = (
                history.status
                if history.status in {"queued", "running", "complete", "error"}
                else "running"
            )
            if job.status == "queued":
                # History is empty for both waiting and executing prompts. Only the live
                # queue distinguishes them, so a running render is not reported as queued.
                try:
                    located = await comfy.queue_state(job.prompt_id)
                except ComfyError:
                    located = "absent"
                if located == "running":
                    job.status = "running"
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
                if job.kind in {"flux", "multiview"}:
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
                            shot.latest_output = job.output_files[0]
            elif job.kind == "h3" and job.status == "error":
                shot = next((item for item in project.shots if item.id == job.target_id), None)
                if shot:
                    shot.status = "error"
            store.save(project)
        return job

    web_root = Path(__file__).parent / "web"
    if web_root.exists():
        assets_root = web_root / "assets"
        if assets_root.exists():
            app.mount("/assets", StaticFiles(directory=assets_root), name="assets")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(web_root / "index.html")

    return app


app = create_app()
