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

from .batch import (
    ReadinessReport,
    prompt_is_missing,
    readiness_refusal,
    readiness_report,
    shot_label,
)
from .comfy import ComfyClient, ComfyError
from .config import Settings
from .director import (
    DirectorClient,
    DirectorError,
    DirectorUnavailable,
    document_rejection,
)
from .models import (
    SHOT_MODE_SPECS,
    Asset,
    MessageNotice,
    Project,
    RenderJob,
    Shot,
    ShotStatus,
    Song,
    TreatmentMessage,
    VisionInspectionRecord,
    citations_in_role,
    mode_specification_problems,
    resolve_shot_mode,
)
from .preferences import EJECT_PREFERENCE_KEY, MachinePreferences
from .store import ProjectNotFound, ProjectStore
from .timeline import (
    H3_MAX_SHOT_SECONDS,
    H3_MIN_SHOT_SECONDS,
    TimelineError,
    build_director_timeline,
    expansion_input,
    ordered_shots,
)
from .vram import CliUnloader, LlmEjector
from .workflows import (
    H3_DEFAULT_PROFILE,
    H3_DIRECTOR_DEFAULT_HEIGHT,
    H3_DIRECTOR_DEFAULT_WIDTH,
    LTX25_ENHANCE_SEED,
    WorkflowCatalog,
    build_flux_payload,
    build_h3_director_payload,
    build_h3_reference_payload,
    build_ltx25_enhance_payload,
    build_multiview_payload,
    build_music3_payload,
    build_songplanner_invented_payload,
    build_songplanner_known_lyrics_payload,
    song_audio_window,
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

# The two context fields of a Song, keyed by field name and named for the screen. One mapping,
# and the recovery slots, the restore route's path segment, the per-field save loop and the
# labels are all derived from it — the same argument DOCUMENT_LABELS makes one line up.
#
# `SONG_LYRICS_FIELD`/`SONG_CAPTION_FIELD` below are these same two things worded for the middle
# of a refusal sentence ("The lyric sheet is 9001 characters"); these are worded to start one
# ("Lyric sheet was restored…"), exactly as DOCUMENT_LABELS' values are. A contract test holds
# the two spellings to the same words.
SONG_CONTEXT_LABELS = {"lyrics": "Lyric sheet", "caption": "Style description"}
SongContextField = Literal["lyrics", "caption"]

#: The suffix every single-slot recovery field in this application carries.
RECOVERY_SLOT_SUFFIX = "_previous"

# The Asset kinds a Krea multiview sheet can be promoted from, mapped to the subject the
# sheet is *of*. Two subjects, two prompt templates: a character sheet asks for a face
# close-up and full-body turns of a person, and no rewording of that sentence describes a
# cargo ship. `api.js`'s MULTIVIEW_SUBJECTS is the frontend half — it holds the templates,
# because the template is a default the Director may replace on the way out, not something
# the route imposes — and a contract test executes both sides and holds the kinds level.
#
# The gate this replaced read `kind != "character"`, which was never a statement about what
# Krea can do: a probe promoted a Flux cargo ship through this exact path by labelling it a
# character and got a clean, consistent sheet back. The capability was there; the refusal
# was ours. What the probe had to fake was the *label*, so the fix is that a prop is
# promotable as a prop.
MULTIVIEW_SUBJECTS = {"character": "character", "prop": "object", "setting": "object"}


def multiview_refusal() -> str:
    """The 422 sentence, naming every kind that *can* be promoted.

    Derived from MULTIVIEW_SUBJECTS rather than written out, because the failure this
    refusal exists to prevent is a Director staring at a promotable asset whose button
    the route disagrees about — and a hardcoded sentence goes stale in exactly the
    direction that produces it.
    """
    kinds = sorted(MULTIVIEW_SUBJECTS)
    named = f"{', '.join(kinds[:-1])} or {kinds[-1]}" if len(kinds) > 1 else kinds[0]
    return f"A completed {named} image is required for multiview generation"


# A mode this application can plan but cannot render, refused at the point of spending GPU time.
#
# Plannable and unrenderable is a deliberate pair, not an oversight to be tidied away: a Director
# laying out a first/middle/last section before that adapter exists is doing real work, and a mode
# that disappeared from the interface until its adapter landed would make that work impossible.
# What must never happen is the other failure — a mode that looks renderable and is not — which is
# what this sentence exists to prevent, naming the modes that *do* render so the refusal is
# actionable rather than only true.
MODE_WITHOUT_ADAPTER_REFUSAL = (
    "{shot} is a {mode} shot, which this application can plan but cannot yet render: no adapter "
    "has been built for it. Nothing was sent to ComfyUI and no GPU time was spent. The modes that "
    "render today are {available}."
)

# A shot whose mode and whose citations disagree, refused rather than resolved.
#
# The alternative is worse than a refusal and is what this branch used to do by omission: build the
# payload the attachments imply and log the render under a mode that was never applied. A GPU job
# recorded as one thing and rendered as another is invisible afterwards, which is the argument the
# profile and selector refusals below already make twice.
MODE_UNSPECIFIED_REFUSAL = (
    "{shot} is not fully specified for its mode. {problems} Nothing was sent to ComfyUI and no "
    "GPU time was spent."
)

#: Every graph builder `generate_h3` actually has a branch for.
#:
#: Checked against `SHOT_MODE_SPECS` at import, on `_withheld_fields`' argument. The hole this
#: closes is specific and silent: the route picks the reference branch on one adapter name and
#: falls through to the text-only graph otherwise, so a mode given a *third* adapter name in the
#: table — the next mode to be built — would pass the "can this render" gate and then render as
#: text-to-video, logged as though its own adapter had run. That is the one failure this story is
#: forbidden to introduce, and it has no symptom at the point it happens. The application refusing
#: to start does.
H3_ADAPTERS = frozenset({"h3-director", "h3-reference"})

if _unbuildable := {
    mode: spec.adapter
    for mode, spec in SHOT_MODE_SPECS.items()
    if spec.adapter and spec.adapter not in H3_ADAPTERS
}:
    raise RuntimeError(
        f"SHOT_MODE_SPECS names adapters generate_h3 cannot build: {sorted(_unbuildable)}. Give "
        "the route a branch for each and add its name to H3_ADAPTERS, or leave the mode's adapter "
        'as "" so it is refused at render rather than rendered as something else.'
    )


def mode_without_adapter_refusal(shot_name: str, mode: str) -> str:
    """The 422 for a plannable-but-unrenderable mode, naming what does render.

    The list of renderable modes is derived from `SHOT_MODE_SPECS` rather than written out, for
    `multiview_refusal`'s reason: a hardcoded list goes stale in exactly the direction that leaves
    a Director staring at a mode the refusal says is unavailable and the route accepts.
    """
    available = sorted(
        spec.label for spec in SHOT_MODE_SPECS.values() if spec.adapter
    )
    named = f"{', '.join(available[:-1])} and {available[-1]}" if len(available) > 1 else available[0]
    return MODE_WITHOUT_ADAPTER_REFUSAL.format(
        shot=shot_name, mode=SHOT_MODE_SPECS[mode].label, available=named
    )

# Every field a `Song` carries, classified into what the Director is shown and what is withheld.
#
# This is two *sets* rather than the one exclusion path the shape appears to want, and the reason
# is written a few lines below in DIRECTOR_CONTEXT_EXCLUDE's own comment: a nested path stops
# covering a field renamed or added beside it, silently. `{"song": {"lyrics_previous"}}` is
# exactly that shape. A third slot added to `Song` later — a `title_previous`, or a rename of
# these two — would leave the path still valid, still matching nothing, and the Director reading
# back a lyric sheet the Director deliberately discarded. A path can only be wrong by omission,
# and omission has no symptom.
#
# Classification does have a symptom. `_withheld_fields` refuses to produce an exclusion at all
# unless every declared field of the model appears in exactly one of these two sets, so adding a
# field to `Song` without deciding which side it belongs on raises at import: the application does
# not start, and every test in the suite fails on collection. That is deliberately louder than a
# leak deserves to be quiet. It costs nothing in production — `Song.model_fields` is code, never
# data, so this can only ever trip on a machine where someone is editing `models.py`.
#
# Only the slots are withheld. `path` and `prompt_id` are of no use to the model either, but they
# were in the dump before this change and taking them out is a change to what the Director is
# prompted with — which is Ask First, and is not what this story is for.
SONG_DIRECTOR_VISIBLE = frozenset({"title", "source", "path", "duration", "lyrics", "caption", "prompt_id"})
SONG_DIRECTOR_WITHHELD = frozenset(
    f"{field}{RECOVERY_SLOT_SUFFIX}" for field in SONG_CONTEXT_LABELS
)


def _withheld_fields(
    model: type[BaseModel],
    *,
    visible: frozenset[str],
    withheld: frozenset[str],
    family: str,
) -> set[str]:
    """The fields of `model` to strip from the Director's context, or a loud refusal.

    Returns `withheld` — but only after proving that `visible | withheld` is exactly the model's
    declared surface. Anything unclassified, anything classified twice, and anything named in a
    set that the model no longer declares are all `RuntimeError`s raised at import time.

    The return value is deliberately the *input*: this function exists for the check, not the
    computation. Deriving the answer instead (say, "every field ending `_previous`") would move
    the silent-omission problem rather than solve it — a slot named `lyrics_backup` would be
    derived out of the exclusion just as quietly as a path fails to match it.

    `family` names the pair of constants the refusal tells the next writer to edit — `"SONG"` for
    `SONG_DIRECTOR_VISIBLE`/`_WITHHELD`, `"SHOT"` for the Shot pair. Passed rather than derived
    from `model.__name__`, because this check is applied to two models and to test subclasses of
    them: a derived name sends someone to a constant that does not exist, and it does so in the one
    message whose whole job is to say what to do.
    """
    declared = set(model.model_fields) | set(model.model_computed_fields)
    where = model.__name__
    if overlap := visible & withheld:
        raise RuntimeError(
            f"{where}: {sorted(overlap)} is classified as both shown to the Director and "
            "withheld from it. Every field belongs on exactly one side."
        )
    if unclassified := declared - visible - withheld:
        raise RuntimeError(
            f"{where}: {sorted(unclassified)} is not classified as shown to the Director or "
            f"withheld from it. Add it to {family}_DIRECTOR_VISIBLE or {family}_DIRECTOR_WITHHELD "
            "— an unclassified field is echoed into every Director prompt by default, and a "
            "recovery slot echoed there is the version the Director deliberately discarded."
        )
    if stale := (visible | withheld) - declared:
        raise RuntimeError(
            f"{where}: {sorted(stale)} is classified but no longer declared on the model. A "
            "classification of a field that does not exist covers nothing."
        )
    return set(withheld)


# Every field a `Shot` carries, classified the same way `Song` is — and the answer to the question
# this story was told to ask, which was whether Shots were classified at all or only Songs.
#
# They were not. Only `Song` had a pair of sets, so every field ever added to `Shot` has entered
# the Director's prompt the moment it was declared, with nobody deciding that it should. This story
# added three at once — `mode`, `citations` and `singing` — which is exactly the situation the
# guard exists for, so it is extended rather than the three fields being waved through.
#
# **Nothing is withheld, and that is a deliberate empty set rather than an unfinished one.** Taking
# a field *out* of the dump changes what the Director is prompted with, which is Ask First and is
# not what this story is for; `latest_review`, `approved_output` and `prompt_id` are the obvious
# candidates and stay in because they were in it yesterday. What the classification buys today is
# that the *next* field cannot arrive without that decision being made — the application refuses to
# start until it is. `DIRECTOR_CONTEXT_EXCLUDE` below carries no `shots` key at all while this set
# is empty, so the dump is byte-identical to the one the Director got before this change.
#
# The three new fields are classified visible on their own merits, not by default. They are plan
# facts — what this shot is, what it cites, whether the performer sings — and they are the facts an
# assistant asked to fill a plan in would need to read. `mode` was already in the dump under its
# old spelling, so withholding it would be a removal.
SHOT_DIRECTOR_VISIBLE = frozenset(
    {
        "id",
        "start",
        "duration",
        "end",
        "prompt",
        "mode",
        "asset_ids",
        "citations",
        "reference_labels",
        "singing",
        "use_song_audio",
        "seed",
        "status",
        "prompt_id",
        "latest_output",
        "latest_review",
        "approved_output",
        "locked",
    }
)
SHOT_DIRECTOR_WITHHELD: frozenset[str] = frozenset()

#: The check, run for its refusal. Empty today; see `SHOT_DIRECTOR_VISIBLE`.
SHOT_DIRECTOR_WITHHELD_FIELDS = _withheld_fields(
    Shot, visible=SHOT_DIRECTOR_VISIBLE, withheld=SHOT_DIRECTOR_WITHHELD, family="SHOT"
)


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
    # The one nested path in this mapping, and the only one that is safe to write: it is not a
    # list of names anyone has to remember to extend, it is whatever `Song` declares and nobody
    # classified as visible. See SONG_DIRECTOR_WITHHELD.
    "song": _withheld_fields(
        Song, visible=SONG_DIRECTOR_VISIBLE, withheld=SONG_DIRECTOR_WITHHELD, family="SONG"
    ),
    # Present only once something is actually withheld from a Shot, so classifying every field as
    # visible leaves this mapping — and therefore the Director's prompt — exactly as it was. An
    # unconditional `{"shots": {"__all__": set()}}` would be an empty exclusion that looks like a
    # policy, and the next reader would have to run it to find out it excludes nothing.
    **(
        {"shots": {"__all__": SHOT_DIRECTOR_WITHHELD_FIELDS}}
        if SHOT_DIRECTOR_WITHHELD_FIELDS
        else {}
    ),
    **{f"{field}{RECOVERY_SLOT_SUFFIX}": True for field in DOCUMENT_LABELS},
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


#: The Shot statuses `render_again` recognises as settled — a Shot that has something to redo.
#:
#: `error` is in here deliberately and is the likeliest use of the whole action: a render that
#: failed is the most obvious thing anyone wants to try again. `approved` is in here too even
#: though the route then refuses it — the refusal is the point, and a status the route did not
#: recognise would fall through to the silent no-op below and say nothing about the approval.
#:
#: `api.js`'s RENDER_AGAIN_STATUSES is the frontend half, and decides when the control is drawn
#: at all. A contract test asserts the two lists are identical, because a control offered for a
#: status the route does not re-open is a button whose only outcome is a refusal.
RENDER_AGAIN_STATUSES: tuple[ShotStatus, ...] = ("complete", "error", "approved")

#: A render that has been accepted and has not finished. Two of these for one Shot would write
#: the same prefix at the same time and race on which take the manifest ends up naming.
RENDER_IN_FLIGHT_STATUSES: frozenset[str] = frozenset({"queued", "running"})

# Why one Shot may not be re-opened. Each names the Shot as the timeline names it, because a bare
# `shot_a1b2c3d4e5f6` appears nowhere in the interface — the same reason `generate_h3`'s refusal
# carries `shot_label`.
#
# The lock wording says what a lock is for rather than only that one is set, matching
# EXPANSION_LOCKED_NOTICE: a lock stops an automated rewrite, and the human who set it can undo it.
RENDER_AGAIN_LOCKED_REFUSAL = (
    "{shot} is locked. A lock is a deliberate hands-off on this shot, and re-opening it for "
    "another render is exactly the kind of change it refuses. Unlock the shot first."
)
# The one refusal here that is about meaning rather than mechanics, and it has to read that way.
# Nothing technical stops a second render over an approved take; what stops it is that the
# approval is a decision *about a particular piece of media*, and after a re-render the decision
# would be attached to something nobody approved. Clearing the approval is how a Director says the
# decision has changed — which is a thing they should have to say, not a side effect of a button.
RENDER_AGAIN_APPROVED_REFUSAL = (
    "{shot} carries an approved take. An approval is an editorial decision about one specific "
    "take, so rendering over it would leave that decision describing a take that no longer "
    "exists. Clear the approval first if the decision has changed."
)
# Concurrency, stated as the concrete harm rather than as a busy signal. The staleness escape is
# named in the same sentence: job status only moves when `read_job` is asked, so a job that
# finished while nobody was polling still reads as in flight here.
RENDER_AGAIN_IN_FLIGHT_REFUSAL = (
    "A render for {shot} has not finished. Two renders of one shot would race on its output, so "
    "nothing was re-opened. Wait for it, or refresh the render queue if it has already finished "
    "and this project has not been told yet."
)
# What re-opening does to the take that is already there, said in full rather than implied.
#
# This is the whole of the honest answer to "no silent destruction of the previous take's record",
# and it is deliberately not a promise of take management. Three separate facts, and the third is
# the one that stops the first two from being read as more than they are:
#
#   * The file survives. ComfyUI's savers number their outputs from the filename prefix, so a
#     second render of one shot writes `…_00002` beside `…_00001` rather than over it. Verified on
#     this installation: one shot's prefix carries `_00001`, `_00002` and `_00003` on disk.
#   * The job that produced it keeps naming it. `RenderJob.output_files` is per submission and is
#     never rewritten, so the previous take stays reachable through the render queue.
#   * The application is not tracking takes. Only `Shot.latest_output` moves, and it is a single
#     pointer, not a list. Nobody should read any of the above as a take history.
RENDER_AGAIN_PREVIOUS_TAKE = (
    "{shot} is open for another render. The take already there is not deleted: ComfyUI numbers "
    "its output files, so the next render writes a new numbered file beside the old one rather "
    "than over it, and the job that produced the old take goes on naming it in the render queue. "
    "What moves is this shot's single latest-take pointer, once the new take lands. This "
    "application does not track takes, so the older file is on disk and not in a take list."
)
# Deliberately ASCII, exactly as `batch.READINESS_REFUSAL` is and for the same reason: the
# frontend half of this sentence is read back through node, whose stdout the contract test decodes
# with the platform encoding on Windows, and a typographic dash would come back mangled and fail
# the test that holds the two wordings together for a reason that has nothing to do with them.


def render_again_refusal(project: Project, shot: Shot) -> tuple[int, str] | None:
    """Why this settled Shot may not be re-opened, as (status code, sentence), or None.

    One function rather than a chain of `raise`s inside the route, so every refusal this action
    can give is visible in one place and the *order* is a thing that can be read. Order matters
    and is not arbitrary: the first three ask whether this Shot may be touched at all, and the
    prompt gate asks whether rendering it would produce anything. A locked Shot with a blank
    prompt is refused for its lock, because unlocking is what the Director has to do first.

    The prompt gate is last and is asked **again**, from the prompt as it is right now. That is
    the point of this story: the readiness gate is not a "render once" rule that a Shot passes
    permanently, it is a "do not render nonsense" rule, and a prompt can be edited to nothing —
    or back to the `"New shot"` placeholder — between one render and the next. Nothing here reads
    the fact that this Shot rendered successfully once as evidence about the prompt it has now.

    Asked through `prompt_is_missing` rather than through `readiness_report`, unlike
    `generate_h3`. Both are AD-5's one implementation — `readiness_report` calls
    `prompt_rejection` per Shot and this calls it through `prompt_is_missing`, so there is no
    second definition of empty — but this route is about exactly one Shot and has no use for a
    whole-plan pass. The refusal sentence is `readiness_refusal`'s, so the Director reads the
    same instruction here as at submission.
    """
    if shot.locked:
        return 422, RENDER_AGAIN_LOCKED_REFUSAL.format(shot=shot_label(project, shot))
    if shot.approved_output or shot.status == "approved":
        return 422, RENDER_AGAIN_APPROVED_REFUSAL.format(shot=shot_label(project, shot))
    if prompt_is_missing(shot):
        return 422, readiness_refusal([shot_label(project, shot)])
    return None


#: The Shot statuses the mark-ready action owns — the two sides of a Shot's *first* render.
#:
#: Deliberately the exact complement of `RENDER_AGAIN_STATUSES` plus the in-flight pair, and a
#: test pins that: every member of `ShotStatus` belongs to exactly one of the two actions, so a
#: status added later cannot fall through both and become a Shot nothing can move.
#:
#: `api.js`'s MARK_READY_STATUSES is the frontend half and decides when the control is drawn at
#: all. A contract test asserts the two lists are identical, for `RENDER_AGAIN_STATUSES`' reason:
#: a control offered for a status the route does not own is a button whose only outcome is a
#: refusal.
MARK_READY_STATUSES: tuple[ShotStatus, ...] = ("draft", "ready")

# Why one Shot may not be armed for, or taken back from, its first render. Each names the Shot as
# the timeline names it, for `render_again`'s reason: a bare `shot_a1b2c3d4e5f6` appears nowhere in
# the interface.
#
# The lock wording is `RENDER_AGAIN_LOCKED_REFUSAL`'s argument applied to this action rather than a
# copy of its sentence, because the two actions do different things and a sentence about
# "re-opening it for another render" read on a shot that has never rendered describes nothing the
# Director did. What is shared is the rule and the remedy: a lock is a deliberate hands-off, and the
# human who set it can undo it.
MARK_READY_LOCKED_REFUSAL = (
    "{shot} is locked. A lock is a deliberate hands-off on this shot, and committing it to the "
    "render queue is exactly the kind of change it refuses. Unlock the shot first."
)
# A Shot past its first render is the other action's subject, and the refusal says so by name
# rather than only refusing. Without the direction, a Director looking at a completed shot is told
# what they may not do and nothing about what they may — and the thing they may do is one button
# further down the same panel.
MARK_READY_ALREADY_RENDERED_REFUSAL = (
    "{shot} has already been through a render, so it is past the point this action is about. "
    'Committing a shot to the queue applies to its first render; use "Render again" to re-open a '
    "shot that has already produced a take."
)
# The in-flight case, separated from the settled one because the honest sentence differs and the
# direction above would be wrong: "Render again" refuses a live render too, so sending a Director
# there would spend a second click on a second refusal. Stated as the concrete harm, with the same
# staleness escape `RENDER_AGAIN_IN_FLIGHT_REFUSAL` names, because job status only moves when
# `read_job` is asked.
MARK_READY_IN_FLIGHT_REFUSAL = (
    "A render for {shot} has not finished, so its status is not yours to set right now. Wait for "
    "it, or refresh the render queue if it has already finished and this project has not been "
    "told yet."
)
# An approval is an editorial decision about one specific take. A Shot carrying one is not a Shot
# waiting for its first render, whatever its status field happens to say — `approved_output` is
# settable independently of `status`, so this is reachable without the `approved` status and must
# not be a way past `RENDER_AGAIN_APPROVED_REFUSAL`'s argument.
MARK_READY_APPROVED_REFUSAL = (
    "{shot} carries an approved take, which is an editorial decision about one specific take "
    "rather than a shot waiting to be rendered. Clear the approval first if the decision has "
    "changed."
)
# Deliberately ASCII, exactly as `batch.READINESS_REFUSAL` and the render-again refusals are, and
# for the same reason: the frontend halves are read back through node, whose stdout the contract
# test decodes with the platform encoding on Windows.

# What the Director is told after each direction, said rather than implied. Both sentences exist to
# manage one belief and its opposite.
#
# Marking ready is the step immediately before the expensive one, so the thing worth saying is that
# it is *not* the expensive one: nothing has been rendered and nothing has been spent. A bare
# "marked ready" toast beside a button that arms a GPU job invites the reading that the job started.
MARK_READY_NOTICE = (
    "{shot} is committed to the render queue. Nothing has been rendered and no GPU time has been "
    "spent by this: the queue submits it when you choose to."
)
# And un-committing has to say that it costs nothing, or a Director who is unsure will leave a shot
# armed rather than risk losing the prompt they wrote.
MARK_DRAFT_NOTICE = (
    "{shot} is back to draft, so the render queue will not submit it. Nothing else about the shot "
    "changed and nothing was deleted."
)


def mark_ready_refusal(
    project: Project, shot: Shot, *, target: ShotStatus
) -> tuple[int, str] | None:
    """Why this Shot may not be moved to `target`, as (status code, sentence), or None.

    One function for both directions and one place the *order* can be read, exactly as
    `render_again_refusal` is. The order is the same argument as that function's: the first three
    ask whether this Shot may be touched at all, and the prompt gate asks whether rendering it
    would produce anything. A locked Shot with a blank prompt is refused for its lock, because
    unlocking is what the Director has to do first.

    The prompt gate is last, is asked only in the arming direction, and is asked **from the prompt
    as it is right now**. Only in the arming direction because `draft` is the un-armed state:
    refusing to disarm a Shot whose prompt was emptied would trap it armed, which is precisely
    backwards. And from the prompt right now because marking ready is not a certificate — nothing
    downstream may read `status == "ready"` as evidence about the prompt a Shot has *later*, which
    is why `generate_h3` asks the same question again and why a matrix row exists for a Shot whose
    prompt is emptied after it was marked.

    Asked through `prompt_is_missing` rather than through `readiness_report`, for
    `render_again_refusal`'s reason: both are AD-5's one implementation — `readiness_report` calls
    `prompt_rejection` per Shot and this calls it through `prompt_is_missing`, so there is no
    second definition of empty — but this action is about exactly one Shot and has no use for a
    whole-plan pass. The refusal sentence is `readiness_refusal`'s, so the Director reads the same
    instruction here as at submission.
    """
    # First, and ahead of everything including the status class, for `render_again`'s reason: an
    # in-flight Shot is the one state where getting this wrong does concrete harm, and it is the
    # state a hand-walked-back status hides. `draft` with a live job is exactly that case, so this
    # must not be reachable only through the statuses below.
    #
    # 409 rather than 422, and the only refusal here that is not a 422. A live render is a state
    # conflict — the same request will succeed once the render lands, and nothing about it is
    # unprocessable — which is why `render_again` answers 409 for this exact shot, and answering it
    # differently through this route was an inconsistency the Director renegotiated on 2026-08-18.
    # The rest stay 422: locked, already-rendered, approved and the prompt gate are all facts about
    # the Shot that no amount of waiting changes. A route test asserts this and `render_again` give
    # one code for one live render, because the drift is what the change exists to close.
    if shot_render_in_flight(project, shot):
        return 409, MARK_READY_IN_FLIGHT_REFUSAL.format(shot=shot_label(project, shot))
    if shot.status not in MARK_READY_STATUSES:
        return 422, MARK_READY_ALREADY_RENDERED_REFUSAL.format(shot=shot_label(project, shot))
    if shot.locked:
        return 422, MARK_READY_LOCKED_REFUSAL.format(shot=shot_label(project, shot))
    if shot.approved_output:
        return 422, MARK_READY_APPROVED_REFUSAL.format(shot=shot_label(project, shot))
    if target == "ready" and prompt_is_missing(shot):
        return 422, readiness_refusal([shot_label(project, shot)])
    return None


def shot_render_in_flight(project: Project, shot: Shot) -> bool:
    """True when a render for this Shot has been accepted and is not known to have finished.

    Both signals are read, because they can disagree and the disagreement is the dangerous case.
    `Shot.status` is the half that is right when no job record was ever written; the job records
    are the durable half, keyed by `target_id`, and they are what still says "in flight" when the
    status has been walked backwards by hand through the generic shots write — which is precisely
    how a Shot can read `complete` on screen while ComfyUI is still working on it.

    Job status only ever moves when `read_job` is asked, so a job that finished while nobody was
    polling still reads `queued` here and this returns True. That is the safe direction to be
    wrong in — the cost is a refusal that says to refresh the queue, against a second render
    racing a first one for the same output prefix.
    """
    if shot.status in RENDER_IN_FLIGHT_STATUSES:
        return True
    return any(
        job.kind == "h3" and job.target_id == shot.id and job.status in RENDER_IN_FLIGHT_STATUSES
        for job in project.jobs
    )


def shot_enhancement_in_flight(project: Project, shot: Shot) -> bool:
    """True when an LTX 2.5 enhancement for this Shot has been accepted and has not landed.

    Its own function rather than a `kind` added to `shot_render_in_flight`, because the two
    read different signals and only one of them may read `Shot.status`. An enhancement
    deliberately does not touch the Shot at all — not its status, not its `latest_output` —
    so that a take being enhanced is left exactly as it was. The job records are therefore
    the *only* evidence an enhancement is running, and folding this into the render check
    would make a live H3 render and a live enhancement indistinguishable in the sentence the
    Director reads.

    Job status only ever moves when `read_job` is asked, so an enhancement that finished
    while nobody was polling still reads `queued` here. That is the safe direction: the cost
    is a refusal that says to refresh the queue, against two enhancements racing on one
    output prefix.
    """
    return any(
        job.kind == "ltx" and job.target_id == shot.id and job.status in RENDER_IN_FLIGHT_STATUSES
        for job in project.jobs
    )


# Why one Shot's take may not be sent to the LTX 2.5 enhancer. Each names the Shot as the
# timeline names it, for `render_again`'s reason: a bare `shot_a1b2c3d4e5f6` appears nowhere in
# the interface.
#
# Enhancement takes a *take* as its input, so the thing that must exist is a rendered file — not
# a prompt, and not a status. This route deliberately asks no readiness question: the graph's
# prompt is empty, so a Shot with no prompt would enhance exactly as well as one with a prompt,
# and refusing it would be a gate borrowed from a path that generates.
ENHANCE_NO_TAKE_REFUSAL = (
    "{shot} has not produced a take, and enhancement improves a take rather than making one. "
    "Render the shot first, then enhance the take you want to keep."
)
# Names the path, per the matrix. A manifest pointing at a file that is gone is usually a moved
# or cleared ComfyUI output directory, and the only way the Director can tell which is to see
# where this looked.
ENHANCE_MISSING_TAKE_REFUSAL = (
    "{shot}'s take is recorded as {path} and there is no file there. Nothing was submitted. "
    "The take may have been moved or the ComfyUI output directory cleared."
)
# Concurrency, stated as the concrete harm rather than as a busy signal, with the same staleness
# escape the render refusals name. Covers a live render as well as a live enhancement: enhancing
# a take while the shot is re-rendering means spending GPU minutes on a take that is about to
# stop being the shot's latest one.
ENHANCE_IN_FLIGHT_REFUSAL = (
    "Work on {shot} has not finished. Enhancing a take while a render or another enhancement is "
    "still running would spend GPU minutes on a take that may be about to change, so nothing was "
    "submitted. Wait for it, or refresh the render queue if it has already finished and this "
    "project has not been told yet."
)
#: The filename prefix an enhancement writes under, appended to the shot's own. Its whole job is
#: to be *different* from the H3 prefixes: ComfyUI numbers its outputs per prefix, so an
#: enhancement sharing the render's prefix would take the next number in that shot's take
#: sequence and become indistinguishable from a take. A separate prefix is what puts the enhanced
#: file beside the take rather than in the middle of the series.
ENHANCE_PREFIX_SUFFIX = "-ltx25-enhance"
# Deliberately ASCII, exactly as `batch.READINESS_REFUSAL` and the render-again refusals are, and
# for the same reason: the frontend halves are read back through node, whose stdout the contract
# test decodes with the platform encoding on Windows.


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


class SongContextRequest(BaseModel):
    """What a Song is, as opposed to where its audio lives.

    Deliberately only these two fields. A body that could carry `path`, `duration`, `source` or
    `prompt_id` would be a Song replacement wearing an edit's name, and the route that binds it
    could not tell the difference — the audio and the provenance are not editable text, so they
    are not on the wire at all. Both are plain defaults rather than tri-state: an omitted field
    blanks its half, which is what makes clearing a wrong lyric sheet possible. The bounds are
    enforced in the route by `_song_context`, shared with the import, so one sheet cannot be
    accepted by one door and refused by the other.
    """

    lyrics: str = ""
    caption: str = ""


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
    # `None` rather than 1344/768, for `steps`' reason one field down: an omitted size has to
    # be distinguishable from one the Director typed, because the reference path now resolves
    # an omission through `select_resolution` while an explicit size is honoured exactly. A
    # literal default here would make every caller who never touched the field look like a
    # caller who asked for 1344x768 — which is how this project came to render everything at a
    # size nobody chose.
    #
    # The bounds are unchanged, so a size that was accepted before is accepted now and one
    # that was refused is still refused. `multiple_of=32` is what keeps an explicit frame on
    # the same grid the selector rounds to; the reference default, 1056x608, satisfies it.
    width: int | None = Field(default=None, ge=256, le=2048, multiple_of=32)
    height: int | None = Field(default=None, ge=256, le=2048, multiple_of=32)
    # The Director's own control surface: `ResolutionSelector`'s three inputs, with its own
    # declared ranges so an out-of-range figure is a 422 here rather than a ComfyUI validation
    # failure seen as an opaque 502 after submission. All three are `None` by default, and all
    # three are refused alongside an explicit width/height — see `build_h3_reference_payload`.
    megapixels: float | None = Field(default=None, ge=0.1, le=16.0)
    # Spelled out as a `Literal` rather than derived from `H3_ASPECT_RATIOS`, for the reason
    # `profile` is: a `Literal` is what puts the choices in `/openapi.json` and turns an
    # unknown value into a 422 before any payload is built. `tests/test_api.py` asserts this
    # list and the builder's table agree, so an option added to one and not the other fails
    # loudly rather than quietly refusing something ComfyUI would have accepted.
    aspect_ratio: (
        Literal[
            "1:1 (Square)",
            "2:3 (Portrait Photo)",
            "3:2 (Photo)",
            "3:4 (Portrait Standard)",
            "4:3 (Standard)",
            "9:16 (Portrait Widescreen)",
            "16:9 (Widescreen)",
            "21:9 (Ultrawide)",
        ]
        | None
    ) = None
    multiple: int | None = Field(default=None, ge=8, le=128)
    # `None` rather than 20 so an omitted count is distinguishable from one the Director
    # typed. The reference profiles carry different step counts — 20 for the audited
    # export's graph, 4 for the turbo bundle, 8 for the canonical References2V one — and a
    # literal default here would silently send 20 steps into a 4-step LoRA bundle for every
    # caller who never touched the field.
    # An omitted count falls through to the profile's own; the text-only Director path,
    # which takes no profile, falls through to `H3_DIRECTOR_DEFAULT_STEPS` — the same 20
    # it defaulted to here before.
    steps: int | None = Field(default=None, ge=1, le=100)
    ref_image_size: Literal["match", "max"] = "match"
    # The literal is spelled out rather than derived from `H3_REFERENCE_PROFILES`, because
    # a `Literal` is what puts the choices in `/openapi.json` and turns an unknown value
    # into a 422 before any payload is built. `tests/test_api.py` asserts the two lists
    # agree, so a profile added to the builder and not offered here fails loudly.
    profile: Literal["default", "turbo", "turbo-references2v"] = "default"


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


class VramEjectRequest(BaseModel):
    """The Director's choice about the VRAM eject. Required, with no default.

    A default here would make an empty body mean something, and the one body a confused
    client is most likely to send is an empty one. This way an omission is a 422 naming the
    field rather than a setting that changed to a value nobody asked for.
    """

    enabled: bool


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


# What a Song's two context fields are called when a refusal has to name one, and the longest
# either may be.
#
# Both ceilings are the ones the generation routes already impose on the very same two fields —
# `SongPlannerRequest.lyrics` caps a supplied lyric sheet at 8000 characters and its `idea`, which
# lands on `Song.caption`, at 4000. An imported Song and a generated one are the same record, read
# by the same Director context, so a second pair of numbers here would mean a sheet that can be
# generated but not imported, or the reverse. They exist to reject nonsense — a pasted novel, a
# whole album — not to legislate lyric length.
SONG_LYRICS_FIELD = "The lyric sheet"
SONG_LYRICS_LIMIT = 8_000
SONG_CAPTION_FIELD = "The style description"
SONG_CAPTION_LIMIT = 4_000

# The bound each context field is measured against, keyed the way `SONG_CONTEXT_LABELS` is, so the
# route that writes both can loop rather than spell each field out twice — which is how a counter,
# a ceiling or a slot ends up wired to the other field.
SONG_CONTEXT_LIMITS = {"lyrics": SONG_LYRICS_LIMIT, "caption": SONG_CAPTION_LIMIT}
SONG_CONTEXT_FIELD_NAMES = {"lyrics": SONG_LYRICS_FIELD, "caption": SONG_CAPTION_FIELD}

# The one wording for a song-context restore, and for refusing one. `api.js`'s
# SONG_CONTEXT_RESTORE_NOTICE and SONG_CONTEXT_RESTORE_REFUSAL_MARKER are the frontend halves.
#
# A restore is a swap, exactly as the document one is: the text being displaced moves into the
# slot, so restoring again puts it back and a mis-click costs nothing. Unlike the document
# restore there is no one-way case to warn about — an empty previous version is a real previous
# version here (`Song.lyrics_previous` is `None` until a save displaces something), so displacing
# a blank leaves a blank in the slot and the swap stays symmetric.
SONG_CONTEXT_RESTORE_NOTICE = (
    "{field} was restored to the version kept before the last save that changed it. The text "
    "that was replaced is now the kept version, so restoring again swaps back. Nothing else "
    "about the song changed: not the audio, its length or its provenance."
)
# The refusal's wording deliberately shares no phrase with `DOCUMENT_RESTORE_REFUSAL`: both halves
# recognise their own refusal by substring, and an overlapping phrase would let one recovery path
# claim the other's failure and "refresh" a project that was never stale.
SONG_CONTEXT_RESTORE_REFUSAL = (
    "No previous version of {field} was kept for this song, so there is nothing to swap back to. "
    "A version is kept when a save replaces stored text with different text."
)

# The one wording for an oversized field, shared by the import and the edit. It says what was *not*
# done, because both callers reach this before anything is written: an import that refuses here has
# copied no audio and left the previous Song exactly as it was, and an edit has changed nothing.
SONG_CONTEXT_TOO_LONG = (
    "{field} is {length} characters, past the {limit} this application stores for it. Nothing was "
    "saved: no audio was written and the song was not changed. Shorten it and try again."
)

# Why the edit route has nothing to edit. Its own sentence rather than a bare 404, because the
# remedy differs from every other missing-thing here: import or generate a song first.
SONG_CONTEXT_WITHOUT_SONG = (
    "This project has no song, so there is no song context to change. Import or generate a song "
    "first; an import can carry its lyric sheet and style description with it."
)


def _song_context(value: str, limit: int, field: str) -> str:
    """One field of a Song's context: trimmed at the edges only, and bounded.

    Leading and trailing whitespace goes and nothing else does. Interior blank lines, indentation
    and section tags are the *structure* of a lyric sheet, and the known-lyrics generation path
    already treats them that way (`SongPlannerRequest.lyrics` is `strip_whitespace=True` and
    nothing more) — a second, tidier contract here would mean the same sheet stored two different
    ways depending on which door it came through.

    Nothing is parsed. A section tag in a supplied sheet looks like timing information and is not;
    reading structure out of it here would create a second, worse source of truth for something a
    song analyser should own.

    The bound is measured after the trim, so a sheet that only *looks* oversized because it was
    pasted with a trailing page of newlines is accepted rather than refused for whitespace.
    """
    text = value.strip()
    if len(text) > limit:
        raise HTTPException(
            status_code=422,
            detail=SONG_CONTEXT_TOO_LONG.format(field=field, length=len(text), limit=limit),
        )
    return text


def _detach_song_recovery_slots(song: Song | None) -> None:
    """Leave `song` with no kept context versions at all.

    A slot describes the track it sits on. Carried across a replacement it would offer the
    Director a "previous version" of a song that is gone — a lyric sheet from the track this
    project used to have, restorable onto the one it has now, silently mislabelled as this
    song's own history. Every route that puts a *different* song on the project calls this, or
    builds a fresh `Song` whose slots default to `None`, which is the same thing said in the
    constructor.
    """
    if song is None:
        return
    for field in SONG_CONTEXT_LABELS:
        setattr(song, f"{field}{RECOVERY_SLOT_SUFFIX}", None)


def _adopt_song_recovery_slots(incoming: Song | None, stored: Song | None) -> None:
    """Overwrite `incoming`'s slots with the stored song's, because a client is never their author.

    `PUT /api/projects/{id}` binds a whole client-supplied `Project`, so its `song` arrives with
    every field defaulted — including these. That is the sibling write path the document slots
    already had to be defended on, and it fails two ways at once here. A client written before the
    slots existed omits them, so an ordinary save arrives carrying `None` and wipes both kept
    versions. A client that *invents* one is worse: it would be planting text that the restore
    route then swaps into the live lyric sheet as "the version you had before".

    Adoption happens *before* the route compares the two songs, so a body that differs only in
    these fields compares equal and does not trip the replacement gate — which would otherwise
    demand a song-replacement confirmation for an ordinary save from an old client.
    """
    if incoming is None:
        return
    for field in SONG_CONTEXT_LABELS:
        slot = f"{field}{RECOVERY_SLOT_SUFFIX}"
        setattr(incoming, slot, getattr(stored, slot) if stored is not None else None)


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
    ejector: LlmEjector | Any | None = None,
    preferences: MachinePreferences | Any | None = None,
) -> FastAPI:
    settings = settings or Settings()
    store = store or ProjectStore(settings.data_root)
    # Machine-scoped, deliberately not project-scoped: see `preferences.py`. It sits beside
    # `projects/` under the same data root, so a project directory copied to another machine
    # carries no opinion about that machine's VRAM.
    preferences = preferences or MachinePreferences(settings.data_root)
    director = director or DirectorClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
    )
    # Built before the ComfyUI client because the client takes it as its pre-submission
    # hook. That single wiring is what covers all five submission routes — and any route
    # added later — without any of them knowing a language model exists. An injected
    # `comfy` (every test does this) has no hook and therefore never ejects, which is why
    # the existing suites gained no dependency on a language-model host.
    #
    # `getattr` on the busy gate, not `director.busy`: `director` may be an injected double
    # that has never heard of it, and a missing gate must mean "not busy" rather than an
    # AttributeError inside a hook that runs before every render.
    #
    # Where the starting value comes from, decided once here rather than in each reader.
    # **The environment decides how the application starts; the control decides what happens
    # next.** An explicitly configured `MVP_LLM_EJECT_BEFORE_RENDER` — from the environment,
    # from `.env`, or passed to `Settings` — wins over anything stored, because an operator
    # who pins it in a startup file is saying how this machine starts and the interface must
    # not show a default it is not honouring. Absent that, the Director's last stored choice
    # wins over the built-in default, which is the whole point of a control that survives a
    # reload. A runtime change through the route always takes effect immediately and is
    # stored; the next start re-applies this same precedence to it.
    #
    # `model_fields_set` is what distinguishes "configured to True" from "defaulted to True".
    # Comparing the value against the default cannot: `MVP_LLM_EJECT_BEFORE_RENDER=1` and no
    # variable at all are the same value and mean different things.
    eject_pinned_by_environment = "llm_eject_before_render" in settings.model_fields_set
    stored_eject = preferences.get_bool(EJECT_PREFERENCE_KEY)
    if eject_pinned_by_environment or stored_eject is None:
        eject_enabled = settings.llm_eject_before_render
        eject_source = "environment" if eject_pinned_by_environment else "default"
    else:
        eject_enabled = stored_eject
        eject_source = "director"
    ejector = ejector or LlmEjector(
        base_url=settings.llm_base_url,
        enabled=eject_enabled,
        unload_timeout=settings.llm_eject_timeout,
        unloader=CliUnloader(settings.llm_eject_executable)
        if settings.llm_eject_executable
        else None,
        is_busy=lambda: bool(getattr(director, "busy", False)),
    )
    comfy = comfy or ComfyClient(
        settings.comfy_url,
        timeout=settings.request_timeout,
        before_submit=ejector.eject,
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
    app.state.ejector = ejector
    app.state.preferences = preferences
    # Where the *current* value came from, which changes the moment the Director changes the
    # setting. Held on `app.state` rather than closed over so it stays inspectable, and read
    # back below rather than captured, because a closure over the local would keep reporting
    # the startup answer forever.
    app.state.eject_source = eject_source

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

    def vram_eject_state() -> dict[str, Any]:
        """The setting, where it came from, and what the last eject actually did.

        `enabled` is read off the ejector rather than from any copy, because the ejector's
        own attribute is the thing every submission consults — a second field that could
        disagree with it is a field that will eventually lie.

        `last` carries only what the host itself reported: which models were resident before
        the attempt and which are resident after it. There is deliberately **no free-VRAM
        figure**. Measured on 2026-08-18, the reading fell 31.6 → 16.0 GB across one eject of
        a 4.71 GB model, because ComfyUI released its own cache in the same moment; a number
        that looks like evidence and is not is worse than no number. See `docs/OPERATIONS.md`.
        """
        outcome = getattr(ejector, "last_outcome", None)
        return {
            "enabled": bool(getattr(ejector, "enabled", False)),
            "source": app.state.eject_source,
            "environment_pinned": eject_pinned_by_environment,
            "last": None
            if outcome is None
            else {
                "status": outcome.status.value,
                "detail": outcome.detail,
                "resident_before": list(outcome.resident_before),
                "resident_after": list(outcome.resident_after),
            },
        }

    @app.get("/api/vram-eject")
    def read_vram_eject() -> dict[str, Any]:
        return vram_eject_state()

    @app.put("/api/vram-eject")
    def set_vram_eject(request: VramEjectRequest) -> dict[str, Any]:
        """Turn the eject on or off for every submission route, from now on.

        One assignment, to the one attribute `LlmEjector._attempt` reads on its way in. It
        gates at the `before_submit` funnel rather than at any route, so a submission path
        added tomorrow is covered without knowing this setting exists — and so turning the
        setting *on* adds no code to any render path that could fail one.

        The store is written before the ejector is changed. A choice that cannot be saved is
        refused outright rather than applied for this session only: leaving the setting live
        but unsaved puts a value on screen that silently reverts at the next start, which is
        the same class of lie this feature exists to remove.
        """
        try:
            preferences.set_bool(EJECT_PREFERENCE_KEY, request.enabled)
        except OSError as error:
            raise HTTPException(
                status_code=500,
                detail=(
                    f"The VRAM eject setting could not be saved, so it was not changed: {error}"
                ),
            ) from error
        ejector.enabled = request.enabled
        app.state.eject_source = "director"
        return vram_eject_state()

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
        #
        # The Song's recovery slots are taken off the stored song first, for the reason
        # `_adopt_song_recovery_slots` gives — and it has to happen ahead of this comparison,
        # or a client that predates the slots would send `None` for both, compare unequal, and
        # be told an ordinary save is a song replacement.
        _adopt_song_recovery_slots(project.song, current.song)
        if project.song != current.song:
            _require_song_replacement_confirmation(current, confirm_song_replacement)
            # Confirmed: this is a different song, so nothing kept for the old one comes with
            # it. Only reached once the gate above has let the replacement through, so a
            # refused save has cleared nothing.
            _detach_song_recovery_slots(project.song)
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
        # The two things the Director already has about a finished track, carried into the fields
        # that exist for them. Both optional: an import that sends neither behaves exactly as every
        # import did before they existed. `caption` rather than a new "style" field because both
        # generation paths already use `caption` for precisely this — the sonic and stylistic
        # direction of the song — and a second field meaning the same thing would need its own
        # answer to which one the Director's context should believe.
        lyrics: Annotated[str, Form()] = "",
        caption: Annotated[str, Form()] = "",
    ) -> Project:
        project = get_project(project_id)
        # Before `_copy_upload`: a refusal must not have written anything, or it is not a
        # refusal. (The write itself no longer overwrites — see the index prefix below.)
        _require_song_replacement_confirmation(project, confirm_song_replacement)
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in {".wav", ".mp3", ".flac"}:
            raise HTTPException(status_code=415, detail="Song must be WAV, MP3, or FLAC")
        # Ahead of the copy for the same reason the confirmation gate is: an oversized lyric sheet
        # must not leave a written file and a half-done import behind it.
        song_lyrics = _song_context(lyrics, SONG_LYRICS_LIMIT, SONG_LYRICS_FIELD)
        song_caption = _song_context(caption, SONG_CAPTION_LIMIT, SONG_CAPTION_FIELD)
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
            lyrics=song_lyrics,
            caption=song_caption,
        )
        return store.save(project)

    @app.put("/api/projects/{project_id}/song/context", response_model=Project)
    def replace_song_context(project_id: str, request: SongContextRequest) -> Project:
        """Set the lyric sheet and style description of the Song this project already has.

        Correcting after the fact, so a Director who imported yesterday is not made to re-import a
        finished master to say what it is. The Song's audio is the one thing this must not touch:
        `path`, `duration`, `source` and `prompt_id` are never assigned here, and the two fields
        are written onto the *stored* Song rather than a rebuilt one, so there is no construction
        site where a provenance field could be defaulted away.

        Both fields are assigned from the body, exactly as `PUT /documents` assigns its text: an
        omitted field is a blank one. That is what makes clearing a wrong lyric sheet possible at
        all, and the client sends both every time. It is also why nothing here is a Song
        *replacement* — the timing spine is untouched, so `_require_song_replacement_confirmation`
        has nothing to protect and asking for an acknowledgement would be theatre.

        Both values are computed before either is assigned, so a refusal over the second field
        cannot leave the first one applied.

        Each field keeps the one version this save displaced, and only when the save genuinely
        displaces something. A save whose text equals the stored text writes no slot: the single
        slot is the whole protection, and spending it on a no-op would overwrite the recoverable
        version with a copy of the live one — destroying the thing it exists to protect, on the
        most likely accidental path there is, a Director opening the editor and clicking save.

        The two fields are independent. Editing the lyric sheet moves the lyric slot and leaves
        the style description's alone, because they are two separate pieces of work and one save
        button is an implementation detail of the screen rather than a fact about the text.
        """
        project = get_project(project_id)
        if project.song is None:
            raise HTTPException(status_code=404, detail=SONG_CONTEXT_WITHOUT_SONG)
        submitted = {
            field: _song_context(
                getattr(request, field), SONG_CONTEXT_LIMITS[field], SONG_CONTEXT_FIELD_NAMES[field]
            )
            for field in SONG_CONTEXT_LABELS
        }
        for field, text in submitted.items():
            stored = getattr(project.song, field)
            # A no-op, and the one case where doing nothing is the whole feature. Note this
            # compares the *normalised* submission against stored text that was normalised the
            # same way on its own way in, so re-saving an untouched sheet is byte-equal here.
            if text == stored:
                continue
            setattr(project.song, f"{field}{RECOVERY_SLOT_SUFFIX}", stored)
            setattr(project.song, field, text)
        return store.save(project)

    @app.post(
        "/api/projects/{project_id}/song/context/{field}/restore", response_model=Project
    )
    def restore_song_context(project_id: str, field: SongContextField) -> Project:
        """Swap one song context field with the single version kept for it.

        A swap rather than a pop, matching the document restore exactly: the text being displaced
        becomes the kept version, so the restore is its own inverse and a mis-click costs nothing.
        The asymmetry would be the surprise — a Director who has used restore on the Treatment
        would reasonably expect the same click to behave the same way here.

        Nothing else about the Song is read or written. This route takes no body at all, so
        `path`, `duration`, `source` and `prompt_id` are not on the wire and cannot be defaulted
        away by it, which is the same guarantee the edit route makes.

        Nothing is appended to the chat thread, which is where this differs from the document
        restore deliberately. That thread is the audit trail of what the *Director* did to the two
        creative documents, and a Director reply can replace them without being asked; song
        context only ever changes when the human clicks save, so a system line about it would be
        the application narrating the human's own click back at them — and, since the thread is
        handed to the model on the next turn, doing so in the model's prompt.

        `None` in the slot means no save has ever displaced anything, and that refuses. `""` means
        a save displaced a blank, and that restores — a Director who pasted a sheet over an empty
        field has a real previous version, and telling them the blank is unrecoverable would be
        the conflation `Song`'s own docstring exists to avoid.

        An empty slot refuses with **409**, which is `restore_document`'s code for the identical
        question. It was 422 when this shipped, because the frozen matrix said so; the Director
        renegotiated it on 2026-08-18 rather than leave two restore routes answering "nothing was
        kept" with two different codes. Nothing about *which* states refuse moved with it — only
        the number — and a route test asserts the two restores stay equal, because the drift is
        what the change exists to close.
        """
        project = get_project(project_id)
        if project.song is None:
            raise HTTPException(status_code=404, detail=SONG_CONTEXT_WITHOUT_SONG)
        slot = f"{field}{RECOVERY_SLOT_SUFFIX}"
        previous = getattr(project.song, slot)
        if previous is None:
            raise HTTPException(
                status_code=409,
                detail=SONG_CONTEXT_RESTORE_REFUSAL.format(field=SONG_CONTEXT_LABELS[field]),
            )
        setattr(project.song, slot, getattr(project.song, field))
        setattr(project.song, field, previous)
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
        if source.kind not in MULTIVIEW_SUBJECTS or not source.path:
            raise HTTPException(status_code=422, detail=multiview_refusal())
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
            raise HTTPException(status_code=404, detail="Multiview source image was not found")
        upload_name = f"mvp_{project_id}_{source.id}{source_path.suffix.lower()}"
        content_type = "image/png" if source_path.suffix.lower() == ".png" else "image/jpeg"
        try:
            uploaded = await comfy.upload(upload_name, source_path.read_bytes(), content_type)
            image_name = "/".join(
                part for part in (uploaded.get("subfolder", ""), uploaded["name"]) if part
            )
            child = Asset(
                name=f"{source.name} · multiview",
                # The sheet is the same subject as what it was promoted from, so the child
                # carries the source's kind. For a character that is exactly what this line
                # said before — character in, character out — so no sheet already in a
                # manifest means anything different than it did. For a ship it is the whole
                # point: promotion must not be the step that files a prop as a person.
                #
                # Nothing downstream reads this for a decision that could change: the H3
                # reference adapter buckets every non-audio, non-video kind to "picture",
                # and shot attachment does not filter by kind at all.
                kind=source.kind,
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
        # What this shot *is*, asked of the shot rather than read off its attachments.
        #
        # This line replaced `if shot.asset_ids or shot.use_song_audio:`, and the replacement is
        # the whole story: that condition could only ever produce two answers, so a taxonomy of six
        # shot kinds had nowhere to live and a Director could not be wrong about a mode before
        # spending a render on it. `resolve_shot_mode` keeps the old condition as its *fallback*,
        # which is what makes every Shot saved before this change route exactly where it did — see
        # the byte-identical payload assertions in `tests/test_api.py`.
        mode = resolve_shot_mode(shot)
        spec = SHOT_MODE_SPECS[mode]
        # Before the payload and before ComfyUI, in that order and for the same reason every other
        # refusal on this route is: a mode with no adapter cannot be built, so the only question is
        # whether the Director finds out here or from a 502 after the submission.
        if not spec.adapter:
            raise HTTPException(
                status_code=422,
                detail=mode_without_adapter_refusal(shot_label(project, shot), mode),
            )
        # Then whether the shot fits the mode it declared. Unreachable for every Shot that existed
        # before this change: an undeclared Shot resolves to `references`, whose citation minimum
        # is zero and which is the one mode that takes the master song, so nothing it can be
        # carrying is a problem. It is reachable only from a declaration, which is exactly when
        # being wrong before the render is the point.
        if problems := mode_specification_problems(shot):
            raise HTTPException(
                status_code=422,
                detail=MODE_UNSPECIFIED_REFUSAL.format(
                    shot=shot_label(project, shot), problems=" ".join(problems)
                ),
            )
        if spec.adapter == "h3-reference":
            references: list[dict[str, Any]] = []
            tags: list[str] = []
            numbers = {"picture": 0, "video": 0, "audio": 0}
            # The reference-role citations in order, which the model guarantees is `asset_ids` in
            # order for every Shot that has ever been saved — see `Shot._reconcile_citations`.
            # Read from the citations rather than from the flat list because the citations are the
            # truth: a Shot whose wolf has been given the middle-frame role must stop sending it
            # as reference picture three, and `asset_ids` is the projection that stops naming it.
            for asset_id in [
                citation.asset_id for citation in citations_in_role(shot, "reference")
            ]:
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
                # The shot's own window, from the same two numbers the timeline draws it with
                # and the same two the text-only path already sends. Without this the loader
                # gets the whole file and H3 is conditioned on the opening of the track no
                # matter where the shot sits — the bug this reference render existed to expose
                # and could not, because every live run so far started at 0 s.
                #
                # Sent for *every* shot, 0 s included: the conditioner does not truncate a
                # reference audio, so a 0 s shot with no window rides the whole track through
                # every sampling step exactly like any other. See `song_audio_window`.
                try:
                    window = song_audio_window(
                        start=shot.start,
                        duration=shot.duration,
                        song_duration=project.song.duration,
                    )
                except ValueError as error:
                    # Before `comfy.submit`, so a window past the end of the song costs no GPU
                    # time. The alternative is the node's own `_slice_audio`, which clamps the
                    # end to the file length and renders a shorter window than asked for
                    # without saying so.
                    raise HTTPException(status_code=422, detail=str(error)) from error
                references.append(
                    {
                        "kind": "audio",
                        "file": str(resolve_song_path(project_id, project.song)),
                        "label": "master song",
                        "trim": window,
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
                    # All five `None` when the request omitted them, which the builder reads
                    # as "select the Director pipeline's own 0.6 MP / 16:9 / 32 frame". An
                    # explicit width and height are honoured exactly; supplying both kinds of
                    # geometry is refused there rather than resolved by precedence here.
                    width=request.width,
                    height=request.height,
                    megapixels=request.megapixels,
                    aspect_ratio=request.aspect_ratio,
                    multiple=request.multiple,
                    # `None` when the request omitted it, which the builder reads as
                    # "use the profile's own count" — see `H3Request.steps`.
                    steps=request.steps,
                    ref_image_size=request.ref_image_size,
                    profile=request.profile,
                    prefix=f"music-video-producer/{project_id}/shots/{shot.id}-h3-reference",
                )
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
        else:
            # `h3-director`, and nothing else can reach here: the adapter gate above refuses `""`,
            # and the import-time check beside `H3_ADAPTERS` refuses a table naming any third
            # adapter this route has no branch for.
            #
            # A sampling profile names a *reference*-graph configuration, and this branch
            # builds the text-only Director graph, which has no evidenced profile: it loads
            # a different checkpoint pair through `MiniMaxH3DirectorCS`, and the installed
            # generic H3 turbo LoRAs are not the `ref2v` one the turbo profile applies.
            #
            # Refused rather than ignored, which is the whole reason this is here. The
            # profile field is on the request model *both* branches bind, so a Director who
            # ticked turbo on a Shot with no references would otherwise get a 202 and a
            # full-price 20-step no-LoRA render — indistinguishable afterwards from a
            # default one, with nothing anywhere recording that the request was not
            # honoured. A GPU job logged under a configuration that was never applied is
            # worse than a refusal, because only the refusal is visible.
            if request.profile != H3_DEFAULT_PROFILE:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"The {request.profile!r} sampling profile applies to reference "
                        f"shots only. {shot_label(project, shot)} has no references, so it "
                        f"renders through the text-only Director graph, which has no "
                        f"evidenced profile. Attach a reference or drop the profile."
                    ),
                )
            # The same refusal, for the same reason, one field over. `ResolutionSelector` is
            # node `115` of the *reference* chain's export; this branch builds
            # `MiniMaxH3DirectorCS`, which sizes its own frame through `custom_width` /
            # `custom_height` / `divisible_by` / `resize_method`, and no frame from this graph
            # has been measured at 0.6 MP. Accepting the field and resolving it anyway would
            # queue a full-price render at a size this path has no evidence for, logged as
            # though it had been chosen — and only the refusal is visible afterwards.
            selector_fields = [
                name
                for name in ("megapixels", "aspect_ratio", "multiple")
                if getattr(request, name) is not None
            ]
            if selector_fields:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"{', '.join(selector_fields)} selects a frame the way the reference "
                        f"chain's ResolutionSelector does. {shot_label(project, shot)} has no "
                        f"references, so it renders through the text-only Director graph, "
                        f"which sizes its own frame and has no measured selection. Attach a "
                        f"reference, or give this shot an explicit width and height."
                    ),
                )
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
                    # This path's own default, unchanged from the one `H3Request` carried
                    # before the size became optional: an omitted frame here is still
                    # 1344x768. See `H3_DIRECTOR_DEFAULT_WIDTH` for why it did not move with
                    # the reference path's.
                    width=H3_DIRECTOR_DEFAULT_WIDTH if request.width is None else request.width,
                    height=(
                        H3_DIRECTOR_DEFAULT_HEIGHT if request.height is None else request.height
                    ),
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

    @app.post(
        "/api/projects/{project_id}/shots/{shot_id}/enhance/ltx25",
        response_model=RenderJob,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def enhance_with_ltx25(project_id: str, shot_id: str) -> RenderJob:
        """Submit one Shot's existing take to the standalone LTX 2.5 enhancer. No body.

        The gap this closes: LTX 2.5 was reachable only by regenerating H3 from scratch inside
        the reference chain, so improving a shot the Director liked cost another full H3 pass
        and produced a different picture. Here the take is the *input*.

        **Nothing on this path re-runs H3.** The payload has no MiniMax node in it at all — see
        `build_ltx25_enhance_payload`, which the audited export is checked against node by node.

        **Nothing here writes to the Shot.** Not `status`, not `latest_output`, not
        `latest_review`, not `prompt_id`. Only a `RenderJob` is appended. Three consequences,
        and the third is the one that stops the first two from being read as more than they are:

        * the enhanced video is written under `ENHANCE_PREFIX_SUFFIX`, a different filename
          prefix from any render's, so ComfyUI numbers it in its own series and it lands beside
          the take rather than over it or in the middle of it;
        * `read_job` has no branch for `kind="ltx"`, so a *completed* enhancement moves no
          pointer either. The shot goes on naming the take that was enhanced, and the enhanced
          file is reachable through `RenderJob.output_files` on the job that produced it;
        * deciding which of the two is the take is take comparison, which this application does
          not do. None of the above is a take list.

        No body, for `render_again`'s reason and more strongly: this route has no controls at
        all. The export fixes the sigmas, the detailer strength and the prompt, exposing any of
        them is marked Ask First and has not been asked, and a request model with nothing in it
        is a place for a future field to arrive without a decision.

        No readiness gate, and that is deliberate rather than an omission. `generate_h3` refuses
        an unprompted Shot because a prompt is what its graph turns into a picture; this graph's
        prompt is **empty**, so a Shot with no prompt enhances exactly as well as one with a
        prompt. Borrowing that gate here would refuse a real take for a field the work does not
        read. What must exist is the take, which is what the two refusals below check.

        Frame count is not claimed, here or anywhere on this path. The LTX boundary in the
        reference chain measurably did not preserve it (192 in, 185 out), this graph tiles
        temporally, and what it does is a measurement to be taken with `ffprobe` on the output —
        not a number this route can promise.
        """
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        # First, ahead of everything, for `mark_ready_refusal`'s reason: an in-flight Shot is the
        # one state where getting this wrong does concrete harm. 409 rather than 422 and for the
        # same reason it is 409 there — a live job is a state conflict, and the same request
        # succeeds once it lands.
        if shot_render_in_flight(project, shot) or shot_enhancement_in_flight(project, shot):
            raise HTTPException(
                status_code=409,
                detail=ENHANCE_IN_FLIGHT_REFUSAL.format(shot=shot_label(project, shot)),
            )
        # Before any path is resolved: a Shot that never rendered has no take to name, and the
        # refusal for that is a different sentence from the one for a take whose file is gone.
        if not shot.latest_output:
            raise HTTPException(
                status_code=422,
                detail=ENHANCE_NO_TAKE_REFUSAL.format(shot=shot_label(project, shot)),
            )
        # `analyze_latest_take`'s resolution, containment check included, so a `latest_output`
        # carrying `..` cannot reach outside ComfyUI's output directory and hand an arbitrary
        # file to the node. The status differs from that route's 404 on purpose: the matrix
        # specifies 422 here, and it is the right code — the request names a Shot that exists,
        # and what cannot be processed is the state its manifest describes.
        output_root = (settings.comfy_root / "output").resolve()
        source = (output_root / Path(shot.latest_output)).resolve()
        if output_root not in source.parents or not source.is_file():
            raise HTTPException(
                status_code=422,
                detail=ENHANCE_MISSING_TAKE_REFUSAL.format(
                    shot=shot_label(project, shot), path=shot.latest_output
                ),
            )
        try:
            payload = build_ltx25_enhance_payload(
                # Forward slashes on Windows too: the value is a plain string to VHS, which
                # opens it with `os.path`, and a backslash path survives the JSON round-trip
                # doubled and unreadable in every log and error message on the way.
                source_video=source.as_posix(),
                prefix=(
                    f"music-video-producer/{project_id}/shots/{shot.id}{ENHANCE_PREFIX_SUFFIX}"
                ),
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        try:
            submission = await comfy.submit(payload)
        except ComfyError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        # The whole write. The Shot itself is untouched: see this route's docstring.
        job = RenderJob(
            kind="ltx",
            prompt_id=submission.prompt_id,
            target_id=shot.id,
            # The seed the graph fixes, recorded so the job says what was sampled rather than
            # defaulting to a 0 that happens to match.
            seed=LTX25_ENHANCE_SEED,
        )
        project.jobs.append(job)
        store.save(project)
        return job

    @app.post(
        "/api/projects/{project_id}/shots/{shot_id}/render-again", response_model=Project
    )
    def render_again(project_id: str, shot_id: str) -> Project:
        """Re-open one settled Shot so it can be submitted once more. Its own action, no body.

        Comparing two takes of a shot is an ordinary creative act, and until this existed the only
        way to do it was to walk `status` back to `ready` by hand through `PUT /shots` with an API
        client — which is what was done on 2026-08-18 to compare two sampling profiles.

        That route is the generic full-project-shaped write, and using it for this is the hazard
        this codebase keeps rediscovering: it takes the whole Shot list from the client, so a
        request whose only intent was "let me render this again" also carries, and therefore can
        silently overwrite, every prompt, window, reference and lock in the plan. This route takes
        **no body at all**. There is nothing on the wire for a stale client to reassert, and the
        only field it writes is `status`. A route test pins that: everything else in the manifest
        compares byte-identical across the call.

        Not a bypass of the readiness gate. The gate refuses unprompted shots so they do not spend
        a GPU pass returning noise, and that question is asked here from scratch — see
        `render_again_refusal`. A shot that rendered successfully and then had its prompt deleted
        is refused exactly as a first render would refuse it.

        What happens to the take already there is stated rather than implied, in
        `RENDER_AGAIN_PREVIOUS_TAKE`: nothing, until the new one lands, and then only the single
        `latest_output` pointer moves. Take management — keeping, naming or comparing several
        outputs per shot — is deliberately not this story and is not started here.

        The response is the whole project, as every other purpose-built action returns, so the
        client redraws the timeline, the inspector and the queue button from one reply.
        """
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        # First, and ahead of the settled check, because an in-flight Shot is neither settled nor
        # untouched: it is the one state where a second submission does concrete harm. It is also
        # the state a hand-walked-back status hides — `ready` with a live job is exactly the race —
        # so this must not be reachable only through the statuses below.
        if shot_render_in_flight(project, shot):
            raise HTTPException(
                status_code=409,
                detail=RENDER_AGAIN_IN_FLIGHT_REFUSAL.format(shot=shot_label(project, shot)),
            )
        # A Shot that has never rendered has nothing to render *again*, so this does nothing to it
        # and says so by changing nothing. Not an error: `draft` is what every Shot the interface
        # creates is, and a 422 here would turn the commonest state in the application into a
        # failure. It is also why this precedes the refusals — the placeholder prompt every new
        # Shot carries would otherwise refuse a shot that was never being re-opened.
        if shot.status not in RENDER_AGAIN_STATUSES:
            return project
        refusal = render_again_refusal(project, shot)
        if refusal:
            raise HTTPException(status_code=refusal[0], detail=refusal[1])
        # The whole write. `latest_output`, `latest_review`, `prompt_id` and the job history are
        # left exactly as they are: the previous take is still this Shot's take until a new one
        # actually lands, and re-opening a Shot the Director then thinks better of must cost them
        # nothing.
        shot.status = "ready"
        return store.save(project)

    def _set_shot_commitment(project_id: str, shot_id: str, target: ShotStatus) -> Project:
        """Move one Shot between `draft` and `ready`. The whole of both routes below.

        Its own action rather than the shots write, for `render_again`'s reason and it is the
        stronger case here because this is the *common* path rather than a repair: `PUT /shots`
        takes the whole Shot list from the client, so a request whose only intent was "I have
        decided to render this one" also carries — and can therefore silently overwrite — every
        prompt, window, reference and lock in the plan, from however long ago the client loaded
        them. These routes take **no body at all**. There is nothing on the wire for a stale client
        to reassert, and the only field written is `status`. A route test pins that: everything
        else in the manifest compares byte-identical across the call.

        The no-op comes *after* the refusals, unlike `render_again`'s, and the difference is not an
        oversight. `render_again` checks its no-op first because `draft` carrying the `"New shot"`
        placeholder is the commonest state in the application and refusing it for its prompt would
        both fail the wrong test and turn that state into an error. Here there is no such case:
        every refusal below is a fact the Director needs whether or not the field already holds the
        value they asked for. A `ready` Shot whose prompt has since been emptied says so rather
        than answering a shrug, which is the only way they learn it before the render refuses it.

        Not a certificate. Reaching `ready` is a decision the Director made about this Shot at this
        moment; it is not a fact about the prompt, and `generate_h3` asks the prompt question again
        from scratch when the shot is actually submitted. Nothing here is remembered by that gate.

        The response is the whole project, as every other purpose-built action returns, so the
        client redraws the timeline, the inspector and the queue button from one reply.
        """
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        refusal = mark_ready_refusal(project, shot, target=target)
        if refusal:
            raise HTTPException(status_code=refusal[0], detail=refusal[1])
        # Nothing to change, so nothing is written and nothing is saved: an unchanged manifest must
        # not get a fresh `updated_at`, or a request that did nothing would collide with the next
        # `PUT /projects` optimistic-concurrency check for no reason.
        if shot.status == target:
            return project
        # The whole write. Nothing else about the Shot is this action's business — a Director who
        # arms a shot and then thinks better of it must get back exactly what they had.
        shot.status = target
        return store.save(project)

    @app.post("/api/projects/{project_id}/shots/{shot_id}/mark-ready", response_model=Project)
    def mark_shot_ready(project_id: str, shot_id: str) -> Project:
        """Commit one drafted Shot to the render queue. Its own action, no body.

        The missing first step of the primary journey. `Shot.status` defaults to `"draft"`, the
        queue button submits only what reads `"ready"`, and until this existed nothing in the
        shipped interface ever wrote that — so the render pipeline was reachable only by an API
        client, which is how every live render in this project had actually been driven.

        Never automatic, and that is a rule rather than an omission. Applying a Director shot plan
        does not do it, expansion writing prompts does not do it, and no other write may: a shot
        becoming submittable is a decision someone made, and a Director who ran expansion to see
        what the model suggested must not discover they had armed a whole plan for rendering.
        """
        return _set_shot_commitment(project_id, shot_id, "ready")

    @app.post("/api/projects/{project_id}/shots/{shot_id}/mark-draft", response_model=Project)
    def mark_shot_draft(project_id: str, shot_id: str) -> Project:
        """Take one committed Shot back out of the render queue. Its own action, no body.

        How a Director un-commits, and it has to be as cheap as committing was or the commitment
        stops being one: a decision nobody can walk back is a decision people avoid making. No
        prompt gate on this direction — `draft` is the un-armed state, so refusing to disarm a
        Shot whose prompt was emptied would trap it armed, which is exactly backwards.
        """
        return _set_shot_commitment(project_id, shot_id, "draft")

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
