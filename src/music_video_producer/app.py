from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import math
import re
import shutil
import subprocess
import tempfile
import threading
from collections import Counter
from collections.abc import AsyncIterator, Callable, Container, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, BeforeValidator, Field, StringConstraints

from .assembly import (
    ASSEMBLY_FPS,
    BOUNDARY_TOLERANCE_SECONDS,
    DEFAULT_EXPORT_PRESET,
    EXPORT_PRESETS,
    PREVIEW_PRESET,
    AssemblyGeometryError,
    AssemblyPlan,
    AudioOverlay,
    ClipWindow,
    ExportProgress,
    TransitionChoice,
    TransitionClip,
    assembly_plan,
    assembly_refusals,
    clip_frames_on_grid,
    concat_args,
    concat_manifest,
    parse_progress_us,
    probe_duration_args,
    probe_streams_args,
    probe_take_args,
    take_cut_refusal,
    transition_segment_args,
    trim_args,
    verification_problems,
    with_progress,
)
from .asset_replacement import ReplacementChange
from .audio import FfmpegMissing, analyze_song
from .batch import (
    JOB_NEVER_SUBMITTED,
    NOTE_KIND_PROMPT,
    PENDING_SUBMISSION_PROMPT_ID,
    SHOT_AFTER_FAILED_RENDER,
    TERMINAL_JOB_STATUSES,
    ReadinessReport,
    accept_submission,
    batch_targets,
    prompt_is_missing,
    prompt_rejection,
    readiness_refusal,
    readiness_report,
    reconcilable_jobs,
    shot_label,
    stamp_job_settled,
    supersede_target_jobs,
)
from .comfy import ComfyClient, ComfyError, ComfyProgressListener, ProgressTracker
from .config import Settings
from .director import (
    PLAN_TEMPERATURE,
    DirectorBudgetExhausted,
    DirectorClient,
    DirectorError,
    # Imported for `DIRECTOR_REPLACEABLE_DOCUMENTS`, which asks this model which documents a
    # reply can carry rather than keeping a second list of them.
    DirectorResult,
    DirectorUnavailable,
    director_result_schema,
    document_rejection,
)
from .effects import (
    BINDING_SETTINGS,
    DRIVE_MODES,
    DRIVE_ONLY_SETTINGS,
    EFFECT_CATALOGUE,
    EFFECT_LUT_UNKNOWN_REFUSAL,
    FAMILY_ORDER,
    NOT_A_NUMBER,
    TRANSITION_CATALOGUE,
    TRANSITION_PAIR_ONLY_OPENING_REFUSAL,
    TRANSITION_PAIR_ONLY_REFUSAL,
    ChoiceParameter,
    DriveScript,
    EffectRefusal,
    EffectStages,
    LutEntry,
    LutParameter,
    NumberParameter,
    ParameterBinding,
    agreed_bindings,
    build_effect_stages,
    discover_luts,
    exported_bindings,
    exported_look,
    fingerprint_size,
    one_sided_transition_stages,
    opening_transition_stages,
    preview_fingerprint,
    song_fingerprint,
    song_fingerprints_match,
    transition_definition,
    validate_stack,
)
from .h3_expansion_prompt import system_prompt as h3_system_prompt
from .h3_prompt import check as h3_check
from .h3_prompt import check_reference_bounds, normalize_audio_fields
from .models import (
    ASSET_ROLE_LABELS,
    CHARACTER_SLOT_LIMIT,
    NO_EVIDENCED_BUNDLE,
    NOTICE_RAW_LIMIT,
    SHOT_MODE_SPECS,
    Asset,
    AssetCitation,
    EffectSpec,
    ExportLook,
    MessageNotice,
    Project,
    RenderJob,
    SamplingBundle,
    SamplingProfile,
    Shot,
    ShotStatus,
    SingingState,
    Song,
    SongAnalysis,
    SongSection,
    TransitionSpec,
    TreatmentMessage,
    VocalType,
    assets_for_proposal,
    citable_assets,
    citations_in_role,
    default_setting_asset,
    identity_sheet_ids,
    mode_specification_problems,
    new_id,
    numbered_references,
    prefer_identity_sheets,
    reference_slot_totals,
    resolve_shot_mode,
    song_audio_tag,
    vocal_cast_problems,
    with_default_setting,
)
from .preferences import EJECT_PREFERENCE_KEY, MachinePreferences
from .prompt_cleanup import (
    PROMPT_CLEANUP_SYSTEM_PROMPT,
    citation_fingerprint,
    echoed_labels,
    prompt_cleanup_input,
    rewrite_rejection,
    window_fingerprint,
)

# Moved out of this module, and re-exported by importing them here: `batch.readiness_report` has
# to ask the same "is this stale" question the submit route asks, and `app` imports `batch`, so
# the answer cannot live here. See `reference_map.py`'s docstring. Every existing spelling
# (`app.stale_reference_map`, `app.reference_map_tag_lines`, …) still resolves.
from .reference_map import (
    REFERENCE_MAP_ROLE_TAGS,
    STALE_REFERENCE_MAP_CAUSE,
    STALE_REFERENCE_MAP_CONSEQUENCE,
    STALE_REFERENCE_MAP_REMEDY,
    reference_map_sentence,
    reference_map_tag_lines,
    song_audio_prose_expansion,
    stale_reference_map,
)
from .routes.context import RouterContext
from .store import ProjectChangedDuringSave, ProjectNotFound, ProjectStore
from .timeline import (
    H3_FPS,
    H3_MAX_SHOT_SECONDS,
    H3_MIN_SHOT_SECONDS,
    MIN_SINGING_VOCAL_SECONDS,
    POPULATE_VARIANCE_DEFAULT,
    POPULATE_VARIANCE_MAX,
    SNAP_TOLERANCE_DEFAULT,
    SNAP_TOLERANCE_MAX,
    SNAP_UNMEASURED,
    SNAP_WITHOUT_CUTS,
    CutMove,
    CutSkip,
    LyricLineSpan,
    SnapWindow,
    TimelineError,
    align_lyric_lines,
    anchored_label,
    build_director_timeline,
    layout_spans,
    ordered_shots,
    over_render_frames,
    over_render_lead,
    over_render_window,
    populate_windows,
    proposal_for_position,
    repair_sections,
    section_edges,
    section_looks_input,
    shot_expansion_input,
    shot_snap_windows,
    shot_vocal_overlap,
    snap_window_plan,
    song_section,
    vocal_density,
)
from .transcription import transcribe_song_words
from .vram import CliUnloader, LlmEjector
from .workflows import (
    H3_DEFAULT_PROFILE,
    H3_DIRECTOR_DEFAULT_HEIGHT,
    H3_DIRECTOR_DEFAULT_WIDTH,
    H3_REFERENCE_LIMITS,
    SONGPLANNER_DEFAULT_DURATION_HEADROOM,
    SONGPLANNER_MAX_DURATION_HEADROOM,
    WorkflowCatalog,
    build_h3_director_payload,
    build_h3_keyframe_payload,
    build_h3_reference_payload,
    resolved_h3_sampling,
    song_audio_window,
)

logger = logging.getLogger(__name__)

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


# The creative documents that carry the document apparatus — a lock, a single recovery slot,
# a restore route and a name to call them on screen — keyed by field name. One mapping, and
# everything about the apparatus is derived from it: the field names the guard loops reach by
# interpolation, the slots kept out of the model's context, the restore route's path segment,
# and the labels used on screen. Adding a fourth document must not require finding four other
# places, because the one that gets missed silently leaks a document's kept copy back into
# every prompt. `api.js`'s DOCUMENT_LABELS is the frontend half; tests assert both sides, the
# `DocumentName` literal, and `Project`'s actual fields all agree.
#
# **This said "the creative documents a Director reply can replace" until 2026-09-03, and it
# was one mapping doing two jobs.** Those two questions — *has the apparatus* and *may an
# ordinary reply rewrite it* — had the same answer while there were two documents, so nothing
# distinguished them. The Brief is the case that separates them: it has the apparatus and no
# reply may write it, because `DirectorResult` carries no field for it and (Director ruling,
# 2026-09-03) will not. `DIRECTOR_REPLACEABLE_DOCUMENTS` below is the second question, and
# every derived site now reads whichever of the two answers the question it is asking.
DOCUMENT_LABELS = {
    "creative_brief": "Creative brief",
    "treatment": "Treatment",
    "style_bible": "Style bible",
}
DocumentName = Literal["creative_brief", "treatment", "style_bible"]

#: The subset an *ordinary Director reply* may rewrite — the second job the mapping above used
#: to do — and it is **derived, not transcribed**: a reply is a `DirectorResult`, so the
#: documents it can replace are exactly the ones that model carries text for. The chat route's
#: apply loop and `api.js`'s `documentChangeToast` read this; nothing about the lock, the slot,
#: the restore route or the labels does.
#:
#: Deriving it rather than listing it is what makes the two mappings unable to disagree: adding
#: a document to `DOCUMENT_LABELS` gives it the apparatus, and whether a reply may write it is
#: then answered by `DirectorResult` itself rather than by a second list somebody has to
#: remember. Suggest Video (TP-3) will write the Brief and is deliberately *not* a member: it is
#: its own long pass with its own route, not a turn of chat, and it goes through
#: `document_lock_refusal` like any other machine write.
DIRECTOR_REPLACEABLE_DOCUMENTS = {
    field: label
    for field, label in DOCUMENT_LABELS.items()
    if field in DirectorResult.model_fields
}

#: The documents whose recovery slot is filled by the Director's **own save**, and the other
#: half of the same partition — derived from the line above rather than listed beside it.
#:
#: The rule is *whichever writer is the threat fills the slot*, and it follows from the split:
#: a document an ordinary reply can replace captures on apply, and capturing on the human's save
#: as well would let one click of Save spend the single slot that exists to protect them from the
#: model. A document no reply can replace has no such writer, so its own save is the
#: displacement — and if it did not capture there, its slot would never fill at all and the
#: restore button beside it would be furniture.
#:
#: This is `routes/song.replace_song_context`'s shape and its threat model, which is the
#: Director's ruling of 2026-09-03: what destroys a Brief is a save landing over pasted text.
#: A byte-equal re-save captures nothing — the one case where doing nothing is the whole
#: feature, in that route's own words — and `replace_documents` is where that is enforced.
SAVE_CAPTURED_DOCUMENTS = tuple(
    field for field in DOCUMENT_LABELS if field not in DIRECTOR_REPLACEABLE_DOCUMENTS
)

#: The two phrases every recovery sentence has to fill in differently depending on which writer
#: fills the document's slot, keyed by document and derived from the partition above.
#:
#: One phrase table rather than two sets of sentences, so the restore confirmation, the one-way
#: variant, the empty-slot refusal and both of the browser's tooltips stay *one* sentence each.
#: Getting this wrong is not cosmetic: the refusal used to say a version is only kept "when a
#: Director reply actually replaces the document", and for the Brief that sentence names a writer
#: that does not exist and never will — a Director reading it would conclude the Brief is not
#: protected and stop looking for the button that protects it.
#:
#: `api.js` derives the same two phrases from its own copy of the partition, and a contract test
#: executes both sides. For `treatment` and `style_bible` every sentence below is byte-identical
#: to what it was before the Brief existed, which is the point.
DOCUMENT_SLOT_DISPLACEMENT = {
    field: ("applied replacement" if field in DIRECTOR_REPLACEABLE_DOCUMENTS else "save that changed it")
    for field in DOCUMENT_LABELS
}
DOCUMENT_SLOT_CAPTURE = {
    field: (
        "a Director reply actually replaces the document"
        if field in DIRECTOR_REPLACEABLE_DOCUMENTS
        else "a save changes the document's text"
    )
    for field in DOCUMENT_LABELS
}

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
H3_ADAPTERS = frozenset({"h3-director", "h3-reference", "h3-keyframe"})

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
) | frozenset(
    # Measured voice activity: second pairs, and every transcribed word. Withheld as raw
    # data: what planning needs from either is per-window facts ("this window is
    # instrumental"), which the code derives via `shot_vocal_overlap` — pages of floats
    # in the prompt are noise the model would misread long before they helped.
    {"vocal_spans", "lyric_words"}
) | frozenset(
    # The declared vocal type, withheld **in pass 1 only**, and on the never-been-in grounds
    # `SHOT_DIRECTOR_WITHHELD` establishes: this field has never been in the dump, so classifying
    # it withheld adds nothing to the prompt rather than subtracting something from it.
    #
    # It is withheld because nothing has yet been designed for the model to *do* with it. The
    # Director's purpose for the field is explicit — "so that the LLM system can account for all
    # that" — and that is pass 2's whole job: the vocal type and the sheet's per-line marks enter
    # the populate instruction together, with wording that says what a tagged line means for a
    # shot's references. Shipping a bare `"vocal_type": "unstated"` key into every chat turn and
    # every populate call *now* would change what every existing project's model sees, in exchange
    # for a fact the instruction never mentions and the model has no use for. That is a prompt
    # regression bought with nothing, and it would put the live project's populate out of
    # byte-identity with the plan the Director already has. Deleting this one entry is pass 2's
    # first line.
    #
    # The per-line marks need no entry here at all, and that is the storage decision paying off:
    # they are characters inside `lyrics`, which is already classified visible, so they travel
    # exactly as the sheet travels and there is no second field to classify, withhold, or forget.
    {"vocal_type"}
) | frozenset(
    # The Song Envelope's pointer. Withheld on the `vocal_spans`/`lyric_words` grounds, which
    # this field shares exactly: it is a *measurement*, and the model has no use for a sidecar
    # path, a sample rate or a content hash. The one part of it a planning turn could ever want
    # is the tempo — and a tempo is a fact about the whole song that belongs in an instruction
    # written for it, phrased as the estimate it is, not smuggled in as a bare `"bpm": 128.3`
    # beside a file path. Adding the estimate to the prompt is a deliberate prompt change with
    # its own wording, and this story is not it.
    #
    # It is also never-been-in: classifying it withheld leaves every existing project's prompt
    # byte-identical, which is the bar `SHOT_DIRECTOR_WITHHELD` sets for a newly declared field.
    {"analysis"}
)

# Every field an `Asset` carries, classified exactly as `Song` and `Shot` are, and for the reason
# `_withheld_fields` exists: the Director's context dumps `assets` whole, so until now every field
# ever added to `Asset` entered the model's prompt the moment it was declared, with nobody deciding
# that it should. `consistency_prompt` went in that way. This is the same guard applied to the
# third and last model in the dump.
#
# **Everything that was in the dump yesterday is classified visible**, so this changes not one
# character of what any model is prompted with. Taking an existing field out is Ask First, exactly
# as the Shot comment rules, and this is not the story for it.
ASSET_DIRECTOR_VISIBLE = frozenset(
    {
        "id",
        "name",
        "kind",
        "path",
        "source",
        "parent_id",
        "prompt",
        "prompt_id",
        "consistency_prompt",
        "vision",
        "created_at",
    }
)
#: `character_slot` is withheld, and it is the first thing ever withheld from an Asset.
#:
#: Never-been-in grounds, and pass 1's own: the slot exists to resolve a `(S1)` mark in the lyric
#: sheet to a character reference, and *that resolution is pass 2*. A model shown "this asset is
#: S1" with no instruction that mentions singers has been handed a number it can only misuse — the
#: recorded failure mode of this model family being to write internal labels into creative prose.
#: It also keeps the field where the Director put it: nothing infers a slot, and a field no tool
#: schema and no context dump carries is a field no model can be blamed for.
ASSET_DIRECTOR_WITHHELD = frozenset({"character_slot"})


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
#: `h3_prompt` is withheld, and this is the first thing ever withheld from a Shot.
#:
#: Not a removal. The comment above rules that taking an existing field *out* of the dump is Ask
#: First, and that still holds — but this field has never been in it, so classifying it withheld
#: adds nothing to the prompt rather than subtracting something from it.
#:
#: Withheld on the numbers. An expansion is the long form: MiniMax's own worked examples run well
#: past a thousand characters, and a thirty-shot plan would add tens of thousands of characters —
#: many thousands of tokens — to *every* chat turn. The recorded root cause of Director degradation
#: in this project is rich context, so shipping the machine-facing form into the conversational
#: model's prompt would be the largest single context regression here, in exchange for nothing: the
#: chat Director writes treatments and intents, and the expansion specialist gets its own
#: purpose-built payload rather than this dump.
#: The AD-13 window snapshot is withheld on the same never-been-in grounds, plus its own: the
#: two fields are copies of `start`/`duration` taken at approval, staleness bookkeeping for
#: assembly's refusal. The chat Director already sees the live window and `approved_output`;
#: echoing near-duplicate numbers into every shot of every turn buys nothing, and no
#: assistant tool writes them — approval is never the Director's act (AD-15).
#:
#: The over-render pair is withheld for the same class of reason: `latest_take_lead` is
#: render bookkeeping written at submission, and `trim_nudge` is the human's own editorial
#: fine-tune on a rendered file — neither is a plan fact a chat turn writes or reads, and
#: nothing the conversational model could do with them is anything but noise.
#: `latest_take_start`/`latest_take_duration` are withheld on the AD-13 pair's own grounds,
#: which they are the take-side twin of: copies of `start`/`duration` taken at submission so
#: `restore_song_audio` can tell a take's window from the live one, near-duplicate numbers a
#: chat turn has no decision to make from and no route lets it write.
#: `mix_take_audio` is the same class again: the human's acceptance of a rendered file's
#: audio into the mix, decided by ear, never by the chat model. `flagged` likewise: the
#: Director's own re-render mark (AD-5), decided by eye on a take, resubmitted by a
#: button — nothing a chat turn writes or reads.
SHOT_DIRECTOR_WITHHELD: frozenset[str] = frozenset(
    {
        "h3_prompt",
        # Withheld with the expansion it describes, and for the same two reasons: it has never
        # been in the dump, and it is bookkeeping about the machine-facing text rather than a
        # plan fact a chat turn writes or reads. No tool schema exposes it either — the only
        # writers are the expansion paths and `refresh_reference_maps`.
        "h3_prompt_map",
        "approved_start",
        "approved_duration",
        "latest_take_lead",
        "latest_take_start",
        "latest_take_duration",
        "trim_nudge",
        "mix_take_audio",
        "flagged",
        # The Effect Stack, withheld on the never-been-in grounds this set was opened with, plus
        # its own: it is *filter configuration* — catalogue ids, bounded numbers and a LUT id
        # resolved server-side — and the Director's chat is about story. A `[{"effect":
        # "punch_in", "parameters": {"zoom": 1.12}}]` in every shot of every turn is a mapping the
        # conversational model has nothing to do with and every reason to imitate: the recorded
        # degradation mode here is JSON in the context begetting JSON in the reply, and this is
        # the largest structured blob a Shot has ever carried.
        #
        # It is also unreachable by the model in either direction, which is what makes withholding
        # it cost nothing. No tool schema declares it, the two automated writers (`fill_shots` and
        # expansion) write prompts and plan facts, and the one route that writes a stack validates
        # it against the catalogue before storing a byte. A look is the Director's eye on a take,
        # made in the Effects tab against a preview; a chat turn has no way to see what it did.
        "effects",
        # The Transition pair, withheld on the never-been-in grounds this set was opened with,
        # plus `effects`' own: a transition is *filter configuration* -- one catalogue id that
        # resolves to an `xfade` name -- and the Director's chat is about story. It is also
        # unreachable by the model in either direction, which is what makes withholding it cost
        # nothing: no tool schema declares either field, neither automated writer touches them,
        # and the one route that writes them validates against the catalogue first.
        #
        # There is a second reason particular to these two, and it is the stronger one. A
        # transition is a fact about the **boundary between two Shots**, and the Director's dump
        # hands the model one Shot at a time. `"transition_out": {"type": "wipe_left"}` inside a
        # shot object says nothing about which shot it wipes into, so the only thing a model
        # could learn from it is that a key exists -- and the recorded degradation mode of this
        # model family is JSON in the context begetting JSON in the reply.
        "transition_in",
        "transition_out",
    }
)

#: A keyframe role names a concrete frame, and a frame is a picture. Same refusal the keyframe
#: branch makes for the same reason: the splitter routes media by kind, and an audio or video
#: cited as a frame would be fed to a loader under a kind it is not, which nothing downstream
#: reports.
REFERENCE_KEYFRAME_NOT_IMAGE = (
    "A {role} must be an image, and {name} is {article} {kind}."
)


#: The measured lipsync clause (2026-08-19, the night's decisive experiment): the two
#: takes the Director's own ear rated "really good" both carried this exact sentence
#: shape, and re-rendered on the A/B shot it measured 0.94 envelope correlation with
#: visible articulation — while every H3-document variant measured ≤0.43. The wording is
#: lifted from the praised take verbatim, not composed.
SONG_AUDIO_SINGS_CLAUSE = "The character from the reference sheet sings to camera."
SONG_AUDIO_SINGS_CLAUSE_BARE = "The performer sings to camera."
#: The anti-morph anchor, from the same two good takes the sings clause came from — both
#: ended "stable face and wardrobe, one continuous take", and the batch prose that
#: dropped it produced the live artifact (2026-08-19, run 2 shot 07): a sparse
#: establishing shot ran out of described action and CUT TO THE CHARACTER SHEET ITSELF,
#: three poses on white, for its final two seconds. "One continuous take" is what tells
#: the sampler there is no cut to invent.
SONG_AUDIO_CONTINUITY_CLAUSE = "Stable face and wardrobe, one continuous take."


def reference_prompt(
    shot: Shot,
    tags: list[str],
    section_prompt: str = "",
    vocal_overlap: float | None = None,
) -> str:
    """What the reference render actually submits for this Shot.

    Without an expansion this is byte-for-byte the string this route has always built:
    the reference map, then the Shot's intent — plus, for a singing song-audio shot whose
    intent never says so, the measured sings-to-camera clause (see the constant above).

    ``vocal_overlap`` is the window's measured voice from `shot_vocal_overlap`, and it
    outranks the singing mark: a shot marked singing over a window the track is measured
    to leave instrumental gets NO sings clause, because H3 told to sing over a voiceless
    reference invents its own words and lipsyncs to them (live, 2026-08-19 — the intro
    and all four outro shots). ``None`` means unmeasured and changes nothing.

    With an expansion, it is submitted **alone**: for song-audio shots the stored text is
    already the whole preamble-prose string (`song_audio_prose` built it), and for the
    document modes prefixing prose would break the required format.
    """
    if shot.h3_prompt.strip():
        return shot.h3_prompt
    base = f"{reference_map_sentence(tags)} {shot.prompt}"
    if (
        shot.use_song_audio
        and shot.singing == "singing"
        # Word boundary, not substring: "closing image" is not singing language, and the
        # substring form silently denied the clause to any singing intent containing
        # "closing"/"using" (found live, 2026-08-19).
        and not re.search(r"\bsing", shot.prompt.lower())
        and not (vocal_overlap is not None and vocal_overlap < MIN_SINGING_VOCAL_SECONDS)
    ):
        clause = SONG_AUDIO_SINGS_CLAUSE if shot.citations else SONG_AUDIO_SINGS_CLAUSE_BARE
        base = f"{base} {clause}"
    if shot.use_song_audio:
        # Every song-audio shot, singing or not: the live sheet-morph hit a NOT-singing
        # establishing shot. See the constant for the artifact this anchors against.
        base = f"{base} {SONG_AUDIO_CONTINUITY_CLAUSE}"
    # The fallback's insurance (run-2 audit item 7): the section's shared look reaches H3
    # only through the expansion, so a shot whose every expansion attempt failed would
    # render from one bare intent sentence with no section character. Appended, never
    # prefixed, and only when a section prompt exists — a sectionless shot's string is
    # byte-identical to what this route has always built, which the pinned payload
    # digests assert.
    if section_prompt:
        return f"{base} Section look: {section_prompt}"
    return base


#: What one shot's reference-bounds refusal says. The problems are the checker's own sentences —
#: one wording for the rule, in `h3_prompt.check_reference_bounds`, rather than a second copy here
#: that can drift from the one the expansion retry loop feeds back to the model.
REFERENCE_BOUNDS_REFUSAL = (
    "Not submitted: {shot} cites a reference slot it does not have. {problems} Nothing was sent "
    "to ComfyUI, because a render conditioned on a slot nothing fills comes back plausible and "
    "wrong rather than failing. Attach the media or renumber the tag, then submit again."
)

#: What a stale reference map's refusal says. The invariant this route exists to keep after
#: 2026-08-20: **no stale reference map ever reaches ComfyUI.** `refresh_reference_maps` re-derives
#: every map it can for free, so what reaches this sentence is one of the three it deliberately
#: cannot — a document-mode expansion, which would cost an unrequested model call; a locked shot;
#: or one with a render in flight.
#:
#: **Composed from the shared clauses, not written out here.** `batch.SHOT_WITH_STALE_REFERENCE_MAP`
#: says the same thing in the pre-flight — the report that now names this shot *before* the batch
#: spends GPU time on the rest — and one problem must not reach the Director as two explanations.
#: The cause, the consequence and the remedy live in `reference_map.py` beside the function that
#: decides staleness; only the framing ("Not submitted", "Nothing was sent to ComfyUI") is this
#: route's own, because only this route is the thing that did not submit. The bytes are unchanged
#: by the split — the frozen-wording tests assert it.
STALE_REFERENCE_MAP_REFUSAL = (
    f"Not submitted: {{shot}}'s {STALE_REFERENCE_MAP_CAUSE}. Nothing was sent to "
    f"ComfyUI, because {STALE_REFERENCE_MAP_CONSEQUENCE}. {STALE_REFERENCE_MAP_REMEDY}"
)


def reference_slot_counts(project: Project, shot: Shot) -> dict[str, int] | None:
    """How many slots of each kind this Shot's render will wire, or `None` when that is unknowable.

    `models.reference_slot_totals` is the answer, and it is the numbering itself rather than a
    second count of it: the same `numbered_references` walk that writes the tags in
    `reference_map_tag_lines` above, in the submit route's own loop, and in the expansion input the
    specialist is handed. Anything not a video or an audio is a picture, which is the route's own
    classification: `character`, `setting`, `prop`, `style` and `image` Assets all travel as
    pictures.

    **`None` — skip — is returned for a Shot citing an Asset this project does not hold**, and for
    nothing else. A dangling citation is dropped by `reference_map_tag_lines` and 422s at the route
    by name, so a count that included it would read as over-citation and replace the render's own
    "Unknown reference asset" refusal with a sentence about the prompt — the wrong thing to send
    the Director to fix.

    A Shot citing a video or an audio **used to skip here too**, and no longer does. That skip was
    honest about a real disagreement: `timeline.shot_expansion_input` numbered *every* citation into
    the `<Picture N>` series while this walk and the route numbered per kind, so bounding a
    video-citing shot would have refused the specialist for writing the tag it was handed. The two
    numberings are now one function (`models.numbered_references`, 2026-08-20), so the count is
    trustworthy for every kind and the check covers what it was skipping.
    """
    held = {asset.id for asset in project.assets}
    if any(citation.asset_id not in held for citation in shot.citations):
        return None
    return reference_slot_totals(project, shot)


def song_audio_prose(project: Project, shot: Shot) -> str:
    """The whole submitted prompt for a song-audio reference Shot, as prose.

    The night of measurements this encodes (2026-08-19, one A/B shot, same trim, turbo):
    every prompt in the H3 document format made the sampler *synthesize* its own score
    over the reference — fields present 0.36/0.27, guide-official citation fields -0.04,
    fields absent with vocal language -0.03, fields absent with plain "singing" 0.43,
    fields absent with a sync declaration 0.06 — while the plain-prose reference-map form
    measured 0.82 (no singing language), 0.77 (the run-1 take the Director praised) and
    **0.94 with the sings-to-camera clause**, with visible lip articulation and the set
    intact. The document header itself is the trigger; no wording inside it recovered.

    So a song-audio shot's "expansion" is this deterministic string: the same reference
    map the submit route numbers, the Shot's intent, the measured sings clause when the
    shot sings and the intent does not already say so, and the section look. It is the
    submit fallback's own construction, stored — record and submission stay one text.
    """
    section = song_section(project, shot)
    return reference_prompt(
        # A copy with the stored expansion blanked, so the fallback construction runs
        # even while a previous document expansion is still on the Shot.
        shot.model_copy(update={"h3_prompt": ""}),
        reference_map_tag_lines(project, shot),
        section_prompt=section.prompt if section is not None else "",
        vocal_overlap=shot_vocal_overlap(
            project.song, start=shot.start, duration=shot.duration
        ),
    )


#: The check, run for its refusal. See `SHOT_DIRECTOR_VISIBLE`.
SHOT_DIRECTOR_WITHHELD_FIELDS = _withheld_fields(
    Shot, visible=SHOT_DIRECTOR_VISIBLE, withheld=SHOT_DIRECTOR_WITHHELD, family="SHOT"
)


# What the Director's project dump leaves out. The recovery slots are *derived* from the
# mapping rather than listed, so a document added to it cannot have its kept copy echoed
# into the prompt by omission. See `director_chat` for why that matters.
DIRECTOR_CONTEXT_EXCLUDE: dict[str, Any] = {
    "jobs": True,
    # `Project.shot_sections` is a derived map from shot id to section id, added for the browser
    # so the Section target does not re-derive `section_of`'s rule. It is withheld here for the
    # reason `notices` is: the model is already told each Shot's section, by *name*, where it can
    # use it — `expansion_input` writes `section` onto the shot entry — and a second encoding of
    # the same fact as opaque id pairs would spend prompt on a restatement the model cannot read
    # any better than the first. Withholding it also keeps every Director prompt byte-identical
    # to what it was before this field existed, which is what makes the field safe to add.
    "shot_sections": True,
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
    # The Asset half, written as a classification for the same reason the Song half is and never
    # as a hand-typed nested path: what is excluded here is whatever `Asset` declares and nobody
    # classified as visible, so a field added beside `character_slot` cannot slip into every
    # Director prompt unnoticed. Unconditional rather than gated on the set being non-empty
    # (`shots`' pattern), because this one is non-empty from the moment it exists; if it ever
    # empties, it should be deleted rather than left as an exclusion that excludes nothing.
    "assets": {
        "__all__": _withheld_fields(
            Asset,
            visible=ASSET_DIRECTOR_VISIBLE,
            withheld=ASSET_DIRECTOR_WITHHELD,
            family="ASSET",
        )
    },
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
#
# `{displacement}` is `DOCUMENT_SLOT_DISPLACEMENT`'s phrase for this document — "applied
# replacement" for the two a reply can rewrite, "save that changed it" for the Brief — because
# naming the wrong writer here tells the Director to look for a protection that is not the one
# they have.
DOCUMENT_RESTORE_NOTICE = (
    "{document} was restored to the version kept before the last {displacement}. "
    "No Director call was made. The text that was replaced is now the kept version, so "
    "restoring again swaps back."
)
# ...except when the text being displaced is empty. An empty slot has to refuse, so that
# restore is one-way, and claiming reversibility exactly where the recovered text matters
# most would be the one lie this feature cannot afford.
DOCUMENT_RESTORE_ONE_WAY_NOTICE = (
    "{document} was restored to the version kept before the last {displacement}. "
    "No Director call was made. The document it replaced was empty, so nothing recoverable "
    "was displaced and there is nothing to swap back to: this restore is one-way."
)
# `{capture}` is `DOCUMENT_SLOT_CAPTURE`'s clause for this document. The refusal is the one
# sentence a Director reads at the exact moment they are looking for a version that is not
# there, so it has to name the writer that would have kept one — the Brief's is a save, and
# telling them to wait for a Director reply would be telling them to wait forever.
DOCUMENT_RESTORE_REFUSAL = (
    "No previous version of {document} was kept, so there is nothing to restore. A version "
    "is only kept when {capture}."
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


# --------------------------------------------------------------------------------------------
# Assistant ProducerBot
#
# The Director's language model, given one tool that fills shots in. Every wording below is either
# shared with the route a Director's own click takes or written here for something only this
# feature can do; nothing restates a rule that already has an implementation.
# --------------------------------------------------------------------------------------------

# Why a turn is refused before any model call. The empty case first, on `EXPANSION_WITHOUT_SHOTS`'
# argument: with nothing to write to there is nothing a result could be keyed to, and a model call
# would spend the Director's time to report that nothing happened.
ASSISTANT_WITHOUT_SHOTS = (
    "Assistant ProducerBot fills in shots you have selected, and this request named none that this "
    "project has. Select a shot on the timeline and ask again."
)
# ...and the same argument for a selection nothing could be written to. Refused before the call
# rather than after it, so a Director who selects one locked shot waits for a sentence instead of
# for a model. The reasons are the ones the reply would have carried, so the refusal before the
# call and the notice after it say the same thing about the same shot.
ASSISTANT_WITHOUT_WRITABLE_SHOTS = (
    "Nothing was sent to the model, because none of the selected shots may be written to. {reasons}"
)

# What one turn actually did, per shot, which is the thing the Director pressed the button for.
#
# `kind="change"`, not a caution: this is good news, and Story 2.2's whole finding was that a
# confirmation wearing warning chrome is how the refusal beside it stops being read. It also states
# what was *not* done, because "Assistant ProducerBot filled 6 shots" beside a button in an
# application that renders video invites exactly one wrong belief.
ASSISTANT_APPLIED_NOTICE = (
    "Assistant ProducerBot filled in {count} shot(s):\n{details}\n"
    "Nothing was rendered and no GPU time was spent. Open a shot to generate an image for it when "
    "you want one."
)
# The model addressing a shot the Director did not select. Refused rather than applied, and this is
# the guard that keeps the tool from widening what it can act *on*: the selection is the turn's
# scope, so a shot elsewhere in the plan — even a real, unlocked, perfectly writable one — is out
# of reach for this turn. Bounded through `_short`, because an id that matched nothing is whatever
# the model emitted and this sentence is persisted into the thread.
ASSISTANT_OUT_OF_SCOPE_NOTICE = (
    "Discarded: the model answered for {count} id(s) that are not among the shots this request "
    "selected ({shots}). Nothing was written for them and no shot was created."
)
# A selected shot that no longer exists, which is a stale selection rather than a model error. Its
# own sentence because the remedy is different: nothing about the reply is wrong.
ASSISTANT_MISSING_TARGET_NOTICE = (
    "Not filled in because this project no longer has them: {shots}. They may have been deleted "
    "while the model was thinking."
)
# The model answering for one shot twice, on `EXPANSION_DUPLICATE_NOTICE`'s argument exactly: first
# answer wins, because two contradictory specifications for one shot is a self-contradiction the
# Director has to see rather than a preference for whichever arrived last.
ASSISTANT_DUPLICATE_NOTICE = (
    "The model answered more than once for {shots}. The first answer for each was applied and the "
    "later ones were ignored, because a shot cannot be two things."
)
# The matrix's asset row, and the one refusal that is deliberately all-or-nothing for the shot: an
# invented asset id means this answer was not written against the library the Director actually
# has, so applying the mode and prompt from it and dropping only the citation would leave a shot
# declared as something its assets cannot satisfy — and leave it looking filled in.
ASSISTANT_UNKNOWN_ASSET_NOTICE = (
    "Nothing was applied to {shot}: the model cited {count} asset id(s) this project's library "
    "does not hold ({assets}). No asset was created and the rest of that answer was discarded, "
    "because a shot built on an id that does not exist is not the shot that was asked for."
)
# A tool call that did not fit the vocabulary at all — a mode the taxonomy has never had, a role
# that is not a role, arguments that are not JSON. This is what the typed surface converts a
# plausible-looking mistake *into*, so it is reported rather than swallowed, with the raw arguments
# carried in the notice's `raw` where `DIRECTOR_CONTEXT_EXCLUDE` keeps them out of the next call.
ASSISTANT_MALFORMED_NOTICE = (
    "Discarded: {count} tool call(s) did not fit the shot vocabulary — a mode, role or performance "
    "value this application does not have, or arguments that could not be read. Nothing was "
    "written for them. What the model sent is kept beside this notice for inspection and is left "
    "out of the context of the next Director call."
)
ASSISTANT_MALFORMED_EMPTY_NOTICE = (
    "Discarded: {count} tool call(s) did not fit the shot vocabulary — a mode, role or performance "
    "value this application does not have, or arguments that could not be read. Nothing was "
    "written for them, and the model sent nothing to inspect."
)
# A selected, writable shot the model simply never answered for. Reported rather than retried, on
# `EXPANSION_OMITTED_NOTICE`'s argument, and reported *because it was selected*: silence about a
# shot the Director explicitly picked is the failure this feature is forbidden to have.
ASSISTANT_OMITTED_NOTICE = (
    "Selected but not answered for by the model, and therefore unchanged: {shots}. Ask again if "
    "you want them filled in."
)
# The model naming a shot and then setting nothing on it. Distinguished from an omission because
# the model did spend a call on it, and from a change because nothing changed.
ASSISTANT_EMPTY_FILL_NOTICE = (
    "Answered for without naming anything to set, and therefore unchanged: {shots}."
)
# A shot that was filled in and still does not fit its own mode. A flag rather than a refusal, and
# that is the frozen matrix's decision rather than a leniency: planning a first/middle/last section
# before its images exist is real work, and the refusal that matters happens where GPU time would
# be spent. The sentences are `mode_specification_problems`', so this reads exactly as the shot
# inspector reads.
ASSISTANT_SPECIFICATION_NOTICE = (
    "Filled in, and still not fully specified for its mode:\n{details}\n"
    "This does not block anything now; it is refused at render."
)
# A citation the model aimed at a source picture and this route re-pointed at that subject's
# promoted identity sheet (`models.prefer_identity_sheets`). Said out loud rather than applied
# quietly: the model named one asset and the shot ended up citing another, and a substitution the
# reply did not mention is a substitution the Director would have to diff the manifest to find.
# The sheet is what the promotion exists to be cited as; the row is editable in the inspector.
ASSISTANT_IDENTITY_SHEET_NOTICE = (
    "Cited the promoted identity sheet instead of the single source picture on: {shots}. A "
    "multi-view sheet is the stronger identity reference and takes the same one slot; change the "
    "cited assets on any of those shots if you wanted the source frame itself."
)
# The model talking instead of calling the tool. Its own notice because it is the failure the
# system prompt is most likely to be iterated against, and a turn that quietly changed nothing
# reads as the feature being broken.
ASSISTANT_WITHOUT_TOOL_CALL_NOTICE = (
    "The model answered in prose and called no tool, so no shot was changed. Ask again, more "
    "directly, if you wanted it to fill the shot in."
)
# What the reply says when the model returned no sentence of its own, on `EXPANSION_EMPTY_MESSAGE`'s
# argument: a reply that is a bare separator followed by notices is not a reply.
ASSISTANT_EMPTY_MESSAGE = "Assistant ProducerBot returned no summary of this turn."


def assistant_fill_summary(applied: dict[str, object]) -> str:
    """One shot's line in the applied notice: what was set on it, in the Director's vocabulary.

    Built from what was *actually* assigned rather than from what the model asked for, so a field
    the tool sent and the route did not apply cannot appear here. Modes and roles are named by
    their labels — `SHOT_MODE_SPECS[…].label`, `ASSET_ROLE_LABELS[…]` — because "first_middle_last"
    is the wire vocabulary and "First / middle / last" is what the mode select says.
    """
    clauses: list[str] = []
    if "mode" in applied:
        clauses.append(f"mode {SHOT_MODE_SPECS[applied['mode']].label}")
    if "prompt" in applied:
        clauses.append("prompt written")
    if "singing" in applied:
        clauses.append(f"performance recorded as {applied['singing']}")
    if "citations" in applied:
        counted = Counter(citation["role"] for citation in applied["citations"])
        named = ", ".join(
            f"{count} {ASSET_ROLE_LABELS[role]}" for role, count in counted.items()
        )
        clauses.append(f"cites {named}" if named else "cites nothing")
    return "; ".join(clauses)


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
    return template.format(
        document=DOCUMENT_LABELS[document],
        displacement=DOCUMENT_SLOT_DISPLACEMENT[document],
    )


def document_restore_refusal(document: DocumentName) -> str:
    """Refuse a restore with an empty slot, naming the writer that *would* fill it.

    A function rather than a bare `.format` at the one call site, for `document_restore_notice`'s
    reason and one more: the refusal now interpolates two things, and a second caller that filled
    in only `{document}` would ship a sentence with a literal `{capture}` in it to the one screen
    where the Director is already looking for something that is missing.
    """
    return DOCUMENT_RESTORE_REFUSAL.format(
        document=DOCUMENT_LABELS[document], capture=DOCUMENT_SLOT_CAPTURE[document]
    )


def document_lock_refusal(project: Project, document: DocumentName) -> str:
    """The sentence refusing a **machine** write to a locked document, or `""` to proceed.

    One implementation of *may a machine write this document*, so that the chat route and every
    machine writer that comes after it — Suggest Video (TP-3), the planning passes (TP-10) —
    cannot answer it differently. The chat route inlined this check for two documents; the Brief
    is the case that makes a shared answer worth having, because its lock is the whole reason it
    has one: nothing writes the Brief today, and what the lock exists for is to stand between a
    re-run of Suggest Video and a Brief the Director spent an hour revising.

    **It is a lock against machines only.** `PUT /documents` assigns a locked document's text
    from the body exactly as it assigns an unlocked one's, and `restore_document` restores a
    locked document — a lock stops the Director's model, not the human who set it, and
    `DOCUMENT_LOCK_NOTICE` says so where the Director reads it. Refusing the human's own edit
    would leave them unable to fix a locked document without unlocking, saving, editing and
    locking again.
    """
    if not getattr(project, f"{document}_locked"):
        return ""
    return DOCUMENT_LOCK_NOTICE.format(document=DOCUMENT_LABELS[document])


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


#: Why an automated write may not touch one Shot, or `None` when it may. `"locked"` or `"rendered"`.
#:
#: One decision for both automated writers — the Director's shot expansion and Assistant
#: ProducerBot — because they are the same rule and a second copy of it is a guard hole waiting to
#: happen: the assistant is a *wider* capability than expansion (it sets modes and citations, not
#: only prompts), so a divergence would show up as the assistant writing to a Shot expansion
#: refuses, which is exactly "a tool that cannot be refused".
#:
#: The order is the precedence both routes already report by, and it is not arbitrary: a lock is a
#: decision the Director made and provenance is a fact about media that exists, so when both apply
#: the lock is the sentence worth reading. `director_chat` uses the same precedence for lock over
#: consent.
def shot_write_refusal(shot: Shot) -> Literal["locked", "rendered"] | None:
    """Whether an automated writer may change this Shot at all. See the note above."""
    if shot.locked:
        return "locked"
    if shot_render_provenance(shot):
        return "rendered"
    return None


def expansion_write_refusal(shot: Shot) -> Literal["locked", "rendered"] | None:
    """`shot_write_refusal`, with the prose exemption the Director asked for by hitting it.

    A song-audio reference shot's expansion is `song_audio_prose` — deterministic text
    derived from the intent, no model call — so re-deriving it after a render is not
    provenance loss: the prompt that produced each take is recorded on its job and in the
    take's own PNG metadata. Refusing it was the live break (2026-08-20): "The 'Expand
    Prompt Again' button was also broken so i couldnt update the creative intent and have
    the structured prompt updated to match" — on a plan where every shot has rendered,
    the refusal made intent edits permanently unable to reach the prompt. Locked stays
    locked, and the document modes keep the full refusal: their expansions are model
    output whose in-place rewrite really does lose the record.
    """
    reason = shot_write_refusal(shot)
    if (
        reason == "rendered"
        and shot.use_song_audio
        and resolve_shot_mode(shot) == "references"
    ):
        return None
    return reason


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
    if shot_is_approved(shot):
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
    status has been walked backwards by hand — which is precisely how a Shot can read `complete`
    on screen while ComfyUI is still working on it. The generic writes were that hand until
    2026-08-20; `_require_in_flight_status_kept` closed them, and what is left is a manifest
    edited on disk, restored from a backup, or saved by a build older than the gate. This still
    reads both signals for exactly those.

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


def refresh_reference_maps(project: Project) -> list[str]:
    """Re-derive every stale reference map that can be re-derived for free. No model call, ever.

    The Director's ask, in full: *"make the re-expand automatic when an asset is attached"* — after
    attaching a character sheet to a shot whose take had invented a bride, and then having to press
    Expand Prompt Again by hand before the re-render was right.

    **Expansion is two things and this only does the free one.** A song-audio reference shot's
    expansion is `song_audio_prose`: deterministic text built from the shot, the project and the
    section, with no model call and measured at 0.0 s. Re-deriving it on attach is free, and the
    result is byte-identical to what pressing the button would have produced — `attempt_expansion`
    returns that same string for that same shot, and `apply_expansions` puts it through the same
    `normalize_audio_fields`. A document-mode expansion is model output. **Nothing here calls the
    specialist**: attaching three assets would fire three unrequested calls on a local model that
    can take minutes, and this codebase refuses by name rather than guessing. Those shots are left
    stale on purpose and `generate_h3` refuses them by name — see `STALE_REFERENCE_MAP_REFUSAL`.

    Idempotent, and swept over the whole plan rather than over "the shots this request touched",
    because the caller does not always know. The shots write does not adopt its own reply, so a
    client holding the pre-refresh `h3_prompt` reasserts it on its very next gesture; a pass keyed
    on what *changed in this request* would see no citation change there and let the stale text
    stand. Keyed on the text instead, the same pass simply re-derives it again. That is also why a
    hand-edited prose whose map no longer matches is rewritten rather than preserved: the map is
    what conditions the render, `Shot.prompt` is where the Director's own words live and is never
    touched here, and this is what re-expanding means.

    **Three shots are skipped and none of them is silently rewritten.** A shot with no expansion is
    not stale (see `stale_reference_map`). A `locked` shot is the Director's explicit hands-off and
    only they clear it. A shot with a render **in flight** is `replace_asset_citations`' one genuine
    correctness block: the job was submitted against the prompt as it stands, and rewriting it
    underneath would leave that job's record describing a submission that never happened. A
    *rendered or approved* shot is refreshed, which is `expansion_write_refusal`'s existing carve-out
    in its existing words — the prompt each take was submitted with is recorded on its job and in
    the take's own PNG metadata, so a prompt is not a take.

    Returns the ids it rewrote, so a caller that wants to report can.
    """
    refreshed: list[str] = []
    for shot in project.shots:
        if not stale_reference_map(project, shot):
            continue
        # The free arm and only the free arm: the deterministic prose recipe, identified by the
        # form of the text actually stored rather than by a rule about the shot.
        if not (song_audio_prose_expansion(shot) and shot.use_song_audio):
            continue
        if resolve_shot_mode(shot) != "references":
            continue
        if expansion_write_refusal(shot) or shot_render_in_flight(project, shot):
            continue
        # `apply_expansions`' two lines, not a third spelling of them.
        shot.h3_prompt = normalize_audio_fields(
            song_audio_prose(project, shot), audio_tag=song_audio_tag(project, shot)
        )
        shot.h3_prompt_map = reference_map_sentence(reference_map_tag_lines(project, shot))
        refreshed.append(shot.id)
    return refreshed


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
# The lip-sync gate, ruled by the Director on 2026-08-18. The enhancer measurably moves lip
# position — `ManualSigmas` starts at 0.909375, so it re-generates rather than refines, and the
# measured frames show a mid-vowel mouth closed — so a singing Shot loses the one thing the H3
# reference path exists to get right. `unknown` refuses too, with the fix named: in a music
# video an unlabelled Shot is likelier singing than not, and a wrong guess destroys lip-sync
# silently. Only an explicit `not_singing` passes. This is the enforcement of the per-shot rule
# the Director stated when the measurement landed: "when we do [sing], its important."
ENHANCE_SINGING_REFUSAL = (
    "{shot} is a singing shot, and the LTX enhancer measurably moves lip position — it "
    "re-generates rather than refines. Enhancing it would destroy the lip-sync the render "
    "exists to produce. Nothing was submitted."
)
ENHANCE_SINGING_UNKNOWN_REFUSAL = (
    "{shot}'s singing state has never been set, and the LTX enhancer measurably moves lip "
    "position. Set the shot's singing state first — Not singing makes it enhanceable; Singing "
    "keeps it protected. Nothing was submitted."
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


def shot_audio_restore_in_flight(project: Project, shot: Shot) -> bool:
    """True when a song-audio restoration for this Shot has been accepted and has not landed.

    Its own function for `shot_enhancement_in_flight`'s reason and by the same rule: this path
    writes nothing to the Shot, so `Shot.status` is not evidence about it and reading it here
    would make an H3 render indistinguishable from a restoration in the sentence the Director
    reads. The job records are the only evidence, keyed by `kind="post"`.
    """
    return any(
        job.kind == "post" and job.target_id == shot.id and job.status in RENDER_IN_FLIGHT_STATUSES
        for job in project.jobs
    )


# Why one Shot's take may not have the master song put back over it. Each names the Shot as the
# timeline names it, for `render_again`'s reason.
#
# There is deliberately no readiness gate and no prompt check here, for the enhancer's reason and
# more strongly: this graph has no prompt input at all, no model of any kind, and does not
# generate. What must exist is a take and a window.
RESTORE_AUDIO_NO_TAKE_REFUSAL = (
    "{shot} has not produced a take, and restoring the song puts audio over a picture that "
    "already exists. Render the shot first, then restore the audio on the take you want to hear."
)
# Names the path, per the matrix, for `ENHANCE_MISSING_TAKE_REFUSAL`'s reason.
RESTORE_AUDIO_MISSING_TAKE_REFUSAL = (
    "{shot}'s take is recorded as {path} and there is no file there. Nothing was submitted. "
    "The take may have been moved or the ComfyUI output directory cleared."
)
# The matrix's "shot without song audio" row, stated as the reason rather than as a flag being
# off. A shot rendered without the master song was never conditioned on any part of it, so there
# is no window this stage could take, and picking one would put the picture out of sync with the
# sound that produced it -- which the frozen spec calls worse than leaving the generated audio in
# place. That is why this is a refusal and not a default.
RESTORE_AUDIO_NOT_SONG_AUDIO_REFUSAL = (
    "{shot} was not rendered with the master song attached, so there is no window of the song "
    "it was conditioned on. Guessing one would put the picture out of sync with the sound that "
    "produced it, which is worse than leaving the take's own audio in place. Nothing was "
    "submitted."
)
# Named per the matrix: what is missing, not that something is.
RESTORE_AUDIO_NO_SONG_REFUSAL = (
    "This project has no song, and restoring {shot}'s audio takes its seconds from the master "
    "track. Add or generate the project song first. Nothing was submitted."
)
RESTORE_AUDIO_MISSING_SONG_REFUSAL = (
    "This project's song is recorded as {path} and there is no file there, so {shot} has "
    "nothing to take its seconds from. Nothing was submitted."
)
# The legacy take, and the one refusal on this route that is about the *take's bookkeeping*
# rather than about the shot, the song or the file.
#
# A take begins `latest_take_lead` seconds before its window, and that number is recorded at
# submission because it cannot be derived afterwards — a pre-margin take and a post-margin one
# are indistinguishable by arithmetic on their lengths (`Shot.latest_take_lead`). Since the
# margin shipped, every song-audio submission records a lead of `min(ideal, extra, start)` with
# `ideal` at least a quarter second and `extra` always positive, so a recorded lead is zero
# **exactly when the shot starts at 0 s** — which is legitimate and is not refused here.
#
# A zero lead on a shot that starts later therefore means one of two things, and neither can be
# repaired from the manifest: the take was rendered before the margin existed, or its
# bookkeeping was cleared when an external clip was selected for the shot (`select_shot_clip`).
# Windowing such a take by any rule is a guess about its provenance, and the failure a wrong
# guess produces is a subtle desync rather than an error — the same reason a shot that never
# rode the master is refused instead of given a window. Re-render the shot and the lead is
# recorded.
#
# **Why 0 s is exempt, corrected 2026-08-21.** The argument above — "a post-margin submission
# records zero only at 0 s" — is true and is *not* what makes the exemption safe: a take at 0 s
# is exactly as likely to be a legacy one as a take at 12 s, and a recorded zero there
# distinguishes nothing, because `min(ideal, extra, start)` could not have recorded anything
# else. What makes it safe is the *offset*, which is the thing this refusal protects: at a
# window start of 0 s, `over_render_window` answers `trim_start = 0` for lead 0, and a take
# rendered before the margin also begins at song second 0. Pre-margin or post-margin, the first
# frame of that take is the song's first sample, so there is no desync to guess wrong about.
# What can still differ at 0 s is the take's *length* — a pre-margin take holds fewer frames
# than `over_render_frames` now asks for — and a length is reported, never assumed: see
# `RESTORE_AUDIO_UNDESCRIBED_TAKE`, which is what the response says whenever the take carries no
# window snapshot to be described from.
RESTORE_AUDIO_NO_LEAD_REFUSAL = (
    "{shot} starts at {start:g}s but its take records no sync lead, so this take was rendered "
    "before takes carried one, or its clip was chosen by hand. How far before the window the "
    "picture begins cannot be worked out after the fact, and a guessed offset would put the "
    "sound out of sync with the mouth. Render the shot again and the lead is recorded with it. "
    "Nothing was submitted."
)
# Concurrency, as the concrete harm. Covers a live render, a live enhancement and a second
# restoration: all three can move or race the file this one reads or the prefix it writes under.
RESTORE_AUDIO_IN_FLIGHT_REFUSAL = (
    "Work on {shot} has not finished. Restoring the song over a take while a render, an "
    "enhancement or another restoration is still running would work from a take that may be "
    "about to change, so nothing was submitted. Wait for it, or refresh the render queue if it "
    "has already finished and this project has not been told yet."
)
#: The filename prefix a restoration writes under, appended to the shot's own, for
#: `ENHANCE_PREFIX_SUFFIX`'s reason and carrying one more guarantee. ComfyUI numbers its outputs
#: per prefix, so this is what makes the restored file a **sibling** of the take rather than the
#: next entry in its numbered series — and it is the mechanism behind the frozen "Never
#: overwriting the take being processed". Run twice, the second restoration takes `_00002` under
#: this same prefix: a further sibling, never an edit in place, which is the matrix's own wording.
RESTORE_AUDIO_PREFIX_SUFFIX = "-song-audio"
#: Half a frame at 24 fps, the tolerance `lengths_match` uses. Both numbers it compares are
#: floats and one of them is a division, so an exact `==` would report a mismatch on arithmetic
#: rather than on a real difference in length.
RESTORE_AUDIO_LENGTH_TOLERANCE = 0.5 / 24
#: The same tolerance, spent on a different question: has the shot's window been edited since
#: this take was rendered? Both sides are manifest floats that survived a JSON round trip, so an
#: exact `!=` would report a move on the last bit of a number nobody touched.
RESTORE_AUDIO_WINDOW_TOLERANCE = 1e-6
#: What the length note says when the take carries no window snapshot — every take rendered
#: before `Shot.latest_take_start` existed (2026-08-21), and every hand-picked clip, whose
#: bookkeeping `select_shot_clip` clears.
#:
#: This is the "stop claiming" half of the 2026-08-21 finding and it is the honest reading of
#: what the numbers are: without a snapshot the only window on the manifest is the *live* one,
#: and the live window is a fact about the plan, not about the file. If it has not been edited
#: since the render the numbers are the take's; if it has, they are not, and nothing in the
#: manifest can tell those apart. Reported rather than refused, because refusing would disable
#: this stage for every take that exists today — including the ones on the GPU right now —
#: over a staleness there is no evidence of, and the failure it would prevent is a *reported*
#: length, not a silent one. The offset is safe on its own terms: `latest_take_lead` is refused
#: when it cannot place the take at all (`RESTORE_AUDIO_NO_LEAD_REFUSAL`).
RESTORE_AUDIO_UNDESCRIBED_TAKE = (
    "These two numbers are read from the shot's window as it reads now, because this take "
    "recorded none of its own — it was rendered before takes carried a window, or its clip was "
    "chosen by hand. They describe the take only if the window has not been edited since, which "
    "the manifest cannot say. Render the shot again and the take's own window is recorded with "
    "it. "
)
#: And what it says when the take *does* carry a snapshot and the window has since moved. Not a
#: refusal: the take is a fixed file and its recorded window places it exactly, so this stage
#: still lays the seconds it was performed against over it — the same file it would have made
#: the moment the render landed. What has changed is the plan around it, which is assembly's
#: refusal to make (`assembly.ASSEMBLY_STALE_REFUSAL`) and not this stage's; said here so the
#: Director reads it from the report rather than discovering it at export.
RESTORE_AUDIO_WINDOW_MOVED = (
    "This shot's window has been edited since the take was rendered — it now reads "
    "{start:g}s for {duration:g}s, and the take was rendered for {take_start:g}s for "
    "{take_duration:g}s. The numbers above are the take's, which is what the master is laid "
    "over; the timeline's own window is a separate decision and assembly answers for it. "
)
# Deliberately ASCII, exactly as `batch.READINESS_REFUSAL` and the render-again refusals are, and
# for the same reason: the frontend halves are read back through node, whose stdout the contract
# test decodes with the platform encoding on Windows.


def shot_is_approved(shot: Shot) -> bool:
    """Whether somebody has made the editorial decision this Shot's refusals key on.

    Both signals, because they are set independently — `approved_output` by the approve route and
    the `approved` status by a manifest that predates `_require_approval_unchanged`, or one edited
    off-route — and a Shot carrying either is a Shot somebody has decided about: the first is the
    decision AGENTS.md names, and the `approved` status is reachable by hand and must not be a
    state nothing can clear. One definition, read by `render_again_refusal`'s approval arm and by the
    un-approve route, so the set of Shots render-again refuses as approved and the set un-approve
    can rescue are the same set by construction rather than by two lists agreeing.
    """
    return bool(shot.approved_output) or shot.status == "approved"


# Why one Shot's take cannot be played. Each names the Shot as the timeline names it, for
# `render_again`'s reason: a bare `shot_a1b2c3d4e5f6` appears nowhere in the interface.
#
# Both are 404s rather than 422s, unlike the enhancer's take refusals, because the request is a
# GET for a resource: what the client asked for does not exist, and the sentence in `detail` is
# for the Director while the code is for the `<video>` element, which treats any error the same
# way. The second names the path, per the matrix's own row: a manifest pointing at a file that is
# gone is usually a moved or cleared ComfyUI output directory, and the only way the Director can
# tell which is to see where this looked.
TAKE_NOT_RENDERED_REFUSAL = (
    "{shot} has not produced a take, so there is nothing to play. Render the shot first."
)
TAKE_MISSING_FILE_REFUSAL = (
    "{shot}'s take is recorded as {path} and there is no file there. The take may have been "
    "moved or the ComfyUI output directory cleared."
)
# Why one Shot's take may not be approved. FR-21's rule is that approval is explicit and
# reversible, so both refusals say what would make the request approvable rather than only that
# it was refused.
APPROVE_NO_TAKE_REFUSAL = (
    "{shot} has not produced a take, and an approval is an editorial decision about one "
    "specific take. Render the shot first, then approve the take you watched."
)
# Concurrency, stated as the concrete harm rather than as a busy signal, with the same staleness
# escape every in-flight refusal names: job status only moves when the queue is refreshed, so a
# job that finished while nobody was polling still reads as in flight here.
APPROVE_IN_FLIGHT_REFUSAL = (
    "A render for {shot} has not finished, so the take on screen is about to be displaced. "
    "Approving it now would leave the decision attached to whichever file lands next. Wait for "
    "it, or refresh the render queue if it has already finished and this project has not been "
    "told yet."
)
# The un-approve refusal names what the Shot actually is, per the matrix: a Director asking to
# clear an approval that does not exist is holding a stale picture of this Shot, and the status
# is the correction.
UNAPPROVE_NOT_APPROVED_REFUSAL = (
    "{shot} carries no approval to clear: no approved take is recorded and its status is "
    "{status}. Nothing was changed."
)
#: The three fields one approval consists of, as a list rather than as three checks. The pair
#: below `approved_output` is AD-13's window snapshot, written and cleared by the approve/
#: un-approve pair in the same two writes; a gate that protected the decision and not the window
#: it was made in would leave a save able to re-point the snapshot at a window nobody approved,
#: which is exactly the staleness assembly refuses on.
APPROVAL_FIELDS: tuple[str, ...] = ("approved_output", "approved_start", "approved_duration")

# Why a whole-manifest save may not change an approval. The middle clause is
# `RENDER_AGAIN_APPROVED_REFUSAL`'s and `APPROVE_NO_TAKE_REFUSAL`'s, word for word, because it is
# the same fact about approval being restated to a save rather than a second opinion about it: an
# approval is a decision about one piece of media, and the routes that make and withdraw it are
# the only places that decision is expressed. The refusal names the two actions, because the
# Director reading it is holding a client that just tried to write the field directly.
#: The optimistic-concurrency refusal, shared by the two manifest writes that take a revision:
#: `replace_project` and `replace_shots`. Named rather than inlined twice because the timeline's
#: undo now says the same sentence *before* the request -- it pre-flights the same comparison so
#: the button can explain itself rather than only failing -- and a second wording of one rule is a
#: second thing to keep true. `api.js` carries a copy pinned to this one by a contract test.
PROJECT_CHANGED_REFUSAL = "Project changed since it was loaded; refresh before replacing it"

#: The same 409 for the same reason, one layer down and without a revision to compare. Where
#: `PROJECT_CHANGED_REFUSAL` is a request that arrived carrying a stale `updated_at`,
#: `SAVE_RACE_REFUSAL` is a request whose *write* found the manifest moved underneath it — the
#: store's `ProjectChangedDuringSave`, raised when another save landed inside this one's replace
#: backoff. Deliberately a separate sentence rather than a reuse of the constant above: that one
#: is pinned byte for byte to a copy in `api.js` by a contract test, and this refusal is about
#: something the client could not have pre-flighted, so it carries "nothing was saved" explicitly.
#: Same remedy either way, which is why the wording ends in the same instruction.
SAVE_RACE_REFUSAL = (
    "Another change was saved to this project while this one was being written, so nothing was "
    "saved; refresh before replacing it"
)

#: How many times one `/render-status` tick may re-read, re-reconcile and try its save again
#: before giving up on writing this tick at all. The poll is the most frequent writer in the
#: application — every two seconds, per open project — so it is the most likely thief, and it
#: passes `save`'s `if_generation` precisely so it is refused rather than allowed to lay a
#: manifest it read a moment ago over whatever the Director just saved.
#:
#: Three because the collision it is built for is a single foreign write landing inside one
#: tick's window (a submission stamping its accepted prompt id, a shot edit, an approval), and
#: one re-read clears that; the second and third are there for a burst, which a batch
#: submission genuinely produces. Bigger would be worse, not better: each attempt is another
#: `/queue` and another round of `/history`, and a tick that cannot win three in a row is losing
#: to a writer that is still going.
#:
#: Exhausting it is deliberately harmless and deliberately silent. Nothing is lost — the
#: reconciliation is *derived* from ComfyUI rather than authored here, so the tick two seconds
#: from now re-reads, asks ComfyUI the same question and reaches the same verdict. Raising would
#: put a toast on the Director's screen for a race the Director caused by typing, which is the
#: same reasoning that made this route absorb `ProjectChangedDuringSave` in the first place. No
#: user-initiated write may copy this: there, the caller is owed the 409.
RENDER_STATUS_SAVE_ATTEMPTS = 3

GENERIC_WRITE_APPROVAL_REFUSAL = (
    "This save would change {shot}'s approval. An approval is an editorial decision about one "
    "specific take, so it is not something an ordinary save carries: approve and un-approve are "
    "their own actions. Nothing was saved. Use them if the decision has changed."
)
# What each direction did, said rather than implied, on `MARK_READY_NOTICE`'s argument: the
# approve toast has to carry the consequence -- the shot stops being re-renderable -- and the
# un-approve toast has to say what was *not* lost, or a Director unsure of the cost will leave a
# wrong approval standing rather than risk the take.
APPROVE_NOTICE = (
    "{shot}'s latest take is approved. The approval names that exact file, so the shot cannot "
    "be re-rendered or re-queued while it stands. Un-approve it if the decision changes."
)
UNAPPROVE_NOTICE = (
    "{shot}'s approval is cleared and the shot is back to complete, so it can be re-opened and "
    "rendered again. Nothing was deleted: the take is still this shot's latest output."
)
# Deliberately ASCII, exactly as the render-again and mark-ready refusals are and for the same
# reason: the frontend halves are read back through node, whose stdout the contract test decodes
# with the platform encoding on Windows.


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


def expansion_shot_label(project: Project, shot: Shot) -> str:
    """Name a Shot in an expansion notice: `shot_label`, plus where it sits in the song.

    **This was the fifth numbering scheme, and it is retired rather than documented.** It read
    `shot index 2 at 90s (shot_id)` — a different noun ("index", not "SHOT") and a different base
    (0, not 1) — on the argument that the notice had to match the `index` `expansion_input` gave
    the model, because the timeline supposedly numbered by the manifest. Two things killed that
    argument on 2026-08-26:

    * The timeline numbers by **song order**, which is the order `expansion_input` uses, so the
      model's `index` and the clip's number were never in conflict — only off by one. Off by one
      is worse than conflict: a Director reading "shot index 3" beside a clip reading `SHOT 03`
      finds a plausible wrong shot rather than an obvious mismatch.
    * The model answers by `shot_id`, never by `index` (`ExpandedShot.shot_id` is the only handle
      the reply carries), so nothing about the reply depends on the notice repeating the index.
      `expansion_input` has no thread and no history, so the notice is never read back to it.

    So the audience for this string is the Director alone, and it says what the clip says.
    `shot_label` is called rather than re-derived from the caller's `enumerate` position, so there
    is exactly one function in this language that turns a Shot into a number.

    The start time stays. It is not the number — it is what makes the sentence findable on the
    timeline when a plan has forty shots, and it survives a re-numbering that the number does not.
    """
    return f"{shot_label(project, shot)} at {shot.start:g}s"


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


def assistant_prompt_rejection(prompt: str) -> str:
    """Why a prompt Assistant ProducerBot wrote must not land on a Shot, or "" when it may.

    Nothing new is decided here. `batch.prompt_rejection` is the existing judgement about what a
    prompt is worth — AD-5's one implementation, the same call `readiness_report` and the queue
    make — and it is deliberately used in preference to `expansion_rejection`, because it catches
    the `"New shot"` placeholder as well as blank. A local model echoing the placeholder it was
    shown in `current_prompt` is an ordinary local-model behaviour, and it would otherwise be
    written onto the Shot as a real prompt and then blocked by the readiness gate afterwards: the
    Director would read "prompt written" and then be refused at the queue for having no prompt.

    The JSON-as-prose half is `document_rejection`'s, reached exactly the way `expansion_rejection`
    reaches it — `""` as the existing text, so only that check runs and the 40% ratio floor, which
    is meaningless against a placeholder, does not.
    """
    return prompt_rejection(prompt) or document_rejection(prompt, "")


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


def _require_in_flight_status_kept(project: Project, shots: list[Shot]) -> None:
    """Refuse a save that takes a Shot **out of** `queued`/`running`. See `RENDER_IN_FLIGHT_STATUSES`.

    The narrow form, on `_require_song_replacement_confirmation`'s precedent: the incoming Shot is
    compared against the stored one, so an ordinary save round-trips every status untouched and
    only a body that *moves* one is refused. That is what makes this shippable — the two generic
    `PUT`s are the normal save path for every edit in the interface, and a gate on the mere
    presence of `status` would refuse every save there is.

    Only one direction is refused, and it is the one with a concrete cost. `shot_render_in_flight`
    reads `Shot.status` because it is "the half that is right when no job record was ever written";
    a body that walks that status back to `draft` or `ready` therefore erases the one record a live
    render left on the Shot, and the next submission sails past the check that exists to stop two
    renders racing on one output prefix. Everything else stays exactly as it was: `draft -> ready`
    is legitimate arming, `complete -> draft` is a Director tidying up after a take, and
    `queued -> running` is still in flight and loses nothing.

    Deliberately keyed on the *stored* status alone rather than on `shot_render_in_flight`. The job
    records are the durable half and they survive a walked-back status regardless, so folding them
    in here would refuse saves on projects whose every Shot is settled and whose only in-flight
    signal is a job nobody has polled since it finished.

    A Shot in the body that the stored project does not hold is a new Shot: there is no live render
    behind it to lose, so it is not this gate's business.

    The sentence is `MARK_READY_IN_FLIGHT_REFUSAL`, verbatim and with its 409, because it is
    already the answer this application gives when a Director tries to set the status of a
    rendering Shot -- "its status is not yours to set right now" -- and a second wording of one
    rule is a second thing to keep true.
    """
    stored = {shot.id: shot for shot in project.shots}
    for shot in shots:
        was = stored.get(shot.id)
        if was is None or was.status not in RENDER_IN_FLIGHT_STATUSES:
            continue
        if shot.status not in RENDER_IN_FLIGHT_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=MARK_READY_IN_FLIGHT_REFUSAL.format(shot=shot_label(project, was)),
            )


def _adopt_expansion_maps(project: Project, stored: dict[str, Shot]) -> None:
    """Server-own `Shot.h3_prompt_map` across the two generic whole-shot writes.

    The recorded hole in this repository, met for the fifth time on `replace_project` and at least
    the third on `replace_shots`: a whole-manifest body binds a defaulted model, so a client
    written before this field existed simply omits it, it arrives as `""`, and one ordinary save
    would erase the recorded map on every shot at once. For a document-mode expansion that record
    is the *only* thing that can tell a stale reference map from a fresh one — its own text never
    writes the map down — so clearing it would silently retire the refusal that keeps a stale map
    out of ComfyUI. Exactly the shape `consistency_prompt` and `default_setting_id` have here.

    Two cases, and the second is why this adopts rather than simply overwrites. A body whose
    `h3_prompt` **equals** the stored one is not writing an expansion at all, whatever else it
    carries, so the stored record is kept and the body's value is ignored. A body whose `h3_prompt`
    **differs** is the inspector's H3 box being written by hand, and the text a Director typed just
    now was typed against the plan as it stands now — so it is stamped with the current map. That
    is what lets someone fix a stale document expansion themselves instead of being told to
    re-expand something they have already corrected. A blank expansion records no map, because an
    unexpanded shot has none.

    A Shot the stored project does not hold is new: it gets the map its own text implies by the
    same two rules, against a stored value of `""`.

    `project` is the project **as it will be saved** — the map is built from the assets and the
    citations this write is landing, never from the ones it is replacing — and `stored` is the
    previous shots by id, which the caller snapshots before overwriting them.
    """
    for shot in project.shots:
        was = stored.get(shot.id)
        if was is not None and shot.h3_prompt == was.h3_prompt:
            shot.h3_prompt_map = was.h3_prompt_map
        elif not shot.h3_prompt.strip():
            shot.h3_prompt_map = ""
        else:
            shot.h3_prompt_map = reference_map_sentence(
                reference_map_tag_lines(project, shot)
            )


def _adopt_shot_effects(
    project: Project,
    stored: dict[str, Shot],
    *,
    looks: Callable[[], Sequence[LutEntry]] | None = None,
) -> None:
    """Server-own `Shot.effects` across the two generic whole-shot writes.

    **Written in the same commit as the field**, because this repository's own history says the
    alternative does not work: `replace_project`'s own comments counted eleven findings of this
    hole before this field arrived to make it twelve, and AD-16 turned "add the adopt guard
    afterwards" into a rule precisely
    because afterwards never arrived. `_adopt_expansion_maps` is the shape being matched here, and
    the failure is identical.

    It fails the two ways every one of its siblings does. `effects` is a defaulted list, so a
    client written before it existed — which is every client until slice C2, and every hand-rolled
    API call forever — simply omits it, it arrives as `[]`, and **one ordinary save would strip the
    look off every Shot in the project at once**. The timeline's own drag makes that the likely
    path rather than the exotic one: moving a clip writes the whole shot list back through
    `PUT .../shots`, so a Director who graded ten shots and then nudged one would lose all ten and
    be told 200. And a body that *invented* a stack would be writing filter configuration through
    a route that never asks the catalogue whether it is composable, past the one validator (AD-27)
    that stands between a client's numbers and an ffmpeg filter string.

    A Shot the stored project does not hold is new, and its stack is **validated and kept** —
    not dropped, which is what this helper did until 2026-08-25 and what two reviewers found
    independently. Dropping it looked like the anchor adoption's `.get(..., "")` reading, and it
    was wrong for this field because of the classification: `effects` is
    `SHOT_PLAN_CONTENT_FIELDS`, so Split and Duplicate copy the stack onto a new id
    (`api.newShotFromPlan`) and persist it through `PUT .../shots` — the only order a Director
    ever produces. The half a Director never touched came back ungraded, and
    `models.SHOT_PLAN_CONTENT_FIELDS` says exactly why that is the sharp case: "the two halves of
    one shot are one shot's look, and a half that lost it would grade differently from its own
    other half."

    Validated rather than trusted, which is what keeps the property this guard actually protects.
    Nothing client-supplied reaches a filter string without `validate_stack` agreeing first
    (AD-27) — the same function the narrow route runs and the same one the export runs again — so
    a new Shot cannot smuggle filter configuration past the catalogue by arriving on a whole-plan
    write. A stack it refuses refuses **the whole write**, by name, with the chain's own sentence
    carried whole: a save that landed nine shots and silently dropped the tenth's look is the
    failure being fixed, not a smaller version of it to introduce here.

    It is also atomic. The split and its look land in one request, where a client-side follow-up
    write to the narrow route could fail — a 409, a closed tab — and leave a half ungraded with
    nothing saying so. `_adopt_expansion_maps`, the sibling this helper matches, already handles
    its own new-Shot case by deriving rather than dropping; dropping was never the only option.

    `looks` is a callable rather than a listing because discovery costs 221 ms cold and this
    helper runs on **every** ordinary save. It is called only when a new Shot actually carries a
    stack, which is the same rule `replace_shot_effects` follows: a plan of unstyled shots reads
    the looks folder exactly never.

    An **existing** Shot's stored stack is adopted whatever the body says, in both directions, and
    that is untouched: a body that omits `effects` cannot blank a look and a body that invents one
    cannot plant it. Deep-copied rather than shared, because the stored `Shot` objects and the
    ones about to be saved are different objects and a shared `EffectSpec` would let a later
    mutation of one show up in the other.

    `PUT .../shots/{shot_id}/effects` is still the one route that *edits* a stack, and it is what
    keeps this field out of reach of anything a model can call — no tool schema declares it and
    the Director's context withholds it.

    **A Parameter Binding lives inside `EffectSpec`, and it needed no matching helper of its own**
    — which was worth checking rather than assuming, because a binding is nested one level deeper
    than anything the `_adopt_*` family has guarded before. Both branches above answer it:

    * An **existing** Shot's whole stack is taken off the store and the body is discarded, in both
      directions. A binding riding inside that stack was therefore server-owned by this guard from
      the moment the field existed. There is nothing to match, because nothing is merged.
    * A **new** Shot's stack is validated and kept, for `SHOT_PLAN_CONTENT_FIELDS`' reason, and a
      binding is part of the look that argument is about. Its bindings are then **adopted from the
      stored card each one names** — `adopted_effect_stack`, with the project's cards as the source,
      because the whole point of a Split is that this id did not exist a moment ago and the stack
      came off a sibling. Every card is minted a new id on the way in, so the two Shots do not both
      claim the card the copy was taken from. A card naming no card the project holds is answered
      by `_copied_bindings` rather than by its id, because this is the door a replayed Undo or Redo
      arrives at and the card such a replay names may be one the replay itself deleted.

    **That replaced a refusal on 2026-08-28 (R-33).** The binding used to be guarded here by
    `carried_bindings_refusal`, a multiset of validated bindings held across every stored Shot,
    and it failed three ways this cannot: one deleted `.cube` anywhere in the project emptied that
    multiset and refused every Split and Duplicate of every bound Shot (A1); the count was checked
    once per arriving Shot rather than across the write, so one held binding multiplied onto
    arbitrarily many new ids (A4); and a body that carried *fewer* bindings than were held was
    accepted, so a Split that dropped one landed silently. Adoption answers all three by not
    reading the body at all — and it costs no folder read, which is what lets it run on every
    ordinary save rather than only on a stack that carries a binding.

    So the nested field costs one call on one branch, not a fourteenth adopt helper.
    """
    for shot in project.shots:
        was = stored.get(shot.id)
        if was is not None:
            shot.effects = [spec.model_copy(deep=True) for spec in was.effects]
            continue
        if not shot.effects:
            continue
        stack = [spec.model_dump() for spec in shot.effects]
        # Counted here as well as on the editing route, because `validate_stack` does not count.
        # It answers "is every card composable", one card at a time, and a thousand composable
        # cards are a thousand valid answers -- so the cap that `replace_shot_effects` applies
        # before it calls the validator has to be applied before this call too. Without it this
        # branch was the widest of the **three** doors past `SHOT_EFFECT_STACK_LIMIT` -- this one,
        # `replace_shot_effects` and `copy_shot_effects`, which is three guards over four routes
        # because `PUT /api/projects/{id}` and `PUT .../shots` both arrive here. Measured at 985
        # cards on an invented shot id it builds a 34,686-character `-vf`, past the 32,767 Windows
        # allows a command line, and the export then reports a working ffmpeg as missing. The
        # editing route was capped in the same session this branch was widened to keep a Split's
        # look, and the cap did not come with it; the copy route went four more days uncapped,
        # which is P4 of Epic 9's retrospective happening while the sentence describing it was
        # being written.
        if len(stack) > SHOT_EFFECT_STACK_LIMIT:
            raise HTTPException(
                status_code=422,
                detail=SHOT_EFFECTS_TOO_MANY_REFUSAL.format(
                    limit=SHOT_EFFECT_STACK_LIMIT, count=len(stack)
                ),
            )
        luts = looks() if looks is not None else ()
        try:
            validate_stack(stack, luts=luts)
        except EffectRefusal as refusal:
            raise HTTPException(
                status_code=422,
                detail=SHOT_EFFECTS_UNCOMPOSABLE_REFUSAL.format(
                    shot=shot_label(project, shot), detail=refusal
                ),
            ) from refusal
        # Adopted from the card each entry names, for the field inside the field. A new Shot's
        # stack is kept because Split and Duplicate produce one, and a binding is part of the look
        # those two copy: `SHOT_PLAN_CONTENT_FIELDS` says the two halves of one shot are one
        # shot's look, and a half that lost its binding would move differently from its own other
        # half. So the bindings this Shot ends up with are the stored card's, whatever the body
        # says about them — invented, altered, relocated or dropped.
        #
        # `elsewhere` and never `own`: this branch is only reached for a Shot the store does not
        # hold, so every card here was copied off a sibling and every one is minted a new id. That
        # is the cloned-id resolution, at the door the clone arrives through.
        #
        # No `luts` and no validation of the stored side, which is what makes it free enough to
        # run on every ordinary save — and it is also A1's dissolution: the old check validated
        # every stored Shot's stack, so one deleted `.cube` anywhere in the project refused every
        # Split of every bound Shot.
        adoption = adopted_effect_stack(
            stack,
            elsewhere=[spec for was_shot in stored.values() for spec in was_shot.effects],
            source=BINDING_CARRIER_PROJECT,
        )
        if adoption.refusal:
            raise HTTPException(
                status_code=422,
                detail=SHOT_BINDINGS_UNCARRIED_REFUSAL.format(
                    shot=shot_label(project, shot), detail=adoption.refusal
                ),
            )
        shot.effects = adoption.stack


def _adopt_shot_transitions(project: Project, stored: dict[str, Shot]) -> None:
    """Server-own `Shot.transition_in`/`transition_out` across the two generic whole-shot writes.

    **Written in the same commit as the fields** (AD-16), because this repository's own history
    says the alternative does not work: `routes/project.py`'s comments count fourteen findings of
    this exact hole, and the rule exists precisely because "add the adopt guard afterwards" never
    arrived. `_adopt_shot_effects` is the sibling this matches, and the failure is identical.

    It fails the two ways every one of its siblings does. Both fields default to `None`, so a
    client written before they existed -- which is every client until story 11.3, and every
    hand-rolled API call forever -- simply omits them, they arrive as `None`, and **one ordinary
    save would clear every transition in the project at once**. `PUT .../shots` makes that the
    likely path rather than the exotic one: **dragging a clip writes the whole shot list back**,
    and dragging is exactly the gesture that *authors* an Overlap, so a Director would set a
    dissolve, drag the clip to size it, and be told 200 while the type they had just chosen was
    thrown away. And a body that *invented* a transition would be writing a catalogue id through a
    route that never asks the catalogue whether it knows one, past `transition_definition` -- the
    one thing standing between a client's string and an `xfade` argument.

    **The stored pair is adopted whatever the body says, in both directions, for every Shot the
    store holds.** A Shot the store does **not** hold gets `None` for both, and that is the
    opposite of `_adopt_shot_effects`' decision about a new Shot -- deliberately, and it follows
    from `models.SHOT_UNINHERITED_DECISION_FIELDS`. A look describes the Shot's own frames and
    travels with them, which is why a Split's two halves must both keep it. A transition describes
    a **boundary between two named Shots**, and a Shot that did not exist a moment ago is on no
    such boundary: a Duplicate carrying `transition_out` would author a blend nobody dragged, and
    a Split's left half would claim one at the interior seam it makes with its own other half. So
    the answer for a new Shot is the anchor adoption's `""`-reading rather than the stack's, and
    for the same reason the anchor gives: a value that arrived on this route was not set by the
    Director on the route that sets them.

    No catalogue read and no validation on any path, which is what makes this free enough to run
    on every ordinary save: nothing client-supplied survives it, so there is nothing to validate.
    """
    for shot in project.shots:
        was = stored.get(shot.id)
        shot.transition_in = was.transition_in.model_copy() if was and was.transition_in else None
        shot.transition_out = (
            was.transition_out.model_copy() if was and was.transition_out else None
        )


#: Everything about a job that this application **recorded** rather than a client supplied. Named
#: as a set so `replace_project`'s guard and the test that proves it cannot drift apart: a field
#: added to `RenderJob`'s provenance block and not to this list is the next instance of the hole.
#:
#: **It was called `JOB_MEASURED_FIELDS` until 2026-08-23, and the rename is the whole of the
#: change** — same tuple, same order, same guard, same two loops below. "Measured" had stopped
#: describing two of the five: `render_frames` and `sampling_bundle` are written at *submission*,
#: at the one moment the graph's length and the graph's bundle are true, because both describe a
#: render that later edits and later settings make unrecoverable. A name that says "measured" asks
#: the next person deciding where a new field belongs the wrong question — "is this a
#: measurement?", to which `render_frames` answers no and belongs here anyway — and the eleven
#: recorded instances of this exact hole in `replace_project` are all a field that somebody
#: decided did not belong. The question the name has to ask is the one that actually governs:
#: **did this application produce the value, such that a client must not be able to move it?**
#: In either direction, since forging one plants provenance for a render nobody ran.
#:
#: The test that holds it is `test_every_field_a_settle_path_measures_is_covered_by_the_routes_guard`,
#: which reads `batch.stamp_job_settled`'s own assignments rather than matching field names. An
#: earlier version derived "measured" as `startswith("render_")`, which a `gpu_seconds` would have
#: slipped straight through — the same hole one level up from the one this tuple closes. It also
#: names the submission-written fields explicitly, so adding one is a deliberate act. Its sibling
#: `test_the_guard_has_teeth_for_every_field_it_names` forges each of these five through the
#: generic `PUT` on a job the store holds *and* on one it does not, and requires every one to come
#: back unmoved — so this rename is proved rather than assumed to be inert.
JOB_RECORDED_FIELDS = (
    "render_seconds",
    "render_seconds_source",
    "render_frames",
    "sampling_bundle",
    "look",
    "updated_at",
)


def _adopt_job_measurements(project: Project, stored: dict[str, RenderJob]) -> None:
    """Server-own every `JOB_RECORDED_FIELDS` entry on `RenderJob` across the generic
    whole-project write.

    The *function* keeps the name `_adopt_job_measurements` where the tuple lost "measured" on
    2026-08-23. It is the older and more visible of the two names — `RenderJob.render_frames`,
    `RenderJob.sampling_bundle` and `docs/WORKFLOW-MAP.md` all cite it by name — and it names a
    verb rather than a category, so unlike the tuple it never asks anybody a question it answers
    wrongly. Renaming both would have been one change more than the defect.

    **The eighth time this exact hole was found in `replace_project`** when this helper was
    written — the eleventh as of 2026-08-23, when `sampling_bundle` joined the list — and it fails the
    identical two ways `_adopt_expansion_maps`, `_adopt_song_recovery_slots` and the anchor, slot
    and location adoptions all describe. `render_seconds` and `render_frames` are defaulted
    numbers and `render_seconds_source` a defaulted string, so a client written before they
    existed — which is every client, including any hand-rolled API call — omits all three, they
    arrive as `0.0`/`0`/`""`, and **one ordinary save would erase every render timing in the
    project at once**: the exact loss this instrumentation exists to make impossible, arriving
    through the exact route that has caused it seven times before. And a body that *invented* a
    duration would be planting a measurement nobody took, which is what this whole change is a
    correction of.

    `sampling_bundle` is adopted on the identical argument and fails both ways at once. It is a
    defaulted `SamplingBundle | None`, so every client omits it, it arrives as `None`, and one
    ordinary save would strip the bundle off every job in the project — turning a library that had
    just become interpretable back into an undifferentiated mixture, through the same route. And a
    body that *invented* one would be worse than a blank: a forged `{"name": "turbo", "steps": 4}`
    on a job that ran twenty steps is provenance for a render nobody performed, which is the
    fabricated-figure failure this whole block of fields exists to prevent, wearing the costume of
    the fix. The default is forced for a record the store does not hold, exactly as below.

    `updated_at` is adopted with them because it is now evidence rather than bookkeeping — it is
    the settle moment, and half of the `record`-sourced span — and because a body that omits it
    gets a fresh `now_utc()` from the default factory, which would silently redate every settled
    job to whenever somebody last pressed save.

    `look` joined on 2026-08-25 with `RenderJob.look` itself, in the same commit as the field
    rather than after the first save that ate one, and it answers the tuple's own question the same
    way the four above it do: the assemble route composed that record from the plan it was about
    to run, no client ever supplies it, and it must not be moveable in either direction. Both ways
    it fails are the familiar ones. `ExportLook` is defaulted and every client that exists omits
    it, so it arrives as three empty lists and one ordinary save would erase the record of what
    every export in the project looked like — the very thing this field is for, deleted through
    the route that has now been the hole twelve times. And a body that *invented* one would be
    provenance for a grade nobody applied: `Shot.effects` is already server-owned on this route
    (`_adopt_shot_effects`), so an unguarded `look` would be the way to claim a look the manifest
    itself refuses to hold.

    `created_at` is deliberately **not** adopted: it is not measured, a client round-trips it
    faithfully, and adopting it would mean this route could never carry a job the store does not
    already hold. Nor is `status`, `error` or `progress` — each has its own writer and its own
    argument, and widening this beyond the measurements would be a different change hiding
    inside a guard.

    Two holes this guard had when it was first written, both of which protected *fields* while
    leaving whole *records* open, and both closed here.

    **A job the store does not hold gets the defaults, not the body's numbers.** It used to keep
    its own values, on the reasoning that there was nothing measured to protect — which is true
    of the record and false of the field. A body inventing `job_forged` with
    `render_seconds: 7920.0`, `render_seconds_source: "comfy"`, `render_frames: 221` persisted
    verbatim, and the queue panel drew `2h12m · 221f` with no `≤`: the fabricated "221 frames =
    2.2 hours" figure this instrumentation exists to retire, re-injectable through the same route,
    now dressed as a recorded measurement. So the precedents are followed exactly as they are
    written — `stored_anchors.get(id, "")`, `stored_slots.get(id, 0)` — and the default is forced.
    A record nobody measured carries no measurement.

    **A job missing from the body is kept.** The loop reads `project.jobs`, which is the *body*,
    so a job the body simply omits was invisible to it — and `Project.jobs` is defaulted, so a
    `PUT` carrying nothing but `id`, `name` and `updated_at` answered 200 and erased every job
    record in the project, timings included. Job records are not editorial content: no route in
    this application removes one, they are appended by the ten submission paths and settled in
    place, and the queue panel is the only thing that reads them. So the stored list is the
    authority for records the store holds, in the store's own order — which is submission order,
    which is what the panel draws as chronology — and the body may only *add* to it. What the body
    still decides for a job the store holds is every field above that this guard does not adopt.
    """
    kept = []
    body = {job.id: job for job in project.jobs}
    for job_id, was in stored.items():
        offered = body.get(job_id)
        if offered is None:
            kept.append(was)
            continue
        for name in JOB_RECORDED_FIELDS:
            setattr(offered, name, getattr(was, name))
        kept.append(offered)
    for job in project.jobs:
        if job.id in stored:
            continue
        for name in JOB_RECORDED_FIELDS:
            field = RenderJob.model_fields[name]
            setattr(job, name, field.get_default(call_default_factory=True))
        # `updated_at`'s default factory is `now_utc`, which would stamp a settle moment onto a
        # record that has never settled. Its documented way of saying "never settled" is
        # equality with `created_at` — see `RenderJob.updated_at` — so that is what it gets.
        job.updated_at = job.created_at
        kept.append(job)
    project.jobs = kept


def _require_approval_unchanged(project: Project, shots: list[Shot]) -> None:
    """Refuse a save that changes any of `APPROVAL_FIELDS`. Approval is the approve route's.

    The same narrow shape as `_require_in_flight_status_kept`, and for the same reason: an
    approved Shot round-trips through the interface's ordinary saves constantly, so the refusal is
    on a *difference* and never on the presence of the fields. An unchanged approval saves.

    AGENTS.md's rule is that `approved_output` is an explicit editorial decision, and the approve
    route is documented as its one writer -- "what is written is what the server resolved from its
    own manifest ... a path accepted from a client would be a claim". These two routes were the
    hole in that: a whole-manifest body binds a defaulted `Project`, so a client that has never
    heard of the field sends `""` and one ordinary save silently withdraws an approval, while one
    that invents a path plants assembly's input without any take behind it.

    A Shot the stored project does not hold is compared against the field's own default rather
    than skipped: nothing has approved a Shot that does not exist yet, so a new Shot arriving with
    an approval is claiming a decision no route made -- which is precisely how a duplicated Shot
    used to arrive owning the original's approval.

    Nothing here assigns an approval field. The gate compares and refuses, so the source scan that
    holds this application to two `approved_output` writes keeps reading two.
    """
    stored = {shot.id: shot for shot in project.shots}
    for shot in shots:
        was = stored.get(shot.id)
        for field in APPROVAL_FIELDS:
            before = getattr(was, field) if was is not None else Shot.model_fields[field].default
            if getattr(shot, field) != before:
                raise HTTPException(
                    status_code=409,
                    detail=GENERIC_WRITE_APPROVAL_REFUSAL.format(
                        shot=shot_label(project, was if was is not None else shot)
                    ),
                )


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
    # How much longer than the song asked for the encoder's latent ceiling is allowed to run.
    # `duration` is what the planner is told to write; `MiniMaxMusic3TextEncode.max_duration`
    # is only a ceiling the song may finish before, so passing the same number to both leaves
    # a song whose lyrics run long no room for its ending. The planner's input never moves —
    # the multiplier applies to the encoder's ceiling alone. See
    # `SONGPLANNER_DEFAULT_DURATION_HEADROOM` for why 1.5 is a default rather than a constant:
    # the creator documents the 50% rule and their own audited export contradicts it.
    # Floor 1.0 — a ceiling under the target can only truncate. Ceiling
    # SONGPLANNER_MAX_DURATION_HEADROOM — above it no duration this route accepts could stay
    # inside the encoder's 360 s schema maximum. Between the two, a product that does leave
    # the schema is refused by the adapter as a 422 naming both numbers, never clamped.
    duration_headroom: float = Field(
        default=SONGPLANNER_DEFAULT_DURATION_HEADROOM,
        ge=1.0,
        le=SONGPLANNER_MAX_DURATION_HEADROOM,
        description=(
            "Multiplier from the requested duration to MiniMaxMusic3TextEncode.max_duration, "
            "the encoder's latent ceiling. M3SongPlanner.duration_seconds still receives the "
            "requested duration unchanged: this only buys a song that runs long room for its "
            "ending. 1.0 gives the encoder exactly the target and no room."
        ),
    )
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


class SongVocalTypeRequest(BaseModel):
    """Who sings this track, and nothing else on the wire.

    One field, deliberately, and it is `SongContextRequest`'s argument in a smaller key: a body
    that could also carry `lyrics` would be a route that can rewrite the lyric sheet — and since
    the per-line singer marks live *in* the sheet, that would be a route that can invent a line's
    singer. It cannot, because the field is not on the wire.

    No default. Every other request model here defaults its fields so an omission is a blank, and
    that is exactly wrong for this one: the omitted value would be `"unstated"`, so a client that
    forgot the field would silently un-declare the Director's cast — the same shape as the
    defaulted-`str` hole the generic `PUT` has now been the site of five times. A body without a
    vocal type is a 422 here, which is the loud version of the same mistake.
    """

    vocal_type: VocalType


class AssetCharacterSlotRequest(BaseModel):
    """Which singer this character asset is, as a slot number. `0` clears the slot.

    Bounded by the schema at `CHARACTER_SLOT_LIMIT`, which is derived from `VOCAL_TYPE_SPECS`, so
    a client cannot reach past the largest slot any dropdown can offer into a number no `(S1)`
    mark could ever name.
    """

    character_slot: int = Field(ge=0, le=CHARACTER_SLOT_LIMIT)


#: Refused by name rather than stored as a number that resolves nothing. A slot says "this is the
#: singer S1 refers to", and a prop or a setting is not a singer.
CHARACTER_SLOT_NOT_A_CHARACTER = (
    "{name} is a {kind} asset, and a character slot names one of the song's singers. Only "
    "character assets can hold a slot."
)

#: Two assets in one slot makes `(S1)` ambiguous, and an ambiguous reference is the fabricated
#: citation this codebase refuses. The refusal names the holder, because "take it off them first"
#: is the action and a refusal that does not say whose slot it is cannot be acted on.
CHARACTER_SLOT_TAKEN = (
    "Slot S{slot} is already held by {name}. A slot names exactly one character, or a tagged line "
    "would point at two — clear that asset's slot first, or give this one a different number."
)


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


# --------------------------------------------------------------------------------------------
# Asset Fill / the Stage Manager (the Director's user workflow, stage 3).
# --------------------------------------------------------------------------------------------

ASSET_FILL_CONFIRM_REFUSAL = (
    "Asset Fill would queue up to {count} Flux image render(s) proposed by the Stage "
    "Manager. Send confirm_gpu=true to proceed."
)
ASSET_FILL_RENDERS_OPEN_REFUSAL = (
    "Renders are in flight. Filling assets now would interleave Flux renders into the "
    "queue and evict the resident video model stack (~150 s per eviction, FR-9). Let "
    "the queue settle first."
)
ASSET_FILL_NO_PROPOSALS_REFUSAL = (
    "The Stage Manager proposed no assets. Its message: {message}"
)


class AssetFillRequest(BaseModel):
    """One Stage Manager pass: how many proposals at most, and the GPU acknowledgement."""

    count: int = Field(default=8, ge=1, le=16)
    confirm_gpu: bool = False


class AssetFillSubmission(BaseModel):
    asset_id: str
    name: str
    kind: str
    job_id: str


class AssetFillResponse(BaseModel):
    """The Stage Manager's own reasoning plus what actually queued — each proposal is an
    ordinary generated asset (keep, delete, AI Mod) once its render lands."""

    message: str
    submitted: list[AssetFillSubmission]


# --------------------------------------------------------------------------------------------
# Generate All (FR-4, AD-5) — the route's own wordings; the per-shot skip sentences come
# from the single-shot handlers verbatim, and the two protection sentences from batch.py.
# --------------------------------------------------------------------------------------------

#: The server-enforced half of "Warning on time/GPU": a client that never showed the
#: warning cannot spend hours of GPU by omission. Names the count, per FR-4's testable
#: consequence, and the measured per-shot range so the Director can do the arithmetic.
GENERATE_BATCH_CONFIRM_REFUSAL = (
    "This batch would queue {count} H3 render(s). A reference shot measured 288-438 s on "
    "the default profile; turbo-references2v measured about 2.0x faster at production "
    "length (2026-08-23), and turbo is unmeasured at that length. Whichever bundle this "
    "project is set to, this is a real GPU commitment. Send confirm_gpu=true to proceed."
)
GENERATE_BATCH_EMPTY_READY = (
    "No shots are ready to generate. Mark shots ready first — or tick Replace existing "
    "takes to re-render settled shots."
)
GENERATE_BATCH_EMPTY_FLAGGED = "No shots are flagged for re-render."
#: The `empty` scope's own nothing-to-do sentence. Says both halves of why the scope came back
#: empty, because both are reachable and they call for different next actions: every shot may
#: already hold a take, or the takeless ones may all be protected (a locked shot with no take is
#: an ordinary state). The browser says the same thing before the request — see
#: `GENERATE_EMPTY_NONE` — so this is the backstop for a client that did not ask first.
GENERATE_BATCH_EMPTY_WITHOUT_TAKES = (
    "Every shot already has a video, or the ones without are locked or approved. Nothing to "
    "generate."
)
#: One sentence per scope, looked up rather than chained, so a fourth scope cannot inherit the
#: `ready` wording by falling off the end of an if/else. The `.get` default is still `ready`'s,
#: because `GenerateBatchRequest.scope` cannot hold anything this table does not name.
GENERATE_BATCH_EMPTY_BY_SCOPE = {
    "ready": GENERATE_BATCH_EMPTY_READY,
    "flagged": GENERATE_BATCH_EMPTY_FLAGGED,
    "empty": GENERATE_BATCH_EMPTY_WITHOUT_TAKES,
}

#: What a batch re-render adds to the shot's seed, so the retake differs from the take
#: the Director just rejected. A named odd stride rather than +1 so it cannot collide
#: with populate's 1..N first-render seeds for many rounds.
RESUBMIT_SEED_STRIDE = 101


# --------------------------------------------------------------------------------------------
# Populate Timeline (the Director's user workflow, stage 4 — spec-populate-timeline).
# --------------------------------------------------------------------------------------------

#: The Director's own warning, server-enforced: the button's dialog shows this and the
#: route refuses without the acknowledgement, so no client can replace a timeline by
#: omission. The wording is the user workflow's: first run, or a deliberate redo.
POPULATE_CONFIRM_REFUSAL = (
    "Populate Timeline lays out the whole plan from the Song, Treatment and Assets — "
    "every existing shot is replaced and unsaved timeline work is lost. It is intended "
    "for a first run on an empty timeline, or for deliberately redoing the plan after "
    "reworking the song, treatment or assets. Send confirm_replace=true to proceed."
)
POPULATE_NO_SONG_REFUSAL = (
    "Populate Timeline lays shots across the song, so the project needs a song with a "
    "known length first."
)
#: Snap Cuts places boundaries against measured voice, so it needs the track it measures.
#: Stated separately from the populate refusal rather than shared, because the two ask for
#: different things: populate needs a *length* to tile, this needs a *recording* to have
#: heard. The follow-on refusal a Director hits after supplying one is
#: `timeline.SNAP_UNMEASURED`, which sends them to Analyze structure.
SNAP_CUTS_NO_SONG = (
    "Snapping cuts places them where the track is not singing, so this project needs a "
    "master song first."
)
POPULATE_PROTECTED_REFUSAL = (
    "Populate Timeline replaces every shot, and {shots} carry protections (approval or a "
    "lock) that must not vanish silently. Un-approve or unlock them first — or delete "
    "them if the plan is truly being redone."
)
POPULATE_NO_PLAN_REFUSAL = (
    "The Director model returned no shots to lay out. Its message: {message}"
)

#: The second-failure refusal: the model was told the number, told again with the shortfall
#: named, and still under-delivered. Named counts and a way forward, like every other
#: refusal here — and it says plainly that nothing was replaced, because the Director's
#: whole timeline was the thing at risk.
POPULATE_SHORT_PLAN_REFUSAL = (
    "The Director model was asked for {required} shots to cover {duration:.1f} seconds and "
    "returned {returned} — twice, the second time with the shortfall spelled out. A plan "
    "that short would repeat each prompt across many windows, so nothing was replaced and "
    "your timeline is as it was. Mark the song's sections first so each one is planned on "
    "its own smaller ask, or point MVP_LLM_MODEL at a larger model and try again."
)

#: See the populate route's window_mean comment: the creator's "fastest / safest" preset, and
#: comfortably inside the flat region of the measured per-frame cost curve, which runs flat out
#: to 6.79 s and only then climbs. The curve is on `POPULATE_MAX_WINDOW_SECONDS`, and so is the
#: history of the three earlier justifications it replaced. This target does not move with the
#: ceiling: it is what the model is *steered* toward, where the ceiling is what is enforced.
POPULATE_TARGET_WINDOW_SECONDS = 5.2

#: The *enforced* ceiling the tiling repair applies, tighter than H3's 15 s legality. It rests
#: on a measured cost curve and on a Director ruling, and both are recorded here because every
#: earlier justification for this constant turned out to be false.
#:
#: **The measured curve (2026-08-22).** `turbo-references2v`, sampling time only, one session,
#: warm, render order decorrelated from frame count (Spearman rho 0.0). See
#: `_bmad-output/planning-artifacts/h3-attention-backend-experiment.md` for the protocol.
#:
#: ====== ======== ========= ==============
#: frames window   s/frame   vs 6.08 s
#: ====== ======== ========= ==============
#: 158     6.083 s  1.025    —
#: 175     6.792 s  0.977    marginally *cheaper*
#: 192     7.500 s  1.448    +48%
#: 209     8.208 s  2.775    +184%
#: 226     8.917 s  3.558    +264%
#: ====== ======== ========= ==============
#:
#: **Per-frame cost is flat to ~6.79 s and then climbs steeply**, so 6.8 is the top of the free
#: region rather than a round number. The mechanism is visible rather than inferred: median
#: power falls 478 -> 227 W across that range while *max* power stays ~576 W, i.e. the card
#: keeps full capability and spends progressively more time waiting on memory.
#:
#: **Three false rationales this replaces, kept so they are not re-derived.** (1) A claim that
#: 221-frame windows "took 2.2 HOURS each" — it had **no primary record anywhere**, appearing
#: only in this comment and quoted onward as though that were corroboration; measured worst case
#: was 39 min. (2) "Acceleration is off, so the cliff is unmeasured" — backwards:
#: `PathchSageAttentionKJ` at `"disabled"` is a passthrough, and ComfyUI runs
#: `--use-sage-attention`, so SageAttention was on the whole time. (3) The whole original table
#: came from the **20-step `default` bundle** while the batch and Render Again silently use
#: different bundles; the curve above is the 8-step one.
#:
#: **Why a bound exists at all, which never depended on the timing.** Guidance alone failed: on
#: the first 5.2 s-target run the local model simply echoed the previous plan's 9 s windows out
#: of its own context. The bound is what the target *means*.
#:
#: **Why 6.8 rather than 6.0 (Director ruling, 2026-08-23).** At 6.0 the Chorus of the live song
#: averaged 5.955 s — 45 ms under the cap — so the largest variance *any* legal tiling of that
#: section could reach was 0.078, against 1.153 at a 6.8 cap. One section was pressed flat
#: against the ceiling, and the headroom that frees it is free. The 4 s floor does **not** move:
#: `over_render_frames` floors at 107 frames, so everything below ~3.27 s costs the same anyway.
POPULATE_MAX_WINDOW_SECONDS = 6.8

def populate_required_shots(duration: float) -> int:
    """How many shots a song of ``duration`` seconds needs — computed here, never asked.

    The arithmetic is the server's because it is arithmetic: the target window is a
    measured render-speed decision (`POPULATE_TARGET_WINDOW_SECONDS`) and dividing a
    number by it is not a creative act. Handing the model the finished number instead of
    "cover the song with 4–6 second shots" is the whole of the count-enforcement pattern's
    first part — the measured failure it answers is a local model that writes five shots
    for a three-minute song and calls the plan complete.

    This is the number stated in the prompt as a hard constraint *and* the number the
    reply is checked against, deliberately the same one: a prompt that asks for N and a
    checker that accepts N/2 teaches the model that N was decoration.
    """
    return max(1, round(duration / POPULATE_TARGET_WINDOW_SECONDS))


#: What the model is asked for. The count and the asset roster matter: a local model told
#: nothing about length writes five shots for a three-minute song, and one told nothing
#: about the library invents characters the project does not hold.
#:
#: The count appears three times on purpose — the opening sentence, the numbered hard
#: constraints, and the FINAL CHECK line appended last (`POPULATE_FINAL_CHECK`) — because
#: a single mention in a long instruction is what this model family demonstrably reads
#: past. The pattern's canonical third site is the system prompt; `SYSTEM_PROMPT` is
#: shared with the chat route, so populate's own opening sentence takes that slot rather
#: than teaching every chat turn about shot counts.
POPULATE_INSTRUCTION = (
    "Lay out the complete shot plan for this music video. Return EXACTLY {count} shots. "
    "The song is {duration:.1f} seconds long; cover it entirely from 0 to {duration:.1f} "
    # The two numbers are read off the enforced band rather than spelled again, because they
    # have already drifted once: the ceiling moved 6.0 → 6.8 (Director ruling, 2026-08-23) and
    # this steering text went on saying "4 and 6" for a day. Numerically it was harmless —
    # `populate_windows` scales every proposal to its span, so only the *ratios* the model
    # returns survive — but an instruction that contradicts the band it is enforcing against is
    # the kind of thing a later reader trusts.
    f"with contiguous shots — no gaps, no overlaps — each between {H3_MIN_SHOT_SECONDS:g} and "
    f"{POPULATE_MAX_WINDOW_SECONDS:g} seconds. "
    f"Deliberately mix lengths inside that band: quick {H3_MIN_SHOT_SECONDS:g}-second cuts on "
    f"high-energy beats, {POPULATE_MAX_WINDOW_SECONDS:g}-second holds on glamour or "
    "establishing moments; do not make "
    "every shot the same length. Follow the treatment and style bible; use the song's "
    "lyrics to place performance moments where the words are. Every shot carries its own "
    "`performance` flag: answer it on each shot — true where a character sings the song "
    "on camera, false everywhere else. It is a field of the shot object and never words "
    "in the shot: no prompt may mention that flag or its value. The project's assets, "
    "by name, are: {assets}. Every shot carries its own `assets` list: put in it the "
    "exact names of the assets that shot uses, and nothing that is not on that list. "
    "That list is the ONLY place an asset may be named — it is what attaches the "
    "picture to the shot, so a shot that uses one and does not list it renders without "
    "it. The prompt itself must never contain one of those names: they are internal "
    "library labels, not words in a script, and a prompt reading \"Extreme close up of "
    "Crimson Lips Close-up\" or \"Blue Haze Atmosphere surrounding the Dusk Warehouse "
    "Bed\" is a shot list written as an inventory. Describe what is in frame in your own "
    "words — the smeared red lips, the blue haze, the dark silk of the bed — and let "
    "`assets` say which picture it is. A shot that uses nothing from the library returns "
    "an empty `assets` list. The one exception is a character: a character asset's name "
    "is the performer's name, so write it in the prompt exactly as you list it in "
    "`assets` — the prose has to say who is in frame. Every shot's prompt is a "
    "short readable visual intent (one or two sentences): what is seen, who is in "
    "frame, how the camera behaves. Vary the camera angle and movement between "
    "adjacent shots — never repeat the same setup, framing or camera move back to "
    "back, and not every shot needs movement at all.{sections_ask}\n"
    "HARD CONSTRAINTS:\n"
    "1. `shots` must contain EXACTLY {count} entries. Not fewer. A shorter list is a "
    "failed answer, however good the individual shots are.\n"
    "2. The shots must run in order and together cover 0 to {duration:.1f} seconds.\n"
    "3. Every shot needs a non-empty `prompt`.{sections_constraint}"
)

#: The structure half of the single-call ask, factored out so it can be *dropped*. It is
#: sent only when the section layer is still unknown at shot time. When the Director has
#: marked the boxes, or when the sections-first stage has already filled them, asking for
#: them again is a second job bolted onto the one that matters — which is the measured
#: complaint the two-stage split exists to answer.
POPULATE_SECTIONS_ASK = (
    " First divide the song into sections by its structure (Intro, Verse, Chorus, "
    "Bridge, Outro), matching the lyric sheet's own [Tag] blocks in order, each with "
    "start and duration in seconds and a one-sentence shared visual prompt; return them "
    "in `sections`. Then lay the shots out inside those sections."
)
POPULATE_SECTIONS_CONSTRAINT = (
    "\n4. `sections` must not be empty — return the song's structure blocks there."
)

#: The declared location, named to the model when the Director has declared one
#: (`Project.default_setting_id`). Appended by the route rather than interpolated into
#: POPULATE_INSTRUCTION, so the instruction's `format` keys are unchanged and the live smoke
#: scripts that call it directly keep working.
#:
#: Newline-led, and that is load-bearing for exactly the reason the section map's is: this text
#: lands after "3. Every shot needs a non-empty `prompt`.", and a leading space glued it onto the
#: end of a numbered hard constraint where it read as a continuation of one (found live,
#: 2026-08-20).
#:
#: It is the *soft* half of the setting fix and is expected to be unreliable — this model family
#: names what it feels like naming, which is the measured defect (a location cited by 5 of 30
#: shots). The half that does not depend on the model is `models.with_default_setting`, which
#: gives the location to any shot that did not name one.
POPULATE_LOCATION_LINE = (
    "\nThe video's location is {name}. Every shot that plays there lists it in its "
    "`assets`, by that exact name, and describes the place in its own words rather than "
    "writing that name in the prompt."
)

#: Stage one of the two-stage populate: structure only, from the lyric sheet. Deliberately
#: a small ask with its own hard constraints and its own closing check, because small is
#: the entire hypothesis — the roadmap's run-2 measurement is that this model family will
#: not emit `sections` beside a 32-shot layout (three rolls, zero sections) while it
#: volunteers them happily in smaller replies. The lyric sheet is not pasted in here: it
#: already rides the project context this call is given, and a second copy in the request
#: text is the "JSON in context begets JSON" degradation this codebase has been bitten by.
POPULATE_SECTIONS_INSTRUCTION = (
    "Divide this song into its structural sections, and return ONLY that structure. The "
    "song is {duration:.1f} seconds long. Work from the lyric sheet's own [Tag] blocks in "
    "order — Intro, Verse, Chorus, Bridge, Outro, and whatever else it names — one "
    "section per block, in order, each with `start` and `duration` in seconds, together "
    "covering 0 to {duration:.1f}, and each with a one-sentence shared visual prompt "
    "saying how that part of the song looks. Return them in `sections`.\n"
    "HARD CONSTRAINTS:\n"
    "1. `sections` must not be empty.\n"
    "2. The sections must run in order and must not overlap.\n"
    "3. Leave `shots` empty. This call is about structure only — the shots are asked for "
    "separately, afterwards.\n"
    "FINAL CHECK before responding: is `sections` non-empty and in song order? If it is "
    "not, fix it, and return only the corrected structure."
)

#: The closing line, appended after everything else so it is the last thing the model
#: reads. Verbatim in spirit from the pattern this ports: state the number, tell it to
#: count, tell it to fix the answer rather than explain it.
POPULATE_FINAL_CHECK = (
    "\nFINAL CHECK before responding: count the entries in `shots`. It must equal "
    "{count}. If it does not, add or remove shots until it does, and do not explain — "
    "just return the corrected list."
)

#: The guided retry, the pattern's fourth part and this codebase's `H3_RETRY_PROMPT`
#: idiom: the failure is named in concrete numbers ahead of the request it is correcting,
#: so the model rewrites against a stated fault instead of rerolling blind.
POPULATE_RETRY_PREFIX = (
    "PREVIOUS ATTEMPT FAILED and was discarded. What was wrong with it:\n{problems}\n"
    "Answer the same request again and fix every one of those problems. Keep whatever was "
    "already right.\n\n"
)
POPULATE_SHORT_COUNT_PROBLEM = (
    "- It returned only {returned} shot(s) in `shots`. The plan needs exactly {required}."
)
#: Named alongside the shortfall when the model was asked for structure and emitted none.
#: A dropped field is reported the same way a short count is — the recorded failure mode
#: here is a model whose narration claims it set fields it silently omitted, so the check
#: reads the reply rather than the message.
POPULATE_MISSING_SECTIONS_PROBLEM = (
    "- It returned an empty `sections`. The song's structure blocks (Intro, Verse, "
    "Chorus, Bridge, Outro) were asked for and omitted entirely."
)

#: One retry, not `EXPANSION_ATTEMPTS`' four. Expansion retries one *shot* against a
#: format checker, and a whole sweep of those runs unattended; populate is one click the
#: Director is sitting in front of, and a single call against this project's local model
#: is recorded at up to a 300 s timeout. Four attempts is twenty minutes of a button that
#: looks stuck. One guided retry buys the pattern's measured benefit; the second failure
#: is worth reporting rather than grinding at.
POPULATE_ATTEMPTS = 2

#: The retry's temperature. Lower than `PLAN_TEMPERATURE` because the retry is not asking
#: for another creative roll — the fault has been named in numbers, and what is wanted now
#: is obedience to a count.
POPULATE_RETRY_TEMPERATURE = 0.2


# ------------------------------------------------------------------------------------------
# The three steps of a populate — lay it out, line it up, fill it in.
#
# The Director's model (2026-08-21): *"Populating the timeline is definitely a multi-step
# process.. Laying it out, lining it up, filling it in."* The three functions below are those
# steps, and they are the **only** implementation of them: `populate_timeline` chains them,
# and `lay_out_timeline` / `line_up_timeline` / `fill_in_timeline` each expose one of them on
# its own route. Nothing re-implements a step for the chain's benefit — this codebase has been
# bitten twice by one rule with two implementations (the reference-map numbering, the
# readiness/submit staleness test), and a chain that drifted from its own steps would make the
# steps decorative.
#
# They are module-level and not closures on purpose: that is what lets a test monkeypatch one
# step and observe the chain calling it, which is the only way "the chain is a caller" is a
# fact rather than a claim.
#
# **Each step takes the previous step's output as data.** No step reads a field the previous
# one did not put on its intermediate, and no step reaches back into the request or the store.
# The intermediates are `ShotLayout` and `ShotAlignment` below; the third step's output is an
# ordinary `list[Shot]`.
#
# **The model call lives in lay-out, and there is exactly one of it.** The single call answers
# both "how many shots and how long" (lay-out's question) and "what is in them" (fill-in's), and
# splitting it into two asks is deliberately *not* this change: the combined ask was measured on
# 2026-08-20 to deliver both halves on 0 of 9 rolls, and unpicking that is its own phase with
# its own live measurement. So lay-out owns the call because lay-out owns the count it is held
# to and the retry that enforces it, and fill-in receives the content half as data —
# `ShotLayout.proposals`, an opaque payload lay-out itself never reads for anything but the
# count. That is the whole of "how the other step receives what it needs, without a second
# call".


@dataclass(frozen=True)
class ShotProposal:
    """One shot as the model proposed it, frozen into a record the next steps can read.

    The director reply is duck-typed at every call site — a double that predates
    `performance` or `assets` must keep working — so the `getattr` defaults live in
    `lay_out_shots` where the reply is read, once, and everything downstream sees these five
    fields and nothing else. `prompt` is the raw string, unstripped: fill-in strips it for
    `Shot.prompt` and scans it unstripped for asset names, exactly as populate always has.
    """

    start: float
    duration: float
    prompt: str
    performance: bool = False
    assets: tuple[str, ...] = ()


@dataclass(frozen=True)
class ShotLayout:
    """Step one's output: the *structure*, plus the model's content payload carried forward.

    `project` is the re-read project — lay-out re-reads across its own model call, because a
    local model can hold that call open for minutes — and it is the object the later steps
    read the library, the identity sheets and the song from, and the one a caller saves.
    `sections` is the section layer as laid out (already assigned onto `project`), and
    `windows` is the contiguous tiling. `proposals` is the content half of the single model
    call, untouched by this step beyond the count check and the sort.
    """

    project: Project
    duration: float
    required: int
    proposals: tuple[ShotProposal, ...]
    windows: tuple[tuple[float, float], ...]
    sections: tuple[SongSection, ...]
    #: Where the section layer came from: `"director"` (boxes already marked), `"structure"`
    #: (the two-stage structure pass), `"shots"` (volunteered by the shots call) or `""`.
    sections_origin: str
    #: The model's narration. Carried for the report only; nothing decides on it — the
    #: recorded failure mode here is a model that narrates fields it did not emit.
    message: str
    #: How much of the room H3's band leaves this layout spent on length variance
    #: (`timeline.POPULATE_VARIANCE_DEFAULT`). Carried because it is an *input* that shaped the
    #: windows and a report that did not name it could not be reproduced from itself. Defaulted
    #: to 0 — the neutral value — so a layout rebuilt from a report written before Phase D says
    #: "no variance was spent" rather than claiming today's default was.
    variance: float = 0.0


@dataclass(frozen=True)
class ShotPlacement:
    """One window, where line-up put it, with the musical facts measured against it.

    `vocal_seconds` is `timeline.shot_vocal_overlap`: seconds of measured voice inside the
    window, and **`None` means unmeasured, not silent** — the codebase's absent-analysis
    convention. `voiceless` is the one-directional guard fill-in consumes: a window the track
    is measured to leave voiceless cannot be marked singing, whatever the model declared.

    `lines` are the sung lines of the lyric sheet this window covers, in song order, each
    with the seconds it was heard at and the singer slots its `(S1)` mark names
    (`timeline.align_lyric_lines`). Empty is the honest answer in three different situations
    and the report says which: an unmeasured song, a window in an instrumental stretch, and a
    line whose words were all misheard. **Nothing here chooses anything** — this is the seam
    the multi-character work reads, and a citation chosen from a singer slot is fill-in's act,
    deliberately not built here.
    """

    index: int
    start: float
    duration: float
    #: The label of the section containing this window, `""` when it sits outside them all.
    section: str
    vocal_seconds: float | None
    voiceless: bool
    #: The sheet lines sung across this window, in song order. `timeline.LyricLineSpan`
    #: verbatim, because a line's identity, text, mark and timing are one fact and a second
    #: shape for them here would be a second place for them to disagree.
    lines: tuple[LyricLineSpan, ...] = ()

    @property
    def singers(self) -> tuple[int, ...]:
        """Every character slot the sheet marks as singing across this window, ascending.

        A **projection of `lines`**, computed rather than stored, so it cannot drift from
        them: the wire row carries it for a reader, and the wire's own value is written from
        this property and read back through it.

        `()` means *no line across this window carries a mark* — untagged, which is the state
        of every sheet written before per-line marks existed. It is not "slot 1": an unstated
        value is never read as a stated one.
        """
        return tuple(sorted({slot for line in self.lines for slot in line.slots}))


@dataclass(frozen=True)
class ShotAlignment:
    """Step two's output: the layout **moved onto the music**, with per-window facts attached.

    Line-up consumes the alignment rather than standing beside it (Phase B, 2026-08-21). Each
    cut in the tiling is offered the nearest moment the track leaves voiceless, through the
    same `timeline.snap_window_plan` the snap-cuts route reaches by its own door, and `moved`
    is how many took it. `status` is that core's honest four-way answer — `"ready"`,
    `"off"` (tolerance 0), `"unmeasured"` (nothing heard on this track) or `"no_cuts"` — and
    three of those four mean nothing was examined.

    `moves` and `skips` are the whole report: every cut that would move and every cut that
    would not, with the sentence saying why. They are what makes a standalone line-up
    report-then-confirm in the same words `snap-cuts` uses; in a chained populate they ride
    along as the record of what the pass did to a timeline nobody had seen yet.
    """

    layout: ShotLayout
    placements: tuple[ShotPlacement, ...]
    #: Whether the song carries measured voice activity at all. False means every
    #: `vocal_seconds` is `None`, and fill-in's voiceless guard is a no-op.
    measured: bool
    moved: int = 0
    #: `CutSnapPlan.status`, carried rather than re-derived from the counts: "0 moved" is a
    #: different statement from "snapping was off" and from "nothing was ever heard".
    status: str = "ready"
    tolerance: float = 0.0
    moves: tuple[CutMove, ...] = ()
    skips: tuple[CutSkip, ...] = ()


def lay_out_protections(project: Project) -> None:
    """The refusals lay-out owns, in the order populate has always asked them.

    Asked once, by the step that would violate them. Lay-out is the destructive step — it
    replaces the windows — so the song check, the in-flight check and the locked/approved
    check are its, and fill-in deliberately does not inherit them: fill-in touches no window,
    which is what will later make re-running content against approved timing safe.

    Shared by the `lay-out` route and by the chain rather than written twice. The *consent*
    (`confirm_replace`) is not here, because the two callers legitimately answer it
    differently — see `lay_out_timeline` and `populate_timeline`.
    """
    if not project.song or project.song.duration <= 0:
        raise HTTPException(status_code=422, detail=POPULATE_NO_SONG_REFUSAL)
    if reconcilable_jobs(project):
        raise HTTPException(
            status_code=409,
            detail="Renders are in flight; populate would replace the shots they "
            "are rendering for. Let the queue settle first.",
        )
    protected = [
        shot_label(project, shot)
        for shot in project.shots
        if shot.locked or shot.approved_output or shot.status == "approved"
    ]
    if protected:
        raise HTTPException(
            status_code=422,
            detail=POPULATE_PROTECTED_REFUSAL.format(shots=", ".join(protected)),
        )


async def lay_out_shots(
    project: Project,
    *,
    director: Any,
    two_stage: bool,
    reread: Callable[[], Project],
    variance: float = POPULATE_VARIANCE_DEFAULT,
) -> ShotLayout:
    """Step one — lay it out. The model call, the count enforcement, and the tiling.

    Every decision in here is the one `populate_timeline` made before this split, moved
    without a change: the roster the model is offered (`citable_assets`, not the whole
    library), the context dump filtered to the same rows, the optional structure-first pass,
    the instruction assembled in the same order with the same fragments, the single guided
    retry that only a short count buys, the two refusals for an empty or short plan, the
    re-read across the await with its staleness 409, the section layer's three sources, and
    the per-section tiling that keeps a shot from straddling a boundary.

    `reread` is how this step re-reads the project after its own await — the store lives on
    the app's closure and this function does not — and the fresh project it returns rides out
    on `ShotLayout.project`, because that is the one being written to.

    **`variance` is Phase D**, the Director's standing complaint that shot lengths all look the
    same answered as a parameter rather than as a hoped-for property of the model's reply. It is
    a *fraction of the room H3's band leaves*, spent by reshaping each section's windows around
    how busy the singing is inside each of them — see `timeline.vocal_density` for the driver
    and the three drivers rejected for it, and `timeline._varied_durations` for the arithmetic.
    **0 is the feature off and a genuine no-op**, byte for byte the tiling this application laid
    before Phase D, and it is the control arm the byte digests are pinned through. So is an
    unmeasured song, on its own terms: no word times, no density, no variance, no guess.

    The probe is built once from the **re-read** project — the song these windows are being
    tiled against — and each section closes over its own start, because `populate_windows`
    speaks the coordinates of the span it is tiling and the song's seconds are what the words
    were heard at.

    Nothing here writes. `project.sections` is assigned on the re-read object and the caller
    decides whether that object is saved, which is what makes the report path of the `lay-out`
    route free of a store call rather than merely careful about one.
    """
    duration = project.song.duration if project.song else 0.0
    # The roster the model is offered, and it is `citable_assets` rather than the whole
    # library on purpose. A promoted identity sheet is no longer separately citable — a
    # citation of its source resolves to it below — so offering its display name buys
    # nothing and costs the naming leak the Director reported: `"Close up on eyes of
    # HarderFaster · multiview with flickering light reflections"`, an internal asset label
    # sitting in a shot's creative prose, written there because citation correctness used
    # to depend on the model typing that label. A name never shown cannot be echoed.
    citable = citable_assets(project)
    assets = "; ".join(f"{asset.name} ({asset.kind})" for asset in citable) or "none yet"
    # The count comes from `POPULATE_TARGET_WINDOW_SECONDS`, the target window populate
    # steers the model toward and thereby the plan's typical shot length. NOT the
    # midpoint of H3's 4–15 s training range: the creator's own preset table calls
    # 5.17 s (124 frames) "fastest / safest", and the instrumented curve of 2026-08-22
    # measured why — per-frame sampling cost is flat out to 175 frames (6.79 s) and then
    # climbs +48% at 192, +184% at 209 and +264% at 226.
    #
    # **Twice corrected, and both corrections are kept on `POPULATE_MAX_WINDOW_SECONDS`.**
    # This comment once said the long windows "took 2.2 HOURS each" — a claim with no primary
    # record anywhere, wrong by roughly 3.4x (2026-08-21) — and the mtime table that replaced
    # it was itself measured on the wrong workflow bundle (2026-08-22). The curve above is the
    # sampling-time one, and the ceiling it supports is 6.8 s by the ruling of 2026-08-23.
    #
    # ~5 s cuts also edit better for music video than 9 s holds.
    required = populate_required_shots(duration)
    context = project.model_dump(mode="json", exclude=DIRECTOR_CONTEXT_EXCLUDE)
    # The roster's rule applied to the context dump, which is the other place the model can
    # read an asset's name from — and the place it would still have read `· multiview` from
    # if only the instruction had been trimmed. Done here, on populate's own copy, rather
    # than in `DIRECTOR_CONTEXT_EXCLUDE`: that mapping withholds *fields* from every caller,
    # and this withholds *rows* from this one. The chat route's dump is untouched.
    citable_ids = {asset.id for asset in citable}
    context["assets"] = [
        asset for asset in context["assets"] if asset["id"] in citable_ids
    ]
    # Stage one of the two-stage populate, opt-in and skipped entirely when the
    # Director has already marked the boxes: structure first, on its own, from the
    # lyric sheet. It gets exactly one call and no retry — its whole premise is that a
    # small ask succeeds where the combined one did not, so spending a second 300 s
    # call to re-ask a small question would be arguing with the premise. If it comes
    # back empty, `wants_sections` stays true below and the shots call asks for the
    # structure the way it always has; nothing is lost but the one call.
    staged_sections: list[SongSection] = []
    if two_stage and not project.sections:
        try:
            structure = await director.plan(
                message=POPULATE_SECTIONS_INSTRUCTION.format(duration=duration),
                project_context=context,
                # This stage's *whole* output is `sections`, so that is what its
                # schema requires — and `shots` is pointedly left optional, because
                # HARD CONSTRAINT 3 asks this call to leave it empty and a schema
                # that demanded it would be arguing with the instruction beside it.
                # See `director_result_schema` for why the required set is the fix.
                response_schema=director_result_schema(require=("sections",)),
            )
        except DirectorUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except DirectorError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        staged_sections = [
            SongSection(label=label, start=start, duration=length, prompt=prompt)
            for label, start, length, prompt in repair_sections(
                [
                    (item.label, item.start, item.duration, item.prompt)
                    for item in structure.sections
                ],
                duration,
            )
        ]
    # Structure is only *asked for and demanded back* in the shots call when it is
    # still unknown. Boxes the Director marked, or boxes stage one just produced, make
    # the ask a second job bolted onto the one that matters — and an empty `sections`
    # in the reply is then not an omission, so naming it in a retry would be noise.
    known_sections = staged_sections or project.sections
    wants_sections = not known_sections
    instruction = POPULATE_INSTRUCTION.format(
        duration=duration,
        count=required,
        assets=assets,
        sections_ask=POPULATE_SECTIONS_ASK if wants_sections else "",
        sections_constraint=POPULATE_SECTIONS_CONSTRAINT if wants_sections else "",
    )
    # The declared location, named before the section map so the two read as project fact
    # then song structure. Only when there is one: nothing is invented, and a project with
    # no declaration sends the instruction it has always sent, character for character.
    declared_location = default_setting_asset(project)
    if declared_location is not None:
        instruction += POPULATE_LOCATION_LINE.format(name=declared_location.name)
    if known_sections:
        section_map = "; ".join(
            f"{section.label} {section.start:.1f}-{section.end:.1f}s"
            + (f" ({section.prompt})" if section.prompt else "")
            for section in known_sections
        )
        origin = (
            "marked by the director"
            if project.sections
            else "just laid out in the structure pass"
        )
        instruction += (
            # Newline, not a leading space, and it is load-bearing: this branch runs
            # only when `known_sections` is truthy, which is exactly when
            # `sections_constraint` is empty — so a space glued the section map onto
            # the end of "3. Every shot needs a non-empty `prompt`." and it read as a
            # continuation of that constraint. Both neighbouring fragments
            # (POPULATE_SECTIONS_CONSTRAINT, POPULATE_FINAL_CHECK) open with "\n";
            # this one was the outlier. Found by the 2026-08-20 live run.
            f"\nThe song's sections, {origin}, are: "
            f"{section_map}. Shots must respect these boundaries — every shot sits "
            "inside one section and takes that section's character."
        )
    # Last, after the section map, so the count is the final thing the model reads.
    instruction += POPULATE_FINAL_CHECK.format(count=required)
    # The pattern's third and fourth parts: verify in code, then one guided retry with
    # the fault named. Only the *count* buys the retry. A dropped `sections` rides
    # along in the corrective feedback when a retry is being spent anyway, but does
    # not trigger one on its own: sections are scaffolding the Director drags, this
    # model family drops them on most rolls (the roadmap's run-2 measurement), and a
    # check that fires on most rolls is a check that doubles every populate's cost.
    attempt_message = instruction
    result = None
    proposals: list[Any] = []
    for attempt in range(1, POPULATE_ATTEMPTS + 1):
        try:
            result = await director.plan(
                message=attempt_message,
                project_context=context,
                temperature=(
                    PLAN_TEMPERATURE if attempt == 1 else POPULATE_RETRY_TEMPERATURE
                ),
                # The shots call cannot proceed without `shots`, so the grammar it is
                # decoded under must say so. Everything above this line — the count
                # stated three times, the final check, the guided retry — asks in
                # *words* for a field the schema did not require, and the constrained
                # decoder was free to close the object without it. That is the whole
                # of the measured `shots: []` failure.
                #
                # The required set follows the instruction rather than being fixed:
                # `sections` is demanded back only when `wants_sections` put the ask
                # and HARD CONSTRAINT 4 in the text above, so the grammar and the
                # words always agree about what this call owes. When the boxes are
                # already known the ask is dropped, and requiring a field nobody asked
                # for would make the model fabricate structure to close the object.
                #
                # Requiring both was measured, not assumed (2026-08-20, N=3 per arm):
                # the combined ask delivered shots *and* sections on 0 of 9 rolls
                # across runs 1–2 and on 3 of 3 with both fields required, at no cost
                # in shots or wall clock.
                response_schema=director_result_schema(
                    require=("shots", "sections") if wants_sections else ("shots",)
                ),
            )
        except DirectorUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except DirectorError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        proposals = sorted(
            (shot for shot in result.shots if shot.prompt.strip()),
            key=lambda shot: shot.start,
        )
        if len(proposals) >= required or attempt == POPULATE_ATTEMPTS:
            break
        # Read off the reply, never off `result.message`: the recorded failure mode is
        # a model that narrates fields it did not emit.
        problems = [
            POPULATE_SHORT_COUNT_PROBLEM.format(
                returned=len(proposals), required=required
            )
        ]
        if wants_sections and not result.sections:
            problems.append(POPULATE_MISSING_SECTIONS_PROBLEM)
        attempt_message = (
            POPULATE_RETRY_PREFIX.format(problems="\n".join(problems)) + instruction
        )
    if not proposals:
        raise HTTPException(
            status_code=502,
            detail=POPULATE_NO_PLAN_REFUSAL.format(
                message=((result.message if result else "") or "").strip()[:300]
                or "(empty)"
            ),
        )
    if len(proposals) < required:
        # Loudly, and with nothing written: the destructive replace below has not
        # happened yet, and a half-length plan laid over the Director's timeline would
        # be worse than no plan at all.
        raise HTTPException(
            status_code=502,
            detail=POPULATE_SHORT_PLAN_REFUSAL.format(
                required=required, duration=duration, returned=len(proposals)
            ),
        )
    # Re-read after the await — the model can hold this open for minutes — and
    # re-check what matters before the destructive write: a protection set or a
    # render submitted while the model thought must still refuse.
    project = reread()
    if reconcilable_jobs(project) or any(
        shot.locked or shot.approved_output or shot.status == "approved"
        for shot in project.shots
    ):
        raise HTTPException(
            status_code=409,
            detail="The project changed while the model was planning (a render or a "
            "protection appeared). Nothing was replaced; try again.",
        )
    # Sections come from the Director's own boxes when marked, else from stage one's
    # structure pass, else from whatever structure the shots call happened to volunteer
    # (repaired: sorted, clamped, overlaps truncated) — Populate fills the section
    # layer too, per the Director's design, and the boxes land on the track for
    # dragging afterward. Stage one's list is carried across the re-read rather than
    # re-derived: the fresh project is the one being written, and it has never seen it.
    sections_origin = "director" if project.sections else ""
    if not project.sections and staged_sections:
        project.sections = staged_sections
        sections_origin = "structure"
    elif not project.sections and result.sections:
        project.sections = [
            SongSection(label=label, start=start, duration=length, prompt=prompt)
            for label, start, length, prompt in repair_sections(
                [
                    (item.label, item.start, item.duration, item.prompt)
                    for item in result.sections
                ],
                duration,
            )
        ]
        sections_origin = "shots"
    # With sections marked, each section tiles independently so no shot straddles a
    # boundary — cuts land exactly on the music's own switches, the Director's ask.
    # Unmarked stretches (before, between, after sections) tile as their own spans so
    # the plan still covers the whole song and assembly's gap refusal stays silent.
    # Without sections, the whole song tiles as one span, exactly as before.
    # The density probe, built once over the whole song and handed to every span. `None` for a
    # track that has never been transcribed, which is `populate_windows`' "lay it as you always
    # did" and not a fabricated silence.
    density = vocal_density(project.song)
    if project.sections:
        # `timeline.layout_spans` since 2026-08-22, character for character what stood here:
        # extracted so the fill-in step can cut the song into the *same* stretches instead of
        # ignoring them. See `paired_proposals`.
        spans = layout_spans(project.sections, duration)
        windows = []
        for span_start, span_length in spans:
            inside = [
                (shot.start, shot.duration)
                for shot in proposals
                if span_start <= shot.start < span_start + span_length
            ]
            for start, length in populate_windows(
                inside,
                span_length,
                maximum=POPULATE_MAX_WINDOW_SECONDS,
                # Span-local seconds in, song seconds out: the probe was built over the song and
                # the tiler counts from this section's start.
                density=(
                    None
                    if density is None
                    else lambda low, high, at=span_start: density(at + low, at + high)
                ),
                variance=variance,
            ):
                windows.append((round(span_start + start, 3), length))
    else:
        windows = populate_windows(
            [(shot.start, shot.duration) for shot in proposals],
            duration,
            maximum=POPULATE_MAX_WINDOW_SECONDS,
            density=density,
            variance=variance,
        )
    return ShotLayout(
        project=project,
        duration=duration,
        required=required,
        # The one place the reply's duck-typed fields are read. `getattr` for the reason
        # the citation and performance lines below it used it: `plan` is duck-typed at
        # every call site and a double that predates `assets` or `performance` must keep
        # working. Absent `assets` is `()`, which is the byte-identical old behaviour —
        # the prose scan alone.
        proposals=tuple(
            ShotProposal(
                start=proposal.start,
                duration=proposal.duration,
                prompt=proposal.prompt,
                performance=bool(getattr(proposal, "performance", False)),
                assets=tuple(getattr(proposal, "assets", None) or ()),
            )
            for proposal in proposals
        ),
        windows=tuple(windows),
        sections=tuple(project.sections),
        sections_origin=sections_origin,
        message=(result.message if result else "") or "",
        variance=variance,
    )


#: How a window of a layout is named in a line-up report, before any Shot exists to name.
#:
#: `shot_label`'s job for a plan that has no shots: the numbers a Director would read off the
#: timeline once this lands, one-based like `SHOT 01`, and deliberately a *different word* so
#: nobody mistakes a report about a proposal for a report about the plan on disk.
LINE_UP_WINDOW_LABEL = "WINDOW {number:02d}"


def line_up_windows(layout: ShotLayout) -> list[SnapWindow]:
    """A layout's tiling as windows the snapping core can be handed.

    **No refusal on any of them, and that is a statement rather than an omission.** These
    windows were created moments ago by the lay-out step; there is no shot at them yet, so
    there is no lock to honour, no approved take whose window would go stale, and no render in
    flight submitted for one. Inventing a check that could never fire would read as a
    protection this path has and does not. The protections are real on the other entry point —
    lining up a timeline the Director already has — and they arrive there from
    `window_move_refusal`, the one reader of them.
    """
    return [
        SnapWindow(
            id=f"window_{index:03d}",
            start=start,
            duration=length,
            label=LINE_UP_WINDOW_LABEL.format(number=index + 1),
        )
        for index, (start, length) in enumerate(layout.windows)
    ]


def line_up_shots(
    layout: ShotLayout,
    *,
    tolerance: float = SNAP_TOLERANCE_DEFAULT,
    windows: Sequence[SnapWindow] | None = None,
    minimum: float = H3_MIN_SHOT_SECONDS,
    maximum: float = POPULATE_MAX_WINDOW_SECONDS,
) -> ShotAlignment:
    """Step two — line it up. Move each cut onto the music, then measure what it now covers.

    Pure, model-free, and it writes nothing: it answers with a proposal and the caller decides
    whether to keep it. Two things come out, and the second is the one nothing downstream has
    had before:

    * **The windows, moved.** Every cut is offered the nearest moment the track leaves
      voiceless, by `timeline.snap_window_plan` — the same core the snap-cuts route reaches
      through `snap_cut_plan`, with the same clearance clamp, the same band check and the same
      per-cut refusals. Populate used to produce its layout blind to phrase boundaries and
      leave the fix to a manual snap afterwards; it no longer does. **A cut sitting on a
      section boundary is left alone** — the layout put it there so no shot straddles a
      section, and until 2026-08-22 this step spent 2 of every 5 moves undoing that. The
      boundaries travel into `snap_window_plan` as `sections`, which is where both doors onto
      the snapper ask the question.
    * **What each window covers.** The seconds of measured voice inside it (the fact that
      downgrades `singing`), the section it falls in, and — new — the lyric lines sung across
      it with the singer slots their `(S1)` marks name. See `ShotPlacement`.

    `windows` is how the second caller identifies and protects its windows: a project-sourced
    line-up hands in the timeline's own shots, named and carrying `window_move_refusal`'s
    sentences. The default is the layout's own fresh tiling (`line_up_windows`), which has
    nothing to protect.

    **The band is the layout's own**, `POPULATE_MAX_WINDOW_SECONDS` at the top rather than
    `H3_MAX_SHOT_SECONDS`: lay-out capped these windows there on a measured render-cost
    decision — 6.8 s since the Director's ruling of 2026-08-23, 6.0 before it — and a step whose
    whole job is to nudge a cut by less than a second must not be the thing that undoes it. A
    caller lining up stored windows passes the wider band, for the reason `snap_window_plan`
    gives.

    **The default is bound at import, so the number in the signature is the one this module was
    loaded with.** That matters only to a harness that reassigns the constant to measure a
    hypothetical ceiling: `lay_out_shots` reads the module global at call time and follows the
    reassignment, this default does not, and a measurement that changed one without the other
    would report a plan laid under one band and lined up under another. The routes take the
    default and are right to — nothing at runtime reassigns the constant — but a measurement
    harness that does must pass `maximum` explicitly.

    **Tolerance 0 is the feature off and is a genuine no-op** — the core answers `"off"`
    before it examines anything, every window comes back with the floats it went in with, and
    a populate run that way is byte-for-byte the one this application shipped before phrase
    awareness existed.

    **An unmeasured song is an explicitly empty branch.** No measurement means no gap to snap
    to (`vocal_gaps` answers `None`, the core answers `"unmeasured"`, nothing moves) and no
    word times to place a line at, so `lines` is empty on every window. Neither is a guess and
    neither is a crash.
    """
    project = layout.project
    song = project.song
    offered = list(line_up_windows(layout) if windows is None else windows)
    plan = snap_window_plan(
        offered,
        song,
        tolerance=tolerance,
        minimum=minimum,
        maximum=maximum,
        # The layout's own section marks, handed to the one decision rather than checked
        # afterwards by a second mechanism. **This is the fix for the live defect of
        # 2026-08-21**: lining up a fresh layout moved 2 of 5 cuts *across* a section
        # boundary (11.000 → 10.850, 103.200 → 103.050), spending in step 2 the boundary
        # step 1 tiles per section to protect. `snap_cut_plan` — the other door — passes
        # the same mapping from `project.sections`, so a cut protected here is protected
        # there. See `timeline.SNAP_SECTION_BOUNDARY`.
        sections=section_edges(layout.sections),
    )
    # Every sung line of the sheet, timed once for the whole plan rather than per window: the
    # alignment is over the song, not over a shot, and asking it 30 times would be 30 chances
    # to answer differently. Empty for an unmeasured song and for a project with no sheet —
    # `align_lyric_lines` states both by returning nothing rather than by guessing a span.
    lines = (
        align_lyric_lines(song.lyrics, [tuple(word) for word in song.lyric_words])
        if song is not None and song.lyrics and song.lyric_words
        else []
    )
    sections = layout.sections
    placements: list[ShotPlacement] = []
    for index, (_id, start, length) in enumerate(plan.windows):
        # One measurement per window, the same call populate made inline. `None` is
        # unmeasured — the absent-analysis convention — and `voiceless` stays False for it,
        # so an untranscribed song changes nothing about what fill-in writes.
        vocal = shot_vocal_overlap(song, start=start, duration=length)
        end = start + length
        middle = start + length / 2
        placements.append(
            ShotPlacement(
                index=index,
                start=start,
                duration=length,
                section=next(
                    (
                        section.label
                        for section in sections
                        if section.start <= middle < section.end
                    ),
                    "",
                ),
                vocal_seconds=vocal,
                voiceless=vocal is not None and vocal < MIN_SINGING_VOCAL_SECONDS,
                # A line is covered when any of the seconds it was *heard* at fall inside this
                # window. Not "mostly inside" and not "its midpoint": a cut through the middle
                # of a line leaves both shots showing a mouth saying those words, and both of
                # them need to know it.
                lines=tuple(
                    line for line in lines if line.start < end and line.end > start
                ),
            )
        )
    return ShotAlignment(
        layout=layout,
        placements=tuple(placements),
        measured=any(placement.vocal_seconds is not None for placement in placements),
        moved=len(plan.moves),
        status=plan.status,
        tolerance=plan.tolerance,
        moves=tuple(plan.moves),
        skips=tuple(plan.skips),
    )


def paired_proposals(layout: ShotLayout, midpoints: Sequence[float]) -> list[int | None]:
    """Which proposal each window takes its content from — **within its own section**.

    **The defect this closes, measured live 2026-08-21 on a 31-window / 30-proposal roll.**
    Lay-out tiles the windows per section and tells the model in words that "every shot sits
    inside one section and takes that section's character"; fill-in then mapped proposals to
    windows by global proportion over the whole song, which undid it. P7 — the Chorus opener —
    was never used at all, P4 and P12 were each used twice (two pairs of adjacent shots came
    out with identical prompts), and four windows carried prose written for a different
    section: window 7 (Chorus) got a Verse line, window 21 (**Bridge**) got a Chorus 2 line,
    window 25 (Outro) got a Bridge line. Latent whenever the window count differs from the
    proposal count, which per-section tiling makes the normal case.

    **The Director's ruling (2026-08-22): map within section, and reuse within the section when
    it has more windows than proposals.** A Chorus window may only ever receive a Chorus
    proposal. Duplicates are acceptable and deliberately visible — `batch.readiness_report`
    surfaces identical adjacent prompts as sameness warnings, and it is what independently
    caught this defect — so a reused proposal shows up for a rewrite rather than hiding. No
    second model call is spent; this is arithmetic over the one reply lay-out already has.

    **How a pair is made, and why by rank.** Inside a span, the section's windows and the
    section's proposals are both in song order, and window *j* of *w* takes proposal
    ``proposal_for_position(j + 0.5, w, p)`` — the same proportional arithmetic this module
    has always used, applied to *ranks* rather than to seconds. Rank rather than time because
    a window's length is not evidence of anything the model said: `populate_windows` states
    that a local model's layout is "treated as *shape*, never as arithmetic", so its proposed
    seconds are used to decide which section a proposal belongs to (lay-out's own rule, below)
    and for nothing else. Weighting the pairing by window length would give a long window a
    bigger claim on the section's story purely because the tiling repair made it long.

    Two properties follow from the half-rank offset, and they are the load-bearing pair:

    * **more windows than proposals** — the step increases by ``p / w ≤ 1``, so no proposal in
      the section is skipped: every one is used at least once, and the pairing stays
      non-decreasing, so nothing arrives out of the order the model wrote it in;
    * **more proposals than windows** — the step is ``≥ 1``, so the pairing is strictly
      increasing and no proposal is used twice. The surplus is dropped by *sampling* rather
      than by truncating the tail: 3 windows against 5 proposals take the 1st, 3rd and 5th.

    **Neither says anything about *where* the reuse or the sampling falls, and two earlier
    sentences here did — wrongly (corrected 2026-08-23).** "The reuse lands in the middle of the
    section rather than doubling its first or last line" holds only while ``w < 2p``: measured,
    ``w=5, p=2`` pairs ``[0, 0, 1, 1, 1]``, doubling the first line *and* tripling the last.
    "The surplus is sampled across the section's arc" is the docstring's own example and no more:
    ``w=2, p=5`` pairs ``[1, 3]``, discarding both the opener and the closer, and the opener goes
    whenever ``p ≥ 2w``.

    Both ratios are **reachable rather than hypothetical**: `timeline.populate_windows` clamps a
    span's window count to at least ``ceil(span / POPULATE_MAX_WINDOW_SECONDS)`` whatever the
    proposal count, so a 34 s section the model wrote two lines for gets five windows and pairs
    ``[0, 0, 1, 1, 1]`` exactly.

    **The claims were corrected rather than the arithmetic, and that is the choice.** Distributing
    the reuse or the sampling differently would mean a second spelling of `proposal_for_position`
    — the function this module deliberately shares between the per-section path and the
    whole-song one — for a purely cosmetic property, and it would move which prose lands in which
    window on every populate this application has ever laid, byte digests and all. The two
    properties that *matter* to the ruling ("no proposal skipped", "no proposal used twice")
    are true as written, and the symptom the false claims were about is already surfaced:
    `batch.readiness_report` reports identical adjacent prompts as sameness warnings, which is
    what caught the original defect. A doubled opener shows up there for a rewrite.

    **A section the model wrote no proposal for gets `None`**, and the caller writes an empty
    shot for it rather than borrowing from a neighbour. That is the ruling's plain reading and
    this codebase's standing rule for an absent answer: an honestly empty prompt is visible in
    the readiness report and in the inspector, where a plausible sentence written for a
    different section is invisible until the take comes back wrong.

    **A layout with no section layer keeps the global rule, byte for byte** — `layout_spans`
    answers `[]` for one, and the whole song is one extent again. That path is what the
    unmarked-sections byte digest in `tests/test_populate_steps.py` still pins.

    A proposal belongs to the span containing its `start`, which is `lay_out_shots`' own
    filter verbatim: a proposal lay-out did not tile into a span is a proposal fill-in does not
    read into one. A window belongs to the first span containing its **midpoint**, which is
    `song_section`'s rule for a shot and the rule `ShotPlacement.section` already reports;
    lay-out's windows never straddle a span, so the two never disagree.
    """
    proposals = layout.proposals
    if not proposals:
        return [None] * len(midpoints)
    spans = layout_spans(layout.sections, layout.duration)
    if not spans:
        return [
            proposal_for_position(position, layout.duration, len(proposals))
            for position in midpoints
        ]
    paired: list[int | None] = [None] * len(midpoints)
    claimed: set[int] = set()
    spent: set[int] = set()
    for span_start, span_length in spans:
        span_end = span_start + span_length
        # Spent as it goes, for the reason `claimed` exists below and by the same argument: two
        # spans that cover the same second would both read the proposals underneath, and "no
        # proposal is used twice" — the property stated above — would quietly stop being true.
        # `layout_spans` makes its spans disjoint whatever the section layer does, so this can
        # only ever be a no-op today; it is here because the guard on the *windows* was written
        # for exactly this hazard and covering one of the two lists was covering half of it.
        inside = [
            index
            for index, proposal in enumerate(proposals)
            if index not in spent and span_start <= proposal.start < span_end
        ]
        # Claimed as it goes so a window can only ever be filled once, whatever a hand-marked
        # section layer does: two boxes the Director overlapped would otherwise both offer to
        # fill the windows underneath, and the later one would silently win.
        windows = [
            index
            for index, position in enumerate(midpoints)
            if index not in claimed and span_start <= position < span_end
        ]
        claimed.update(windows)
        spent.update(inside)
        if not inside:
            continue
        for rank, index in enumerate(windows):
            paired[index] = inside[
                proposal_for_position(rank + 0.5, len(windows), len(inside))
            ]
    return paired


def fill_in_shots(alignment: ShotAlignment) -> list[Shot]:
    """Step three — fill it in. What each window contains, and nothing about where it sits.

    Pure and model-free: every content decision here is made from `ShotAlignment` and the
    project's own library, and the model's half of it arrived on `ShotLayout.proposals` from
    the single call lay-out spent. Windows are read and never written — this step is the one
    that will later be re-runnable against timing the Director has already approved, and that
    promise starts by it having no way to move anything.

    **Which proposal a window takes is decided within that window's own section**
    (`paired_proposals`, 2026-08-22). This step used to map content to windows by global
    proportion over the song, which undid the per-section tiling lay-out had just built; a
    section with no proposal of its own now produces an honestly empty shot rather than
    borrowing a neighbour's prose. Everything below is what happens *once* a window has its
    proposal, and is unchanged.

    The mechanical fills the first run needed a hand-run script for, now populate's own act
    (the run-2 audit's items 4 and 5):

    * `performance` comes from the model and maps onto `singing`/`use_song_audio`.
      `resolve_shot_mode` then routes performance shots to references automatically, so no
      mode needs writing. A field with a default is not in the schema's `required`, so the
      decoder was free to omit it, and on 2026-08-20 one model omitted it on 4 of 5 rolls and
      every shot came through here silently non-performance. `director_result_schema` now
      promotes `performance` into `PlannedShot.required` whenever `shots` is required, which
      is what makes the model decide per shot rather than fall through a default. Note what
      that does *not* change: absent and `false` are indistinguishable by the time a
      `ShotProposal` exists, so the line below still reads `not_singing` off silence on any
      path where the grammar is not enforced (the schema-free retry in `_completion`, a
      provider that ignores strict).
    * citations come from the shot's own `assets` field first and from its prose only as a
      fallback (`models.assets_for_proposal`). The instruction commands prose that names
      nothing; the scan is kept, demoted, because a model that writes a name and omits the
      field has said unambiguously which picture it wants, and dropping that citation would
      send the shot to render without the reference it asked for — invisible until the take
      comes back wrong. Names under `NAME_SCAN_MIN_LENGTH` characters are still skipped as
      substring noise, and an asset named both ways is cited once.

      The scan is over `citable_assets`, not the whole library, and that is the same line the
      roster and the context dump are drawn on: an identity sheet is not separately citable,
      and scanning for its name was also a live substring bug — "HarderFaster · multiview"
      contains "HarderFaster", so a prompt naming the sheet matched *both* assets and spent
      two picture slots on one face.

      Two rules then run over what the scan produced, in this order and no other:
      `with_default_setting` may append the project's declared location (it counts picture
      slots, so it must see the pre-substitution list — the widest the list can be), and
      `prefer_identity_sheets` then re-points every reference at the promoted sheet of what it
      names and collapses duplicates, which can only make the list shorter. Both are no-ops on
      a project with no promotions and no declared location, so such a project's citations are
      byte-identical to what populate has always written.
    """
    layout = alignment.layout
    project = layout.project
    proposals = layout.proposals
    library = citable_assets(project)
    sheets = identity_sheet_ids(project)

    def proposal_citations(proposal: ShotProposal) -> list[AssetCitation]:
        named = [
            AssetCitation(asset_id=asset.id, role="reference", order=order)
            for order, asset in enumerate(
                assets_for_proposal(
                    library, declared=proposal.assets, prose=proposal.prompt
                )
            )
        ]
        located = with_default_setting(
            project, named, picture_limit=H3_REFERENCE_LIMITS["picture"]
        )
        return prefer_identity_sheets(located, sheets)

    # Which proposal each window takes, decided **within its section** since 2026-08-22 — see
    # `paired_proposals` for the live defect that bought the change and for how a pair is made.
    # Taken for the whole plan in one call rather than per window, because "the third window of
    # the Chorus" is a fact about the section's list and not about one window.
    paired = paired_proposals(
        layout,
        [
            placement.start + placement.duration / 2
            for placement in alignment.placements
        ],
    )
    shots: list[Shot] = []
    for placement, chosen in zip(alignment.placements, paired, strict=True):
        if chosen is None:
            # A window in a section the model wrote no shot for. Left honestly empty rather than
            # given a neighbouring section's prose: the Director's ruling is that a Chorus window
            # may only ever receive a Chorus proposal, and an empty prompt is visible in the
            # readiness report and the inspector where a sentence written for the wrong section
            # is invisible until the take comes back wrong. `singing` is left at the Shot default
            # — `unknown`, which is not `not_singing` — because nothing was declared about it.
            shots.append(
                Shot(
                    start=placement.start,
                    duration=placement.duration,
                    use_song_audio=True,
                    seed=1 + placement.index,
                )
            )
            continue
        proposal = proposals[chosen]
        # Mapped from the model's own `performance` declaration — a dedicated strict-
        # schema field the instruction explicitly asks for — never inferred from
        # prose. The nothing-infers-singing guard permits exactly this mapping and
        # forbids everything looser; the Director reviews the result per shot in the
        # inspector, exactly as they reviewed the hand-run script it replaces.
        # One measured exception to the declaration, one-directional, and it arrives
        # from line-up rather than being measured here: a window the track is
        # *measured* to leave voiceless cannot be sung, whatever the model declared —
        # live on 2026-08-19 it marked the intro and the whole instrumental outro
        # singing, and H3 invented words for them and lipsynced to the invention.
        # Unmeasured changes nothing, and a not-singing declaration over vocals is a
        # legitimate creative choice, untouched. This is not the inference the singing
        # guard forbids: nothing reads prose, mode or library for it — only Whisper's
        # measured voice activity on the track.
        # Named locals rather than the attribute chain, and deliberately: the mapping below is
        # pinned to one spelling at one site by the nothing-infers-singing test, and the pin is
        # what keeps a second, looser writer of `singing` from appearing somewhere else.
        performing = proposal.performance
        voiceless = placement.voiceless
        declared_singing: SingingState = (
            "singing" if performing and not voiceless else "not_singing"
        )
        shots.append(
            Shot(
                start=placement.start,
                duration=placement.duration,
                prompt=proposal.prompt.strip(),
                citations=proposal_citations(proposal),
                singing=declared_singing,
                # Every shot rides its window of the master as reference — the
                # Director's ruling (2026-08-19): a non-singing shot still gets its
                # piece of the track "for dancing and moving on beat"; `singing`
                # alone decides whether the prompt asks for an articulating mouth.
                use_song_audio=True,
                # Distinct per shot, derived from the window rather than random so a
                # re-populate of the same plan is reproducible. Sixteen shots sharing
                # seed 0 made one bad sampling trajectory a batch-wide risk on the
                # first live batch (3 of 4 lost to a NaN'd audio latent).
                seed=1 + placement.index,
            )
        )
    return shots


# ------------------------------------------------------------------------------------------
# Report-then-confirm, made real: the plan the Director read is the plan that lands.
#
# Both model-backed bulk passes below (`clean_shot_prompts`, `fill_section_looks`) are
# report-first by design, and both were report-first in name only until 2026-08-21: each call
# asked the model *before* looking at `confirm_apply`, so the report and the apply were two
# independent generations at `PLAN_TEMPERATURE = 0.7`. Measured on the Director's live 33-shot
# plan that night: of 24 rewrites read and approved in the report, **one landed as different
# text** ("Extreme close up of smeared crimson lips, wet." reviewed, "Extreme close up of
# crimson lips, smeared and wet." applied). Both readings happened to be acceptable; that is
# luck, not a guarantee, and the guarantee is the whole point of a report a person reads. It
# also spent a second local-model call — up to 300 s — to do it.
#
# The fix: **the confirm carries the report back**, whole, and the route applies exactly that.
# `plan` on each request is the response model of the same route, so the client echoes the body
# it was given rather than reconstructing a plan the server would have to trust. Two things
# make the echo as strong as a server-side cache would be:
#
# * `plan_id` — a SHA-256 the *server* mints over the report it emitted, recomputed here over
#   the report it is handed. Any field of any row that changed in between fails it, so the
#   text that lands is provably the text that was reported.
# * `updated_at` — the report's revision of the project, checked against the live one with
#   `PROJECT_CHANGED_REFUSAL`, the wording `replace_project` and `replace_shots` already use.
#   It is inside the digest too, so a client cannot pass the revision check by rewriting it.
#
# Echo rather than a server-side cache keyed by plan id, weighed and chosen:
#
# * No server state, so no lifetime, no eviction, no "which worker holds it", and a plan
#   survives a restart exactly the way a shot list does.
# * It is this codebase's existing idiom rather than a new one — `ShotListRequest` is a full
#   payload plus a revision token for precisely this reason, and the refusal is already
#   written.
# * The guarantee becomes checkable from the wire alone: the report body and the confirm body
#   can be compared byte for byte by anything holding both, with no reach into server internals.
# * A cache would still need the revision check (so it is strictly more machinery for the same
#   promise), and its extra failure mode — plan evicted — refuses in a way the Director can
#   only fix by spending the 300 s call again, which is the cost this change exists to remove.
#
# The digest is a plain hash, not a keyed MAC, and that is honest about what it defends: this
# is a single-user local application, and the hazard measured here is a *client that
# regenerates*, not an attacker. A client that recomputes the digest over substituted text is
# asserting "this is what I reviewed" as deliberately as any `PUT /shots` asserts its body.
# What the digest ends is text arriving that nobody chose.
#: Fields left out of the digest: the two a confirm is *allowed* to change about the report it
#: echoes, and the id itself, which cannot be inside its own hash.
PLAN_DIGEST_EXCLUDE = {"applied", "project", "plan_id"}


def plan_fingerprint(project: Project, plan: BaseModel) -> str:
    """The id a report is minted with and a confirm is checked against.

    Canonical by construction: pydantic has already coerced every field to its declared type
    before this runs (so `12` and `12.0` hash alike), `mode="json"` renders datetimes as their
    ISO strings, and `sort_keys` removes key order from the answer. The project id is inside it
    so a plan cannot be replayed against a different project.
    """
    payload = json.dumps(
        {
            "project": project.id,
            "plan": plan.model_dump(mode="json", exclude=PLAN_DIGEST_EXCLUDE),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


#: The honest-empty refusal, `snap_timeline_cuts`' rule: nothing was examined that could
#: change, so there is no plan to report over and a 200 saying "0 rewritten" would read as the
#: model having been asked and having had nothing to say. Names the good news, because on this
#: route an empty answer *is* the good news.
CLEAN_PROMPTS_NOTHING_TO_CLEAN = (
    "No shot's prompt names a library asset, so there is nothing to clean. The prose is "
    "already free of asset labels and every shot keeps the citations it has."
)
#: The other honest empty, and it is a refusal for a different reason: there is nothing to send
#: a model. Reached only when every echoing shot is protected, which is a state the Director
#: fixes (unlock, or let the queue settle) rather than a report they act on.
CLEAN_PROMPTS_ALL_PROTECTED = (
    "Every shot whose prompt names an asset is locked or has a render in flight, so there was "
    "nothing to send for rewriting: {shots}."
)
#: A shot with no echo, reported rather than omitted. It is a third of the Director's plan and
#: "nothing happened to this shot" has to say why — the same rule `SECTION_LOOK_SKIP_*` and
#: `ReplacementSkip` are written to. It is deliberately *not* sent to the model at all.
CLEAN_PROMPTS_ALREADY_CLEAN = "this prompt names no asset label; left exactly as it is"
#: The model was given this shot and answered about a different one, or about none.
CLEAN_PROMPTS_UNANSWERED = "the model returned no rewrite for this shot"
#: One answer emitted twice is not a second opinion to arbitrate — `fill_section_looks`' rule,
#: first answer per id wins, and the later copy is reported rather than silently dropped.
CLEAN_PROMPTS_DUPLICATED = (
    "the model answered for this shot more than once; the first answer was used and the rest "
    "discarded"
)
#: The hand-edit rule, and the defect it closes (2026-08-21). The rewrites come back addressed
#: to shot ids, and the route re-reads the project after the await so a lock or a deletion in
#: that window is honoured — but the *rewrite* was made from the prompt as it read when the
#: call went out. If the Director edited that prompt while the model was reading, applying the
#: answer replaces their edit with a rewrite of the text they replaced. Dropped and named, not
#: guessed at: a named skip beats a silent guess, and re-asking would cost the call again.
CLEAN_PROMPTS_EDITED = (
    "this prompt was edited by hand while the model was reading it, so the rewrite that came "
    "back was made from text that no longer exists; your edit was left exactly as you wrote it"
)
#: Counted, never written. An id that matches no shot cannot be created into one: that would
#: invent a window, which is the one thing this pass exists not to do.
CLEAN_PROMPTS_STRAY = "; {count} rewrite(s) addressed no shot in this project"
#: A confirm that carries nothing to apply. The rewrites are model output read by a person, so
#: "apply whatever you generate now" is not an act this route offers at all — see the
#: plan-carrying block above.
CLEAN_PROMPTS_NO_PLAN = (
    "This confirm carries no plan, so there is nothing it could apply. The rewrites this pass "
    "writes are the ones a person read in a report: run the report, read it, then confirm with "
    "that report as `plan`. Nothing was written and no model was asked."
)
#: The digest firing. Deliberately a refusal rather than a re-ask: re-asking is how prose that
#: nobody read used to land on a hand-reviewed plan.
CLEAN_PROMPTS_PLAN_MISMATCH = (
    "The plan sent with this confirm is not the plan this pass reported: it does not match its "
    "own plan_id. Refused rather than rewriting the prompts again, because the text that lands "
    "has to be the text that was read. Nothing was written. Run the report again."
)
#: Reported, never refused — `REPLACE_ASSET_RENDERED_NOTE`'s shape and its argument, applied to
#: prose. The Director's ruling on citations (2026-08-20): *"even with takes we do want the
#: asset for the shot replaceable ... This helps facilitate experimentation."* Prose is the
#: looser coupling of the two — the prompt that produced each take is recorded on its job and
#: in the take's own PNG metadata, so the record is not lost by editing the shot — and the
#: label being removed is not a word H3 needed: the pictures arrive as reference images, not as
#: names in the text. `expansion_write_refusal` already carves the same exemption for the same
#: reason. Named here so the count is on screen before the confirm.
CLEAN_PROMPTS_RENDERED_NOTE = (
    "{count} shot(s) already hold a take that was rendered from the prompt as it stands: "
    "{shots}. The takes are untouched — the files, the takes strip and the job records are "
    "exactly as they are — and each job still records the prompt it was actually submitted "
    "with. Only the shot's own text changes; nothing is re-rendered."
)
#: The same, for an editorial approval. Its own line so the number is visible: an approval is a
#: stronger statement than a render, and `approved_output`, `approved_start` and
#: `approved_duration` are not written on any path here, so AD-13's staleness comparison and
#: assembly read exactly what they read before.
CLEAN_PROMPTS_APPROVED_NOTE = (
    "{count} shot(s) carry an approved take: {shots}. The approval is untouched and so is the "
    "window it was made against — only the prompt's wording changes."
)
#: The guarantee, enforced rather than promised. Unreachable by construction: the write loop
#: assigns `Shot.prompt` and nothing else. It exists because the Director's hand-tuned windows
#: are the most expensive thing in this project — 33 deliberately varied edges including
#: several micro-cuts, placed against musical timing — and "the code only assigns one field" is
#: a claim about code, where this is a check on data. If it ever fires, nothing has been saved.
CLEAN_PROMPTS_WINDOWS_MOVED = (
    "Refused: the shot windows changed while the prompts were being rewritten. Nothing was "
    "saved. This pass may only ever change a prompt's wording — the timeline's geometry is "
    "the director's own work and is not this route's to touch."
)
#: The summary sentence, counts first because they are what a Director sanity-checks before
#: reading 33 rows of diff.
CLEAN_PROMPTS_REPORT = (
    "{echoing} of {examined} shot(s) name an asset label. {rewritten} rewritten, {skipped} "
    "left alone, {clean} already clean. Only the prompt text changes: windows, citations, "
    "modes, takes and approvals are untouched."
)
#: Said in the report itself, not only in a log. The rewrites are model output that no live
#: model has been run against from this route, and the Director reads this report to decide
#: whether to apply it — so the one thing they most need to know about the wording is in the
#: thing they are reading.
CLEAN_PROMPTS_REVIEW_NOTE = (
    "Read every rewrite before applying. The wording is the model's and this pass judges only "
    "that the label is gone and the sentence was not gutted — it does not judge whether the "
    "replacement is the right description."
)


SECTIONS_OVERLAP_REFUSAL = (
    "Sections may not overlap: {first} ends at {end:.2f}s but {second} starts at "
    "{start:.2f}s. Adjust the windows so each moment of the song belongs to one section."
)


def legal_sections(sections: Sequence[SongSection]) -> list[SongSection]:
    """The section layer sorted by start, refused if any two overlap. **Every writer's rule.**

    Extracted 2026-08-23 because it was one route's rule and two routes write the field.
    `PUT /sections` sorted and refused; the generic `PUT /api/projects/{id}` — the normal save
    path for every edit the browser makes, and this codebase's recorded sibling-write hole
    *nine* times over — validated `sections` not at all. Everything downstream reads the layer
    as sorted and disjoint: `timeline.layout_spans` tiles it, `song_section` breaks a tie by
    "the later start wins", `section_edges` lets a start win a collision. Three rules that agree
    on a sorted disjoint layer and on nothing else.

    Unlike the `_adopt_*` helpers beside the generic route, this is **validation and not
    adoption**: sections are the Director's own hand-dragged structure and that route is where
    the browser saves them, so taking the stored value would make the boxes undraggable. What it
    must not do is accept a layer no reader can make sense of.

    Sorting rather than refusing an out-of-order body is `replace_sections`' own long-standing
    choice, kept: order in the list carries no meaning that time order does not, so a client that
    sends the boxes in the order they were drawn is not wrong, only unsorted.
    """
    ordered = sorted(sections, key=lambda section: section.start)
    for first, second in itertools.pairwise(ordered):
        if second.start < first.end - 1e-6:
            raise HTTPException(
                status_code=422,
                detail=SECTIONS_OVERLAP_REFUSAL.format(
                    first=first.label, end=first.end,
                    second=second.label, start=second.start,
                ),
            )
    return ordered


class SectionListRequest(BaseModel):
    """The whole section list, replaced as one — the shots-PUT idiom, with the same
    justification: sections are a small, hand-authored structure and partial edits of a
    timeline invite gaps nobody chose."""

    sections: list[SongSection]


#: The three honest-empty refusals of the section-look pass. Refusals rather than empty
#: reports, `snap_timeline_cuts`' distinction: there is nothing to report because nothing was
#: examined, and a 200 saying "0 filled" would read like the model was asked and had nothing
#: to say. Each names the one thing to write or mark, because the fix is the Director's.
SECTION_LOOKS_NO_SECTIONS = (
    "This project has no sections yet, so there is nothing to fill. Mark the song's "
    "structure first — align the lyric sheet, or drag the section boxes onto the timeline."
)
SECTION_LOOKS_NO_TREATMENT = (
    "The treatment is empty, so there is nothing to read a section's look out of. A "
    "section look is the treatment's own words for that stretch of the song; inventing "
    "one from nothing would be worse than leaving it blank. Write the treatment first."
)
SECTION_LOOKS_NO_STYLE_BIBLE = (
    "The style bible is empty. It is the fixed visual language every section's look is "
    "written in — palette, lighting, lenses, wardrobe, locations — and without it the "
    "looks would carry the treatment's story with no way to render it. Write it first."
)

#: What the report says when every section already carries a look the Director wrote. Not a
#: refusal: the answer is that there is nothing *to* do, and the sentence names the step that
#: would overrule it so the Director does not have to guess.
#:
#: Reworded 2026-08-21, because the sentence named the wrong step. "Send overwrite=true"
#: describes a *confirm*, and this report has nothing for a confirm to write: it short-circuits
#: ahead of the model call, so its rows carry no proposed look. An API caller that followed it
#: literally — `confirm_apply` and `overwrite` with this report as `plan` — passed every check,
#: skipped every row for want of a prompt, and got 200 with `applied: false` and nothing
#: written. Two halves to the fix and this is the first: name the step that actually works,
#: which is the one the browser has always taken (report again *with* the consent, read what it
#: proposes, confirm that). The second half is `SECTION_LOOKS_UNREAD_PLAN`, which refuses this
#: report if it comes back as a plan anyway.
SECTION_LOOKS_ALL_WRITTEN = (
    "Every section already has a look. Nothing was changed and no look was read for any of "
    "them — run this again with overwrite=true to see what would replace them, then confirm "
    "that report."
)

#: The per-section skip reasons, one sentence each, because "skipped" without a why is the
#: report step doing nothing (`spec-arm-a-plan`'s argument, and snap-cuts' `SnapCutSkip`).
SECTION_LOOK_SKIP_WRITTEN = (
    "already has a look you wrote; send overwrite=true to replace it"
)
#: The same skip, on the one report where "send overwrite=true" would be a lie: the all-written
#: short-circuit, which never asked the model, so this row carries no proposed look for a
#: confirm to write. Its own sentence rather than a reuse of the one above, and that is not only
#: wording — `section_looks_plan_writes` reads this string to recognise the report and refuse it
#: as a plan, and `plan_fingerprint` covers `sections`, so the marker cannot be stripped off a
#: plan without the digest failing first. Opens with the same clause the browser's preview
#: renders, so the two skips read alike where they mean alike.
SECTION_LOOK_SKIP_ALL_WRITTEN = (
    "already has a look you wrote; nothing was read for it — report again with overwrite=true "
    "to see what would replace it"
)
SECTION_LOOK_SKIP_UNDESCRIBED = "the treatment does not describe this section"
SECTION_LOOK_SKIP_UNANSWERED = "the model returned no look for this section"
SECTION_LOOK_SKIP_MISLABELLED = (
    "the model addressed this section but called it {label!r}; refused rather than risk "
    "writing another section's look here"
)
#: The plan-carrying refusals, `CLEAN_PROMPTS_NO_PLAN`'s and `CLEAN_PROMPTS_PLAN_MISMATCH`'s
#: rule in this pass's own words — see the plan-carrying block above for why the confirm echoes
#: the report rather than asking the treatment to be read a second time.
SECTION_LOOKS_NO_PLAN = (
    "This confirm carries no plan, so there is nothing it could write. The looks this pass "
    "writes are the ones a person read in a report: run the report, read it, then confirm with "
    "that report as `plan`. Nothing was written and no model was asked."
)
SECTION_LOOKS_PLAN_MISMATCH = (
    "The plan sent with this confirm is not the plan this pass reported: it does not match its "
    "own plan_id. Refused rather than reading the treatment again, because the look that lands "
    "has to be the look that was read. Nothing was written. Run the report again."
)
#: The plan that can write nothing, refused as a plan (2026-08-21). The all-written report is
#: produced *ahead of* the model call — see `SECTION_LOOKS_ALL_WRITTEN` — so every row of it
#: carries an empty `prompt`, and confirming it walked every check, skipped every row, and
#: answered 200 `applied: false` having written nothing. A 200 that means "your request was
#: fine and I did nothing" is the silence this codebase refuses everywhere else, and it was
#: reachable by following this route's own message. Refused rather than served, because the
#: thing to do instead is a different call and the sentence has to name it: this pass cannot
#: write a look nobody read, and nobody read one here.
SECTION_LOOKS_UNREAD_PLAN = (
    "This confirm carries the report that said every section already has a look. That report "
    "stops before the treatment is read, so it holds no look for any section and confirming it "
    "would write nothing. Run the report again with overwrite=true, read the looks it proposes, "
    "then confirm that report. Nothing was written and no model was asked."
)


class SectionLooksRequest(BaseModel):
    """One section-look pass: whether it may write, and whether it may overwrite.

    Two flags rather than one, and they answer different questions. `confirm_apply` is
    `SnapCutsRequest`'s field in the same key and for the same reason, which is `populate`'s
    `confirm_replace` at bottom: the default is a **report**, and only an explicit true
    writes. `overwrite` is the narrower consent — a section look is editable in the
    inspector, so replacing a sentence the Director typed is exactly the bulk edit the
    report-then-confirm convention exists to forbid, and confirming *this pass* is not the
    same act as agreeing to lose that sentence. Empty looks fill on `confirm_apply` alone;
    written ones need both, and the report names them individually either way.

    `plan` is the report being confirmed, echoed back whole — see the plan-carrying block
    above. A `confirm_apply` without one is refused rather than served by reading the
    treatment a second time. `overwrite` is deliberately **outside** the plan and outside its
    digest: it is a consent given after the report is read, and the report already carries the
    proposed look for a written section on its row, so the confirm can honour the second
    question without the treatment being read again to answer it.
    """

    confirm_apply: bool = False
    overwrite: bool = False
    plan: SectionLooksResponse | None = None


class SectionLookRow(BaseModel):
    """One section in the report, named as the timeline names it.

    `prompt` is what would be written (or was), `""` for a skip. `previous` is what the
    section carried before, so an overwrite shows both halves of the trade in the report
    the Director is confirming, rather than after the fact.
    """

    section_id: str
    label: str
    start: float
    filled: bool
    prompt: str = ""
    previous: str = ""
    reason: str = ""


class SectionLooksResponse(BaseModel):
    """The report, and — only on an applied call — the saved project.

    `project` is `None` on a report, `SnapCutsResponse`'s rule and for its reason: the
    absence is the wire's own statement that nothing was written.

    This model is also the *request* body of the confirm that applies it (`plan` on
    `SectionLooksRequest`), which is why `plan_id` and `updated_at` are on it: a report has to
    be able to identify itself when it comes back. `stray` is the one count the message says
    and the rows cannot, and it is a field rather than prose so the confirm can rebuild that
    sentence exactly instead of losing its last clause.
    """

    applied: bool
    filled: int
    skipped: int
    sections: list[SectionLookRow]
    message: str = ""
    project: Project | None = None
    #: Answers that addressed no section in this project. Counted, never written.
    stray: int = 0
    #: The digest that ties this report to the confirm that applies it — `plan_fingerprint`.
    plan_id: str = ""
    #: The project revision this report was read from. Checked against the live one on the
    #: confirm with `PROJECT_CHANGED_REFUSAL`, `replace_shots`' rule and its wording.
    updated_at: datetime | None = None


class CleanPromptsRequest(BaseModel):
    """One prose-cleanup pass, and whether it is allowed to write.

    `confirm_apply` is `SnapCutsRequest`'s field in the same key and for the same reason, which
    is `populate`'s `confirm_replace` at bottom: the default is a **report**, and only an
    explicit true writes. Server-enforced rather than trusted to the browser, and here the
    report is more than half the feature — every row carries the old prose and the proposed
    prose so a human reads the diff before it lands on a hand-reviewed plan.

    One flag and no second one. `SectionLooksRequest` needs `overwrite` because it would be
    replacing a sentence the Director typed; this pass only ever replaces a sentence a *tool*
    wrote badly, and it replaces it with the same sentence minus a library label, so there is
    no second trade to consent to separately.

    `plan` is the report being confirmed, echoed back whole — see the plan-carrying block
    above. A `confirm_apply` without one is refused rather than served by rewriting the prompts
    a second time, because a second rewrite is a second generation and the Director read the
    first.
    """

    confirm_apply: bool = False
    plan: CleanPromptsResponse | None = None


class CleanPromptRow(BaseModel):
    """One shot in the cleanup report, named as the timeline names it.

    `before` and `after` are the whole point: this report is read by a person deciding whether
    to apply it, so every row that would change carries both halves of the change. `after` is
    `""` on any row that would not change, which is what makes "did this shot move" answerable
    from the row rather than from the counts.

    `labels` is what this shot echoed, so a Director who disagrees with a rewrite can see which
    word forced it. `provenance` is `"approved"`, `"rendered"` or `""` — it changes nothing
    about what happens to the shot and exists so the count is visible before the confirm.

    `start` and `duration` ride along and are never written back. They are here so the report
    itself is evidence that the windows did not move: the Director can read the geometry in the
    report and in the manifest afterwards and compare.
    """

    shot_id: str
    label: str
    start: float
    duration: float
    rewritten: bool
    labels: list[str] = Field(default_factory=list)
    before: str = ""
    after: str = ""
    reason: str = ""
    provenance: str = ""


class CleanPromptsResponse(BaseModel):
    """The report, and — only on an applied call — the saved project.

    `project` is `None` on a report, `SnapCutsResponse`'s rule and for its reason: the absence
    is the wire's own statement that nothing was written.

    The counts are the sanity check a Director reads first and they partition the plan
    exactly: `examined` is every shot, and `clean + rewritten + skipped == examined`.

    This model is also the *request* body of the confirm that applies it (`plan` on
    `CleanPromptsRequest`), which is why `plan_id` and `updated_at` are on it: a report has to
    be able to identify itself when it comes back. Every other field rides back untouched — the
    confirm returns the report it was handed with `applied` flipped and `project` filled in, so
    the counts, the notes and every row of diff on screen after the write are the same bytes
    that were on screen before it.
    """

    applied: bool
    examined: int
    echoing: int
    clean: int
    rewritten: int
    skipped: int
    rendered: int
    approved: int
    notes: list[str] = Field(default_factory=list)
    shots: list[CleanPromptRow] = Field(default_factory=list)
    message: str = ""
    project: Project | None = None
    #: The digest that ties this report to the confirm that applies it — `plan_fingerprint`.
    plan_id: str = ""
    #: The project revision this report was read from. Checked against the live one on the
    #: confirm with `PROJECT_CHANGED_REFUSAL`, `replace_shots`' rule and its wording.
    updated_at: datetime | None = None


# Both requests name their route's response model as `plan`, and both are declared above it —
# `from __future__ import annotations` makes that a forward reference, so the two are rebuilt
# here rather than left for whatever first touches them to discover.
SectionLooksRequest.model_rebuild()
CleanPromptsRequest.model_rebuild()


def section_looks_summary(filled: int, left: int, stray: int) -> str:
    """The report's one-line summary. One spelling, because the confirm rebuilds it.

    A `overwrite=true` confirm can turn a written section's skip into a write, which changes
    both counts — so the sentence has to be reproducible from the numbers rather than carried
    as prose, or the confirm would either lie about what it did or lose the stray clause.
    """
    summary = f"{filled} filled, {left} left alone"
    if stray:
        summary += f"; {stray} answer(s) addressed no section in this project"
    return summary


def section_looks_plan_writes(
    project: Project, request: SectionLooksRequest
) -> tuple[SectionLooksResponse, list[tuple[SongSection, str]]]:
    """Check a confirm's echoed report against the live project; say what it writes.

    No model is asked on this path and none can be: the looks it writes are the strings that
    came back on the report, and the checks above them are what makes "the look that lands is
    the look that was read" a fact rather than a hope. Raises rather than reporting, because a
    confirm that cannot prove its plan has nothing to report *about* — the thing it was asked
    to write is exactly the thing it cannot identify.

    Four checks, not three: the fourth is the all-written short-circuit, a report that is
    genuinely this pass's own and genuinely matches its digest, and still cannot write anything
    because no look was ever read for it. Refused by name (`SECTION_LOOKS_UNREAD_PLAN`) rather
    than allowed to fall through the loop and answer 200 with nothing written.
    """
    plan = request.plan
    if plan is None or not plan.sections or not plan.plan_id:
        raise HTTPException(status_code=422, detail=SECTION_LOOKS_NO_PLAN)
    if plan.updated_at is None or plan.updated_at != project.updated_at:
        raise HTTPException(status_code=409, detail=PROJECT_CHANGED_REFUSAL)
    if plan_fingerprint(project, plan) != plan.plan_id:
        raise HTTPException(status_code=422, detail=SECTION_LOOKS_PLAN_MISMATCH)
    # Fourth, and after the digest deliberately: this is a statement about *which* report is
    # being confirmed, so it is only worth making once the report has proved it is the one it
    # says it is. The all-written short-circuit never asked the model, so it carries no look on
    # any row; without this the loop below would `continue` past every one of them and the
    # route would answer 200 having written nothing — while its own message told the caller to
    # send exactly this. See `SECTION_LOOKS_UNREAD_PLAN`.
    if any(row.reason == SECTION_LOOK_SKIP_ALL_WRITTEN for row in plan.sections):
        raise HTTPException(status_code=422, detail=SECTION_LOOKS_UNREAD_PLAN)
    by_id = {section.id: section for section in project.sections}
    response = plan.model_copy(deep=True)
    response.applied = False
    response.project = None
    pending: list[tuple[SongSection, str]] = []
    for row in response.sections:
        section = by_id.get(row.section_id)
        # Unreachable while the revision check above holds — a section cannot be added,
        # deleted, renamed or written without the manifest being saved, and a save moves
        # `updated_at`. Checked rather than argued, `CLEAN_PROMPTS_WINDOWS_MOVED`'s rule.
        if (
            section is None
            or section.label.strip().casefold() != row.label.strip().casefold()
            or section.prompt != row.previous
        ):
            raise HTTPException(status_code=422, detail=SECTION_LOOKS_PLAN_MISMATCH)
        if not row.prompt.strip():
            continue
        # The second consent is answered *here*, against the report, which is what keeps it
        # from costing a second reading of the treatment: the report deliberately carries the
        # proposed look on a written section's row so this decision has something to be taken
        # against. Declining still writes the empty ones — the whole point of two flags.
        if section.prompt.strip() and not request.overwrite:
            row.filled = False
            row.reason = SECTION_LOOK_SKIP_WRITTEN
            continue
        row.filled = True
        row.reason = ""
        pending.append((section, row.prompt))
    response.filled = sum(1 for row in response.sections if row.filled)
    response.skipped = len(response.sections) - response.filled
    # Rebuilt only when `overwrite` actually changed the outcome. Left alone otherwise, so an
    # unchanged confirm returns the reported message byte for byte — including
    # `SECTION_LOOKS_ALL_WRITTEN`, which is not this sentence at all.
    if (response.filled, response.skipped) != (plan.filled, plan.skipped):
        response.message = section_looks_summary(
            response.filled, response.skipped, response.stray
        )
    return response, pending


def clean_prompts_plan_writes(
    project: Project, request: CleanPromptsRequest
) -> tuple[CleanPromptsResponse, list[tuple[Shot, str]]]:
    """Check a confirm's echoed report against the live project; say what it writes.

    `section_looks_plan_writes`' plan-identity checks (its fourth is that pass's own — this one
    has no short-circuit report to recognise), and then the row-level ones. Nothing is
    recomputed into the response: this pass has no second flag, so every count, note and row
    of diff rides back exactly as it was read, and the confirm's body differs from the
    report's in `applied` and `project` alone.
    """
    plan = request.plan
    if plan is None or not plan.shots or not plan.plan_id:
        raise HTTPException(status_code=422, detail=CLEAN_PROMPTS_NO_PLAN)
    if plan.updated_at is None or plan.updated_at != project.updated_at:
        raise HTTPException(status_code=409, detail=PROJECT_CHANGED_REFUSAL)
    if plan_fingerprint(project, plan) != plan.plan_id:
        raise HTTPException(status_code=422, detail=CLEAN_PROMPTS_PLAN_MISMATCH)
    by_id = {shot.id: shot for shot in project.shots}
    library = list(project.assets)
    response = plan.model_copy(deep=True)
    response.applied = False
    response.project = None
    pending: list[tuple[Shot, str]] = []
    for row in response.shots:
        if not row.rewritten:
            continue
        shot = by_id.get(row.shot_id)
        # All three are unreachable while the revision check above holds, and all three are
        # checked anyway: they are the statements the report made about the world, re-read
        # against the world it is about to write to. The prompt still reads as it did, the
        # shot is still unprotected, and the rewrite still passes the acceptance rule.
        if (
            shot is None
            or shot.prompt != row.before
            or shot.locked
            or shot_render_in_flight(project, shot)
            or rewrite_rejection(
                row.after,
                original=shot.prompt,
                labels=echoed_labels(shot.prompt, library),
            )
        ):
            raise HTTPException(status_code=422, detail=CLEAN_PROMPTS_PLAN_MISMATCH)
        pending.append((shot, row.after))
    return response, pending


class PopulateTimelineRequest(BaseModel):
    confirm_replace: bool = False
    #: Ask for the song's structure in its own call before asking for the shots
    #: (`POPULATE_SECTIONS_INSTRUCTION`). Off by default, and that default is the honest
    #: one: the split is the roadmap's answer to a measured single-call failure, but it has
    #: never been run against a live model from here, and turning it on by default would
    #: spend a second local-model call — up to 300 s — on every Director's populate on the
    #: strength of an argument rather than a measurement. Flipping it is one word once a
    #: live run says which way is better, and `false` is byte-for-byte the old behaviour,
    #: so this is not a one-way door in either direction.
    two_stage: bool = False
    #: How far the line-up step may move a cut onto a phrase boundary, `SnapCutsRequest`'s
    #: field in the same key's meaning and with the same bound. Defaulted to the same 0.75 s
    #: the snap-cuts route defaults to, so a first-pass populate lands its cuts where the
    #: Director would have snapped them by hand.
    #:
    #: **0 is the feature switched off and is a genuine no-op** — the layout is written
    #: exactly as `populate_windows` tiled it, byte for byte the plan this application
    #: produced before phrase awareness existed. That is not a courtesy: it is the control
    #: arm, and the test suite pins the pre-Phase-B digests through it.
    snap_tolerance: float = Field(
        default=SNAP_TOLERANCE_DEFAULT, ge=0, le=SNAP_TOLERANCE_MAX
    )
    #: How much of the room H3's band leaves the lay-out step may spend making its windows
    #: different lengths — Phase D, and the Director's standing complaint that "shot lengths all
    #: look the exact same". A **fraction of what is available**, never a number of seconds: the
    #: step is capped at *all* the room there is, so 1.0 is the whole of what the band allows
    #: rather than merely a lot of it, and `POPULATE_VARIANCE_MAX` refuses anything larger.
    #:
    #: **1.0 does not mean any window reaches its band end**, and the earlier wording here said
    #: it did. Two things stop it, both in `timeline._varied_durations`: each window's room is
    #: its distance to the band end *less* `WINDOW_LAY_RESOLUTION`, so a saturated window lands
    #: a millisecond inside rather than exactly on; and the transfer is the **minimum** of that
    #: room and the band's width times the deviations being spread, so a section whose density
    #: barely varies is capped by magnitude and saturates nobody at all. That second cap is the
    #: "magnitude is respected, not just shape" rule, and it binds first far more often than the
    #: room does.
    #:
    #: **0 is the feature switched off and is a genuine no-op** — the windows are the ones
    #: `populate_windows` has always tiled, and that arm is what the byte digests are pinned
    #: through. So is a song nobody has transcribed: no word times, no density, no variance.
    #: The default is `POPULATE_VARIANCE_MAX` itself; see `timeline.POPULATE_VARIANCE_DEFAULT`
    #: for the measurement that moved it there, and for why a dial whose default sits on its own
    #: bound is still a dial — it exists to be turned **down**.
    variance: float = Field(
        default=POPULATE_VARIANCE_DEFAULT, ge=0, le=POPULATE_VARIANCE_MAX
    )


class PopulateTimelineResponse(BaseModel):
    """What populate did: the counts a Director sanity-checks first, then the project."""

    proposed: int
    created: int
    project: Project
    #: How many cuts the line-up step moved onto a phrase boundary, and how many it left where
    #: they were with a reason. Counts rather than the sentences: a chained populate's report
    #: is the timeline itself, and the per-cut reasons are what the standalone `line-up` route
    #: exists to show. 0/0 is what an unmeasured song and a 0 tolerance both produce.
    moved: int = 0
    skipped: int = 0
    #: What the declared cast needs and the library does not have — `models.vocal_cast_problems`.
    #:
    #: The Director's own placement: "this is something that could be flagged if the user labeled
    #: the song as a duet but they only have one character asset **if the user clicks Populate
    #: Timeline**". A flag rather than a refusal, and it rides the response rather than an
    #: exception because populate succeeded — the plan is laid out, and the cast is a thing to fix
    #: in another tab before the shots are wired.
    #:
    #: Defaulted empty, which is what every project that has declared no vocal type gets, and what
    #: every response written before this field existed means. A client that ignores it behaves
    #: exactly as it did.
    cast_notices: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------------------------------
# The three steps on the wire. One route each, chained by `populate_timeline`.
#
# Each route reports first and writes only on an explicit confirm, and each carries the
# previous step's report forward as `plan` — the plan-carrying idiom above, for its reason:
# lay-out spends the model call, so its confirm must apply *that* reading rather than roll
# again, and line-up and fill-in are pure functions of what lay-out returned.
#
# Reading the chain off the routes: lay-out's report is line-up's `plan`, line-up's report is
# fill-in's `plan`, and fill-in's confirm is what writes the content. A first-run
# `populate` does the same three steps in one call with one confirmation — see
# `populate_timeline`.
# ------------------------------------------------------------------------------------------

#: The plan-carrying refusals, `CLEAN_PROMPTS_NO_PLAN`'s rule in each step's own words.
LAY_OUT_NO_PLAN = (
    "This confirm carries no lay-out report, so there is nothing it could write. The windows "
    "this step writes are the ones a person read in a report: run the report, read it, then "
    "confirm with that report as `plan`. Nothing was written and no model was asked."
)
LAY_OUT_PLAN_MISMATCH = (
    "The plan sent with this confirm is not the plan this step reported: it does not match "
    "its own plan_id. Refused rather than asking the model again, because the layout that "
    "lands has to be the layout that was read. Nothing was written. Run the report again."
)
LINE_UP_NO_PLAN = (
    "Line up needs a lay-out report to line up. Run the lay-out step, then send its report "
    "here as `plan`. Nothing was written."
)
LINE_UP_PLAN_MISMATCH = (
    "The lay-out report sent to this step does not match its own plan_id, so it is not a "
    "report this server emitted. Nothing was written. Run the lay-out step again."
)
#: Line-up's confirm moves the windows of shots a lay-out already created. If the timeline is
#: not that layout — the lay-out report was never confirmed, or the plan has been edited since
#: — the shot a row addresses is not the shot it was lined up for, so it refuses rather than
#: write a window onto whatever sits at that index now. `FILL_IN_WINDOWS_CHANGED`'s rule, one
#: step earlier, and the same sentence shape.
LINE_UP_WINDOWS_CHANGED = (
    "This line-up was computed for a layout the timeline does not hold ({expected} windows "
    "reported, {found} shots on the timeline). Its confirm moves the windows of shots the "
    "lay-out step already created, so it refused rather than move a cut belonging to some "
    "other plan. Nothing was written. Confirm the lay-out first, then line it up."
)
FILL_IN_NO_PLAN = (
    "Fill in needs a line-up report to fill in. Run the lay-out step, then the line-up step, "
    "then send the line-up report here as `plan`. Nothing was written and no model was asked."
)
FILL_IN_PLAN_MISMATCH = (
    "The line-up report sent to this step does not match its own plan_id, so it is not a "
    "report this server emitted. Nothing was written. Run the line-up step again."
)
#: Fill in writes content onto windows that already exist and never moves or makes one. If
#: the timeline no longer matches the report's windows, the shot a row addresses is not the
#: shot it was written for — refused rather than written to whatever is at that index now.
#: The other half of the same promise, and the half that is about this server rather than
#: about the timeline: the shots the fill-in step produced must sit in the windows it was
#: given. Unreachable while the step reads its geometry from the alignment it was handed —
#: which is exactly why it is checked rather than argued, `CLEAN_PROMPTS_WINDOWS_MOVED`'s rule.
#: It fires on the report too, because a report that describes windows the confirm would not
#: write is worse than a refusal.
FILL_IN_WINDOWS_MOVED = (
    "Refused: filling in produced shots whose windows are not the windows it was given. "
    "Nothing was written. This step may only ever change what is inside a window — the "
    "timeline's geometry is the director's own work and is not this step's to touch."
)
FILL_IN_WINDOWS_CHANGED = (
    "The timeline's windows are not the windows this report was laid out for ({expected} "
    "shots reported, {found} on the timeline{detail}). Fill in only writes what is inside a "
    "window and never moves one, so it refused rather than write a shot's content onto a "
    "different shot. Nothing was written. If the line-up moved cuts, confirm the line-up "
    "first — that is the step that writes a window — or lay the timeline out again and walk "
    "the three steps from that report."
)


class LayOutWindowRow(BaseModel):
    """One window of the laid-out structure. Geometry only — nothing about content."""

    index: int
    start: float
    duration: float


class LayOutProposalRow(BaseModel):
    """One shot proposal from the model, carried on the report for the fill-in step.

    This is the content half of the single model call lay-out spends: lay-out reads only the
    *count* of these, and fill-in is what turns them into prompts and citations. They ride
    the report so the next step is a pure function of what a person read, rather than of a
    second ask.
    """

    index: int
    start: float
    duration: float
    prompt: str
    performance: bool = False
    assets: list[str] = Field(default_factory=list)


class LayOutSectionRow(BaseModel):
    """One section of the layout's section layer, as it would be written."""

    label: str
    start: float
    duration: float
    prompt: str = ""


class LayOutContent(BaseModel):
    """The half of a lay-out report the **fill-in step reads**, and nothing else.

    What `LineUpResponse.layout` carries, and it is a trim rather than a new fact: every field
    here is `LayOutResponse`'s own, unchanged, and `fill_in_shots` reads all three of them and
    none of the rest.

    **The defect this closes, measured live 2026-08-21.** `LineUpResponse.layout` echoed the
    whole lay-out *confirm* response, and a confirm carries `project` — a complete manifest
    copy. `lineup_report.json` came back at **125 KB** and `lineup_applied.json` at **211 KB**,
    the copyrighted lyric sheet rode the wire twice per step, and the nested `updated_at` was
    stale by one revision with nothing validating it. Harmless in what it produced —
    `layout_from_report` has always read the **live** project and ignored the carried one — but
    the client had to echo that stale manifest byte for byte or `plan_fingerprint` refused the
    confirm.

    **What the digest covers, before and after.** `plan_fingerprint` hashes every field of the
    response except `applied`, `project` and `plan_id`, and `PLAN_DIGEST_EXCLUDE` applies at the
    top level only — so *before*, the nested layout's `project`, `plan_id`, `applied` and stale
    `updated_at` were all inside the digest, along with `required`, `proposed`, `created`,
    `windows`, `sections_origin` and `message`. *After*, the digest covers `duration`,
    `proposals` and `sections`. **Nothing the next step consumes left the digest**: those three
    are the entire input `layout_from_content` gives `fill_in_shots`, and the geometry is
    covered as fully as it ever was by `LineUpResponse.windows`, which is the moved tiling
    fill-in is actually matched against shot by shot. What left is a project snapshot nothing
    reads, a digest for a different report, a flag, a stale revision, and six fields no
    downstream step consults.
    """

    #: The song length this layout was tiled across — what `paired_proposals` divides when a
    #: project has no section layer, so it must be the number the layout was computed with.
    duration: float = 0
    proposals: list[LayOutProposalRow] = Field(default_factory=list)
    #: Load-bearing since 2026-08-22: `paired_proposals` cuts the plan into these sections and
    #: pairs a window only with a proposal from its own one.
    sections: list[LayOutSectionRow] = Field(default_factory=list)


class LayOutResponse(BaseModel):
    """Step one's report, and — only on a confirmed call — the saved project.

    `project` is `None` on a report, `SnapCutsResponse`'s rule and for its reason: the
    absence is the wire's own statement that nothing was written.

    This model is also the *request* body of the confirm that applies it (`plan` on
    `LayOutRequest`) and of the line-up step (`plan` on `LineUpRequest`), which is why
    `plan_id` and `updated_at` are on it.
    """

    applied: bool = False
    #: The song length this layout was tiled across, carried rather than re-read: it is what
    #: `proposal_for_position` divides, so the step that consumes it must see the number the
    #: layout was actually computed with.
    duration: float = 0
    #: The count the model was held to (`populate_required_shots`) and the count it returned.
    required: int = 0
    proposed: int = 0
    #: Windows in the tiling. Not the same number as `proposed`: the geometry is repaired.
    created: int = 0
    windows: list[LayOutWindowRow] = Field(default_factory=list)
    proposals: list[LayOutProposalRow] = Field(default_factory=list)
    sections: list[LayOutSectionRow] = Field(default_factory=list)
    #: How much of the room H3's band leaves this layout spent on length variance — Phase D.
    #: An *input* rather than a measurement, reported so the windows above can be accounted for
    #: from the report alone, and inside `plan_fingerprint` so the confirm cannot claim a
    #: different one than the Director read. 0 is the neutral value and is what a report written
    #: before Phase D means.
    variance: float = 0
    #: `"director"`, `"structure"`, `"shots"` or `""` — see `ShotLayout.sections_origin`.
    sections_origin: str = ""
    message: str = ""
    project: Project | None = None
    #: The digest that ties this report to the confirm that applies it — `plan_fingerprint`.
    plan_id: str = ""
    #: The project revision this report was read from. Checked against the live one on the
    #: confirm with `PROJECT_CHANGED_REFUSAL`, `replace_shots`' rule and its wording.
    updated_at: datetime | None = None


class LayOutRequest(BaseModel):
    """One lay-out pass: whether it may write, and how it asks for the structure.

    `confirm_replace` is populate's own field in populate's own key, because this is the
    step that carries populate's destructive act: without it this route **reports** — the
    model is asked, the tiling is computed, and `store.save` is not called. With it, and with
    the report echoed back as `plan`, the windows land.

    `two_stage` is populate's field unchanged: ask for the song's structure in its own call
    before asking for the shots. Off by default, and that default is the honest one — the
    split answers a measured single-call failure but has never been run against a live model
    from here, and `false` is byte-for-byte the old behaviour.

    `variance` is `PopulateTimelineRequest`'s field in the same key and with the same bound —
    how much of the room H3's band leaves the layout may spend making its windows different
    lengths. 0 is the feature off and a genuine no-op; a value past
    `timeline.POPULATE_VARIANCE_MAX` is **refused here, at the edge**, rather than clamped.
    """

    confirm_replace: bool = False
    two_stage: bool = False
    variance: float = Field(
        default=POPULATE_VARIANCE_DEFAULT, ge=0, le=POPULATE_VARIANCE_MAX
    )
    plan: LayOutResponse | None = None


class SnapCutMove(BaseModel):
    """One cut that would move, named by both windows that share it.

    `gap` is how long the voiceless stretch it lands in is, carried on the wire because the
    length is what tells a Director what kind of opportunity the cut found — a one-second
    breath is an extended shot, four seconds is room for something else entirely. It is
    `timeline.CutMove.gap` verbatim; nothing is decided here.

    Shared by `SnapCutsResponse` and `LineUpResponse` rather than written twice: the two
    routes reach one snapping core through two doors, and a cut that came back described in
    two shapes would invite the two to drift in the reader's mind even while the core did not.
    `before`/`after` are `shot_label`'s names on the timeline route and
    `LINE_UP_WINDOW_LABEL`'s on a layout that has no shots yet.
    """

    before: str
    after: str
    #: Where the cut is. For an overlapping seam that is the transition's centre and neither
    #: clip has an edge there — `timeline.SEAM_POINT` argues why — which is why `overlap`
    #: travels beside it rather than leaving the number unexplained.
    boundary: float
    proposed: float
    shift: float
    gap: float
    #: How long the transition at this seam is, 0 for a hard cut. `timeline.CutMove.overlap`
    #: verbatim. It is the same before and after the move: a transition is authored with a
    #: length the Director chose, so snapping moves both its edges together and resizes
    #: nothing.
    overlap: float = 0


class SnapCutSkip(BaseModel):
    """One cut that would not move, and the sentence saying why."""

    before: str
    after: str
    boundary: float
    reason: str


class LineUpLineRow(BaseModel):
    """One sheet line sung across a window — `timeline.LyricLineSpan` on the wire.

    `index` addresses the line in the Director's own lyric sheet, so a reader can find the
    very line the mark was typed on. `slots` are the `(S1)` mark's character slots and `[]`
    is **untagged**, never "the first singer".
    """

    index: int
    text: str = ""
    slots: list[int] = Field(default_factory=list)
    start: float = 0
    end: float = 0


class LineUpWindowRow(BaseModel):
    """One window with the musical facts measured against it — `ShotPlacement` on the wire.

    `vocal_seconds` is `None` for unmeasured, which is not the same as silent: a project
    whose song has never been transcribed reports `None` on every row and `voiceless` false
    on every row, and fill-in's guard is then a no-op.

    `lines` and `singers` are Phase B's new facts and the seam the multi-character work reads.
    `singers` is written from `ShotPlacement.singers` and read back through it, so the wire
    cannot carry a slot no line on the row names.
    """

    index: int
    start: float
    duration: float
    section: str = ""
    vocal_seconds: float | None = None
    voiceless: bool = False
    lines: list[LineUpLineRow] = Field(default_factory=list)
    #: Every character slot marked as singing across this window, ascending. A projection of
    #: `lines`; `[]` is untagged.
    singers: list[int] = Field(default_factory=list)


class LineUpResponse(BaseModel):
    """Step two's report, and — only on a confirmed project-sourced call — the saved project.

    Two shapes of the same report, because the step has two entry points:

    * **From a lay-out report** (`plan` on the request). Nothing is written on any path: the
      windows this describes do not exist yet, and it is fill-in's confirm — or the chain's
      single save — that lands them. `project` stays `None` and `applied` stays false.
    * **From the project's own timeline** (no `plan`). Then this is report-then-confirm in
      `SnapCutsResponse`'s exact shape: `moves` and `skips` name every cut, and only a call
      carrying `confirm_apply` writes the windows.

    `status` is the snapping core's four-way answer and three of its values mean *nothing was
    examined* — see `ShotAlignment`.
    """

    applied: bool = False
    measured: bool = False
    #: The core's `"ready"` / `"off"` / `"unmeasured"` / `"no_cuts"`.
    status: str = "ready"
    #: How far a cut was allowed to travel on this pass. 0 is the feature switched off.
    tolerance: float = 0
    moved: int = 0
    skipped: int = 0
    moves: list[SnapCutMove] = Field(default_factory=list)
    skips: list[SnapCutSkip] = Field(default_factory=list)
    windows: list[LineUpWindowRow] = Field(default_factory=list)
    #: The content half of the lay-out report this was lined up from, carried so fill-in needs
    #: only this one body. **Trimmed to `LayOutContent` on 2026-08-22** — it used to be the
    #: whole `LayOutResponse`, manifest copy and all; see there for the sizes and for what the
    #: digest covers before and after. `None` on the project-sourced path, and that absence is
    #: what makes such a report unusable as a fill-in input — correctly, because it describes
    #: windows rather than content.
    layout: LayOutContent | None = None
    message: str = ""
    project: Project | None = None
    plan_id: str = ""
    updated_at: datetime | None = None


class LineUpRequest(BaseModel):
    """One line-up pass: which windows, how far a cut may travel, and whether it may write.

    **`plan` is what picks the entry point.** With a lay-out report, line-up is a pure
    function of that report — the by-hand walk through the three routes, where its confirm
    moves the windows the lay-out step's confirm created. Without one, it lines up the
    timeline the project already has, which is the pass a Director runs on a plan they have
    been editing.

    `tolerance` is `SnapCutsRequest`'s field in the same key, with the same bound and the same
    meaning: how far a cut may travel, 0 being the feature switched off and a genuine no-op.

    `confirm_apply` is `SnapCutsRequest`'s too, and on both paths it writes the same thing and
    only that thing: each shot's `start` and `duration`. Nothing else on a shot is read or
    written by this route, on any path.
    """

    plan: LayOutResponse | None = None
    tolerance: float = Field(
        default=SNAP_TOLERANCE_DEFAULT, ge=0, le=SNAP_TOLERANCE_MAX
    )
    confirm_apply: bool = False


class FillInShotRow(BaseModel):
    """One filled shot in the report: what would be written into that window.

    `start` and `duration` ride along and are never written back. They are here so the
    report itself is evidence that the windows did not move — `CleanPromptRow`'s rule.
    """

    index: int
    start: float
    duration: float
    prompt: str = ""
    citations: list[AssetCitation] = Field(default_factory=list)
    singing: str = ""
    use_song_audio: bool = False
    seed: int = 0


class FillInResponse(BaseModel):
    """Step three's report, and — only on a confirmed call — the saved project.

    `project` is `None` on a report, `SnapCutsResponse`'s rule and for its reason.
    """

    applied: bool = False
    filled: int = 0
    shots: list[FillInShotRow] = Field(default_factory=list)
    message: str = ""
    project: Project | None = None
    plan_id: str = ""
    updated_at: datetime | None = None


class FillInRequest(BaseModel):
    """One fill-in pass: the line-up report it fills from, and whether it may write.

    `confirm_apply` is `SnapCutsRequest`'s field in the same key and for the same reason.

    **This step deliberately inherits none of lay-out's refusals.** A locked shot, an
    approved take and a render in flight all refuse a lay-out, because a lay-out replaces the
    windows those protections were placed on. Fill in writes inside windows it never touches,
    and that is checked rather than promised: the window fingerprint is taken before the
    writes and compared after, and a mismatch refuses without saving.
    """

    confirm_apply: bool = False
    plan: LineUpResponse | None = None


# `plan` names a response model declared above it, and `from __future__ import annotations`
# makes that a forward reference — rebuilt here rather than left for whatever first touches
# them to discover. `SectionLooksRequest`'s line, for its reason.
LayOutRequest.model_rebuild()
LineUpRequest.model_rebuild()
FillInRequest.model_rebuild()


# The four translations between the steps' intermediates and their wire forms. They are the
# only place either shape is built, so "what the intermediate carries" has one spelling, and a
# field added to a step's output either travels or fails to compile here.


def layout_report(layout: ShotLayout) -> LayOutResponse:
    """`ShotLayout` as a report a person can read and the next step can be handed."""
    return LayOutResponse(
        duration=layout.duration,
        required=layout.required,
        proposed=len(layout.proposals),
        created=len(layout.windows),
        windows=[
            LayOutWindowRow(index=index, start=start, duration=length)
            for index, (start, length) in enumerate(layout.windows)
        ],
        proposals=[
            LayOutProposalRow(
                index=index,
                start=proposal.start,
                duration=proposal.duration,
                prompt=proposal.prompt,
                performance=proposal.performance,
                assets=list(proposal.assets),
            )
            for index, proposal in enumerate(layout.proposals)
        ],
        sections=[
            LayOutSectionRow(
                label=section.label,
                start=section.start,
                duration=section.duration,
                prompt=section.prompt,
            )
            for section in layout.sections
        ],
        sections_origin=layout.sections_origin,
        variance=layout.variance,
        message=(
            f"{len(layout.windows)} windows laid out from {len(layout.proposals)} "
            f"proposals across {layout.duration:.1f}s"
        ),
    )


def layout_content(plan: LayOutResponse) -> LayOutContent:
    """A lay-out report trimmed to what the fill-in step reads. The one projection.

    Built here rather than at the two `alignment_report` call sites so "what travels to fill-in"
    has a single spelling: a field added to `LayOutContent` and forgotten here would ship empty
    on every path at once rather than on one of them.
    """
    return LayOutContent(
        duration=plan.duration,
        proposals=[row.model_copy(deep=True) for row in plan.proposals],
        sections=[row.model_copy(deep=True) for row in plan.sections],
    )


def layout_from_content(project: Project, content: LayOutContent) -> ShotLayout:
    """The trimmed lay-out content back into the intermediate the fill-in step reads.

    `layout_from_report`'s sibling for the smaller body, and its rule verbatim: `project` is the
    **live** project, never one carried on a report, and the revision check every route runs
    before calling this is what makes that safe.

    `required`, `windows`, `sections_origin` and `message` come back empty, and the emptiness is
    a statement rather than a loss — `fill_in_shots` reads none of them, and the geometry it is
    matched against is `LineUpResponse.windows` (the *moved* tiling), which the route compares
    to the live timeline row by row before anything is written.
    """
    return ShotLayout(
        project=project,
        duration=content.duration,
        required=0,
        proposals=tuple(
            ShotProposal(
                start=row.start,
                duration=row.duration,
                prompt=row.prompt,
                performance=row.performance,
                assets=tuple(row.assets),
            )
            for row in content.proposals
        ),
        windows=(),
        sections=tuple(
            SongSection(
                label=row.label,
                start=row.start,
                duration=row.duration,
                prompt=row.prompt,
            )
            for row in content.sections
        ),
        sections_origin="",
        message="",
    )


def layout_from_report(project: Project, plan: LayOutResponse) -> ShotLayout:
    """A lay-out report back into the intermediate the next steps read.

    `project` is the **live** project, not one carried on the report: the later steps read the
    library, the identity sheets and the song from it, and a report is a statement about
    geometry rather than a snapshot of the manifest. What makes that safe is the revision
    check every route runs before calling this — the report is refused unless the project is
    still the revision it was read from.
    """
    return ShotLayout(
        project=project,
        duration=plan.duration,
        required=plan.required,
        proposals=tuple(
            ShotProposal(
                start=row.start,
                duration=row.duration,
                prompt=row.prompt,
                performance=row.performance,
                assets=tuple(row.assets),
            )
            for row in plan.proposals
        ),
        windows=tuple((row.start, row.duration) for row in plan.windows),
        sections=tuple(
            SongSection(
                label=row.label,
                start=row.start,
                duration=row.duration,
                prompt=row.prompt,
            )
            for row in plan.sections
        ),
        sections_origin=plan.sections_origin,
        message=plan.message,
        variance=plan.variance,
    )


def alignment_report(
    alignment: ShotAlignment, layout: LayOutResponse | None
) -> LineUpResponse:
    """`ShotAlignment` as a report, with the lay-out report's *content half* carried on it.

    `layout` is `None` for a line-up sourced from the project's own timeline: there was no
    lay-out report, and inventing one would hand fill-in a plan with no proposals in it.

    Trimmed through `layout_content` since 2026-08-22 — the whole report used to travel, a
    manifest copy with it. See `LayOutContent`.
    """
    voiced = sum(1 for placement in alignment.placements if not placement.voiceless)
    lined = sum(1 for placement in alignment.placements if placement.lines)
    return LineUpResponse(
        measured=alignment.measured,
        status=alignment.status,
        tolerance=alignment.tolerance,
        moved=alignment.moved,
        skipped=len(alignment.skips),
        moves=[
            SnapCutMove(
                before=move.before_label,
                after=move.after_label,
                boundary=move.boundary,
                proposed=move.proposed,
                shift=move.shift,
                gap=move.gap,
                overlap=move.overlap,
            )
            for move in alignment.moves
        ],
        skips=[
            SnapCutSkip(
                before=skip.before_label,
                after=skip.after_label,
                boundary=skip.boundary,
                reason=skip.reason,
            )
            for skip in alignment.skips
        ],
        windows=[
            LineUpWindowRow(
                index=placement.index,
                start=placement.start,
                duration=placement.duration,
                section=placement.section,
                vocal_seconds=placement.vocal_seconds,
                voiceless=placement.voiceless,
                lines=[
                    LineUpLineRow(
                        index=line.index,
                        text=line.text,
                        slots=list(line.slots),
                        start=line.start,
                        end=line.end,
                    )
                    for line in placement.lines
                ],
                # The property, never a second sum over the rows above: one implementation of
                # "who sings across this window", and the wire is a copy of its answer.
                singers=list(placement.singers),
            )
            for placement in alignment.placements
        ],
        layout=layout_content(layout) if layout is not None else None,
        message=(
            f"{len(alignment.placements)} windows measured against the track, "
            f"{voiced} carrying voice, {lined} carrying lyric lines, "
            f"{alignment.moved} moved onto a phrase boundary, {len(alignment.skips)} left"
            if alignment.measured
            else f"{len(alignment.placements)} windows; the song's voice has not been "
            f"measured, so no window carries a vocal fact and {alignment.moved} moved"
        ),
    )


def alignment_from_report(project: Project, plan: LineUpResponse) -> ShotAlignment:
    """A line-up report back into the intermediate the fill-in step reads.

    `singers` is deliberately **not** read off the wire: `ShotPlacement.singers` derives it
    from `lines`, and reading a second copy back would let a hand-edited body claim a singer
    no line names. The moves and skips are not read back either — they are the report of what
    the pass did, and fill-in decides nothing from them.
    """
    return ShotAlignment(
        layout=layout_from_content(project, plan.layout or LayOutContent()),
        placements=tuple(
            ShotPlacement(
                index=row.index,
                start=row.start,
                duration=row.duration,
                section=row.section,
                vocal_seconds=row.vocal_seconds,
                voiceless=row.voiceless,
                lines=tuple(
                    LyricLineSpan(
                        index=line.index,
                        text=line.text,
                        slots=tuple(line.slots),
                        start=line.start,
                        end=line.end,
                    )
                    for line in row.lines
                ),
            )
            for row in plan.windows
        ),
        measured=plan.measured,
        moved=plan.moved,
        status=plan.status,
        tolerance=plan.tolerance,
    )


class SnapCutsRequest(BaseModel):
    """One snap pass: how far a cut may travel, and whether this call is allowed to write.

    `confirm_apply` is `populate`'s `confirm_replace` in a smaller key: the default is a
    **report**, and only an explicit true writes. The report step is the point of the feature
    as much as the moves are — "22 cuts moved, 3 skipped" is the moment a Director notices
    that three is wrong (`spec-arm-a-plan`'s argument, and the roadmap keeps it on its own
    merits). The server enforces it rather than trusting the browser to ask first.

    `tolerance` is bounded by the schema, so a client cannot reach past `SNAP_TOLERANCE_MAX`
    into a "snap" that rewrites the plan's rhythm; 0 is admissible and is the feature
    switched off, which `snap_cut_plan` answers as a genuine no-op.
    """

    tolerance: float = Field(
        default=SNAP_TOLERANCE_DEFAULT, ge=0, le=SNAP_TOLERANCE_MAX
    )
    confirm_apply: bool = False


class SnapCutsResponse(BaseModel):
    """The report, and — only on an applied call — the saved project.

    `project` is `None` on a report, and that absence is load-bearing: it is the wire's own
    statement that nothing was written. A client that redraws from it would be redrawing the
    manifest it already has.
    """

    applied: bool
    status: str
    tolerance: float
    moved: int
    skipped: int
    moves: list[SnapCutMove]
    skips: list[SnapCutSkip]
    message: str = ""
    project: Project | None = None


class AssetReplacementRequest(BaseModel):
    """Which asset takes over, and whether this call is allowed to write.

    `confirm_apply` is `SnapCutsRequest`'s field in the same key and for the same reason, which
    is `populate`'s `confirm_replace` at bottom: the default is a **report**, and only an
    explicit true writes. Server-enforced rather than trusted to the browser — the report is the
    moment a Director sees that eight of their thirty shots already cite the replacement, and a
    client that skipped showing it must not be able to skip the decision.
    """

    replacement_id: str
    confirm_apply: bool = False


class AssetReplacementShot(BaseModel):
    """One shot the replacement changes, named as the timeline names it.

    `roles` is every role the replaced asset held on this shot, so the "role and order are
    carried across" guarantee is checkable from the report itself. `carried_label` is the
    `reference_labels` entry that travels, `""` when none does.

    `provenance` is `"approved"`, `"rendered"` or `""` — whether this shot already holds a take
    produced against the old asset. It changes nothing about what happens to the shot; it is why
    the row is also counted into `rendered`/`approved` and named in `notes`.
    """

    shot_id: str
    label: str
    roles: list[str]
    carried_label: str = ""
    provenance: str = ""


class AssetReplacementSkip(BaseModel):
    """One shot left exactly as it is, and the sentence saying why. `BatchSkippedShot`'s shape."""

    shot_id: str
    label: str
    reason: str


class AssetReplacementResponse(BaseModel):
    """The report, and — only on an applied call — the saved project.

    `project` is `None` on a report, `SnapCutsResponse`'s rule verbatim: the absence is the
    wire's own statement that nothing was written.

    `merged` is the Director's "already in N shots" — the shots that cite both, where the old
    citation is removed and the standing one is left alone. `still_cited` is what the delete this
    was reached from will meet next, which is why the counts are three and not two.

    `rendered` and `approved` count shots **that are being changed** and already hold a take made
    against the old asset. They are not skips and do not overlap `skipped`: they are a subset of
    `swapped` + `merged`, reported because the consequence is real and unrecoverable. `notes`
    carries the sentences naming them, which is what a client draws.
    """

    applied: bool
    replaced: str
    replacement: str
    swapped: int
    merged: int
    skipped: int
    still_cited: int
    rendered: int = 0
    approved: int = 0
    swaps: list[AssetReplacementShot] = Field(default_factory=list)
    merges: list[AssetReplacementShot] = Field(default_factory=list)
    skips: list[AssetReplacementSkip] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    warning: str = ""
    message: str = ""
    project: Project | None = None


def _replacement_row(change: ReplacementChange) -> AssetReplacementShot:
    """One planned change on the wire. The two buckets are the same row shape, so it is written
    once — a second copy is a second thing to keep true."""
    return AssetReplacementShot(
        shot_id=change.shot_id,
        label=change.label,
        roles=list(change.roles),
        carried_label=change.carried_label,
        provenance=change.provenance,
    )


class GenerateBatchRequest(BaseModel):
    """One batch, one confirmation. `scope` picks FR-4's ready set, AD-5's flagged set, or the
    `empty` set — every shot with no video yet (`batch_targets`, 2026-08-23);
    `replace_existing` widens the ready scope to settled, unprotected shots; `profile`
    applies one evidenced sampling bundle to the whole batch (per-shot profiles are Ask
    First). `confirm_gpu` is the acknowledgement itself — a client sends true only after
    showing the warning, exactly like `confirm_song_replacement`.

    `empty` is a third value on the field that already existed rather than a second route, and
    that is the whole of its implementation cost: the readiness gate, the per-shot refusals, the
    bundle resolution, the batch id, the seed stride and the report are the ones this route
    already had."""

    confirm_gpu: bool = False
    scope: Literal["ready", "flagged", "empty"] = "ready"
    replace_existing: bool = False
    # `None` rather than `"default"`, and the distinction is the whole of the fix: an omitted
    # profile now means "whatever this project is set to", which is how one setting reaches the
    # batch and the single-shot re-render alike. A named one still overrides for that submission.
    # See `H3Request.profile`, whose `None` this is handed straight to.
    profile: SamplingProfile | None = None


class BatchSubmittedShot(BaseModel):
    shot_id: str
    label: str
    job_id: str


class BatchSkippedShot(BaseModel):
    shot_id: str
    label: str
    reason: str


class BatchSubmissionResponse(BaseModel):
    """FR-4's report: what queued and what was skipped, each by name with a sentence.
    `batch_id` is empty when nothing submitted — a batch that never formed has no id."""

    batch_id: str
    submitted: list[BatchSubmittedShot]
    skipped: list[BatchSkippedShot]


class CancelledJob(BaseModel):
    """One render this cancellation stopped, under the name the queue panel already draws."""

    job_id: str
    label: str


class UncancelledJob(BaseModel):
    """One render the cancellation did **not** stop, with the route's own sentence for why.

    `BatchSkippedShot`'s shape and its reason: a job that refused must be reported by name with
    the refusal that produced it, never counted into a total the Director cannot account for.
    The overwhelmingly common entry here is a job that settled between the click and the loop
    reaching it — `cancel_job`'s own `CANCEL_JOB_SETTLED`, which is not a failure at all.
    """

    job_id: str
    label: str
    reason: str


class CancellationReport(BaseModel):
    """What one whole-queue cancellation did: every job it stopped, and every job it did not.

    `BatchSubmissionResponse`'s shape deliberately, because it answers the same question in the
    other direction and the Director reads the two toasts in the same session.
    """

    cancelled: list[CancelledJob]
    skipped: list[UncancelledJob]


class AssetEditRequest(BaseModel):
    """AI Mod's wire shape: the edit in the Director's words, and which evidenced bundle.

    `instruction` is a plain sentence by default — the route wraps it in the workflow's
    own prompting form — or the full structured prompt when it carries
    `subject_definitions:`, which travels verbatim. The two profiles are the two imported
    exports' bundles and nothing in between; a `Literal` so an unknown one is a 422
    before any payload exists.
    """

    instruction: str = Field(min_length=1)
    profile: Literal["default", "turbo"] = "default"
    seed: int = Field(default=0, ge=0, le=0xFFFFFFFFFFFFFFFF)


class AssetConsistencyRequest(BaseModel):
    """The appearance anchor's whole wire shape: one field, and deliberately only one.

    A general "update this asset" body would put `name`, `kind`, `path` and `prompt` on the
    wire beside it, each defaulted, and the route binding it could not tell an edit of one
    from an omission of the other four — which is precisely the shape that made the generic
    full-project `PUT` a data-loss hole three times over. One field means an omission and a
    clear are the same instruction, which is what the Director means by emptying the box.
    """

    consistency_prompt: str = ""


#: The longest display name an asset may be renamed to.
#:
#: Short on purpose, and shorter than `CONSISTENCY_PROMPT_LIMIT` by an order of magnitude
#: because the two are different kinds of text: an anchor is a sentence describing an
#: appearance, a name is what the Director calls this picture out loud. The name is also the
#: token the prose scan matches on (`models.assets_for_proposal`) and the label
#: `timeline.anchored_label` composes into every reference map, so a name long enough to be a
#: description would be a second anchor written in the wrong box.
ASSET_NAME_LIMIT = 80

#: A name cannot be cleared, which is the one place this route parts company with its siblings.
#: An empty anchor means "no anchor" and an empty slot means "not one of the singers", but an
#: asset with no name is a row in the library nobody can pick, a citation nobody can read back,
#: and a reference map line that names nothing. Refused before anything is assigned.
ASSET_NAME_EMPTY = (
    "An asset needs a name — it is what the library lists, what a reference map line calls "
    "this picture, and what you type to find it. Send the name you want; there is no way to "
    "leave one blank."
)
ASSET_NAME_TOO_LONG = (
    "{name} would be renamed to {length} characters, and a display name is bounded at "
    "{limit:g}. A name is what you call this picture, not a description of it — the "
    "appearance anchor is the box for that."
)
#: What a rename does, and — the half worth saying — what it does not. The Director's ask
#: (2026-08-22), because the two halves are easy to assume wrongly in *either* direction: that a
#: rename breaks the shots citing this asset (it cannot — a citation is by id), and that a
#: rename rewrites the prose that already spells the old name (it does not — those are words a
#: model or a person wrote, and this route does not edit anybody's prose).
ASSET_RENAME_APPLIED = (
    "Renamed {previous} to {name}. Every citation follows the asset by id, so no shot lost its "
    "reference{maps}.{prompts}{children}{scan}"
)
ASSET_RENAME_MAPS = ", and {count} reference map(s) were re-derived under the new name"
ASSET_RENAME_PROMPTS = (
    " {count} shot prompt(s) still spell {previous}: a rename does not rewrite prose that was "
    "already written, so those keep the old name until they are re-expanded or cleaned."
)
#: The fourth consequence, added 2026-08-23: a rename reaches the one asset it was pointed at.
#:
#: `generate_multiview` mints its child as `f"{source.name} · multiview"` and `edit_asset` as
#: `f"{source.name} · edit"`, **frozen at creation** — there is no derivation to re-run, only a
#: string that was composed once. So renaming a parent leaves the child spelling the old name in
#: the library, in `citable_assets`, and (for an ` · edit` child, which `citable_assets` does not
#: hide) on the roster the model reads.
#:
#: **Reported rather than propagated, and that is a decision.** A child's name is editorial the
#: moment it exists: this very route treats the whole display name as the Director's to set, and
#: the Director's own successful fix was renaming the *child* directly. A parent rename that
#: rewrote its children would silently overwrite exactly that — and edits chain, so it would walk
#: an unbounded tree from one gesture, in an application with no undo for it. The count is what
#: makes the alternative actionable: rename the child on this same route.
ASSET_RENAME_CHILDREN = (
    " {count} derived asset(s) keep their own names and this rename did not reach them; {stale} "
    "still spell {previous}. A ` · multiview` or ` · edit` suffix is frozen into the child's "
    "name when it is minted, so rename each child on this same route."
)
#: The prose fallback, and the two ways a rename walks a name across its fence without saying so.
#:
#: `models.assets_for_proposal` cites an asset two ways: what the shot *declared* — by id or by
#: exact display name, and exact is exact at any length — and, as a fallback, whether the shot's
#: own prose contains the name. The fallback is a plain case-insensitive substring test, which is
#: why it is fenced below `NAME_SCAN_MIN_LENGTH`. A rename is the one gesture that can move a name
#: from one side of that fence to the other, and neither direction breaks anything a Director
#: would notice at the time: citations resolve by id, so the plan keeps working either way.
ASSET_RENAME_UNSCANNABLE = (
    " {name} is {length} character(s), under the {minimum}-character floor the prose scan will "
    "trust, so a shot that names this picture in its prose without citing it no longer picks it "
    "up. Declared citations are unaffected — those match by id or exact name, at any length."
)
ASSET_RENAME_OVER_MATCHES = (
    " {count} shot prompt(s) in this plan already contain {name} without citing this picture: "
    "the prose scan is a plain substring test, so a short name sitting inside an ordinary word "
    "matches it. Those shots would pick this picture up the next time they are filled in."
)


class AssetNameRequest(BaseModel):
    """This asset's display name. One field, `AssetConsistencyRequest`'s argument verbatim.

    A general "update this asset" body would carry `kind`, `path` and `prompt` beside it, each
    defaulted, and the route binding it could not tell an edit of one from an omission of the
    rest — the exact shape that made the generic full-project `PUT` a data-loss hole seven
    times. One field means the body says one thing.

    **No default**, which is where this parts from the anchor and follows `SongVocalTypeRequest`:
    an omitted name would arrive as `""`, and `""` is not a name a caller could have meant. A
    body without one is a 422 from the schema, which is the loud version of the same mistake.
    """

    name: str


class AssetRenameResponse(BaseModel):
    """The saved project, plus the sentence saying what the rename touched and what it did not.

    `SnapCutsResponse`' shape — the project a client adopts, beside a message a person reads —
    rather than the bare `Project` its two sibling asset routes return, and the difference is
    earned: setting an anchor or a slot has no consequence outside the field, while a rename
    has five, four of which are invisible from the manifest. Citations survive (they are by id),
    reference maps are re-derived where they can be for free, **prose already written keeps
    the old name**, **derived children keep their own names**, and the new spelling may sit on
    the other side of the prose scan's length fence from the old one. A Director who is not told
    the third will read the first prompt they open as evidence the rename failed; a Director not
    told the fourth will believe a leak is closed that is still open on a child; and neither of
    the fifth's two directions is visible from anywhere.
    """

    project: Project
    #: The name as stored — trimmed, so a client sees what actually landed.
    name: str = ""
    previous: str = ""
    #: Shots whose `prompt` still contains the old name. Counted on the prose the Director and
    #: the model wrote, which this route deliberately does not edit.
    prompts: int = 0
    #: Reference maps `refresh_reference_maps` was able to re-derive for free under the new name.
    maps: int = 0
    #: Assets minted *from* this one (`Asset.parent_id`), which this route does not rename.
    children: int = 0
    #: How many of those still spell the old name — the ones with something left to do.
    children_stale: int = 0
    #: Whether the new name reaches `NAME_SCAN_MIN_LENGTH`, and so whether the prose fallback in
    #: `assets_for_proposal` will consider it at all. `True` is the ordinary case and says nothing.
    scannable: bool = True
    #: Shots whose prose already contains the **new** name without citing this asset — the
    #: over-match the substring scan would make. Zero when the name is too short to be scanned,
    #: because a name the scan skips cannot over-match either.
    prose_matches: int = 0
    message: str = ""


class DefaultSettingRequest(BaseModel):
    """Which `setting` Asset is this video's location — one field, `AssetConsistencyRequest`'s
    argument verbatim: an omission and a clear must mean the same thing, and a body carrying the
    rest of the project alongside it is the shape that made the generic `PUT` a data-loss hole."""

    asset_id: str = ""


#: The refusal for a location that is not a location. Named rather than generic, because "422"
#: on a picker is indistinguishable from a bug: the Director picked a real asset and it was the
#: wrong kind of real asset.
DEFAULT_SETTING_NOT_A_SETTING = (
    "{name} is a {kind} asset, and a video's location has to be a setting. Pick a setting "
    "asset, or send an empty asset_id to declare no location."
)


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


class AudioRestoreResponse(BaseModel):
    """What restoring a shot's song audio returns: the job, and the two lengths involved.

    A richer reply than the enhancer's bare `RenderJob`, and the extra fields are the spec's
    length-mismatch row rather than decoration. That row asks for a mismatch to be "reported
    with both numbers; never silently padded or cut" — the saver's `trim_to_audio: False` is
    the second half, and this is the first. A number reported in a log line nobody reads is not
    a report.

    `requested_picture_seconds` is deliberately not called "picture_seconds": it is what the
    render *asked H3 for*, on the same 17k+5 grid, and not a measurement of the file. The
    matrix's frame-count row says the count is measured and never asserted equal to the input,
    and this application does not open video files — `ffprobe` does, on the two files, after the
    run. See `workflows.audio_replace_lengths`.
    """

    job: RenderJob
    #: The length of the window this stage sends, in seconds — the **take's** own seconds of the
    #: song, not the exposed slice's. A 2.083 s micro-cut's take is 4.4583 s long and gets
    #: 4.4583 s of song, beginning `latest_take_lead` before the window.
    audio_seconds: float
    #: Seconds of picture a render of `latest_take_start`/`latest_take_duration` asks for, from
    #: `timeline.over_render_frames`. Equal to `audio_seconds` except where the song ends before
    #: the picture does, which is the only way the two numbers *this route computes* can differ.
    #:
    #: **Whether that is the count the submission sent is `describes_take`'s question, not this
    #: field's** (corrected 2026-08-21). It read "the same count the submission sent", and that
    #: was a guarantee the code did not provide: the window was rebuilt from the shot's live
    #: `start`/`duration`, which go on being edited after the take is fixed. It is the count the
    #: submission sent whenever `describes_take` is true, and a count read off the current plan
    #: when it is false.
    requested_picture_seconds: float
    requested_frames: int
    #: Whether the three numbers above describe **this take** or merely the shot as it reads now.
    #: True when the take carried a window snapshot to compute them from; false for a take
    #: rendered before those existed and for a hand-picked clip. `length_note` says the same
    #: thing in the sentence the Director reads — this is the machine-readable half, so a client
    #: never has to match on prose to know which kind of answer it is holding.
    describes_take: bool
    #: False when the two above differ by more than half a frame at 24 fps. Half a frame rather
    #: than an exact comparison because both are floats and one is a division.
    lengths_match: bool
    #: Both numbers in one sentence, always populated — a report that only appears on mismatch
    #: is a report the Director cannot tell from a report that failed to run.
    length_note: str


# ------------------------------------------------------------------------------------------
# Assembly (FR-22, AD-9). The plan-shaped refusals — unapproved, stale, gaps — live in
# `assembly.py` beside the logic that decides them; these are the route's own: state
# conflicts, the song, and what a failed stage writes on the job.
# ------------------------------------------------------------------------------------------

ASSEMBLY_BUSY_REFUSAL = (
    "An assembly is already running for this project. One export at a time — wait for it "
    "to finish."
)
ASSEMBLY_RENDERS_OPEN_REFUSAL = (
    "{count} render job(s) are still in flight for this project. Assembly reads the "
    "manifest as it stands, and a landing job would change it mid-read. Let the queue "
    "settle, then assemble."
)
ASSEMBLY_NO_SONG_REFUSAL = (
    "This project has no song on record. Assembly synchronizes the video to the master "
    "song — add or generate one first."
)
ASSEMBLY_SONG_FILE_REFUSAL = (
    "The project's song is not on disk: {path}. Restore the file, then assemble."
)
ASSEMBLY_SONG_UNREADABLE_REFUSAL = (
    "The project's song could not be measured as audio: {path}. Assembly cannot verify "
    "a duration against a file ffprobe cannot read."
)
ASSEMBLY_TAKE_UNREADABLE_REFUSAL = (
    "{shot}'s approved take could not be read as video: {path}."
)
#: What an interrupted assembly's job says after a restart. The in-process registry is the
#: only thing that can settle a local job, so a running assembly job with no live process
#: behind it is a job nothing will ever finish — healed to `error` rather than left to
#: block every future assembly.
ASSEMBLY_ORPHANED_ERROR = (
    "This assembly was interrupted by an application restart and did not finish."
)
ASSEMBLY_STAGE_FAILED_ERROR = "Assembly failed at the {stage} stage: {detail}"


def heal_orphaned_local_jobs(project: Project, live_job_ids: Container[str]) -> list[RenderJob]:
    """Settle every local job with no process behind it. One rule, two callers.

    A **local** job is one with an empty `prompt_id` — the marker `reconcilable_jobs` and the
    frontend poll both key on, and today that is exactly the assembly job (`kind="post"`,
    `target_id="assembly"`). Nothing on ComfyUI knows about it, so the in-process registry
    handed in as `live_job_ids` is the only thing that can say it is still running; a
    non-terminal local job absent from that registry is one nothing will ever settle.

    A job carrying a `prompt_id` is **never** touched here, and that is the whole boundary.
    ComfyUI is user-managed and outlives this process: a prompt submitted before a restart may
    be executing on the Director's GPU right now, and healing it to `error` on the strength of
    our own restart would throw away a render being paid for in GPU minutes. Those jobs are
    the reconciler's — the queue, then history, then the three-tick settle — which asks ComfyUI
    rather than assuming.

    Called by the assemble route with the live registry, and once at startup with an empty one,
    where "empty" is not a convenience: a process that has just started is running no
    assemblies, so the registry *is* empty, and passing it makes the two callers the same
    question rather than two rules that can drift. Mutates and returns what it changed; the
    caller decides whether to save.
    """
    healed = [
        job
        for job in project.jobs
        if job.kind == "post"
        and not job.prompt_id
        and job.status not in TERMINAL_JOB_STATUSES
        and job.id not in live_job_ids
    ]
    for job in healed:
        job.status = "error"
        job.error = ASSEMBLY_ORPHANED_ERROR
        # A settle, and stamped like every other one. The span it records runs from the export
        # being enqueued to the boot that noticed the crash, which is not how long the export
        # ran — `render_timing_summary` reports a non-`complete` job as exactly that.
        stamp_job_settled(job)
    return healed


def heal_orphaned_local_jobs_at_startup(store: ProjectStore) -> int:
    """Apply `heal_orphaned_local_jobs` to every readable project, once, at boot.

    The gap this closes: a crash mid-export left the assembly job at `running`, and the only
    thing that healed it was the *next assemble*. A Director reopening the project after a
    crash therefore saw an export in progress that nothing would ever finish, and every gate
    counting open local work went on refusing, until they happened to assemble again. Boot is
    the honest moment for that verdict, because boot is the event that made it true.

    **Startup must not be able to fail.** `ProjectStore.list` already skips a manifest it
    cannot read or parse, so a corrupt project is invisible here rather than fatal; the
    save is guarded per project so one unwritable directory cannot take the others down with
    it; and the whole pass is guarded so no unforeseen store failure can stop the application
    from serving. A project with nothing to heal is not written at all, so the common case
    costs one read per project and no writes.

    Returns how many jobs were healed, for the log line and for tests.
    """
    healed = 0
    try:
        projects = store.list()
    # Broad on purpose: serving the application outranks this pass, so nothing the store can
    # raise — a permission error, an exotic OS failure — may reach the caller.
    except Exception:
        logger.warning("Could not list projects for startup job healing", exc_info=True)
        return 0
    for project in projects:
        # An empty registry, deliberately and not as a shortcut: this process has just
        # started, so it is running no assemblies. See `heal_orphaned_local_jobs`.
        jobs = heal_orphaned_local_jobs(project, ())
        if not jobs:
            continue
        try:
            store.save(project)
        # Broad for the same reason, one project narrower: an unwritable project directory
        # must not take the rest of the pass — or the boot — down with it.
        except Exception:
            logger.warning(
                "Could not save healed jobs for project %s", project.id, exc_info=True
            )
            continue
        healed += len(jobs)
        logger.info(
            "Healed %d interrupted local job(s) on project %s at startup", len(jobs), project.id
        )
    return healed


class AssemblyRequest(BaseModel):
    """The one thing an assemble request carries: which build to make.

    A `Literal` for `H3Request.profile`'s reason — it puts the choices in `/openapi.json`
    and turns an unknown preset into a 422 raised by *request validation*, which runs
    before the route body and therefore before any ffmpeg process exists at all.

    The default is `draft` because `draft` **is** the settings this application has
    exported with since FR-22 (`assembly.DRAFT_PRESET`): every client that never learned
    about presets — and the whole body-less history of this route — keeps getting the
    byte-identical file it got yesterday. `master` is opt-in, and moves the encoder and
    the delivery sample rate only — **neither preset normalizes loudness**, because the
    export's audio track is the Director's own master song (`assembly.MASTER_PRESET`).
    """

    preset: Literal["draft", "master"] = DEFAULT_EXPORT_PRESET


class AssemblyResponse(BaseModel):
    """What a completed assembly returns: the settled job and the measured facts.

    Everything numeric here is a measurement of the written file or the plan that built
    it, not an intention — `duration_seconds` is ffprobe's reading of the export, already
    verified against `song_seconds` within one frame before this response exists.
    """

    job: RenderJob
    #: Which preset built this file — echoed rather than assumed, because "is this the
    #: master?" is a question about a file on disk that the response is the only record of.
    preset: str
    #: Media-relative path under the project's media dir — `exports/assembly_00001.mp4`.
    export: str
    #: The URL the existing project-media route serves it at, Range service included.
    export_url: str
    duration_seconds: float
    song_seconds: float
    width: int
    height: int
    total_frames: int
    clip_count: int


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
    # `None` rather than `"default"`, for `steps`' reason one field over: an omitted profile has
    # to be distinguishable from one the Director named. It resolves to `Project.sampling_profile`
    # — the one place the choice is stored — so the batch, "Render Again" and a hand-rolled API
    # call all render the bundle the project is set to, instead of the two disagreeing silently
    # (the batch sent nothing and got 20 steps; `app.js` hardcoded `turbo` and got 4).
    #
    # A named profile still wins for that one submission, and it is a *different request* from an
    # inherited one: the keyframe and text-only branches refuse a named non-default bundle,
    # because naming a bundle those graphs cannot apply is the silent mis-logging this route
    # refuses, while a project-wide preference is a standing choice only the reference graph has
    # evidence for and those branches simply render their own way.
    #
    # `SamplingProfile` is imported rather than re-spelled — the `Literal` inside it is still what
    # puts the choices in `/openapi.json` and turns an unknown value into a 422 before any payload
    # is built. `tests/test_api.py` asserts it and the builder's table agree, so a profile added to
    # the builder and not offered here fails loudly.
    profile: SamplingProfile | None = None


class SamplingProfileRequest(BaseModel):
    """The Director's bundle choice, and deliberately nothing else beside it.

    One field, for `AssetConsistencyRequest`'s reason: a body carrying anything else would make an
    omission indistinguishable from a clear, which is the exact shape that made the generic
    full-project `PUT` a data-loss hole nine times. There is no "clear" here — a project always has
    a bundle — so the field is required rather than defaulted, and a body that omits it is a 422
    rather than a silent reset to 20 steps.
    """

    profile: SamplingProfile


def resolved_sampling_profile(requested: SamplingProfile | None, project: Project) -> str:
    """Which bundle a submission renders on: the one it named, or the project's standing choice.

    The one place that decision is made, which is what makes "one setting governs both paths"
    true by construction rather than by two call sites agreeing. `generate_batch` submits through
    `generate_h3`, and `generate_h3` asks this — so the batch button, the per-shot re-render and a
    hand-rolled API call cannot disagree about a project's bundle the way they did until now.

    `None` is not `"default"`. An omitted profile means *inherit*; `"default"` means the Director
    (or a test) named the 20-step bundle for this submission, which is why the caller keeps
    `request.profile` around for the branches that refuse a *named* bundle they cannot apply.
    """
    return project.sampling_profile if requested is None else requested


def submitted_sampling_bundle(profile: str, steps: int | None) -> SamplingBundle:
    """The provenance record for a reference submission: the bundle's name **and** its values.

    Beside `resolved_sampling_profile` because it is the other half of the same moment. That
    function decides which bundle a submission renders on; this one writes down what that
    decision resolved to, at the one instant it is true. `Project.sampling_profile` is a standing
    choice the Director changes between renders — which is the whole reason a project's takes are
    now a mixture — so nothing read later can recover what a given take ran on.

    The values come from `workflows.resolved_h3_sampling`, which is the same call
    `build_h3_reference_payload` makes to build the graph, including the `steps` override. So the
    record cannot describe a bundle other than the one submitted, and it cannot claim the
    profile's own step count for a submission that overrode it.

    Why both halves: see `SamplingBundle`. In one line — the name is readable and drifts, the
    values are the fact and cannot rot, and holding the two together is what lets a later reader
    *detect* that `H3_REFERENCE_PROFILES` has moved rather than merely hope it has not.

    `lora`/`lora_strength` fall to `""`/`0.0` for a bundle that applies none; `H3SamplingProfile`
    guarantees the pair is either both set or both `None`, so this cannot record a strength
    without the LoRA it was applied to.
    """
    sampling = resolved_h3_sampling(profile, steps)
    return SamplingBundle(
        name=profile,
        steps=sampling.steps,
        sampler=sampling.sampler,
        scheduler=sampling.scheduler,
        lora=sampling.lora or "",
        lora_strength=0.0 if sampling.lora_strength is None else sampling.lora_strength,
    )


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


#: The modes whose H3 prompt opens with an instruction line stating how each picture aligns to
#: a time in the target video. Text-to-video has none and must not be given one; the guide's
#: checklist treats an instruction on a text-only prompt as a mode confusion.
#:
#: `references` is deliberately absent: full-reference mode has its own six-section structure
#: rather than the keyframe instruction line, and lumping it in here would have the specialist
#: open a reference prompt with a sentence that belongs to a different mode.
H3_KEYFRAME_MODES: frozenset[str] = frozenset(
    {"image_to_video", "first_last", "first_middle_last"}
)

#: Why one Shot cannot be expanded. Separate wordings because the fixes are different: one
#: is "write an intent first", the other two are "this Shot is not yours to rewrite".
EXPAND_PROMPT_WITHOUT_INTENT = (
    "{shot} has no intent to expand. Pass one writes the short intent; this pass turns that "
    "into the H3 format. Write or generate an intent first."
)
EXPAND_PROMPT_LOCKED = "{shot} is locked, so nothing automated may rewrite it."
EXPAND_PROMPT_RENDERED = (
    "{shot} has already rendered, so its prompt is the record of what produced a take rather "
    "than an intention. Use render again if you want a different take."
)

#: What the model returned when it did not return a usable prompt. Kept beside the report
#: rather than stored: a malformed expansion that reached the manifest would be submitted by
#: the next render, which is the one outcome the check exists to prevent.
EXPAND_PROMPT_MALFORMED = (
    "The model's answer is not a well-formed H3 prompt, so it was not saved. What it returned "
    "is below, with what is wrong with it."
)


class ShotExpansionResult(BaseModel):
    """What one expansion did, including when it did nothing."""

    project: Project
    applied: bool
    #: Empty when applied. Each entry is one thing wrong, in the checker's own words.
    problems: list[str] = Field(default_factory=list)
    #: What the model returned, always — the Director can read a refused prompt and judge it,
    #: which is the same argument `MessageNotice.raw` makes for refused Director output.
    prompt: str = ""
    note: str = ""
    #: How many model calls the answer cost, out of `EXPANSION_ATTEMPTS`. Diagnostic signal
    #: about the model — a shot that took three tries is worth the Director knowing.
    attempts: int = 1


#: Why a whole-plan sweep is refused before any model call, on `EXPANSION_WITHOUT_SHOTS`' argument.
EXPAND_PROMPTS_WITHOUT_SHOTS = (
    "This project has no shots to expand into H3 prompts. Expansion writes onto shots that already "
    "exist and never creates one, so add shots to the timeline first."
)

#: What one sweep did, per shot. Every wording here either *is* one the single-shot route and the
#: whole-plan pass-one expansion already use, or is new because only a sweep can produce it.
#:
#: The lock and provenance sentences are deliberately `EXPANSION_LOCKED_NOTICE` and
#: `EXPANSION_RENDERED_NOTICE` themselves rather than rewordings: the frozen matrix asks for a
#: refusal "in the same words", and a second spelling of one rule is how the two start describing
#: different rules.
EXPAND_PROMPTS_WRITTEN_NOTICE = (
    "H3 prompts written for {count} shot(s): {shots}. Each was checked against the format's own "
    "rules before it was stored. Nothing was rendered and no GPU time was spent."
)
#: The prompt gate, plural. `EXPAND_PROMPT_WITHOUT_INTENT` is the single-shot wording and says the
#: same thing about one shot; this is the sweep's, because listing twenty shots through a
#: `{shot}`-shaped sentence twenty times is not a report anyone reads.
EXPAND_PROMPTS_WITHOUT_INTENT_NOTICE = (
    "Not expanded because they have no intent to expand from: {shots}. Pass one writes the short "
    "intent and this pass turns it into the H3 format, so write or generate those first."
)
#: A malformed answer, per shot. `raw` carries what the model returned — bounded by
#: `NOTICE_RAW_LIMIT` and dropped from the Director's context by `DIRECTOR_CONTEXT_EXCLUDE` — so the
#: Director can read and judge it without it ever reaching the next call, which is the argument
#: `EXPANSION_REJECTED_NOTICE` already makes for refused Director output.
EXPAND_PROMPTS_MALFORMED_NOTICE = (
    "NOT saved for {shot}: the answer is not a well-formed H3 prompt after {attempts} attempt(s). "
    "{problems} The last attempt's text is kept beside this notice for inspection and is left out "
    "of the context of the next Director call. The shot is exactly as it was."
)
EXPAND_PROMPTS_MALFORMED_EMPTY_NOTICE = (
    "NOT saved for {shot}: the answer is not a well-formed H3 prompt after {attempts} attempt(s). "
    "{problems} There is no returned text. The shot is exactly as it was."
)
#: One shot's model call failing while the rest of the sweep carried on. Its own sentence because
#: the remedy is not the Director's: nothing about the shot is wrong.
EXPAND_PROMPTS_FAILED_NOTICE = (
    "Not expanded because the model call for them failed: {shots}. The rest of the sweep carried "
    "on and their shots are unchanged, so expanding them again is all this needs. The host said: "
    "{detail}"
)
#: The reply's own sentence. The specialist returns a prompt and never prose, so unlike pass one
#: and unlike the assistant there is no model message to carry — and a reply that is a bare
#: separator followed by notices is not a reply.
EXPAND_PROMPTS_MESSAGE = (
    "The H3 expansion specialist was run once for each shot, and each answer was judged on its own."
)


#: What happened to one Shot in a sweep, before anything has been written.
#:
#: `expanded` is not `applied`: it means the model answered and the answer passed the format check,
#: which is a judgement about text. Whether it may be *stored* is decided later, against the
#: project as it is at commit time, and `apply_expansions` is what turns this into `applied`,
#: `locked`, `rendered` or `missing`.
ExpansionKind = Literal[
    "applied", "expanded", "malformed", "locked", "rendered", "no_intent", "failed", "missing"
]


@dataclass(frozen=True)
class ShotExpansionOutcome:
    """One shot's result from a sweep. Frozen, because a sweep reports rather than accumulates."""

    shot_id: str
    kind: ExpansionKind
    #: What the model returned, applied or refused. Empty for every kind that never called it.
    text: str = ""
    #: The checker's own sentences. Populated for `malformed`, and empty otherwise — a well-formed
    #: prompt can still carry advisory problems, and those are not what this field is for.
    problems: tuple[str, ...] = ()
    #: The host's own words, for `failed`.
    detail: str = ""
    #: How many model calls this outcome cost. 1 for every kind that answered first time, and for
    #: every kind that never called the model at all. Reported rather than hidden, because a shot
    #: that took three tries is diagnostic signal about the model, not noise.
    attempts: int = 1


#: How many model calls one shot's expansion may cost: the first attempt plus three automatic
#: retries, the Director's own ruling after a live plan-wide run ("some failed and took a couple
#: tries due to formatting, 3 auto retries per would be fine"). Only two failures are worth a
#: retry — a checker-rejected answer, which retries as a corrective follow-up turn carrying the
#: failed text and the checker's sentences, and a reasoning-budget exhaustion, which is sampling
#: luck (roughly 1 call in 6 on this machine's model) and independent across calls. Every other
#: `DirectorError` — and `DirectorUnavailable` above all — is a fact that will be identical on the
#: next attempt, so retrying it spends the Director's seconds to learn nothing.
EXPANSION_ATTEMPTS = 4

#: Appended to a shot's name in a sweep notice when its answer cost more than one model call.
EXPANSION_TRIES_SUFFIX = " (took {attempts} tries)"


async def attempt_expansion(
    project: Project, shot: Shot, *, director: DirectorClient
) -> ShotExpansionOutcome:
    """One shot's expansion, with up to `EXPANSION_ATTEMPTS` model calls behind one outcome.

    This is the level both call paths reach — `expand_shots` for the sweep and the ProducerBot
    tool, `expand_shot_prompt` for the inspector's single-shot route — so the retry loop exists
    exactly once. It stops at the first well-formed answer; a malformed one retries with the
    checker's problems fed back as a corrective turn (`DirectorClient.expand_shot` documents the
    shape), and a budget exhaustion retries clean, with the temperature's natural variation as
    the fallback benefit either way. When every attempt fails, the *last* attempt's text and
    problems are the report, and nothing is ever stored here — the caller decides that, and only
    for `expanded`.

    `DirectorUnavailable` propagates untouched: it is a configuration fact, identical on every
    attempt, and both callers already map it to their own 503.
    """
    mode = resolve_shot_mode(shot)
    # A song-audio reference shot's expansion is deterministic prose, no model call: the
    # H3 document format itself was measured (2026-08-19, eight renders) to make the
    # sampler synthesize its own score over the referenced track — every document
    # variant ≤0.43 envelope correlation against the master window, every plain-prose
    # reference-map prompt ≥0.77, the sings-clause form 0.94 with visible lipsync. See
    # `song_audio_prose` for the full table. The keyframe modes keep the document path:
    # they carry no evidence either way and their graphs differ.
    if shot.use_song_audio and mode == "references":
        return ShotExpansionOutcome(
            shot.id, "expanded", text=song_audio_prose(project, shot)
        )
    expect_instruction = mode in H3_KEYFRAME_MODES
    payload = shot_expansion_input(project, shot)
    system = h3_system_prompt(
        expect_instruction=expect_instruction,
        # The keyframe-inside-references shape and nothing else: a references shot actually
        # citing a picture in a keyframe role. The dedicated keyframe modes take the
        # instruction line instead, and a references shot without the shape gets the
        # byte-identical prompt it always got — the rule rides only where the roles do.
        keyframe_references=(
            mode == "references"
            and any(citation.role in ("first", "last") for citation in shot.citations)
        ),
        # Read off the payload the model is actually handed rather than off the project, so
        # the rule and the data cannot disagree: `shot_expansion_input` emits `anchor` only
        # for a citation whose Asset this project holds and whose anchor is non-blank, and
        # the rule appears exactly when at least one of those keys does. A shot whose
        # references carry no anchor gets the byte-identical system prompt it always got.
        appearance_anchors=any(
            "anchor" in reference
            for reference in payload["shot"].get("references", [])
        ),
    )
    rejected = ""
    rejected_problems: tuple[str, ...] = ()
    last = ShotExpansionOutcome(shot.id, "failed", detail="no attempt was made")
    for attempt in range(1, EXPANSION_ATTEMPTS + 1):
        try:
            text = await director.expand_shot(
                shot_input=payload,
                system_prompt=system,
                rejected=rejected,
                rejected_problems=rejected_problems,
            )
        except DirectorBudgetExhausted as error:
            last = ShotExpansionOutcome(
                shot.id, "failed", detail=str(error), attempts=attempt
            )
            continue
        except DirectorError as error:
            return ShotExpansionOutcome(
                shot.id, "failed", detail=str(error), attempts=attempt
            )
        checked = h3_check(
            text,
            duration=shot.duration,
            expect_instruction=expect_instruction,
            # A song-audio shot may carry no <d> block, and this is enforced here rather
            # than in the rules because the rules failed: the model invented well-formed
            # lyrics that no lyric-sheet comparison could catch (2026-08-19). Flagging it
            # makes the retry loop feed the removal back as a corrective turn.
            forbid_dialogue=shot.use_song_audio,
            # The tags this shot was *handed* in `shot_expansion_input`, counted. A specialist
            # citing <Picture 3> on a two-picture shot has invented a slot, and the retry loop
            # is exactly where that gets fixed — for free, before a GPU pass, in the model's
            # own next turn. `None` skips it; see `reference_slot_counts`.
            reference_slots=reference_slot_counts(project, shot),
        )
        if checked.well_formed:
            return ShotExpansionOutcome(shot.id, "expanded", text=text, attempts=attempt)
        rejected = text
        rejected_problems = tuple(problem.message for problem in checked.problems)
        last = ShotExpansionOutcome(
            shot.id, "malformed", text=text, problems=rejected_problems, attempts=attempt
        )
    return last


async def expand_shots(
    project: Project, shots: list[Shot], *, director: DirectorClient
) -> list[ShotExpansionOutcome]:
    """Expand each Shot in turn. **N model calls, not one.** Writes nothing, anywhere.

    This is pass two applied to many shots, and the sequence is the feature rather than an
    implementation detail: one H3 prompt is a long document, thirty of them will not fit one
    context, and quality degrades well before the limit. `director/expand` is pass one and keeps
    its single-call shape because cross-shot variance is a property of the plan; this is the
    opposite shape for the opposite reason, run once per shot with `shot_expansion_input`'s
    per-shot payload. Each shot's call is `attempt_expansion`, so "one call per shot" is now
    "up to `EXPANSION_ATTEMPTS` calls per shot", stopping at the first well-formed answer.

    **Each shot is judged on its own and a refusal on one does not stop the rest.** That is the
    frozen matrix's own sentence, and it is why `DirectorError` is caught per shot and recorded as
    that shot's outcome rather than raised. The cost is honest and worth naming: if the model host
    dies part-way through, the remaining shots are still attempted and each still waits for its own
    failure. Stopping instead would be faster and would break the guarantee, and the guarantee is
    the one the Director wrote down.

    `DirectorUnavailable` is the exception, and deliberately propagates. It means the language
    model is not *configured* — a fact about this installation, identical for every shot — so
    retrying it N times would produce N identical sentences and no information.

    **Nothing here writes.** Every outcome is returned and the caller commits them in one pass, so
    a failure part-way through this loop leaves both the manifest and the in-memory project
    untouched rather than half-written. `assistant_fill`'s staging makes the same guarantee for the
    same reason.

    `project` is the snapshot every payload is built from, so every call sees one consistent plan —
    including the neighbours' intents, which would otherwise shift under the sweep as it ran.
    """
    outcomes: list[ShotExpansionOutcome] = []
    for shot in shots:
        # Write-refusal before prompt-gate, which is the order phase one pinned with its own test:
        # a locked shot with an empty intent must hear that it is locked, because telling it to
        # write an intent first sends the Director to do work that would then be refused anyway.
        if reason := expansion_write_refusal(shot):
            outcomes.append(ShotExpansionOutcome(shot.id, reason))
            continue
        if prompt_is_missing(shot):
            outcomes.append(ShotExpansionOutcome(shot.id, "no_intent"))
            continue
        outcomes.append(await attempt_expansion(project, shot, director=director))
    return outcomes


def apply_expansions(
    project: Project, outcomes: list[ShotExpansionOutcome]
) -> list[ShotExpansionOutcome]:
    """Commit the well-formed expansions onto `project`, in memory, in one pass.

    Three rules, each of which is a phase-one decision this must not undo:

    * **A malformed expansion is never stored.** It arrives here as `malformed` and is passed
      through untouched, so there is no branch that could write one. A broken prompt in the
      manifest is one the *next render* submits, and the failure would surface as a bad take
      rather than as a message.
    * **`Shot.prompt` is never overwritten.** Only `h3_prompt` is assigned. The intent is what
      re-expansion works from, and the first expansion will not be the good one.
    * **Every refusal is re-checked here**, against the project as it is now rather than as it was
      when the payload was built. A sweep is many model calls long, so a shot can be locked,
      rendered or deleted while it runs, and an answer written against a plan that no longer
      describes it must not land. `expand_shot_prompt` re-reads after its single await for exactly
      this reason; a sweep has more windows, not fewer.
    """
    held = {shot.id: shot for shot in project.shots}
    committed: list[ShotExpansionOutcome] = []
    for outcome in outcomes:
        if outcome.kind != "expanded":
            committed.append(outcome)
            continue
        shot = held.get(outcome.shot_id)
        if shot is None:
            committed.append(replace(outcome, kind="missing"))
            continue
        if reason := expansion_write_refusal(shot):
            committed.append(replace(outcome, kind=reason))
            continue
        # A song-audio shot's audio fields are normalized to the guide's own reuse
        # declaration at write time — stored and submitted stay one text. Measured
        # 2026-08-19: freely-written fields 0.36/0.27, untagged deferral prose
        # 0.36-0.73 (unreliable); see `h3_prompt.normalize_audio_fields`.
        shot.h3_prompt = (
            normalize_audio_fields(outcome.text, audio_tag=song_audio_tag(project, shot))
            if shot.use_song_audio
            else outcome.text
        )
        # The map this expansion was written against, recorded beside it. Every writer of
        # `h3_prompt` writes this too — that is what makes `stale_reference_map` able to answer
        # for a document expansion, whose own text never spells the map out. Taken from the
        # project the write lands on, which is the re-read one, so a citation changed while the
        # model was thinking is recorded as the map this prompt now disagrees with rather than
        # as the one it was handed.
        shot.h3_prompt_map = reference_map_sentence(reference_map_tag_lines(project, shot))
        committed.append(replace(outcome, kind="applied"))
    return committed


def expansion_sweep_notices(
    outcomes: list[ShotExpansionOutcome], labels: dict[str, str]
) -> list[MessageNotice]:
    """One sweep's report, per shot, in the order every other route on this module reports.

    What changed goes first — it is the thing the Director pressed the button for, and dressing a
    confirmation as caution is how caution stops being read — then the deliberate refusals, then
    the flags. Every shot that was swept appears in exactly one of them.

    **No expansion text is in any `text` here.** `assistant_reply` concatenates these sentences
    into `TreatmentMessage.content`, and content *is* in the Director's context dump. The refused
    text rides in `raw` instead, which `DIRECTOR_CONTEXT_EXCLUDE` drops — the same split
    `EXPANSION_REJECTED_NOTICE` uses, and the reason `SHOT_DIRECTOR_WITHHELD` exists at all.
    """
    grouped: dict[str, list[ShotExpansionOutcome]] = {}
    for outcome in outcomes:
        grouped.setdefault(outcome.kind, []).append(outcome)

    def named(group: list[ShotExpansionOutcome]) -> str:
        # A shot that cost more than one model call says so beside its name. That is the
        # Director's diagnostic signal about the model — a plan where every shot took three
        # tries is a fact about the model or the prompt, and hiding it would erase the only
        # place it shows.
        return ", ".join(
            labels.get(outcome.shot_id, _short(outcome.shot_id))
            + (
                EXPANSION_TRIES_SUFFIX.format(attempts=outcome.attempts)
                if outcome.attempts > 1
                else ""
            )
            for outcome in group
        )

    notices: list[MessageNotice] = []
    if applied := grouped.get("applied"):
        notices.append(
            MessageNotice(
                kind="change",
                text=EXPAND_PROMPTS_WRITTEN_NOTICE.format(
                    count=len(applied), shots=named(applied)
                ),
            )
        )
    for kind, wording in (
        ("locked", EXPANSION_LOCKED_NOTICE),
        ("rendered", EXPANSION_RENDERED_NOTICE),
        ("missing", ASSISTANT_MISSING_TARGET_NOTICE),
        ("no_intent", EXPAND_PROMPTS_WITHOUT_INTENT_NOTICE),
    ):
        if reported := grouped.get(kind):
            notices.append(
                MessageNotice(kind="refusal", text=wording.format(shots=named(reported)))
            )
    # Per shot rather than grouped, because each carries its own problems and its own refused text.
    for outcome in outcomes:
        if outcome.kind != "malformed":
            continue
        notices.append(
            rejection_notice(
                EXPAND_PROMPTS_MALFORMED_NOTICE,
                EXPAND_PROMPTS_MALFORMED_EMPTY_NOTICE,
                raw=outcome.text,
                shot=labels.get(outcome.shot_id, _short(outcome.shot_id)),
                problems=" ".join(outcome.problems),
                attempts=outcome.attempts,
            )
        )
    # Grouped by the host's own sentence: one dead host produces one message repeated, and a wall
    # of identical notices is how a report stops being read.
    failures: dict[str, list[ShotExpansionOutcome]] = {}
    for outcome in outcomes:
        if outcome.kind == "failed":
            failures.setdefault(outcome.detail, []).append(outcome)
    for detail, reported in failures.items():
        notices.append(
            MessageNotice(
                kind="flag",
                text=EXPAND_PROMPTS_FAILED_NOTICE.format(
                    shots=named(reported), detail=_short(detail, limit=200)
                ),
            )
        )
    return notices


class AssistantRequest(BaseModel):
    """One Assistant ProducerBot turn: what the Director asked, and which shots they asked it about.

    `shot_ids` is required and non-empty, and it is this feature's answer to "opt-in per turn".
    `director_chat`'s consent is a boolean because chat's *purpose* is conversation and writing is
    the side effect; the closest relative here is `expand`, which writes to shots and carries no
    flag at all, because a control whose only purpose is to write is its own opt-in. A boolean on
    this route would either be hardcoded true by the client — decorative, and the exact criticism
    `apply_shots: false` already earns — or would make the primary journey a tick and a click.

    What is kept is the property the boolean encodes, in a stronger form: **the turn's consent is
    the selection, and the model cannot widen it.** A tool call naming any other shot is refused,
    including a real, unlocked, perfectly writable one elsewhere in the plan. A boolean says
    "you may write"; this says "you may write *here*", which is the guarantee the frozen block is
    actually about — a tool must not widen what it can act on.

    No `apply_documents` sibling and no way to reach one: this route never touches the Treatment,
    the Style bible or the Song, so there is nothing to consent to.
    """

    message: str = Field(min_length=1)
    shot_ids: list[str] = Field(min_length=1)


class SelectTakeRequest(BaseModel):
    #: A file one of this Shot's own h3 jobs produced (repo-relative under ComfyUI's
    #: output root), or —
    output: str = ""
    #: — a video asset to attach as this Shot's clip. Exactly one of the two.
    asset_id: str = ""


SELECT_TAKE_LOCKED = (
    "{shot} is locked. A lock is a deliberate hands-off on this shot, and swapping the "
    "clip it shows is exactly the kind of change it refuses. Unlock the shot first."
)
SELECT_TAKE_UNKNOWN = (
    "That file is not one of {shot}'s own takes. A shot's clip can be switched to a take "
    "its render history produced, or to a video asset via asset_id."
)
SELECT_TAKE_NOT_VIDEO = (
    "{name} is not a video asset, so it cannot be a shot's clip. Upload the video in the "
    "Assets panel first."
)
SELECT_TAKE_EMPTY = "Send output (one of the shot's takes) or asset_id (a video asset)."

DELETE_PROJECT_CONFIRM = (
    "This deletes {name!r} — its manifest, its {shots} shot(s) and its media directory — "
    "permanently. Takes already rendered into ComfyUI's output tree stay on disk. Send "
    "confirm_delete=true to proceed."
)
DELETE_ASSET_CITED = (
    "{name} is cited by {shots}, and deleting it would leave those citations dangling — "
    "the render would refuse them one at a time. Remove it from those shots first."
)
#: The act `DELETE_ASSET_CITED` implies, and the Director asked for by hitting that refusal:
#: "a nice Replace With/Cancel option set would be nice so then i could select another image
#: while i am here in assets and auto replace the one i am trying to remove across the affected
#: shots" (2026-08-20). The refusal above is unchanged and stays — this is the way through it,
#: not a way around it, and it deliberately does **not** delete: `replace_asset_citations`
#: moves citations and `delete_asset` deletes, so a replacement that had to skip a locked shot
#: still meets the same refusal for the same reason instead of half-deleting an asset.
REPLACE_ASSET_WITH_ITSELF = (
    "{name} cannot replace itself. Pick a different asset from the library, or cancel — "
    "replacing an asset with itself would report every shot that cites it as changed and "
    "change nothing."
)
REPLACE_ASSET_UNKNOWN = "Replacement asset not found"
#: The one genuine correctness block, and the only refusal here that is not a lock.
#:
#: A new sentence rather than a reuse, because no existing one says this: the two in-flight
#: wordings this module already carries name a different act — `RENDER_AGAIN_IN_FLIGHT_REFUSAL`
#: says "nothing was re-opened" and `MARK_READY_IN_FLIGHT_REFUSAL` says a status is not yours to
#: set — and a refusal about a *citation* that claimed either would be describing something that
#: did not happen. It keeps their staleness escape verbatim, because job status only moves when
#: the queue is polled (`shot_render_in_flight`).
REPLACE_ASSET_IN_FLIGHT = (
    "A render for {shot} has not finished, and it was submitted against {replaced}. Rewriting "
    "the citation now would leave that job's record describing a render that never happened. "
    "Wait for it, or refresh the render queue if it has already finished and this project has "
    "not been told yet."
)
#: An asset no shot cites needs no replacement, and saying so is more useful than an empty
#: report: the delete it was reached from will now simply go through. `snap_timeline_cuts`'
#: honest-empty rule — refuse when there was nothing to examine, rather than reporting a plan
#: over zero shots.
REPLACE_ASSET_UNCITED = (
    "No shot cites {name}, so there is nothing to replace. Delete it directly — the citation "
    "refusal will not fire."
)
#: Reported, never refused. `Asset.kind` is the library's own taxonomy, and the render path
#: buckets `character`, `setting`, `prop`, `style` and `image` into one anonymous picture series
#: (`models.citation_slot_kind`) — so citing a setting where a character was is a creative
#: change, not a structural error, and refusing it would block the ordinary act of replacing a
#: rough concept `image` with the finished `character` drawn from it. The structural half of a
#: kind change *is* refused, per shot and by name: a replacement that moves a citation between
#: the picture, video and audio series is checked against `H3_REFERENCE_LIMITS` and skips any
#: shot it would push over (`asset_replacement.REPLACE_OVER_SLOT_LIMIT`).
REPLACE_ASSET_KIND_CHANGE = (
    "{replacement} is a {replacement_kind} asset and {replaced} is a {replaced_kind}. The "
    "replacement is allowed, but every shot below will be conditioned on a different sort of "
    "reference than it was — read the list before confirming."
)
#: A shot whose take was rendered against the asset being replaced. **A note, not a refusal**, on
#: the Director's ruling of 2026-08-20: "So even with takes we do want the asset for the shot
#: replaceable, that way a re-render would use the updated asset without losing previous takes.
#: This helps facilitate experimentation."
#:
#: Deliberately not `EXPANSION_RENDERED_NOTICE`, and that is the point rather than an oversight:
#: that sentence says a shot was *left unchanged*, and these shots are changed. Reusing it here
#: would be the drift the verbatim rule exists to prevent, in the other direction. What it does
#: keep is that wording's argument — the take and what sits beside it now disagree — because the
#: consequence is real and unrecoverable: nothing in this application records which assets
#: produced a take, so the old take's true references are gone the moment this is applied.
REPLACE_ASSET_RENDERED_NOTE = (
    "{count} shot(s) already hold a take that was rendered against {replaced}: {shots}. The "
    "takes are untouched — the files, the takes strip and the job records are all exactly as "
    "they are — but nothing records which assets produced them, so after this those takes and "
    "the references beside them no longer agree. Re-rendering uses the new asset."
)
#: The same, for shots carrying an editorial approval. Its own line so the count is visible: an
#: approval is a stronger statement than a render, and a Director changing the references under
#: eight approved shots should see the number before confirming.
#:
#: The approval itself is untouched — `approved_output`, `approved_start` and `approved_duration`
#: are not written by this route on any path. AD-13's staleness comparison is between the stored
#: window and the live one, and citations are not the window, so assembly reads exactly what it
#: read before.
REPLACE_ASSET_APPROVED_NOTE = (
    "{count} shot(s) carry an approved take rendered against {replaced}: {shots}. The approval "
    "and its window snapshot are untouched and assembly is unaffected — citations are not the "
    "window — but the approved file was produced from the old reference."
)
#: The counts a Director sanity-checks first, in their own phrasing: "a report would be nice and
#: could include 'already in N shots'".
REPLACE_ASSET_REPORT = (
    "{replacement} would replace {replaced} in {swapped} shot(s); {merged} shot(s) already "
    "cite {replacement}, and there the {replaced} reference is simply removed. {skipped} "
    "shot(s) skipped. Role and order are carried across, and nothing is rendered, armed or "
    "queued."
)
#: Said after an applied call, because "can I delete it now" is the next question by
#: construction — this route is only ever reached from the delete refusal.
REPLACE_ASSET_FREED = "No shot cites {replaced} any more, so it can now be deleted."
REPLACE_ASSET_STILL_CITED = (
    "{count} shot(s) still cite {replaced}, so deleting it will still be refused until those "
    "are resolved."
)
#: How long an appearance anchor may be. It is an *anchor*, not a description: the Calliope
#: teardown's own example is eight words ("a teenage girl with a chestnut ponytail and yellow
#: rain jacket"), and the string is appended to every tag line of every prompt citing the asset,
#: where a paragraph would crowd out the shot's own direction. The bound is generous enough for
#: a sentence or two and small enough that it cannot become a second style bible; it is measured
#: after trimming, exactly as the song-context bounds are.
CONSISTENCY_PROMPT_LIMIT = 400

#: The refusal, in `SONG_CONTEXT_TOO_LONG`'s shape and for its reason: it says what was not
#: saved as well as what was wrong, because a 422 that only names the rule leaves the Director
#: guessing whether their text is now half-applied.
CONSISTENCY_PROMPT_TOO_LONG = (
    "The appearance anchor for {name} is {length} characters, past the {limit} this "
    "application stores for it. Nothing was saved. An anchor is a short phrase naming what "
    "this asset looks like, carried into every prompt that cites it — shorten it and try again."
)

CANCEL_JOB_SETTLED = "This job is already {status}; there is nothing running to cancel."
CANCEL_JOB_NOTE = (
    "Cancelled by the Director before it finished. Nothing was produced. "
    f"{SHOT_AFTER_FAILED_RENDER}"
)

# --------------------------------------------------------------------------------------------
# Cancel every open render (the Director's report, 2026-08-23). The per-job `×` works and
# twenty-six of them is not a control, it is a chore — so a Generate All the Director changed
# their mind about was cancelled in ComfyUI's own UI instead, which is the path that produced
# the takeless-`error` shots `shot_status_after_failed_render` now settles.
#
# **Scope: every open ComfyUI render in this project, not one batch.** Four reasons, and the
# first is the whole point of the control:
#
# * the gesture it replaces is ComfyUI's own "clear queue", which stops *everything*. A control
#   that stops less than the trip to ComfyUI does will not replace the trip to ComfyUI;
# * `reconcilable_jobs` is already **the** definition of "this project has renders in flight" —
#   the poll's `active` flag is computed from it and `api.js`'s `hasActiveRenderJobs` mirrors it
#   — so cancelling exactly that set is what makes the poll stand down afterwards. A batch-scoped
#   cancel would leave `active` true and the panel still polling, which reads as "it did nothing";
# * `batch_id` is **empty** for every render submitted outside a batch: a lone Render again, an
#   LTX enhance, a music or Flux job. Those rows sit in the same queue panel wearing the same
#   `queued`, and a batch-only cancel could not touch one — the chore returns for exactly the
#   rows it was added to remove;
# * the confirmation names the count, which is what makes the wider scope safe to offer: the
#   Director reads the number of renders that will stop before any of them does.
#
# Local work is **not** in scope and needs no rule to exclude it: `reconcilable_jobs` skips a job
# with an empty `prompt_id`, which is this application's local-work marker (an assembly export).
# Nothing here could stop a running ffmpeg, so settling its record would be a claim this route
# cannot back — and an export cannot be open beside a render anyway, since `assemble_project`
# refuses while any render is.
CANCEL_ALL_NONE_OPEN = (
    "No renders are open for this project, so there is nothing to cancel. A settled job keeps "
    "its record in the queue; only a queued or running one can be stopped."
)
#: The server-enforced half, in `GENERATE_BATCH_CONFIRM_REFUSAL`'s shape and for its reason: a
#: client that never showed the warning must not be able to stop twenty-six renders by omission.
#: Names the count, because that is the one fact that makes the gesture judgeable.
CANCEL_ALL_CONFIRM_REFUSAL = (
    "This would cancel {count} open render(s) for this project — every queued and running one, "
    "not just the newest batch. Cancelling is not undoable: a render stopped part-way produces "
    "nothing and the GPU time it has already spent is gone. Send confirm_cancel=true to proceed."
)
#: Deliberately **no** cancellation note of its own. Every job this route settles is settled by
#: `cancel_job` itself and therefore carries `CANCEL_JOB_NOTE`, which is already true of it — the
#: Director cancelled it before it finished — and one wording for one act is the whole reason this
#: route delegates rather than writing its own settle. A second sentence would be a second
#: spelling of the same rule, which is this repository's recurring defect.

#: The job kinds whose `target_id` is a Shot id, mirroring `api.js`'s JOB_KINDS_TARGETING_A_SHOT.
#: `music` names the Song, `flux`/`multiview`/`edit` name an Asset and `post` names nothing, so
#: none of them can be labelled as a shot.
JOB_KINDS_TARGETING_A_SHOT: frozenset[str] = frozenset({"h3", "ltx"})


def job_label(project: Project, job: RenderJob) -> str:
    """One job under the name the Director already reads for it, for a report that lists jobs.

    `shot_label` where the job names a Shot this project still holds — which is what the queue
    panel draws through `jobTarget`, so a cancellation report and the row it is about say the same
    thing. Everything else falls back to the kind and the record id rather than to a bare
    `target_id`: an asset or song id is a string that appears nowhere in the interface, and a job
    whose shot a populate replaced has a `target_id` naming a shot that no longer exists (see
    `api.js`'s JOB_TARGET_DETACHED for why those records are kept).
    """
    if job.kind in JOB_KINDS_TARGETING_A_SHOT:
        shot = next((item for item in project.shots if item.id == job.target_id), None)
        if shot is not None:
            return shot_label(project, shot)
    return f"{job.kind} job {job.id}"


class AlignLyricsRequest(BaseModel):
    #: Re-run Whisper even when words are already stored — for a replaced or re-mastered file.
    retranscribe: bool = False
    #: Overwrite existing section boxes with the measured proposal. Off by default because
    #: dragged boxes are the Director's marks.
    replace_sections: bool = False


#: Every way the align route refuses, each naming the remedy.
ALIGN_LYRICS_WITHOUT_SONG = (
    "A completed project song is required before its lyrics can be aligned to time."
)
ALIGN_LYRICS_WITHOUT_TAGS = (
    "The lyric sheet has no [Tag] blocks to align. Mark the sheet's structure with tags "
    "like [Verse], [Chorus], [Bridge] in the song context editor first."
)
ALIGN_LYRICS_SECTIONS_EXIST = (
    "This project already has section boxes, and they are the Director's marks. Send "
    "replace_sections=true to overwrite them with the measured alignment."
)
ALIGN_LYRICS_TRANSCRIBE_FAILED = (
    "The track could not be transcribed: {error}. Whisper (faster-whisper) must be "
    "installed and the song file readable."
)
ALIGN_LYRICS_NOTHING_PLACED = (
    "No lyric block could be placed on the track — the transcription and the sheet do not "
    "agree anywhere. The transcribed words were kept; check the sheet matches this "
    "recording's actual words."
)

#: Why a song was not measured. These are **reasons, not refusals**: nothing in this story
#: returns one as an HTTP error. An import whose analysis failed is still an import — the Song is
#: stored, the Project is otherwise untouched, and the reason is logged and reported by the
#: envelope endpoint rather than turned into a 500 the Director cannot act on. Named separately
#: rather than collapsed into one string because the remedies are different: install ffmpeg, fix
#: the file, free some disk.
SONG_ANALYSIS_WITHOUT_SONG = "This project has no song audio, so there is nothing to analyze."
SONG_ANALYSIS_MEDIA_MISSING = (
    "The song's audio file was not found on disk, so the song was not analyzed."
)
SONG_ANALYSIS_FFMPEG_MISSING = (
    "ffmpeg was not found on PATH, so the song was not analyzed. Install ffmpeg and import "
    "the song again."
)
SONG_ANALYSIS_DECODE_FAILED = (
    "The song could not be decoded, so it was not analyzed: {reason}. The file must be audio "
    "ffmpeg can read."
)
SONG_ANALYSIS_WRITE_FAILED = (
    "The song was measured but the analysis could not be written beside the project: {reason}."
)

#: Why the envelope endpoint has nothing to serve. **Absence is not an error state** — every one
#: of these rides a 200 with `present: false`, because "this song has not been measured yet" is an
#: ordinary state of an ordinary project and a consumer that met it as a 404 would draw an error
#: where it should draw nothing.
#:
#: Each is derived at read time. There is no stored flag anywhere that says an envelope is stale,
#: and there must never be: a flag is a second truth, and the routes that replace a song do not
#: know this feature exists.
SONG_ENVELOPE_WITHOUT_SONG = "This project has no song, so there is no analysis of one."
SONG_ENVELOPE_NOT_TAKEN = "No song analysis yet."
SONG_ENVELOPE_SONG_CHANGED = (
    "The song changed after it was analyzed, so the stored analysis describes a track this "
    "project no longer has."
)
SONG_ENVELOPE_FILE_UNREADABLE = (
    "The song analysis file is missing or unreadable, so there is nothing to serve."
)
SONG_ENVELOPE_UNDECODABLE = (
    "The song file is not audio ffmpeg can read, so it has never been analyzed. Re-import the "
    "track from a file ffmpeg can decode."
)
SONG_ENVELOPE_AUDIO_PENDING = (
    "This song has no audio yet — its render has not landed. The analysis is taken "
    "automatically when it does."
)
SONG_ENVELOPE_RECORD_DISAGREES = (
    "The stored analysis record and the analysis file disagree about how the song was "
    "measured, so the file is not served. It will be replaced the next time the song is "
    "analyzed."
)

#: The part of a measurement the *timeline* is served, beside the seconds a drag may land on.
#:
#: **Measured on a real 202 s master:** the sidecar is 469,472 bytes, of which `bands`, `rms`,
#: `peak` and `flux` are 460,264 — 98.0%, and not one byte of it is read by the browser. Those
#: four are the per-frame time series, one value per analysis frame per band, and they are what
#: makes an envelope large. The four below are fixed-size or one-per-event: `beats` and `onsets`
#: are the marks the waveform draws, and `band_average` (8 numbers) and `band_edges` (9) are what
#: AD-26's band selector needs to name a band and show what it holds. Together they are 8,846
#: bytes on that same master.
#:
#: **This is a projection, not a new shape.** The sidecar on disk is unchanged, `audio.py` still
#: writes every key, `ENVELOPE_REQUIRED_KEYS` still validates the whole set, and
#: `GET /song/envelope` still serves all of it to anyone who wants it. What is trimmed is the
#: copy that rides the timeline's own read, and the rule for trimming it is the honest one: serve
#: what is consumed, plus what the next epic's selector will need, and leave on disk the arrays
#: nothing reads.
#:
#: **Still not `bpm`, and the reason has changed.** This said the estimate "appears nowhere in the
#: interface" and that it would join this tuple the moment something drew it. Something draws it
#: now — the Song page's analysis strip, 2026-08-26 — and it still does not belong here, because
#: it never needed to travel this way. `SongAnalysis.bpm` is already on the wire in every project
#: read, which is where `songEnvelopeIdentity` has always got `song_fingerprint` from, so serving
#: it again would be a second copy rather than a first.
#:
#: The strip reads the number from the stored record and its *right to print it* from `analysed`
#: on this route — the record supplies the digits, the served flag supplies the currency, and the
#: flag is recomputed from a fingerprint at read time rather than stored (AD-21). Two sources for
#: two different questions is not the "one question, two answers" shape this project rejects; it
#: would become that only if the currency were also derivable from the record, and it is not.
#:
#: The standing rule the old wording got right: a field served on the strength of being cheap is
#: how this payload grows back. A consumer is necessary and not sufficient.
SERVED_ENVELOPE_KEYS = ("beats", "onsets", "band_average", "band_edges")


def _served_length(value: Any) -> int | None:
    """How many numbers this value would put on the wire, or ``None`` if it would be dropped.

    `song_measurement_verdict` asks this so it can tell two states apart that look alike from the
    outside. A *short* array is a sidecar that disagrees with its own record and nothing downstream
    can see it -- the spectrum strip positions its bars off `band_average` while the compiler
    weights off `bands`, so a Director selects one band and drives another. A *malformed* array is
    already handled by `served_measurement` dropping that one key, which leaves the beats and the
    gaps intact and lets the client refuse the strip on its own.

    Mirrors `served_measurement`'s own rule deliberately -- a list of finite, non-bool numbers, all
    or nothing -- rather than restating it loosely, because two spellings of "would this reach the
    wire" is the shape of defect this application keeps paying for.
    """
    if not isinstance(value, list):
        return None
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        if not math.isfinite(item):
            return None
    return len(value)


def served_measurement(envelope: dict[str, Any] | None) -> dict[str, Any] | None:
    """`SERVED_ENVELOPE_KEYS` off a read envelope, or `None` when there is no measurement.

    `None` rather than an empty dict, because the browser branches on it: a dict with no beats in
    it is a measurement that found nothing, and an absent measurement is a song nobody has
    analysed. Those are different sentences on the "Snap to" rows and the route already tells
    them apart with `analysed` — this key answers the same question the same way, from the same
    expression, so the two cannot disagree.

    Missing keys are carried as missing rather than defaulted, which cannot happen for an envelope
    `store.read_song_envelope` has validated and is written this way so that a hand-edited sidecar
    surfaces as a drawing that is short rather than as arrays of zeros that look measured.

    **Non-numbers are dropped for the same reason, and it is not hypothetical.** The reader checks
    a key is *present*, never what is under it, and the currency verdict checks only `band_count`,
    `analysis_rate` and the length of `bands` — so `"onsets": null`, a string, or a list of lists
    all reach this function intact from a manifest a Director can edit. Serving them raised a
    `ResponseValidationError` against the declared shape, which is a 500 on a route whose own
    contract two paragraphs above the decorator says absence is a 200 and that none of these may
    become an error. Worse in the browser, where the read fails silently and takes the *gap* half
    down with it — targets that never came from the envelope at all.
    """
    if envelope is None:
        return None
    def numbers(value: Any) -> list[float] | None:
        if not isinstance(value, list):
            return None
        kept = [
            float(item)
            for item in value
            if isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item)
        ]
        return kept if len(kept) == len(value) else None
    served = {}
    for key in SERVED_ENVELOPE_KEYS:
        if key not in envelope:
            continue
        usable = numbers(envelope[key])
        if usable is not None:
            served[key] = usable
    return served


class SnapTargetsEnvelope(BaseModel):
    """`SERVED_ENVELOPE_KEYS`, declared — the drawing half of the timeline's one measurement read.

    **The keys are `SERVED_ENVELOPE_KEYS` and the two must not drift**, which
    `test_the_snap_targets_shape_is_declared_and_drops_nothing` holds by comparing them. A key
    added to that tuple without a field here would be filtered off the wire by this model, which
    is precisely the failure a `response_model` introduces if it is allowed to fall behind.

    Every field defaults to empty **and the route is served with `response_model_exclude_unset`**,
    so a key `served_measurement` did not carry is still absent here rather than becoming `[]`.
    That is not a detail: `served_measurement`'s own docstring says a missing key is carried as
    missing so that a hand-edited sidecar surfaces as a drawing that is *short* rather than as
    arrays of zeros that look measured, and a model that defaulted it would quietly reverse that.
    """

    beats: list[float] = Field(default_factory=list)
    onsets: list[float] = Field(default_factory=list)
    band_average: list[float] = Field(default_factory=list)
    band_edges: list[float] = Field(default_factory=list)


class SnapTargetsResponse(BaseModel):
    """What `GET /timeline/snap-targets` answers: what a drag may land on, and what the band draws.

    Declared rather than left a bare dict — the retrospective's pattern-divergence finding (A13):
    this was the only `/timeline/` route without a `response_model`, beside seven siblings that
    all have one, so its seven fields appeared in no `/openapi.json` and no client could discover
    them. (`GET /song/envelope` is deliberately **not** given one; its docstring argues the
    omission and the argument holds — it serves `audio.py`'s own recorded arrays, thousands of
    floats whose shape that module already owns.)

    **A declared shape on a route that already had consumers is a filter, and that is the risk it
    carries.** FastAPI serialises the handler's answer *through* this model, so a field the model
    omits disappears from the wire silently — no error, no log, and two e2e scripts plus the
    browser read this route. So the fields below are the route's return dict exactly, and
    `test_the_snap_targets_shape_is_declared_and_drops_nothing` derives that dict's keys from the
    handler's own source and fails if the two ever differ.

    `measured` and `analysed` are here rather than inferred from the two lists being empty,
    because `vocal_gaps` distinguishes *unmeasured* from *measured and voiced throughout* and this
    application never flattens the two together. `envelope` is `null` when there is no
    measurement, which is `analysed: false` said in the shape the band consumes.
    """

    gaps: list[float] = Field(default_factory=list)
    beats: list[float] = Field(default_factory=list)
    measured: bool = False
    analysed: bool = False
    #: **Why there is no measurement, and `""` where there is one** — `song_envelope_report`'s own
    #: sentence, which this route already computed and used to throw away.
    #:
    #: `analysed: false` says *that* there is nothing; this says *which* nothing, and the three the
    #: band panel acts on are three different remedies — never taken, taken from a track this song
    #: no longer is, and a sidecar that will not read (R-11 derives every one at read time and
    #: stores none). The alternative was a second client read of `GET /song/envelope`, which hashes
    #: the whole master again: two reads of one measurement is what let the band and the drag
    #: describe different states, and the merged read exists because of it.
    reason: str = ""
    start: float = 0.0
    end: float = 0.0
    envelope: SnapTargetsEnvelope | None = None


class ShotEffectsRequest(BaseModel):
    """The body of a stack write: raw JSON, deliberately, so the catalogue answers first.

    `list[dict[str, Any]]` and not `list[EffectSpec]`, and that is the whole design of this
    route's refusal. Bound as `EffectSpec` the two shapes a client actually gets wrong both
    disappear before anything can report them: pydantic *ignores* a key it does not declare, so
    `{"effect": "grain", "paramters": {...}}` would store a grain card at its defaults and say
    nothing — the "quietly does nothing" failure the whole guard exists to prevent — while
    `extra="forbid"` would answer it in pydantic's own words, about a model no Director has heard
    of, in place of a sentence written to be read.

    So the list arrives untouched and `effects.validate_stack` is the first thing that looks at
    it (AD-27). It owns the catalogue, so it is the only thing entitled to say what an effect is;
    it raises `EffectRefusal` with a sentence naming the offending effect, parameter and bound;
    and it is the same function `build_effect_stages` runs again at export, so what this route
    accepts and what the chain composes cannot come to different verdicts.

    A body that is not a list at all is pydantic's 422, which is correct: `validate_stack`'s
    `EFFECT_STACK_NOT_A_LIST_REFUSAL` covers the Python caller handing it a string, and this
    field's declared type covers the wire.

    **`None` is the default and not `[]`**, which is the whole of the difference between a body
    that clears a stack and a body that names nothing. Under `Field(default_factory=list)` the
    two were indistinguishable, so `{"efects": [...]}` — pydantic ignores the undeclared key —
    bound to `[]`, stored `[]`, and answered **200**: a misspelling destroyed a Director's grade
    and reported success, with `validate_stack` never seeing the body. The route refuses `None`
    by name (`SHOT_EFFECTS_ABSENT_REFUSAL`) and treats `[]` exactly as it always has, because
    clearing every card has to stay possible and has to stay explicit. This route's own docstring
    already argues at length that a misspelled `paramters` must not quietly do nothing one level
    down; `efects` did worse than nothing one level up.
    """

    effects: list[dict[str, Any]] | None = None


class ShotBindingsRequest(BaseModel):
    """The body of a Parameter Binding write: which card the client believes it is addressing,
    and the whole of that card's bindings.

    `bindings` is `list[dict[str, Any]]` and not a model, for `ShotEffectsRequest`'s reason and
    the same two failures: bound as a model, pydantic would *ignore* `{"paramter": ...}` and store
    a binding on nothing, or refuse it in words about a model no Director has heard of. So the
    list arrives untouched and `effects.validate_stack` — which owns `BINDING_SPEC_KEYS`, the two
    drive modes and every bound — is the first thing that looks at it (AD-27). It is the same
    function the export runs again, so this route and the chain cannot come to different verdicts
    about one binding.

    **`None` is the default and not `[]`**, which is `ShotEffectsRequest.effects`' lesson applied
    before it can be learned twice: `{"bindigns": [...]}` would otherwise bind to `[]`, clear the
    Director's binding and answer 200. `[]` is how a binding comes off, and it stays explicit.

    **`effect` is what the client saw at `index`, checked against the stored entry there.** An
    index alone is the one thing R-26 rejected — correct until the Director drags a card, and then
    silently addressing a different effect while still resolving. Naming the effect the client drew
    at that position turns that silence into a refusal: the write either lands on the card the
    Director was looking at or it lands on nothing. It is not an id and does not pretend to be one
    — two Blooms are still two Blooms — but a binding can no longer cross a *family* boundary
    unnoticed, which is the whole of what a stack reorder can do to an index.

    **A card has carried an `id` since R-33, and this field is deliberately still `effect`.** The
    id is what the *generic* doors adopt a card's bindings by; re-addressing this route on it would
    change the wire, the panel and the route for no defect anybody has reproduced, and the
    2026-08-28 ruling did not ask for it. A card id here is the obvious next slice and is not this
    one.
    """

    effect: str | None = None
    bindings: list[dict[str, Any]] | None = None


class ShotEffectsResponse(BaseModel):
    """One Shot's stack, read back. `shot_id` is carried so a reply cannot be misfiled.

    A `response_model` because every `/timeline/` sibling has one and retrospective item A13 is
    the reason: a route whose shape is a bare dict appears in no `/openapi.json` and no client can
    discover it. The write returns the whole `Project` instead — the idiom every purpose-built
    shot action already follows, and what a client needs to redraw and to keep its optimistic
    concurrency stamp — so this shape is the read's alone.
    """

    shot_id: str
    effects: list[EffectSpec] = Field(default_factory=list)


#: The "this body said nothing about this side" sentinel for `ShotTransitionsRequest`.
#:
#: An object identity rather than a string or `None`, because both of those are values a client
#: could send: `None` is how a transition is **cleared**, and any string would be a type. The one
#: shape that cannot arrive over JSON is a Python object nobody can spell.
SHOT_TRANSITION_UNSAID: Any = object()

#: The refusal for a body that names neither side. `ShotEffectsRequest`'s lesson, applied before
#: it can be learned a second time: a misspelled key must not read as the value that destroys
#: something and answer 200.
SHOT_TRANSITION_ABSENT_REFUSAL = (
    "This request named no transition for {shot}. Send `transition_out` (or `transition_in`) with "
    "a type to set one, or with `null` to clear it — a body that names neither is not a way to "
    "clear both."
)
#: A Shot the Director has put a hands-off on. `SHOT_EFFECTS_LOCKED_REFUSAL`'s wording and its
#: 422, on the ruling of 2026-08-18 that puts `locked` on the unprocessable side rather than the
#: conflict side: a lock clears by a deliberate act, never by patience.
SHOT_TRANSITION_LOCKED_REFUSAL = (
    "{shot} is locked, so its transition was not changed. Unlock the shot to change it."
)
#: The same lock, met from the other end of one blend (2026-08-30). AD-30's mirror writes the
#: *neighbour* Shot's field, and until this constant existed it did so with no lock check at all --
#: so a Director who had locked a Shot could still author a blend on it by naming the unlocked end.
#: It is not a cosmetic hole: `transition_in` mirrors **backwards** onto the predecessor's
#: `transition_out`, which is the only side the export reads at a boundary, so writing the later
#: unlocked Shot's incoming field is how you set the locked earlier Shot's outgoing blend, and it
#: is that blend and not R-45's opening -- a Shot with something in front of it never opens the
#: plan. The mirror sets both
#: sides, so `_report_transition_divergence` saw nothing to say either.
#:
#: **It refuses rather than skipping the mirror**, and the reason is the shape of the alternative.
#: Skipping would answer 200 to a `transition_in` write that changed nothing the export reads, and
#: would leave `api.transitionMirrorToast` announcing a mirror that did not fire -- a past-tense
#: sentence about something that did not happen, which is the one idiom this application has a
#: rule about. A blend is a fact about **both** Shots, so a lock on either end holds it, and the
#: refusal names the end that is locked rather than the one the request was addressed to.
SHOT_TRANSITION_MIRROR_LOCKED_REFUSAL = (
    "{shot} is locked, and a transition between {before} and {after} is written on both of them, "
    "so nothing was changed. Unlock {shot} to change this boundary."
)

class ShotTransitionsRequest(BaseModel):
    """The body of a Transition write: which type, on which side, or `null` to clear it.

    **Both fields default to a sentinel rather than to `None`**, and that is the whole of the
    difference between "clear this transition" and "say nothing about it". `None` is the value
    that *clears*, so it cannot also be the value that means absent — the shape
    `ShotEffectsRequest.effects` learned the expensive way, where a misspelled key bound to the
    clearing value and answered 200 over a destroyed grade. Here `{"transiton_out": {...}}` reaches
    the route as "neither side named", which is refused by name
    (`SHOT_TRANSITION_ABSENT_REFUSAL`), rather than as "clear both sides" reported as success.

    `type` arrives inside a `TransitionSpec` and is a free string on that model, so the catalogue
    answers first and answers in a sentence written for a Director — `EffectSpec.effect`'s design,
    for its reason (AD-27). A `Literal` here would put the catalogue in a second place.

    **This route writes the pair together and keeps the mirror in step** (AD-30). Writing
    `transition_out` on a Shot also writes `transition_in` on the Shot that follows it in song
    order, so the later Shot's own panel can draw its half of one blend; the outgoing field stays
    the authoritative one and is the only side a boundary's picture is built from. `transition_in`
    is read at exactly one place, which is not a boundary between two Shots: the opening frames of
    the Shot that lays the plan's own first frame (R-45, `app._compose_opening_transition`).
    """

    #: The sentinel: a value no client can send, because JSON has no way to spell it. `Any` rather
    #: than a union with the sentinel's type, because pydantic would otherwise coerce.
    #: `default_factory` rather than a bare default, and it is not a style choice: pydantic tries
    #: to serialise a plain default into the JSON schema and warns
    #: (`PydanticJsonSchemaWarning`) on every `openapi()` call for one it cannot. A factory is
    #: never inlined into the schema, so `/openapi.json` is clean and the field still reads as
    #: optional — which is what a client discovering this route has to see.
    transition_out: TransitionSpec | None | Any = Field(
        default_factory=lambda: SHOT_TRANSITION_UNSAID
    )
    transition_in: TransitionSpec | None | Any = Field(
        default_factory=lambda: SHOT_TRANSITION_UNSAID
    )


class TransitionCatalogueEntry(BaseModel):
    """One transition the application offers, as a client with no interface can discover it.

    `pair_only` is FX-19's requirement on the wire: a pair-only entry is **present in the list**
    and refuses one-sided use with its reason, rather than being silently absent from a list a
    Director is trying to learn.

    `xfade` is carried because it is not a secret and because it is the one field that makes the
    catalogue checkable against ffmpeg by a reader who has neither this source nor a browser —
    R-34's `hblur`-is-"Blur wipe" ruling is exactly the kind of claim that has to be readable.

    `one_sided_frames` is story 11.4's *"bounded by the Shot's own duration and by **nothing
    invisible**"*, on the wire. A one-sided transition's length is not stored on the manifest and
    is not a gesture the Director makes — there is no Overlap to make it with — so it is a
    server-side constant (`effects.ONE_SIDED_TRANSITION_FRAMES`), and a constant nobody can read
    is precisely the invisible bound that criterion refuses. It is `None` for a pair-only entry,
    which is the same fact `pair_only` states and is stated twice on purpose: a client drawing the
    row that says *"this treats shot 04's last frames, then cuts"* needs the number in the same
    breath as the permission, and would otherwise hard-code it.

    **It is a ceiling rather than a promise.** The export clamps it to the clip's own frames on the
    grid, so a Shot shorter than half a second is treated over its whole length and the export's
    own record (`TRANSITION_ONE_SIDED_RECORD`) says which number actually ran.
    """

    transition_id: str
    label: str
    xfade: str
    pair_only: bool
    one_sided_frames: int | None = None


class ShotTransitionsResponse(BaseModel):
    """One Shot's Transition pair, read back, with the catalogue beside it.

    `shot_id` is carried so a reply cannot be misfiled, exactly as `ShotEffectsResponse` carries
    it. A `response_model` for that model's reason too: a route whose shape is a bare dict appears
    in no `/openapi.json` and no client can discover it.

    **The catalogue rides on the read**, which is what makes story 11.1's own final acceptance
    criterion true — *"the route is sufficient to set and clear a Transition without any
    interface"*. A headless client that could set a transition but could not find out which twelve
    exist would have to read this source to use the route. It is a constant, computed from
    `effects.TRANSITION_CATALOGUE`, and costs no disk read on any path.
    """

    shot_id: str
    transition_out: TransitionSpec | None = None
    transition_in: TransitionSpec | None = None
    catalogue: list[TransitionCatalogueEntry] = Field(default_factory=list)


class ShotDriveBinding(BaseModel):
    """One Parameter Binding's compiled drive over one Shot's window — the Drive readout's whole
    subject, and R-27's ruling as a wire shape.

    **These are the numbers the `sendcmd` script carries**, not a curve derived from a band
    series a second time. `at` is the clip-local second each command is stamped with and `values`
    is the number each command's argument is formatted from; both come off `effects.drive_samples`,
    which is the same walk `effects.sendcmd_script` writes its lines from. A picture drawn from
    these is the argv, which is the only form of FX-22's *"the signal drawn is the same one the
    export will use"* that cannot drift.

    **The per-frame `bands` array stays on disk** (AD-20, R-27). It is about 98 % of a 469 KB
    sidecar, and shipping it so a browser could compute this itself is the second *renderer*
    R-27 refused by name: the picture and the export could then disagree while every automated
    gate passed.

    `silenced` is per tick and is the band level against the Director's own Trigger Floor —
    *silenced*, not merely low. It is here rather than inferred from `values` because the drive
    cannot answer it: a `sustain` binding holds through a dip well under its floor, and a `punch`
    envelope decays to nothing in a passage that never approached it.

    `rest` and `reach` are the two ends the picture is measured between: where a shut gate leaves
    the parameter, and where a full drive takes it, each already clamped into the parameter's own
    declared range by the compiler. `reach` may sit below `rest`, because depth is signed. They
    are the compiler's clamp and not the catalogue's bound restated — a client that scaled to the
    bound would draw a flat line for every binding whose depth is small.

    `index` is the card's position in the stored stack, which is how a client matches a drawn
    envelope to the band panel it belongs to. The list arrives in composed-chain order.
    """

    index: int
    effect: str
    parameter: str
    rest: float
    reach: float
    at: list[float] = Field(default_factory=list)
    values: list[float] = Field(default_factory=list)
    silenced: list[bool] = Field(default_factory=list)


class ShotDriveResponse(BaseModel):
    """What `GET .../shots/{id}/drive` answers: the seconds the readout spans, and one entry per
    binding that would compile a script for it.

    **An empty list is the whole of every absence**, and that is FX-22's *absent, not empty*:
    a Shot carrying no binding, a Shot whose only bound card is switched off, a song with no
    current measurement, and a stored stack the validator refuses all answer the same way, because
    the readout's only question is *is there a compiled drive to draw*. Which absence it is, and
    what to do about it, is the band panel's sentence and is already answered there — a second
    account of it here would be the two-sources-for-one-question shape this application refuses.

    `seconds` is the clip the drive was compiled over: `clip_frames_on_grid` at `ASSEMBLY_FPS`,
    which is the Shot's window measured the way the preview measures it, so the readout's time
    axis is the axis of the picture above it rather than a rounding of the manifest's own float.
    It is served because nothing on the client can compute it — the frame grid is the export's
    arithmetic — and it is the denominator every `at` is drawn against.
    """

    shot_id: str
    seconds: float = 0.0
    bindings: list[ShotDriveBinding] = Field(default_factory=list)


def wake_joiner(future: asyncio.Future[None]) -> None:
    """Release one waiting preview request. Runs on that request's own event loop.

    A future that is already done is the ordinary case of a client that hung up while its render
    was running, not an error: the request it belonged to is gone and there is nobody to wake.
    """
    if not future.done():
        future.set_result(None)


@dataclass(slots=True)
class PreviewRender:
    """The one in-flight preview render for a project, and the guarantees AD-24 needs.

    **Cancellation is a kill, and it is not the guarantee.** Killing the ffmpeg is how a
    superseded render stops costing CPU, and it is genuinely all it is for. It cannot be the
    thing that keeps a stale picture off the Monitor, because there is always a moment when the
    process has already exited successfully and the answer has not been returned yet — and
    because a supersede can arrive in the window between this record being registered and the
    subprocess existing at all, when there is nothing to kill.

    **`superseded` is the guarantee.** The render writes to a scratch file named by `token` and
    the caller publishes it — one atomic rename onto the fingerprint's name — only after reading
    this flag. A render that was superseded at any point, at any stage, deletes its scratch file
    instead. So a cancelled render cannot land its output and be served as current, whether it
    was killed, whether the kill arrived too late, and whether it was ever spawned: the file only
    ever reaches the cache through a gate that is closed the moment a newer request appears.

    Both directions are covered by `attach`, which is what closes the ordering hole: a supersede
    that arrives before the process exists sets the flag, and the process kills itself as soon as
    it is handed over.

    **`join` and `finish` are the pair R-22 needs.** Supersede discards *stale* work; a request
    whose fingerprint equals this one's is not stale work, it is this exact render asked for a
    second time, so it joins rather than restarting. `join` hands back something to await;
    `finish` records the outcome and releases everyone waiting. Every terminal path calls
    `finish` exactly once — published, failed, superseded, or abandoned by an exception — so a
    joiner is released whatever happens and can never wait on something that will never publish.
    Waiting is a future, never a poll: nothing here sleeps and retries.

    `published` is the joiner's half of the publish gate. It is set only on the one path that has
    already read `superseded` as false, in the same run of synchronous code that renames the
    scratch file into the cache — so a joiner that sees it knows the clip is there, and a joiner
    on a render that was superseded at any point sees `False` and is refused like the render it
    joined.
    """

    #: Names the scratch file this render writes to, so two renders — the superseded one still
    #: winding down and its replacement — can never write the same bytes.
    token: str
    #: What this render is producing, and, since R-22, what an arriving request is compared
    #: against: a different fingerprint supersedes this render, an equal one joins it. The
    #: *publish* gate remains `superseded` alone — a fingerprint comparison at publication time
    #: would be answering a question nobody asked, since by then the only thing that matters is
    #: whether something newer arrived.
    fingerprint: str
    process: Any = None
    superseded: bool = False
    #: True once this render has an outcome, whatever the outcome is.
    finished: bool = False
    #: One future per waiting request, paired with the event loop that future belongs to.
    #:
    #: **Not an `asyncio.Event`**, which would be the obvious primitive and is the wrong one
    #: here: an Event binds to the first loop that touches it and refuses every other, so a
    #: render finishing on one loop cannot release a request parked on another — it raises
    #: inside the render and leaves the waiter hanging for good. Served by uvicorn there is only
    #: ever one loop and it would work; under `TestClient`, which starts a fresh loop per
    #: request, it deadlocks. A primitive that is correct only under a topology the tests do not
    #: reproduce is a primitive whose failure mode is a hang in somebody else's test, so this
    #: pairs each future with its loop and wakes it through `call_soon_threadsafe`, which is
    #: correct on one loop and on several. (`dataclasses.field` is imported aliased because
    #: `field` is a loop variable in six places in this module and the bare name would
    #: shadow every one of them.)
    waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = dataclass_field(
        default_factory=list
    )
    #: Guards `waiters` and `finished` as one fact, so a request cannot register into a list
    #: that `finish` has already emptied and then wait on nobody. Uncontended whenever there is
    #: one loop, and never held across an await, a subprocess or any I/O.
    lock: threading.Lock = dataclass_field(default_factory=threading.Lock)
    #: True only if the scratch file was renamed into the cache under `fingerprint`.
    published: bool = False
    #: Why the render failed, as ffmpeg told it — the `detail` a joiner puts in its own refusal,
    #: rather than a finished sentence, because a joiner names its own Shot and may not be the
    #: Shot that started the render.
    error: str | None = None
    #: How many requests attached to this render instead of starting one of their own. It is
    #: here for the same reason the registry is on `app.state`: a join is otherwise invisible
    #: from outside, and a test that has to infer one from timings is a test that can pass by
    #: luck. It also names, in one number, the work R-22 did not do.
    joiners: int = 0

    def attach(self, process: Any) -> None:
        """Take the live subprocess, and kill it immediately if the supersede already came."""
        self.process = process
        if self.superseded:
            self._kill()

    def supersede(self) -> None:
        """A newer request has arrived. Close the publish gate first, then stop the work."""
        self.superseded = True
        self._kill()

    def join(self) -> asyncio.Future[None] | None:
        """Attach the calling request to this render, and hand back what it must await.

        `None` means the render finished before this request could attach — not a race lost but
        a question already answered: `published`, `superseded` and `error` are readable right
        now, and the caller reads them the same way it would after waiting.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        with self.lock:
            if self.finished:
                return None
            self.waiters.append((loop, future))
            self.joiners += 1
        return future

    def finish(self, *, published: bool = False, error: str | None = None) -> None:
        """Record how this render ended and release everyone waiting on it.

        Idempotent, and that is the point rather than a convenience: the render's own `finally`
        calls it unconditionally so that a handler killed mid-flight still releases its joiners,
        and it must not overwrite the outcome the body already recorded a line earlier.
        """
        with self.lock:
            if self.finished:
                return
            self.finished = True
            self.published = published
            self.error = error
            waiting = list(self.waiters)
            self.waiters.clear()
        for loop, future in waiting:
            try:
                loop.call_soon_threadsafe(wake_joiner, future)
            except RuntimeError:
                # That request's loop has already closed, so the request itself is gone. There
                # is nobody to wake and nothing to report; the outcome stands recorded either
                # way, which is what the publish gate reads.
                continue

    def _kill(self) -> None:
        process = self.process
        if process is None or process.returncode is not None:
            return
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            # Already gone. The publish gate is closed either way, which is the part that
            # matters; a process that ended between the check and the signal is not a failure.
            pass


class ShotPreviewResponse(BaseModel):
    """One Preview Clip, and every derived number that decided which clip it is.

    `fingerprint` is the whole staleness story travelling on the wire (AD-23). Nothing stores a
    stale flag; a client holds the fingerprint it is playing, asks again after a change, and
    compares. A different fingerprint means a different picture, and that is the only comparison
    there is — which is why there is no `stale` field here and no percentage anywhere.

    `rendered` says whether this answer came out of a render that ran for it — its own, or the
    identical one it joined under R-22 — rather than out of a clip that was already on disk. It
    is a fact about what just happened rather than a property of the clip, and it is here because
    "nothing was re-rendered" is otherwise unobservable from outside — the AC that the cache is
    served and nothing re-runs has to be checkable by something. A joiner reports `true`: no
    second ffmpeg ran, but the clip it is being handed was made just now and a client that
    re-loads on `rendered` must re-load.

    `width`/`height` are the clip's real dimensions: **half the export's**, never half this
    take's (AD-29). A Shot whose aspect differs from the project's therefore reports the
    project's shape and carries the letterbox inside it.
    """

    shot_id: str
    fingerprint: str
    #: Media-relative, under the project's media dir — `previews/<fingerprint>.mp4`.
    preview: str
    #: The URL the existing project-media route serves it at, Range service included.
    preview_url: str
    width: int
    height: int
    #: The window's frames on the export's own 24 fps grid — `clip_frames_on_grid`, the same
    #: arithmetic the export uses, so the preview is exactly as long as the shipped clip.
    frames: int
    window_seconds: float
    rendered: bool


class BoundaryPreviewResponse(BaseModel):
    """One Preview Clip of a **boundary**: the outgoing Shot, the blend and the incoming Shot as
    one continuous piece (FX-21, story 11.5).

    **A different subject from `ShotPreviewResponse`, which is R-35 stated on the wire.** That one
    names a Shot and a window; this names a seam between two Shots, and its `fingerprint` comes
    out of `effects.boundary_fingerprint` rather than `preview_fingerprint`. Widening the Shot
    preview's key to carry two takes was rejected on a measurement: an input added there that does
    not canonicalise to nothing when absent renames every cached clip in every project on the day
    this merges, for pictures that did not change.

    `transition` and `transition_seconds` are what the clip is a picture *of*, and they are here
    for FX-NFR-3 rather than for decoration: a client showing "Dissolve, 0.50 s" beside the clip
    is showing the export's own two facts, which are the two `assembly.xfade_stage` writes into
    both graphs. `transition_seconds` is the blend on the assembly grid -- `blend_frames /
    ASSEMBLY_FPS` -- so it is the Overlap's own length, quantised the one way the export quantises
    it, and never a second number derived from a second rule.

    `lead_frames` and `tail_frames` say how much of each Shot is on either side of the blend. They
    are not always `assembly.TRANSITION_PREVIEW_MARGIN_FRAMES`: a Shot with less than that on its
    own side of the boundary supplies what it has, which is the same clamp
    `effects.one_sided_transition_stages` applies to a treatment and for the same reason -- a leg
    read from frames its Shot does not cover is a picture of the wrong seconds, at rc 0.
    """

    #: The **outgoing** Shot, which is the boundary's identity: AD-30 makes `transition_out` on
    #: the earlier Shot authoritative and the later Shot's `transition_in` a mirror, so a boundary
    #: named by its incoming side would be named by the field nothing renders from.
    shot_id: str
    after_shot_id: str
    fingerprint: str
    preview: str
    preview_url: str
    width: int
    height: int
    #: Every frame in the clip: the lead, the blend and the tail. `frames` and `window_seconds`
    #: mean here exactly what they mean on a Shot preview, so a client holding either can ask the
    #: same two questions of it.
    frames: int
    window_seconds: float
    lead_frames: int
    blend_frames: int
    tail_frames: int
    transition: str
    transition_seconds: float
    rendered: bool


class ShotEffectsCopyRequest(BaseModel):
    """Which Shots one stack is copied onto. Named, never inferred.

    `None` and not `[]` as the default, exactly as `ShotEffectsRequest.effects` is and for the
    same reason: under a defaulting factory a body whose key is misspelled — `{"targts": [...]}` —
    binds to the empty list and becomes indistinguishable from a deliberate one. Here that would
    answer "0 shots copied" and report success for a request that named five. So absence is
    refused by name (`SHOT_EFFECTS_COPY_WITHOUT_TARGETS_REFUSAL`), and so is an explicitly empty
    list, because a copy onto nobody is not a thing this route does either way.
    """

    targets: list[str] | None = None


class ShotEffectsCopyRefusal(BaseModel):
    """One target a copy left alone, and the sentence saying why.

    The sentence travels rather than a code, so the client shows it whole. Those refusals are
    written to be read by a Director — they name the Shot and the act that clears the lock — and a
    client composing its own wording from a code would be a second refusal for one fact.
    """

    shot_id: str
    #: The Shot as this application names one everywhere else: `SHOT 03 (shot_a1b2)`. A bare id
    #: appears nowhere in the interface, and the report is read beside a timeline.
    shot: str
    detail: str


class ShotEffectsCopyResponse(BaseModel):
    """What one copy did: the whole Project back, a count that landed, and every refusal named.

    A `response_model` for retrospective item A13's reason — a route shaped as a bare dict appears
    in no `/openapi.json` and no client can discover it — and the `Project` is carried for the
    idiom every purpose-built shot action already follows, so a client redraws from what was
    stored rather than from what it hoped it sent.

    `applied` is a list of names and not just a number because the count is derived from it here
    rather than reported alongside it, which is one fewer pair of numbers that can disagree; the
    client's report says "4 shots" and never enumerates them, which is FX-6's own division.

    `effects` is how many effects were copied, **and zero is a real answer**: a copy from an empty
    stack clears its targets, and a report that only counted shots would describe that write in
    exactly the same words as one that graded them.
    """

    project: Project
    source: str
    effects: int = 0
    applied: list[str] = Field(default_factory=list)
    refused: list[ShotEffectsCopyRefusal] = Field(default_factory=list)


class EffectParameterSpec(BaseModel):
    """One parameter of one catalogue entry, flattened for the wire.

    Three parameter kinds live in `effects.py` as three dataclasses, and a picker has to draw a
    different control for each — so `kind` is stated rather than inferred from which fields came
    back populated. `minimum`/`maximum`/`integer` belong to a number, `choices` to a choice, and a
    LUT parameter carries **no default at all**: every other parameter has a value that means
    "leave it alone", and a grade with no look chosen has nothing to apply, so omitting it is
    refused by name rather than resolved to whichever file happened to sort first.
    """

    name: str
    label: str
    kind: Literal["number", "choice", "lut"]
    default: float | str | None = None
    minimum: float | None = None
    maximum: float | None = None
    integer: bool = False
    choices: list[str] = Field(default_factory=list)
    #: **Why the music cannot reach this parameter, or `""` where it can** — the catalogue's own
    #: clause off `NumberParameter.drive`, served rather than re-derived, because AD-27 says the
    #: catalogue is the only thing entitled to decide and R-25/R-29 say the answer is a measured
    #: property of the `(parameter -> filter option)` pair rather than of the family. A client
    #: that decided drivability for itself would need the composers to do it.
    #:
    #: Empty is the *only* thing that means drivable, so there is one flag and not two: a
    #: `drivable: bool` beside this could disagree with it, and the pair would then have to be
    #: kept in step by hand at both ends of the wire.
    #:
    #: A choice and a look carry `NOT_A_NUMBER`'s clause, which is the same object
    #: `effects._validate_bindings` substitutes for them — a band produces a number, and neither
    #: is one. Carried whole and unparaphrased: the panel frames it, never rewrites it.
    drive_reason: str = ""


class EffectDefinitionSpec(BaseModel):
    """One effect a stack may name: its id, its family, what to call it, and its parameters."""

    effect: str
    family: str
    label: str
    parameters: list[EffectParameterSpec] = Field(default_factory=list)


class EffectLookSpec(BaseModel):
    """One look the server found in the folder — **the id and the name, never the path.**

    That omission is the security property of the Grade family, not tidiness. A client picks from
    this list and sends an id back; the server matches the id against what it discovered and
    resolves the file itself. Nothing a client sends is ever joined onto a directory or
    interpolated into a filter string, so there is no path here for a client to have opinions
    about — and a listing that leaked absolute paths would invite exactly that.
    """

    lut_id: str
    name: str


class EffectBindingSettingSpec(BaseModel):
    """One of a Parameter Binding's six *settings*, with the bounds it is refused outside.

    `effects.BINDING_SETTINGS` owns these and this is derived from it, for the reason every other
    bound on this response is derived rather than transcribed: Epic 9 shipped a defect where the
    client ignored bounds the server was already sending, and the answer to that is not to teach
    the client a second copy of them but to send the ones it must obey.

    **Depth is deliberately not here.** Its bound is not a constant — it is the span of whatever
    parameter the binding drives — so it is read off that parameter's own `minimum` and `maximum`,
    which this response already carries. A `depth` entry with invented bounds would be the one
    number on this panel that did not come from the thing that refuses it.
    """

    name: str
    default: float
    minimum: float
    maximum: float
    #: The one drive that reads this setting, or `""` for one both read. `effects.DRIVE_ONLY_SETTINGS`
    #: owns it: `hold` and `sustain` are the sustain gate's own timings and `drive_series` reads
    #: neither under `punch`.
    #:
    #: Served because the panel has to draw them and cannot answer this for itself. A live `Hold`
    #: box under a `punch` binding is the control-that-does-nothing R-24 rejects by name; a client
    #: that decided which drive reads what would be a second copy of this module's drive model.
    drive: str = ""


class EffectCatalogueResponse(BaseModel):
    """Every effect, every bound, and every discovered look — one read, so a picker is one request.

    Together rather than as two routes because they are opened at the same moment by the same
    control: the Effects tab needs the cards *and* the looks the Grade card offers before it can
    draw anything, and two requests would be two round trips for one panel.

    `families` is the fixed stage order (AD-17), served so a client groups cards the way the chain
    composes them without a second copy of that order living in JavaScript. **There is no second
    definition of the catalogue on the client**: everything a picker needs to draw a control is
    here, derived from `effects.EFFECT_CATALOGUE` at the moment of the read.
    """

    families: list[str] = Field(default_factory=list)
    effects: list[EffectDefinitionSpec] = Field(default_factory=list)
    looks: list[EffectLookSpec] = Field(default_factory=list)
    #: The two drives, in `effects.DRIVE_MODES`' own order, so the band panel's segmented control
    #: is built from a served list rather than from two literals in JavaScript. There is no third
    #: and a third would appear here without the panel being edited — which is the same bargain
    #: `families` already makes with `FAMILY_ORDER`.
    #:
    #: **Neither is marked as a default and none can be**, which is FX-14 expressed on the wire: a
    #: served `default_drive` would be exactly the inference the acceptance criterion forbids.
    drives: list[str] = Field(default_factory=list)
    #: The six settings a binding carries besides its parameter, its drive and its depth, with
    #: their defaults and their bounds. See `EffectBindingSettingSpec`.
    binding_settings: list[EffectBindingSettingSpec] = Field(default_factory=list)


def effect_catalogue_report(looks: Sequence[LutEntry]) -> EffectCatalogueResponse:
    """The catalogue as the wire carries it, built from `effects.EFFECT_CATALOGUE` itself.

    Derived, never transcribed: an effect added to that table appears here without anybody
    remembering to add it, which is the same argument `_withheld_fields` makes one level up. The
    `looks` are passed in rather than discovered here because discovery reads every `.cube` in the
    folder to its end — 221 ms cold on the Director's 44.2 MB pack — and a picker that re-read the
    folder every time it opened is the measured failure this route exists to avoid.
    """
    definitions: list[EffectDefinitionSpec] = []
    for definition in EFFECT_CATALOGUE.values():
        parameters: list[EffectParameterSpec] = []
        for parameter in definition.parameters:
            if isinstance(parameter, NumberParameter):
                parameters.append(
                    EffectParameterSpec(
                        name=parameter.name,
                        label=parameter.label,
                        kind="number",
                        default=parameter.default,
                        minimum=parameter.minimum,
                        maximum=parameter.maximum,
                        integer=parameter.integer,
                        # The catalogue's own clause, whole. `NumberParameter.drive` has no
                        # default precisely so this can never be a guess: a parameter added
                        # without a classification does not construct and the application does
                        # not start.
                        drive_reason=parameter.drive.reason,
                    )
                )
            elif isinstance(parameter, ChoiceParameter):
                parameters.append(
                    EffectParameterSpec(
                        name=parameter.name,
                        label=parameter.label,
                        kind="choice",
                        default=parameter.default,
                        choices=list(parameter.choices),
                        # The same object `_validate_bindings` substitutes for anything that is
                        # not a `NumberParameter`, so the sentence the panel shows and the one
                        # the route would refuse with are the same string.
                        drive_reason=NOT_A_NUMBER.reason,
                    )
                )
            elif isinstance(parameter, LutParameter):
                # Declares no default. See `EffectParameterSpec`.
                parameters.append(
                    EffectParameterSpec(
                        name=parameter.name,
                        label=parameter.label,
                        kind="lut",
                        drive_reason=NOT_A_NUMBER.reason,
                    )
                )
            else:
                # Named rather than fallen through to. A fourth parameter type added to
                # `effects.py` and not to `EffectParameterSpec.kind` would otherwise be served as
                # whichever branch happened to be last, and a picker would draw the wrong control
                # for it — the silent direction. `EFFECT_CATALOGUE`'s own duplicate-id check is
                # the precedent for refusing loudly instead.
                raise TypeError(
                    f"{type(parameter).__name__} is not a parameter kind this route can serve."
                )
        definitions.append(
            EffectDefinitionSpec(
                effect=definition.effect_id,
                family=definition.family,
                label=definition.label,
                parameters=parameters,
            )
        )
    return EffectCatalogueResponse(
        families=list(FAMILY_ORDER),
        effects=definitions,
        looks=[EffectLookSpec(lut_id=entry.lut_id, name=entry.name) for entry in looks],
        drives=list(DRIVE_MODES),
        binding_settings=[
            EffectBindingSettingSpec(
                name=name,
                default=default,
                minimum=minimum,
                maximum=maximum,
                drive=DRIVE_ONLY_SETTINGS.get(name, ""),
            )
            for name, default, minimum, maximum in BINDING_SETTINGS
        ],
    )


def stored_effect_stack(specs: Sequence[Mapping[str, Any]]) -> list[EffectSpec]:
    """An agreed stack as the manifest holds it: **what the Director wrote, not what it resolved to.**

    Call this only after `effects.validate_stack` has agreed, which is what lets it index `effect`
    and trust `enabled` without asking again.

    The decision worth stating, because the other one is tempting. `validate_stack` hands back a
    `ResolvedEffect` per entry with *every* declared parameter filled in from the catalogue, and
    storing that would freeze each effect at the defaults of the day it was added. It is rejected
    for two reasons. A read must return what a write sent — the panel would otherwise show a card
    the Director never touched sprouting eight explicit numbers the moment it was saved — and a
    manifest full of frozen defaults makes a corrected default unable to reach the projects that
    would benefit from it, silently, which is the same class of staleness `SongAnalysis` refuses a
    flag for. What is *not* lost by storing sparsely: the resolution is pure and deterministic, so
    `build_effect_stages` recomputes exactly the same values at export from exactly this stack.

    `parameters` is normalised from a JSON `null` to an empty mapping, which is the one shape
    `validate_stack` deliberately accepts and `EffectSpec`'s `dict[str, Any]` deliberately does
    not — the validator reads "no parameters given" out of it and fills every default, so refusing
    it here would 500 on a body the validator had just agreed to. Copied rather than aliased, so
    the stored model never shares a mutable object with the request that carried it.

    **`bindings` is stored exactly as `parameters` is, and this function is deliberately not where
    a binding is judged.** It writes whatever the spec it is handed carries, which is what makes it
    usable by both of its callers; *which* bindings a card is entitled to carry is
    `adopted_effect_stack`'s question, and this is called with that answer already substituted in.
    Putting the judgement here instead would make the one function that stores a stack unusable by
    the one route that mints a binding.

    **`id` is the same bargain one field up** (R-33). This is the *store* point, so a spec that
    names no id is minted one here and a spec that names one is trusted — and it is trusted
    because `adopted_effect_stack` is the only thing that puts one in front of this function: a
    client's own id reaches a manifest never, because a body's id is only ever compared for
    equality against an id the store already holds, and anything else is dropped in favour of a
    fresh mint. Calling this with a raw request body would store a forged card id, which is why
    both callers adopt first and why this says so rather than assuming the next one will look.
    """
    return [
        EffectSpec(
            id=spec["id"] if isinstance(spec.get("id"), str) and spec.get("id") else new_id("fx"),
            effect=spec["effect"],
            enabled=bool(spec.get("enabled", True)),
            parameters=dict(spec.get("parameters") or {}),
            bindings=[dict(binding) for binding in spec.get("bindings") or ()],
        )
        for spec in specs
    ]


#: Who a write's bindings were adopted from, filled into the sentence below. Two constants rather
#: than two sentences, because there is one rule and it is stated once.
BINDING_CARRIER_SHOT = "this shot"
BINDING_CARRIER_PROJECT = "this project"


#: Why a stack write was refused: the bindings a card arrived with are not the bindings the store
#: holds on the card of that id, so the write was minting, altering or dropping one.
#:
#: **This is AD-16 and story 10.1's "written only by the dedicated binding route", made a property
#: of every other door rather than a promise about one** — but it is a *diagnostic* now rather than
#: the guard. `adopted_effect_stack` has already replaced the card's bindings with the stored
#: card's before this sentence is composed, so nothing a body says about a binding can reach a
#: manifest whether it is refused or not. What the refusal buys is the thing every `_adopt_*`
#: sibling gives up: a client that has lost a binding is *told*, by name, instead of being answered
#: 200 for a write that quietly changed the picture. Epic 10 refused by name on purpose and that
#: much was right; the id is what makes the comparison unambiguous enough to keep it.
#:
#: **Its predecessor was `carried_bindings_refusal`, and it was the guard**: a multiset of
#: validated bindings, subtracted, on the reasoning that an `EffectSpec` had no id to compare
#: instead. Two cards of one effect are indistinguishable to a multiset, so a generic write could
#: move a binding from a Bloom resting at 0.1 to one resting at 0.9 and change the rendered picture
#: at 200 (A3). Adopting by id makes that write express nothing at all rather than express a
#: relocation this sentence has to catch — which is R-33's whole reason for choosing the id over
#: patching the census.
#:
#: Compared as `effects.ParameterBinding` — the **validated** binding, every default filled in by
#: the catalogue — and never as stored JSON. The stored form is sparse on purpose
#: (`stored_effect_stack`), so `{"parameter": "amount", "drive": "punch", "depth": 0.4}` and the
#: same binding written out with all nine keys are one binding, and comparing text would call them
#: two and refuse a client that had merely round-tripped what it read.
BINDING_NOT_AS_HELD_REFUSAL = (
    "This write does not carry {effect}'s {parameter} as {source} holds it, so nothing was saved. "
    "A Parameter Binding is made, changed and taken off through the Shot's own bindings route and "
    "through nothing else; every other write echoes back the bindings it read, on the card id it "
    "read them from."
)


#: Why a bound Shot's stack write was refused for naming no card ids: the one write whose intent
#: genuinely cannot be recovered.
#:
#: A card is adopted by its id, so *removing* a card and *forgetting to send it* are different
#: writes — which is the whole point of the id, and it is what a client that has never heard of
#: one takes away again. Such a client sends a stack of cards that name nothing, every card reads
#: as new, and every binding on the Shot goes with the cards it could not name. That is
#: indistinguishable from a Director clearing the stack, and it is the defect this whole thread
#: began with: losing a binding looks exactly like removing its card unless something says
#: otherwise.
#:
#: **It fires only on a Shot that actually holds a binding**, so every client written before ids
#: existed goes on writing unbound stacks exactly as it always did. The remedy is in the sentence
#: because the client that hits this cannot work out from the outside what it failed to send.
#:
#: The residue, stated rather than papered over: a body that names *some* ids and drops others is
#: read at its word, because a card whose id is absent is a card this write does not have. A client
#: that echoed one id and invented a second card in the same write, on a Shot whose binding sat on
#: the card it forgot, is the one shape left that loses a binding at 200 — and it is the shape that
#: is also a legitimate "take that card off and add this one", which is why it is not refused.
SHOT_EFFECTS_WITHOUT_CARD_IDS_REFUSAL = (
    "This shot holds a Parameter Binding and this stack names no card ids at all, so nothing was "
    "saved. Every effect card carries an id, and a write echoes back the ids it read: without them "
    "a card this write left out cannot be told from a card whose binding it would have destroyed. "
    "Send the stack back as it was read, ids and all."
)


@dataclass(frozen=True, slots=True)
class AdoptedStack:
    """What a generic stack write actually stores, and what it silently changed on the way.

    Two answers from one walk rather than two functions over one list, because the second is a
    statement *about* the first: a refusal composed by a second pass could disagree with what the
    first pass stored, and disagreeing answers to one question is the defect this repository has
    now paid for six times.

    `refusal` is `""` when the body said nothing about a binding that the store did not already
    say. It is a *detail* sentence, never wrapped: `replace_shot_effects` raises it whole and
    `_adopt_shot_effects` puts `SHOT_BINDINGS_UNCARRIED_REFUSAL`'s Shot name in front of it.
    """

    stack: list[EffectSpec]
    refusal: str


def _card_id(spec: Mapping[str, Any]) -> str:
    """The card id a body named, or `""` for a body that named none.

    **A non-string is `""` and never raises.** `EFFECT_SPEC_KEYS` declares `id` so that a client
    round-tripping the stack it read is not refused for an undeclared key, and the validator says
    nothing about the *value* — it does not need to, because the value is never stored and is only
    ever looked up. But a lookup is a `dict` lookup, and `{"id": {"a": 1}}` is a body a client can
    genuinely send: unguarded, that is `TypeError: unhashable type` — a 500 out of the one function
    whose whole job is to keep a body's claims away from the manifest.
    """
    given = spec.get("id")
    return given if isinstance(given, str) else ""


def _card_bindings_refusal(
    spec: Mapping[str, Any], adopted: Sequence[Mapping[str, Any]], *, source: str
) -> str:
    """`""` when a card arrived carrying exactly the bindings that were adopted onto it; the
    sentence naming the first parameter they differ on, otherwise.

    The offender is chosen **by parameter name** and not by the order it arrived in, so one card
    refuses with one sentence however its bindings were ordered — a refusal that moved when a list
    was reordered would be a refusal a test could not assert.

    Both sides go through `effects.agreed_bindings`, which is a fact about the *catalogue* and not
    about the looks folder, so a card whose `.cube` has been deleted is compared exactly as it was
    yesterday (A1). A card the catalogue cannot agree to at all — an effect this build no longer
    ships, a hand-edited stored binding — returns `""` rather than refusing: the write it would
    refuse has already had the stored bindings adopted onto it, so silence here loses nothing, and
    a refusal a Director cannot act on is worse than the `_adopt_*` family's ordinary silence.
    """
    given = spec.get("bindings") or ()
    if not given and not adopted:
        return ""
    effect_id = spec.get("effect")
    if not isinstance(effect_id, str):
        return ""
    try:
        arrived = {binding.parameter: binding for binding in agreed_bindings(effect_id, given)}
        held = {binding.parameter: binding for binding in agreed_bindings(effect_id, adopted)}
    except EffectRefusal:
        return ""
    differing = sorted(
        name for name in set(arrived) | set(held) if arrived.get(name) != held.get(name)
    )
    if not differing:
        return ""
    return BINDING_NOT_AS_HELD_REFUSAL.format(
        effect=effect_id, parameter=differing[0], source=source
    )


def _held_bindings(cards: Sequence[EffectSpec]) -> dict[ParameterBinding, dict[str, Any]]:
    """Every Parameter Binding a set of stored cards holds, agreed, mapped to the JSON it is
    stored as.

    **The agreed binding is the key and the stored spelling is the value**, which is what lets the
    fallback below be an *adoption* rather than a body read: a body may say which held binding it
    means, and the bytes that reach the manifest come off the card that already holds it. A
    manifest full of frozen defaults is what `stored_effect_stack` argues against, and a client
    that spelled all nine keys out would otherwise write one.

    Keyed on the agreed binding for `_card_bindings_refusal`'s reason: the stored form is sparse,
    so comparing text would call two spellings of one binding two bindings.

    A card the catalogue cannot agree to contributes nothing and raises nothing. It costs no folder
    read on any path — `agreed_bindings` asks the catalogue about parameters and never about looks,
    which is what keeps a deleted `.cube` out of this answer (A1).
    """
    held: dict[ParameterBinding, dict[str, Any]] = {}
    for card in cards:
        try:
            agreed = agreed_bindings(card.effect, card.bindings)
        except EffectRefusal:
            continue
        # One agreed binding per stored entry, in order: `_validate_bindings` appends exactly one
        # for each and refuses a repeated parameter, so the two lists cannot differ in length.
        for binding, spelled in zip(agreed, card.bindings, strict=True):
            held.setdefault(binding, dict(spelled))
    return held


def _copied_bindings(
    spec: Mapping[str, Any],
    held: dict[ParameterBinding, dict[str, Any]],
    *,
    source: str,
) -> tuple[list[dict[str, Any]], str]:
    """What a card arriving on a Shot the store does **not** hold may keep, and the refusal when it
    named a binding nothing in the project holds.

    **This is the one place a card id is not the key, and it is the one place it cannot be.** A
    Shot that is new to the store has no stored card to adopt from, so the body's card id is a
    *provenance claim* — "this card was copied from that one" — and nothing guarantees the card it
    names still exists. Measured 2026-08-28, both ways round: a **redo** of a Split replays the
    plan as the browser last held it, naming the card the Split actually minted, which the undo in
    between has since deleted; an **undo of a delete** replays the plan as the last save left it,
    naming the card that Shot was *copied from*, which does still exist. Requiring the id refuses
    the first; requiring it and correcting the browser's snapshot to match the store refuses the
    second instead. The two replay paths want opposite answers, so accuracy is not the axis and no
    ordering fixes it.

    So this door asks AD-16's actual question — *was this binding handed to you?* — as **set
    membership over what the project holds**. What it is not, and what the census it replaced was:
    it is not a multiset, so nothing counts; it is not an address, so it never decides *which* card
    anything lands on; and it never runs `validate_stack` over a stored stack, so a deleted `.cube`
    cannot empty it (A1). A3 is untouched because `PUT .../effects` passes no `elsewhere` at all
    and stays strict — a binding still cannot move between two cards of one effect, and that is
    still structural rather than refused.

    The bindings handed back are the **stored** spellings, so a body selects and never supplies.
    """
    given = spec.get("bindings") or ()
    effect_id = spec.get("effect")
    if not isinstance(effect_id, str):
        return [], ""
    try:
        arrived = agreed_bindings(effect_id, given)
    except EffectRefusal:
        # Unagreeable here means unagreeable at the caller's own `validate_stack`, which has
        # already refused this write in the catalogue's words. Nothing to add and nothing to keep.
        return [], ""
    absent = sorted(binding.parameter for binding in arrived if binding not in held)
    if absent:
        return [], BINDING_NOT_AS_HELD_REFUSAL.format(
            effect=effect_id, parameter=absent[0], source=source
        )
    return [dict(held[binding]) for binding in arrived], ""


def adopted_effect_stack(
    specs: Sequence[Mapping[str, Any]],
    *,
    own: Sequence[EffectSpec] = (),
    elsewhere: Sequence[EffectSpec] = (),
    source: str = BINDING_CARRIER_SHOT,
) -> AdoptedStack:
    """An arriving stack with **every card's bindings taken from the stored card of that id**, and
    every id the store cannot vouch for replaced by a fresh one.

    This is AD-16's Rule spelled for a field one level down — *"adopts them from the stored Shot,
    via the established `_adopt_*` idiom… a body that omits them, or invents them, does not change
    them"* — and R-33's ruling as a function. **The bytes of every binding that reaches a manifest
    through here come off a card the store already holds**, so a body cannot invent one, alter one,
    relocate one or lose one. What a body may do, at one door and one door only, is *select* which
    held binding a card being created copies — see `_copied_bindings`, and the two measurements
    that put it there.

    **The two card sources are different questions, and that is why they are two arguments.**

    * `own` is the cards **this Shot already holds**. A body naming one of these is writing that
      card back, so it keeps its id and takes its bindings. This is `PUT .../effects` — the route
      the panel writes on every slider drag, card toggle and Story 9.4 reorder.
    * `elsewhere` is every card the **project** holds on some *other* Shot. A body naming one of
      these is a new Shot built from an old one — Split and Duplicate, through
      `_adopt_shot_effects`, which is why `SHOT_PLAN_CONTENT_FIELDS` says the two halves of one
      shot are one shot's look. It takes that card's bindings and is minted **a new id**, because
      the card it was copied from belongs to a Shot that still exists and still holds it.
      A card here that names *no* card the project holds falls to `_copied_bindings`, which is the
      one place an id is not the key and the one place it cannot be: a replayed creation — an Undo,
      a Redo — names a card that the write it is undoing may have deleted. That door asks whether
      the binding is one the project holds instead, and hands back the store's own copy of it.

    `own` never contributes to that fallback and `PUT .../effects` passes no `elsewhere` at all, so
    the narrow route stays strict: an id or nothing. That is what keeps A3 structural rather than
    refused, and it is why a binding still cannot be copied onto a second card of one effect on a
    Shot the store already holds.

    That last mint is the whole of the cloned-id problem, resolved where the clone is stored rather
    than detected afterwards. `api.newShotFromPlan` deep-copies the source Shot's stack, ids and
    all, so a Split arrives with two Shots claiming one card id; a lookup keyed on that id would
    then depend on which Shot was read first the moment the halves diverged. Minting at the moment
    a card is stored onto a Shot that does not already hold it means **no two Shots ever hold one
    card id**, so there is no collision to resolve later and no order for a later lookup to depend
    on. `POST .../effects/copy` mints for the same reason at its own copy point, and it is the
    third door rather than an exception to this one.

    **An id may be claimed once.** A body naming one stored card twice gets one adoption: the first
    entry takes the card and the second is a new card with a new id and no bindings. Otherwise one
    echoed id would multiply a binding across a stack — A4's shape, one level down — and a manifest
    would hold two cards claiming one identity.

    **The count invariant is gone and is not replaced.** *"Carried at most as many times as it was
    held"* was `carried_bindings_refusal`'s rule and it was never true of the gesture it was meant
    to describe: a Split of a bound Shot legitimately ends with two bound cards, and
    `POST .../effects/copy` produces that state on purpose, with an announcement. What is bounded
    now is *authorship* rather than *arithmetic* — every binding in the result is a copy of one the
    store already held, on a card whose identity the store minted.

    **No look is read and nothing is validated here.** Adoption is a copy keyed by a string, so it
    costs no folder read on any path — which is what lets `_adopt_shot_effects` ask it on every
    ordinary save. The callers still run `effects.validate_stack` over the body first; that is
    unchanged and is the catalogue's own gate (AD-27).
    """
    kept = {spec.id: spec for spec in own if spec.id}
    held: dict[str, EffectSpec] = dict(kept)
    for spec in elsewhere:
        if spec.id:
            held.setdefault(spec.id, spec)
    claimed: set[str] = set()
    # Built once, on the first card that needs it, and never at all for the common write: it is
    # asked only by a Shot the store does not hold, carrying a binding, naming no card that does.
    copied: dict[ParameterBinding, dict[str, Any]] | None = None
    carried: list[dict[str, Any]] = []
    refusal = ""
    named = False
    for spec in specs:
        card_id = _card_id(spec)
        if card_id:
            named = True
        stored = held.get(card_id) if card_id and card_id not in claimed else None
        if stored is not None:
            claimed.add(card_id)
            adopted = [dict(binding) for binding in stored.bindings]
            refusal = refusal or _card_bindings_refusal(spec, adopted, source=source)
        elif elsewhere and (spec.get("bindings") or ()):
            # A card on a Shot the store does not hold, naming no card it does hold. See
            # `_copied_bindings`: this is the one door where an id cannot be required, because the
            # card a replayed creation names may have been destroyed by the write it is undoing.
            if copied is None:
                copied = _held_bindings(elsewhere)
            adopted, unheld = _copied_bindings(spec, copied, source=source)
            refusal = refusal or unheld
        else:
            adopted = []
            refusal = refusal or _card_bindings_refusal(spec, adopted, source=source)
        carried.append(
            {
                **spec,
                # Kept only where the store can vouch for it *on this Shot*, minted by
                # `stored_effect_stack` everywhere else — including for a card copied off another
                # Shot, which is the clone the paragraphs above are about.
                "id": card_id if stored is not None and card_id in kept else "",
                "bindings": adopted,
            }
        )
    if not refusal and not named and carried and any(spec.bindings for spec in own):
        refusal = SHOT_EFFECTS_WITHOUT_CARD_IDS_REFUSAL
    return AdoptedStack(stack=stored_effect_stack(carried), refusal=refusal)


#: Why one Shot's effects may not be written. Names the Shot as the timeline names it — a bare
#: `shot_a1b2c3d4e5f6` appears nowhere in the interface — and says what a lock is *for* rather
#: than only that one is set, which is `RENDER_AGAIN_LOCKED_REFUSAL`'s and
#: `EXPANSION_LOCKED_NOTICE`'s shape: a lock stops a write, and the human who set it can undo it.
#:
#: FX-7's server half. C2 disables the controls; this refuses regardless of what any client draws,
#: because a guard that exists only in the interface is not a guard.
SHOT_EFFECTS_LOCKED_REFUSAL = (
    "{shot} is locked, so its effects were not changed. Unlock the shot to change its look."
)


#: Why a whole-plan write was refused: a Shot it is *adding* carries a stack the catalogue will
#: not compose. `_adopt_shot_effects` raises it, so it is Split's and Duplicate's refusal.
#:
#: Two-layered like `ASSEMBLY_EFFECTS_REFUSAL` and for the same reason — `EffectRefusal` is a pure
#: function of a stack and has no idea which Shot carries it — and it says the *whole* write was
#: refused, because it was: nothing is saved. A save that landed nine shots and quietly dropped
#: the tenth's look is the failure this fix exists against, so the refusal must not read as
#: "your plan was saved, minus one thing".
SHOT_EFFECTS_UNCOMPOSABLE_REFUSAL = (
    "{shot} is new to this plan and arrives with an effect stack that cannot be composed, so "
    "nothing was saved. {detail}"
)


#: Why a whole-plan write was refused for its *bindings* rather than for its stack: a Shot it is
#: adding names a Parameter Binding that is not the one the card it copied holds, so the body was
#: minting or altering one through a route that never asked for it.
#:
#: A second wrapper rather than a reuse of the sentence above, because that one says the stack
#: "cannot be composed" and this stack composes perfectly well — the fault is what the body says
#: about a binding, not what the catalogue says about the card. `adopted_effect_stack` supplies
#: the whole of `{detail}`, and the write it refuses had already had the stored bindings adopted
#: onto it: the refusal is a diagnostic, and nothing a body claims here reaches a manifest either
#: way.
SHOT_BINDINGS_UNCARRIED_REFUSAL = "{shot} is new to this plan, so nothing was saved. {detail}"


#: Why a stack write that named no stack was refused, and how to say the thing it probably meant.
#:
#: `PUT .../effects {"efects": [...]}` used to answer 200 and store `[]`: pydantic ignores an
#: undeclared key, the declared one took its default, and a Director's grade was destroyed by a
#: typo that reported success. `validate_stack` never saw the body at all. So `effects` is
#: optional-with-no-default on the request model and its **absence** is refused here, while
#: `{"effects": []}` keeps working untouched — that is how a Director takes every card off, and
#: it is the one write on this route that costs no folder read.
#:
#: The remedy is in the sentence because absence has two very different causes — a misspelled key
#: and a client that meant to clear the stack — and the reader cannot be assumed to know which
#: one this application saw.
SHOT_EFFECTS_ABSENT_REFUSAL = (
    "This write named no effects at all, so {shot}'s look was left exactly as it was. An effect "
    'stack is sent as "effects": [...], and "effects": [] is how every effect comes off. '
    "Nothing was changed."
)


#: Why a binding write that named no bindings was refused. `SHOT_EFFECTS_ABSENT_REFUSAL`'s lesson,
#: applied to the field one level down before it can cost anything: a misspelled key binds to the
#: default, and a default of `[]` would clear a Director's binding and answer 200.
SHOT_BINDINGS_ABSENT_REFUSAL = (
    "This write named no bindings at all, so {shot}'s look was left exactly as it was. Bindings "
    'are sent as "bindings": [...], and "bindings": [] is how a binding comes off. Nothing was '
    "changed."
)


#: Why a binding write named a card that is not there. The count is in the sentence because the
#: two ways to get here — a stale panel and an off-by-one — read completely differently once the
#: reader knows how many cards the Shot actually has.
SHOT_BINDINGS_NO_SUCH_CARD_REFUSAL = (
    "{shot} has {count} effect cards, so there is nothing at position {index} to bind. Nothing "
    "was changed. Reload the shot's effects and try again."
)


#: Why a binding write named the wrong card. The client sends the effect it believes sits at that
#: position and this is what happens when it does not — a stack reordered or edited since the
#: panel was drawn, which is the one failure R-26 rejected an index for, made loud.
SHOT_BINDINGS_CARD_MOVED_REFUSAL = (
    "Position {index} of {shot} holds {held}, not {named}, so nothing was changed. The effect "
    "stack has been edited since this panel was drawn. Reload the shot's effects and try again."
)


#: Why a binding write named no effect at all. Separate from the sentence above because "you sent
#: the wrong one" and "you sent none" have different remedies, and a client that omitted the field
#: is a client that has not been written yet rather than one looking at a stale stack.
SHOT_BINDINGS_UNNAMED_CARD_REFUSAL = (
    "This write named no effect, so {shot}'s look was left exactly as it was. A binding write "
    'says which card it is addressing — "effect": "bloom" beside the position — because an '
    "effect stack can be reordered and a position on its own is not an identity."
)


#: Windows' own error number for a command line past its 32,767-character ceiling. Named because
#: `FileNotFoundError` is raised for both a missing binary and an over-long argv, and only the
#: number tells them apart — a bare `except FileNotFoundError` reported a working ffmpeg as
#: missing, which is the sort of message that sends a Director to reinstall something.
_COMMAND_LINE_TOO_LONG = 206

#: What that fault actually is, said to the person who can act on it. It names the tool so the
#: sentence reads like its sibling, and the length so the number is a fact rather than an
#: adjective — a Director who sees 40,060 knows the difference between "slightly over" and "this
#: is not what the control is for".
TOOL_COMMAND_TOO_LONG = (
    "The command built for {tool} is {length} characters long, and Windows will not accept one "
    "past 32767. {tool} itself is fine. Something in this project asked for more work in one "
    "step than a single command can carry."
)


#: How many effects one Shot may carry. The chain goes into a single `-vf` argument and Windows
#: caps a command line at 32767 characters, so an unbounded stack does not fail as an unbounded
#: stack: measured 2026-08-25, 985 grain cards built an argv of 32725 characters and exported,
#: 1200 built 40060 and came back **502 "ffmpeg is not installed or not on PATH"**, because
#: Windows raises `FileNotFoundError [WinError 206]` and `run_tool` maps every `FileNotFoundError`
#: to that sentence. That mapping predates this slice and is recorded as its own defect; what is
#: new here is that a client can grow the argv, so the bound belongs at the write.
#:
#: 32 and not 985. The cap is not the argv limit converted into cards — it is what a stack can
#: plausibly be, with the widest card (a `lut_look`, which carries a whole escaped path) leaving
#: the argv an order of magnitude clear of the limit. A Director who needs a thirty-third effect
#: on one Shot has found something this design has not thought about, which is a conversation and
#: not a number to raise quietly.
SHOT_EFFECT_STACK_LIMIT = 32

#: Why a stack was refused for its length. Names both numbers, because "too many" without the
#: bound tells a client nothing about what to send instead.
SHOT_EFFECTS_TOO_MANY_REFUSAL = (
    "An effect stack holds at most {limit} effects, and this one names {count}. "
    "Nothing was composed."
)


#: Why a copy that named no shots was refused. **Nothing here applies to "all shots"** — the
#: frozen boundary of this slice — so an empty target list is a client that has not chosen yet
#: rather than one asking for everything, and it is answered as a refusal instead of as a
#: successful copy onto nobody.
#:
#: The remedy is in the sentence for `SHOT_EFFECTS_ABSENT_REFUSAL`'s reason: absence has two
#: causes here, a client that meant to name shots and a client that thinks omission means all of
#: them, and the reader cannot be assumed to know which one this application saw.
SHOT_EFFECTS_COPY_WITHOUT_TARGETS_REFUSAL = (
    "This copy named no shots to copy {shot}'s look onto, so nothing was written. Name every "
    'target explicitly as "targets": [...] — this route has no "apply to every shot".'
)


#: Why a copy naming a shot this project does not hold was refused **whole**. The alternative —
#: applying to the ids that resolve and mentioning the rest in the report — is the half-applied
#: write this route exists to make impossible: a client that mistyped one id would grade four
#: shots and be told, in a line among others, that the fifth was never a shot at all.
SHOT_EFFECTS_COPY_UNKNOWN_TARGET_REFUSAL = (
    "This copy names {missing}, which this project does not hold, so nothing was written. Every "
    "shot named by a copy is written, or none of them is."
)


#: Why a copy naming its own source was refused. A shot cannot replace its stack with its own
#: stack meaningfully, and a client that named it has miscounted its target set — which is worth
#: saying, because the same miscount is what would silently drop a real target.
SHOT_EFFECTS_COPY_ONTO_ITSELF_REFUSAL = (
    "{shot} is both the source of this copy and one of its targets, so nothing was written. "
    "A shot does not need copying onto itself."
)


#: Why a copy was refused for the source's own stack. `validate_stack` runs **once**, before a
#: byte reaches any target, so a source stack a hand-edited manifest left uncomposable cannot be
#: multiplied across the plan. The chain's own sentence is carried whole inside it, as
#: `ASSEMBLY_EFFECTS_REFUSAL` carries one and for its reason.
SHOT_EFFECTS_COPY_UNCOMPOSABLE_REFUSAL = (
    "{shot}'s own effect stack cannot be composed, so it was not copied onto anything. {detail}"
)


#: The opening of `EFFECT_LUT_UNKNOWN_REFUSAL`, up to the id it names — derived from the constant
#: rather than typed again, so a reworded refusal carries this along with it instead of leaving a
#: literal here that matches nothing and a rescan that silently stops happening.
_UNKNOWN_LOOK_OPENING = EFFECT_LUT_UNKNOWN_REFUSAL.split("{lut!r}", 1)[0]


def _names_an_undiscovered_look(refusal: EffectRefusal) -> bool:
    """Whether this refusal is the one that a fresh look at the folder could answer differently.

    `EffectRefusal` carries a sentence and nothing else — it is `effects.py`'s to raise and this
    module does not get to add a code to it — so the sentence is what identifies it. Read against
    the constant's own opening, which is what keeps the two together.

    Only the *unknown id* refusal qualifies. Every other one — a number out of bounds, a
    misspelled key, an unnamed look — is a fact about the body, and re-reading the disk cannot
    change the answer. The file-missing refusal is not this one either: that id **was** discovered
    and the file has gone, so a rescan would turn a precise sentence into a vaguer one.
    """
    return str(refusal).startswith(_UNKNOWN_LOOK_OPENING)


#: Why an export refused a Shot's stack, with the chain's own sentence inside it.
#:
#: Two-layered on purpose. `effects.EffectRefusal` says what is wrong with the stack and never
#: which Shot carries it — it is a pure function of a stack and has no idea — while an export
#: refusal has to send the Director to one clip in a timeline of thirty. So the shot is named here
#: and the refusal's own sentence is carried whole rather than paraphrased: those sentences were
#: written to be read by a Director and are asserted verbatim in slice B's tests.
ASSEMBLY_EFFECTS_REFUSAL = "{shot}: {detail}"

#: Why an export refused a Shot's stored Transition, in `ASSEMBLY_EFFECTS_REFUSAL`'s own two-layer
#: shape and for its reason: `effects.transition_definition` says what is wrong with the type and
#: has no idea which Shot carries it. Kept as a second constant rather than reusing that one,
#: because the two name different things about a Shot and a Director reading a report needs to
#: know whether the look or the blend is the problem.
ASSEMBLY_TRANSITION_REFUSAL = "{shot}'s transition: {detail}"

#: How a Transition the plan **refused** is written into `ExportLook.transitions` (R-37, FX-25).
#:
#: The refusal sentence already names both Shots, so this prefixes rather than re-labels: the slot
#: is read as `"<shot_id>=<value>"` and a reader must be able to tell a blend that ran from one
#: that did not. The alternative -- listing only what composed -- makes a refused transition
#: indistinguishable from one nobody ever set, which is the silence R-37 exists against.
TRANSITION_REFUSED_RECORD = "refused: {shot}"

#: How a Shot that laid **no frames** is written into `ExportLook.omitted` (item 78,
#: 2026-08-31). It states the window the Director drew and the fact that nothing came of it,
#: because those are the two things they cannot see from the timeline: a clip is on screen at
#: a length they set, and the export used none of it.
#:
#: Seconds rather than frames, deliberately. The count is zero by definition and saying so
#: twice would be noise; what a Director can act on is the window, which is what they drag.
ASSEMBLY_OMITTED_RECORD = (
    "{shot} runs {start:.3f}s to {end:.3f}s but lays no frames on the assembly grid, so the "
    "export used none of its take. Lengthen it past a frame, or remove it."
)

#: How a **one-sided** Transition is written into `ExportLook.transitions` (story 11.4, FX-25).
#:
#: It keeps the slot's `"<shot_id>=<value>"` shape and spends the value saying the two things a
#: paired record does not have to: that this treated one clip's own final frames rather than
#: blending two, and **how many frames it ran for**. The length is the point. Story 11.4 asks that
#: a one-sided transition's length be *"bounded by the Shot's own duration and by nothing
#: invisible"*, and this is where an export answers it -- the number that actually ran, after the
#: clamp against the clip's own frames, in the record that outlives the Shot. Without it the two
#: records would be indistinguishable and the length would be visible nowhere at all.
TRANSITION_ONE_SIDED_RECORD = "{shot}={transition} one-sided over {frames} frames"

#: How the **opening** Transition is written into `ExportLook.transitions` (R-45, story 11.f8).
#:
#: `TRANSITION_ONE_SIDED_RECORD`'s shape and its reason, for the one boundary that is not a cut:
#: it keeps the slot's `"<shot_id>=<value>"` form and spends the value saying that this treated a
#: Shot's own **opening** frames and how many of them ran after the clamp. A separate word from
#: `one-sided` because the two are separate boundaries and the Shot that opens the plan can carry
#: both -- a record that called them one thing would leave a reader of a delivered export unable
#: to say which end of a Shot the number belonged to.
TRANSITION_OPENING_RECORD = "{shot}={transition} opening over {frames} frames"

#: How a Transition Pair that **disagrees across an Overlap** is written into the same slot
#: (story 11.3's third criterion, AD-30).
#:
#: **It records rather than refuses**, which is the whole of what AD-30 asks for: *"the outgoing
#: Shot's value is used and the divergence is reported once, so an editable manifest cannot
#: produce an undecidable export"*. The read path that makes it decidable already shipped -- the
#: export reads only `transition_out` -- and this is the half that stops it being silent.
#:
#: It names both types and says which one ran, because a Director who hand-edited a manifest, or
#: whose write landed halfway, needs to know which half of the pair the picture came from before
#: they can decide whether to keep it.
#:
#: **An unset mirror is not a divergence.** A Shot whose `transition_in` is simply absent is the
#: ordinary state of a one-sided transition, and reporting it would make this line fire on nearly
#: every export that carries a transition at all. See `_report_transition_divergence`.
#:
#: **And a `transition_in` that composed an opening is not one** (R-45, 2026-08-31). It is read at
#: the plan's first entry, where there is no earlier Shot and therefore no `transition_out` for it
#: to disagree with; this record needs both halves of a pair and there is no pair there.
TRANSITION_DIVERGED_RECORD = (
    "diverged: {before} sets {out} and {after} sets {incoming}. The export used {out}, which is "
    "the outgoing shot's own field."
)


#: Why a render — an export or a preview — refused a Shot whose look is driven by a measurement
#: that is not there any more. Story 10.4's third acceptance criterion, and the *only* half of
#: that story this slice ships: the bindings themselves are already retained by everything that
#: writes them, and the Director-facing half (an inert glyph with `[Analyze song]` beside it) is a
#: panel this slice does not build.
#:
#: **It refuses rather than rendering undriven, and the reason is that undriven is invisible.** A
#: `sendcmd` aimed at a target that is not in the graph is discarded at rc 0 with no warning and
#: byte-identical frames (R-25) — so an export that quietly dropped the drive would ship a picture
#: that never moved, succeed, and say nothing; and a preview that did it would be Story 9.7's
#: defect again, a clip that lies about the cut it is showing. Refusing is the only outcome that
#: has a symptom.
#:
#: **It carries the verdict's own sentence** — `SONG_ENVELOPE_NOT_TAKEN`,
#: `SONG_ENVELOPE_SONG_CHANGED`, `SONG_ENVELOPE_FILE_UNREADABLE`,
#: `SONG_ENVELOPE_RECORD_DISAGREES` or `SONG_ANALYSIS_MEDIA_MISSING`, whichever
#: `song_measurement_verdict` reached — rather than a wording of its own, because the difference
#: between "never measured" and "measured, then the song was replaced" is the whole of what tells
#: a Director what to do next, and this application already has one sentence for each.
#:
#: Both routes say it, whole and identical, for `SHOT_EFFECTS_LOCKED_REFUSAL`'s reason: a preview
#: is the export's promise, so the two must not refuse the same state in two wordings.
BINDING_WITHOUT_ENVELOPE_REFUSAL = (
    "{shot} has a Parameter Binding and no current song analysis to drive it, so nothing was "
    "rendered. {reason} Analyze the song again and the binding is live once more with its stored "
    "values intact — nothing about it has been dropped or zeroed."
)


# ------------------------------------------------------------------------------------------
# The export's pre-flight, as a registered list of checks (FX-24).
#
# What this replaces: two accumulators written inline in the assemble route, one per feature,
# each with its own list, its own loop and its own `raise`. That shape works and it does not
# scale — the binding case (Epic 10) and the transition case (Epic 11) are both coming, and
# each would have arrived as a third and a fourth loop edited into the middle of a 400-line
# route, with the report's ordering an accident of where somebody inserted their block.
#
# So the checks are named functions in an ordered tuple, and adding one is appending to the
# tuple. What has *not* changed is the promise the accumulators existed for: every reason an
# export cannot run comes back in one answer, because a Director fixing a fifteen-shot plan one
# refusal at a time is a Director being rationed. Nor have the sentences: every wording is the
# one that was already there, produced by the same code, and slices B and C1 assert them
# verbatim.
#
# **Two stages, because the plan is not free.** A check that needs only the clips runs before
# `assembly_plan` is laid out; one that needs the export's own geometry runs after. That split is
# real rather than tidiness: `assembly_plan` presumes clips that passed the first stage — a clip
# whose take is missing has `source=None` — so the geometry genuinely cannot exist until the
# window checks have agreed. A composition check may also *produce* what the export then runs,
# which is why it is handed an `ExportComposition` to fill in; a plan check gets no such thing
# and can only report.
# ------------------------------------------------------------------------------------------


def _unmeasured_song() -> SongMeasurement:
    """The verdict for a project nobody has asked about: no analysis, recorded as never taken.

    `ExportSubject.measurement`'s default. It is a function rather than a stored instance because
    `SongMeasurement` is declared further down this module — a default constructed here would
    force the class up, for a value every caller that matters overrides.
    """
    return SongMeasurement(False, SONG_ENVELOPE_NOT_TAKEN, recorded=False)


@dataclass(frozen=True, slots=True)
class ExportSubject:
    """Everything an export check may read about the export it is judging, and nothing it may
    write.

    Frozen, and the plan arrives by `dataclasses.replace` rather than by assignment, so a check
    cannot reach sideways and edit the thing the next check is about to judge. What a composition
    check produces goes into `ExportComposition` instead, which is the one mutable half.

    `looks` is the *callable* rather than the discovered listing, and that is load-bearing:
    `discovered_looks` reads and validates every `.cube` on the machine on its first call --
    221 ms cold on the Director's 48-file pack — and a project with no stack anywhere must go on
    reading the looks folder exactly never. A check calls it only once it knows it has something
    to judge.
    """

    #: One per Shot, in no particular order; every check that reports per clip sorts for itself.
    #:
    #: **Every Shot the manifest holds, including one the plan will bury.** `assembly_plan`
    #: resolves an overlap as layers — later on top — so a Shot another one completely covers
    #: contributes no frames and does not appear in `plan.clips` at all. It still appears here,
    #: and that is the plan stage's whole question: *can this project export*, asked of the
    #: manifest, before a plan exists to ask anything of. The field above says so — `plan` is
    #: `None` for every plan-stage check — so a check registered in `EXPORT_PLAN_CHECKS` could
    #: not consult the resolved clips if it wanted to.
    #:
    #: Measured 2026-08-28 on a buried Shot, because a rule nobody has run is a guess: **all
    #: four** plan-stage checks refuse over one — no approved take, a take missing from disk, a
    #: stack the catalogue refuses, a stack past the card limit, and the binding check beside
    #: them. `assembly_refusals` has behaved that way since long before any effect existed. See
    #: `_binding_envelope_refusals` for the ruling that keeps the fifth consistent with the four.
    clips: tuple[ClipWindow, ...]
    #: ffprobe's reading of the song that will play, never the stored field.
    song_seconds: float
    #: Shot id → the stack as `validate_stack` reads it, for the Shots that carry one. A Shot
    #: with an empty stack is absent, so `if not subject.stacks` is "this project has no look".
    stacks: Mapping[str, list[dict[str, Any]]]
    #: Every look this machine holds, on demand. See above for why it is not the listing itself.
    looks: Callable[..., Sequence[LutEntry]]
    #: The export's own geometry and per-clip frame counts — `None` for every plan-stage check,
    #: which runs before it can exist.
    plan: AssemblyPlan | None = None
    #: This project's Song Envelope verdict, **on demand**, for exactly `looks`' reason and at a
    #: larger cost: answering it hashes the whole master and reads a ~405 KB sidecar off disk
    #: (`song_measurement_verdict`), and an export whose Shots carry no Parameter Binding — which
    #: is every export in every project until one is bound — must never pay either. So the field
    #: is a callable, the route memoises it, and a check calls it only once it knows it has a
    #: binding to judge.
    #:
    #: Defaulted to the unmeasured verdict rather than to `None`, so a caller that constructs a
    #: subject without one (every existing test) reads "no analysis" instead of an attribute that
    #: has to be tested for before it can be asked.
    measurement: Callable[[], SongMeasurement] = _unmeasured_song
    #: Shot id -> the **stored** `transition_out.type` of the Shots that carry one, unresolved.
    #:
    #: The *outgoing* Shot's field and only it, which is AD-30: `transition_out` is authoritative
    #: for a paired transition and the later Shot's `transition_in` is a mirror the write path
    #: keeps in step. A manifest whose pair disagrees -- hand-edited, or a write that landed
    #: halfway -- therefore has a decidable export rather than an undefined one.
    #:
    #: Raw strings, not `TransitionDefinition`s, for `stacks`' reason: the plan-stage check below
    #: is what asks the catalogue, and a subject that had already resolved them could not report a
    #: type the catalogue refuses. A Shot with no transition is absent, so `if not
    #: subject.transitions` is "this project blends nothing".
    transitions: Mapping[str, str] = dataclass_field(default_factory=dict)
    #: Shot id -> the stored `transition_in.type` of the Shots that carry one: **the mirror, and
    #: at every boundary between two Shots it decides nothing.**
    #:
    #: AD-30 is unchanged by its presence -- `transition_out` on the earlier Shot is authoritative
    #: and the field above is the only one a boundary's picture is built from. This one is read by
    #: `_report_transition_divergence`, which compares the two so that a pair which disagrees is
    #: *said* rather than silently resolved.
    #:
    #: ~~Nothing else may read it, and a composer that did would be re-opening the question AD-30
    #: closed.~~ **Narrowed by R-45 on 2026-08-31, and amended here in the same commit as the
    #: composer that narrows it.** `_compose_opening_transition` reads this mapping at **exactly
    #: one** boundary: the first entry of the plan, where the Shot laying the video's first frame
    #: has no predecessor in song order. That is not re-opening AD-30 -- it is naming the one
    #: place AD-30 does not reach, because there is no outgoing field there to be authoritative
    #: *with*. Everywhere else a stored value in here composes nothing, which is what keeps one
    #: boundary to one treatment: AD-30's mirror writes this field on the neighbour whenever
    #: `transition_out` is set, so a composer that read it at any second boundary would be
    #: fading one Shot out and the next one in from a single gesture -- the picture `Fade through
    #: black` is already called, and the substitution FX-18 exists to forbid.
    #:
    #: Kept as a second mapping rather than folded into `transitions` because the two are read by
    #: different questions and one of them must not be answerable by the other: a single mapping
    #: of pairs would let a check reach the mirror by accident, which is the shape AD-30 exists to
    #: make impossible.
    transitions_in: Mapping[str, str] = dataclass_field(default_factory=dict)


@dataclass(slots=True)
class ExportComposition:
    """What the composition stage builds for the export to run, filled in by its checks.

    A composition check both reports and produces: `build_effect_stages` is the only thing that
    can see a look whose `.cube` has left the folder, and it is also what the trim is driven with,
    so the refusal and the artifact come out of one pass. Epic 11's composed transitions land
    beside `effect_stages` here.

    Empty is a complete answer: an export whose Shots carry no look composes nothing, and
    `trim_args` then receives the empty groups it has always defaulted to.
    """

    #: **The clip's index in `plan.clips`** → its composed chain, for the clips that have one.
    #: Absent means no stages, which is what `EffectStages()` at the call site turns into "the
    #: argv this route always built".
    #:
    #: Keyed by clip and not by Shot, which it was until story 9.7. A Shot with a later one
    #: nested inside it resolves into two clips, and the whole point of that story is that the
    #: two do **not** get the same filter text: the second one's stages carry where it begins
    #: inside its Shot, so a shake does not snap back to phase zero and grain does not run the
    #: same noise twice. The refusals and the provenance below are still judged once per Shot —
    #: they are facts about the stack, and a Director told the same sentence twice is worse off.
    effect_stages: dict[int, EffectStages] = dataclass_field(default_factory=dict)
    #: **The transition entry's index in `plan.clips`** -> its two legs' composed chains, in the
    #: order they play: the outgoing Shot's, then the incoming one's. Epic 11's slot, and the one
    #: this class's docstring reserved.
    #:
    #: Two entries and not one, because a transition segment is two full effect chains inside one
    #: `-filter_complex` and each is composed against its **own** Shot's stack -- FX-NFR-3 says the
    #: preview is the export's own chain, and a blend of two *ungraded* takes would not match the
    #: clips on either side of it (R-41). They are composed with a leg prefix for the same ruling:
    #: both legs start at chain slot 0, so two graded Shots would emit duplicate filtergraph labels
    #: and two *bound* Shots would emit one `sendcmd` target driving both legs, at rc 0.
    transition_stages: dict[int, tuple[EffectStages, EffectStages]] = dataclass_field(
        default_factory=dict
    )
    #: The provenance record of the same composition — what goes onto `RenderJob.look` (FX-25).
    #: Built here rather than at the job write so it cannot describe a different composition from
    #: the one the export is about to run.
    look: ExportLook = dataclass_field(default_factory=ExportLook)


def _window_refusals(subject: ExportSubject) -> list[str]:
    """Every reason the *plan* cannot assemble: approval, staleness, the take on disk, the trim
    offsets, and the tiling of the song. `assembly.assembly_refusals` unchanged, registered."""
    return assembly_refusals(list(subject.clips), subject.song_seconds)


def _effect_stack_refusals(subject: ExportSubject) -> list[str]:
    """Every Shot whose Effect Stack the catalogue will not agree to, named by its label.

    Judged here rather than only at composition because the catalogue's verdict needs no
    geometry, so it can join the same answer as everything else wrong with the plan — a Director
    with an unapproved shot *and* two impossible stacks is told all three at once, where a stack
    judged only at composition would stay silent behind the approval refusal and be discovered one
    run later. The composition check below re-derives the verdict; `validate_stack` is pure and
    costs nothing to run twice, and the pair is what keeps the answer honest at both moments.

    **AD-21:** nothing stored says a stack is valid. The write said so at the time, and this asks
    again, because a manifest is hand-editable and the catalogue's bounds are not stored beside it.
    """
    if not subject.stacks:
        return []
    luts = subject.looks()
    refusals: list[str] = []
    for clip in sorted(subject.clips, key=lambda item: item.start):
        stack = subject.stacks.get(clip.shot_id)
        if not stack:
            continue
        try:
            validate_stack(stack, luts=luts)
        except EffectRefusal as refusal:
            refusals.append(
                ASSEMBLY_EFFECTS_REFUSAL.format(shot=clip.label, detail=refusal)
            )
    return refusals


def _compose_effect_chains(
    subject: ExportSubject, composition: ExportComposition
) -> list[str]:
    """Compose each Shot's chain against the export's delivery geometry, and record what it was.

    The geometry is the *export's* target and not the take's, which is what a treatment stage is
    composed for (`effects.StageContext`). Run before the job record is written, so a look whose
    `.cube` has gone missing since it was chosen refuses the export with nothing half-started
    behind it rather than failing inside ffmpeg with a message about `clut`.

    **Accumulated, not raised on the first fault**, and this loop is the only place a missing
    *file* can be seen — `validate_stack` checks ids against the discovered listing and never
    touches the disk. Two shots whose `.cube` files had both been deleted used to name only the
    first: restore it, run again, be told about the second.

    **Composed once per clip, reported once per Shot.** A Shot with a later one nested inside it
    resolves into two `ClipWindow`s carrying one shot id, and story 9.7 is the story of those two
    not being handed identical filter text: each is composed against *where it begins inside its
    Shot*, so a time-dependent stage carries on across the seam instead of restarting. What is
    wrong with a stack, though, is wrong with it once — the catalogue's verdict has nothing to do
    with which clip is being cut — so a refusal is said once and the provenance is recorded once.

    **Where a clip sits inside its Shot is read off the `ClipWindow` itself**, and it survives
    the split for free: `assembly_plan` resolves an overlap with `replace(clip, ...)`, which
    moves `start`, `duration` and `offset` and leaves the *approved snapshot* alone. So
    `start - approved_start` is the seconds from the Shot's first frame to this clip's first
    frame, and `approved_duration` is the Shot's whole window — for every survivor of the split,
    from the same two fields. They are the snapshot rather than the live window on purpose: the
    live window is what the split just moved, and the staleness check that ran before this
    (`EXPORT_PLAN_CHECKS`, and the route raises on its report) has already refused every Shot
    whose snapshot and window disagree, so the snapshot is the Shot's window here by proof.

    Neither number reaches `assembly.trim_args`, which goes on receiving two lists of finished
    strings and knowing nothing about the catalogue. They go to `effects.build_effect_stages`,
    from the plan, in this route — which is the only place that has both.
    """
    plan = subject.plan
    if plan is None or not subject.stacks:
        return []
    luts = subject.looks()
    # The envelope, read once for the whole export and only when something is bound. The plan
    # stage has already refused every Shot whose binding has no current measurement
    # (`_binding_envelope_refusals`, and the route raises on its report), so `None` here is
    # unreachable from a bound Shot — and `build_effect_stages` refuses by name rather than
    # composing an inert `sendcmd` if that argument ever stops being true.
    envelope = subject.measurement().envelope if _bound_shot_ids(subject) else None
    refusals: list[str] = []
    # Two sets and not one, because they answer different questions about the same Shot.
    # `reported` is "this Shot's refusal has been said"; `recorded` is "this Shot's look is in
    # the provenance". Conflating them silences a refusal raised on a Shot's *second* clip
    # after its first composed cleanly — a `.cube` deleted between the two, which is a
    # microsecond-wide race and exactly the shape that ships an export missing an effect
    # nobody was told about (FX-24: never silently dropped).
    reported: set[str] = set()
    recorded: set[str] = set()
    for index, clip in enumerate(plan.clips):
        # A transition entry is not one clip's chain and is composed by `_compose_transitions`,
        # which needs a leg prefix this call has no notion of (R-41). Skipped here rather than
        # handled here, so that the two composers cannot come to different answers about one
        # segment — and so that this function goes on being about "one clip, one chain".
        if isinstance(clip, TransitionClip):
            continue
        stack = subject.stacks.get(clip.shot_id)
        if not stack:
            continue
        try:
            composition.effect_stages[index] = build_effect_stages(
                stack,
                width=plan.width,
                height=plan.height,
                luts=luts,
                clip_offset=clip.start - clip.approved_start,
                shot_seconds=clip.approved_duration,
                # What a binding needs, and the one piece of arithmetic this epic adds. The
                # drive's clock is the **song's** and the filter graph's is the **clip's**:
                # `trim_args` prepends `setpts=PTS-STARTPTS` to every clip cut at an offset, so
                # ffmpeg's `t` is zero at the first frame of each. `shot_start` is the Shot's own
                # start in the song and `clip_offset` above is the seconds from the Shot's first
                # frame to this clip's, so `build_effect_stages` adds them and gets `clip.start`
                # — the song second this clip's first frame lands on — without either number
                # meaning anything new. A Shot that was never split adds zero.
                #
                # `clip_seconds` is the frames ffmpeg is actually asked for over the grid rate,
                # not `clip.duration`: the script must not compile a command past the last frame
                # the trim will write, and `plan.frames` is the number that decides that.
                envelope=envelope,
                shot_start=clip.approved_start,
                clip_seconds=plan.frames[index] / ASSEMBLY_FPS,
            )
        except EffectRefusal as refusal:
            if clip.shot_id not in reported:
                reported.add(clip.shot_id)
                refusals.append(
                    ASSEMBLY_EFFECTS_REFUSAL.format(shot=clip.label, detail=refusal)
                )
            continue
        if clip.shot_id in recorded:
            continue
        recorded.add(clip.shot_id)
        composition.look.effects.extend(
            # The plan's own geometry, because one composer's identity is a function of it:
            # `chroma_split` turns a stored *fraction* into pixels, so whether it composes
            # anything at all depends on how wide the delivery is. `exported_look` defaults to a
            # deliberately huge probe when it is told nothing, which errs toward recording an
            # effect that did not run; the export knows the real width one line above, so it says
            # so and the record is exact rather than conservative.
            f"{clip.shot_id}={entry}"
            for entry in exported_look(stack, luts=luts, width=plan.width, height=plan.height)
        )
        # FX-25's other reserved slot, filled by the epic that reserved it. What is recorded is
        # the **agreed** binding — every setting filled in by the catalogue, formatted by the same
        # `_canonical` the effects list uses — so a record read six months from now says what the
        # export was actually driven by rather than what the sparse manifest happened to spell.
        # Once per Shot, like the effects beside it: a binding is a fact about the card and not
        # about which clip of a split Shot is being cut.
        composition.look.bindings.extend(
            f"{clip.shot_id}={entry}" for entry in exported_bindings(stack, luts=luts)
        )
    return refusals


#: Every check the export runs before its plan exists, in the order their sentences are reported.
#: The window checks first, so the report reads top to bottom the way the timeline does, and the
#: look after them.
#:
def _oversized_stack_refusals(subject: ExportSubject) -> list[str]:
    """Every Shot carrying more effects than one command line can hold, named by its label.

    The cap is enforced at all **three** write doors, each before it validates:
    `replace_shot_effects`, `_adopt_shot_effects` on a stack arriving on a Shot the store does not
    hold, and `copy_shot_effects` on the source stack it is about to multiply. That is three
    guards over four routes — `_adopt_shot_effects` is what `PUT /api/projects/{id}` and
    `PUT .../shots` both go through — and the third of them was missing until 2026-08-26, which is
    how a hand-edited 985-card Shot could answer 200 through `POST .../effects/copy` and 422
    through `PUT .../effects` on the identical stack. This docstring said "both write doors… so no
    client reaches this" for the whole of that window, which is worse than the hole: a false
    invariant is one the next reader builds on.

    So no client reaches this. A **manifest edited by hand** does, and so does one written before
    any of the caps existed, and the failure it produces is the least useful in the application:
    the
    chain becomes one `-vf` argument, Windows refuses a command line past 32,767 characters, and
    the `FileNotFoundError` that comes back used to be reported as a missing ffmpeg. Measured
    2026-08-25: 985 grain cards build 32,725 characters and export, 1,200 build 40,060 and do not.

    Registered here rather than checked at composition because it is a fact about the stack alone
    — it needs no geometry — so it joins the one report every other plan fault joins, and a
    Director with two oversized Shots is told about both at once.

    The bound is the same constant the write routes use. A cap that lived at two of three doors
    would be the shape this project has now counted twelve times — and it did, for four days, at
    the copy route. Said once per Shot, like its sibling: a Shot with a later one nested inside it
    resolves into two clips carrying one id, and an oversized stack is oversized once.
    """
    refusals: list[str] = []
    seen: set[str] = set()
    for clip in sorted(subject.clips, key=lambda item: item.start):
        stack = subject.stacks.get(clip.shot_id)
        if not stack or len(stack) <= SHOT_EFFECT_STACK_LIMIT or clip.shot_id in seen:
            continue
        seen.add(clip.shot_id)
        refusals.append(
            ASSEMBLY_EFFECTS_REFUSAL.format(
                shot=clip.label,
                detail=SHOT_EFFECTS_TOO_MANY_REFUSAL.format(
                    limit=SHOT_EFFECT_STACK_LIMIT, count=len(stack)
                ),
            )
        )
    return refusals


def stack_is_driven(stack: Sequence[Mapping[str, Any]]) -> bool:
    """Whether this stack will ask the music for anything: an **enabled** card with a binding.

    The one question that decides whether a render needs the Song Envelope at all, and it is
    asked before the envelope is read because reading one costs a SHA-256 of the whole master and
    a ~405 KB sidecar. Every project answers `False` until a binding exists.

    Read off the **stored** spec rather than off `validate_stack`, and truthily: a `bindings`
    value the catalogue will not agree to is `_effect_stack_refusals`' business, said once, in the
    catalogue's own words. What this must not do is answer `False` for a stack it could not read
    and thereby let a bound Shot past the check that needs it.

    A **disabled** card composes no stage, so nothing addressed it and nothing was driven — the
    rule `exported_look` and `exported_bindings` already apply to the record, applied to the
    question. Without it, a Shot whose bound card the Director had switched off would refuse its
    own export over a measurement that could not have reached the picture.
    """
    return any(
        spec.get("bindings") and spec.get("enabled", True) is not False for spec in stack
    )


def _bound_shot_ids(subject: ExportSubject) -> set[str]:
    """Every Shot id in this export whose look is driven by the music. See `stack_is_driven`."""
    return {
        shot_id for shot_id, stack in subject.stacks.items() if stack_is_driven(stack)
    }


def _binding_envelope_refusals(subject: ExportSubject) -> list[str]:
    """Every Shot whose look is driven by a measurement this project no longer has.

    Story 10.4's export criterion, registered rather than written into the route so it joins the
    one report everything else joins: a Director with an unapproved shot *and* a replaced song is
    told both at once.

    **The envelope is read at most once, and only when something is bound.** The verdict costs a
    SHA-256 of the whole master plus a ~405 KB sidecar read, so the set above is computed first
    and this returns before touching either when it is empty — which it is for every project until
    a binding exists.

    Said once per Shot, like both of its siblings: a Shot another nests inside is two clips here
    and one missing analysis, and a Director told the same sentence twice is worse off.

    **A Shot that renders no frame is still refused over, decided 2026-08-28 and recorded because
    it was a judgement rather than an oversight.** A Shot completely covered by a later one is in
    `subject.clips` and not in `plan.clips`, so this refuses an export the buried Shot could not
    have changed. Three things settle it. The first is that this check *cannot* see the plan —
    `ExportSubject.plan` is `None` at the plan stage, by construction — so "skip it" would mean
    moving this to the composition stage, where the two remaining plan checks about a stack would
    still disagree with it. The second is measured: with a Shot buried, an unapproved take, a
    take missing from disk, a stack the catalogue refuses and an oversized stack **each already
    refuse the export by that Shot's name**, and the oldest of those is `assembly_refusals`,
    which predates every effect in this application. Skipping here would make one of five checks
    answer a different question about the same Shot. The third is the sentence itself: its remedy
    is *analyse the song again*, one gesture that clears every bound Shot in the project at once,
    not *unbind this one* — so a Director is never held by a Shot they cannot see.

    The mirror is what would be worse. An export that ignores a buried Shot succeeds today and
    refuses tomorrow, when the Director drags the covering Shot aside and unburies a binding they
    never touched, for a song they replaced weeks ago.

    `_compose_effect_chains` iterates `plan.clips` instead, and that is not the same rule read
    two ways: it does not judge, it *builds* — one composed chain per plan index, and a record of
    the look that actually ran. A Shot contributing no frames must contribute no chain and no
    provenance entry, which is the same principle as `ExportLook`'s "only the effects that
    actually composed a stage are listed".
    """
    bound = _bound_shot_ids(subject)
    if not bound:
        return []
    verdict = subject.measurement()
    if verdict.current:
        return []
    refusals: list[str] = []
    seen: set[str] = set()
    for clip in sorted(subject.clips, key=lambda item: item.start):
        if clip.shot_id not in bound or clip.shot_id in seen:
            continue
        seen.add(clip.shot_id)
        refusals.append(
            BINDING_WITHOUT_ENVELOPE_REFUSAL.format(shot=clip.label, reason=verdict.reason)
        )
    return refusals


def _transition_catalogue_refusals(subject: ExportSubject) -> list[str]:
    """Every Shot whose stored Transition the catalogue will not agree to, named by its label.

    `_effect_stack_refusals`' rule applied to the other catalogue, and registered beside it for
    the same reason: the verdict needs no geometry, so it joins the one answer everything else
    joins -- a Director with an unapproved shot *and* a transition type this build no longer
    ships is told both at once.

    **AD-21:** nothing stored says a transition is valid. The route said so at the time; this asks
    again, because a manifest is hand-editable and the catalogue is not stored beside it.

    **This one does refuse the export, and R-37's geometry refusal does not.** The two are not the
    same question read two ways. An unknown type is a stored *value* the catalogue rejects -- the
    same class as an unknown effect id, which has refused the export since Epic 9 -- and there is
    no picture that could be rendered from it: `transition_definition` is the only thing that
    turns a type into an `xfade` name, so a plan holding one cannot be built at all. R-37 is about
    *geometry*, where a perfectly good hard cut is already available and refusing the whole export
    would be stricter than `assembly_plan` itself. See `assembly.TRANSITION_CROWDED_REFUSAL`.

    **The first Shot's *incoming* field is asked too, and only that one** (R-45, story 11.f8).
    `_compose_opening_transition` builds a picture from `transition_in` at the plan's first entry,
    so a stored value the catalogue cannot name is the same fault there as anywhere else and gets
    the same sentence -- and asking it here is what keeps that composer's own claim true, that
    every type reaching it has already been agreed to. Asked of the first Shot **in song order**,
    which is a superset of the Shots that can open a plan: the opening entry's Shot is always that
    one, and `assembly_plan` is not built yet at this stage to narrow it further. Every other
    Shot's `transition_in` composes nothing, so a value stored there is not something this export
    builds from and is not refused over -- it is the ordinary mirror AD-30 writes.
    """
    if not subject.transitions and not subject.transitions_in:
        return []
    refusals: list[str] = []
    seen: set[tuple[str, str]] = set()
    for position, clip in enumerate(sorted(subject.clips, key=lambda item: item.start)):
        for stored in (
            subject.transitions.get(clip.shot_id),
            subject.transitions_in.get(clip.shot_id) if position == 0 else None,
        ):
            if stored is None or (stored, clip.shot_id) in seen:
                continue
            seen.add((stored, clip.shot_id))
            try:
                transition_definition(stored)
            except EffectRefusal as refusal:
                refusals.append(
                    ASSEMBLY_TRANSITION_REFUSAL.format(shot=clip.label, detail=refusal)
                )
    return refusals


def _compose_transitions(
    subject: ExportSubject, composition: ExportComposition
) -> list[str]:
    """Compose both legs of every transition segment, and record what the export blended.

    Registered in `EXPORT_COMPOSITION_CHECKS` because it needs the plan: which boundaries actually
    became `TransitionClip`s is `assembly_plan`'s answer, and the delivery geometry a leg is
    composed against is the plan's too.

    **Each leg gets a leg prefix** (R-41). `effects.build_effect_stages` names a branch's links
    `fx{slot}a` and a bound filter's instance `b{slot}`, where `slot` is the position in *that
    Shot's own* chain -- and both legs of a segment start at slot 0. Without the prefix two graded
    Shots emit `[fx0a]` twice in one `-filter_complex`, which is at least loud, and two *bound*
    Shots emit one `sendcmd` target addressing the filters of both legs, which is silent at rc 0
    and is the class `DriveScript.target`'s docstring says nothing else can catch.

    **A leg is composed against its own Shot's whole window, never the Overlap's slice**, and the
    two numbers say so: `clip_offset` is the seconds from the Shot's first frame to this leg's
    first frame -- the Overlap's start minus the Shot's own start -- and `shot_seconds` is the
    Shot's whole window. That is `_compose_effect_chains`' rule exactly, and it is what keeps a
    time-dependent stage carrying on across the seam instead of restarting inside the blend: the
    outgoing Shot's grain does not re-seed at the dissolve, and its ramp does not jump back.

    **The look record is written here and nowhere else for a transition** (FX-25). Every entry is
    `"<outgoing shot id>=<value>"`, the shape `effects` and `bindings` already use, so all three
    slots of `ExportLook` read alike. A transition the plan **refused** is listed too, carrying
    `assembly.TRANSITION_CROWDED_REFUSAL` whole -- that is the only place saying a boundary the
    manifest asked to blend stayed a hard cut (R-37), and a record that listed only the successes
    would make a refused transition indistinguishable from one nobody set.

    Refusals from *this* function are the catalogue's, accumulated rather than raised on the first
    fault, for `_compose_effect_chains`' reason -- and a Shot whose stack has already been refused
    there is refused there, once, in the catalogue's own words.
    """
    plan = subject.plan
    if plan is None:
        return []
    composition.look.transitions.extend(
        TRANSITION_REFUSED_RECORD.format(shot=item.sentence)
        for item in plan.transition_refusals
    )
    refusals: list[str] = []
    luts_read: list[Sequence[LutEntry]] = []

    def luts() -> Sequence[LutEntry]:
        """The looks folder, read at most once and only if a leg actually names one."""
        if not luts_read:
            luts_read.append(subject.looks())
        return luts_read[0]

    envelope = subject.measurement().envelope if _bound_shot_ids(subject) else None
    for index, entry in enumerate(plan.clips):
        if not isinstance(entry, TransitionClip):
            continue
        composed: list[EffectStages] = []
        for leg, clip in (("A", entry.before), ("B", entry.after)):
            stack = subject.stacks.get(clip.shot_id)
            if not stack:
                composed.append(EffectStages())
                continue
            try:
                composed.append(
                    build_effect_stages(
                        stack,
                        width=plan.width,
                        height=plan.height,
                        luts=luts(),
                        clip_offset=clip.start - clip.approved_start,
                        shot_seconds=clip.approved_duration,
                        envelope=envelope,
                        shot_start=clip.approved_start,
                        clip_seconds=plan.frames[index] / ASSEMBLY_FPS,
                        leg=leg,
                    )
                )
            except EffectRefusal as refusal:
                refusals.append(
                    ASSEMBLY_EFFECTS_REFUSAL.format(shot=clip.label, detail=refusal)
                )
                composed.append(EffectStages())
        if len(composed) == 2:
            composition.transition_stages[index] = (composed[0], composed[1])
        composition.look.transitions.append(
            f"{entry.before.shot_id}={entry.choice.transition_id}"
        )
    return refusals


def _boundary_is_overlapped(
    ordered: Sequence[ClipWindow | Shot], position: int
) -> bool:
    """Whether the Shot at `position` overlaps the one that follows it in song order.

    **The one predicate that separates story 11.4's work from story 11.1's**, and it is deliberately
    the same arithmetic in the same three places: `assembly._paired_transitions` decides a blend by
    it, `routes/shots.replace_shot_transitions` refuses a pair-only type by it at the write, and
    this decides a one-sided treatment by its negation. `BOUNDARY_TOLERANCE_SECONDS` is applied for
    `tiling_refusals`' reason -- below half a frame an "overlap" is one boundary written twice.

    **`ClipWindow` or `Shot`, because it reads only `start` and `end` and both carry the same
    two.** The preview route asks this question of `project.shots` and the export asks it of
    `subject.clips` -- and those are the same set, because `assemble_project` builds one
    `ClipWindow` per Shot whether or not its take resolves. Two callers, one arithmetic, and the
    widened annotation is what says so rather than a comment claiming it.

    Read off the **Shot windows** rather than off `plan.clips`, and that is what makes it right in
    the case that is easy to get wrong. A pair the plan *refused* -- three clips over one instant,
    or an incoming Shot swallowed whole (R-37) -- leaves no `TransitionClip` behind, so a rule that
    looked for one would treat those boundaries one-sided and quietly apply a fade where the
    Director asked for a blend and was already told it could not happen. Here they are overlapped,
    so they are the pair path's business and are reported once, by it.
    """
    if position + 1 >= len(ordered):
        return False
    return (
        ordered[position].end - ordered[position + 1].start > BOUNDARY_TOLERANCE_SECONDS
    )


def _compose_one_sided_transitions(
    subject: ExportSubject, composition: ExportComposition
) -> list[str]:
    """Every Transition with no Overlap under it, as stages on that Shot's own last clip.

    Story 11.4 and AD-19: *"A transition-out with no Overlap is one-sided: a filter applied to the
    tail of that clip's own intermediate, single-input, no `xfade`, no change to frame count, no
    frames taken from a neighbour."* All four of those are structural here rather than promised.
    The stages are appended to the chain `_compose_effect_chains` already composed for this clip,
    the argv is `trim_args`' own, and **`assembly_plan` is not touched at all** -- which is why
    this could be built beside the divergence report without either going near the frame grid.

    **It rides `treatment_stages`, after the Shot's whole look.** A transition treats the finished
    picture: a Director who graded a Shot and then faded it out expects the graded picture to fade,
    not the ungraded one to fade and then be graded back up. The `sendcmd` a blur ramp needs goes
    on the end of `geometry` instead, which keeps it upstream of every filter it drives and leaves
    `effects.BRANCH_FRAME_GUARD` -- whose whole job is to be the first stage in the chain -- where
    it was.

    **The one thing it reads from the plan is `plan.frames[index]`**, the frames ffmpeg will
    actually write for this clip, and it reads it to clamp the treatment's length. Seconds would
    not do: a `start_frame` past the last frame written is a treatment that composes cleanly,
    renders at rc 0 and changes nothing, which is this pipeline's own recurring failure.

    **A Shot that another nests inside is two clips, and only its last one is treated.** The
    transition is a fact about the Shot's *final* frames, so `index` is the entry with the greatest
    end among that Shot's clips -- the same reasoning `_compose_effect_chains` applies from the
    other side when it composes each clip against where it begins inside its Shot.

    **A pair-only type reaching here is refused with the sentence the write route already says**
    (FX-19, R-34), and nothing is substituted. It is reachable without a hand-edited manifest:
    FX-16 and R-36 keep a stored type when a Director drags the two clips apart, so a "Wipe left"
    authored across an Overlap becomes a wipe with nothing to wipe onto. Recorded rather than
    raised, for R-37's reason -- the boundary is a perfectly good hard cut and refusing the export
    over it would cost a Director a render over one geometry.

    **The catalogue cannot refuse a type here**, which is why nothing below catches an
    `EffectRefusal`: `_transition_catalogue_refusals` is a *plan*-stage check and the route raises
    on its report, so every stored type reaching this function has already been agreed to. Same
    proof, same shape, as the resolution `assembly_plan` is handed.

    **`transition_in` is not read here, and that stayed true when the opening half shipped.**
    AD-30 makes it the mirror and `subject.transitions` carries only the outgoing field, so every
    boundary this function walks is decided by the outgoing Shot -- which is R-45's rule and the
    whole of why one cut cannot collect two treatments. ~~A one-sided *fade in* on the very first
    Shot -- the one boundary where an incoming field has no pair to mirror -- is described by no
    acceptance criterion in Epic 11 and would need that mapping widened.~~ **Ruled by R-45 and
    shipped 2026-08-31 by story 11.f8**, in `_compose_opening_transition` beside this one rather
    than by widening this walk: that boundary is not a cut between two Shots, it is the plan's
    first frame, and the two are separated here so that neither can reach the other's.

    ~~And the **preview** does not show this: `preview_fingerprint`'s seventh input is still
    hashed empty, which R-35 reserved for exactly *"a transition on a Shot's own preview"* and
    gave to story 11.5. Until it lands, a one-sided transition is the one thing in this
    application an export does that a preview does not -- a real gap in FX-NFR-3.~~ **Closed
    2026-08-29 by story 11.5.** `render_shot_preview` composes `one_sided_transition_stages` from
    the same catalogue with the same clamp, splices it onto the same two groups, and fills the
    seventh fingerprint slot with what it composed -- so the treatment moves the clip's name and
    an untreated Shot's name does not move at all. The one difference is deliberate and is the
    preview's own geometry: the blur's sigma is a count of pixels and is scaled to the half-size
    grid, which is `StageContext.reference_width`'s rule reached through `effects.pixel_scale`.
    """
    plan = subject.plan
    if plan is None or not subject.transitions:
        return []
    ordered = sorted(subject.clips, key=lambda item: item.start)
    for position, clip in enumerate(ordered):
        stored = subject.transitions.get(clip.shot_id)
        if stored is None or _boundary_is_overlapped(ordered, position):
            continue
        index = _final_clip_index(plan, clip.shot_id)
        if index is None:
            continue
        composed = one_sided_transition_stages(
            stored, clip_frames=plan.frames[index], fps=ASSEMBLY_FPS
        )
        if composed is None:
            entry = TRANSITION_CATALOGUE[stored]
            composition.look.transitions.append(
                TRANSITION_REFUSED_RECORD.format(
                    shot=TRANSITION_PAIR_ONLY_REFUSAL.format(
                        label=entry.label,
                        shot=clip.label,
                        # Always the seam after: this composer reads
                        # `subject.transitions`, which is `transition_out` alone (AD-30).
                        neighbour="after",
                        alternatives=", ".join(
                            sorted(
                                item.label
                                for item in TRANSITION_CATALOGUE.values()
                                if not item.pair_only
                            )
                        ),
                    )
                )
            )
            continue
        already = composition.effect_stages.get(index, EffectStages())
        composition.effect_stages[index] = EffectStages(
            geometry=(*already.geometry, *composed.geometry),
            treatment=(*already.treatment, *composed.treatment),
            scripts=(*already.scripts, *composed.scripts),
        )
        composition.look.transitions.append(
            TRANSITION_ONE_SIDED_RECORD.format(
                shot=clip.shot_id, transition=stored, frames=composed.frames
            )
        )
    return []


def _final_clip_index(plan: AssemblyPlan, shot_id: str) -> int | None:
    """Where one Shot's **last** clip sits in `plan.clips`, or `None` if it has none.

    `None` is reachable and is not a fault: a Shot lying wholly under later ones contributes no
    visible range at all (`assembly_plan`'s resolution loop drops it), and a transition out of a
    picture nobody sees has nothing to treat. Silent because there is nothing to say -- the Shot
    itself is already absent from the export, which is a decision made and reported elsewhere.

    A `TransitionClip` is never the answer: it is two Shots' frames and is composed by
    `_compose_transitions` with its own leg namespace. Skipping it here is what keeps the two
    composers from writing into one entry.

    **For a one-sided Shot the answer is provably a single entry today, and the `max` is written
    anyway.** A Shot resolves into more than one clip only where a later-starting Shot overlaps
    its interior -- and if *any* later Shot overlaps it, so does its **immediate successor in song
    order**, because that successor has the smallest start among the later Shots and therefore
    also starts before this Shot ends. `_boundary_is_overlapped` has already sent every such Shot
    down the pair path, so what arrives here is unsplit. The `max` costs nothing, says what the
    answer *means* -- the Shot's final frames -- and is the line that stays correct if that
    predicate is ever widened.
    """
    spots = [
        spot
        for spot, entry in enumerate(plan.clips)
        if isinstance(entry, ClipWindow) and entry.shot_id == shot_id
    ]
    if not spots:
        return None
    return max(spots, key=lambda spot: plan.clips[spot].end)


def _opening_clip_frames(ordered: Sequence[ClipWindow | Shot]) -> int:
    """How many frames the plan opens with, when the first Shot in song order is what lays them.

    `0` means nothing may be treated there, which is a state and not a failure. A positive answer
    is both facts at once: that this Shot opens the video, and how many frames of it the export
    will write before anything else -- which is the clamp an opening treatment is bounded by.
    Answering the two together is deliberate: a caller that asked *whether* and then computed
    *how long* from the Shot's own window would name a treatment longer than the frames that
    exist, which is the untreated-picture-at-rc-0 failure this whole family is written against.

    R-45 composes an opening treatment *"on the first Shot of the plan in song order, where there
    is no predecessor and nothing owns the cut"*, and those are two conditions rather than one.
    They part on a geometry the export meets: a Shot laid over another one's head. `A` at
    `[0, 10]` under `B` at `[0.01, 4.01]` resolves to `B[0.01, 4.01]` and then `A[4.01, 10]`,
    because `assembly_plan` cuts an overlaid head at the later Shot's start and discards what is
    left of it when that is no longer than `BOUNDARY_TOLERANCE_SECONDS`. `A` is first by `start`;
    `B` lays the first frame. Executed, not reasoned:
    `test_the_shot_that_lays_the_first_frame_is_not_always_the_first_shot_by_start` builds it.

    **Neither Shot may be treated there, and that is what this predicate says.** `A`'s own opening
    frames are not in the export at all, so treating its first *clip* would treat frames four
    seconds into the video -- at a cut `B`'s `transition_out` already owns, which is exactly the
    two-treatments-for-one-boundary this slice exists to make impossible. And `B` has a
    predecessor: R-45's own clause excludes it, AD-30's mirror wrote `B.transition_in` the moment
    a Director set anything on `A`, and composing there would make one gesture fade `A` out and
    `B` in -- the picture `Fade through black` is already called.

    So the opening treatment composes exactly where the two definitions **agree**, which is what
    this returns. The export does not ask this question: it reads `plan.clips[0]` and compares the
    Shot, which is the resolution's own answer rather than a description of it
    (`_opening_clip_index`). This is the port for the two places that have no plan to read -- the
    Shot preview, and `api.openingClipFrames` in the browser -- and
    `test_the_window_rule_and_the_plan_agree_about_what_opens` sweeps all three against each
    other over the geometries that separate them, comparing the **number** and not only the
    verdict: two engines agreeing on `0` for different reasons is two engines. **Where they
    disagree the plan wins**, exactly as `boundaryBlendVerdicts` is subordinate to
    `_paired_transitions`.

    **The grid is asked as well as the seconds**, and it is not decoration. A head longer than
    half a frame can still round to *no* frames -- `round(end * 24) == round(start * 24)` holds
    across a span of up to 0.98 of a frame -- and `assembly_plan` drops a zero-frame entry, so the
    seconds question alone answers `True` for a Shot the plan does not open with. It is the same
    correction the split rule was given on 2026-08-30: ask the grid the question the grid decides.
    """
    if not ordered:
        return 0
    first = ordered[0]
    # What cuts the head, if anything does: the resolution loop subtracts every later-*starting*
    # window, and `ordered` is in song order, so the earliest of them is the only one that can
    # move this edge. A later Shot starting at or before this one within half a frame removes the
    # head whole, which this reads as an edge no longer than the tolerance.
    edge = first.end
    if len(ordered) > 1 and ordered[1].start < first.end:
        edge = ordered[1].start
    if edge - first.start <= BOUNDARY_TOLERANCE_SECONDS:
        return 0
    return max(0, clip_frames_on_grid(first.start, edge))


def _opening_clip_index(subject: ExportSubject) -> int | None:
    """Where the plan's opening treatment goes, or `None` when nothing may be treated there.

    **The plan's own answer, not a reading of the windows.** `plan.clips[0]` is the entry that
    lays the video's first frame -- `assembly_plan` sorts its resolved entries by `start` and
    `_paired_transitions` only ever splices a blend *behind* the outgoing Shot's own surviving
    frames, which its `outgoing > 0` term guarantees -- so the answer is one index rather than a
    search. It is checked for its type anyway; see below.

    **And the Shot that entry belongs to must be the first Shot in song order**, which is R-45's
    other half: *"where there is no predecessor"*. The two part exactly when a later Shot covers
    the first one's head, and `_opening_clip_frames` records that geometry and why neither Shot may
    treated in it. Reading only `plan.clips[0]` would treat the burier -- a Shot whose opening cut
    its predecessor already owns.

    **`None` is four states and every one of them is correctly nothing**: no plan, an empty plan,
    an opening entry that is not a plain window, and the two definitions disagreeing. The third is
    not reachable through `assemble_project` today -- a `TransitionClip` at index 0 would need a
    blend with no frames of the outgoing Shot in front of it, which `_paired_transitions` refuses
    by name -- and it is written because this function is handed a plan by a caller that may one
    day build one differently, which is `assembly._split_frames`' own reason for its two `lead`
    guards. `test_an_opening_treatment_is_refused_when_the_plan_opens_with_a_blend` pins it at
    this function's own boundary, which is the only place that state exists.
    """
    plan = subject.plan
    if plan is None or not plan.clips or not subject.clips:
        return None
    opening = plan.clips[0]
    if not isinstance(opening, ClipWindow):
        return None
    # `min` and not `sorted(...)[0]`: both answer the first minimal element, and two Shots
    # starting at the same instant are a tie `assembly_plan`'s own stable sort breaks the
    # same way -- whichever wins, it is the Shot the resolution loop treats as the earlier.
    first = min(subject.clips, key=lambda item: item.start)
    if opening.shot_id != first.shot_id:
        return None
    return 0


def _compose_opening_transition(
    subject: ExportSubject, composition: ExportComposition
) -> list[str]:
    """The `transition_in` of the Shot that opens the plan, as stages on that clip's own head.

    R-45 and story 11.f8, and FX-18's other half: *"a one-sided transition treats a Shot's own
    final **or opening** frames"*. Only the final ones shipped. This is the opening ones, at the
    **one** boundary where an incoming field has nothing to disagree with -- the plan's first
    frame, where there is no predecessor and no `transition_out` to be authoritative with.

    **Everything structural about `_compose_one_sided_transitions` is true here** and is not
    restated: the stages ride `treatment_stages` after the Shot's whole look, the `sendcmd` a blur
    needs rides the end of `geometry`, the clamp is against `plan.frames[index]` rather than
    against seconds because a treatment past the last frame written composes cleanly and renders
    nothing at rc 0, and `assembly_plan` is not touched at all. What differs is the two things
    this docstring is about: which entry, and which field.

    **Which entry: `_opening_clip_index`**, which is the plan's own first entry qualified by
    R-45's *"where there is no predecessor"*. Never a search for a Shot's clips: an opening
    treatment is a fact about the video's first frame, not about a Shot, and a Shot whose head a
    later one covers contributes no opening frames to treat.

    **Which field: `subject.transitions_in`, and only at that entry.** AD-30's mirror writes it on
    the neighbour whenever a `transition_out` is set, so reading it at any second boundary would
    make one gesture compose two treatments for one cut -- a Dissolve faded out and then faded in,
    which is the picture `Fade through black` is named for and the substitution FX-18 forbids.
    That is why this is a separate walk over exactly one index rather than a widening of the walk
    beside it: neither can reach the other's boundary.

    **A first Shot with an Overlap after it is unaffected in both directions.** The blend is a
    `TransitionClip` of its own and the entry treated here is the outgoing Shot's frames *before*
    the Overlap, which `_paired_transitions` guarantees exists. So the head is treated, the blend
    is the blend, and the two are different entries of the plan.

    **The two treatments a single Shot may carry are two boundaries, not two answers to one.** The
    Shot that opens the plan can hold a `transition_in` treating its first frames and a
    `transition_out` with no Overlap treating its last, and on an unsplit Shot those land on one
    chain. They are the video's opening and the cut into the next Shot; the labels are distinct
    (`effects.OPENING_TRANSITION_LABEL`) so the two `gblur` ramps cannot be driven by one
    `sendcmd`, and on a Shot shorter than twice `ONE_SIDED_TRANSITION_FRAMES` they overlap in
    frames, which is a picture that fades up and back down over a half-second Shot and is what was
    asked for.

    **A pair-only type is refused by name with nothing substituted**, in its own sentence: the
    tail's refusal offers *"drag the two clips across each other"* and there is nothing before the
    first Shot to drag. Recorded rather than raised, for `_compose_one_sided_transitions`' reason
    -- the boundary is a perfectly good opening cut and refusing the export over one geometry
    would cost a Director a render. It is reachable without a hand-edited manifest: the write
    route refuses a pair-only `transition_in` on a Shot with no predecessor, but a Shot that
    acquired one across an Overlap keeps it when the Shot in front of it is deleted.

    **The catalogue cannot refuse a type here**, which is why nothing below catches an
    `EffectRefusal`: `_transition_catalogue_refusals` asks the first Shot's incoming field at the
    plan stage and the route raises on its report, exactly as it already does for every stored
    `transition_out`.
    """
    plan = subject.plan
    if plan is None or not subject.transitions_in:
        return []
    index = _opening_clip_index(subject)
    if index is None:
        return []
    clip = plan.clips[index]
    assert isinstance(clip, ClipWindow)
    stored = subject.transitions_in.get(clip.shot_id)
    if stored is None:
        return []
    composed = opening_transition_stages(
        stored, clip_frames=plan.frames[index], fps=ASSEMBLY_FPS
    )
    if composed is None:
        entry = TRANSITION_CATALOGUE[stored]
        composition.look.transitions.append(
            TRANSITION_REFUSED_RECORD.format(
                shot=TRANSITION_PAIR_ONLY_OPENING_REFUSAL.format(
                    label=entry.label,
                    shot=clip.label,
                    alternatives=", ".join(
                        sorted(
                            item.label
                            for item in TRANSITION_CATALOGUE.values()
                            if not item.pair_only
                        )
                    ),
                )
            )
        )
        return []
    already = composition.effect_stages.get(index, EffectStages())
    composition.effect_stages[index] = EffectStages(
        geometry=(*already.geometry, *composed.geometry),
        treatment=(*already.treatment, *composed.treatment),
        scripts=(*already.scripts, *composed.scripts),
    )
    composition.look.transitions.append(
        TRANSITION_OPENING_RECORD.format(
            shot=clip.shot_id, transition=stored, frames=composed.frames
        )
    )
    return []


def _report_transition_divergence(
    subject: ExportSubject, composition: ExportComposition
) -> list[str]:
    """Every Transition Pair that disagrees across an Overlap, said once and refused never.

    Story 11.3's third criterion and AD-30's second half. The read path shipped with story 11.1 --
    only `transition_out` is ever built from, so the export is decidable whatever the manifest
    holds -- and this is what stops that decision being silent. It returns `[]` unconditionally:
    **the export is not refused**, because an editable manifest that could make an export
    impossible is exactly what AD-30 exists to prevent.

    **What "diverged" means, and it is narrower than it sounds.** A pair diverges when an Overlap
    exists, *both* sides are set, and they differ. An **unset** mirror is not a divergence -- it is
    a one-sided transition, or a pair whose mirror a client never wrote, and reporting it would
    make this fire on the ordinary state of nearly every project that carries a transition at all.
    No Overlap is not a divergence either: there is no pair there to disagree, only the outgoing
    Shot's own one-sided treatment and an incoming field nothing reads **at that boundary**.
    R-45 does not add one: the opening treatment is composed where there is no outgoing field at
    all, so there is nothing there for the mirror to disagree with either.

    **Once per diverging pair**, which is the load-bearing word in the criterion. The walk is over
    consecutive Shots in song order, so a pair is visited exactly once however many clips either
    Shot resolves into -- a per-clip walk would say it twice for a Shot another nests inside, and a
    per-Shot walk over a three-Shot chain of disagreements would say it three times, which is
    right, because those are three pairs.

    A pair that also had its geometry refused (R-37) gets both records, and that is deliberate:
    they answer different questions. One says the boundary stayed a hard cut; this says the
    manifest cannot agree with itself, which is still true and still worth fixing.
    """
    if not subject.transitions or not subject.transitions_in:
        return []
    ordered = sorted(subject.clips, key=lambda item: item.start)
    for position, clip in enumerate(ordered):
        outgoing = subject.transitions.get(clip.shot_id)
        if outgoing is None or not _boundary_is_overlapped(ordered, position):
            continue
        after = ordered[position + 1]
        incoming = subject.transitions_in.get(after.shot_id)
        if incoming is None or incoming == outgoing:
            continue
        composition.look.transitions.append(
            TRANSITION_DIVERGED_RECORD.format(
                before=clip.label, after=after.label, out=outgoing, incoming=incoming
            )
        )
    return []


#: **This is the list Epic 10 appends to.** A binding refusal — an envelope that was never
#: measured, a parameter no effect declares — is a fact about the stack and the song, needs no
#: geometry, and belongs here as a third entry with nothing else edited.
#:
#: *Appended to on 2026-08-27, as that comment said it would be.* The parameter half is
#: `_effect_stack_refusals`' already — `validate_stack` learned bindings in slice E1, so a binding
#: on a parameter no effect declares, or on one the catalogue marks undrivable, is refused there
#: in the catalogue's own words with nothing new registered. What needed a fourth entry is the
#: half `validate_stack` cannot see, because it is a fact about the *song* and not about the
#: stack: an envelope that was never taken, or was taken from a track this project no longer has.
#:
#: *Appended to on 2026-08-28 by story 11.1, as a fifth entry with nothing else edited.* A stored
#: transition type the catalogue does not know is a fact about the stack's sibling and the song's
#: neither -- it needs no geometry at all -- so it belongs here beside the stack's own verdict.
EXPORT_PLAN_CHECKS: tuple[Callable[[ExportSubject], list[str]], ...] = (
    _window_refusals,
    _oversized_stack_refusals,
    _effect_stack_refusals,
    _binding_envelope_refusals,
    _transition_catalogue_refusals,
)

#: Every check that needs the export's own geometry, and may build what the export then runs.
#:
#: **This is the list Epic 11 appends to.** A transition is composed against the plan — it needs
#: both neighbours' frame counts and the delivery size — so it registers here, fills its own slot
#: on `ExportComposition`, and reports into the same one answer.
#:
#: *Appended to on 2026-08-28, as that comment said it would be.* `_compose_transitions` composes
#: both legs of every transition segment and writes `ExportLook.transitions`.
#:
#: *Appended to twice more on 2026-08-29, by story 11.4 and story 11.3's third criterion, with
#: nothing else edited -- which is the property this tuple was built for.* Both write into
#: `ExportLook.transitions` and **both return `[]` always**: a one-sided transition on a type that
#: has none, and a pair that disagrees, are recorded rather than refused (R-37, AD-30). The two
#: entries that *can* refuse an export are still the two above.
#:
#: *Appended to once more on 2026-08-31, by R-45 and story 11.f8, with nothing else edited.*
#: `_compose_opening_transition` treats the opening frames of the Shot that lays the plan's first
#: frame, and returns `[]` always for the same reason its neighbour does.
#:
#: **Order is read order.** A reader of the slot gets, per boundary: what the plan refused, what it
#: blended, what a Shot treated on its own, and finally what the manifest could not agree with
#: itself about.
#:
#: **The opening is registered after the three that walk boundaries**, because it is the only one
#: that is not about a boundary between two Shots and because it appends to a chain the walk
#: before it may already have added a tail to. A reader of the slot therefore gets every cut and
#: then the video's own first frame, which is one entry and cannot be confused for a cut: its
#: record says `opening`.
def _report_omitted_clips(
    subject: ExportSubject, composition: ExportComposition
) -> list[str]:
    """Every Shot the plan dropped for laying no frames, onto `ExportLook.omitted`.

    **Reports and refuses nothing** (item 78). A window that rounds to the same grid frame at
    both ends is a legal edit and a sum-neutral drop; what was wrong was that it left no trace.
    `job.inputs` is built from the plan's surviving entries, so a Shot whose only entry was
    dropped vanished from FR-24's *"the exact takes this export was built from"* entirely —
    measured on `assembly_plan`'s own documented geometry, `A[0,10]` with
    `B[0.483333,0.516667]`, whose inputs list named `shot_a` twice and `shot_b` never.

    Returning `[]` always is the point: refusing here would refuse an export over a Shot the
    Director is entitled to leave sub-frame, and `EXPORT_DURATION_PROBLEM` already answers the
    only question that could make it fatal.
    """
    plan = subject.plan
    if plan is None:
        return []
    composition.look.omitted.extend(
        ASSEMBLY_OMITTED_RECORD.format(
            shot=clip.label, start=clip.start, end=clip.end
        )
        for clip in plan.omitted
    )
    return []


EXPORT_COMPOSITION_CHECKS: tuple[
    Callable[[ExportSubject, ExportComposition], list[str]], ...
] = (
    _compose_effect_chains,
    _compose_transitions,
    _compose_one_sided_transitions,
    _compose_opening_transition,
    _report_transition_divergence,
    _report_omitted_clips,
)


def export_plan_refusals(subject: ExportSubject) -> list[str]:
    """Every registered plan-stage check, run in order, into one report."""
    return [line for check in EXPORT_PLAN_CHECKS for line in check(subject)]


def compose_export(subject: ExportSubject) -> tuple[ExportComposition, list[str]]:
    """Every registered composition check, run in order: what the export will run, and every
    reason it cannot.

    The composition is returned whether or not the report is empty — a caller that is about to
    refuse discards it, and a half-filled one has driven nothing, since the route raises before
    the job record is written.
    """
    composition = ExportComposition()
    refusals = [
        line for check in EXPORT_COMPOSITION_CHECKS for line in check(subject, composition)
    ]
    return composition, refusals


#: Why a Shot cannot be previewed: there is no approved take to run the chain over. A preview is
#: a picture of a file, and this Shot has not decided which file yet — so the remedy is the
#: approval, and the sentence says so rather than only reporting the absence.
#:
#: `latest_output` is deliberately not accepted in its place. AD-13 keeps the two apart on
#: purpose, the export reads `approved_output` and nothing else, and a preview of the unapproved
#: take would be a picture of a frame the export will not produce — which is the one thing a
#: preview must never be.
PREVIEW_NO_TAKE_REFUSAL = (
    "{shot} has no approved take, so there is nothing to preview. Approve a take for this shot "
    "and its look can be judged against it."
)


#: Why a preview refused although the Shot is approved: the file the approval names is not on
#: disk, or is not inside ComfyUI's output root. Names the recorded path, because the useful
#: information is *which* file went missing — the same shape `ASSEMBLY_TAKE_UNREADABLE_REFUSAL`
#: uses at the export.
PREVIEW_TAKE_MISSING_REFUSAL = (
    "{shot}'s approved take is not on disk, so there is nothing to preview: {path}. "
    "Re-render the shot and approve the new take."
)


#: Why a preview has no geometry to render at. AD-29 makes preview geometry a fact about the
#: project — the size the export would normalize to — so this is reached when **no** approved
#: take in the project could be measured at all, which for the previewed Shot means ffprobe
#: cannot read the very file the render would decode.
#:
#: AD-29 offers a fallback to the take's own dimensions "and says so". It is not taken here, and
#: this is the reason: the previewed Shot's take is itself approved, so whenever it is readable
#: it is in the plan and the export geometry is derivable. The only way to reach this branch is a
#: take nothing can measure — and rendering it at a guessed size would fail in ffmpeg a moment
#: later with a worse sentence. A refusal that names the measurement is the honest form of
#: "it fell back", so nothing silently chooses a different frame.
PREVIEW_NO_GEOMETRY_REFUSAL = (
    "{shot}'s approved take could not be measured, so the size the export would give it is "
    "unknown and there is nothing to preview at. Check the take plays, then try again."
)


#: Why this preview was thrown away: a newer request for the same project arrived while it was
#: rendering, and AD-24 says the newer one wins. **The cancelled render is discarded, never
#: played** — its output file is deleted rather than published, so nothing that arrives late can
#: be served as current.
#:
#: A refusal rather than a 200 carrying the older picture, because the older picture is exactly
#: what a Director dragging a slider must not be shown. A client that fired the superseded
#: request has already fired the one that replaced it; there is nothing for it to do about this
#: answer, and saying so plainly beats returning a clip that is wrong.
PREVIEW_SUPERSEDED_REFUSAL = (
    "{shot}'s preview was replaced by a newer one before it finished, so this one was discarded. "
    "The newer request is the one that answers."
)


#: Why a preview render failed in ffmpeg. 502 like the export's stage failure and for its reason:
#: a local tool this application drives returned non-zero, which is not something the request
#: could have avoided. The Effect Stack is untouched — a preview reads the manifest and never
#: writes it, so there is nothing to roll back.
PREVIEW_FAILED_ERROR = "{shot}'s preview could not be rendered: {detail}"


#: What a joiner is told when the render it attached to ended without recording an outcome at
#: all — its handler raised something no branch here anticipated, or was cancelled out from under
#: it. It is a `detail` for `PREVIEW_FAILED_ERROR` rather than a refusal of its own, because from
#: the joiner's side that is exactly what happened: the render it was waiting on did not produce
#: a clip. It exists so that the render's `finally` always has something honest to release its
#: joiners with; a joiner that is never released is the one outcome R-22 cannot have.
PREVIEW_ABANDONED_DETAIL = "the render ended without producing a clip"


#: Why there is no boundary to preview after this Shot: nothing follows it in song order. A
#: `transition_out` on the last Shot is a real editorial choice and it is one-sided -- the Shot's
#: own preview shows it, which is what the sentence sends the Director to rather than leaving the
#: control looking broken.
BOUNDARY_PREVIEW_NO_NEIGHBOUR_REFUSAL = (
    "{shot} is the last shot in the song, so there is no boundary after it to preview. Its "
    "transition treats its own last frames and its own preview shows them."
)

#: Why there is no boundary preview although both Shots exist: they do not overlap, so there is
#: no blend -- the transition is one-sided and the outgoing Shot's own preview is the picture of
#: it. **Which absence it is, said plainly** (story 11.5's last acceptance criterion): a control
#: that simply did nothing here would read as a fault, where the honest answer is that this
#: boundary has no blend to look at.
BOUNDARY_PREVIEW_NO_OVERLAP_REFUSAL = (
    "{before} and {after} do not overlap, so there is no blend between them to preview. "
    "{before}'s transition treats its own last frames and its own preview shows them."
)

#: Why there is no boundary preview although the two Shots do overlap: no transition is stored on
#: the outgoing side, so the boundary is a hard cut. An Overlap with nothing chosen is still a cut
#: (UX-DR8, `api.TRANSITION_UNTYPED_LABEL`), and previewing it would show two clips meeting with
#: nothing in between -- which is what the timeline already draws.
BOUNDARY_PREVIEW_NO_TRANSITION_REFUSAL = (
    "{before} and {after} overlap, but no transition is set on {before}, so the boundary is a "
    "hard cut and there is no blend to preview. Choose a transition to see one."
)

#: Why the plan did not compose this boundary at all, said in the plan's own words. R-37's
#: geometry refusals -- a third clip over the Overlap, or the incoming Shot swallowed whole -- are
#: recorded on `ExportLook.transitions` at the export and are the same fact here: there is no
#: blend to look at because there will be no blend in the video. The sentence travels whole rather
#: than being reworded, so the preview and the export do not hold two opinions about one geometry.
BOUNDARY_PREVIEW_REFUSED_BY_PLAN = (
    "{before} and {after} will not blend, so there is nothing to preview: {detail}"
)

#: Why a boundary preview cannot be rendered although the blend exists: one of the two takes could
#: not be resolved or measured, so it is not in the plan. `PREVIEW_TAKE_MISSING_REFUSAL`'s
#: division, for a subject that needs two takes rather than one.
BOUNDARY_PREVIEW_TAKE_MISSING_REFUSAL = (
    "{before} and {after} cannot be previewed together: one of the two approved takes could not "
    "be read, so the export has no plan for this boundary. Check both takes play, then try again."
)


class ShotListRequest(BaseModel):
    shots: list[Shot]
    #: The project revision this shot list was edited against, for optimistic concurrency —
    #: enforced when present, so a client that sends it can never silently overwrite work
    #: saved after it loaded. Optional because the wire has always been bare `{shots}`:
    #: scripts and older clients keep working, with the documented hazard they always had.
    #: What made it real (2026-08-19): the Director's open tab fired a background shot save
    #: with a list loaded before a repair pass, and one PUT reverted 32 prompts and four
    #: singing flags at once — the whole-manifest guard hole this codebase keeps meeting,
    #: now with a lock the interface actually sends.
    updated_at: datetime | None = None


class ProjectDocumentsRequest(BaseModel):
    creative_brief: str = ""
    treatment: str = ""
    style_bible: str = ""
    # Locks are tri-state on the wire: `None` means "leave the stored lock as it is". Every
    # other field here defaults to "", which is why an omitted one blanks its document —
    # a lock defaulting to False the same way would silently unlock every document on every
    # ordinary save, and the save path would quietly defeat the feature.
    #
    # The recovery slots are deliberately absent from this model, and that is *more* true now
    # that one of them is filled here. A body cannot forge, clear, or advance a kept version:
    # the route computes the Brief's slot from the text it is displacing, so the only thing a
    # client can influence is the new text. The other two survive untouched because the route
    # mutates the stored project.
    creative_brief_locked: bool | None = None
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
#
# The Brief's document refusal (2026-09-03) is the near miss this sentence predicted: it names a
# *save* as what keeps a version, exactly as this one does, because for the Brief that is true.
# What keeps the two apart is that neither marker -- "was kept for this song" and "nothing to
# restore" -- appears in the other's sentence, and a contract test executes that over every
# document rather than over `treatment` alone.
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


def _adopt_song_vocal_type(incoming: Song | None, stored: Song | None) -> None:
    """Overwrite `incoming`'s vocal type with the stored song's, because a client is never its author.

    `_adopt_song_recovery_slots`' argument for a field with a different shape and the same hole.
    `PUT /api/projects/{id}` binds a whole client-supplied `Project`, so its `song` arrives with
    every field defaulted — including this one, whose default is `"unstated"`. A client written
    before the field existed omits it and one ordinary save silently un-declares the cast; a client
    that invents one declares a duet the Director never declared, and the per-line dropdown then
    appears over their lyric sheet on the strength of a value nobody set.

    A Song-less body is a no-op, exactly as the slots' adoption is: there is no vocal type to carry
    across, and `import_song` builds a fresh `Song` whose default says the same thing.

    Called *before* the route compares the two songs, so a body differing only here compares equal.
    A confirmed replacement then clears it through `_detach_song_recovery_slots`' path — a different
    track's vocal type is not this one's.
    """
    if incoming is None:
        return
    incoming.vocal_type = stored.vocal_type if stored is not None else "unstated"


def _detach_song_analysis(song: Song | None) -> None:
    """Leave `song` with no analysis at all — `_detach_song_recovery_slots`' argument, measured.

    An envelope describes the audio it was taken from. Carried across a replacement it would name
    a sidecar full of the *old* track's beats and hand them to the new one, and the pointer would
    look perfectly healthy doing it. The fingerprint would catch it on the next read — that is the
    whole point of deriving validity — but leaving a pointer that is known-wrong at the moment it
    is written is not something to lean on a downstream check for.

    Note what this deliberately does *not* do: it does not delete the sidecar, and that is a
    statement about tidiness rather than about recovery. The file is at one fixed path, so the
    next measurement overwrites it and no orphan accumulates; until then the cleared fingerprint
    means every read reports the analysis absent, which is correct.

    It buys **no** re-import shortcut, and an earlier version of this comment claimed it did. It
    cannot: clearing the record clears the fingerprint, `song_fingerprints_match` requires both
    sides non-empty, and `upload_song` builds a fresh `Song` whose record is empty anyway. A
    Director who re-imports the same file pays for the measurement again — 176 ms — and that is
    the honest description.
    """
    if song is None:
        return
    song.analysis = SongAnalysis()


def _adopt_song_analysis(incoming: Song | None, stored: Song | None) -> None:
    """Overwrite `incoming`'s analysis with the stored song's, because a client is never its author.

    `_adopt_song_recovery_slots`' argument again, for the **twelfth** recorded time this hole has
    been found in this one route, and it fails the same two ways. A client written before the field existed
    omits it, so an ordinary save arrives carrying a default `SongAnalysis` and would silently
    discard the pointer to a measurement that is still on disk and still current — the envelope
    would report absent from then on, and nothing would say why. A client that *invents* one is
    worse: `SongAnalysis.path` is a path this application then reads, and a fabricated fingerprint
    would make a stale envelope pass the one check that exists to catch it.

    A Song-less body is a no-op, exactly as the other two adoptions are.

    Called *before* the route compares the two songs, so a body differing only here compares equal
    and an old client's ordinary save is not told it is replacing the song.

    The narrower sibling write path, `PUT .../shots`, needs no counterpart: `ShotListRequest`
    carries shots and a revision and no Song at all, so there is no client-supplied `analysis` on
    that wire to adopt. Checked rather than assumed — it is the route this codebase's guard holes
    keep turning up on.
    """
    if incoming is None:
        return
    incoming.analysis = (
        stored.analysis.model_copy(deep=True) if stored is not None else SongAnalysis()
    )


@dataclass(frozen=True)
class SongMeasurement:
    """One verdict on one question: does this stored measurement still describe this song?

    `current` is the answer. `reason` is the sentence a caller reporting absence has to report,
    so no caller re-derives it from the same inputs — a second answer to a smaller question is
    the shape this type exists to remove. `envelope` is the sidecar's contents when, and only
    when, the verdict is `current`, so the read path is not made to open the file twice.

    `recorded` is false when there was no measurement to judge at all. It is separate from
    `current` because "never measured" and "measured, and stale" have different remedies, and
    because the read path answers the first one with the more specific sentence
    `analysis_absence_reason` works out from the state of this machine.
    """

    current: bool
    reason: str
    envelope: dict[str, Any] | None = None
    recorded: bool = True


def song_measurement_verdict(
    store: ProjectStore, project_id: str, analysis: SongAnalysis, source: Path
) -> SongMeasurement:
    """Whether `analysis` still describes the audio at `source`, and why not when it does not.

    **One implementation, because there was nearly a second.** The read path and the analysis
    skip both asked this question and answered it differently: only the read path checked that
    the record and the sidecar agree about how the song was measured, so an envelope the read
    reported absent with `SONG_ENVELOPE_RECORD_DISAGREES` was current-and-skippable to the
    analysis — and, the non-forced caller being the render-landing path, stayed that way until
    something forced a measurement. Two answers to one question is the defect; this is the fix.

    **Cheapest first, and the order is the contract.** A `stat` against the byte count the stored
    fingerprint was taken over settles the common case for free — a replaced song is almost never
    the same length as the one before it — and the SHA-256 behind it runs only when the sizes
    agree. The sidecar is opened only once the bytes are known to match, because a measurement of
    a song that is no longer here is not worth reading.

    Not to be confused with `_song_bytes_moved`, which asks the cheaper and genuinely different
    question *could the bytes have moved?* on the two-second render poll. That one may never grow
    a hash or a sidecar read; this one is both, and is reached only when somebody asks.
    """
    if not analysis.path or not analysis.song_fingerprint:
        # Nothing to judge. The reason is the plain one; a caller with a file in front of it and
        # a reason to be specific refines it with `analysis_absence_reason`.
        return SongMeasurement(False, SONG_ENVELOPE_NOT_TAKEN, recorded=False)
    try:
        measured = source.stat().st_size
    except OSError:
        # The media, not the measurement, is what is missing — and the sentence has to say so.
        # "The song changed" would send a Director looking for a replacement they never made.
        return SongMeasurement(False, SONG_ANALYSIS_MEDIA_MISSING)
    if fingerprint_size(analysis.song_fingerprint) != measured:
        return SongMeasurement(False, SONG_ENVELOPE_SONG_CHANGED)
    # Same size is only a *maybe* — an edit in place, a re-render of the same length — so the
    # digest still settles it. This is the expensive read, and it is now reached only by a song
    # whose length is unchanged.
    current = song_fingerprint(source)
    if not current:
        return SongMeasurement(False, SONG_ANALYSIS_MEDIA_MISSING)
    if not song_fingerprints_match(analysis.song_fingerprint, current):
        return SongMeasurement(False, SONG_ENVELOPE_SONG_CHANGED)
    envelope = store.read_song_envelope(project_id, analysis.path)
    if envelope is None:
        return SongMeasurement(False, SONG_ENVELOPE_FILE_UNREADABLE)
    # The record and the file have to agree about how the song was measured. They are written in
    # the same breath and cannot disagree by any path this application takes — but a consumer
    # reads `band_count` off the *record* and then indexes `bands` in the *file*, so a
    # disagreement is an out-of-range read on a screen rather than an error anybody sees. Checked
    # because it is the manifest that is hand-editable, and because "they cannot differ" is an
    # argument, not a guarantee. It lives here rather than at the read path so that the analysis
    # re-measures the state the read refuses to serve, instead of skipping it forever.
    if (
        envelope.get("band_count") != analysis.band_count
        or envelope.get("analysis_rate") != analysis.analysis_rate
        or len(envelope.get("bands") or ()) != analysis.band_count
        # The two arrays this application actually *serves*, added 2026-08-28. The comment above
        # names the hazard exactly -- a consumer reads `band_count` off the record and then indexes
        # the file -- and `band_average` and `band_edges` were the two consumers it did not check.
        # The browser never sees `bands`, so the spectrum strip positions its bars off
        # `band_average` while the compiler weights off `bands`: with the two disagreeing, the
        # strip painted its middle bar at full weight over bands the export weighted at 8e-05, and
        # a Director selected one band and drove another. The client now refuses to draw when
        # `band_edges` fails to corroborate `band_average`, but corroboration is not proof -- a
        # sidecar trimmed in both still lies, and only this check can see that. `audio.py` writes
        # all three from one `band_count` in one breath, so any disagreement is a hand edit or a
        # half-written file, and the remedy is the one this verdict already offers: re-analyse.
        #
        # **Only when the array is drawable, which is the whole subtlety.** A *malformed*
        # `band_average` -- nested lists, a string, a null inside it -- is already handled, and
        # better: `served_measurement` drops that one key, the rest of the envelope survives, and
        # the client's own corroboration then refuses to draw a strip. Refusing the whole
        # measurement there would take the beats and the gaps down with a key nobody asked for,
        # which is what `test_a_sidecar_the_reader_accepts_but_cannot_be_drawn_is_still_a_200`
        # exists to prevent -- and what the first draft of this check did. So the length is
        # compared only for a value that would actually reach the wire; anything else is left to
        # the drop. The state this catches is the one no client can see: both arrays present,
        # well-formed, agreeing with each other, and disagreeing with the record.
        or _served_length(envelope.get("band_average")) not in (None, analysis.band_count)
        or _served_length(envelope.get("band_edges")) not in (None, analysis.band_count + 1)
    ):
        return SongMeasurement(False, SONG_ENVELOPE_RECORD_DISAGREES)
    return SongMeasurement(True, "", envelope)


def analyze_project_song(
    store: ProjectStore,
    project_id: str,
    project: Project,
    source: Path,
    *,
    force: bool = False,
) -> str:
    """Measure `source` into the project's sidecar and point `Song.analysis` at it.

    Returns `""` when the project now has a current envelope, and a **named reason** when it does
    not. It never raises and never saves: the caller decides what a failure means for its own
    route, and every caller so far decides it means "carry on". The Project is mutated only on
    success, so a failed analysis leaves the manifest byte-identical to what it would have been if
    this function had never been called — which is what "the Project is otherwise unchanged" means
    in practice.

    **Module level, with its dependencies passed in.** Treatment Story 16.2 folds this and the
    lyric-structure pass under one trigger without merging the computations, so it has to be
    callable from somewhere that is not the import route, and testable without one. The store and
    the already-resolved path are arguments for exactly that reason.

    **Skippable when the measurement is still current**, and *current* is decided by
    `song_measurement_verdict` — the one function that answers that question for this
    application, and the same one `song_envelope_report` asks. So this path skips precisely what
    the read path is willing to serve: a sidecar that has gone, or one whose header disagrees
    with the record that names it, is re-measured here rather than treated as done while the
    read reports it absent. A skip returns `""`, because "already done" is a success and must
    never read as a failure to run. `force` is for a caller that wants the measurement retaken
    at a changed rate or band count, and skips the verdict entirely.

    **Synchronous by design.** A three-minute song measures in **168 ms** end to end on this
    build — 53 ms of ffmpeg decode and 115 ms of transform and features, against a 245 ms budget
    measured while the story was planned — which is cheaper than the `ffprobe` call the import
    route already makes. Peak memory is 27 MB and does not grow with the song: see
    `audio.TRANSFORM_CHUNK_FRAMES`. There is no background job lane in this application, and inventing one
    for a fifth of a second of arithmetic would buy a task registry, a polling endpoint and a
    whole new class of state that can be lost. Being fast satisfies "never blocks" better than being
    deferred does. Callers on the event loop hand this to the threadpool; callers already in the
    threadpool call it directly.
    """
    song = project.song
    if song is None:
        return SONG_ANALYSIS_WITHOUT_SONG
    # The currency question, asked of the one function that answers it, so this path skips
    # exactly what the read path serves and re-measures exactly what the read path refuses.
    # Asked before the fingerprint is taken, because the verdict settles the common "the song
    # was replaced" case on a `stat` and a digest here would have made that saving impossible.
    if not force and song_measurement_verdict(store, project_id, song.analysis, source).current:
        return ""
    fingerprint = song_fingerprint(source)
    if not fingerprint:
        return SONG_ANALYSIS_MEDIA_MISSING
    try:
        envelope = analyze_song(source)
    except FfmpegMissing:
        return SONG_ANALYSIS_FFMPEG_MISSING
    except Exception as error:
        # **Deliberately every exception, not the two this module knows how to raise.** The
        # narrow version was `(SongAnalysisError, ValueError)`, and what it let through was worse
        # than what it caught: a `MemoryError` on a long track, or an `OSError` reading the file
        # out from under a decode, escaped this function into `upload_song`, which raised before
        # `store.save` and answered 500 — leaving the uploaded audio on disk and no Song in the
        # manifest at all. An import losing its own upload because a *measurement* failed is the
        # exact outcome the comment at the call site promises cannot happen, and a promise that
        # depends on having enumerated every exception numpy and ffmpeg can raise is not one.
        #
        # This is a measurement. Nothing downstream of it is allowed to fail because of it, so
        # nothing escapes it. By class as well as by message, because some exceptions in this
        # codebase stringify to nothing at all and a reason that renders as an empty string reads
        # to a Director as a bug in the application rather than as a fact about their file.
        reason = str(error) or type(error).__name__
        logger.warning("Song analysis failed for %s", project_id, exc_info=True)
        return SONG_ANALYSIS_DECODE_FAILED.format(reason=reason)
    try:
        relative = store.write_song_envelope(project_id, envelope)
    except Exception as error:
        # Same rule on the write half, and `ValueError` is a real member of it: the writer refuses
        # a non-finite value rather than putting a bare `NaN` token in a JSON file.
        logger.warning("Song analysis could not be written for %s", project_id, exc_info=True)
        return SONG_ANALYSIS_WRITE_FAILED.format(reason=str(error) or type(error).__name__)
    # Assigned last, and only here. A record pointing at a sidecar that was never written would
    # report absent on every read — correct, but it would have thrown the measurement away.
    song.analysis = SongAnalysis(
        path=relative,
        # Read back off the envelope, never off `audio.py`'s defaults. The record has to describe
        # the file that was actually written, and the effective analysis rate can differ from the
        # requested one when the hop does not divide the sample rate.
        analysis_rate=envelope["analysis_rate"],
        band_count=envelope["band_count"],
        bpm=envelope["bpm"],
        song_fingerprint=fingerprint,
    )
    return ""


def _audio_stream_present(source: Path) -> bool | None:
    """Whether ffprobe can see an audio stream in `source`. `None` means it could not tell.

    Three answers, not two, and the third one matters. `False` is ffprobe having read the file and
    refused it — a real verdict about the file. `None` is ffprobe not being on this machine, or
    timing out: no verdict at all, and reporting "your file is broken" on the strength of a tool
    that never ran would be exactly the fabricated statement this codebase refuses.

    Reuses `assembly.probe_streams_args` rather than spelling a fourth ffprobe invocation, and
    borrows `_media_duration`'s tolerant shape. `check=False` because a non-zero exit is the
    signal here rather than an exception: ffprobe exits non-zero on a file it cannot parse, which
    is precisely the case being detected.
    """
    try:
        result = subprocess.run(
            probe_streams_args(source), capture_output=True, check=False, text=True, timeout=15
        )
    except Exception:  # noqa: BLE001 - a read-time hint may not raise; see below
        # Every exception, for the same reason the analysis catches every exception: this runs
        # inside a **read-only** endpoint that is merely explaining why something is absent, and
        # there is no failure here worth turning into a 500. `text=True` alone can raise
        # `UnicodeDecodeError` on a binary-ish stderr, and a locked or unreadable file raises
        # `PermissionError`, neither of which is a `SubprocessError`. Any of them means the same
        # thing: this check has no opinion.
        return None
    if result.returncode != 0:
        return False
    return "audio" in result.stdout.split()


def analysis_absence_reason(source: Path) -> str:
    """Why a song that *is* on disk carries no envelope. Worked out now, never remembered.

    **This is the whole of AD-21 and standing law 5 applied to a failure.** The obvious
    implementation is a `reason` field on `SongAnalysis` written when an analysis fails — and it
    is the same mistake as a `valid` flag, one step further removed from the truth. A stored
    string describes an attempt that happened once, on a machine that may since have had ffmpeg
    installed, over a file that may since have been replaced. It can only ever get more wrong, and
    nothing would ever clear it. So the reason is derived at read time from what is true at read
    time, exactly as validity is.

    Ordered by remedy, cheapest first. `shutil.which` is a PATH lookup and no subprocess at all;
    the ffprobe only runs when ffmpeg *is* installed and there is still no envelope, which is the
    genuinely unusual case. Neither cost lands on a common path: this is reached only when a
    project has a song, has no analysis record, and somebody asked why.

    Read-only, and it must stay that way — nothing here decodes the song, computes an envelope or
    writes a byte. It answers a question; it does not fix anything.
    """
    if shutil.which("ffmpeg") is None:
        return SONG_ANALYSIS_FFMPEG_MISSING
    if _audio_stream_present(source) is False:
        # `is False` rather than `not`: a `None` from the probe is "could not tell", and the
        # honest answer to that is the plain "not analysed yet" below.
        return SONG_ENVELOPE_UNDECODABLE
    return SONG_ENVELOPE_NOT_TAKEN


def song_audio_path(project: Project) -> str:
    """The Song's audio path as it stands right now, or `""` when there is no Song.

    One spelling of "where is this project's audio", because it is read on both sides of a
    comparison at two different call sites and four hand-written copies of `project.song.path if
    project.song else ""` is how one of them ends up not handling `None`.
    """
    return project.song.path if project.song is not None else ""


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


# ------------------------------------------------------------------------------------------
# Which manifest write is guarded, and by what.
#
# **Read this before adding a route that calls `store.save`.** Every route in this module
# reads the whole project, changes part of it, and writes the whole thing back. Nothing about
# that is atomic: two requests that overlap both read the same manifest and the second one to
# save silently reverts the first, with both answering 200. It is not hypothetical. On
# 2026-08-19 one background shot save reverted thirty-two prompts and four singing flags in a
# single `PUT /shots`, which is what `ShotListRequest.updated_at` was added for.
#
# The mechanisms all exist. `ProjectStore.save`'s `if_generation` is a compare-and-swap on the
# process's own write counter, `read_for_update` hands out the token, `handle_save_race` turns
# the refusal into one 409 carrying `SAVE_RACE_REFUSAL`, and `record_submission` is the
# re-read-and-patch shape for a write that must not be refused. What was missing was any
# record of *which routes reach for them*, so each hole had to be found one audit at a time —
# and the same hole was found in a sibling route six separate times. This table is that
# record. It is enumerated off the live app and asserted against the source by
# `test_every_manifest_write_is_classified`, so a route added here without a classification
# fails the suite rather than quietly joining the bottom group.
#
# The three answers, and when each is right:
#
# * `WRITE_GUARD_COMPARE_AND_SWAP` — read through `get_project_for_update`, save with
#   `if_generation`. The loser is refused with a 409 it can act on. This is the answer for a
#   **user-initiated write that can be retried**: the Director clicked something, they can look
#   and click again. It is the only answer for a write whose loss is undetectable afterwards —
#   a recovery slot swapped, a Song detached, a whole shot list replaced — because there both
#   outcomes are a well-formed manifest and nothing downstream can tell them apart.
#
# * `WRITE_GUARD_REREAD` — re-read the manifest and write only the fields this route owns
#   (`record_submission`, `settle_unsubmitted_jobs`, `assemble_project`'s `settle`). This is
#   the answer for a write that **must not be refused**: a graph ComfyUI has already accepted,
#   an export already running. Refusing there would leave real work in flight with nothing
#   recorded to receive it, so the write lands on whatever manifest is current and touches
#   nothing else.
#
# * `WRITE_GUARD_LAST_WRITER_WINS` — no guard. Honest rather than approving: these are the
#   routes nobody has been through yet. Each is a plain read, mutate, save whose window is
#   short (no await between the read and the save) and whose loss is usually visible — a
#   rename that did not take, an approval that has to be clicked again. Short is not zero:
#   FastAPI runs a sync endpoint in a threadpool, so two of these genuinely overlap. Converting
#   one is two lines, and the reason it is not done wholesale is that each route has to be able
#   to say what its own write owns before it can be told it lost.
#
# A revision token (`updated_at`, `plan_id`) is a *different* guard and does not appear here:
# it catches a request built against a revision the server has already moved past, is compared
# at the top of the route, and says nothing about what lands between that comparison and the
# save. `replace_project` and `replace_shots` carry both, which is what two guards for two
# different lies about one manifest looks like.
# ------------------------------------------------------------------------------------------

WRITE_GUARD_COMPARE_AND_SWAP = "compare-and-swap"
WRITE_GUARD_REREAD = "re-read and patch"
WRITE_GUARD_LAST_WRITER_WINS = "last writer wins"

#: Every route in this module that writes the manifest, by the guard on its write.
MANIFEST_WRITE_GUARDS: dict[str, str] = {
    # Refused when it loses, because the Director can look and click again.
    "assistant_fill": WRITE_GUARD_COMPARE_AND_SWAP,
    "director_chat": WRITE_GUARD_COMPARE_AND_SWAP,
    "expand_shot_prompts": WRITE_GUARD_COMPARE_AND_SWAP,
    "read_render_status": WRITE_GUARD_COMPARE_AND_SWAP,
    "remove_song": WRITE_GUARD_COMPARE_AND_SWAP,
    "replace_project": WRITE_GUARD_COMPARE_AND_SWAP,
    "replace_shots": WRITE_GUARD_COMPARE_AND_SWAP,
    # Moved up from "not yet been through" on 2026-09-03, when the Brief's recovery slot
    # started being filled here. The rule above is that a compare-and-swap is the only
    # answer for a write whose loss is undetectable afterwards, and it names a recovery
    # slot as its example; this route is now `replace_song_context`'s twin rather than
    # three strings a client was already holding.
    "replace_documents": WRITE_GUARD_COMPARE_AND_SWAP,
    "replace_song_context": WRITE_GUARD_COMPARE_AND_SWAP,
    "restore_document": WRITE_GUARD_COMPARE_AND_SWAP,
    "restore_song_context": WRITE_GUARD_COMPARE_AND_SWAP,
    # Never refused: a graph is already accepted or an export is already running.
    "assemble_project": WRITE_GUARD_REREAD,
    "edit_asset": WRITE_GUARD_REREAD,
    "enhance_with_ltx25": WRITE_GUARD_REREAD,
    "fill_assets": WRITE_GUARD_REREAD,
    "generate_flux": WRITE_GUARD_REREAD,
    "generate_h3": WRITE_GUARD_REREAD,
    "generate_multiview": WRITE_GUARD_REREAD,
    "generate_music": WRITE_GUARD_REREAD,
    "generate_songplanner": WRITE_GUARD_REREAD,
    "restore_song_audio": WRITE_GUARD_REREAD,
    # Not yet been through. Two lines each; see the note above for why not wholesale.
    "align_song_lyrics": WRITE_GUARD_LAST_WRITER_WINS,
    "analyze_asset": WRITE_GUARD_LAST_WRITER_WINS,
    "analyze_latest_take": WRITE_GUARD_LAST_WRITER_WINS,
    "analyze_song_now": WRITE_GUARD_LAST_WRITER_WINS,
    "approve_take": WRITE_GUARD_LAST_WRITER_WINS,
    "cancel_job": WRITE_GUARD_LAST_WRITER_WINS,
    "clean_shot_prompts": WRITE_GUARD_LAST_WRITER_WINS,
    "copy_shot_effects": WRITE_GUARD_LAST_WRITER_WINS,
    "delete_asset": WRITE_GUARD_LAST_WRITER_WINS,
    "expand_plan_prompts": WRITE_GUARD_LAST_WRITER_WINS,
    "expand_shot_prompt": WRITE_GUARD_LAST_WRITER_WINS,
    "fill_in_timeline": WRITE_GUARD_LAST_WRITER_WINS,
    "fill_section_looks": WRITE_GUARD_LAST_WRITER_WINS,
    "generate_batch": WRITE_GUARD_LAST_WRITER_WINS,
    "lay_out_timeline": WRITE_GUARD_LAST_WRITER_WINS,
    "line_up_timeline": WRITE_GUARD_LAST_WRITER_WINS,
    "populate_timeline": WRITE_GUARD_LAST_WRITER_WINS,
    "read_job": WRITE_GUARD_LAST_WRITER_WINS,
    "rename_asset": WRITE_GUARD_LAST_WRITER_WINS,
    "render_again": WRITE_GUARD_LAST_WRITER_WINS,
    "replace_asset_citations": WRITE_GUARD_LAST_WRITER_WINS,
    "replace_character_slot": WRITE_GUARD_LAST_WRITER_WINS,
    "replace_consistency_prompt": WRITE_GUARD_LAST_WRITER_WINS,
    "replace_default_setting": WRITE_GUARD_LAST_WRITER_WINS,
    "replace_sampling_profile": WRITE_GUARD_LAST_WRITER_WINS,
    "replace_sections": WRITE_GUARD_LAST_WRITER_WINS,
    # Beside its sibling and classified the same way, for the same reason: it is one Director,
    # one panel, one card. What a second writer could take from it is one binding, and the
    # Director is looking at the control that lost it — a compare-and-swap here would refuse a
    # write nobody else is racing for, on a route where the remedy is to click again anyway.
    # The narrow-write half of the Director's 2026-08-20 ruling; the *generic* routes are the
    # ones that had to be gated, and both of them are.
    "replace_shot_bindings": WRITE_GUARD_LAST_WRITER_WINS,
    "replace_shot_effects": WRITE_GUARD_LAST_WRITER_WINS,
    # The third of the narrow look writers, classified with the two above and by their argument:
    # one Director, one panel, one boundary. What a second writer could take is one transition
    # type, and the Director is looking at the control that lost it. The *generic* routes are the
    # ones that had to be gated, and `_adopt_shot_transitions` is what gates them.
    "replace_shot_transitions": WRITE_GUARD_LAST_WRITER_WINS,
    "replace_song_vocal_type": WRITE_GUARD_LAST_WRITER_WINS,
    "select_shot_take": WRITE_GUARD_LAST_WRITER_WINS,
    "snap_timeline_cuts": WRITE_GUARD_LAST_WRITER_WINS,
    "unapprove_take": WRITE_GUARD_LAST_WRITER_WINS,
    "upload_asset": WRITE_GUARD_LAST_WRITER_WINS,
    "upload_song": WRITE_GUARD_LAST_WRITER_WINS,
}


def create_app(
    *,
    settings: Settings | None = None,
    store: ProjectStore | None = None,
    comfy: ComfyClient | Any | None = None,
    director: DirectorClient | Any | None = None,
    ejector: LlmEjector | Any | None = None,
    preferences: MachinePreferences | Any | None = None,
    # The song transcriber, injectable for tests exactly as `comfy` and `director` are:
    # a callable from an audio path to Whisper's (text, start, end) words. The default
    # imports faster-whisper lazily inside the call, so nothing pays for the dependency
    # until the align-lyrics route actually runs.
    transcriber: Any | None = None,
) -> FastAPI:
    settings = settings or Settings()
    store = store or ProjectStore(settings.data_root)
    transcriber = transcriber or transcribe_song_words
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
    # The SageAttention choke point. Every H3 adapter emits a `PathchSageAttentionKJ`
    # node with the exports' own `disabled`. When the Director opts in via
    # MVP_SAGE_ATTENTION, the value is patched here — one wrapper over `comfy.submit`, so
    # every current and future adapter is covered and no builder or digest moves. Blank
    # (the default) leaves every payload byte-identical to the evidence.
    #
    # **Corrected 2026-08-21.** This comment used to add "their creator launches ComfyUI with
    # `--use-sage-attention`; this installation does not". The second half is false, and had
    # been since 2026-08-19. Live `/system_stats` reports this server's own argv as
    # `[main.py, --use-sage-attention, --fast]`, and `ComfyUI/user/comfyui.log` prints
    # `Using sage attention` at every start it retains — including `2026-08-20 09:00:34`,
    # inside the serial overnight batch the render-cost cliff on `POPULATE_MAX_WINDOW_SECONDS`
    # was reconstructed from. `PathchSageAttentionKJ` at `disabled` returns the model
    # untouched (`model_optimization_nodes.py:124`), so it writes no override and the model
    # samples on ComfyUI's *global* backend — which on this machine is SageAttention. The
    # node does not disable acceleration; it declines to override it. Blank here therefore
    # means "whatever ComfyUI was launched with", not "none", and the cliff was measured on
    # SageAttention rather than on an unaccelerated path.
    if settings.sage_attention:
        unpatched_submit = comfy.submit

        async def submit_with_sage(payload, client_id=None):
            for node in payload.values():
                if node.get("class_type") == "PathchSageAttentionKJ":
                    node["inputs"]["sage_attention"] = settings.sage_attention
            if client_id is None:
                return await unpatched_submit(payload)
            return await unpatched_submit(payload, client_id=client_id)

        comfy.submit = submit_with_sage
    catalog = WorkflowCatalog(settings.workflow_root)

    # Live render percentages, held in memory for as long as this process runs and written
    # nowhere else. `progress_listener` owns one WebSocket to ComfyUI; `render_progress` is the
    # `prompt_id → percent` map it fills and the `render-status` poll reads. Constructed here so
    # the object always exists — every route can ask it for a percentage whether or not the
    # socket ever connected — while the *task* is started by the lifespan below, because a task
    # needs a running loop and `create_app` is called from plenty of places that have none.
    render_progress = ProgressTracker()
    progress_listener = ComfyProgressListener(settings.comfy_url, render_progress)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Start the progress listener for the served app, and take it with us on the way out.

        `start` cannot fail: it creates a task, and the task owns every connection error. ComfyUI
        being down at boot is the ordinary case — the Director launches it separately — and it
        must cost the application nothing but a retry on a backoff. `stop` cancels the task and
        closes the socket, so neither leaks past shutdown.

        Deliberately the only thing in this hook. The startup work that must happen for *every*
        caller — `heal_orphaned_local_jobs_at_startup` — stays inside `create_app` above, where
        it runs for the many tests and scripts that never enter the app's lifespan at all.
        """
        progress_listener.start()
        try:
            yield
        finally:
            await progress_listener.stop()

    app = FastAPI(
        title="Music Video Producer",
        version="0.1.0",
        description="Standalone local-first music and music-video production studio.",
        lifespan=lifespan,
    )
    app.state.render_progress = render_progress
    app.state.progress_listener = progress_listener
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
    # Job ids of assemblies this *process* is running. Local jobs have no ComfyUI record to
    # reconcile against, so this set is the one truth about "still running" — a non-terminal
    # local job whose id is not in here was orphaned by a restart and gets healed to `error`
    # at the next assemble. Held on `app.state` so tests can inspect it.
    app.state.live_assemblies = set()
    # Every look the machine holds, discovered once and held for as long as this process
    # runs. `None` means "not read yet" and is distinct from `()`, which is a folder that
    # genuinely holds no usable `.cube` — flattening the two would re-read 44 MB on every
    # request against an empty folder. See `discovered_looks`.
    app.state.looks = None
    # The one in-flight preview render per project (AD-24), by project id. A dict rather than a
    # queue *is* the rule: a new request finds whatever is here and either cancels it and puts
    # itself in its place — a different fingerprint — or joins it and puts nothing here at all,
    # which is R-22. Either way a dragged slider can never accumulate renders. Empty between
    # renders, and held on `app.state` so a test can watch a render arrive rather than sleeping
    # for one.
    #
    # What is registered here is always live: a record is replaced the same instant it is
    # superseded, and removed in the same synchronous stretch that finishes it, so a joiner that
    # finds a matching fingerprint here is never attaching to something already over.
    app.state.preview_renders = {}
    # What ffprobe said about one take's geometry *and length*, for the life of the process.
    # Keyed by the file's path, byte length and modification time together, so a re-render under
    # the same name is measured again rather than remembered.
    #
    # It exists because AD-29 makes preview geometry a fact about the *whole project* — the
    # largest-area approved take — so answering it honestly means measuring every approved take,
    # and one ffprobe costs ~20 ms on this machine (measured 2026-08-25). A forty-shot project
    # would spend 800 ms deciding what size to render before rendering anything, against a
    # one-second budget for the whole answer. This is a memo of a measurement, not a verdict:
    # nothing derived from it is stored, and losing it costs a re-measure.
    app.state.take_measurements = {}
    # And once, here, for every project on disk: the restart that emptied that set is the
    # event that orphaned the jobs, so this is the moment the verdict is honest, rather than
    # whenever the Director next happens to assemble. Synchronous and inside `create_app`
    # rather than on a lifespan hook, because `create_app` *is* this application's boot and a
    # lifespan hook would not run for the many callers that never enter the app's context.
    # It cannot raise: see `heal_orphaned_local_jobs_at_startup`. ComfyUI jobs are untouched.
    app.state.startup_healed_jobs = heal_orphaned_local_jobs_at_startup(store)

    @app.exception_handler(ProjectChangedDuringSave)
    async def handle_save_race(_: Request, error: ProjectChangedDuringSave) -> JSONResponse:
        """One answer for the store's lost-update refusal, across every route that saves.

        `ProjectStore.save` refuses rather than replay a manifest it serialised before another
        save landed, because replaying it would revert a write whose caller was already told 200.
        Roughly seventy routes call `save`, and the refusal means the same thing at every one of
        them — *nothing was written, re-read and try again* — so it is answered once here rather
        than seventy times. A 409 and not a 500: this is the client's to act on, it is the status
        the optimistic-concurrency refusal already uses for the same remedy, and `api.js` already
        knows to re-read on one. The `detail` shape matches `HTTPException`'s so the client's
        error reader needs no special case.

        Not registered for the store's other failure, an `OSError` from a manifest that stayed
        locked for the whole retry: that one is not a concurrency problem and a retry will not
        fix it, so it stays a 500 with its traceback.
        """
        logger.warning("Refused a save that raced another write: %s", error)
        return JSONResponse(status_code=409, content={"detail": SAVE_RACE_REFUSAL})

    def discovered_looks(*, rescan: bool = False) -> tuple[LutEntry, ...]:
        """Every look this machine holds, read from the folder **once** and then held.

        `effects.discover_luts` is not cheap and cannot be made cheap: a truncated `.cube` carries
        its `LUT_3D_SIZE` header on line 1, so nothing short of counting the data lines against
        `N³` can tell a half-copied 33-cube from a whole one, and that means reading every file to
        its end. Measured 2026-08-25 on the Director's 48-file, 44.2 MB pack: **221 ms cold, 23 ms
        warm**. A picker that called it per request would re-read 44 MB every time a Director
        opened the Grade card — the named failure to avoid — so the answer is held on `app.state`
        and every consumer (the catalogue read, the stack write's validation, the export's chain)
        goes through here.

        **Lazy, not eager.** Discovery on a data root with no `luts/` folder *generates* the
        default set — six 33-cube LUTs, ~215 000 lines of text — and doing that inside `create_app`
        would put it in the boot path of every test in the suite, every script and every CLI
        invocation, in exchange for nothing. Nothing outside the effects surfaces needs a look, so
        nothing outside them pays.

        `rescan` is the one escape hatch, and it exists because the alternative is worse than the
        cost it saves: a Director who drops a `.cube` into the folder while the application is
        running would otherwise have to restart it to see the look. The catalogue read exposes it
        as an explicit parameter — a *rescan* the Director asks for, never something a picker does
        on open — which is the distinction the measurement is actually about.
        """
        held = getattr(app.state, "looks", None)
        if held is None or rescan:
            held = discover_luts(settings.data_root)
            app.state.looks = held
        return held

    def get_project(project_id: str) -> Project:
        try:
            return store.get(project_id)
        except ProjectNotFound as error:
            raise HTTPException(status_code=404, detail="Project not found") from error

    def get_project_for_update(project_id: str) -> tuple[Project, int]:
        """`get_project`, plus the write generation to hand back to `save(if_generation=...)`.

        Two kinds of caller, and they want opposite things from the same token. The background
        writer passes it so it can *retry* — see `RENDER_STATUS_SAVE_ATTEMPTS`, which re-reads
        and re-derives rather than telling anyone. A user-initiated route passes it so it can be
        *refused*: `handle_save_race` turns the store's `ProjectChangedDuringSave` into one 409
        carrying `SAVE_RACE_REFUSAL`, which is the answer a Director can act on. What no route
        may do is read here and then save without the token — that is the plain read-mutate-save
        this repair exists to remove, and it is silent by construction.

        Most routes still read through `get_project` and are still last-writer-wins. That is a
        retrofit per route, not a property of this helper: each one has to be able to say what
        its own write owns before it can be told it lost. Which routes have which guard is
        `MANIFEST_WRITE_GUARDS`, above `create_app`, and it is asserted rather than maintained
        by hand.
        """
        try:
            return store.read_for_update(project_id)
        except ProjectNotFound as error:
            raise HTTPException(status_code=404, detail="Project not found") from error

    def settle_unsubmitted_jobs(
        project_id: str,
        *jobs: RenderJob,
        accepted: Sequence[RenderJob] = (),
        patch: Callable[[Project], None] | None = None,
    ) -> None:
        """Close the records whose graphs were never accepted, and write the manifest.

        The other half of the record-first ordering (the Director's 2026-08-21 ruling). Every
        submission route now saves its `RenderJob` *before* `comfy.submit`, so a submission
        that fails leaves a record behind — and a record left `queued` on a prompt that was
        never queued is a phantom: `reconcilable_jobs` counts it, the poll keeps asking about
        it, `shot_render_in_flight` refuses the next render, and assembly and asset fill go on
        reporting the project busy. Settled here instead, in `JOB_NEVER_SUBMITTED`'s words —
        the same sentence the reconciler uses for the orphan this cannot reach, the one where
        the process died between the two steps.

        Nothing of the *target* is touched, because nothing of the target was written: the
        pre-submission save carries the job record and only the job record, so a Shot whose
        submission failed is still `ready` with its old take, an Asset that would have been
        created does not exist, and the Song was not replaced. That is what "leaves no phantom
        in-flight shot" means here — there is no state to restore.

        **Onto a re-read, not onto the caller's project.** This is called after `comfy.submit`
        has been awaited, so the object the route is holding predates the whole `/prompt` round
        trip; writing it back would revert everything committed during it while the caller is on
        its way to a 502 about something else entirely. Only the records named here are settled,
        and `accepted`/`patch` carry the two things a partial batch still has to commit — the
        graphs that *did* go out, and what they created. `record_submission` is the same rule on
        the success path.

        **A save race here is swallowed, not raised.** The caller is on its way to a 502 that
        names why the submission failed, which is the fact the Director needs; converting it
        into a 409 about the manifest would report the wrong failure. What is left behind when
        this save is refused is a record still carrying `PENDING_SUBMISSION_PROMPT_ID`, which
        the reconciler settles with this same sentence after three unknown ticks.
        """
        try:
            fresh = get_project(project_id)
        except HTTPException:
            # The project went away while `/prompt` was answering. There is no manifest to
            # settle anything on, and the caller's 502 is still the fact worth reporting.
            return
        recorded = {item.id: item for item in fresh.jobs}

        def close(record: RenderJob) -> None:
            record.status = "error"
            record.error = JOB_NEVER_SUBMITTED
            record.missing_ticks = 0
            # Stamped like every other settle, though what it records is the seconds a
            # submission spent being refused — nothing rendered here, and nothing may read it
            # as though something had. See `batch.render_timing_summary`.
            stamp_job_settled(record)

        for job in accepted:
            held = recorded.get(job.id)
            if held is None:
                fresh.jobs.append(job)
            else:
                accept_submission(held, job.prompt_id)
        for job in jobs:
            # Both objects, because they are two parses of one record and the caller keeps its
            # own: `generate_batch` reads back the job it just failed to submit. Same inputs and
            # the same `created_at`, so the two settles agree on the span they stamp.
            close(job)
            held = recorded.get(job.id)
            if held is None:
                fresh.jobs.append(job)
            else:
                close(held)
        if patch is not None:
            patch(fresh)
        try:
            store.save(fresh)
        except ProjectChangedDuringSave:
            logger.warning(
                "Could not settle %d unsubmitted job record(s) on project %s; the reconciler "
                "will settle them from the pending prompt id",
                len(jobs),
                project_id,
            )

    def record_submission(
        project_id: str,
        *jobs: RenderJob,
        patch: Callable[[Project], None] | None = None,
    ) -> None:
        """Write what an accepted submission bought onto a **fresh** manifest.

        Nine routes share one shape: read the project, save the job record, await
        `comfy.submit`, then write the accepted prompt id and whatever the acceptance implies
        for the target — an Asset created, a Shot marked queued, the Song replaced. Every one of
        them used to write that second save onto the object read *before* the await, which is
        the read-modify-write revert this codebase keeps meeting. The window is a `/prompt`
        round trip, which this application allows thirty seconds, and it is not idle time: the
        Director is in the interface that just queued the render.

        What that stale save costs is worse than the field it meant to write. It reverts *every*
        field of the manifest, and the likeliest victim is a sibling submission's own job record
        — trivially reachable on the music routes, where every job carries `target_id="song"`
        and neither route refuses an in-flight sibling, and reachable on any of them from two
        tabs. A lost record is a prompt running on the card that `reconcilable_jobs` cannot
        count, the poll never asks about, and whose output nothing will ever adopt.

        So only two things are written here: the accepted prompt id onto each named record, and
        whatever `patch` says this route owns. A record the fresh manifest no longer holds is
        re-appended rather than dropped — the graph is on the card either way, and the record is
        the only thing that can ever settle it. `settle_unsubmitted_jobs` is the same rule on
        the failure path.

        **Not a 409**, and this is the line between the two remedies in this module. A
        user-initiated write that loses a race is owed the refusal because it can be retried —
        see `handle_save_race`. These cannot: the graph is already accepted, and refusing here
        would leave a render running whose result nothing is recorded to receive.
        """
        fresh = get_project(project_id)
        recorded = {item.id: item for item in fresh.jobs}
        for job in jobs:
            held = recorded.get(job.id)
            if held is None:
                fresh.jobs.append(job)
            else:
                accept_submission(held, job.prompt_id)
        if patch is not None:
            patch(fresh)
        store.save(fresh)

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

    def analyze_song_for_project(project_id: str, project: Project, *, force: bool = False) -> str:
        """`analyze_project_song` with this application's song path resolution in front of it.

        The thin half, and the only half that needs the closure: everything about *which file* is
        the project's song lives in `resolve_song_path`, which is containment-checked, and
        everything about measuring it lives in the module-level function this delegates to. A
        missing or out-of-tree song becomes a named reason here rather than the 404 that accessor
        raises, because nothing in this story turns a missing measurement into a refusal.
        """
        song = project.song
        if song is None or not song.path:
            return SONG_ANALYSIS_WITHOUT_SONG
        try:
            source = resolve_song_path(project_id, song)
        except (HTTPException, OSError):
            # `OSError` beside the accessor's own refusal, for `song_envelope_report`'s reason:
            # `Path.resolve` touches the filesystem and can raise rather than refuse.
            return SONG_ANALYSIS_MEDIA_MISSING
        return analyze_project_song(store, project_id, project, source, force=force)

    def _song_bytes_moved(project_id: str, song: Song) -> bool:
        """Whether the song file's length disagrees with the record that describes it.

        The cheap arm of the landing gate. `False` when there is no record to compare against, or
        when the file cannot be measured at all — both mean "nothing here says the bytes moved",
        and neither is worth a re-analysis on a settle.
        """
        stored = song.analysis.song_fingerprint
        if not stored:
            return False
        try:
            measured = resolve_song_path(project_id, song).stat().st_size
        except (HTTPException, OSError):
            return False
        return fingerprint_size(stored) != measured

    async def analyze_a_landed_song(project_id: str, project: Project, before: str) -> bool:
        """Measure the Song when a finished render has just put audio behind it.

        A generated Song is created with no `path` at all: `generate_music` and the Song Planner
        both build the record when ComfyUI *accepts* the graph, and the audio only exists minutes
        later when the render lands. `batch.apply_job_history` is the one writer that fills the
        path in, so this is where "the song was stored" happens for everything that is not an
        import — and without it the acceptance criterion's "imported **or generated**" holds for
        only half of its subject.

        **The trigger is a path change, compared in memory, and that is the whole design.** The
        obvious alternative — ask the analysis to decide for itself, which it can, by fingerprint
        — would put a SHA-256 of a multi-megabyte audio file on the two-second `/render-status`
        poll, every tick, forever, to answer "no" every single time. A string comparison against
        the path as it stood before reconciliation costs nothing and is exactly as correct: the
        path is empty until the take lands, and it is `apply_job_history` that changes it.

        Called from the route rather than from `batch.reconcile_render_jobs`, deliberately.
        `batch.py`'s reconciliation functions are pure — that is stated at the top of the module
        — and threading a store, a media path and a subprocess through them to save one line here
        would trade that for nothing.

        **A path that did not change is not proof the audio did not.** A re-render that lands on
        the same output filename rewrites the bytes underneath an unchanged `Song.path`, and the
        path comparison alone would never fire again — leaving a stale envelope with nothing in
        this story able to retake it. So the gate has a second arm: a `stat` of the file against
        the byte count the stored fingerprint was taken over. That is still not a hash, and it is
        still not on the ordinary tick — both call sites reach this only when something actually
        settled — but it catches the case the path cannot see.

        The second arm requires an existing record on purpose. Without one there is nothing to
        compare against, and firing on "no record" would re-attempt a failing analysis on every
        settle for the whole life of a project whose song ffmpeg cannot read.

        A failure is logged and swallowed, `upload_song`'s rule: a render that produced real audio
        must not be reported as failed because the measurement of it did not work.

        Returns whether an envelope was actually written, which the render-status caller needs in
        order to clean up after a save it could not land.
        """
        song = project.song
        if song is None or not song.path:
            return False
        if song.path == before and not _song_bytes_moved(project_id, song):
            return False
        if reason := await run_in_threadpool(analyze_song_for_project, project_id, project):
            logger.warning("Song analysis skipped for %s: %s", project_id, reason)
            return False
        return True

    def song_envelope_report(project_id: str, project: Project) -> dict[str, Any]:
        """What this project's song analysis is, decided entirely at read time.

        One shape, **nine sentences and the envelope**, and every one of them derived here and
        now. `present` is the only thing a consumer branches on; `reason` says why when it is
        false and is never empty when it is false. There is no further answer where an envelope
        is served *and* something is wrong with it — an envelope that fails any check here is
        absent, not degraded, because a partly-trusted measurement is how an envelope of zeros
        gets downstream.

        The nine are named rather than counted, because a count retyped in prose is a claim about
        this function that goes stale the moment a reason is added — which is exactly what
        happened: this docstring said *six* and omitted `SONG_ENVELOPE_RECORD_DISAGREES`, which
        the branch at the bottom returns. `test_the_absence_reasons_split_into_read_and_write`
        derives the split from this module and fails if any of it drifts again.

        The branches, in the order they are decided:

        * no Song on the project at all — `SONG_ENVELOPE_WITHOUT_SONG`;
        * a Song whose render has not landed, so it has no audio path yet —
          `SONG_ENVELOPE_AUDIO_PENDING`;
        * a Song whose audio file is not on disk — `SONG_ANALYSIS_MEDIA_MISSING`;
        * a measurement `song_measurement_verdict` accepts — **the envelope itself**;
        * a Song that has never been measured, with the specific reason worked out by
          `analysis_absence_reason` — ffmpeg absent from this machine
          (`SONG_ANALYSIS_FFMPEG_MISSING`), a file ffmpeg will not decode
          (`SONG_ENVELOPE_UNDECODABLE`), or simply not yet (`SONG_ENVELOPE_NOT_TAKEN`);
        * a stored measurement the verdict refuses — bytes that no longer match the fingerprint
          it was taken from (`SONG_ENVELOPE_SONG_CHANGED`), a sidecar that is missing or is not
          readable JSON (`SONG_ENVELOPE_FILE_UNREADABLE`), or a sidecar that disagrees with the
          record about how the song was measured (`SONG_ENVELOPE_RECORD_DISAGREES`).

        The three remaining `SONG_ANALYSIS_*` constants — `SONG_ANALYSIS_WITHOUT_SONG`,
        `SONG_ANALYSIS_DECODE_FAILED` and `SONG_ANALYSIS_WRITE_FAILED` — are the write half's,
        logged by the analysis path and never served here.

        **The middle answers are not decided here.** Whether a stored measurement still describes
        the song in front of it is `song_measurement_verdict`'s question, and the analysis path
        asks it too — so an envelope this route reports absent is one the analysis re-measures,
        rather than one it skips as current. This route's own contribution is the two ends the
        verdict has no opinion about: which sentence a *never measured* song gets, and the states
        that exist before there is a file to judge at all.

        Nothing above is stored. The fingerprint is recomputed from the song's current bytes, and
        the failure reason is recomputed from the state of this machine and this file — so no
        route that replaces a song needs to know this feature exists, and no reason can outlive
        the condition that produced it. That is AD-21 and standing law 5, and it is why
        `SongAnalysis` has neither a `valid` flag nor a `reason` string.

        **Read-only in the strong sense:** it computes no envelope, writes no sidecar and saves no
        manifest, even when it has just worked out exactly why one is missing. Offering the fix is
        an interface decision with its own route; this endpoint only reports.
        """
        song = project.song
        if song is None:
            return {"present": False, "reason": SONG_ENVELOPE_WITHOUT_SONG}
        # Resolved before anything else is decided, because every remaining answer needs the file:
        # the fingerprint hashes it, and the absence reason probes it. An unresolvable path covers
        # both "this generated Song has no audio yet" and "the file was deleted", and the sentence
        # is true of each.
        if not song.path:
            # Not the same state as a missing file, and not the same remedy. A generated Song is
            # recorded when ComfyUI accepts the graph and has no audio until the render lands;
            # nothing is lost and nothing needs doing. Telling a Director their audio "was not
            # found on disk" while it is still rendering would send them looking for a file that
            # was never supposed to exist yet.
            return {"present": False, "reason": SONG_ENVELOPE_AUDIO_PENDING}
        try:
            source = resolve_song_path(project_id, song)
        except (HTTPException, OSError):
            # `OSError` as well as the accessor's own refusal: `Path.resolve` touches the
            # filesystem, and a path on a disconnected drive raises rather than returning
            # something this can compare. A read-only endpoint may not 500 over that.
            source = None
        if source is None:
            return {"present": False, "reason": SONG_ANALYSIS_MEDIA_MISSING}
        # Everything from here — is the record still describing this file, and if not, why not —
        # belongs to `song_measurement_verdict`, which the analysis path asks in the same words.
        # It works cheapest-first: a `stat` before any digest, and the sidecar opened only once
        # the bytes are known to agree. What is left to decide here is which *sentence* an
        # absence gets, which is the one thing this route knows and the analysis path does not.
        verdict = song_measurement_verdict(store, project_id, song.analysis, source)
        if verdict.current:
            return {"present": True, "reason": "", "envelope": verdict.envelope}
        if not verdict.recorded:
            # Never measured, so the interesting question is why nobody could — ffmpeg absent
            # from this machine, or a file ffmpeg will not decode, or simply not yet. Worked out
            # now, from this machine and this file, and never remembered.
            return {"present": False, "reason": analysis_absence_reason(source)}
        return {"present": False, "reason": verdict.reason}

    async def run_tool(
        args: list[str],
        on_progress: Callable[[int], None] | None = None,
        on_start: Callable[[asyncio.subprocess.Process], None] | None = None,
        cwd: Path | None = None,
    ) -> tuple[int, str, str]:
        """One ffmpeg/ffprobe invocation, event loop left free. Returns (rc, stdout, stderr).

        `vram.py`'s subprocess pattern: `create_subprocess_exec` (never a shell), stdin
        closed, both streams captured. A missing binary reads as a failed run with the
        absence in stderr rather than a 500 — ffmpeg not being on PATH is an environment
        fact the job's error field should carry in words.

        With `on_progress`, stdout is read a line at a time — that is `-progress pipe:1`'s
        channel, and reading it after the process exits would report nothing until there is
        nothing left to report — **while a concurrent task drains stderr**. That task is the
        whole point of the second branch: ffmpeg writes diagnostics to stderr throughout,
        the pipe holds ~64 KB, and a parent that reads one pipe to completion before
        touching the other deadlocks a long export against a full buffer with neither side
        able to move. `communicate()` does this drain for the no-progress branch already,
        which is why that branch is left exactly as it was.

        `on_progress` receives elapsed *output microseconds*, and is called only for lines
        that carry one; `parse_progress_us` refuses everything else, so a garbled or partial
        line costs a callback, not the export.

        `on_start` hands the caller the live `Process` the moment it exists, and exists for
        exactly one caller: the preview render, which AD-24 requires be **cancellable** while
        it runs. Killing an ffmpeg is the only way to stop one, and a caller that only ever
        sees the return value has nothing to kill. It is called before the first `await` on the
        process, so a supersede that arrives during the render always finds a handle — and the
        window before this line is closed by the preview's own publish gate rather than here,
        because a subprocess that has not been spawned yet cannot be killed by anyone.

        **`cwd` is what makes a `sendcmd` script reachable, and `None` — the default — is what
        every other invocation here gets: this process's own directory, exactly as before.** A
        drive script is passed to ffmpeg as a *bare relative filename* and read against the
        process's working directory (R-30, `effects.DriveScript.filename`). That is a choice
        rather than an escape from a parser bug: re-measured 2026-08-27, a plain forward-slash
        absolute path renders fine and AD-22's stated cause was false. It stays relative because a
        generated name is `[a-z0-9_.-]` and needs no escaping at all, and because an absolute path
        inside the composed chain is an absolute path inside `preview_fingerprint`'s fourth
        input — a project directory that moved would then invalidate every preview it owns.

        Two callers pass one, and they are the two this application renders with: the export,
        whose scripts go beside `clips.txt` in its own `workdir`, and the preview, whose scripts
        go in `previews/`. Every probe and every unbound render passes nothing, so the argv *and*
        the environment of a Shot with no bindings are what they were.

        `Path` rather than `str` so a caller cannot pass a directory that does not exist without
        the type saying what it is; `create_subprocess_exec` raises `NotADirectoryError` for one
        that is missing, which is not a `FileNotFoundError` and is deliberately not caught below —
        it means this application failed to write a file it was about to read, which is a fault in
        this application and not a fact about the environment.
        """
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
        except FileNotFoundError as missing:
            # `FileNotFoundError` carries two very different faults on Windows and this handler
            # used to report both as the same one. `WinError 206` is the command line being too
            # long — the binary is there and working, and the argv this application built is what
            # cannot be passed. Telling a Director their ffmpeg is missing sends them to install
            # something they already have, over a fault entirely of this application's making.
            # Measured 2026-08-25: 1200 grain cards build a 40,060-character argv against the
            # 32,767 Windows allows, and it surfaced as "ffmpeg is not installed".
            if getattr(missing, "winerror", None) == _COMMAND_LINE_TOO_LONG:
                return 127, "", TOOL_COMMAND_TOO_LONG.format(tool=args[0], length=len(" ".join(args)))
            return 127, "", f"{args[0]} is not installed or not on PATH"
        if on_start is not None:
            on_start(process)
        if on_progress is None:
            out_bytes, err_bytes = await process.communicate()
            out = out_bytes.decode("utf-8", "replace")
            err = err_bytes.decode("utf-8", "replace")
        else:
            assert process.stdout is not None and process.stderr is not None
            draining = asyncio.create_task(process.stderr.read())
            lines: list[str] = []
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace")
                lines.append(line)
                microseconds = parse_progress_us(line)
                if microseconds is not None:
                    on_progress(microseconds)
            err = (await draining).decode("utf-8", "replace")
            await process.wait()
            out = "".join(lines)
        code = process.returncode if process.returncode is not None else -1
        return code, out.strip(), err.strip()

    # Imported here rather than beside the other imports at the top of this module: each
    # route module imports *this* one for the request models, refusal sentences and helpers
    # its routes are written against, and a module-scope import in both directions is a
    # cycle. `create_app` runs after this module is fully defined, so by the time this line
    # executes there is nothing circular left to trip over.
    from .routes.assets import register as register_assets_routes
    from .routes.project import register as register_project_routes
    from .routes.render import register as register_render_routes
    from .routes.shots import register as register_shots_routes
    from .routes.song import register as register_song_routes
    from .routes.timeline import register as register_timeline_routes
    from .routes.unsorted import register as register_unsorted_routes

    # The one contract the route modules are handed. Everything a moved route used to reach
    # through this closure is named here, so the set is enumerable instead of implicit --
    # see `routes/context.py` for what each field is and why it is a value and not a
    # `Depends`. Each module registers straight onto `app`, so `app.routes` stays the flat
    # list this repository's three route-enumerating guards walk.
    # ------------------------------------------------------------------------------
    # The preview helpers, declared **above** `RouterContext` because two modules now
    # need them. `render_shot_preview` is pinned in this file by the tests that patch its
    # neighbours' names, and the boundary preview is a new route and therefore lives in
    # `routes/shots.py` -- so the take memo, the project's delivery plan, the halving rule
    # and the envelope read are shared rather than owned by either. That is exactly the
    # question `RouterContext`'s docstring asks of a new field, answered the way
    # `song_envelope_report` answered it: two resources read these and neither owns them.
    # ------------------------------------------------------------------------------

    async def take_measurement(source: Path) -> tuple[int, int, float | None] | None:
        """One take's `(width, height, seconds)` by ffprobe, remembered for the life of the
        process.

        `None` for a file that cannot be measured — missing, truncated, or not a video — which
        is an answer rather than an error: the callers below drop such a take from the plan
        exactly as the export's own refusal report would.

        The length rides along because `probe_take_args` reads it in the same probe the
        dimensions come from, and the preview needs both: the geometry decides what size to
        render, and the length decides whether the cut fits inside the take at all
        (`assembly.take_cut_refusal`). One probe answers both questions, as it does for the
        export. A container that reports no readable duration answers `None` for that third
        value alone and keeps its dimensions — an unmeasurable length is undecidable rather
        than a fault, which is what `assembly_refusals` has always done with one.

        The memo is keyed by path, byte length and modification time **together**. Neither half
        is trusted alone: a take re-rendered under the same name changes at least one of them,
        and this is a memo of a measurement rather than a stored verdict, so a stale entry could
        only be produced by a file rewritten to the same length in the same nanosecond.
        `song_fingerprint`'s content-not-mtime rule answers a different question — "is this still
        the same audio?" — and reading every byte of every approved take to answer "how wide is
        it?" would cost more than the measurement it guards.
        """
        try:
            stat = source.stat()
        except OSError:
            return None
        key = (source.as_posix(), stat.st_size, stat.st_mtime_ns)
        remembered = app.state.take_measurements.get(key)
        if remembered is not None:
            return remembered
        rc, out, _err = await run_tool(probe_take_args(source))
        lines = out.splitlines() if rc == 0 else []
        try:
            width, height = (int(part) for part in lines[0].split(","))
        except (ValueError, IndexError):
            return None
        try:
            seconds: float | None = float(lines[1])
        except (ValueError, IndexError):
            seconds = None
        measured = (width, height, seconds)
        app.state.take_measurements[key] = measured
        return measured

    async def preview_assembly(
        project: Project,
        transitions: Mapping[str, TransitionChoice] | None = None,
    ) -> AssemblyPlan | None:
        """The plan the export would build for **this project**, or `None` if no approved take in
        it can be measured.

        AD-29, and it is computed by calling `assembly_plan` rather than by re-deriving its rule:
        "the largest-area approved take" is a sentence, and a sentence copied into a second
        function drifts from the one that ships the video. Two things this delegation gets right
        that a `max()` here would not — the plan resolves overlaps first, so a take completely
        covered by a later Shot contributes nothing to a geometry it will not appear at; and if
        the normalization rule is ever changed, the preview follows it without anyone remembering
        that a second copy exists.

        `song_seconds` is `0.0`. Nothing here reads the plan's tiling: the geometry is `.width`
        and `.height`, and the boundary preview reads `.clips` and `.frames`, which the tiling
        does not decide.

        Shots whose take cannot be resolved or measured are left out entirely. That is the same
        set the export would refuse over, so the answer is either the export's own plan or the
        export was never going to run.

        **`transitions` changes what the plan *emits* and never what it measures** (R-39). A
        `TransitionClip` is a union entry in `plan.clips` and the width and height are taken from
        the resolved windows, which the transition pass runs after and does not touch — so
        `export_geometry` below gets the identical answer whether it asks with transitions or
        without, and a boundary preview gets the export's own segment rather than a second
        arithmetic for one.
        """
        output_root = (settings.comfy_root / "output").resolve()
        clips: list[ClipWindow] = []
        dimensions: dict[str, tuple[int, int]] = {}
        for shot in project.shots:
            if not shot.approved_output:
                continue
            candidate = (output_root / Path(shot.approved_output)).resolve()
            if output_root not in candidate.parents or not candidate.is_file():
                continue
            measured = await take_measurement(candidate)
            if measured is None:
                continue
            dimensions[shot.id] = measured[:2]
            clips.append(
                ClipWindow(
                    shot_id=shot.id,
                    # The label a refusal names a Shot by, and it is `shot_label`'s rather than
                    # the bare id: `plan.transition_refusals` is a Director-facing sentence and
                    # the boundary preview reports it whole (R-37). Nothing else here reads it.
                    label=shot_label(project, shot),
                    start=shot.start,
                    duration=shot.duration,
                    approved_output=shot.approved_output,
                    approved_start=shot.approved_start,
                    approved_duration=shot.approved_duration,
                    source=candidate,
                )
            )
        if not clips:
            return None
        # **`None` for a geometry `assembly_plan` cannot answer, which is this function's own
        # idiom** (2026-08-31). `AssemblyGeometryError` is addressed to a Director, and it reached
        # both of this module's call sites as a bare `ValueError` that neither caught -- an HTTP
        # 500 and a stack trace where a sentence was meant. Here there is already a `None` for
        # "no approved take in this project can be measured", every caller handles it, and a plan
        # that lays no frame at all is the same absence arriving a step later. The export says the
        # sentence out loud instead; a preview is a picture, and there is no picture.
        try:
            return assembly_plan(clips, 0.0, dimensions, transitions)
        except AssemblyGeometryError:
            return None

    async def export_geometry(project: Project) -> tuple[int, int] | None:
        """The dimensions the export would normalize **this project** to, or `None` if no
        approved take in it can be measured. AD-29, one question of the plan above.
        """
        plan = await preview_assembly(project)
        return None if plan is None else (plan.width, plan.height)

    def preview_envelope(
        project_id: str, project: Project, *, label: str
    ) -> dict[str, Any]:
        """The Song Envelope a **driven** preview is composed against, or the export's own
        refusal.

        Extracted 2026-08-29 so the Shot preview and the boundary preview cannot come to two
        answers about one measurement (story 11.5). A boundary blends two Shots and either of them
        may carry a Parameter Binding, so the question is asked at two routes now; asking it twice
        in two places is the shape this repository has already paid for five times.

        **Only ever called for a picture that asks the song something.** The verdict hashes the
        whole master and reads a ~405 KB sidecar, and a preview is measured in tens of
        milliseconds — so an unbound Shot must not pay it and its fingerprint must come out of
        exactly the arguments it came out of before. The gate is the caller's (`stack_is_driven`,
        whose client-side counterpart `api.stackIsDriven` is pinned to it by a cross-engine test).

        A song whose file has gone is a *reason*, not a 404: `song_measurement_verdict` already
        answers `SONG_ANALYSIS_MEDIA_MISSING` for a path it cannot `stat`, and that sentence sends
        a Director somewhere useful where "Song media was not found" would read as a fault in the
        preview. So the resolver's refusal becomes a path that does not exist and the verdict says
        the rest.

        The refusal is the **export's** sentence, whole. A preview is the export's promise, so the
        two may not refuse one state in two wordings — and refusing is the only outcome with a
        symptom: an undriven render succeeds and looks like a still look, at rc 0, with nothing in
        the response saying the music was dropped.
        """
        try:
            source_song = (
                resolve_song_path(project_id, project.song)
                if project.song
                else store.project_dir(project_id) / "song-that-was-never-imported"
            )
        except HTTPException:
            source_song = store.project_dir(project_id) / Path(project.song.path or "song")
        verdict = song_measurement_verdict(
            store,
            project_id,
            project.song.analysis if project.song else SongAnalysis(),
            source_song,
        )
        if not verdict.current:
            raise HTTPException(
                status_code=422,
                detail=BINDING_WITHOUT_ENVELOPE_REFUSAL.format(
                    shot=label, reason=verdict.reason
                ),
            )
        return verdict.envelope

    async def preview_into_cache(
        project_id: str,
        *,
        label: str,
        fingerprint: str,
        previews_root: Path,
        scripts: Sequence[DriveScript],
        argv: Callable[[Path], list[str]],
    ) -> bool:
        """Serve one Preview Clip out of the cache, or render it: AD-23, AD-24 and R-22 in one
        place. `True` when a render produced the clip -- its own, or the identical one it joined
        -- and `False` when the file named by `fingerprint` was already on disk.

        Extracted 2026-08-29 so the Shot preview and the boundary preview share the cache and the
        supersede registry rather than each keeping their own (story 11.5). **Sharing the registry
        is the correct reading of AD-24 rather than a convenience**: it is keyed by project because
        what it protects is one Director looking at one project, and a boundary render that let a
        superseded Shot render go on burning CPU beside it would be exactly the waste that ruling
        is about.

        **The cache, and the whole of it**: the fingerprint names a file, and the file is either
        there or it is not. Deleting the folder costs a re-render and nothing else, and no export
        ever reads this directory -- `exports/` is the assemble route's, and it builds its own
        intermediates from the approved takes every time.

        **Supersede, never queue** (AD-24). Whatever this project already has in flight with a
        *different* fingerprint is cancelled before this render starts, and the render writes to a
        scratch file that is published -- one atomic rename -- only if `PreviewRender.superseded`
        is still false when ffmpeg exits. Killing the subprocess is how a superseded render stops
        burning CPU; the gate is what makes it impossible for its output to be served, including
        in the two races a kill cannot cover -- the process that finished before the signal, and
        the process that did not exist yet.

        **Join an identical render, never restart it** (R-22). A request whose fingerprint equals
        the one already rendering is not stale work, it is the same work asked for twice, and
        there is no client that can promise never to ask twice: a retry, a poll and a re-render on
        window focus each produce one. So it waits on that render and answers with its result,
        spawning nothing.

        `argv` is a callable rather than a list because the scratch path is this function's own
        secret -- named by the render's token and hidden by a leading dot, so a half-written file
        is neither a cache entry nor a collision with the render that replaced it.
        """
        clip = previews_root / f"{fingerprint}.mp4"
        if clip.is_file():
            return False
        previews_root.mkdir(parents=True, exist_ok=True)
        renders = app.state.preview_renders
        in_flight = renders.get(project_id)
        # R-22, and the one comparison that decides between the two rules. A *different*
        # fingerprint is a different picture, so the render underway is stale work and AD-24
        # discards it. An *equal* fingerprint is this exact render, asked for twice -- and
        # restarting it would throw away completed effort to produce a byte-identical answer.
        # Worse, under identical requests arriving faster than a render completes, nothing would
        # ever land at all. So it joins.
        if in_flight is not None and in_flight.fingerprint == fingerprint:
            # No supersede, no second ffmpeg, no scratch file, and nothing published by this
            # request: it reads the outcome the render records and answers with it. The publish
            # gate is untouched -- a joiner can only ever be handed a clip that a render already
            # renamed into the cache after finding `superseded` false.
            waiter = in_flight.join()
            if waiter is not None:
                await waiter
            if in_flight.published:
                return True
            if in_flight.superseded:
                # What it was waiting for will never publish, so it is refused for the same
                # reason and by the same sentence: something newer is the one that answers.
                raise HTTPException(
                    status_code=409, detail=PREVIEW_SUPERSEDED_REFUSAL.format(shot=label)
                )
            raise HTTPException(
                status_code=502,
                detail=PREVIEW_FAILED_ERROR.format(
                    shot=label, detail=in_flight.error or PREVIEW_ABANDONED_DETAIL
                ),
            )
        record = PreviewRender(token=new_id("preview"), fingerprint=fingerprint)
        if in_flight is not None:
            in_flight.supersede()
        renders[project_id] = record
        scratch = previews_root / f".{record.token}.mp4"
        # The compiled drive scripts, into the cache folder this render is about to write its
        # clip into, and read from there as bare relative names with ffmpeg's working directory
        # set to it (R-30, `run_tool`'s `cwd`). Content-addressed like the clip beside them: the
        # name carries a digest of the script's own text, so writing one that is already there
        # rewrites identical bytes, and two renders that compile the same drive share a file.
        # Nothing here is a cache entry -- the clip is named by the fingerprint and only the clip
        # is served -- and emptying the folder costs a re-render exactly as it did before.
        for script in scripts:
            (previews_root / script.filename).write_text(script.text, encoding="utf-8")
        try:
            rc, _out, err = await run_tool(
                argv(scratch),
                on_start=record.attach,
                # Only when there is a script to read: a render with no drive spawns ffmpeg in
                # this process's own directory, exactly as it did before Epic 10.
                cwd=previews_root if scripts else None,
            )
            if record.superseded:
                # The gate. Whatever ffmpeg managed to write is deleted rather than published,
                # so a render cancelled at any point -- including one that finished before the
                # kill landed -- can never be served as the current picture.
                scratch.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=409, detail=PREVIEW_SUPERSEDED_REFUSAL.format(shot=label)
                )
            if rc != 0 or not scratch.is_file():
                scratch.unlink(missing_ok=True)
                record.error = err[-500:] if err else "no error output"
                raise HTTPException(
                    status_code=502,
                    detail=PREVIEW_FAILED_ERROR.format(shot=label, detail=record.error),
                )
            # One atomic rename into the cache. A reader either sees no file or sees a complete
            # one; there is no window in which a partial preview carries a fingerprint's name.
            #
            # Everything from `run_tool` returning to here is synchronous, and that is what lets
            # a joiner trust `published`: no supersede can be interleaved between reading the
            # gate and passing through it, so `published` is only ever true of a render that was
            # never superseded.
            scratch.replace(clip)
            record.finish(published=True)
            return True
        finally:
            # Only if this render is still the registered one. A superseded render must not
            # clear the entry belonging to the render that replaced it.
            if renders.get(project_id) is record:
                del renders[project_id]
            # Unconditional, and idempotent: whatever happened above -- a publish, a refusal, a
            # supersede, or an exception no branch here wrote -- every joiner is released with
            # the outcome that was recorded, or with `error=None` and `published=False`, which is
            # the abandoned case and still an answer. Nothing waits forever.
            record.finish(error=record.error)

    def preview_side(value: int) -> int:
        """One export dimension, halved for the preview and kept even.

        Even because `format=yuv420p` — which `trim_args` pins on every clip it builds — has
        half-resolution chroma planes and refuses an odd dimension. Every size this pipeline
        renders is a multiple of 32, so the rounding never fires on real media; it is here so
        that a hand-placed take of an odd width is a smaller preview rather than an ffmpeg
        failure with a sentence about chroma. Never below 2, for the same reason.
        """
        half = value // 2
        return max(2, half - (half % 2))

    ctx = RouterContext(
        app=app,
        settings=settings,
        store=store,
        comfy=comfy,
        director=director,
        ejector=ejector,
        preferences=preferences,
        transcriber=transcriber,
        catalog=catalog,
        render_progress=render_progress,
        eject_pinned_by_environment=eject_pinned_by_environment,
        get_project=get_project,
        get_project_for_update=get_project_for_update,
        settle_unsubmitted_jobs=settle_unsubmitted_jobs,
        record_submission=record_submission,
        resolve_asset_path=resolve_asset_path,
        resolve_song_path=resolve_song_path,
        analyze_song_for_project=analyze_song_for_project,
        analyze_a_landed_song=analyze_a_landed_song,
        song_envelope_report=song_envelope_report,
        discovered_looks=discovered_looks,
        run_tool=run_tool,
        take_measurement=take_measurement,
        preview_assembly=preview_assembly,
        preview_envelope=preview_envelope,
        preview_into_cache=preview_into_cache,
        preview_side=preview_side,
    )
    register_project_routes(ctx)
    register_song_routes(ctx)
    register_shots_routes(ctx)
    register_assets_routes(ctx)
    register_render_routes(ctx)
    register_timeline_routes(ctx)
    register_unsorted_routes(ctx)








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
        # `kind=NOTE_KIND_PROMPT` because this gate raises the *prompt* refusal. Since 2026-08-21
        # the report also blocks a stale reference map — so that the pre-flight stops calling a
        # shot submittable that this route then turns away — and an unfiltered read here would
        # answer such a shot with `READINESS_REFUSAL`'s "no prompt on SHOT 07", about a shot that
        # has a prompt, while its own refusal sits three checks below. One rule, one sentence.
        if shot.id in readiness_report(project, include_warnings=False).blocked_ids(
            kind=NOTE_KIND_PROMPT
        ):
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
        # Then whether the prompt cites a reference slot this shot does not have. Before the
        # payload and before ComfyUI, for the reason every other refusal on this route is: H3's
        # media slots are anonymous, so `<Picture 3>` on a two-picture shot is not an error the
        # sampler can report — it renders, plausibly, conditioned on nothing. Only the blocking
        # half runs here; an attached-but-unmentioned picture is a warning and reaches the
        # Director through the expansion's advisory list, never as a refusal.
        #
        # The text checked is what the submission actually sends: the stored expansion when there
        # is one, the intent when there is not. The fallback's reference map is built from these
        # same counts, so it can only ever cite slots that exist — which is why checking the
        # intent rather than the assembled `Reference map: …` string loses nothing.
        #
        # `None` from `reference_slot_counts` means the count is not trustworthy for this shot and
        # the check is skipped entirely; see that function for the two cases.
        if (slots := reference_slot_counts(project, shot)) is not None:
            blocking = [
                problem.message
                for problem in check_reference_bounds(
                    shot.h3_prompt if shot.h3_prompt.strip() else shot.prompt, slots=slots
                )
                if problem.fatal
            ]
            if blocking:
                raise HTTPException(
                    status_code=422,
                    detail=REFERENCE_BOUNDS_REFUSAL.format(
                        shot=shot_label(project, shot), problems=" ".join(blocking)
                    ),
                )
        # Then whether the stored expansion still describes the references this shot cites. After
        # the bounds check because the bounds refusal is the sharper sentence when both apply — a
        # prompt naming `<Picture 3>` on a two-picture shot has a concrete tag to fix — and before
        # the payload and ComfyUI, for the reason every other refusal on this route is.
        #
        # **This is the guarantee, and it is a refusal rather than a rewrite.** The free half of
        # staleness is already gone by the time a submission happens: `refresh_reference_maps` runs
        # on every route that can move a citation, a label or an anchor. What is left is the half
        # that would cost a model call nobody asked for, plus the two shots nothing automated may
        # write — and a render is the wrong moment to spend either. `generate_batch` submits
        # through this same handler, so its per-shot skip carries this sentence too.
        if stale_reference_map(project, shot):
            raise HTTPException(
                status_code=422,
                detail=STALE_REFERENCE_MAP_REFUSAL.format(shot=shot_label(project, shot)),
            )
        # The sync-correct offset of the take the submission below will produce — nonzero
        # only when the reference branch extends a song-audio window ahead of the shot.
        # Written onto the Shot with `prompt_id` at submission; see `Shot.latest_take_lead`.
        take_lead = 0.0
        # What this submission will be recorded as having sampled on, decided here so that no
        # branch has to remember to write it and a branch that says nothing cannot leave the job
        # claiming to predate the field — which is what `None` means and would be a lie about a
        # render submitted today.
        #
        # `NO_EVIDENCED_BUNDLE` is the honest default rather than a placeholder: the keyframe and
        # text-only branches below *refuse* a named bundle and render their own way, so "this
        # graph has none" is precisely true for both, and it stays true for any fourth adapter,
        # which cannot reach this route at all until the import-time check beside `H3_ADAPTERS`
        # is satisfied by a branch someone wrote deliberately.
        bundle = SamplingBundle(name=NO_EVIDENCED_BUNDLE)
        if spec.adapter == "h3-reference":
            references: list[dict[str, Any]] = []
            tags: list[str] = []
            # Every citation this mode declares, numbered by `models.numbered_references` — which
            # walks `citations_in_prompt_order`, which for a reference-only Shot is the
            # reference-role citations in order, which the model guarantees is `asset_ids` in order
            # for every Shot that has ever been saved — see `Shot._reconcile_citations`. Read from
            # the citations rather than from the flat list because the citations are the truth: a
            # Shot whose wolf has been given the middle-frame role must stop sending it as
            # reference picture three, and `asset_ids` is the projection that stops naming it.
            #
            # One walk numbers the tags *and* appends the media, deliberately: the payload fills
            # its per-kind slots in list order, so `<Picture N>` in the map is the Nth picture
            # appended here and nothing else. `reference_map_tag_lines`, `reference_slot_counts`
            # and `shot_expansion_input` call that same function, which is what keeps the four
            # numberings — map, slots, expansion, payload — one numbering. The
            # `mode_specification_problems` gate above has already refused any role this mode does
            # not declare, so only `reference`, `first` and `last` can reach this loop.
            for numbered in numbered_references(project, shot):
                citation = numbered.citation
                asset = numbered.asset
                if not asset:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Unknown reference asset: {citation.asset_id}",
                    )
                label = shot.reference_labels.get(asset.id, asset.name)
                # The prose half of the label: the same name, carrying the Asset's stored
                # appearance anchor when it has one. `reference_map_tag_lines` composes it
                # identically and the two must stay byte-for-byte the same sentence — the
                # stored expansion and the submitted map are supposed to name the same slots.
                #
                # The anchor rides the *tag line only*, never `references[].label` below. That
                # label names a media slot for a ComfyUI-side reader; the tag line is the prose
                # the sampler is conditioned on, and only the second one is a description.
                tag_label = anchored_label(asset, label)
                if citation.role in REFERENCE_MAP_ROLE_TAGS:
                    # A keyframe riding the reference graph, per the guide's §2.2.2: the picture
                    # travels as an ordinary reference slot — no new node input, no graph change —
                    # and *only this tag line* makes it the shot's first or last frame. It counts
                    # against the same 9-picture ceiling as any other picture, in
                    # `build_h3_reference_payload`, which is where the per-kind limits live.
                    if asset.kind in ("audio", "video"):
                        raise HTTPException(
                            status_code=422,
                            detail=REFERENCE_KEYFRAME_NOT_IMAGE.format(
                                role=ASSET_ROLE_LABELS[citation.role],
                                name=asset.name,
                                article="an" if asset.kind == "audio" else "a",
                                kind=asset.kind,
                            ),
                        )
                    references.append(
                        {
                            "kind": "picture",
                            "file": str(resolve_asset_path(project_id, asset)),
                            "label": label,
                        }
                    )
                    tags.append(
                        REFERENCE_MAP_ROLE_TAGS[citation.role].format(
                            number=numbered.number, label=tag_label
                        )
                    )
                    continue
                reference: dict[str, Any] = {
                    "kind": numbered.kind,
                    "file": str(resolve_asset_path(project_id, asset)),
                    "label": label,
                }
                if numbered.kind == "video":
                    # **A cited video travels as a picture reference and nothing more.** Said in
                    # the payload rather than left to the loader's default, because until today
                    # this route sent neither `has_audio` nor `audio_mode` and the node's
                    # `bool(item.get("has_audio"))` therefore read false every time — the
                    # soundtrack was never conditioned and nothing anywhere said so. The
                    # behaviour is unchanged and now it is a decision on the wire.
                    #
                    # It is off rather than on for two reasons, in order of weight. A reference
                    # shot is *performed against the master song*: `use_song_audio` appends that
                    # song as an audio reference and the whole over-render window exists to make
                    # the take land on real song seconds. A clip's own soundtrack conditioned
                    # alongside it is a second, unrelated piece of music in the same pass, and
                    # turning that on by default would silently change what every existing
                    # citation renders. And there is nothing to decide it with: no Asset field
                    # records whether a video has an audio stream, no control offers the
                    # paired/standalone choice the node reads, and inventing both here would be
                    # guessing at a creative decision that is the Director's.
                    #
                    # The Director is told, rather than left to infer it from a silent payload:
                    # `batch.video_soundtrack_note` reports it per shot in the readiness list.
                    reference["has_audio"] = False
                references.append(reference)
                tags.append(f"{numbered.tag} is {tag_label}")
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
                    # The *shot's own* window first, for its refusal: a window past the end
                    # of the song is refused here in the same words as ever, before any GPU
                    # time. The trim actually sent is then the over-rendered one below.
                    song_audio_window(
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
                # The over-render margin (spec-monitor-and-over-render): the picture runs
                # ~half a second past the window, and the conditioning audio extends with
                # it — up to a quarter second *before* the window when the song allows —
                # so the whole take is performed against real song seconds and editable
                # room exists at either end. `take_lead` is the sync-correct offset the
                # submission write records on the Shot; the Monitor and assembly both cut
                # there by default.
                picture_seconds = over_render_frames(shot.duration) / H3_FPS
                take_lead = over_render_lead(
                    start=shot.start,
                    duration=shot.duration,
                    picture_seconds=picture_seconds,
                    song_duration=project.song.duration,
                )
                # The take's own seconds of the song, through the one function that expresses
                # them (`over_render_window`). The whole-song edge lives in there: no room
                # either side, so the file simply ends before the picture does — rendered with
                # the mismatch, exactly as every pre-margin render behaved, never silently
                # shortened elsewhere. `restore_song_audio` calls the same function with the
                # lead this submission is about to record, so the seconds conditioned and the
                # seconds restored are one computation rather than two that agree.
                trim_start, trim_end = over_render_window(
                    start=shot.start,
                    lead=take_lead,
                    picture_seconds=picture_seconds,
                    song_duration=project.song.duration,
                )
                references.append(
                    {
                        "kind": "audio",
                        "file": str(resolve_song_path(project_id, project.song)),
                        "label": "master song",
                        "trim": {"start": trim_start, "end": trim_end},
                    }
                )
                # One past every cited audio, from `song_audio_tag` — the same number the
                # expansion input tells the specialist and `reference_map_tag_lines` writes.
                tags.append(
                    f"<Audio {song_audio_tag(project, shot)}> is the master song "
                    "for synchronization"
                )
            # The Director's standing choice unless this submission named one, resolved once and
            # used twice: the graph is built on it and the job records it. Two reads of one
            # decision rather than two decisions — the shape `resolved_sampling_profile` itself
            # exists to enforce, applied one level down, so a take's recorded provenance cannot
            # name a bundle other than the one its graph was built from.
            profile = resolved_sampling_profile(request.profile, project)
            # Inside the `try`, with the builder: both call `resolved_h3_sampling`, so both refuse
            # an unknown profile or a step count below one in the same words, and that refusal has
            # to reach the Director as this route's 422 rather than as a 500. Before the builder,
            # so a submission is never recorded as having run a bundle whose graph was refused.
            try:
                bundle = submitted_sampling_bundle(profile, request.steps)
                payload = build_h3_reference_payload(
                    prompt=reference_prompt(
                        shot,
                        tags,
                        section_prompt=(
                            section.prompt
                            if (section := song_section(project, shot)) is not None
                            else ""
                        ),
                        vocal_overlap=shot_vocal_overlap(
                            project.song, start=shot.start, duration=shot.duration
                        ),
                    ),
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
                    # Resolved above, and the same value `bundle` records. The single place a
                    # bundle is decided, so the batch and "Render Again" cannot ship different
                    # graphs for the same project. See `resolved_sampling_profile`.
                    profile=profile,
                    prefix=f"music-video-producer/{project_id}/shots/{shot.id}-h3-reference",
                )
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
        elif spec.adapter == "h3-keyframe":
            # A sampling profile names a *reference*-graph configuration, and this branch
            # builds the first/last keyframe graph on the `fl2va` checkpoint, which has no
            # evidenced profile: the turbo exports are T2V, Director and References2V graphs
            # on other checkpoints, and blending one across is marked Ask First and not asked.
            # Refused rather than ignored, for the text-only branch's exact reason: a GPU job
            # logged under a configuration that was never applied is worse than a refusal,
            # because only the refusal is visible.
            #
            # `is not None` is the whole of the difference between a *named* profile and an
            # *inherited* one, and it is deliberate. A named bundle this graph cannot apply is
            # still refused, word for word as before. A project-wide `sampling_profile` is not a
            # request about this shot: it is a standing preference that only the reference graph
            # has evidence for, so a keyframe shot in a turbo project renders on its own bundle
            # rather than being skipped. Refusing it would make choosing turbo quietly drop every
            # keyframe shot out of Generate All, which is a worse silence than the one this
            # branch exists to prevent — and the control names the scope where it is set.
            if request.profile is not None and request.profile != H3_DEFAULT_PROFILE:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"The {request.profile!r} sampling profile applies to reference "
                        f"shots only. {shot_label(project, shot)} renders through the "
                        f"MiniMax H3 first/last keyframe graph, which has no evidenced "
                        f"profile. Drop the profile."
                    ),
                )
            # The same refusal one field over, and stricter than the reference branch needs
            # to be: `ref_image_size` is a `MiniMaxH3ReferenceToVideo` input, and the
            # keyframe conditioner has no such input at all — live `/object_info` declares
            # only clip/vae/prompt/width/height/length plus the two optional frames — so a
            # non-default value here could only be silently dropped, logged as though it
            # had been applied.
            if request.ref_image_size != "match":
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"ref_image_size sizes reference media for the references graph. "
                        f"{shot_label(project, shot)} renders through the MiniMax H3 "
                        f"first/last keyframe graph, whose conditioner has no such input, "
                        f"so the value would not be sent. Drop the field."
                    ),
                )
            # The shot's frames, resolved from its citations **by role** — the same
            # citation-to-file resolution the reference branch does for its pictures, keyed
            # by `first`/`last` rather than by position, so a citation list that happens to
            # hold the last frame before the first one still renders the right way round.
            # The roles come off the mode's own table row: `image_to_video` declares only
            # `first`, `first_last` declares both, and the `mode_specification_problems`
            # gate above has already refused a shot whose counts do not match — which is
            # what makes the `[0]` below safe.
            frames: dict[str, str] = {}
            for requirement in spec.roles:
                citation = citations_in_role(shot, requirement.role)[0]
                role_label = ASSET_ROLE_LABELS[requirement.role]
                asset = next(
                    (item for item in project.assets if item.id == citation.asset_id), None
                )
                if not asset:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Unknown {role_label} asset: {citation.asset_id}",
                    )
                # The splitter routes media by the `kind` this branch writes into
                # `media_state`, and a frame travels as a picture. An audio or video Asset
                # cited as a frame would be fed to the loader under a kind it is not,
                # which nothing downstream reports — so it is refused here by name.
                if asset.kind in ("audio", "video"):
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"A {role_label} must be an image, and {asset.name} is "
                            f"{'an' if asset.kind == 'audio' else 'a'} {asset.kind}."
                        ),
                    )
                # `resolve_asset_path`'s own resolution and containment, with its 404 for a
                # vanished file translated to the 422 the keyframe matrix specifies — the
                # request names a Shot that exists, and what cannot be processed is the
                # state its manifest describes. The path is named so the refusal is
                # actionable rather than only true.
                try:
                    frames[requirement.role] = str(resolve_asset_path(project_id, asset))
                except HTTPException as error:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"{shot_label(project, shot)} cites {asset.name} as its "
                            f"{role_label}, but its media was not found at {asset.path}."
                        ),
                    ) from error
            try:
                payload = build_h3_keyframe_payload(
                    # `reference_prompt`'s rule without its reference map: an expansion is
                    # submitted **alone** — an H3-format prompt opens with its own
                    # instruction line (`H3_KEYFRAME_MODES` names both these modes for it)
                    # and prose in front would break the format. Without one, the shot's
                    # intent goes as written, exactly as the text-only path sends it.
                    prompt=shot.h3_prompt if shot.h3_prompt.strip() else shot.prompt,
                    first_frame=frames["first"],
                    # Absent for `image_to_video`, whose table row declares no `last` role;
                    # the builder then omits `last_frame` entirely, which the node's schema
                    # declares optional.
                    last_frame=frames.get("last"),
                    duration=shot.duration,
                    seed=shot.seed,
                    # The same geometry contract as the reference branch, resolved by the
                    # same `_resolve_frame`: an explicit width/height honoured exactly, the
                    # selector triple through `select_resolution`, an omission taking the
                    # measured 0.6 MP default, both kinds together refused.
                    width=request.width,
                    height=request.height,
                    megapixels=request.megapixels,
                    aspect_ratio=request.aspect_ratio,
                    multiple=request.multiple,
                    # `None` falls through to the export's own 20; see
                    # `H3_KEYFRAME_DEFAULT_STEPS`.
                    steps=request.steps,
                    prefix=f"music-video-producer/{project_id}/shots/{shot.id}-h3-keyframe",
                )
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
        else:
            # `h3-director`, and nothing else can reach here: the adapter gate above refuses `""`,
            # the two named branches take theirs, and the import-time check beside `H3_ADAPTERS`
            # refuses a table naming any fourth adapter this route has no branch for.
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
            #
            # `is not None` for the keyframe branch's reason, which is the same reason here: a
            # bundle the Director *named* on a text-only Shot is refused exactly as before, and a
            # project-wide standing choice is not a claim about this Shot. See that branch.
            if request.profile is not None and request.profile != H3_DEFAULT_PROFILE:
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
            # The over-render margin: the take runs at least half a second past the
            # window (spec-monitor-and-over-render). The timeline the Director node sees
            # is widened to the whole picture — window *and* the shot's own segment — so
            # the prompt governs the margin rather than leaving unprompted tail frames.
            picture_seconds = over_render_frames(shot.duration) / H3_FPS
            try:
                timeline = build_director_timeline(
                    [shot.model_copy(update={"duration": picture_seconds})],
                    window_start=shot.start,
                    window_duration=picture_seconds,
                    fps=24,
                )
            except TimelineError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            try:
                payload = build_h3_director_payload(
                    timeline_data=timeline.timeline_data,
                    duration=picture_seconds,
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
        # **The record first, then the graph** — the Director's 2026-08-21 ruling, and the
        # defect it closes: this route submitted and *then* saved, so once `ProjectStore.save`
        # gained its lost-update refusal a save race answered 409 for a graph already queued.
        # The GPU rendered, the take landed on disk, and nothing recorded it — no job, no
        # `latest_output`, no way for the application to find the file. Reversed, a save race
        # refuses here, before a byte reaches ComfyUI, which is the cheap direction to fail.
        #
        # The stated cost, accepted with the ruling: a record now briefly exists for a graph
        # that is not queued yet, so a crash in that window leaves an orphan. It carries
        # `PENDING_SUBMISSION_PROMPT_ID` rather than an empty id precisely so the reconciler
        # can settle it — see that constant, and `JOB_NEVER_SUBMITTED` for what it settles as.
        #
        # Only the job record goes in this save. Everything below is what the *acceptance*
        # means for the Shot, and none of it may be written for a submission that never
        # happened — the record itself is the in-flight marker in the meantime, because
        # `shot_render_in_flight` reads the job records as well as `Shot.status`.
        job = RenderJob(
            kind="h3",
            prompt_id=PENDING_SUBMISSION_PROMPT_ID,
            target_id=shot.id,
            seed=shot.seed,
            # What this render is actually being asked for, recorded at the one moment it is
            # true. All three H3 branches above resolve to the same count — the reference and
            # keyframe builders each call `over_render_frames(duration)` on `shot.duration`, and
            # the Director branch's `timeline.aligned_frames` is that same number arrived at
            # through `picture_seconds` — so this is the graph's length however the shot renders.
            #
            # Persisted because it cannot be recovered afterwards: `shot.duration` is edited
            # after a render (an edge drag, a snapped cut), so re-deriving the frame count later
            # describes a render that never happened. A duration with no frame count beside it
            # is what made the mtime reconstruction of 2026-08-21 as laborious as it was.
            render_frames=over_render_frames(shot.duration),
            # And what it is being asked for it *with*, recorded at the same one moment and for
            # the same reason: `Project.sampling_profile` is a standing choice the Director
            # changes between renders, so a project's takes are a mixture and nothing read later
            # can say which take ran on which bundle. Never `None` here — a submission this route
            # accepted always knows its bundle, and `None` is reserved for a job written before
            # any of this existed. See `RenderJob.sampling_bundle`.
            sampling_bundle=bundle,
        )
        project.jobs.append(job)
        store.save(project)
        try:
            submission = await comfy.submit(payload)
        except ComfyError as error:
            settle_unsubmitted_jobs(project_id, job)
            raise HTTPException(status_code=502, detail=str(error)) from error
        accept_submission(job, submission.prompt_id)

        def take_the_shot(fresh: Project) -> None:
            """What this accepted render means for the Shot, onto a **fresh** manifest.

            `project` was read before `comfy.submit` and saving it back would revert everything
            committed while `/prompt` answered — including, on a Generate All, the record and
            the shot state of the render submitted immediately before this one, because that
            loop calls this route once per shot. See `record_submission`.

            The window numbers are the *pre-await* ones deliberately: they describe the picture
            that was actually asked for, and the payload was built from the shot as it stood
            when this route read it. A Shot deleted while `/prompt` answered leaves the record
            standing and writes nothing — the graph is on the card, and there is no Shot left
            to mark queued.
            """
            live = next((item for item in fresh.shots if item.id == shot.id), None)
            if live is None:
                return
            live.status = "queued"
            live.prompt_id = submission.prompt_id
            # The take this job produces begins `take_lead` seconds before the shot's window
            # (0 for every non-song path). Recorded at the moment of truth because it cannot
            # be derived later; the Monitor, the nudge control and assembly all cut from it.
            live.latest_take_lead = take_lead
            # And the window it begins that far before, snapshotted with it (2026-08-21). The
            # lead alone does not describe a take: `start` and `duration` are edited afterwards
            # — dragging a rendered clip's left edge moves `start` while `trim_nudge`
            # compensates — and every number `restore_song_audio` reported was read off the
            # live window as though it were the take's. Two fields written where one already
            # was, in the same statement, so a take can never carry half a description. See
            # `Shot.latest_take_start`.
            live.latest_take_start = shot.start
            live.latest_take_duration = shot.duration
            # Job-record hygiene, after the accept and not a gate. Every refusal above stands
            # — in particular the `status != "ready"` one, which is what normally makes a second
            # render for a live shot impossible. It stopped being enough when a whole-manifest
            # write walked the status back underneath a live job; both generic writes now refuse
            # that (`_require_in_flight_status_kept`, 2026-08-20), so no shipped route produces
            # the state any more. It is still reachable by a manifest edited on disk, restored
            # from a backup, or saved by a build older than the gate, and in that state the older
            # record is not merely untidy: `apply_job_history` adopts by `target_id`, so a late
            # answer to it would move `latest_output` back onto the older take and drop the newer
            # one's review with it. Read from the fresh manifest, so a record written while
            # `/prompt` answered is seen. See `batch.supersede_target_jobs`.
            supersede_target_jobs(
                fresh, kinds={"h3"}, target_id=shot.id, keep_job_id=job.id
            )

        record_submission(project_id, job, patch=take_the_shot)
        return job

    @app.post(
        "/api/projects/{project_id}/generate/batch",
        response_model=BatchSubmissionResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def generate_batch(
        project_id: str, request: GenerateBatchRequest
    ) -> BatchSubmissionResponse:
        """FR-4: every eligible shot as one batch, one confirmation, per-shot skip-and-report.

        Every submission rides the *identical* single-shot handlers — `render_again` to
        re-open a settled shot, `generate_h3` to submit — called in-closure, so no gate,
        payload rule, or refusal wording exists twice. A shot whose submission refuses
        lands in `skipped` with that route's own sentence and blocks nothing else, which
        is FR-4's testable consequence verbatim.

        FR-9 by construction: every submission is kind `h3`, they go out consecutively in
        timeline order, and nothing on this path issues a ComfyUI free, unload, or
        interrupt (the LM-Studio eject before the first submit is AD-8's control, on the
        other host). The measured fact FR-9 reduces to — ComfyUI keeps the stack resident
        between same-kind prompts — is preserved exactly because nothing else is
        interleaved.

        The confirmation is server-enforced: `confirm_gpu` false answers 422 with the
        exact count that would queue, so a client that never showed the warning cannot
        spend hours of GPU by omission (AD-15: expensive renders require explicit
        confirmation; one confirmation covers a batch).

        AD-5's bookkeeping happens after the loop on a fresh read: the submitted jobs are
        stamped with one freshly-minted `batch_id` (a batch is the set of jobs sharing
        it, active iff any member is non-terminal — derived, never stored), and each
        successfully resubmitted shot's `flagged` clears — success only; a skip keeps
        the flag, and the batch draining never touches it.
        """
        project = get_project(project_id)
        targets, protected = batch_targets(
            project, scope=request.scope, replace_existing=request.replace_existing
        )
        if not targets:
            raise HTTPException(
                status_code=422,
                detail=GENERATE_BATCH_EMPTY_BY_SCOPE.get(
                    request.scope, GENERATE_BATCH_EMPTY_READY
                ),
            )
        if not request.confirm_gpu:
            raise HTTPException(
                status_code=422,
                detail=GENERATE_BATCH_CONFIRM_REFUSAL.format(count=len(targets)),
            )
        batch_id = new_id("batch")
        submitted: list[BatchSubmittedShot] = []
        skipped = [
            BatchSkippedShot(shot_id=shot.id, label=shot_label(project, shot), reason=reason)
            for shot, reason in protected
        ]
        for target in targets:
            label = shot_label(project, target)
            try:
                # The `empty` scope's one extra step, and the only place drafts are armed by
                # anything but a click on that shot. It rides `mark_shot_ready` in-closure for
                # exactly `render_again`'s reason two lines below: the arming refusals — locked,
                # approved, already-rendered, a live job (409), and the readiness prompt gate —
                # are that route's own, in that route's own words, so this batch cannot say yes
                # to a shot a lone Mark ready would turn away. A refusal lands in `skipped` by
                # name through the same `except` and blocks nothing else in the batch.
                #
                # Scoped to `empty` deliberately. `ready` cannot reach a draft at all, and a
                # draft in the `flagged` scope goes on meeting `generate_h3`'s "must be ready"
                # exactly as it did before this change — that scope is unchanged, by construction
                # rather than by intention.
                #
                # `mark_shot_ready` is "never automatic" and stays so: this is one deliberate,
                # confirmed act by the Director, on a set they were shown the count of, in the
                # same shape `Mark all drafts ready` already promotes a whole plan with. What the
                # rule forbids is a *side effect* — a populate or an expansion arming shots
                # nobody asked to arm — and nothing here is one.
                if request.scope == "empty" and target.status == "draft":
                    mark_shot_ready(project_id, target.id)
                # A settled shot re-opens through the same route a lone click uses; its
                # refusals (in-flight, locked, approved, the prompt gate re-asked) are
                # the batch's refusals, in the same words.
                if target.status in ("complete", "error"):
                    render_again(project_id, target.id)
                    # A re-render at the same seed and prompt reproduces the identical take while
                    # ComfyUI keeps the model resident — measured 2026-08-23, all 141 frames
                    # byte-identical across two repeats, and re-rolled only when ComfyUI evicted
                    # and reloaded the model unprompted between them. The stride does not depend
                    # on which of those happens: a byte-identical resubmission is also served
                    # straight from ComfyUI's execution cache in ~1.2 s with no sampling at all,
                    # so without the stride this loop can spend no GPU and still report a take.
                    # The fixed-seed trap the roadmap already recorded for Flux,
                    # met again by the flag/replace loop (the run-2 audit). The stride
                    # lands here, in the batch route, because render_again's contract is
                    # pinned as "writes exactly one field"; a lone-click re-render keeps
                    # its seed on purpose (comparisons want it), while resubmitting a
                    # rejected take is asking for a different one.
                    fresh = get_project(project_id)
                    for candidate in fresh.shots:
                        if candidate.id == target.id:
                            candidate.seed += RESUBMIT_SEED_STRIDE
                            store.save(fresh)
                            break
                # `request.profile` is `None` for every client that does not override, and
                # `generate_h3` resolves that to `Project.sampling_profile`. So the batch renders
                # the bundle the Director chose, and it is the *same* resolution the per-shot
                # re-render gets, because it is the same call. Until 2026-08-23 this route sent
                # `"default"` (20 steps) while `app.js` hardcoded `"turbo"` (4) one button over.
                job = await generate_h3(
                    project_id, target.id, H3Request(profile=request.profile)
                )
            except HTTPException as refusal:
                skipped.append(
                    BatchSkippedShot(
                        shot_id=target.id, label=label, reason=str(refusal.detail)
                    )
                )
                continue
            submitted.append(
                BatchSubmittedShot(shot_id=target.id, label=label, job_id=job.id)
            )
        # One fresh read for the bookkeeping: the loop's handlers each saved, so this
        # patch must land on the manifest as it now stands, not on the pre-loop copy.
        if submitted:
            fresh = get_project(project_id)
            submitted_jobs = {entry.job_id for entry in submitted}
            submitted_shots = {entry.shot_id for entry in submitted}
            for job in fresh.jobs:
                if job.id in submitted_jobs:
                    job.batch_id = batch_id
            for shot in fresh.shots:
                if shot.id in submitted_shots and shot.flagged:
                    shot.flagged = False
            store.save(fresh)
        return BatchSubmissionResponse(
            batch_id=batch_id if submitted else "",
            submitted=submitted,
            skipped=skipped,
        )


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


    @app.post(
        "/api/projects/{project_id}/shots/{shot_id}/preview",
        response_model=ShotPreviewResponse,
    )
    async def render_shot_preview(project_id: str, shot_id: str) -> ShotPreviewResponse:
        """One Shot's exposed window, through the chain the export will run, at half its size.

        The point of the whole slice: judging a grade used to mean exporting the entire video,
        so a Director graded against imagination. What comes back is the same picture the export
        will produce, reduced in geometry and in encoder quality and **differing in nothing
        else** — `assembly.trim_args` builds the argv and `effects.build_effect_stages` composes
        the stages, exactly as the assemble route composes them, because a preview built by a
        second chain stops predicting the export, and predicting the export is the only thing it
        is for.

        **Nothing here is written to the manifest, and that is a load-bearing absence** (AD-23).
        There is no stored "stale" flag, no cached geometry on the Shot, no record that a preview
        exists. `preview_fingerprint` names the file, and a name either exists on disk or does
        not; a state that is derived cannot outlive the thing it describes. `store.save` appears
        nowhere in this route on any path, refusal or success.

        **The order of the work is the order of the refusals it can raise.** The Shot, then the
        approval, then the file, then the geometry, then whether the cut lands inside the take,
        then the chain — each the cheapest remaining question whose answer could make the next
        one meaningless. The cut question is the export's own, asked in the export's own words
        (`assembly.take_cut_refusal`), and it comes before the chain because it is a fact about
        the media that no Effect Stack can change. The chain is composed
        **before** the cache is consulted, deliberately: a look whose `.cube` has been deleted
        must refuse by name today even though a clip rendered yesterday still sits in the cache
        and is still a perfectly good picture of that look. The export would refuse; a preview
        that quietly served the old picture would be the more comfortable answer and the less
        honest one.

        **Supersede, never queue** (AD-24). Whatever this project already has in flight with a
        *different* fingerprint is cancelled before this render starts, and the render writes to
        a scratch file that is published — one atomic rename — only if `PreviewRender.superseded`
        is still false when ffmpeg exits. Killing the subprocess is how a superseded render stops
        burning CPU; the gate is what makes it impossible for its output to be served, including
        in the two races a kill cannot cover — the process that finished before the signal, and
        the process that did not exist yet. A cancelled render's answer is a refusal, so nothing
        late is played.

        **Join an identical render, never restart it** (R-22). AD-24 exists to discard *stale*
        work, so a Director dragging a slider is not shown five outdated pictures in sequence. A
        request whose fingerprint equals the one already rendering is not stale work — it is the
        same work, asked for twice, and there is no client that can promise never to ask twice: a
        retry, a poll and a re-render on window focus each produce one. So it waits on that
        render and answers with its result, spawning nothing. Restarting would throw away
        completed effort for a byte-identical answer, and under identical requests arriving
        faster than a render finishes (~116 ms here) it would mean nothing ever lands at all.

        **This route neither blocks an export nor waits on one.** No busy check, no job record,
        no entry in `live_assemblies`, nothing on ComfyUI. A preview is a transcode of a file
        that already exists, it costs tens of milliseconds, and a Director who cannot grade while
        a Batch runs is a Director rationed by a queue that had no reason to hold them.
        """
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        label = shot_label(project, shot)
        if not shot.approved_output:
            raise HTTPException(
                status_code=422, detail=PREVIEW_NO_TAKE_REFUSAL.format(shot=label)
            )
        output_root = (settings.comfy_root / "output").resolve()
        source = (output_root / Path(shot.approved_output)).resolve()
        if output_root not in source.parents or not source.is_file():
            raise HTTPException(
                status_code=422,
                detail=PREVIEW_TAKE_MISSING_REFUSAL.format(
                    shot=label, path=shot.approved_output
                ),
            )
        # This take's own measurement, taken here so that every await in this route happens
        # before the manifest is re-read below. `export_geometry` measures it too — same memo,
        # same key, no second ffprobe — but what it answers is the *project's* delivery grid,
        # and the question the overrun check asks is about this one file's length.
        # `None` from an unmeasurable take is carried through as an unknown length rather than
        # invented as zero: it is the same take `export_geometry` will drop, and a preview that
        # gets that far fails at ffmpeg with its own sentence, exactly as it did before.
        measurement = await take_measurement(source)
        take_seconds = measurement[2] if measurement is not None else None
        delivery = await export_geometry(project)
        if delivery is None:
            raise HTTPException(
                status_code=422, detail=PREVIEW_NO_GEOMETRY_REFUSAL.format(shot=label)
            )
        width, height = preview_side(delivery[0]), preview_side(delivery[1])
        # Re-read after the awaits above, and take the Shot again from the fresh manifest: a
        # slider moved while the takes were being measured must fingerprint as the state that is
        # true now, or the clip lands under a name describing a look nobody is looking at.
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        # The export's own offset rule, resolved from the Shot's own fields exactly as the
        # assemble route resolves it. The *current* window is used rather than the approved
        # snapshot: a Director who has moved a boundary is previewing the look they are editing,
        # and the export's staleness refusal is a decision about shipping, not about looking.
        offset = shot.latest_take_lead + shot.trim_nudge
        frames = clip_frames_on_grid(shot.start, shot.start + shot.duration)
        # And the export's own two refusals about the cut, in the export's own words
        # (`assembly.take_cut_refusal`, the one function both routes call). Without them this
        # route computed `frames` from the *window*, took `offset` on trust, and rendered
        # whatever came out. Both failures were reachable and both were published.
        #
        # **Off the end.** A forward trim nudge past the take's tail, or the overflow branch of
        # `timeline.over_render_lead` plus any nudge at all, and ffmpeg returns 0 having written
        # a clip one or more frames short — `-frames:v` is a cap, not a demand. Measured at
        # preview geometry on the 5.167 s take a 4 s window is really rendered for: at a 1.25 s
        # nudge the response said `frames: 96, window_seconds: 4.0` over a file holding **94**,
        # and at 2 s over one holding 76.
        #
        # **Before the beginning**, and this one is worse, because the file is the right length
        # and the wrong picture. `trim_args` writes its trim pair only `if skip > 0`, so a
        # negative offset is not clamped, reported or honoured — it is silently discarded, and
        # the preview shows the take from its first frame as though that were the window.
        # Measured on a take whose luma encodes its frame index: at nudges of 0, -0.5 and -1.0
        # the route answered **three different fingerprints** — three cache entries, three
        # different claimed cuts — over **one byte-identical file**, every one of them starting
        # at take frame 0. Nothing about the response says so: 96 frames were asked for and 96
        # were delivered.
        #
        # In both cases the clip was published under the look's fingerprint, so every later
        # request served it from the cache without re-rendering. A preview exists to predict
        # the export; the one thing it must never do is show a picture the export refuses to
        # make.
        #
        # Asked *before* the chain is composed, unlike the look refusals below: where the cut
        # lands inside its take is a fact about the media, true or false whatever the Effect
        # Stack does to the picture, and composing a chain for a cut that cannot be made is
        # work with nowhere to go.
        cut = take_cut_refusal(
            label=label,
            offset=offset,
            duration=shot.duration,
            take_seconds=take_seconds,
        )
        if cut is not None:
            raise HTTPException(status_code=422, detail=cut)
        stack = [spec.model_dump() for spec in shot.effects]
        # Read once and handed to both calls below. `preview_fingerprint` composes the same
        # chain to name the clip, and the two must be composed from one set of arguments or the
        # name stops describing the picture — which is the whole of the defect this closes.
        looks = discovered_looks() if stack else ()
        # The Song Envelope, for a Shot that carries a Parameter Binding and for no other. The
        # verdict hashes the whole master and reads a ~405 KB sidecar, and a preview is measured
        # in tens of milliseconds — so an unbound Shot must not pay it, and every unbound Shot's
        # fingerprint must come out of exactly the arguments it came out of before. It is read
        # *before* the cache is consulted rather than after, for the reason the chain is:
        # a preview whose drive has stopped resolving has to refuse today, even though the clip
        # rendered yesterday is still sitting in the cache and is still a perfectly good picture
        # of a look this project can no longer render.
        #
        # **Evaluated once, into a name, because two things read it.** The envelope read below
        # and the `song` slot of the fingerprint further down are one decision -- *does this
        # picture ask the song a question* -- and asking it twice is how the two come to answer
        # differently. `api.stackIsDriven` is this predicate's counterpart on the other side of
        # the wire and the two are pinned to each other by
        # `test_the_client_and_the_server_answer_driven_identically`.
        envelope: dict[str, Any] | None = None
        driven = stack_is_driven(stack)
        if driven:
            # `preview_envelope` above, which is the same read the boundary preview makes and
            # refuses by the same sentence. It was written inline here until 2026-08-29 and was
            # extracted rather than copied, on the rule this codebase applies everywhere else:
            # one question, one implementation.
            envelope = preview_envelope(project_id, project, label=label)
        # The composer's geometry is the **preview's**, not the export's, and that is what makes
        # this the same look rather than the same numbers. `StageContext` describes the delivery
        # grid a treatment is composed for — `chroma_split` stores a fraction and turns it into
        # pixels there — so composing against the export's size and rendering at half of it would
        # show a split twice as wide, relative to the frame, as the export will ship.
        #
        # And `reference_width` is the export's, which is the other half of the same sentence.
        # Five parameters in the catalogue are a *count of pixels* rather than a fraction, so the
        # geometry above is not enough on its own: measured 2026-08-26 through the real chain,
        # `pixelate size=32` laid 60 blocks across the frame at 1920 and 30 at 960, and
        # `soft_focus sigma=8` spread an edge over 1.458 % of the frame at 1920 and 2.917 % at
        # 960. Naming the grid those numbers were written for is what lets the five scale to this
        # one. The export passes nothing, keeps a scale of exactly 1, and its argv does not move —
        # a stored `size: 32` still means 32 pixels at delivery, which is the one thing this
        # correction may not change. See `effects.StageContext.reference_width`.
        try:
            stages = build_effect_stages(
                stack,
                width=width,
                height=height,
                reference_width=delivery[0],
                luts=looks,
                # A preview is the whole Shot, from its own first frame: it is never one half of
                # a resolved overlap, so its offset inside its Shot is zero and the span a ramp
                # is measured against is the Shot's own window. The window is already an input to
                # the fingerprint below (`window_duration`), so a Director who drags a boundary
                # gets a new clip rather than a cached picture of the old ramp.
                clip_offset=0.0,
                shot_seconds=shot.duration,
                # A preview is the whole Shot from its own first frame, so the clip *is* the
                # Shot: `shot_start` is the Shot's start in the song and `clip_seconds` is the
                # frames this render will actually write over the grid rate — the same number
                # `frames` above is, and never `shot.duration`, so the compiled script cannot
                # carry a command past the last frame ffmpeg is asked for. The export resolves
                # the same pair from the clip window it is cutting, which is a different pair
                # for the second half of a split Shot and the identical one for every other.
                envelope=envelope,
                shot_start=shot.start,
                clip_seconds=frames / ASSEMBLY_FPS,
            )
        except EffectRefusal as refusal:
            raise HTTPException(
                status_code=422,
                detail=ASSEMBLY_EFFECTS_REFUSAL.format(shot=label, detail=refusal),
            ) from refusal
        # **The one-sided transition, which is the seventh fingerprint slot's whole reason**
        # (R-35, story 11.5). A Transition with no Overlap under it treats this Shot's own last
        # frames and then cuts, and until this landed it was the one thing an export did that a
        # preview did not -- named as a gap in `_compose_one_sided_transitions`' own docstring.
        #
        # **Composed by the export's own function, with the export's own clamp.** `clip_frames`
        # is `frames`, which is `clip_frames_on_grid` over this Shot's window -- the same number
        # the export hands it, because a Shot with a one-sided transition is provably unsplit
        # (`_final_clip_index`: any later Shot overlapping it would have sent this boundary down
        # the pair path). So the treatment starts on the same frame in both.
        #
        # **`None` covers three states and they are all correct as nothing.** No transition
        # stored; an Overlap under this boundary, where the blend is a `TransitionClip` of its
        # own and this Shot's clip carries no treatment at all; and a pair-only type left behind
        # by a dragged-apart Overlap, which the export records as a refusal and renders untreated
        # (FX-19, R-34). In every one of the three the picture is the picture this Shot already
        # had, so the fingerprint must be the fingerprint it already had -- and it is, because
        # `None` canonicalises to exactly what the empty slot has hashed since 2026-08-26.
        #
        # The catalogue's refusal is raised here for the reason a missing `.cube` is: the export
        # refuses an unknown type by name at its plan stage (`_transition_catalogue_refusals`),
        # and a preview that quietly rendered the untreated picture would be predicting an export
        # that will not run.
        ordered_for_boundary = ordered_shots(project)
        boundary = next(
            (
                spot
                for spot, item in enumerate(ordered_for_boundary)
                if item.id == shot.id
            ),
            None,
        )
        one_sided = None
        stored_transition = shot.transition_out.type if shot.transition_out else None
        if stored_transition is not None and (
            boundary is None
            or not _boundary_is_overlapped(ordered_for_boundary, boundary)
        ):
            try:
                one_sided = one_sided_transition_stages(
                    stored_transition,
                    clip_frames=frames,
                    fps=ASSEMBLY_FPS,
                    # The preview's own grid and the export's, which is `build_effect_stages`'
                    # pair above and is here for the identical reason: `ONE_SIDED_BLUR_SIGMA` is
                    # a count of pixels, so a ramp composed for the delivery width and rendered
                    # at half of it would show a blur twice as heavy as the export will ship.
                    width=width,
                    reference_width=delivery[0],
                )
            except EffectRefusal as refusal:
                raise HTTPException(
                    status_code=422,
                    detail=ASSEMBLY_TRANSITION_REFUSAL.format(shot=label, detail=refusal),
                ) from refusal
        # **The opening treatment, which is the same slot's other half** (R-45, story 11.f8).
        # The Shot that lays the plan's first frame treats its own **opening** frames from its
        # `transition_in`, and FX-NFR-3 is why this is here rather than left to the export: a
        # preview that showed the untreated head while the export shipped a fade up would be
        # exactly the gap story 11.5 closed for the tail, re-opened in the mirror.
        #
        # **The same clamp as the export's, by construction.** `_opening_clip_frames` is the
        # frames of the plan's own first entry read off the windows, which is `plan.frames[0]`
        # over every geometry the sweep in `test_the_window_rule_and_the_plan_agree_about_what_opens`
        # can build -- so this is not `frames`, the whole Shot. They part exactly where the first
        # Shot has an Overlap or a nested Shot inside the first half-second, and taking the Shot's
        # own length there would preview a treatment longer than the frames the export writes.
        #
        # **`boundary == 0` as well as the frames**, because both halves of R-45 have to hold: the
        # Shot must be first in song order *and* lay the first frame. A Shot the plan opens with
        # that is not first has a predecessor, and its `transition_in` is the mirror AD-30 wrote.
        opening = None
        opening_frames = _opening_clip_frames(ordered_for_boundary)
        stored_opening = shot.transition_in.type if shot.transition_in else None
        if stored_opening is not None and boundary == 0 and opening_frames > 0:
            try:
                opening = opening_transition_stages(
                    stored_opening,
                    clip_frames=opening_frames,
                    fps=ASSEMBLY_FPS,
                    # The preview's grid and the export's, for `one_sided`'s reason above.
                    width=width,
                    reference_width=delivery[0],
                )
            except EffectRefusal as refusal:
                raise HTTPException(
                    status_code=422,
                    detail=ASSEMBLY_TRANSITION_REFUSAL.format(shot=label, detail=refusal),
                ) from refusal
        # Appended **after** the Shot's whole look, on both groups, exactly as the export splices
        # them: a transition treats the finished picture, and the `sendcmd` a blur ramp needs
        # rides the end of `geometry` so it stays upstream of the filter it drives. The tail then
        # the head, which is `EXPORT_COMPOSITION_CHECKS`' own order -- two `fade` filters commute
        # and the two blurs carry different instance labels, so the order is a fact about the
        # bytes rather than about the picture, and it is one order so the two can be compared.
        for composed_transition in (one_sided, opening):
            if composed_transition is None:
                continue
            stages = EffectStages(
                geometry=(*stages.geometry, *composed_transition.geometry),
                treatment=(*stages.treatment, *composed_transition.treatment),
                scripts=(*stages.scripts, *composed_transition.scripts),
            )
        # The name of the clip, taken over the chain composed above rather than over the
        # stack it was composed from. The stack is stored sparsely, so a corrected catalogue
        # default and a corrected composer both change the picture without changing a byte of
        # it — and a cache keyed on the spec went on serving the old picture for ever, because
        # nothing in this application evicts `previews/`. Same arguments, same geometry, same
        # `luts`: see `effects.preview_fingerprint`.
        fingerprint = preview_fingerprint(
            take=shot.approved_output,
            window_start=shot.start,
            window_duration=shot.duration,
            offset=offset,
            stack=stack,
            luts=looks,
            # Epic 10 fills the fifth slot; Epic 11 still owns the seventh. `bindings` is the
            # **stored** binding spec of every card, in stack order, and it is `()` for every
            # Shot that carries none — which is what keeps a Shot with no binding naming the
            # clip it already named and its cached preview served (R-20).
            #
            # It is hashed as well as the chain rather than instead of it, and neither is
            # redundant. The chain carries the compiled script's *name*, whose digest is taken
            # over the script's own text, so it moves when the envelope, the Shot's place in the
            # song or the resting value moves. This slot moves when the Director's own numbers
            # move — including for the one case the chain cannot see, a change that compiles to
            # the identical text.
            # `()` and not `((), (), ())` for an unbound stack, which is not a nicety: the two
            # canonicalise as `[]` and `[[],[]]`, so hashing the per-card shape unconditionally
            # would move the name of every Shot in every project that carries any effect at all
            # and re-render the lot for a picture that did not change.
            bindings=(
                tuple(
                    tuple(dict(binding) for binding in spec.get("bindings") or ())
                    for spec in stack
                )
                if envelope is not None
                else ()
            ),
            envelope=envelope,
            shot_start=shot.start,
            clip_seconds=frames / ASSEMBLY_FPS,
            # **The seventh slot, filled by the epic that reserved it.** It carries the composed
            # stages rather than the stored type, which is the fourth slot's own rule applied to
            # the same question: the name has to be a function of the picture, so a corrected
            # ramp, a changed clamp or a different sigma at this geometry each move it, and a
            # stored type that composes nothing does not. `None` for every Shot without one, and
            # `_canonical(None)` is byte-for-byte what this slot has hashed since it was
            # reserved -- so no clip cached before today is renamed by this (R-20).
            #
            # The scripts are absent for `boundary_fingerprint`'s reason: a `sendcmd` stage
            # carries its script's filename and that filename carries a digest of the script's
            # own text, so the ramp is already in `geometry` by name.
            # **Both treatments, in the order they were spliced** (R-45 widened this on
            # 2026-08-31). `None` for a Shot carrying neither, and for a Shot carrying only a
            # tail the value is byte-for-byte the list this slot has hashed since story 11.5 --
            # which is what keeps every clip cached before today still named by its own picture
            # (R-20). A Shot that opens the video renames, because its picture changed.
            transition=(
                None
                if one_sided is None and opening is None
                else [
                    [
                        stage
                        for composed_transition in (one_sided, opening)
                        if composed_transition is not None
                        for stage in composed_transition.geometry
                    ],
                    [
                        stage
                        for composed_transition in (one_sided, opening)
                        if composed_transition is not None
                        for stage in composed_transition.treatment
                    ],
                ]
            ),
            # **Gated on `driven`, and that gate is the whole of this slot's meaning.** The song
            # is part of this picture's identity exactly when the picture asks the song a
            # question, and for an unbound Shot it never does: `build_effect_stages` ignores
            # `envelope`, `shot_start` and `clip_seconds` entirely for a stack with no binding,
            # composes no `sendcmd` stage, and returns the chain it composed before this epic
            # existed. Ungated -- which is what shipped -- a re-analysis renamed the clip of
            # **every** Shot carrying any effect at all, bound or not, and orphaned every one of
            # their cached previews for a reason that cannot reach the picture. Analysing a song
            # is a first-class gesture (`POST /song/analyze`, reachable from the Snap-to rows),
            # so that was a full re-render sweep of the plan on a gesture about *beats*.
            #
            # It is the same rule the client keys on (`api.previewInputKey`), from the same
            # predicate, so the two cannot disagree about whether a song change is a new
            # picture -- which is what they did: the server renamed clips the client never
            # re-asked for, so a bound Shot went on showing a Monitor picture driven by a song
            # this project no longer has, at the same moment the export refused it by name.
            #
            # Redundant for a bound Shot and kept anyway: the fourth slot already moves, because
            # a bound stage composes `sendcmd=f=<name>.cmds` whose name carries a digest of the
            # compiled script's own text. A `sendcmd` that misses is silent at rc 0, so the
            # cheap second statement of the same fact stays.
            song_fingerprint=(
                project.song.analysis.song_fingerprint
                if driven and project.song and project.song.analysis
                else ""
            ),
            width=width,
            height=height,
            # The same grid the chain above was composed against, for the same reason: the name
            # is taken over the composed chain, and a chain composed with a different reference
            # is a different chain. Passing one here and not there would name the clip after a
            # picture this route did not render.
            reference_width=delivery[0],
        )
        # Resolved for the reason the export's `workdir` is: a bound Shot's render runs with this
        # as ffmpeg's working directory, and the scratch clip's own path is written relative to
        # wherever this process is standing. See the export's note. `relative` below is a URL
        # fragment and is untouched by this.
        previews_root = (store.media_dir(project_id) / "previews").resolve()
        relative = f"previews/{fingerprint}.mp4"

        # The cache, the supersede registry and the render, all of it `preview_into_cache`'s
        # (AD-23, AD-24, R-22). It was written inline here until 2026-08-29 and was extracted
        # rather than copied when the boundary preview needed the identical mechanism: two
        # supersede registries for one project would each discard work the other was serving.
        rendered = await preview_into_cache(
            project_id,
            label=label,
            fingerprint=fingerprint,
            previews_root=previews_root,
            scripts=stages.scripts,
            argv=lambda scratch: trim_args(
                source,
                scratch,
                frames,
                width,
                height,
                offset=offset,
                preset=PREVIEW_PRESET,
                geometry_stages=stages.geometry,
                treatment_stages=stages.treatment,
            ),
        )
        return ShotPreviewResponse(
            shot_id=shot.id,
            fingerprint=fingerprint,
            preview=relative,
            preview_url=f"/api/projects/{project_id}/media/{relative}",
            width=width,
            height=height,
            frames=frames,
            window_seconds=frames / ASSEMBLY_FPS,
            rendered=rendered,
        )

    @app.post("/api/projects/{project_id}/assemble", response_model=AssemblyResponse)
    async def assemble_project(
        project_id: str, request: AssemblyRequest | None = None
    ) -> AssemblyResponse:
        """Every approved take, trimmed to its window, joined to the master song.

        FR-22 through AD-9: local ffmpeg, never ComfyUI — `comfy.prompts` stays empty on
        every path through here, refusal or success. The body carries **one** field, and
        that is the whole of it: which preset to build. Everything else is the manifest's
        for the approve route's reason — the export is evidence assembled from evidence:
        `approved_output` paths the server wrote from its own job records, a song path the
        manifest records, windows the approve route snapshotted. The body is optional and
        defaults to `draft`, so the body-less request this route shipped with still means
        exactly what it always meant.

        The order of refusals: state conflicts first (409 — the same request succeeds once
        the conflict clears), then the song (without a measured song duration the plan
        cannot be judged at all), then the one comprehensive 422 carrying *every*
        plan-shaped reason at once — `assembly.py`'s report. A Director fixing a 15-shot
        plan one refusal at a time is a Director being rationed.

        The response is synchronous, deliberately. The work is seconds of local CPU; a
        background lane would need local reconciliation, a task registry surviving
        nothing, and frontend polling changes, whose only payoff is not holding a request
        open. The `RenderJob` (kind `post`, empty `prompt_id`/`seed` by design) is still
        written *before* the work and settled after it, so provenance survives a crash
        mid-run: a later assemble finds the orphan and heals it to `error` rather than
        letting it block forever. `reconcilable_jobs` skips empty-`prompt_id` jobs, so
        nothing here ever reaches the ComfyUI queue path (AD-9's "reconciled locally").

        What could still move under a running assembly: the manifest is re-read before
        every job write, and only the job is patched on the fresh read — a Director
        editing shots mid-assembly loses nothing, and the export honestly reflects the
        plan as validated when the run began, which `job.inputs` records (FR-24 adapted).

        The request being held open is also why the job carries `progress`: the AD-1 poll
        deliberately ignores an empty-`prompt_id` job, so nothing else in the application
        can say how far a multi-minute export has got. ffmpeg's own `-progress` clock is
        written onto the job as it runs, and the Assembly bar reads it back.
        """
        preset = EXPORT_PRESETS[(request or AssemblyRequest()).preset]
        project = get_project(project_id)
        # Heal orphans before judging "busy": a local job left `running` by a crash has no
        # process behind it and nothing else will ever settle it. The same rule startup
        # applies, called with the live registry rather than an empty one — one function, so
        # the two moments cannot come to different verdicts about one job record.
        if heal_orphaned_local_jobs(project, app.state.live_assemblies):
            project = store.save(project)
        if any(
            job.kind == "post"
            and not job.prompt_id
            and job.status not in TERMINAL_JOB_STATUSES
            for job in project.jobs
        ):
            raise HTTPException(status_code=409, detail=ASSEMBLY_BUSY_REFUSAL)
        open_jobs = reconcilable_jobs(project)
        if open_jobs:
            raise HTTPException(
                status_code=409,
                detail=ASSEMBLY_RENDERS_OPEN_REFUSAL.format(count=len(open_jobs)),
            )
        if not project.song or not project.song.path:
            raise HTTPException(status_code=422, detail=ASSEMBLY_NO_SONG_REFUSAL)
        try:
            song_path = resolve_song_path(project_id, project.song)
        except HTTPException as error:
            raise HTTPException(
                status_code=422,
                detail=ASSEMBLY_SONG_FILE_REFUSAL.format(path=project.song.path),
            ) from error
        # The song's duration is ffprobe's reading of the file, never the stored field —
        # the stored value may be 0 (imported before measurement) or stale (file replaced),
        # and FR-22's one-frame bound is against the audio that will actually play.
        rc, out, _err = await run_tool(probe_duration_args(song_path))
        try:
            song_seconds = float(out.splitlines()[0]) if rc == 0 and out else 0.0
        except ValueError:
            song_seconds = 0.0
        if song_seconds <= 0:
            raise HTTPException(
                status_code=422,
                detail=ASSEMBLY_SONG_UNREADABLE_REFUSAL.format(path=project.song.path),
            )
        # Re-read after the await, then judge the plan from the fresh manifest. Sourced
        # takes are probed *before* the refusal report so the offset checks — the
        # over-render lead plus the Director's nudge, judged against the take's measured
        # length — land in the same comprehensive answer as everything else.
        project = get_project(project_id)
        output_root = (settings.comfy_root / "output").resolve()
        clips: list[ClipWindow] = []
        dimensions: dict[str, tuple[int, int]] = {}
        for shot in project.shots:
            source: Path | None = None
            take_seconds: float | None = None
            label = shot_label(project, shot)
            if shot.approved_output:
                candidate = (output_root / Path(shot.approved_output)).resolve()
                if output_root in candidate.parents and candidate.is_file():
                    source = candidate
            has_audio: bool | None = None
            if source is not None:
                rc, out, _err = await run_tool(probe_take_args(source))
                lines = out.splitlines() if rc == 0 else []
                try:
                    width, height = (int(part) for part in lines[0].split(","))
                    take_seconds = float(lines[1])
                except (ValueError, IndexError):
                    raise HTTPException(
                        status_code=422,
                        detail=ASSEMBLY_TAKE_UNREADABLE_REFUSAL.format(
                            shot=label, path=shot.approved_output
                        ),
                    ) from None
                dimensions[shot.id] = (width, height)
                if shot.mix_take_audio:
                    # An acceptance needs something to accept: the no-audio-stream case
                    # joins the comprehensive report rather than failing mid-mix.
                    rc, out, _err = await run_tool(probe_streams_args(source))
                    has_audio = "audio" in out.splitlines() if rc == 0 else None
            clips.append(
                ClipWindow(
                    shot_id=shot.id,
                    label=label,
                    start=shot.start,
                    duration=shot.duration,
                    approved_output=shot.approved_output,
                    approved_start=shot.approved_start,
                    approved_duration=shot.approved_duration,
                    source=source,
                    # One offset rule, resolved here from the Shot's own fields; the
                    # client's `effectiveOffset` mirrors it and a contract test holds
                    # the two together.
                    offset=shot.latest_take_lead + shot.trim_nudge,
                    take_seconds=take_seconds,
                    mix_audio=shot.mix_take_audio,
                    has_audio=has_audio,
                )
            )
        # The pre-flight, run as `EXPORT_PLAN_CHECKS` rather than as a chain of accumulators
        # written into this route (FX-24). Every registered check reports into one answer, so a
        # Director with an unapproved shot, a hole in the timeline and two impossible stacks is
        # told all four at once — being sent back four times for four faults is the failure the
        # comprehensive report exists against. Epic 10's binding check and Epic 11's transition
        # check register into those tuples; nothing here changes when they do.
        #
        # Only a Shot with a stack appears in `stacks`, and `ExportSubject.looks` is the callable
        # rather than the listing, so a project with no effects at all reads the looks folder
        # exactly never.
        # The envelope, behind a one-shot memo for `discovered_looks`' reason and at a larger
        # cost: the verdict hashes the whole master and reads a ~405 KB sidecar, and it is asked
        # twice — once by the plan check that refuses a binding with no measurement, once by the
        # composition that compiles the scripts. An export whose Shots carry no binding never
        # calls it at all. `song` is the manifest's, re-read above; `song_path` is the resolved
        # and containment-checked file the duration was probed from a few lines earlier, so this
        # judges the audio that will actually play.
        measured: list[SongMeasurement] = []

        def song_measurement() -> SongMeasurement:
            if not measured:
                measured.append(
                    song_measurement_verdict(
                        store,
                        project_id,
                        project.song.analysis if project.song else SongAnalysis(),
                        song_path,
                    )
                )
            return measured[0]

        subject = ExportSubject(
            clips=tuple(clips),
            song_seconds=song_seconds,
            stacks={
                shot.id: [spec.model_dump() for spec in shot.effects]
                for shot in project.shots
                if shot.effects
            },
            looks=discovered_looks,
            measurement=song_measurement,
            # Only the **outgoing** Shot's field, which is AD-30: `transition_out` is
            # authoritative for a paired transition, `transition_in` is the mirror the write path
            # keeps in step, and at export only one of the two is read — so a manifest whose pair
            # disagrees produces a decidable export rather than an undecidable one.
            transitions={
                shot.id: shot.transition_out.type
                for shot in project.shots
                if shot.transition_out
            },
            # The mirror. Carried so a pair that disagrees can be said out loud
            # (`_report_transition_divergence`, story 11.3) and -- since R-45, 2026-08-31 -- so
            # that the Shot laying the plan's first frame can open the video
            # (`_compose_opening_transition`). AD-30 is untouched: at every boundary between two
            # Shots the line above is still the whole of what the export reads to decide a
            # picture, and this one is read only where there is no earlier Shot to have one.
            transitions_in={
                shot.id: shot.transition_in.type
                for shot in project.shots
                if shot.transition_in
            },
        )
        refusals = export_plan_refusals(subject)
        if refusals:
            raise HTTPException(status_code=422, detail="\n".join(refusals))
        # The catalogue resolved once, after the plan check above has agreed to every stored type,
        # so `transition_definition` cannot raise here. `assembly.py` is handed the `xfade` name
        # the way `trim_args` is handed finished stage strings: it goes on importing nothing from
        # `effects.py`, and the frame arithmetic cannot be reached by a catalogue at all.
        # **Caught, because the sentence inside it is addressed to a Director** (2026-08-31).
        # `AssemblyGeometryError` carries `ASSEMBLY_NEGATIVE_FRAMES_ERROR` or
        # `ASSEMBLY_NO_GEOMETRY_ERROR`, both written to be read; raised as a bare `ValueError` and
        # caught by nobody, they reached this route as an HTTP 500 with a stack trace. It becomes
        # the same 422 every other stage of this route builds -- the plan refusals above and the
        # composition refusals below -- so a Director gets one shape of answer whichever stage
        # found the problem, and nothing has been half-started behind it.
        try:
            plan = assembly_plan(
                clips,
                song_seconds,
                dimensions,
                {
                    shot_id: TransitionChoice(
                        stored, transition_definition(stored).xfade
                    )
                    for shot_id, stored in subject.transitions.items()
                },
            )
        except AssemblyGeometryError as geometry:
            raise HTTPException(status_code=422, detail=str(geometry)) from geometry
        # The composition stage: the checks that need the export's own delivery geometry, which
        # is why they could not run above. They build what the export is driven with as well as
        # reporting on it — `build_effect_stages` is the only thing that can see a look whose
        # `.cube` has left the folder, and it is also what produces the chain — so the artifact
        # and the refusal come out of one pass, before the job record is written and with nothing
        # half-started behind a fault. A Shot with no stack gets no entry and therefore no stages
        # at all, which is what keeps its argv the argv this route has always built.
        composition, composition_refusals = compose_export(replace(subject, plan=plan))
        if composition_refusals:
            raise HTTPException(status_code=422, detail="\n".join(composition_refusals))
        effect_stages = composition.effect_stages

        exports_root = store.media_dir(project_id) / "exports"
        exports_root.mkdir(parents=True, exist_ok=True)
        taken = [
            int(match.group(1))
            for match in (
                re.fullmatch(r"assembly_(\d{5})\.mp4", item.name)
                for item in exports_root.glob("assembly_*.mp4")
            )
            if match
        ]
        export_name = f"assembly_{max(taken, default=0) + 1:05d}.mp4"

        # The job is written before any work so provenance survives a crash. `inputs` is
        # FR-24 adapted: the exact takes this export was built from, by shot. `look` is FX-25,
        # the other half of that question — what was *done* to those takes — taken from the
        # composition the export is about to be driven with rather than re-derived from the
        # Shots, so the record cannot describe a different look from the argv. Empty for a
        # project whose Shots carry none, which is what every job written before this field
        # existed also reads as; see `models.ExportLook`.
        job = RenderJob(
            kind="post",
            status="running",
            target_id="assembly",
            # FR-24 adapted: the exact takes this export was built from, by shot. A transition
            # entry consumed **two** takes in one segment and contributes both, in the order they
            # play — the record answers "which takes went in", and a segment that listed one of
            # its two legs would understate the export by the frames of the other.
            inputs=[
                f"{clip.shot_id}={clip.approved_output}"
                for entry in plan.clips
                for clip in (
                    (entry.before, entry.after)
                    if isinstance(entry, TransitionClip)
                    else (entry,)
                )
            ],
            look=composition.look,
        )
        # Appended to a **fresh** read, never to `project`. That object was read before the
        # per-shot probes above — one ffprobe per sourced shot and two for a shot that mixes its
        # take's audio — and saving it back would lay every Shot field in the project as it stood
        # before the first of those awaits. Grading is exactly what a Director does while an
        # export churns, so the window is not theoretical: a prompt typed, a stack changed, a
        # nudge dragged, all silently reverted by the record of the export they were watching.
        # This is `settle`'s rule twenty lines below applied to the one write on this route that
        # used to be the exception to it, and it is what makes the docstring's "the manifest is
        # re-read before every job write" true. Only the job is added; nothing else of the
        # pre-probe object reaches the disk, and the export still runs against the plan validated
        # above, which `job.inputs` records.
        fresh = get_project(project_id)
        fresh.jobs.append(job)
        store.save(fresh)
        app.state.live_assemblies.add(job.id)
        # Resolved, because a bound Shot's trim runs with this directory as ffmpeg's **working
        # directory** — that is how `sendcmd=f=` reaches a bare relative script name (R-30) — and
        # every other path in that argv is written relative to wherever this process happens to
        # be standing. A data root configured as a relative path (`MVP_DATA_ROOT=data`, and every
        # harness that passes one) then had ffmpeg looking for its own output inside `.work-…`
        # and failing with `No such file or directory` on a file this application had just chosen
        # the name of. Measured 2026-08-27 on the drive harness, and it is the one thing a `cwd`
        # can break that has nothing to do with the script.
        #
        # `resolve` is the identity for the absolute root every real installation has, so the
        # argv and `clips.txt` are unchanged wherever this was already correct.
        workdir = (exports_root / f".work-{job.id}").resolve()
        workdir.mkdir(parents=True, exist_ok=True)

        def settle(patch) -> RenderJob | None:
            """Re-read, patch only this job on the fresh manifest, save. The house re-read
            rule: awaits happened, and shot edits made meanwhile must not be overwritten."""
            fresh = get_project(project_id)
            recorded = next((item for item in fresh.jobs if item.id == job.id), None)
            if recorded:
                patch(recorded)
                store.save(fresh)
            return recorded

        def failed(stage: str, detail: str) -> HTTPException:
            trimmed = detail[-500:] if detail else "no error output"
            message = ASSEMBLY_STAGE_FAILED_ERROR.format(stage=stage, detail=trimmed)

            def patch(recorded: RenderJob) -> None:
                recorded.status = "error"
                recorded.error = message
                # A settle, stamped like the rest. `settle` is also the progress writer, so the
                # stamp goes in the two terminal patches and nowhere else — a hundred progress
                # ticks moving `updated_at` would leave it meaning "last touched" instead of
                # "when this ended", and the duration would evaporate. See `RenderJob.updated_at`.
                stamp_job_settled(recorded)

            settle(patch)
            return HTTPException(status_code=502, detail=message)

        progress = ExportProgress(total_seconds=song_seconds)
        reported = -1

        def report(percent: int) -> None:
            """Write a changed percent onto the job, and only a changed one.

            Every save is a whole-manifest write with an fsync behind it, and ffmpeg
            reports several times a second; throttling to whole-percent movement caps the
            entire bar at 101 writes however long the export runs. `ExportProgress` is
            already monotonic, so this cannot write a number that goes backwards either.
            """
            nonlocal reported
            if percent == reported:
                return
            reported = percent

            def patch(recorded: RenderJob) -> None:
                recorded.progress = percent

            settle(patch)

        try:
            intermediates: list[Path] = []
            trimmed_seconds = 0.0
            for index, (clip, frames) in enumerate(
                zip(plan.clips, plan.frames, strict=True)
            ):
                dest = workdir / f"clip_{index:03d}.mp4"
                # This clip's look, at the two insertion points AD-17 fixed — geometry before
                # `scale`, treatment before `pad`. `EffectStages()` for a clip whose Shot has no
                # stack, and both of its groups are empty, which is what `trim_args` already
                # defaults them to: the argv for an unstyled Shot is the argv this route built
                # before effects existed, argument for argument. `tests/test_assembly.py`'s
                # `TODAYS_TRIM_ARGV` pins that at the builder and `test_assembly_route` pins it
                # at this call site.
                #
                # Keyed by the clip's own index, not by its Shot's id: a Shot that another nests
                # inside is two clips here, and story 9.7 composed them apart on purpose so the
                # second carries on where the first stopped rather than replaying it.
                if isinstance(clip, TransitionClip):
                    # AD-18's third entry, rendered by its own pinned argv from **both** takes in
                    # one invocation (R-38). Re-cutting it from the finished intermediates is not
                    # merely costlier, it is impossible: `assembly_plan` truncates the earlier clip
                    # at the later one's start, so the outgoing Shot's overlap frames are in no
                    # intermediate at all.
                    #
                    # `-c:v copy` on the join is unchanged (FX-NFR-2), which is what
                    # `assembly.normalized_stages` guarantees structurally: this segment's legs are
                    # built from the same chain builder `trim_args` uses, and the `xfade`'s own
                    # output is pinned back to `yuv420p` — measured, because it is `yuv444p`
                    # otherwise, at rc 0.
                    legs = composition.transition_stages.get(
                        index, (EffectStages(), EffectStages())
                    )
                    for leg in legs:
                        for script in leg.scripts:
                            (workdir / script.filename).write_text(
                                script.text, encoding="utf-8"
                            )
                    rc, _out, err = await run_tool(
                        with_progress(
                            transition_segment_args(
                                clip.before.source,
                                clip.after.source,
                                dest,
                                frames,
                                plan.width,
                                plan.height,
                                clip.choice.xfade,
                                before_offset=clip.before.offset,
                                after_offset=clip.after.offset,
                                preset=preset,
                                before_geometry=legs[0].geometry,
                                before_treatment=legs[0].treatment,
                                after_geometry=legs[1].geometry,
                                after_treatment=legs[1].treatment,
                            )
                        ),
                        on_progress=lambda microseconds, at=trimmed_seconds: report(
                            progress.trim(at, microseconds)
                        ),
                        cwd=workdir
                        if any(leg.scripts for leg in legs)
                        else None,
                    )
                    if rc != 0 or not dest.is_file():
                        raise failed(f"transition ({clip.label})", err)
                    intermediates.append(dest)
                    trimmed_seconds += frames / ASSEMBLY_FPS
                    continue
                composed = effect_stages.get(index, EffectStages())
                # This clip's compiled drive scripts, written beside `clips.txt` and the
                # intermediates in the export's own `workdir`, which is then ffmpeg's working
                # directory for exactly this call. `sendcmd=f=` reads a **bare relative** name
                # (R-30, `run_tool`'s `cwd`), and the name carries a digest of the script's own
                # text — so a Shot that becomes two clips writes two files rather than one clip
                # silently driving the other, and two clips that really do compile the same
                # script share one, which is right rather than lucky.
                #
                # `cwd` is passed **only** when there is a script to read. An export whose Shots
                # carry no binding — every export in every project until one is bound — spawns
                # ffmpeg exactly as it did before, argv and working directory alike.
                for script in composed.scripts:
                    (workdir / script.filename).write_text(script.text, encoding="utf-8")
                rc, _out, err = await run_tool(
                    with_progress(
                        trim_args(
                            clip.source,
                            dest,
                            frames,
                            plan.width,
                            plan.height,
                            offset=clip.offset,
                            preset=preset,
                            geometry_stages=composed.geometry,
                            treatment_stages=composed.treatment,
                        )
                    ),
                    # `at` is a default argument, so the clip's own start on the timeline
                    # is bound when the callback is made rather than read when it fires:
                    # each trim restarts ffmpeg's clock at zero, and a reading has to be
                    # placed at the clip it came from.
                    on_progress=lambda microseconds, at=trimmed_seconds: report(
                        progress.trim(at, microseconds)
                    ),
                    cwd=workdir if composed.scripts else None,
                )
                if rc != 0 or not dest.is_file():
                    raise failed(f"trim ({clip.label})", err)
                intermediates.append(dest)
                trimmed_seconds += frames / ASSEMBLY_FPS
            list_file = workdir / "clips.txt"
            list_file.write_text(concat_manifest(intermediates), encoding="utf-8")
            candidate = workdir / export_name
            # The accepted-audio overlays: same slice as the picture (the clip's offset
            # and grid frames), placed at the clip's cumulative timeline position. Empty
            # for an untouched project, which keeps the command byte-identical to the
            # song-only ruling's.
            overlays: list[AudioOverlay] = []
            elapsed_frames = 0
            for entry, frames in zip(plan.clips, plan.frames, strict=True):
                # **A transition contributes the incoming Shot's leg, and only it — so the mix
                # does not move.** This is the decision constraint 6 asked to be stated.
                #
                # `AudioOverlay` has always stopped the earlier take's audio at the later one's
                # start, because `assembly_plan` truncates the earlier clip there. The Overlap's
                # seconds are seconds the later Shot has already begun, so they were the later
                # Shot's before this epic and they stay the later Shot's now. Take the segment's
                # frames out of the accumulator and the incoming Shot's accepted audio would lose
                # exactly the head of itself, silently, on the day a Director sets a dissolve.
                #
                # What changes is the *shape* and not the content: a Shot whose take audio is
                # accepted and which is preceded by a transition contributes **two** overlays
                # where it contributed one — the Overlap's frames at its own take offset and its
                # own delay, then the remainder at the offset the split advanced. Same source,
                # same take seconds, same timeline positions, in two pieces. The sub-frame
                # discrepancy between the second piece's take offset and the first piece's end is
                # the one `assembly_plan`'s own `replace(clip, offset=clip.offset + ...)` has
                # produced for every nested overlay since the layers ruling; it is bounded by half
                # a frame and it is the established convention rather than something new here.
                #
                # The outgoing Shot's take audio does **not** come in under the blend, and that is
                # the same rule read consistently: it stopped at the later Shot's start before,
                # and this epic moved no clip's start.
                clip = entry.after if isinstance(entry, TransitionClip) else entry
                if clip.mix_audio:
                    overlays.append(
                        AudioOverlay(
                            source=clip.source,
                            offset_seconds=clip.offset,
                            window_seconds=frames / ASSEMBLY_FPS,
                            delay_seconds=elapsed_frames / ASSEMBLY_FPS,
                        )
                    )
                elapsed_frames += frames
            rc, _out, err = await run_tool(
                with_progress(
                    concat_args(list_file, song_path, candidate, overlays, preset=preset)
                ),
                on_progress=lambda microseconds: report(progress.join(microseconds)),
            )
            if rc != 0 or not candidate.is_file():
                raise failed("concat", err)
            # FR-22's last consequence: verified after writing, and the export reaches its
            # public name only after passing — a failed verification leaves nothing under
            # `exports/` to be mistaken for a result.
            rc, out, err = await run_tool(probe_duration_args(candidate))
            try:
                measured = float(out.splitlines()[0]) if rc == 0 and out else 0.0
            except ValueError:
                measured = 0.0
            rc, out, err = await run_tool(probe_streams_args(candidate))
            streams = [line for line in out.splitlines() if line] if rc == 0 else []
            problems = verification_problems(song_seconds, measured, streams)
            if problems:
                raise failed("verification", " ".join(problems))
            shutil.move(str(candidate), str(exports_root / export_name))
        finally:
            app.state.live_assemblies.discard(job.id)
            shutil.rmtree(workdir, ignore_errors=True)

        export_relative = f"exports/{export_name}"

        def complete(recorded: RenderJob) -> None:
            recorded.status = "complete"
            recorded.output_files = [export_relative]
            # 100 stated rather than inferred: ffmpeg's last reading is against a clock
            # that can stop a few milliseconds short of the file it just wrote, and a
            # finished export reading 99 % is a bar that never lands.
            recorded.progress = 100
            # An assembly is local work started the moment its record is created, so here —
            # uniquely — `created_at` really is the start and `record` is an exact export time
            # rather than an upper bound. It is still labelled `record`, because the label
            # describes where the span came from and not how much to trust it in one case.
            stamp_job_settled(recorded)

        settled = settle(complete)
        return AssemblyResponse(
            job=settled or job,
            preset=preset.name,
            export=export_relative,
            export_url=f"/api/projects/{project_id}/media/{export_relative}",
            duration_seconds=measured,
            song_seconds=song_seconds,
            width=plan.width,
            height=plan.height,
            total_frames=plan.total_frames,
            clip_count=len(plan.clips),
        )




    @app.post(
        "/api/projects/{project_id}/sections/fill-looks",
        response_model=SectionLooksResponse,
    )
    async def fill_section_looks(
        project_id: str, request: SectionLooksRequest
    ) -> SectionLooksResponse:
        """Read each marked section's shared look out of the treatment and the style bible.

        The gap this closes, reported by the Director (2026-08-20): "I clicked on a Section
        in the timeline and noticed that the shared prompt wasn't pre-filled with
        information from the Treatment." `SongSection.prompt` is layered under every shot in
        its section — `reference_prompt` appends it as "Section look: …", `expansion_input`
        and `dp_input` carry it into the prose passes — and on the live project all seven
        sections held `""`. The cause is structural, not a dropped field: those sections were
        created by the Whisper lyric-alignment pass, so populate takes its `known_sections`
        branch and never asks for structure at all, and the one path that would have written
        a look (`PlannedSection.prompt`) runs only when the sections are *unknown*.

        A dedicated action rather than a fill inside populate, and the choice is the
        codebase's own convention rather than a preference:

        * Populate is the destructive one. It refuses without `confirm_replace`, refuses
          outright over a lock or an approval, and replaces the whole shot list. Bolting a
          second model call and a bulk edit of a hand-editable field onto that button means
          the Director cannot take one without the other.
        * This is re-runnable and populate is not. The treatment gets rewritten — that is
          what the treatment lane is *for* — and the looks want refreshing afterward without
          the timeline being thrown away to get it.
        * Report first, apply on confirm is what this codebase does for every bulk plan
          change (`populate`'s `confirm_replace`, `snap_timeline_cuts`, asset replacement),
          and the report is half the feature: "6 filled, 1 skipped — the treatment does not
          describe the Bridge" is the sentence that sends the Director back to the treatment.

        Sections are matched **by id, corroborated by label** — never by position. Labels
        come from the lyric sheet's `[Tag]` blocks and repeat by design ("Verse"/"Verse 2",
        "Chorus"/"Chorus 2"), the treatment groups them ("Verse 1 & Verse 2"), and a look
        landing one row off would put Chorus 2's canopy bed on the Bridge — worse than
        leaving the Bridge empty. So the model answers on the id it was handed
        (`ExpandedShot.shot_id`'s contract) and also copies the label back; a pair that
        disagrees is refused, an unknown id is dropped, and neither writes anything.

        **Report and confirm are two different acts here, and only the report asks the
        model.** Until 2026-08-21 both did: the call was made before `confirm_apply` was
        looked at, so the confirmed pass read the treatment a second time at
        `PLAN_TEMPERATURE` and wrote whatever *that* reading said. The confirm now carries the
        report back as `plan` and writes exactly it — see the plan-carrying block above for the
        digest, the revision check and why the plan travels on the wire rather than in a cache.

        **One report is not a plan**, and it is the one this route answers with most often on a
        finished project: the all-written short-circuit below returns ahead of the model call,
        so its rows hold no look for a confirm to write. It used to say "send overwrite=true to
        replace what is there", and a caller who did exactly that got 200, `applied: false`, and
        an untouched project — the route answering its own documented sequence with silence. The
        message now names the step that works (report again *with* the consent, then confirm
        that report, which is what the browser has always done) and
        `section_looks_plan_writes` refuses the short-circuit report if it comes back as a plan
        regardless.

        Nothing here renders, arms, queues or approves. It writes one string per section and
        touches no other field on the project.
        """
        project = get_project(project_id)
        # The confirm, and the only path that writes. No model is asked on it: `plan` is the
        # report a person read, and the looks it writes are that report's own strings.
        if request.confirm_apply:
            response, pending = section_looks_plan_writes(project, request)
            if not pending:
                return response
            for section, prompt in pending:
                section.prompt = prompt
            response.applied = True
            response.project = store.save(project)
            return response
        if not project.sections:
            raise HTTPException(status_code=422, detail=SECTION_LOOKS_NO_SECTIONS)
        # The absent-analysis rule, applied to prose: an empty treatment is *unwritten*, not
        # "no look wanted", exactly as an empty `Song.vocal_spans` is unmeasured rather than
        # silent. Refused before the model call, so an empty project costs nothing and the
        # sentence names which half is missing instead of a fabricated look arriving.
        if not project.treatment.strip():
            raise HTTPException(status_code=422, detail=SECTION_LOOKS_NO_TREATMENT)
        if not project.style_bible.strip():
            raise HTTPException(status_code=422, detail=SECTION_LOOKS_NO_STYLE_BIBLE)
        ordered = sorted(project.sections, key=lambda section: section.start)
        # Checked before spending a 300 s local-model call: with every look written and no
        # consent to replace them, the answer is already known and the model has no question
        # to answer. A report, not a refusal — nothing is wrong, there is simply nothing to do.
        if not request.overwrite and all(
            section.prompt.strip() for section in ordered
        ):
            written = SectionLooksResponse(
                applied=False,
                filled=0,
                skipped=len(ordered),
                sections=[
                    SectionLookRow(
                        section_id=section.id,
                        label=section.label,
                        start=section.start,
                        filled=False,
                        previous=section.prompt,
                        # Not `SECTION_LOOK_SKIP_WRITTEN`: that row carries the look the
                        # consent would buy, and this one cannot — the model was never asked.
                        # The distinct sentence is also what lets a confirm recognise this
                        # report and refuse it rather than write nothing.
                        reason=SECTION_LOOK_SKIP_ALL_WRITTEN,
                    )
                    for section in ordered
                ],
                message=SECTION_LOOKS_ALL_WRITTEN,
                updated_at=project.updated_at,
            )
            # Identified like any other report, though no row of it carries a look to write:
            # one shape for every answer this route gives means a client never has to ask
            # which kind of report it is holding before it can confirm one.
            written.plan_id = plan_fingerprint(project, written)
            return written
        try:
            answer = await director.section_looks(
                looks_input=section_looks_input(project)
            )
        except DirectorUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except DirectorError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        # Re-read after the await — a local model can hold this open for minutes, and the
        # Director may have marked, dragged or written a section in the meantime. The fresh
        # project is the one being written, so it is also the one being matched against: a
        # section deleted while the model thought simply has no row, and a look written by
        # hand in that window is protected by the same rule as any other written look.
        project = get_project(project_id)
        by_id = {section.id: section for section in project.sections}
        # First answer per id wins. A model that repeats an id is not offering a second
        # opinion to arbitrate; it is one answer emitted twice, and letting the later copy
        # win would make the written look depend on emission order.
        answered: dict[str, Any] = {}
        for look in answer.looks:
            answered.setdefault(look.section_id, look)
        rows: list[SectionLookRow] = []
        for section in sorted(project.sections, key=lambda item: item.start):
            row = SectionLookRow(
                section_id=section.id,
                label=section.label,
                start=section.start,
                filled=False,
                previous=section.prompt,
            )
            look = answered.get(section.id)
            if look is None:
                row.reason = SECTION_LOOK_SKIP_UNANSWERED
            elif look.label.strip().casefold() != section.label.strip().casefold():
                # The checksum firing. Case and surrounding space are not a disagreement —
                # "verse 2" is the Director's "Verse 2" — but anything else means the id and
                # the name do not describe the same box, and one of the two is wrong.
                row.reason = SECTION_LOOK_SKIP_MISLABELLED.format(label=look.label)
            elif not look.prompt.strip():
                # The honest empty, and the reason `SectionLook.prompt` allows one at all:
                # the model was required to emit the key and answered "the treatment does
                # not say". Left blank rather than filled with something plausible.
                row.reason = SECTION_LOOK_SKIP_UNDESCRIBED
            elif section.prompt.strip() and not request.overwrite:
                row.reason = SECTION_LOOK_SKIP_WRITTEN
                # Carried on the row so the report shows what the consent would buy.
                row.prompt = look.prompt.strip()
            else:
                row.filled = True
                row.prompt = look.prompt.strip()
            rows.append(row)
        # Ids the model invented or copied from another project are dropped in silence above
        # (they match no section) and counted here, so the report can say so rather than the
        # Director wondering why a look they can see in the message never landed.
        stray = sum(1 for section_id in answered if section_id not in by_id)
        filled = sum(1 for row in rows if row.filled)
        response = SectionLooksResponse(
            applied=False,
            filled=filled,
            skipped=len(rows) - filled,
            sections=rows,
            message=section_looks_summary(filled, len(rows) - filled, stray),
            stray=stray,
            updated_at=project.updated_at,
        )
        # Minted last, over the finished report, and over the *fresh* project's revision — the
        # one the confirm will be checked against. Nothing is written on this path.
        response.plan_id = plan_fingerprint(project, response)
        return response

    @app.post(
        "/api/projects/{project_id}/timeline/lay-out",
        response_model=LayOutResponse,
    )
    async def lay_out_timeline(
        project_id: str, request: LayOutRequest
    ) -> LayOutResponse:
        """Step one of a populate, on its own route: how many shots, where, how long.

        `lay_out_shots` makes every decision; this route's additions are the project lookup,
        the refusals lay-out owns (`lay_out_protections`), the plan-carrying checks and the
        write. The same function is what `populate_timeline` calls, so a layout produced
        here and a layout produced inside a chained populate cannot differ.

        Report first, apply on confirm. Without `confirm_replace` this route asks the model,
        computes the tiling and **does not call `store.save`** — the response carries no
        project at all, so "nothing was written" is visible on the wire. With it, the report
        must come back as `plan` and the windows in that report are the windows that land:
        no model is asked on the confirming call, which is the 2026-08-21 rule (a second
        generation at `PLAN_TEMPERATURE` is a different layout, and the Director read the
        first one).

        **What it writes is structure, and only structure.** Sections, when this step laid
        them out, and one draft shot per window carrying its `start` and `duration` and
        nothing else. Prompts, citations, `singing`, `use_song_audio` and seeds are the
        fill-in step's to write — that is what makes them re-runnable against timing that is
        already approved. A Director stepping through the three routes by hand runs
        `line-up` and then `fill-in` on this report to get a plan with content in it; a
        Director who wants all three at once clicks Populate Timeline, which is the chain.

        Destructive by design and guarded exactly as populate is: the protections
        (`lay_out_protections`) refuse before the model is asked, and refuse again on the
        re-read inside `lay_out_shots` after it, because a lock or a render that appeared
        while the model thought must still stop the replace.

        Nothing renders, arms, queues or approves. `comfy` is not touched on any path.
        """
        project = get_project(project_id)
        # The confirm, and the only path that writes. No model is asked on it: `plan` is the
        # report a person read, and the windows it writes are that report's own numbers.
        if request.confirm_replace:
            plan = request.plan
            if plan is None or not plan.windows or not plan.plan_id:
                raise HTTPException(status_code=422, detail=LAY_OUT_NO_PLAN)
            if plan.updated_at is None or plan.updated_at != project.updated_at:
                raise HTTPException(status_code=409, detail=PROJECT_CHANGED_REFUSAL)
            if plan_fingerprint(project, plan) != plan.plan_id:
                raise HTTPException(status_code=422, detail=LAY_OUT_PLAN_MISMATCH)
            # Asked again on the confirming call, and not only on the report: the two are
            # separate requests, and a lock set between them is a protection that would
            # otherwise vanish with the timeline it protected.
            #
            # Unreachable while the revision check above holds — a lock, an approval and a
            # submitted job all move `updated_at`, so the 409 speaks first — and checked
            # anyway, `CLEAN_PROMPTS_WINDOWS_MOVED`'s rule: the cost is one comparison and
            # what it defends is the whole timeline. It survives mutation for that reason
            # (2026-08-21 sweep), which is the honest reading of a defence-in-depth line
            # rather than a gap in the tests.
            lay_out_protections(project)
            # Written only when this step is what produced them. A section layer the Director
            # marked is left exactly as it is — rewriting it from the report would mint new
            # ids for boxes nobody changed.
            if plan.sections_origin in ("structure", "shots"):
                project.sections = [
                    SongSection(
                        label=row.label,
                        start=row.start,
                        duration=row.duration,
                        prompt=row.prompt,
                    )
                    for row in plan.sections
                ]
            project.shots = [
                Shot(start=row.start, duration=row.duration) for row in plan.windows
            ]
            response = plan.model_copy(deep=True)
            saved = store.save(project)
            response.project = saved
            response.applied = True
            # Re-stamped over the saved revision so this body is a *valid* plan for the
            # line-up step: the confirm moved `updated_at`, and a report still claiming the
            # revision it was read from would be refused by the next route on its way past.
            # The layout itself is unchanged — same windows, same proposals, same sections.
            response.updated_at = saved.updated_at
            response.plan_id = plan_fingerprint(saved, response)
            return response
        lay_out_protections(project)
        layout = await lay_out_shots(
            project,
            director=director,
            two_stage=request.two_stage,
            reread=lambda: get_project(project_id),
            variance=request.variance,
        )
        response = layout_report(layout)
        # Minted over the *re-read* project — the one `lay_out_shots` returned and the one a
        # confirm will be checked against. Nothing is written on this path.
        response.updated_at = layout.project.updated_at
        response.plan_id = plan_fingerprint(layout.project, response)
        return response

    @app.post(
        "/api/projects/{project_id}/timeline/line-up",
        response_model=LineUpResponse,
    )
    def line_up_timeline(project_id: str, request: LineUpRequest) -> LineUpResponse:
        """Step two of a populate: move each cut onto the music, and say what it now covers.

        `line_up_shots` makes every decision, and it is the function a chained populate calls
        too. It is pure and asks no model. What comes back is the input the fill-in step
        needs — each window's measured voice, whether the track leaves it voiceless, which
        section it falls in, and the lyric lines sung across it with their singer marks — plus
        the cut report: every cut that moved and every cut that did not, with the sentence
        saying why.

        **Two entry points, one step, and the difference is where the windows come from.**

        * **With a lay-out report as `plan`** — the by-hand walk through the three routes. The
          report has to prove it is one this server emitted (the digest and the revision,
          `section_looks_plan_writes`' checks) and is echoed back so the fill-in step needs
          one body rather than two. Its confirm writes the moved windows onto the shots the
          lay-out step's own confirm created, and refuses when the timeline is not that layout
          — line-up is the step that owns where a cut sits, so it is the step that writes one.
          Before lay-out has been confirmed there is no shot at any of these windows, so the
          report describes proposals and protects nothing, which is the truth about them.
        * **Without one** — line up the timeline the project already has, which is the pass a
          Director runs over a plan they have been editing. Report-then-confirm in
          `snap_timeline_cuts`' exact shape: without `confirm_apply` this route **does not
          call `store.save`** and the response carries no project at all. The three window
          protections hold here and are `window_move_refusal`'s own sentences — a locked shot,
          a shot carrying an approved take and a shot with a render in flight each refuse the
          cuts at their edges, by name.

        Only the windows are ever written, and only on the second path's confirm. The lyric
        lines and singer slots are **derived facts and are not persisted**: they are re-read
        from the sheet and the transcript every time, so a sheet the Director retags is
        answered by the next line-up rather than by a stale copy in the manifest.

        Nothing renders, arms, queues or approves. `comfy` is not touched.
        """
        project = get_project(project_id)
        plan = request.plan
        if plan is not None:
            if not plan.windows or not plan.plan_id:
                raise HTTPException(status_code=422, detail=LINE_UP_NO_PLAN)
            if plan.updated_at is None or plan.updated_at != project.updated_at:
                raise HTTPException(status_code=409, detail=PROJECT_CHANGED_REFUSAL)
            if plan_fingerprint(project, plan) != plan.plan_id:
                raise HTTPException(status_code=422, detail=LINE_UP_PLAN_MISMATCH)
            layout = layout_from_report(project, plan)
            # Whether the timeline is *already* this layout — the state lay-out's own confirm
            # leaves behind. When it is, the shots at these windows are the windows: they are
            # named as the Director sees them and they carry their protections, so a locked
            # shot's cut is skipped by name rather than moved. When it is not — the ordinary
            # report, taken before lay-out was confirmed, and every chained populate — there
            # is no shot at any of these windows and nothing to protect, which
            # `line_up_windows` says in as many words.
            laid_out = [
                (shot.start, shot.duration) for shot in ordered_shots(project)
            ] == list(layout.windows)
            rendering = frozenset(
                shot.id for shot in project.shots if shot_render_in_flight(project, shot)
            )
            try:
                alignment = line_up_shots(
                    layout,
                    tolerance=request.tolerance,
                    windows=(
                        shot_snap_windows(project, rendering=rendering)
                        if laid_out
                        else None
                    ),
                )
            except TimelineError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            response = alignment_report(alignment, plan)
            response.updated_at = project.updated_at
            response.plan_id = plan_fingerprint(project, response)
            if not request.confirm_apply:
                return response
            # The confirm writes windows and only windows, onto shots this layout already
            # created. It refuses rather than write when the timeline is something else: the
            # shot a row addresses would not be the shot the row was lined up for.
            if not laid_out:
                raise HTTPException(
                    status_code=422,
                    detail=LINE_UP_WINDOWS_CHANGED.format(
                        expected=len(plan.windows), found=len(project.shots)
                    ),
                )
            if alignment.moves:
                for shot, placement in zip(ordered_shots(project), alignment.placements):
                    shot.start = placement.start
                    shot.duration = placement.duration
                saved = store.save(project)
                response.project = saved
                response.applied = True
                # Re-stamped over the saved revision so this body is a *valid* plan for the
                # fill-in step, `lay_out_timeline`'s line and for its reason: the confirm
                # moved `updated_at`, and a report still claiming the revision it was read
                # from would be refused by the next route on its way past. The alignment
                # itself is unchanged — same windows, same facts, same cut report.
                response.updated_at = saved.updated_at
                response.plan_id = plan_fingerprint(saved, response)
            return response
        # The project-sourced path. Its windows are the timeline's own shots, so it needs the
        # song check and the protections `snap_timeline_cuts` needs, in the same words.
        if not project.song or project.song.duration <= 0:
            raise HTTPException(status_code=422, detail=SNAP_CUTS_NO_SONG)
        # The in-flight set and the protections are `snap_timeline_cuts`' own, through the one
        # builder both routes call: a cut that is protected on one of them is protected on the
        # other because there is only one answer to ask.
        rendering = frozenset(
            shot.id for shot in project.shots if shot_render_in_flight(project, shot)
        )
        stored = shot_snap_windows(project, rendering=rendering)
        # A **hole** has no seam in it, so a plan with one is refused in `snap_timeline_cuts`'
        # own words. An overlap is not a hole: it is an authored transition (R-3) and the core
        # moves it as a unit, which is what made this route usable on the Director's live plan
        # at all — 15 of its 33 seams are overlaps, one of them 5.49 s long.
        try:
            alignment = line_up_shots(
                ShotLayout(
                    project=project,
                    duration=project.song.duration,
                    required=len(project.shots),
                    # No proposals, and the emptiness is what stops this report being fed to
                    # fill-in: there is no content half here to fill anything in from.
                    # `layout` comes back `None` on the response for the same reason.
                    proposals=(),
                    windows=tuple(
                        (shot.start, shot.duration) for shot in ordered_shots(project)
                    ),
                    sections=tuple(project.sections),
                    sections_origin="director" if project.sections else "",
                    message="",
                ),
                tolerance=request.tolerance,
                windows=stored,
                # The stored-window band, not the layout's tighter ceiling: these windows
                # were not laid out by this pass and a Director may deliberately have edited
                # one to 9 s — `snap_window_plan` argues the case.
                maximum=H3_MAX_SHOT_SECONDS,
            )
        except TimelineError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        # The two honest-empty branches refuse rather than report, `snap_timeline_cuts`' rule
        # and its wording: no cut was examined, so there is nothing to report over.
        if alignment.status in ("unmeasured", "no_cuts"):
            raise HTTPException(
                status_code=422,
                detail=(
                    SNAP_UNMEASURED
                    if alignment.status == "unmeasured"
                    else SNAP_WITHOUT_CUTS.format(count=len(project.shots))
                ),
            )
        response = alignment_report(alignment, None)
        response.updated_at = project.updated_at
        response.plan_id = plan_fingerprint(project, response)
        if not request.confirm_apply or not alignment.moves:
            return response
        # Applied by shot id from the alignment's own placements, which are the whole tiling —
        # unmoved windows included — so the contiguity the core builds structurally is the
        # contiguity that lands in the manifest rather than being re-derived here.
        by_id = {shot.id: shot for shot in project.shots}
        for window, placement in zip(stored, alignment.placements):
            shot = by_id[window.id]
            shot.start = placement.start
            shot.duration = placement.duration
        saved = store.save(project)
        response.project = saved
        response.applied = True
        response.updated_at = saved.updated_at
        response.plan_id = plan_fingerprint(saved, response)
        return response

    @app.post(
        "/api/projects/{project_id}/timeline/fill-in",
        response_model=FillInResponse,
    )
    def fill_in_timeline(project_id: str, request: FillInRequest) -> FillInResponse:
        """Step three of a populate: what is inside each window. Never where it sits.

        `fill_in_shots` makes every decision, and it is the function a chained populate calls
        too. It is pure and asks no model — the model's content half arrived on the lay-out
        report this call carries — so the report and the confirm compute the same thing, and
        the confirm is a write rather than a second reading.

        **The windows are sacred and this route may not touch them**, which is what lets it
        inherit none of lay-out's refusals: a locked shot, an approved take and a render in
        flight all refuse a *lay-out*, because a lay-out replaces the windows those
        protections were placed on; they do not refuse this. Three things make that a
        guarantee rather than an intention, `clean_shot_prompts`' rule:

        * the write assigns the five content fields and nothing else — no branch here reads or
          writes `start`, `duration`, `mode`, `status`, `locked`, `latest_output`,
          `approved_output` or the takes;
        * every row of the report is matched to the shot at its index and refused unless that
          shot's window is still the window the row was laid out for
          (`FILL_IN_WINDOWS_CHANGED`);
        * `prompt_cleanup.window_fingerprint` is taken before the writes and compared after,
          and a mismatch **refuses without saving**.

        Report first, apply on confirm — `snap_timeline_cuts`' shape. Without `confirm_apply`
        this route **does not call `store.save`** and the response carries no project at all.

        Nothing renders, arms, queues or approves. `comfy` is not touched and no `status`
        moves; a rendered shot keeps its take and its job record, and the Director decides
        what to re-render.
        """
        project = get_project(project_id)
        plan = request.plan
        if (
            plan is None
            or plan.layout is None
            or not plan.windows
            or not plan.plan_id
        ):
            raise HTTPException(status_code=422, detail=FILL_IN_NO_PLAN)
        if plan.updated_at is None or plan.updated_at != project.updated_at:
            raise HTTPException(status_code=409, detail=PROJECT_CHANGED_REFUSAL)
        if plan_fingerprint(project, plan) != plan.plan_id:
            raise HTTPException(status_code=422, detail=FILL_IN_PLAN_MISMATCH)
        # Matched in manifest order, which is the order `window_fingerprint` and `shot_label`
        # use — a report is about shot 12 of the list, not about the twelfth shot to play.
        shots = list(project.shots)
        mismatch = next(
            (
                row.index
                for row, shot in zip(plan.windows, shots)
                if (shot.start, shot.duration) != (row.start, row.duration)
            ),
            None,
        )
        if len(shots) != len(plan.windows) or mismatch is not None:
            raise HTTPException(
                status_code=422,
                detail=FILL_IN_WINDOWS_CHANGED.format(
                    expected=len(plan.windows),
                    found=len(shots),
                    detail=(
                        f"; shot {mismatch + 1} no longer sits at its reported window"
                        if mismatch is not None
                        else ""
                    ),
                ),
            )
        filled = fill_in_shots(alignment_from_report(project, plan))
        if any(
            (produced.start, produced.duration) != (shot.start, shot.duration)
            for shot, produced in zip(shots, filled)
        ) or len(filled) != len(shots):
            # Before the report is built, not only before the write: a report describing a
            # window the confirm would refuse to write is a report nobody can act on.
            raise HTTPException(status_code=500, detail=FILL_IN_WINDOWS_MOVED)
        response = FillInResponse(
            filled=len(filled),
            shots=[
                FillInShotRow(
                    index=index,
                    start=shot.start,
                    duration=shot.duration,
                    prompt=shot.prompt,
                    citations=list(shot.citations),
                    singing=shot.singing,
                    use_song_audio=shot.use_song_audio,
                    seed=shot.seed,
                )
                for index, shot in enumerate(filled)
            ],
            message=f"{len(filled)} shots filled in; no window moved",
        )
        response.updated_at = project.updated_at
        response.plan_id = plan_fingerprint(project, response)
        if not request.confirm_apply:
            return response
        # The guarantee, taken on the project about to be written and checked against it after.
        geometry = window_fingerprint(project)
        for index, (shot, produced) in enumerate(zip(shots, filled)):
            # Validated as a whole Shot rather than assigned field by field,
            # `asset_replacement`'s rule and for its reason: this is what runs
            # `Shot._reconcile_citations`, so `asset_ids` is rebuilt as the projection of the
            # new reference-role citations instead of being kept in sync by a second hand.
            project.shots[index] = Shot.model_validate(
                {
                    **shot.model_dump(),
                    "prompt": produced.prompt,
                    "citations": [
                        citation.model_dump() for citation in produced.citations
                    ],
                    "singing": produced.singing,
                    "use_song_audio": produced.use_song_audio,
                    "seed": produced.seed,
                }
            )
        if window_fingerprint(project) != geometry:
            # Nothing has been saved at this point, and nothing will be. Unreachable by
            # construction — the loop above carries `id`, `start` and `duration` across
            # untouched — which is exactly why it is checked rather than argued.
            raise HTTPException(status_code=500, detail=CLEAN_PROMPTS_WINDOWS_MOVED)
        response.project = store.save(project)
        response.applied = True
        return response

    @app.post(
        "/api/projects/{project_id}/timeline/populate",
        response_model=PopulateTimelineResponse,
    )
    async def populate_timeline(
        project_id: str, request: PopulateTimelineRequest
    ) -> PopulateTimelineResponse:
        """Stage 4 of the Director's user workflow: one button lays out the whole plan.

        **This route is a caller, not a fourth implementation.** It runs the same three steps
        the `lay-out`, `line-up` and `fill-in` routes run, in the same order, through the same
        three module-level functions — `lay_out_shots`, `line_up_shots`, `fill_in_shots` —
        and its own body is the four lines that join them plus the single save. A chain with
        its own copy of any step would be a second spelling of a rule this codebase has twice
        been bitten by having two of (the reference-map numbering, the readiness/submit
        staleness test), and the byte-identity test that pins this change would go on passing
        while the two drifted.

        The model's answer is treated as *shape*, never as arithmetic: its shots carry the
        story (prompts, relative lengths, order), and `populate_windows` repairs the
        geometry into what assembly will later demand — contiguous from 0 to the song's
        end, every window inside H3's reliable 4–15 s range. The one number the model is
        *held* to is how many shots it returns, and that is a judgement about shape, not
        arithmetic: `populate_required_shots` computes the count, the instruction states it
        three times over as a hard constraint, the reply is counted rather than believed, and
        a short one buys exactly one guided retry at a lower temperature with the shortfall
        named in numbers. Each tiled window draws its prompt from the proposal whose
        proportional span of the song contains it (`proposal_for_position`), so a count repair
        cannot orphan a window from the story. The shots land as ordinary drafts — mode and
        expansion remain the existing lanes' acts, exactly as the workflow describes ("this
        is also when the prompts for each shot would be Expanded").

        **One confirmation, asked by the step that would violate the protection.** Each step's
        own route is report-then-confirm on its own terms, but three modal confirmations to
        lay out one timeline would be worse than the one button this replaces. Lay-out is the
        destructive step, so its consent — `confirm_replace`, in populate's own words, with
        the browser showing the same warning — is the consent for the whole pass: line-up
        writes nothing at all, and fill-in writes content into windows that same consent
        created. The refusals are `lay_out_protections`, the one implementation the `lay-out`
        route also calls, asked in the order populate has always asked them and before the
        model is spent.

        The consent is required up front here rather than answered by a report, and that is
        the one place this route deliberately differs from the `lay-out` route it calls: a
        chained populate has no report step to confirm against — it never had one — and
        adding one would either spend the 300 s model call twice or hand back a plan the
        Director has to send again. The standalone route is where a layout can be read before
        it lands.

        Destructive by design and doubly guarded: the browser shows the warning, and the
        route refuses without `confirm_replace` in the same words — while shots carrying
        protections (approval, a lock) refuse populate entirely by name, because a
        protection that vanishes with the timeline it protected was never a protection.
        """
        project = get_project(project_id)
        lay_out_protections(project)
        if not request.confirm_replace:
            raise HTTPException(status_code=422, detail=POPULATE_CONFIRM_REFUSAL)
        # Lay it out — the model call, the count enforcement, the re-read across it, and the
        # tiling. Everything after this point is arithmetic over what it returned.
        layout = await lay_out_shots(
            project,
            director=director,
            two_stage=request.two_stage,
            reread=lambda: get_project(project_id),
            variance=request.variance,
        )
        # Line it up — move each cut onto the nearest moment the track leaves voiceless, then
        # measure what each window now covers. **No second confirmation is asked for it**, and
        # that is the ruling rather than an oversight: `confirm_replace` above is consent to
        # replace this timeline's windows, these windows were created by the lay-out call it
        # consented to seconds ago, and there is no shot on the timeline for a protection to
        # be about — `lay_out_protections` has already refused if there were. The standalone
        # `line-up` route is where a Director lining up a plan they have been editing reads a
        # report first and confirms it.
        #
        # **Translated rather than allowed to fall through, which is the half this route was
        # missing until 2026-08-23.** Both standalone siblings wrap their `line_up_shots` call
        # and answer 422 in the core's own sentence; this one did not, so a plan geometry the
        # snapper has no seam for — `SNAP_HOLE`, `SNAP_NESTED` — came back as an opaque 500
        # *after* the Director had spent a ~110 s model call. There is nothing for a client to
        # read in a 500 and nothing for the Director to do about it. The refusals name the
        # shape and say what to fix, so they are what goes on the wire here too.
        try:
            alignment = line_up_shots(layout, tolerance=request.snap_tolerance)
        except TimelineError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        # Fill it in — prompts, citations, singing, seeds.
        shots = fill_in_shots(alignment)
        # The re-read project lay-out returned, carrying the section layer it assigned. One
        # save for the whole pass: the intermediates travelled as data, so no half-populated
        # timeline is ever visible on disk.
        project = layout.project
        project.shots = shots
        saved = store.save(project)
        return PopulateTimelineResponse(
            proposed=len(layout.proposals),
            created=len(saved.shots),
            project=saved,
            moved=alignment.moved,
            skipped=len(alignment.skips),
            # Read off the manifest that was actually written, so the flag describes the project
            # the Director is now looking at rather than the one this route read minutes ago
            # before the model call. Pure, and no model, clock or file is touched to produce it —
            # `[]` for every project that has declared no vocal type, which is every project that
            # existed before this feature.
            #
            # **Nothing here consumes the declaration.** The shots above are wired exactly as they
            # were: no citation is chosen by a line's singer, and no window is attributed to a
            # slot. That is pass 2, deliberately unbuilt — the Director asked for the data to
            # exist and be checkable first, so it can be tagged on a real song before the logic
            # that reads it is written against a guess.
            cast_notices=vocal_cast_problems(saved),
        )


    @app.post(
        "/api/projects/{project_id}/timeline/clean-prompts",
        response_model=CleanPromptsResponse,
    )
    async def clean_shot_prompts(
        project_id: str, request: CleanPromptsRequest
    ) -> CleanPromptsResponse:
        """Take the asset labels out of an existing plan's prose, and change nothing else.

        The Director's report on their live 33-shot plan (2026-08-21): the prompts read like an
        inventory. "Extreme close up of Crimson Lips Close-up while HarderFaster performs" says
        "close up of ... Close-up"; "Blue Haze Atmosphere surrounding the Dusk Warehouse Bed" is
        two library labels and a preposition. 24 of the 33 carry a non-character asset's display
        name. The cause is `populate`'s old citation rule, which attached a picture to a shot by
        scanning the prose for that picture's label — so naming the asset in the sentence *was*
        the citation mechanism. `director.PlannedShot.assets` retires that for new plans. This
        retires it for the plan that already exists.

        **The windows are sacred and this route may not touch them.** The Director: *"I have
        done some timeline touch ups and am generally happy for now."* Those 33 edges are
        hand-placed against musical timing and include several deliberate micro-cuts (0.5 s,
        1.75 s, 1.83 s, 1.88 s, 2.08 s, 2.67 s); re-populating would destroy that work with no
        way back, and re-populating is exactly what a prose fix must not become. Three things
        make that a guarantee rather than an intention:

        * the write loop assigns `Shot.prompt` and nothing else — no branch here reads or
          writes `start`, `duration`, `citations`, `singing`, `use_song_audio`, `seed`, `mode`,
          `status`, `locked`, `latest_output`, `approved_output` or the takes;
        * `prompt_cleanup.window_fingerprint` is taken before the rewrites are applied and
          compared after, and a mismatch **refuses without saving** — a check on data rather
          than a claim about code;
        * `prompt_cleanup.citation_fingerprint` is compared per shot across the same boundary,
          because the citations are precisely what makes removing a name from the prose safe.
          The ids are on the shot already; the sentence never had to carry them.

        Report first, apply on confirm — `snap_timeline_cuts`' shape, `populate`'s
        `confirm_replace` at bottom. Without `confirm_apply` this route **does not call
        `store.save`** and the response carries no project at all, so "nothing was written" is
        visible on the wire. Every row carries the old prose beside the proposed prose, because
        this report exists to be read by a person before it lands.

        **And the report is what lands.** Until 2026-08-21 it was not: the model was asked
        before `confirm_apply` was looked at, so the confirming call was a second, independent
        generation at `PLAN_TEMPERATURE = 0.7`. Measured on the Director's live plan that night
        — 24 rewrites read and approved, **one landed as different text**. The confirm now
        carries the report back as `plan`, this route asks no model on that path at all, and
        `clean_prompts_plan_writes` refuses any plan it cannot tie to a report it emitted. See
        the plan-carrying block above.

        **A shot with no echo is not sent to the model at all** and is reported as already
        clean. Asking for a rewrite of a prompt nobody complained about is how a hand-reviewed
        plan quietly acquires 33 changes when it needed 24.

        **Two protections, and they are the existing ones in the existing words.**
        `EXPANSION_LOCKED_NOTICE` for a lock — an explicit hands-off only the Director clears —
        and `EXPANSION_RENDERED_NOTICE` for a render **in flight**, which is the one arm of that
        sentence this route still honours: a job executing right now was submitted with the
        prompt as it stands, and rewriting it underneath would leave the record describing a
        submission that never happened.

        **A rendered or approved shot is rewritten, and told about.** `shot_write_refusal` is
        deliberately not the gate, exactly as it is not the gate for `replace_asset_citations`,
        and the Director's ruling there transfers with room to spare: a citation change on a
        rendered shot is fine because the take is untouched, and prose is *less* coupled to a
        take than a citation is — the prompt each take was actually submitted with is recorded
        on its job and in the take's own PNG metadata, so nothing is lost by editing the shot's
        text. `expansion_write_refusal` already carves the same exemption. What survives is the
        report: `CLEAN_PROMPTS_RENDERED_NOTE` and `CLEAN_PROMPTS_APPROVED_NOTE` name them and
        count them before the confirm.

        Nothing renders, arms, queues or approves. `comfy` is not touched on any path and no
        `status` moves.
        """
        project = get_project(project_id)
        # The confirm, and the only path that writes. No model is asked on it: `plan` is the
        # report a person read, and the prose it writes is that report's own `after` strings.
        if request.confirm_apply:
            response, pending = clean_prompts_plan_writes(project, request)
            if not pending:
                return response
            # The guarantee, taken on the project about to be written and checked against it
            # after.
            geometry = window_fingerprint(project)
            citations = {shot.id: citation_fingerprint(shot) for shot in project.shots}
            for shot, prose in pending:
                shot.prompt = prose
            if window_fingerprint(project) != geometry or any(
                citation_fingerprint(shot) != citations[shot.id]
                for shot in project.shots
            ):
                # Nothing has been saved at this point, and nothing will be. Unreachable by
                # construction — the loop above assigns one field — which is exactly why it is
                # checked rather than argued.
                raise HTTPException(status_code=500, detail=CLEAN_PROMPTS_WINDOWS_MOVED)
            response.project = store.save(project)
            response.applied = True
            return response
        # Detection runs over the **whole** library, not `citable_assets`. The roster rule is
        # about what a model is offered; this is about what is already written down, and a
        # label that reached the prose before a promotion hid its asset is still a label.
        library = list(project.assets)
        # Two protections and no more, read from the Shot and the job records directly rather
        # than through `shot_write_refusal`, whose `rendered` arm this route does not honour —
        # `replace_asset_citations`' line, for `replace_asset_citations`' reason.
        protected: dict[str, str] = {}
        for shot in project.shots:
            if shot.locked:
                protected[shot.id] = EXPANSION_LOCKED_NOTICE.format(
                    shots=shot_label(project, shot)
                )
            elif shot_render_in_flight(project, shot):
                protected[shot.id] = EXPANSION_RENDERED_NOTICE.format(
                    shots=shot_label(project, shot)
                )
        echoes = {
            shot.id: labels
            for shot in project.shots
            if (labels := echoed_labels(shot.prompt, library))
        }
        if not echoes:
            raise HTTPException(status_code=422, detail=CLEAN_PROMPTS_NOTHING_TO_CLEAN)
        # The selection sent to the model: echoing and not protected. Built before the call so
        # a wholly protected plan costs no model time at all.
        askable = {
            shot_id: labels
            for shot_id, labels in echoes.items()
            if shot_id not in protected
        }
        if not askable:
            raise HTTPException(
                status_code=422,
                detail=CLEAN_PROMPTS_ALL_PROTECTED.format(
                    shots=", ".join(
                        shot_label(project, shot)
                        for shot in ordered_shots(project)
                        if shot.id in echoes
                    )
                ),
            )
        # The text each rewrite will have been made from, kept across the await. A rewrite is
        # an edit *of a particular sentence*, so the sentence has to be part of the answer's
        # identity — see `CLEAN_PROMPTS_EDITED`, and the loop below that spends this.
        asked_prompts = {
            shot.id: shot.prompt for shot in project.shots if shot.id in askable
        }
        try:
            # `director.expand`'s wire and `ShotExpansion`'s contract, selected by system
            # prompt — the DP pass's own idiom (`dp_prompt.DP_SYSTEM_PROMPT`), because
            # "revised prompts addressed by shot id" is exactly this pass's output shape too.
            # No new director method, no new schema, and every existing `expand` double keeps
            # working.
            answer = await director.expand(
                expansion_input=prompt_cleanup_input(project, askable),
                system_prompt=PROMPT_CLEANUP_SYSTEM_PROMPT,
            )
        except DirectorUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except DirectorError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        # Re-read after the await — a local model can hold this open for minutes, and the
        # Director may have locked, edited, split or deleted a shot meanwhile. The fresh
        # project is the one being reported on, so it is also the one being matched and
        # re-scanned against: a lock set in that window protects, a shot deleted in it has no
        # row, and a prompt **edited by hand** in that window is dropped and named
        # (`CLEAN_PROMPTS_EDITED`) rather than overwritten by a rewrite of the text it
        # replaced. The docstring here claimed the opposite until 2026-08-21 — that such a
        # prompt was "re-examined on its new text" — and the code never did it: `labels` and
        # `before` were recomputed from the fresh prompt while `answered[shot.id]` stayed a
        # rewrite of the stale one, and the stale rewrite was what got written.
        project = get_project(project_id)
        library = list(project.assets)
        protected = {}
        for shot in project.shots:
            if shot.locked:
                protected[shot.id] = EXPANSION_LOCKED_NOTICE.format(
                    shots=shot_label(project, shot)
                )
            elif shot_render_in_flight(project, shot):
                protected[shot.id] = EXPANSION_RENDERED_NOTICE.format(
                    shots=shot_label(project, shot)
                )
        # First answer per id wins, and a repeat is reported rather than dropped in silence —
        # `fill_section_looks`' rule: a model that repeats an id is not offering a second
        # opinion to arbitrate, and letting the later copy win would make the written prose
        # depend on emission order.
        answered: dict[str, str] = {}
        duplicated: set[str] = set()
        for item in answer.shots:
            if item.shot_id in answered:
                duplicated.add(item.shot_id)
                continue
            answered[item.shot_id] = item.prompt
        rows: list[CleanPromptRow] = []
        for shot in ordered_shots(project):
            labels = echoed_labels(shot.prompt, library)
            row = CleanPromptRow(
                shot_id=shot.id,
                label=shot_label(project, shot),
                start=shot.start,
                duration=shot.duration,
                rewritten=False,
                labels=labels,
                before=shot.prompt,
                provenance=(
                    "approved"
                    if shot.approved_output or shot.status == "approved"
                    else "rendered"
                    if shot_render_provenance(shot)
                    else ""
                ),
            )
            if not labels:
                # Completely alone. Not sent, not rewritten, and reported so the count adds up.
                row.reason = CLEAN_PROMPTS_ALREADY_CLEAN
            elif reason := protected.get(shot.id, ""):
                row.reason = reason
            elif shot.id not in answered:
                row.reason = CLEAN_PROMPTS_UNANSWERED
            elif shot.prompt != asked_prompts.get(shot.id, shot.prompt):
                # The hand-edit rule. The rewrite in hand is an edit of a sentence that no
                # longer exists, so applying it would replace the Director's own words with a
                # revision of the words they threw away. Dropped and named — a named skip
                # beats a silent guess, and re-asking would spend the call a second time.
                row.reason = CLEAN_PROMPTS_EDITED
            elif problem := rewrite_rejection(
                answered[shot.id], original=shot.prompt, labels=labels
            ):
                row.reason = problem
                # Carried so the Director can see *what* was refused, not only that something
                # was — `EXPANSION_REJECTED_NOTICE`'s `raw` in a field of its own.
                row.after = answered[shot.id].strip()[:NOTICE_RAW_LIMIT]
            else:
                row.rewritten = True
                row.after = answered[shot.id].strip()
            if shot.id in duplicated and not row.reason:
                row.reason = CLEAN_PROMPTS_DUPLICATED
            rows.append(row)
        clean = sum(1 for row in rows if not row.labels)
        rewritten = sum(1 for row in rows if row.rewritten)
        changed = [row for row in rows if row.rewritten]
        notes = [
            wording.format(
                count=len(group), shots=", ".join(row.label for row in group)
            )
            for wording, group in (
                (
                    CLEAN_PROMPTS_APPROVED_NOTE,
                    [row for row in changed if row.provenance == "approved"],
                ),
                (
                    CLEAN_PROMPTS_RENDERED_NOTE,
                    [row for row in changed if row.provenance == "rendered"],
                ),
            )
            if group
        ]
        notes.append(CLEAN_PROMPTS_REVIEW_NOTE)
        # Ids the model invented or copied from another project matched no shot above and are
        # counted here rather than created into one — a new shot would be a new window, which
        # is the one thing this pass exists not to do.
        known = {shot.id for shot in project.shots}
        stray = sum(1 for shot_id in answered if shot_id not in known)
        summary = CLEAN_PROMPTS_REPORT.format(
            echoing=len(rows) - clean,
            examined=len(rows),
            rewritten=rewritten,
            skipped=len(rows) - clean - rewritten,
            clean=clean,
        )
        if stray:
            summary += CLEAN_PROMPTS_STRAY.format(count=stray)
        response = CleanPromptsResponse(
            applied=False,
            examined=len(rows),
            echoing=len(rows) - clean,
            clean=clean,
            rewritten=rewritten,
            skipped=len(rows) - clean - rewritten,
            rendered=sum(1 for row in changed if row.provenance == "rendered"),
            approved=sum(1 for row in changed if row.provenance == "approved"),
            notes=notes,
            shots=rows,
            message=summary,
            updated_at=project.updated_at,
        )
        # Minted last, over the finished report, and over the *fresh* project's revision — the
        # one the confirm will be checked against. Nothing is written on this path.
        response.plan_id = plan_fingerprint(project, response)
        return response



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
