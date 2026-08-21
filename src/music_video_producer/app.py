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
from collections import Counter
from collections.abc import AsyncIterator, Callable, Container
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, BeforeValidator, Field, StringConstraints

from .assembly import (
    ASSEMBLY_FPS,
    DEFAULT_EXPORT_PRESET,
    EXPORT_PRESETS,
    AudioOverlay,
    ClipWindow,
    ExportProgress,
    assembly_plan,
    assembly_refusals,
    concat_args,
    concat_manifest,
    parse_progress_us,
    probe_duration_args,
    probe_streams_args,
    probe_take_args,
    trim_args,
    verification_problems,
    with_progress,
)
from .asset_replacement import ReplacementChange, asset_replacement_plan
from .batch import (
    JOB_NEVER_SUBMITTED,
    PENDING_SUBMISSION_PROMPT_ID,
    TERMINAL_JOB_STATUSES,
    ReadinessReport,
    RenderStatusReport,
    accept_submission,
    apply_job_history,
    batch_targets,
    prompt_is_missing,
    prompt_rejection,
    readiness_refusal,
    readiness_report,
    reconcilable_jobs,
    reconcile_render_jobs,
    render_status_report,
    shot_label,
    supersede_target_jobs,
)
from .comfy import ComfyClient, ComfyError, ComfyProgressListener, ProgressTracker
from .config import Settings
from .director import (
    PLAN_TEMPERATURE,
    DirectorBudgetExhausted,
    DirectorClient,
    DirectorError,
    DirectorUnavailable,
    director_result_schema,
    document_rejection,
)
from .dp_prompt import DP_SYSTEM_PROMPT, dp_input
from .h3_expansion_prompt import system_prompt as h3_system_prompt
from .h3_prompt import check as h3_check
from .h3_prompt import check_reference_bounds, normalize_audio_fields
from .models import (
    ASSET_ROLE_LABELS,
    NOTICE_RAW_LIMIT,
    SHOT_MODE_SPECS,
    Asset,
    AssetCitation,
    MessageNotice,
    Project,
    RenderJob,
    Shot,
    ShotStatus,
    SingingState,
    Song,
    SongSection,
    TreatmentMessage,
    VisionInspectionRecord,
    assets_for_proposal,
    citable_assets,
    citations_in_role,
    dangling_citations,
    default_setting_asset,
    identity_sheet_ids,
    mode_specification_problems,
    new_id,
    numbered_references,
    prefer_identity_sheets,
    reference_slot_totals,
    resolve_shot_mode,
    song_audio_tag,
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
from .store import ProjectChangedDuringSave, ProjectNotFound, ProjectStore
from .timeline import (
    H3_FPS,
    H3_MAX_SHOT_SECONDS,
    H3_MIN_SHOT_SECONDS,
    MIN_SINGING_VOCAL_SECONDS,
    SNAP_TOLERANCE_DEFAULT,
    SNAP_TOLERANCE_MAX,
    TimelineError,
    align_lyric_blocks,
    anchored_label,
    assistant_input,
    build_director_timeline,
    expansion_input,
    lyric_blocks,
    ordered_shots,
    over_render_frames,
    over_render_lead,
    over_render_window,
    populate_windows,
    proposal_for_position,
    proposed_sections_from_alignment,
    repair_sections,
    section_looks_input,
    shot_expansion_input,
    shot_vocal_overlap,
    snap_cut_plan,
    song_section,
)
from .transcription import merge_vocal_spans, transcribe_song_words
from .vram import CliUnloader, LlmEjector
from .workflows import (
    H3_DEFAULT_PROFILE,
    H3_DIRECTOR_DEFAULT_HEIGHT,
    H3_DIRECTOR_DEFAULT_WIDTH,
    H3_REFERENCE_LIMITS,
    LTX25_ENHANCE_SEED,
    SONGPLANNER_DEFAULT_DURATION_HEADROOM,
    SONGPLANNER_MAX_DURATION_HEADROOM,
    WorkflowCatalog,
    audio_replace_lengths,
    build_audio_replace_payload,
    build_flux_payload,
    build_h3_director_payload,
    build_h3_image_edit_payload,
    build_h3_keyframe_payload,
    build_h3_reference_payload,
    build_ltx25_enhance_payload,
    build_multiview_payload,
    build_music3_payload,
    build_songplanner_invented_payload,
    build_songplanner_known_lyrics_payload,
    image_edit_prompt,
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
        "approved_start",
        "approved_duration",
        "latest_take_lead",
        "latest_take_start",
        "latest_take_duration",
        "trim_nudge",
        "mix_take_audio",
        "flagged",
    }
)

#: How the reference map declares a keyframe-role picture, per MiniMax's guide §2.2.2 — read
#: from the bundled ``Video_Prompt_Writing_Guide.pdf``, never copied: the guide's own example is
#: this sentence shape, and the retention marker is the guide's fixed English value for a frame
#: anchor. `[Shot 1]` for the first frame because the guide has `[Shot 1]` mark the opening shot
#: of every prompt; the last frame is tied to *the final shot* in the guide's own alignment
#: language ("the last frame must be reached by the final [Shot N]"), and an un-expanded intent
#: declares no shot numbers this map could name, so the final shot is named as what it is rather
#: than guessed at as an index.
#:
#: A plain reference keeps the exact line this route has always built — `<Picture N> is
#: {label}` — so a shot with no keyframe roles is byte-identical to before these existed.
REFERENCE_MAP_ROLE_TAGS = {
    "first": "<Picture {number}> is the first frame of [Shot 1] (fully_preserved), showing {label}",
    "last": "<Picture {number}> is the last frame of the final shot (fully_preserved), showing {label}",
}

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
    base = f"Reference map: {'; '.join(tags)}. {shot.prompt}"
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


def reference_map_tag_lines(project: Project, shot: Shot) -> list[str]:
    """The submit walk's tag sentences, computed outside the submit route.

    Byte-for-byte the lines `generate_h3`'s reference branch builds — same
    `models.numbered_references` walk, same per-kind numbering, same role wording, same
    master-song line last — so a prompt stored ahead of submission names exactly the
    slots the payload will fill. A citation whose asset is missing writes no line here
    where the route 422s: this function writes text, and the render is where a dangling
    citation must stop the world. It still consumes its *number* from the shared walk, so
    the tags that do get written are the tags the specialist was handed.

    Each label carries the Asset's stored appearance anchor when it has one, so the map
    reads `<Picture 1> is Lucy, a woman in a red leather jacket and black boots` rather
    than a bare name that tells the sampler nothing about the person it is holding fixed.
    `timeline.anchored_label` is the one composition — including how a per-shot rename and
    an anchor compose — and an asset with no anchor returns the bare label unchanged, so
    an anchor-free project's map is byte-for-byte the map it has always been.
    """
    tags: list[str] = []
    for numbered in numbered_references(project, shot):
        asset = numbered.asset
        if asset is None:
            continue
        label = anchored_label(asset, shot.reference_labels.get(asset.id, asset.name))
        if numbered.citation.role in REFERENCE_MAP_ROLE_TAGS:
            tags.append(
                REFERENCE_MAP_ROLE_TAGS[numbered.citation.role].format(
                    number=numbered.number, label=label
                )
            )
            continue
        tags.append(f"{numbered.tag} is {label}")
    if shot.use_song_audio:
        tags.append(
            f"<Audio {song_audio_tag(project, shot)}> is the master song for synchronization"
        )
    return tags


#: What one shot's reference-bounds refusal says. The problems are the checker's own sentences —
#: one wording for the rule, in `h3_prompt.check_reference_bounds`, rather than a second copy here
#: that can drift from the one the expansion retry loop feeds back to the model.
REFERENCE_BOUNDS_REFUSAL = (
    "Not submitted: {shot} cites a reference slot it does not have. {problems} Nothing was sent "
    "to ComfyUI, because a render conditioned on a slot nothing fills comes back plausible and "
    "wrong rather than failing. Attach the media or renumber the tag, then submit again."
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
    "the default profile (about 2 min on turbo), so this is a real GPU commitment. "
    "Send confirm_gpu=true to proceed."
)
GENERATE_BATCH_EMPTY_READY = (
    "No shots are ready to generate. Mark shots ready first — or tick Replace existing "
    "takes to re-render settled shots."
)
GENERATE_BATCH_EMPTY_FLAGGED = "No shots are flagged for re-render."

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

#: See the populate route's window_mean comment: the creator's "fastest / safest" preset,
#: measured on this card as the difference between minutes and hours per shot.
POPULATE_TARGET_WINDOW_SECONDS = 5.2

#: The *enforced* ceiling the tiling repair applies, tighter than H3's 15 s legality.
#: Guidance alone is not enough: on the first 5.2 s-target run the local model simply
#: echoed the previous plan's 9 s windows out of its own context, and 9 s windows are
#: the measured 2.2-hour cliff. The bound is what the target means; a Director who
#: wants longer shots edits them deliberately, one at a time, in the timeline.
POPULATE_MAX_WINDOW_SECONDS = 6.0

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
    "with contiguous shots — no gaps, no overlaps — each between 4 and 6 seconds. "
    "Deliberately mix lengths inside that band: quick 4-second cuts on "
    "high-energy beats, 6-second holds on glamour or establishing moments; do not make "
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


class PopulateTimelineResponse(BaseModel):
    """What populate did: the counts a Director sanity-checks first, then the project."""

    proposed: int
    created: int
    project: Project


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


class SnapCutMove(BaseModel):
    """One cut that would move, named by both shots that share it.

    `gap` is how long the voiceless stretch it lands in is, carried on the wire because the
    length is what tells a Director what kind of opportunity the cut found — a one-second
    breath is an extended shot, four seconds is room for something else entirely. It is
    `timeline.CutMove.gap` verbatim; nothing is decided here.
    """

    before: str
    after: str
    boundary: float
    proposed: float
    shift: float
    gap: float


class SnapCutSkip(BaseModel):
    """One cut that would not move, and the sentence saying why."""

    before: str
    after: str
    boundary: float
    reason: str


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
    """One batch, one confirmation. `scope` picks FR-4's ready set or AD-5's flagged set;
    `replace_existing` widens the ready scope to settled, unprotected shots; `profile`
    applies one evidenced sampling bundle to the whole batch (per-shot profiles are Ask
    First). `confirm_gpu` is the acknowledgement itself — a client sends true only after
    showing the warning, exactly like `confirm_song_replacement`."""

    confirm_gpu: bool = False
    scope: Literal["ready", "flagged"] = "ready"
    replace_existing: bool = False
    profile: Literal["default", "turbo", "turbo-references2v"] = "default"


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
    "Cancelled by the Director before it finished. Nothing was produced; render again "
    "re-opens the shot."
)


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
    # node with the exports' own `disabled` (their creator launches ComfyUI with
    # `--use-sage-attention`; this installation does not). When the Director opts in via
    # MVP_SAGE_ATTENTION, the value is patched here — one wrapper over `comfy.submit`, so
    # every current and future adapter is covered and no builder or digest moves. Blank
    # (the default) leaves every payload byte-identical to the evidence.
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

    def get_project(project_id: str) -> Project:
        try:
            return store.get(project_id)
        except ProjectNotFound as error:
            raise HTTPException(status_code=404, detail="Project not found") from error

    def settle_unsubmitted_jobs(project: Project, *jobs: RenderJob) -> None:
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

        **A save race here is swallowed, not raised.** The caller is on its way to a 502 that
        names why the submission failed, which is the fact the Director needs; converting it
        into a 409 about the manifest would report the wrong failure. What is left behind when
        this save is refused is a record still carrying `PENDING_SUBMISSION_PROMPT_ID`, which
        the reconciler settles with this same sentence after three unknown ticks.
        """
        for job in jobs:
            job.status = "error"
            job.error = JOB_NEVER_SUBMITTED
            job.missing_ticks = 0
        try:
            store.save(project)
        except ProjectChangedDuringSave:
            logger.warning(
                "Could not settle %d unsubmitted job record(s) on project %s; the reconciler "
                "will settle them from the pending prompt id",
                len(jobs),
                project.id,
            )

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
            raise HTTPException(status_code=409, detail=PROJECT_CHANGED_REFUSAL)
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
        # Render state and approval are the dedicated routes', not a save's. Both gates compare
        # the body against the stored Shot and refuse only a *difference*, so an ordinary save --
        # which round-trips both fields on every Shot -- is untouched. After the Song gate rather
        # than before it, so a body that changes both still gets the Song's refusal it always got.
        _require_in_flight_status_kept(current, project.shots)
        _require_approval_unchanged(current, project.shots)
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
        # Every Asset's appearance anchor is server-owned here, for the third time this exact
        # hole has been found in this exact route. `consistency_prompt` is a defaulted `str`,
        # so a body that simply omits it — which is what every client written before it
        # existed sends, and what any hand-rolled API call sends — arrives as `""` and one
        # ordinary save would blank the Director's own text on every asset at once. Adopting
        # the stored value by id means this route cannot write the field in either direction:
        # the dedicated `PUT .../consistency-prompt` is its one writer, which is also what
        # keeps it out of reach of anything a model can call.
        #
        # An asset in the body that the stored project does not hold gets `""` rather than
        # whatever it carried, by the same rule: an anchor that arrived on this route was not
        # set by the Director on the route that sets anchors.
        stored_anchors = {asset.id: asset.consistency_prompt for asset in current.assets}
        for asset in project.assets:
            asset.consistency_prompt = stored_anchors.get(asset.id, "")
        # The declared location is server-owned on the same argument, and it is the *fourth*
        # time this route has been the hole: `default_setting_id` is a defaulted `str`, so
        # every client written before it existed sends `""` and one ordinary save would clear
        # the Director's choice. `PUT .../default-setting` is its one writer, which also keeps
        # it out of reach of anything a model can call.
        project.default_setting_id = current.default_setting_id
        return store.save(project)

    @app.put("/api/projects/{project_id}/shots", response_model=Project)
    def replace_shots(project_id: str, request: ShotListRequest) -> Project:
        project = get_project(project_id)
        # Enforced only when sent — see `ShotListRequest.updated_at`. The wording is
        # `replace_project`'s, because it is the same rule met on the other manifest write.
        if request.updated_at is not None and request.updated_at != project.updated_at:
            raise HTTPException(status_code=409, detail=PROJECT_CHANGED_REFUSAL)
        # The same two gates the whole-project `PUT` carries, on the same argument. This route is
        # the *narrower* sibling and has been the guard hole at least as often, because a client
        # that only wants to move a clip still sends every field of every Shot back.
        _require_in_flight_status_kept(project, request.shots)
        _require_approval_unchanged(project, request.shots)
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

    @app.post("/api/projects/{project_id}/song/align-lyrics", response_model=Project)
    def align_song_lyrics(project_id: str, request: AlignLyricsRequest) -> Project:
        """Hear the track, time the sheet's `[Tag]` blocks against it, fill the sections.

        The Director's ask (2026-08-20): "I did add the tags in the lyrics so that those
        would at least be clear... knowing where words are and arent is useful for knowing
        which Shots have words, when the cuts should happen, when the chorus and verses
        are." Three writes, all measured: `lyric_words` (every word Whisper hears, kept so
        nothing ever transcribes twice), `vocal_spans` (the singing-flag guard's evidence),
        and — when the plan has no sections, or `replace_sections` says to — the section
        boxes themselves, one per aligned block plus an Intro when the voice starts late,
        repaired by the same rules a populate proposal is.

        A sync `def`, deliberately: FastAPI runs it in the threadpool, and a CPU
        transcription of a whole track must not park the event loop for minutes.

        Prompts on the proposed sections are left empty — timing is measured, look is
        authored — and existing sections are never replaced without the flag: boxes the
        Director has dragged are their marks, not this route's.
        """
        project = get_project(project_id)
        if project.song is None or not project.song.path:
            raise HTTPException(status_code=422, detail=ALIGN_LYRICS_WITHOUT_SONG)
        if not lyric_blocks(project.song.lyrics):
            raise HTTPException(status_code=422, detail=ALIGN_LYRICS_WITHOUT_TAGS)
        if project.sections and not request.replace_sections:
            raise HTTPException(status_code=409, detail=ALIGN_LYRICS_SECTIONS_EXIST)
        words = project.song.lyric_words
        if not words or request.retranscribe:
            try:
                words = transcriber(resolve_song_path(project_id, project.song))
            except Exception as error:  # the dependency or the decode, named either way
                raise HTTPException(
                    status_code=502, detail=ALIGN_LYRICS_TRANSCRIBE_FAILED.format(error=error)
                ) from error
            project.song.lyric_words = words
            project.song.vocal_spans = merge_vocal_spans(words)
        aligned = align_lyric_blocks(project.song.lyrics, words)
        if not aligned:
            store.save(project)  # the transcription is still worth keeping
            raise HTTPException(status_code=422, detail=ALIGN_LYRICS_NOTHING_PLACED)
        project.sections = [
            SongSection(label=label, start=start, duration=length, prompt=prompt)
            for label, start, length, prompt in repair_sections(
                proposed_sections_from_alignment(aligned, project.song.duration),
                project.song.duration,
            )
        ]
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

    @app.delete("/api/projects/{project_id}/jobs/{job_id}", response_model=Project)
    async def cancel_job(project_id: str, job_id: str) -> Project:
        """Cancel one open render job: dequeue (and interrupt, when running) on ComfyUI,
        settle the record, release the shot.

        The gap the analyst named (2026-08-20): a mistaken Generate All could only be
        cleared from ComfyUI's own UI — and this same night proved what THAT leaves
        behind: pulled queue entries orphan their job records, which is exactly the
        stuck-"queued" state the reconciler's missing-ticks rule now cleans up. This
        route does both halves in one act, so nothing is left for the strike counter.
        """
        project = get_project(project_id)
        job = next((item for item in project.jobs if item.id == job_id), None)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.status in TERMINAL_JOB_STATUSES:
            raise HTTPException(
                status_code=422, detail=CANCEL_JOB_SETTLED.format(status=job.status)
            )
        if job.prompt_id:
            try:
                await comfy.cancel(job.prompt_id)
            except ComfyError as error:
                raise HTTPException(status_code=502, detail=str(error)) from error
        job.status = "cancelled"
        job.error = CANCEL_JOB_NOTE
        if job.kind == "h3":
            shot = next((item for item in project.shots if item.id == job.target_id), None)
            if shot and shot.status in ("queued", "running"):
                shot.status = "error"
        return store.save(project)

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: str, confirm_delete: bool = False) -> dict[str, str]:
        """Remove one project — manifest and media directory — for good.

        The gap the analyst named (2026-08-20): a night of experiments accumulates
        projects the switcher can never shed; eighteen had to be deleted by hand at the
        store level the same night. The confirmation flag is the song-replacement idiom:
        the first call without it is refused with the sentence naming what will be lost,
        so no client can delete by accident of a stray request.

        Takes rendered into ComfyUI's output tree are NOT touched: they live outside the
        project directory, other projects may reference study copies of them, and disk is
        the Director's to prune. Only the manifest and the project's own media go.
        """
        project = get_project(project_id)  # 404 before any confirmation talk
        if not confirm_delete:
            raise HTTPException(
                status_code=409,
                detail=DELETE_PROJECT_CONFIRM.format(
                    name=project.name, shots=len(project.shots)
                ),
            )
        shutil.rmtree(store.project_dir(project_id))
        return {"deleted": project_id}

    @app.put(
        "/api/projects/{project_id}/assets/{asset_id}/consistency-prompt",
        response_model=Project,
    )
    def replace_consistency_prompt(
        project_id: str, asset_id: str, request: AssetConsistencyRequest
    ) -> Project:
        """Set this Asset's appearance anchor — the Director's own words, and the only writer.

        The anchor wins over the generation prompt and over the vision summary everywhere a
        description of this asset is consumed (`timeline._asset_description` writes that
        ordering down), so it must never be written by anything that guesses. **Nothing in
        this application infers one**: no route derives it from `Asset.prompt`, the vision
        inspection route writes `vision` and only `vision`, no tool schema exposes it to a
        model, and the generic full-project `PUT` re-adopts the stored value rather than
        trusting a body. This route is the one door.

        Written onto the *stored* Asset rather than a rebuilt one, `replace_song_context`'s
        rule: there is no construction site here where `path`, `source` or `prompt_id` could
        be defaulted away by an edit that was only ever about one string.

        An empty body clears the anchor, which is what emptying the box means. Trimmed at the
        edges and bounded by `CONSISTENCY_PROMPT_LIMIT`, measured after trimming; the refusal
        happens before anything is assigned, so a rejected anchor leaves the asset untouched.
        """
        project = get_project(project_id)
        asset = next((item for item in project.assets if item.id == asset_id), None)
        if asset is None:
            raise HTTPException(status_code=404, detail="Asset not found")
        anchor = request.consistency_prompt.strip()
        if len(anchor) > CONSISTENCY_PROMPT_LIMIT:
            raise HTTPException(
                status_code=422,
                detail=CONSISTENCY_PROMPT_TOO_LONG.format(
                    name=asset.name, length=len(anchor), limit=CONSISTENCY_PROMPT_LIMIT
                ),
            )
        asset.consistency_prompt = anchor
        return store.save(project)

    @app.put("/api/projects/{project_id}/default-setting", response_model=Project)
    def replace_default_setting(
        project_id: str, request: DefaultSettingRequest
    ) -> Project:
        """Declare which library setting is this video's location — the one writer of it.

        The Director's report (2026-08-20): on a 30-shot plan whose brief specifies a location,
        the setting asset was cited by 5 shots, because whether a shot carried its environment
        reference depended entirely on whether the model happened to spell the asset's display
        name into prose. This is the half of the fix that does not depend on a model:
        `populate` gives the declared location to every new shot that named no location of its
        own, and names it in the instruction so the model has a chance to name it first.

        **Explicit, and therefore refusable and reversible.** Nothing infers this field — no
        route derives it from a library that happens to hold one setting, no tool schema exposes
        it to a model, and the generic full-project `PUT` re-adopts the stored value rather than
        trusting a body (`replace_consistency_prompt`'s rule, for the reason that route's
        docstring gives). An empty `asset_id` clears it, which is what "no location" means, and
        an unset field is a genuine no-op: `populate` writes exactly the citations it wrote
        before this existed.

        What it does *not* do is touch a single existing shot. It is a declaration about the
        project, read by the next plan; a sweep over a plan the Director already has is the
        silent bulk edit this codebase's report-then-confirm convention exists to forbid.
        """
        project = get_project(project_id)
        asset_id = request.asset_id.strip()
        if not asset_id:
            project.default_setting_id = ""
            return store.save(project)
        asset = next((item for item in project.assets if item.id == asset_id), None)
        if asset is None:
            raise HTTPException(status_code=404, detail="Asset not found")
        if asset.kind != "setting":
            raise HTTPException(
                status_code=422,
                detail=DEFAULT_SETTING_NOT_A_SETTING.format(
                    name=asset.name, kind=asset.kind
                ),
            )
        project.default_setting_id = asset_id
        return store.save(project)

    @app.delete("/api/projects/{project_id}/assets/{asset_id}", response_model=Project)
    def delete_asset(project_id: str, asset_id: str) -> Project:
        """Remove one asset from the library — refused by name while any shot cites it.

        Two dialogs promised this ("keep, delete, or AI Mod"; "delete it to reject") and
        no route existed (the analyst's finding, 2026-08-20). The citation refusal names
        the shots because a dangling citation is the render-time 422 this route would
        otherwise be manufacturing. An uploaded asset's file goes with it; a generated
        asset's file stays in ComfyUI's output tree, same rule as project deletion.
        """
        project = get_project(project_id)
        asset = next((item for item in project.assets if item.id == asset_id), None)
        if asset is None:
            raise HTTPException(status_code=404, detail="Asset not found")
        citing = [
            shot_label(project, shot)
            for shot in ordered_shots(project)
            if any(citation.asset_id == asset_id for citation in shot.citations)
        ]
        if citing:
            raise HTTPException(
                status_code=422,
                detail=DELETE_ASSET_CITED.format(name=asset.name, shots=", ".join(citing)),
            )
        if asset.source == "upload" and asset.path:
            target = store.project_dir(project_id) / asset.path
            if target.is_file():
                target.unlink()
        project.assets = [item for item in project.assets if item.id != asset_id]
        # A location that is no longer in the library is not this project's location. Cleared
        # here rather than left to `default_setting_asset`'s re-validation — which would also
        # no-op it — so the manifest never carries a pointer to something that is gone, and a
        # later asset re-using the id could not silently inherit the declaration.
        if project.default_setting_id == asset_id:
            project.default_setting_id = ""
        return store.save(project)

    @app.post(
        "/api/projects/{project_id}/assets/{asset_id}/replace-citations",
        response_model=AssetReplacementResponse,
    )
    def replace_asset_citations(
        project_id: str, asset_id: str, request: AssetReplacementRequest
    ) -> AssetReplacementResponse:
        """Re-point every shot citing this asset at another one. The way through `delete_asset`.

        The Director hit `DELETE_ASSET_CITED` trying to remove the HarderFaster source now that
        its Krea multiview exists, liked that it was caught, and asked for the act the refusal
        implies: "a nice Replace With/Cancel option set ... so then i could select another image
        while i am here in assets and auto replace the one i am trying to remove across the
        affected shots" (2026-08-20). **The delete refusal is untouched**, and this route does
        not delete: it moves citations, and the Director deletes afterwards — so an asset one
        locked shot still cites meets the same refusal for the same reason, rather than
        half-vanishing from a library it is still referenced in.

        Report first, apply on confirm, enforced here rather than trusted to the browser —
        `snap_timeline_cuts`' shape, which is `populate`'s `confirm_replace` in a smaller key.
        Without `confirm_apply` this route **does not call `store.save`** and the response
        carries no project at all, so "nothing was written" is visible on the wire.

        Every decision is `asset_replacement.asset_replacement_plan`'s. This route's own additions
        are the two lookups, the three refusals, the protection map, the kind warning, the
        sentences, and the write.

        **A rendered shot is replaced, and told about — `shot_write_refusal` is deliberately not
        the gate here.** This route shipped using it and the Director overruled that the same day:
        *"So even with takes we do want the asset for the shot replaceable, that way a re-render
        would use the updated asset without losing previous takes. This helps facilitate
        experimentation."* The reasoning is the general rule for citations and is worth keeping in
        one place: **replacing a citation does not touch the take.** The file is still on disk,
        `latest_output` still names it, the takes strip still lists it, `RenderJob.output_files` is
        unchanged, and a citation describes what a *future* render would use. `shot_write_refusal`
        is right about prose — an in-place prompt rewrite really does destroy the record — and its
        `rendered` arm does not transfer to a field that is not the prompt. `timeline.
        window_move_refusal` already reasons this way for windows, for the same reason.

        What survives is the report. `REPLACE_ASSET_RENDERED_NOTE` and
        `REPLACE_ASSET_APPROVED_NOTE` name those shots before the confirm, because the consequence
        is real and unrecoverable: nothing in this application records which assets produced a
        take, so afterwards the take and the references beside it simply disagree with no way back.

        **Approved shots are the same case, and their approval is untouched.** No path here writes
        `approved_output`, `approved_start` or `approved_duration`. AD-13's staleness comparison is
        between the stored window and the live one, and citations are not the window, so assembly
        reads exactly what it read before. They get their own report line rather than their own
        rule, because an approval is a stronger statement than a render and the count should be
        visible.

        **Two protections remain.** A `locked` shot is an explicit hands-off the Director set and
        only they may clear it. An **in-flight** render is the one genuine correctness block: the
        job was submitted against the old asset and is executing now, so rewriting the citation
        underneath it would leave that job's record describing a render that never happened. Read
        through `shot_render_in_flight`, the single reader of the job records, which also catches
        a shot whose status was walked backwards by hand.

        Nothing renders, arms, queues or approves. `comfy` is not touched on any path, no
        `status` moves, and the only fields any shot differs in afterwards are `citations`,
        `reference_labels` and the `asset_ids` projection the model rebuilds from the first.
        """
        project = get_project(project_id)
        replaced = next((item for item in project.assets if item.id == asset_id), None)
        if replaced is None:
            raise HTTPException(status_code=404, detail="Asset not found")
        replacement = next(
            (item for item in project.assets if item.id == request.replacement_id), None
        )
        if replacement is None:
            raise HTTPException(status_code=404, detail=REPLACE_ASSET_UNKNOWN)
        # Before the plan, because a self-replacement is not an empty plan: every citation would
        # match, every shot would be reported as changed, and the manifest would be rewritten to
        # exactly what it already said. A report claiming thirty changes and a save that changes
        # nothing is worse than a refusal.
        if replacement.id == replaced.id:
            raise HTTPException(
                status_code=422,
                detail=REPLACE_ASSET_WITH_ITSELF.format(name=replaced.name),
            )
        # Two protections and no more. A lock is the Director's own hands-off and only they clear
        # it; a render executing right now is the one genuine correctness block. `shot_write_refusal`
        # is deliberately NOT the gate here — see the ruling in the docstring — so the lock is read
        # from the Shot directly rather than through a function whose `rendered` arm this route no
        # longer honours. In-flight is `shot_render_in_flight`, the one reader of the job records,
        # rather than a second walk over them.
        protected: dict[str, str] = {}
        for shot in project.shots:
            if shot.locked:
                protected[shot.id] = EXPANSION_LOCKED_NOTICE.format(
                    shots=shot_label(project, shot)
                )
            elif shot_render_in_flight(project, shot):
                protected[shot.id] = REPLACE_ASSET_IN_FLIGHT.format(
                    shot=shot_label(project, shot), replaced=replaced.name
                )
        # Not a protection: a note. Approved outranks rendered because an approval is the stronger
        # statement about the same take, and a shot reported under both headings would be counted
        # twice. `shot_render_provenance` is the same predicate `shot_write_refusal`'s second arm
        # reads — the fact is unchanged, only what this route does about it.
        provenance = {
            shot.id: (
                "approved"
                if shot.approved_output or shot.status == "approved"
                else "rendered"
            )
            for shot in project.shots
            if shot_render_provenance(shot)
        }
        plan = asset_replacement_plan(
            project,
            replaced=replaced,
            replacement=replacement,
            protected=protected,
            provenance=provenance,
            limits=H3_REFERENCE_LIMITS,
        )
        # The honest-empty refusal, `snap_timeline_cuts`' rule: nothing cites this asset, so there
        # is no plan to report over. Checked on `cited` rather than on the writable buckets, so an
        # asset every one of whose citing shots is locked still *reports* — those skips are the
        # answer to "why can I still not delete it", and refusing them into a 422 would hide it.
        if not plan.cited:
            raise HTTPException(
                status_code=422,
                detail=REPLACE_ASSET_UNCITED.format(name=replaced.name),
            )
        still_cited = len(plan.skips)
        # The two provenance lines, each drawn only when it has shots. Grouped rather than one row
        # per shot, `expansion_sweep_notices`' rule: listing twenty shots through a `{shot}`-shaped
        # sentence twenty times is not a report anyone reads.
        rendered = plan.with_provenance("rendered")
        approved = plan.with_provenance("approved")
        notes = [
            wording.format(
                count=len(changes),
                replaced=replaced.name,
                shots=", ".join(change.label for change in changes),
            )
            for wording, changes in (
                (REPLACE_ASSET_APPROVED_NOTE, approved),
                (REPLACE_ASSET_RENDERED_NOTE, rendered),
            )
            if changes
        ]
        response = AssetReplacementResponse(
            applied=False,
            replaced=replaced.name,
            replacement=replacement.name,
            swapped=len(plan.swaps),
            merged=len(plan.merges),
            skipped=len(plan.skips),
            still_cited=still_cited,
            rendered=len(rendered),
            approved=len(approved),
            notes=notes,
            swaps=[_replacement_row(change) for change in plan.swaps],
            merges=[_replacement_row(change) for change in plan.merges],
            skips=[
                AssetReplacementSkip(
                    shot_id=skip.shot_id, label=skip.label, reason=skip.reason
                )
                for skip in plan.skips
            ],
            warning=(
                ""
                if replacement.kind == replaced.kind
                else REPLACE_ASSET_KIND_CHANGE.format(
                    replacement=replacement.name,
                    replacement_kind=replacement.kind,
                    replaced=replaced.name,
                    replaced_kind=replaced.kind,
                )
            ),
            message=" ".join(
                (
                    REPLACE_ASSET_REPORT.format(
                        replacement=replacement.name,
                        replaced=replaced.name,
                        swapped=len(plan.swaps),
                        merged=len(plan.merges),
                        skipped=len(plan.skips),
                    ),
                    REPLACE_ASSET_FREED.format(replaced=replaced.name)
                    if not still_cited
                    else REPLACE_ASSET_STILL_CITED.format(
                        count=still_cited, replaced=replaced.name
                    ),
                )
            ),
        )
        if not request.confirm_apply or not plan.writes:
            return response
        # Committed by position from the plan's own candidates, `assistant_fill`'s one pass after
        # every shot has been judged: nothing above this line touched the project, so a plan that
        # raised part-way through would have left both the manifest and the in-memory project
        # exactly as they were.
        for index, shot in enumerate(project.shots):
            if (candidate := plan.candidates.get(shot.id)) is not None:
                project.shots[index] = candidate
        response.project = store.save(project)
        response.applied = True
        return response

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
        # The record first, then the graph (the Director's 2026-08-21 ruling). A save that
        # loses a race refuses here, before a single byte reaches ComfyUI, so the refusal
        # costs no GPU time — where a save refused *after* the submit answered 409 for a
        # prompt already on the card and lost the only record of it.
        job = RenderJob(
            kind="music",
            prompt_id=PENDING_SUBMISSION_PROMPT_ID,
            target_id="song",
            seed=request.seed,
        )
        project.jobs.append(job)
        store.save(project)
        try:
            submission = await comfy.submit(payload)
        except ComfyError as error:
            settle_unsubmitted_jobs(project, job)
            raise HTTPException(status_code=502, detail=str(error)) from error
        accept_submission(job, submission.prompt_id)
        # **The Song is replaced only once the graph is accepted**, and that is the one thing
        # this route deliberately does *not* move ahead of the submission. Replacing it is
        # destructive — it is why `_require_song_replacement_confirmation` exists — and doing
        # it for a graph ComfyUI then refused would trade a lost job record for a lost song,
        # which is the expensive direction the ruling exists to avoid.
        project.song = Song(
            title=request.title,
            source="generated",
            duration=request.duration,
            lyrics=request.lyrics,
            caption=request.caption,
            prompt_id=submission.prompt_id,
        )
        # **Deliberately NOT superseded**, and the music routes are the one place a leftover
        # record is left standing on purpose. Every music job shares `target_id="song"` and
        # this route has no per-target in-flight refusal, so two live records here is the
        # easiest state in the application to reach — but the older one cannot do the harm
        # supersession exists to prevent: `apply_job_history` gates song adoption on
        # `Song.prompt_id`, which the assignment above has just replaced, so a late answer to
        # it can never be pasted onto the Song that is now the project's.
        #
        # What it *can* still do is record where its audio landed. Settling it would stop it
        # being reconciled at all, and its `output_files` — the one place an orphaned take is
        # recoverable from, which
        # `test_a_completing_music_job_matches_the_song_by_prompt_id_not_by_source` pins —
        # would stay empty forever. That is a real loss traded for cleanup the three-tick
        # settle already performs. See `batch.supersede_target_jobs`.
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
        # Before `comfy.submit` for the same reason the confirmation gate is: a duration and
        # headroom whose product leaves `MiniMaxMusic3TextEncode`'s 0.04–360 s schema range
        # would be rejected at `/prompt` validation and reach the Director as an opaque 502.
        # Refused here instead, naming both numbers and the ceiling — never silently clamped,
        # because a quietly shortened ceiling is the very truncation this setting exists to
        # prevent.
        try:
            if request.lyrics is not None:
                payload = build_songplanner_known_lyrics_payload(
                    idea=request.idea,
                    genre_hint=request.genre_hint,
                    lyrics=request.lyrics,
                    duration=request.duration,
                    duration_headroom=request.duration_headroom,
                    seed=request.seed,
                    prefix=prefix,
                )
            else:
                payload = build_songplanner_invented_payload(
                    idea=request.idea,
                    genre_hint=request.genre_hint,
                    duration=request.duration,
                    duration_headroom=request.duration_headroom,
                    seed=request.seed,
                    prefix=prefix,
                )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        # The record first, then the graph, for `generate_music`'s reason and by the same
        # rule: a save race refuses before any GPU time is spent.
        job = RenderJob(
            kind="music",
            prompt_id=PENDING_SUBMISSION_PROMPT_ID,
            target_id="song",
            seed=request.seed,
        )
        project.jobs.append(job)
        store.save(project)
        try:
            submission = await comfy.submit(payload)
        except ComfyError as error:
            settle_unsubmitted_jobs(project, job)
            raise HTTPException(status_code=502, detail=str(error)) from error
        accept_submission(job, submission.prompt_id)
        # And the Song is replaced only once the graph is accepted, for `generate_music`'s
        # reason: the replacement is the destructive act the confirmation gate guards.
        project.song = Song(
            title=request.title,
            source="generated",
            duration=request.duration,
            lyrics=request.lyrics or "",
            caption=request.idea,
            prompt_id=submission.prompt_id,
        )
        # Not superseded either, for `generate_music`'s reason and by the same argument: a
        # song planned here and a song generated there are both `kind="music"` on
        # `target_id="song"`, and neither may lose its record of where its audio landed.
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
        # The record first, then the graph (the Director's 2026-08-21 ruling): a save that
        # loses a race refuses before any GPU time is spent. The Asset itself is appended only
        # once the graph is accepted — an asset with no path and no prompt id renders as an
        # empty library row, and a submission that failed must leave nothing behind for the
        # Director to delete by hand.
        job = RenderJob(
            kind="flux",
            prompt_id=PENDING_SUBMISSION_PROMPT_ID,
            target_id=asset.id,
            seed=request.seed,
        )
        project.jobs.append(job)
        store.save(project)
        try:
            submission = await comfy.submit(payload)
        except ComfyError as error:
            settle_unsubmitted_jobs(project, job)
            raise HTTPException(status_code=502, detail=str(error)) from error
        accept_submission(job, submission.prompt_id)
        asset.prompt_id = submission.prompt_id
        project.assets.append(asset)
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
                # **The sheet inherits its source's appearance anchor.** A multiview
                # promotion is the one child relationship in this application that promises
                # the child depicts *the same subject unchanged* — that is what a turnaround
                # sheet is, and `kind` is already inherited on that reasoning. The sheet is
                # then the asset shots actually cite, so an anchor that stopped at the parent
                # would be an anchor no render ever sees. It is a copy and not a link: the
                # Director may correct one without the other, and a link would make editing
                # a source silently rewrite what every shot citing the sheet is conditioned
                # with.
                #
                # Contrast `edit_asset` below, which deliberately does not inherit.
                consistency_prompt=source.consistency_prompt,
            )
            payload = build_multiview_payload(
                image_name=image_name,
                prompt=request.prompt,
                seed=request.seed,
                prefix=f"music-video-producer/{project_id}/assets/{child.id}-multiview",
            )
        except ComfyError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        # The record first, then the graph, for `generate_flux`'s reason. The upload above
        # stays outside it: it puts a file in ComfyUI's input directory and costs no GPU
        # time, and its own failure is the same 502 it always was, with nothing recorded.
        job = RenderJob(
            kind="multiview",
            prompt_id=PENDING_SUBMISSION_PROMPT_ID,
            target_id=child.id,
            seed=request.seed,
        )
        project.jobs.append(job)
        store.save(project)
        try:
            submission = await comfy.submit(payload)
        except ComfyError as error:
            settle_unsubmitted_jobs(project, job)
            raise HTTPException(status_code=502, detail=str(error)) from error
        accept_submission(job, submission.prompt_id)
        child.prompt_id = submission.prompt_id
        project.assets.append(child)
        store.save(project)
        return job

    @app.post(
        "/api/projects/{project_id}/assets/{asset_id}/edit",
        response_model=RenderJob,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def edit_asset(
        project_id: str, asset_id: str, request: AssetEditRequest
    ) -> RenderJob:
        """AI Mod: one image asset plus one instruction becomes a *new* asset beside it.

        The Director's stage-3 ask, verbatim shape: prompt an edit, get a new image asset
        to keep, delete (rejection is ordinary deletion), or modify further — a child of
        an edit is an ordinary image asset, so edits chain. The source is never touched.

        The instruction travels in the workflow's own prompting form via
        `image_edit_prompt` — identity preserved, the edit stated, everything else kept —
        unless it already carries the structured marker, in which case the Director wrote
        the full form and it goes verbatim. The media reaches ComfyUI the reference
        path's way: a resolved absolute file path through the H3 media loader, no upload.

        `generate_multiview` is the template for everything else here: the child is
        created before submission with an empty path, the job (kind `edit`) targets it,
        and `apply_job_history` — the one completion writer — adopts the landed file.
        """
        project = get_project(project_id)
        source = next((item for item in project.assets if item.id == asset_id), None)
        if not source:
            raise HTTPException(status_code=404, detail="Asset not found")
        if source.kind in ("audio", "video"):
            raise HTTPException(
                status_code=422,
                detail=f"AI Mod edits images, and {source.name} is {source.kind} media.",
            )
        if not source.path:
            raise HTTPException(
                status_code=422,
                detail=f"{source.name} has no image yet — render or upload it first.",
            )
        if not request.instruction.strip():
            raise HTTPException(
                status_code=422,
                detail="Describe the edit: what should change, and what must stay.",
            )
        source_path = resolve_asset_path(project_id, source)
        prompt = image_edit_prompt(
            request.instruction,
            source_kind=source.kind,
            source_label=source.name,
        )
        child = Asset(
            name=f"{source.name} · edit",
            # An edited character is still a character; an edited setting is still a
            # setting. The multiview promotion's rule, for the multiview promotion's
            # reason.
            kind=source.kind,
            path="",
            source="h3-image-edit",
            parent_id=source.id,
            prompt=prompt,
            # **An edit does NOT inherit the source's appearance anchor**, and that is the
            # opposite decision to `generate_multiview` above on purpose. An anchor is an
            # assertion about what a subject looks like; an AI Mod is the act of changing
            # what it looks like ("put her in the black coat instead"). Copying the anchor
            # onto the child would carry a description the edit was run to invalidate, and
            # would carry it *silently* into every tag line and expansion citing the new
            # asset — the exact "plausible and wrong" failure this codebase keeps refusing.
            #
            # So the child starts with no anchor, which means "no anchor stored" and produces
            # the bare label everywhere, and the Director writes one for the edited look if
            # they want one. Nothing is lost: the source keeps its own, and this route never
            # touches the source.
        )
        try:
            payload = build_h3_image_edit_payload(
                prompt=prompt,
                pictures=[{"file": str(source_path), "label": source.name}],
                seed=request.seed,
                profile=request.profile,
                prefix=f"music-video-producer/{project_id}/assets/{child.id}-edit",
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        # The record first, then the graph, for `generate_flux`'s reason, and the child asset
        # appended only once the graph is accepted for the same one.
        job = RenderJob(
            kind="edit",
            prompt_id=PENDING_SUBMISSION_PROMPT_ID,
            target_id=child.id,
            seed=request.seed,
        )
        project.jobs.append(job)
        store.save(project)
        try:
            submission = await comfy.submit(payload)
        except ComfyError as error:
            settle_unsubmitted_jobs(project, job)
            raise HTTPException(status_code=502, detail=str(error)) from error
        accept_submission(job, submission.prompt_id)
        child.prompt_id = submission.prompt_id
        project.assets.append(child)
        store.save(project)
        return job

    @app.post(
        "/api/projects/{project_id}/assets/fill",
        response_model=AssetFillResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def fill_assets(project_id: str, request: AssetFillRequest) -> AssetFillResponse:
        """The Stage Manager (stage 3 of the Director's user workflow): assess and create.

        One model pass over the whole project proposes the supporting image assets the
        library still lacks; each proposal queues an ordinary Flux render through the
        exact asset shape `generate_flux` creates, so a landed proposal is
        indistinguishable from a hand-generated asset — keep it, delete it to reject,
        AI Mod it onward. The count is guidance to the model and a hard truncation here.

        Refused while renders are open, deliberately and with FR-9's number: Flux
        interleaved into an H3 batch evicts the resident stack at ~150 s per eviction.
        The GPU acknowledgement is server-enforced like every expensive path's.
        """
        project = get_project(project_id)
        if reconcilable_jobs(project):
            raise HTTPException(status_code=409, detail=ASSET_FILL_RENDERS_OPEN_REFUSAL)
        if not request.confirm_gpu:
            raise HTTPException(
                status_code=422,
                detail=ASSET_FILL_CONFIRM_REFUSAL.format(count=request.count),
            )
        context = project.model_dump(mode="json", exclude=DIRECTOR_CONTEXT_EXCLUDE)
        try:
            result = await director.stage_manager(
                project_context=context, count=request.count
            )
        except DirectorUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except DirectorError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        proposals = [item for item in result.assets if item.prompt.strip()][: request.count]
        if not proposals:
            raise HTTPException(
                status_code=502,
                detail=ASSET_FILL_NO_PROPOSALS_REFUSAL.format(
                    message=(result.message or "").strip()[:300] or "(empty)"
                ),
            )
        # Re-read after the await, and re-check the eviction guard: an H3 batch submitted
        # while the model thought must not get Flux interleaved into it.
        project = get_project(project_id)
        if reconcilable_jobs(project):
            raise HTTPException(status_code=409, detail=ASSET_FILL_RENDERS_OPEN_REFUSAL)
        submitted: list[AssetFillSubmission] = []
        # Every record first, then every graph (the Director's 2026-08-21 ruling). One save
        # covers the whole batch rather than one per proposal: the property the ruling is
        # about is that a save race is answered *before* any GPU time is spent, and a batch
        # whose records could not be written spends none at all.
        pending: list[tuple[Asset, dict[str, Any], RenderJob]] = []
        for index, proposal in enumerate(proposals):
            asset = Asset(
                name=proposal.name,
                kind=proposal.kind,
                path="",
                source="stage-manager",
                prompt=proposal.prompt,
            )
            payload = build_flux_payload(
                prompt=proposal.prompt,
                width=1024,
                height=1024,
                steps=20,
                guidance=4.0,
                # Distinct seeds so two similar proposals cannot land the identical image.
                seed=index,
                prefix=f"music-video-producer/{project_id}/assets/{asset.id}",
            )
            job = RenderJob(
                kind="flux",
                prompt_id=PENDING_SUBMISSION_PROMPT_ID,
                target_id=asset.id,
                seed=index,
            )
            project.jobs.append(job)
            pending.append((asset, payload, job))
        store.save(project)
        for index, (asset, payload, job) in enumerate(pending):
            try:
                submission = await comfy.submit(payload)
            except ComfyError as error:
                # Partial batches are reported honestly: what queued is queued, and the
                # failure names itself; nothing already submitted is rolled back. The records
                # for the graphs that never went out — this one and every one after it — are
                # settled rather than left open, which is also what writes the accepted half
                # of the batch to disk.
                settle_unsubmitted_jobs(
                    project, *(entry[2] for entry in pending[index:])
                )
                raise HTTPException(status_code=502, detail=str(error)) from error
            accept_submission(job, submission.prompt_id)
            asset.prompt_id = submission.prompt_id
            project.assets.append(asset)
            submitted.append(
                AssetFillSubmission(
                    asset_id=asset.id, name=asset.name, kind=asset.kind, job_id=job.id
                )
            )
        store.save(project)
        return AssetFillResponse(message=result.message, submitted=submitted)

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
        # The sync-correct offset of the take the submission below will produce — nonzero
        # only when the reference branch extends a song-audio window ahead of the shot.
        # Written onto the Shot with `prompt_id` at submission; see `Shot.latest_take_lead`.
        take_lead = 0.0
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
                references.append(
                    {
                        "kind": numbered.kind,
                        "file": str(resolve_asset_path(project_id, asset)),
                        "label": label,
                    }
                )
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
            try:
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
                    profile=request.profile,
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
            if request.profile != H3_DEFAULT_PROFILE:
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
        )
        project.jobs.append(job)
        store.save(project)
        try:
            submission = await comfy.submit(payload)
        except ComfyError as error:
            settle_unsubmitted_jobs(project, job)
            raise HTTPException(status_code=502, detail=str(error)) from error
        accept_submission(job, submission.prompt_id)
        shot.status = "queued"
        shot.prompt_id = submission.prompt_id
        # The take this job produces begins `take_lead` seconds before the shot's window
        # (0 for every non-song path). Recorded at the moment of truth because it cannot
        # be derived later; the Monitor, the nudge control and assembly all cut from it.
        shot.latest_take_lead = take_lead
        # And the window it begins that far before, snapshotted with it (2026-08-21). The
        # lead alone does not describe a take: `start` and `duration` are edited afterwards
        # — dragging a rendered clip's left edge moves `start` while `trim_nudge`
        # compensates — and every number `restore_song_audio` reported was read off the
        # live window as though it were the take's. Two fields written where one already
        # was, in the same statement, so a take can never carry half a description. See
        # `Shot.latest_take_start`.
        shot.latest_take_start = shot.start
        shot.latest_take_duration = shot.duration
        # Job-record hygiene, after the accept and not a gate. Every refusal above stands —
        # in particular the `status != "ready"` one, which is what normally makes a second
        # render for a live shot impossible. It stopped being enough when a whole-manifest write
        # walked the status back underneath a live job; both generic writes now refuse that
        # (`_require_in_flight_status_kept`, 2026-08-20), so no shipped route produces the state
        # any more. It is still reachable by a manifest edited on disk, restored from a backup,
        # or saved by a build older than the gate, and in that state the older record is not
        # merely untidy: `apply_job_history` adopts by `target_id`, so a
        # late answer to it would move `latest_output` back onto the older take and drop the
        # newer one's review with it. See `batch.supersede_target_jobs`.
        supersede_target_jobs(
            project, kinds={"h3"}, target_id=shot.id, keep_job_id=job.id
        )
        store.save(project)
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
                detail=GENERATE_BATCH_EMPTY_FLAGGED
                if request.scope == "flagged"
                else GENERATE_BATCH_EMPTY_READY,
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
                # A settled shot re-opens through the same route a lone click uses; its
                # refusals (in-flight, locked, approved, the prompt gate re-asked) are
                # the batch's refusals, in the same words.
                if target.status in ("complete", "error"):
                    render_again(project_id, target.id)
                    # A re-render at the same seed and prompt reproduces the identical
                    # take — the fixed-seed trap the roadmap already recorded for Flux,
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
        # The meaning-refusal, ahead of the mechanical ones, on mark-ready's precedent: whether
        # this Shot may be enhanced at all comes before whether its inputs exist. A singing Shot
        # with no take should hear that it is a singing shot — telling it to render first would
        # send the Director to spend GPU on a take this route would then refuse anyway.
        if shot.singing == "singing":
            raise HTTPException(
                status_code=422,
                detail=ENHANCE_SINGING_REFUSAL.format(shot=shot_label(project, shot)),
            )
        if shot.singing == "unknown":
            raise HTTPException(
                status_code=422,
                detail=ENHANCE_SINGING_UNKNOWN_REFUSAL.format(shot=shot_label(project, shot)),
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
        # The whole write, and it happens **before** the graph goes out — the Director's
        # 2026-08-21 ruling, for `generate_h3`'s reason: a save race then refuses before any
        # GPU time is spent, where a save refused after the submit answered 409 for a prompt
        # already accepted and lost the only record of the enhancement. The Shot itself is
        # untouched either way: see this route's docstring.
        job = RenderJob(
            kind="ltx",
            prompt_id=PENDING_SUBMISSION_PROMPT_ID,
            target_id=shot.id,
            # The seed the graph fixes, recorded so the job says what was sampled rather than
            # defaulting to a 0 that happens to match.
            seed=LTX25_ENHANCE_SEED,
        )
        project.jobs.append(job)
        store.save(project)
        try:
            submission = await comfy.submit(payload)
        except ComfyError as error:
            settle_unsubmitted_jobs(project, job)
            raise HTTPException(status_code=502, detail=str(error)) from error
        accept_submission(job, submission.prompt_id)
        store.save(project)
        return job

    @app.post(
        "/api/projects/{project_id}/shots/{shot_id}/restore-song-audio",
        response_model=AudioRestoreResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def restore_song_audio(project_id: str, shot_id: str) -> AudioRestoreResponse:
        """Put the master song's own seconds back over one Shot's rendered take. No body.

        The gap this closes, measured on 2026-08-18: **H3 generates its output audio.**
        `VHS_VideoCombine.audio` is fed by a `VAEDecodeAudio` on the sampler's own latent — in
        `build_h3_reference_payload` and in both canonical exports alike — so a song attached
        with `use_song_audio` conditions the lip movement as `ref_audios` and is deliberately
        never the soundtrack. A rendered take correlates with the master at about 0.01 at every
        lag within a second and is 3.4x louder. That is correct, nothing on this path changes
        it, and this route is the stage that was missing: the one that puts the real track back
        over the finished picture.

        **The window is not computed here.** This route hands `build_audio_replace_payload` four
        numbers off the Shot — a window start, a window duration, `song_duration` and the
        recorded `latest_take_lead` — and that builder puts them through the same two functions
        `generate_h3` puts them through. There is no window parameter anywhere on this path for
        the two stages to disagree through, and the failure a second computation would produce —
        a subtle desync rather than an error — has nowhere to come from.

        **The window is the take's, not the shot's** (fixed 2026-08-21). Since the over-render
        margin a take is longer than its window and, below about 3.271 s, centred on it: a
        2.083 s window is 4.4583 s of picture whose first frame is song second `start - 1.2083`.
        This route windowed by the bare `start`/`duration` until that date, which laid the
        exposed slice's seconds over the whole take — the sound running `lead` ahead of the mouth
        and stopping a margin early. It now sends `over_render_frames(duration)` frames of song
        from `start - latest_take_lead`, which is `over_render_window`: the same call, with the
        same lead, that conditioned the render. A shot at 12 s with a 0.25 s lead is restored
        from 11.75 s, and frame 6 of that take is song second 12.000 exactly.

        **And the take's window is the one recorded on the take** (2026-08-21, second pass). The
        paragraph above was the whole fix at first, and it still read the window off the *live*
        `start`/`duration`: correct only until somebody edited them. A take is fixed the moment
        it is submitted; a window is not. Drag a rendered clip's left edge and `start` moves by
        `delta` while `trim_nudge` compensates — the take goes on beginning where it always did —
        so the master was laid `delta` seconds off the lip-sync it was performed against, by a
        frame count the render had not asked for, and both the docstring and
        `AudioRestoreResponse.requested_picture_seconds` called the result "the same count the
        submission sent". `generate_h3` now snapshots the window beside the lead
        (`Shot.latest_take_start`), this route computes from the snapshot, and the claim is true
        by construction rather than by nobody having dragged anything.

        For a take with no snapshot — every take rendered before that date, and every clip
        chosen by hand — the live window is all there is, and the route uses it and **says so**:
        `describes_take` is false and `RESTORE_AUDIO_UNDESCRIBED_TAKE` opens the note. Not
        refused, and the choice is deliberate: nothing distinguishes an unmoved legacy window
        from a moved one, so a refusal would disable this stage for every take that exists today
        over a staleness there is no evidence of, and the harm it would avert is a length
        reported wrongly rather than a sync lost silently — the offset such a take is placed at
        is the one `RESTORE_AUDIO_NO_LEAD_REFUSAL` already refuses to guess.

        The refusal for a window past the end of the song is `song_audio_window`'s, raised inside
        the builder and translated here, so this stage refuses exactly the shots the render
        refuses and in the same words. It is not a second rule. The one refusal this route owns
        beyond the render's is the take with no recorded lead — see
        `RESTORE_AUDIO_NO_LEAD_REFUSAL`, which is a refusal precisely because the alternative is
        a guess about a take's provenance.

        **Nothing here writes to the Shot.** Not `status`, not `latest_output`, not
        `latest_review`, not `prompt_id`. Only a `RenderJob` of `kind="post"` is appended, and
        `read_job` has no branch for that kind, so a *completed* restoration moves no pointer
        either. Three consequences, and the third is the point of the other two:

        * the restored video is written under `RESTORE_AUDIO_PREFIX_SUFFIX`, so ComfyUI numbers
          it in its own series and it lands *beside* the take;
        * the take is opened read-only by `VHS_LoadVideoPath` and is byte-identical afterwards.
          **Its generated audio stays recoverable**, which is not tidiness: hearing "voices but
          no phonetics" in a take is what let the Director find a real conditioning bug on
          2026-08-18, and a pipeline that discards H3's own output discards its best
          diagnostic;
        * deciding which of the two files is *the* take is take comparison, and stitching many
          takes to a master is assembly (FR-22). Both remain unbuilt and neither is presumed
          here.

        This is deliberately a separate act and not something a render does. Applying it
        automatically at render time is marked Ask First in the spec and has not been asked —
        it would remove the ability to hear what H3 actually produced.

        No body, for the enhancer's reason: this route has no controls at all. The window comes
        from the shot, the paths come from the manifest, and the sampling does not exist because
        nothing here samples.

        **No GPU time is spent on any refusal**: every branch below sits ahead of the
        submission. There is very little to spend either way — this payload names zero model
        files and loads no network at all. See `build_audio_replace_payload`.
        """
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        # First, for `enhance_with_ltx25`'s reason: an in-flight Shot is the one state where
        # getting this wrong does concrete harm, and 409 rather than 422 because a live job is a
        # state conflict — the same request succeeds once it lands.
        if (
            shot_render_in_flight(project, shot)
            or shot_enhancement_in_flight(project, shot)
            or shot_audio_restore_in_flight(project, shot)
        ):
            raise HTTPException(
                status_code=409,
                detail=RESTORE_AUDIO_IN_FLIGHT_REFUSAL.format(shot=shot_label(project, shot)),
            )
        # Then the take, because a take is what this route's subject *is*: the picture the song
        # goes over. A Shot that never rendered has no take to name, and that is a different
        # sentence from a take whose file is gone.
        if not shot.latest_output:
            raise HTTPException(
                status_code=422,
                detail=RESTORE_AUDIO_NO_TAKE_REFUSAL.format(shot=shot_label(project, shot)),
            )
        # `enhance_with_ltx25`'s resolution, containment check included, so a `latest_output`
        # carrying `..` cannot reach outside ComfyUI's output directory and hand an arbitrary
        # file to the node.
        output_root = (settings.comfy_root / "output").resolve()
        source = (output_root / Path(shot.latest_output)).resolve()
        if output_root not in source.parents or not source.is_file():
            raise HTTPException(
                status_code=422,
                detail=RESTORE_AUDIO_MISSING_TAKE_REFUSAL.format(
                    shot=shot_label(project, shot), path=shot.latest_output
                ),
            )
        # Then whether this shot has a window at all. Before the song is resolved, because a
        # shot that never rode the master is refused for that whether or not a song exists —
        # telling such a Director to add a song would send them to fix the wrong thing.
        if not shot.use_song_audio:
            raise HTTPException(
                status_code=422,
                detail=RESTORE_AUDIO_NOT_SONG_AUDIO_REFUSAL.format(
                    shot=shot_label(project, shot)
                ),
            )
        if not project.song or not project.song.path:
            raise HTTPException(
                status_code=422,
                detail=RESTORE_AUDIO_NO_SONG_REFUSAL.format(shot=shot_label(project, shot)),
            )
        try:
            song = resolve_song_path(project_id, project.song)
        except HTTPException as error:
            # `resolve_song_path` answers 404 for "the media is not there", which is right for
            # a media route and wrong here: the request names a project and a Shot that both
            # exist, and what cannot be processed is the state the manifest describes. Re-raised
            # as the matrix's 422, naming the recorded path so a moved file is distinguishable
            # from a cleared directory.
            raise HTTPException(
                status_code=422,
                detail=RESTORE_AUDIO_MISSING_SONG_REFUSAL.format(
                    shot=shot_label(project, shot), path=project.song.path
                ),
            ) from error
        # The take's own window, before anything is computed from a window at all. A
        # `latest_take_duration` of 0 is "never snapshotted" — the model constrains a real
        # `duration` to `gt=0` — and it is what every take rendered before 2026-08-21 reads, and
        # every clip `select_shot_clip` cleared the bookkeeping for. Described takes compute from
        # the take; undescribed ones fall back to the live window and the response says which of
        # the two it was. See `Shot.latest_take_start` and `RESTORE_AUDIO_UNDESCRIBED_TAKE`.
        describes_take = shot.latest_take_duration > 0
        take_start = shot.latest_take_start if describes_take else shot.start
        take_duration = shot.latest_take_duration if describes_take else shot.duration
        # Only askable of a described take: an undescribed one has nothing to compare the live
        # window against, which is exactly why it cannot be claimed to describe anything.
        window_moved = describes_take and (
            abs(take_start - shot.start) > RESTORE_AUDIO_WINDOW_TOLERANCE
            or abs(take_duration - shot.duration) > RESTORE_AUDIO_WINDOW_TOLERANCE
        )
        # The take's own bookkeeping, last of the refusals and still before any submission. See
        # `RESTORE_AUDIO_NO_LEAD_REFUSAL`: a take that begins past 0 s and records no lead is a
        # take this route cannot place, and placing it anyway is the guess the whole stage
        # refuses to make elsewhere. Asked of the take's window rather than the shot's, so a
        # rendered shot dragged to 0 s is still refused for the take it actually holds.
        if take_start > 0 and not shot.latest_take_lead:
            raise HTTPException(
                status_code=422,
                detail=RESTORE_AUDIO_NO_LEAD_REFUSAL.format(
                    shot=shot_label(project, shot), start=take_start
                ),
            )
        try:
            payload = build_audio_replace_payload(
                # Forward slashes on Windows too, for `enhance_with_ltx25`'s reason: the value
                # is a plain string to VHS, and a backslash path survives the JSON round-trip
                # doubled and unreadable in every log and error message on the way.
                source_video=source.as_posix(),
                source_audio=song.as_posix(),
                # The four numbers, unmodified. Everything correct about this stage follows from
                # these going to `song_audio_window` and `over_render_window` rather than to a
                # window computed here. All four describe the take rather than the plan:
                # `latest_take_lead` is read off the Shot and never recomputed, because
                # `over_render_lead` would answer for the take a submission *now* would produce;
                # and the window is the one recorded with that lead, for the same reason one step
                # further out — the live `start`/`duration` are a different pair the moment
                # anybody drags the clip.
                start=take_start,
                duration=take_duration,
                song_duration=project.song.duration,
                take_lead=shot.latest_take_lead,
                prefix=(
                    f"music-video-producer/{project_id}/shots/"
                    f"{shot.id}{RESTORE_AUDIO_PREFIX_SUFFIX}"
                ),
            )
        except ValueError as error:
            # Covers the window-past-the-end refusal, raised by `song_audio_window` inside the
            # builder, and every path-shape refusal beside it. Before `comfy.submit`, so none of
            # them costs anything.
            raise HTTPException(status_code=422, detail=str(error)) from error
        # The same four numbers again, so what the Director is told about the take is the take
        # the payload above carries rather than a second description of it.
        lengths = audio_replace_lengths(
            start=take_start,
            duration=take_duration,
            song_duration=project.song.duration,
            take_lead=shot.latest_take_lead,
        )
        # The whole write, and it happens **before** the graph goes out — the Director's
        # 2026-08-21 ruling, and this is the route that found the defect: a save refused after
        # the submit answered 409 for a graph already queued, and the restored file landed on
        # disk with no record of it anywhere. The Shot itself is untouched either way: see
        # this route's docstring.
        #
        # `prompt_id` is `PENDING_SUBMISSION_PROMPT_ID` in the window and deliberately **not**
        # the empty string, which on a `kind="post"` record already means something else
        # entirely — local ffmpeg work, which the assemble route's busy check, startup healing
        # and `api.js`'s progress branch all key on. See that constant.
        job = RenderJob(
            kind="post",
            prompt_id=PENDING_SUBMISSION_PROMPT_ID,
            target_id=shot.id,
            # No sampling happens here, so there is no seed. Left at the model's 0 rather than
            # borrowed from the shot, which would record a number nothing used.
        )
        project.jobs.append(job)
        store.save(project)
        try:
            submission = await comfy.submit(payload)
        except ComfyError as error:
            settle_unsubmitted_jobs(project, job)
            raise HTTPException(status_code=502, detail=str(error)) from error
        accept_submission(job, submission.prompt_id)
        store.save(project)
        matched = (
            abs(lengths["requested_picture_seconds"] - lengths["audio_seconds"])
            <= RESTORE_AUDIO_LENGTH_TOLERANCE
        )
        return AudioRestoreResponse(
            job=job,
            audio_seconds=lengths["audio_seconds"],
            requested_picture_seconds=lengths["requested_picture_seconds"],
            requested_frames=int(lengths["requested_frames"]),
            lengths_match=matched,
            describes_take=describes_take,
            length_note=(
                f"{lengths['audio_seconds']:g}s of the master song, from "
                f"{take_start - shot.latest_take_lead:g}s to "
                f"{take_start - shot.latest_take_lead + lengths['audio_seconds']:g}s, over a "
                f"picture the render asked H3 for as {int(lengths['requested_frames'])} frames "
                f"({lengths['requested_picture_seconds']:.4g}s at 24 fps). "
                + (
                    "The two agree. "
                    if matched
                    # The one way the two numbers *this route computed* can differ, and it is
                    # stated about them rather than about the file: `over_render_window`'s only
                    # clamp is the song's own end, so a shortfall is the song running out before
                    # the requested picture would have reached. Whether the file on disk holds
                    # that many frames is a separate claim and the sentence below refuses to
                    # make it — which is what keeps this branch honest for an undescribed take,
                    # where the requested count is the plan's rather than the render's.
                    else "The two differ: the master runs out before the requested picture "
                    "does, so the tail of the take keeps its own audio. "
                )
                + (RESTORE_AUDIO_UNDESCRIBED_TAKE if not describes_take else "")
                + (
                    RESTORE_AUDIO_WINDOW_MOVED.format(
                        start=shot.start,
                        duration=shot.duration,
                        take_start=take_start,
                        take_duration=take_duration,
                    )
                    if window_moved
                    else ""
                )
                + "Neither is padded or cut: trim_to_audio is off. The frames the file "
                "actually holds are an ffprobe reading, not a number this application claims."
            ),
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
        "/api/projects/{project_id}/shots/{shot_id}/select-take", response_model=Project
    )
    def select_shot_take(
        project_id: str, shot_id: str, request: SelectTakeRequest
    ) -> Project:
        """Point one Shot's `latest_output` at a different clip — an earlier take, or a
        video asset.

        The Director's asks, verbatim (2026-08-20): "Could also use a way to switch the
        selected clip in a shot to a different one if i want" and "I currently have no way
        of attaching a video of my selection from files/assets to a shot i add to the
        timeline". One route for both, because both are the same write: the single
        `latest_output` pointer moves, nothing else. The take strip in the inspector is
        derived client-side from the shot's own job history, so this route only has to
        accept what it can verify:

        - ``output``: a file one of this Shot's own h3 jobs actually produced — the job
          record is the provenance check, so the route cannot be pointed at another
          shot's take by path games.
        - ``asset_id``: a video asset. A generated one already lives under ComfyUI's
          output root and is pointed at directly; an *uploaded* one is copied under
          ``music-video-producer/{project}/clips/`` first, because every reader of
          `latest_output` — the Monitor stream, assembly's probes — resolves against the
          output root and teaching them all a second root is how path handling forks.

        Selecting an external clip clears the over-render bookkeeping (`latest_take_lead`,
        `trim_nudge`): those numbers describe a take rendered with the margin, and carried
        onto a hand-picked clip they would cut its opening quarter-second for no reason.
        A draft shot gains `complete` — it has a clip now, which is what the status tracks.
        """
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        if shot.locked:
            raise HTTPException(
                status_code=422,
                detail=SELECT_TAKE_LOCKED.format(shot=shot_label(project, shot)),
            )
        output_root = (settings.comfy_root / "output").resolve()
        if request.output:
            produced = {
                file
                for job in project.jobs
                if job.kind == "h3" and job.target_id == shot.id
                for file in job.output_files
            }
            if request.output not in produced:
                raise HTTPException(
                    status_code=422,
                    detail=SELECT_TAKE_UNKNOWN.format(shot=shot_label(project, shot)),
                )
            target = (output_root / Path(request.output)).resolve()
            if output_root not in target.parents or not target.is_file():
                raise HTTPException(
                    status_code=404,
                    detail=TAKE_MISSING_FILE_REFUSAL.format(
                        shot=shot_label(project, shot), path=request.output
                    ),
                )
            if shot.latest_output != request.output:
                shot.latest_review = None
            shot.latest_output = request.output
        elif request.asset_id:
            asset = next((a for a in project.assets if a.id == request.asset_id), None)
            if asset is None:
                raise HTTPException(status_code=404, detail="Asset not found")
            if asset.kind != "video":
                raise HTTPException(
                    status_code=422, detail=SELECT_TAKE_NOT_VIDEO.format(name=asset.name)
                )
            source = resolve_asset_path(project_id, asset)
            if asset.source == "upload":
                clips_dir = output_root / "music-video-producer" / project_id / "clips"
                clips_dir.mkdir(parents=True, exist_ok=True)
                landed = clips_dir / f"{asset.id}{source.suffix}"
                if not landed.is_file():
                    shutil.copyfile(source, landed)
                pointer = landed.relative_to(output_root).as_posix()
            else:
                pointer = source.relative_to(output_root).as_posix()
            if shot.latest_output != pointer:
                shot.latest_review = None
            shot.latest_output = pointer
            # External clip: no over-render margin exists in it, so no lead to cut — and no
            # window snapshot either, because nothing rendered this file for a window of this
            # plan. Cleared together with the lead so the pair cannot claim a provenance the
            # clip does not have; `restore_song_audio` reads the absence and says so.
            shot.latest_take_lead = 0.0
            shot.latest_take_start = 0.0
            shot.latest_take_duration = 0.0
            shot.trim_nudge = 0.0
        else:
            raise HTTPException(status_code=422, detail=SELECT_TAKE_EMPTY)
        if shot.status in ("draft", "ready"):
            shot.status = "complete"
        return store.save(project)

    @app.get("/api/projects/{project_id}/shots/{shot_id}/take")
    def read_shot_take(project_id: str, shot_id: str) -> FileResponse:
        """Stream one Shot's latest take to the browser, by ids and by nothing else.

        The URL carries a project id and a shot id and **no path**. The file served is resolved
        here from the Shot's own `latest_output` through `analyze_latest_take`'s resolution,
        containment check included, so there is no path-injection surface to defend: a client
        cannot ask this route for anything except what the manifest says the Shot produced. That
        is the same discipline `read_project_media` applies to the media tree, pointed at
        ComfyUI's output root, where takes actually land.

        Starlette's `FileResponse` answers `Range` itself — verified on this installation, 1.6.0:
        a `bytes=` request gets a 206 with the right `Content-Range`, a suffix or open-ended
        range is served, an unsatisfiable one gets a 416, and the plain 200 advertises
        `Accept-Ranges: bytes`. That is what makes the `<video>` element's scrub bar work, and a
        route test holds it to a real 206 so a change of response class cannot silently turn
        seeking off.

        Both failure rows are 404s with a sentence, per the matrix: the code is for the `<video>`
        element, which treats every error alike, and the sentence is for the Director. The
        missing-file row names the path this looked for, because a manifest pointing at a file
        that is gone is usually a moved or cleared ComfyUI output directory and the path is the
        only way to tell which.
        """
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        if not shot.latest_output:
            raise HTTPException(
                status_code=404,
                detail=TAKE_NOT_RENDERED_REFUSAL.format(shot=shot_label(project, shot)),
            )
        output_root = (settings.comfy_root / "output").resolve()
        target = (output_root / Path(shot.latest_output)).resolve()
        if output_root not in target.parents or not target.is_file():
            raise HTTPException(
                status_code=404,
                detail=TAKE_MISSING_FILE_REFUSAL.format(
                    shot=shot_label(project, shot), path=shot.latest_output
                ),
            )
        return FileResponse(target)

    @app.post("/api/projects/{project_id}/shots/{shot_id}/approve", response_model=Project)
    def approve_take(project_id: str, shot_id: str) -> Project:
        """Approve one Shot's latest take. FR-21: explicit, reversible, never automatic. No body.

        **This is the one writer of approval.** Nothing else in this application assigns
        `approved_output` or the `approved` status — not job completion (`apply_job_history`
        deliberately stops at `complete`), not the assistant, not expansion — and a test scans
        the whole package to keep it that way. What is written is what the server resolved from
        its own manifest: `approved_output := latest_output`, never a value from the wire.
        `approved_output` is about to become assembly's input, and a path the server copied from
        its own record of what rendered is evidence; a path accepted from a client would be a
        claim. This route binds no body at all, so there is nothing on the wire to trust.

        Both fields move together, and the pairing is what makes the un-approve path honest:
        while the approval stands, render-again and mark-ready refuse this Shot, so
        `latest_output` cannot move and `approved_output == latest_output` holds for the life of
        the approval. A test pins that invariant end to end rather than trusting it.

        The refusal order is the house order. In flight first, from the job records as well as
        the status — `shot_render_in_flight` — because a status walked back by hand through the
        generic shots write is exactly what hides a live render, and approving a take that is
        about to be displaced attaches the decision to whichever file lands next; 409, because a
        live render is a state conflict the same request survives. Then idempotence: an approved
        Shot answers 200 and nothing is rewritten, not even `updated_at`. Then the take gate:
        approval is a decision about a specific piece of media, so a Shot that never produced
        one has nothing to approve, and that is a 422 fact no waiting changes.
        """
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        if shot_render_in_flight(project, shot):
            raise HTTPException(
                status_code=409,
                detail=APPROVE_IN_FLIGHT_REFUSAL.format(shot=shot_label(project, shot)),
            )
        # Idempotent, and genuinely a no-op: nothing is saved, so an unchanged manifest does not
        # get a fresh `updated_at` to collide with the next optimistic-concurrency check.
        if shot.approved_output:
            return project
        if not shot.latest_output:
            raise HTTPException(
                status_code=422,
                detail=APPROVE_NO_TAKE_REFUSAL.format(shot=shot_label(project, shot)),
            )
        # The whole write, every half together. The value is the server's own resolution of
        # what this Shot's take is; nothing from the request is on the right-hand side. The
        # window snapshot (AD-13) rides in the same write: the approval is a decision about
        # this take *in this window*, and assembly refuses the Shot if the window moves
        # afterward — see `Shot.approved_start`.
        shot.approved_output = shot.latest_output
        shot.status = "approved"
        shot.approved_start = shot.start
        shot.approved_duration = shot.duration
        return store.save(project)

    @app.post("/api/projects/{project_id}/shots/{shot_id}/unapprove", response_model=Project)
    def unapprove_take(project_id: str, shot_id: str) -> Project:
        """Clear one Shot's approval. The reversal FR-21 promises, and the one way back. No body.

        Un-approval is what re-enables everything that keys on approval — render-again,
        mark-ready, expansion and the assistant all refuse an approved Shot, and none of them
        may be weakened instead — so this route accepts *either* approval signal through
        `shot_is_approved`, the same definition render-again refuses by. A Shot with the
        `approved` status and no `approved_output`, reachable only by hand through the generic
        shots write, would otherwise be a Shot nothing can move: mark-ready disowns the status,
        render-again says to clear the approval, and a route that only recognised
        `approved_output` would refuse to.

        Both fields are cleared together, `status` back to `complete` per the matrix — the Shot
        had a take when it was approved, and a complete Shot is exactly what it goes back to
        being, re-renderable through render-again like any other. Nothing else is touched:
        `latest_output` stays, the take stays on disk, and the refusal for a Shot that is not
        approved names what the Shot actually is rather than only refusing.
        """
        project = get_project(project_id)
        shot = next((item for item in project.shots if item.id == shot_id), None)
        if not shot:
            raise HTTPException(status_code=404, detail="Shot not found")
        if not shot_is_approved(shot):
            raise HTTPException(
                status_code=422,
                detail=UNAPPROVE_NOT_APPROVED_REFUSAL.format(
                    shot=shot_label(project, shot), status=shot.status
                ),
            )
        # The whole write: the decision is withdrawn, the record of what rendered is not.
        # The window snapshot goes with it — it described the withdrawn approval, and a
        # snapshot outliving its approval would make the *next* approval's staleness check
        # read a window nobody decided about.
        shot.approved_output = ""
        shot.status = "complete"
        shot.approved_start = 0
        shot.approved_duration = 0
        return store.save(project)

    async def run_tool(
        args: list[str], on_progress: Callable[[int], None] | None = None
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
        """
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return 127, "", f"{args[0]} is not installed or not on PATH"
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
        refusals = assembly_refusals(clips, song_seconds)
        if refusals:
            raise HTTPException(status_code=422, detail="\n".join(refusals))
        plan = assembly_plan(clips, song_seconds, dimensions)

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
        # FR-24 adapted: the exact takes this export was built from, by shot.
        job = RenderJob(
            kind="post",
            status="running",
            target_id="assembly",
            inputs=[f"{clip.shot_id}={clip.approved_output}" for clip in plan.clips],
        )
        project.jobs.append(job)
        store.save(project)
        app.state.live_assemblies.add(job.id)
        workdir = exports_root / f".work-{job.id}"
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
                        )
                    ),
                    # `at` is a default argument, so the clip's own start on the timeline
                    # is bound when the callback is made rather than read when it fires:
                    # each trim restarts ffmpeg's clock at zero, and a reading has to be
                    # placed at the clip it came from.
                    on_progress=lambda microseconds, at=trimmed_seconds: report(
                        progress.trim(at, microseconds)
                    ),
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
            for clip, frames in zip(plan.clips, plan.frames, strict=True):
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

    @app.put("/api/projects/{project_id}/sections", response_model=Project)
    def replace_sections(project_id: str, request: SectionListRequest) -> Project:
        """The Director's section marks: Intro/Verse/Chorus/Bridge/Outro windows + prompts.

        Sorted by start on write so every reader walks them in time order, refused on
        overlap because a moment of the song belonging to two sections makes both the
        shot→section mapping and the lyric-block pairing ambiguous. Gaps are legal — an
        unmarked stretch simply has no section, and everything downstream treats that as
        unknown rather than inventing coverage.
        """
        project = get_project(project_id)
        ordered = sorted(request.sections, key=lambda section: section.start)
        for first, second in itertools.pairwise(ordered):
            if second.start < first.end - 1e-6:
                raise HTTPException(
                    status_code=422,
                    detail=SECTIONS_OVERLAP_REFUSAL.format(
                        first=first.label, end=first.end,
                        second=second.label, start=second.start,
                    ),
                )
        project.sections = ordered
        return store.save(project)

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
        "/api/projects/{project_id}/timeline/populate",
        response_model=PopulateTimelineResponse,
    )
    async def populate_timeline(
        project_id: str, request: PopulateTimelineRequest
    ) -> PopulateTimelineResponse:
        """Stage 4 of the Director's user workflow: one button lays out the whole plan.

        The model's answer is treated as *shape*, never as arithmetic: its shots carry the
        story (prompts, relative lengths, order), and `populate_windows` repairs the
        geometry into what assembly will later demand — contiguous from 0 to the song's
        end, every window inside H3's reliable 4–15 s range.

        The one number the model is *held* to is how many shots it returns, and that is a
        judgement about shape, not arithmetic: the geometry is repaired either way, but a
        four-shot answer to a three-minute song leaves each prompt smeared across a dozen
        windows, which is a plan in name only. `populate_required_shots` computes the
        count here, the instruction states it three times over as a hard constraint, the
        reply is counted rather than believed, and a short one buys exactly one guided
        retry at a lower temperature with the shortfall named in numbers. Each tiled
        window draws its
        prompt from the proposal whose proportional span of the song contains it
        (`proposal_for_position`), so a count repair cannot orphan a window from the
        story. The shots land as ordinary drafts — mode, citations, singing and
        expansion remain the existing lanes' acts, exactly as the workflow describes
        ("this is also when the prompts for each shot would be Expanded").

        Destructive by design and doubly guarded: the browser shows the warning, and the
        route refuses without `confirm_replace` in the same words — while shots carrying
        protections (approval, a lock) refuse populate entirely by name, because a
        protection that vanishes with the timeline it protected was never a protection.
        """
        project = get_project(project_id)
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
        if not request.confirm_replace:
            raise HTTPException(status_code=422, detail=POPULATE_CONFIRM_REFUSAL)
        duration = project.song.duration
        # The roster the model is offered, and it is `citable_assets` rather than the whole
        # library on purpose. A promoted identity sheet is no longer separately citable — a
        # citation of its source resolves to it below — so offering its display name buys
        # nothing and costs the naming leak the Director reported: `"Close up on eyes of
        # HarderFaster · multiview with flickering light reflections"`, an internal asset label
        # sitting in a shot's creative prose, written there because citation correctness used
        # to depend on the model typing that label. A name never shown cannot be echoed.
        citable = citable_assets(project)
        assets = (
            "; ".join(f"{asset.name} ({asset.kind})" for asset in citable) or "none yet"
        )
        # The count comes from `POPULATE_TARGET_WINDOW_SECONDS`, the target window populate
        # steers the model toward and thereby the plan's typical shot length. NOT the
        # midpoint of H3's 4–15 s training range: the creator's own preset table calls
        # 5.17 s (124 frames) "fastest / safest", and the first live batch measured why —
        # 124-frame renders take ~2–6 min while the 221-frame windows a 9.5 s mean produced
        # took 2.2 HOURS each on this card (2026-08-19). ~5 s cuts also edit better for
        # music video than 9 s holds.
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
        if request.two_stage and not project.sections:
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
        project = get_project(project_id)
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
        if not project.sections and staged_sections:
            project.sections = staged_sections
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
        # With sections marked, each section tiles independently so no shot straddles a
        # boundary — cuts land exactly on the music's own switches, the Director's ask.
        # Unmarked stretches (before, between, after sections) tile as their own spans so
        # the plan still covers the whole song and assembly's gap refusal stays silent.
        # Without sections, the whole song tiles as one span, exactly as before.
        if project.sections:
            spans: list[tuple[float, float]] = []
            cursor = 0.0
            for section in project.sections:
                if section.start - cursor > 0.5:
                    spans.append((cursor, section.start - cursor))
                spans.append((section.start, section.duration))
                cursor = section.end
            if duration - cursor > 0.5:
                spans.append((cursor, duration - cursor))
            windows = []
            for span_start, span_length in spans:
                inside = [
                    (shot.start, shot.duration)
                    for shot in proposals
                    if span_start <= shot.start < span_start + span_length
                ]
                for start, length in populate_windows(
                    inside, span_length, maximum=POPULATE_MAX_WINDOW_SECONDS
                ):
                    windows.append((round(span_start + start, 3), length))
        else:
            windows = populate_windows(
                [(shot.start, shot.duration) for shot in proposals],
                duration,
                maximum=POPULATE_MAX_WINDOW_SECONDS,
            )
        # The mechanical fills the first run needed a hand-run script for, now populate's
        # own act (the run-2 audit's items 4 and 5):
        #
        # * `performance` comes from the model and maps onto `singing`/`use_song_audio`.
        #   `resolve_shot_mode` then routes performance shots to references automatically,
        #   so no mode needs writing. This used to say the strict json_schema's decoder
        #   "forces every key to be emitted"; it does not, and did not — a field with a
        #   default is not in `required`, so the decoder was free to omit it, and on
        #   2026-08-20 one model omitted it on 4 of 5 rolls and every shot came through
        #   here silently non-performance. `director_result_schema` now promotes
        #   `performance` into `PlannedShot.required` whenever `shots` is required, which
        #   is what makes the model decide per shot rather than fall through a default.
        #   Note what that does *not* change: absent and `false` are indistinguishable by
        #   the time a `PlannedShot` exists, so the line below still reads `not_singing`
        #   off silence on any path where the grammar is not enforced (the schema-free
        #   retry in `_completion`, a provider that ignores strict). Telling those apart
        #   needs a tri-state on the shared chat schema and a live measurement of what
        #   `singing="unknown"` does to expansion; neither was in this change.
        # * citations come from the shot's own `assets` field first and from its prose only
        #   as a fallback (`models.assets_for_proposal`). This used to read "citations come
        #   from the prompt itself", and that sentence was the defect: the instruction
        #   commanded exact asset names *in the prose* because the scan was the only
        #   mechanism, so the model had to write "Extreme close up of Crimson Lips Close-up"
        #   to attach a picture — 24 of 30 prompts on the Director's live plan carried a
        #   label. `PlannedShot.assets` is a structural place to name an asset, promoted into
        #   the strict grammar by `PLANNED_SHOT_DECISIONS`, and the instruction now asks for
        #   prose that names nothing.
        #
        #   The scan is kept, demoted. A model that writes a name and omits the field has
        #   said unambiguously which picture it wants, and dropping that citation would send
        #   the shot to render without the reference it asked for — invisible until the take
        #   comes back wrong. An awkward sentence is reviewable; a missing reference is not.
        #   Names under `NAME_SCAN_MIN_LENGTH` characters are still skipped as substring
        #   noise, and an asset named both ways is cited once.
        #
        #   The scan is over `citable_assets`, not the whole library, and that is the same
        #   line the roster and the context dump are drawn on: an identity sheet is not
        #   separately citable, and scanning for its name was also a live substring bug —
        #   "HarderFaster · multiview" contains "HarderFaster", so a prompt naming the sheet
        #   matched *both* assets and spent two picture slots on one face.
        #
        #   Two rules then run over what the scan produced, in this order and no other:
        #   `with_default_setting` may append the project's declared location (it counts
        #   picture slots, so it must see the pre-substitution list — the widest the list
        #   can be), and `prefer_identity_sheets` then re-points every reference at the
        #   promoted sheet of what it names and collapses duplicates, which can only make
        #   the list shorter. Both are no-ops on a project with no promotions and no
        #   declared location, so such a project's citations are byte-identical to what
        #   populate has always written.
        library = citable_assets(project)
        sheets = identity_sheet_ids(project)

        def proposal_citations(proposal: Any) -> list[AssetCitation]:
            # `getattr` for the same reason the `performance` line below uses it: `plan` is
            # duck-typed at every call site and a double that predates this field must keep
            # working, exactly as one that predates `performance` does. Absent is `()`, which
            # is the byte-identical old behaviour — the prose scan alone.
            named = [
                AssetCitation(asset_id=asset.id, role="reference", order=order)
                for order, asset in enumerate(
                    assets_for_proposal(
                        library,
                        declared=getattr(proposal, "assets", None) or (),
                        prose=proposal.prompt,
                    )
                )
            ]
            located = with_default_setting(
                project, named, picture_limit=H3_REFERENCE_LIMITS["picture"]
            )
            return prefer_identity_sheets(located, sheets)

        shots: list[Shot] = []
        for index, (start, length) in enumerate(windows):
            proposal = proposals[
                proposal_for_position(start + length / 2, duration, len(proposals))
            ]
            performing = bool(getattr(proposal, "performance", False))
            # Mapped from the model's own `performance` declaration — a dedicated strict-
            # schema field the instruction explicitly asks for — never inferred from
            # prose. The nothing-infers-singing guard permits exactly this mapping and
            # forbids everything looser; the Director reviews the result per shot in the
            # inspector, exactly as they reviewed the hand-run script it replaces.
            # One measured exception to the declaration, one-directional: a window the
            # track is *measured* to leave voiceless cannot be sung, whatever the model
            # declared — live on 2026-08-19 it marked the intro and the whole
            # instrumental outro singing, and H3 invented words for them and lipsynced
            # to the invention. Unmeasured (`None`) changes nothing, and a not-singing
            # declaration over vocals is a legitimate creative choice, untouched. This
            # is not the inference the singing guard forbids: nothing here reads prose,
            # mode or library — only Whisper's measured voice activity on the track.
            vocal = shot_vocal_overlap(project.song, start=start, duration=length)
            voiceless = vocal is not None and vocal < MIN_SINGING_VOCAL_SECONDS
            declared_singing: SingingState = (
                "singing" if performing and not voiceless else "not_singing"
            )
            shots.append(
                Shot(
                    start=start,
                    duration=length,
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
                    seed=1 + index,
                )
            )
        project.shots = shots
        saved = store.save(project)
        return PopulateTimelineResponse(
            proposed=len(proposals), created=len(saved.shots), project=saved
        )

    @app.post(
        "/api/projects/{project_id}/timeline/snap-cuts",
        response_model=SnapCutsResponse,
    )
    def snap_timeline_cuts(
        project_id: str, request: SnapCutsRequest
    ) -> SnapCutsResponse:
        """Move each shot cut to the nearest moment the track leaves voiceless.

        The Director's ruling on the roadmap's long-open "vocal transition points between
        shots" item (2026-08-20): **cut placement is the lever.** Two adjacent references
        shots each perform their own window of the song, so the mouth on A's last frame and
        the mouth on B's first frame come from two calls that never saw each other. Placing
        the cut where nobody is singing removes the mismatch instead of masking it, and costs
        no GPU and no re-render.

        Report first, apply on confirm — `populate`'s `confirm_replace` shape, enforced here
        rather than trusted to the browser. Without `confirm_apply` this route **does not
        call `store.save`**, and the response carries no project at all, so "nothing was
        written" is visible on the wire rather than asserted in prose.

        Every decision is `timeline.snap_cut_plan`'s; this route's only additions are the
        project lookup, the in-flight set (the job records are the evidence, and
        `shot_render_in_flight` is the one reader of them), the honest-empty refusals, and
        the write. Nothing here renders, arms, queues or approves: the shots' windows move
        and every other field on every shot is untouched.
        """
        project = get_project(project_id)
        if not project.song or project.song.duration <= 0:
            raise HTTPException(status_code=422, detail=SNAP_CUTS_NO_SONG)
        rendering = frozenset(
            shot.id for shot in project.shots if shot_render_in_flight(project, shot)
        )
        try:
            plan = snap_cut_plan(
                project, tolerance=request.tolerance, rendering=rendering
            )
        except TimelineError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        # The two honest-empty branches refuse rather than report, because there is nothing
        # to report: no cut was examined. `unmeasured` is the one the codebase's absent-
        # analysis convention is about — an empty `Song.vocal_spans` means *unmeasured, not
        # silent* (`shot_vocal_overlap`), so the alternative to this sentence would be
        # placing every cut in the plan against a silence nobody heard.
        if plan.status in ("unmeasured", "no_cuts"):
            raise HTTPException(status_code=422, detail=plan.message)
        response = SnapCutsResponse(
            applied=False,
            status=plan.status,
            tolerance=plan.tolerance,
            moved=len(plan.moves),
            skipped=len(plan.skips),
            moves=[
                SnapCutMove(
                    before=move.before_label,
                    after=move.after_label,
                    boundary=move.boundary,
                    proposed=move.proposed,
                    shift=move.shift,
                    gap=move.gap,
                )
                for move in plan.moves
            ],
            skips=[
                SnapCutSkip(
                    before=skip.before_label,
                    after=skip.after_label,
                    boundary=skip.boundary,
                    reason=skip.reason,
                )
                for skip in plan.skips
            ],
            message=plan.message,
        )
        if not request.confirm_apply or not plan.moves:
            return response
        # Applied by shot id from the plan's own `windows`, which is the whole tiling —
        # unchanged shots included — so the contiguity `snap_cut_plan` builds structurally is
        # the contiguity that lands in the manifest, rather than being re-derived here from
        # the moves and given a second chance to drift.
        by_id = {shot.id: shot for shot in project.shots}
        for shot_id, start, duration in plan.windows:
            shot = by_id[shot_id]
            shot.start = start
            shot.duration = duration
        response.project = store.save(project)
        response.applied = True
        return response

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
    async def expand_shot_prompts(
        project_id: str, focus: Literal["story", "photography"] = "story"
    ) -> Project:
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
        #
        # `focus` selects the persona over the identical machinery (2026-08-19): "story"
        # is pass one; "photography" is the DP pass the Director asked for after the
        # first full run's repeated setups — same whole-plan shape, same id-keyed apply,
        # same guards and notices, a different job description and a camera-trimmed input.
        snapshot = get_project(project_id)
        if not snapshot.shots:
            raise HTTPException(status_code=422, detail=EXPANSION_WITHOUT_SHOTS)
        photography = focus == "photography"
        try:
            # The kwarg travels only on the DP pass, so every existing `expand` double —
            # and the story pass's own call shape — stays byte-identical.
            result = await (
                director.expand(
                    expansion_input=dp_input(snapshot), system_prompt=DP_SYSTEM_PROMPT
                )
                if photography
                else director.expand(expansion_input=expansion_input(snapshot))
            )
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

    @app.post(
        "/api/projects/{project_id}/shots/{shot_id}/expand-prompt",
        response_model=ShotExpansionResult,
    )
    async def expand_shot_prompt(project_id: str, shot_id: str) -> ShotExpansionResult:
        """Turn one Shot's intent into an H3-format prompt. Pass two, one Shot at a time.

        No body: everything this needs is already on the Shot and its project. The whole-plan
        `director/expand` above is pass one and is untouched — it lays shots out so they flow
        together, in one call, because cross-shot variance is a property of the plan. This is
        the opposite shape for the opposite reason: one H3 prompt is long, and thirty of them
        will not fit a single context.

        Refusal order matters and is the same one every other automated writer uses: whether
        this Shot may be written to at all comes before whether there is anything to write
        from. A locked Shot with an empty intent should hear that it is locked — telling it to
        write an intent first would send the Director to do work that would then be refused.

        **A malformed answer is not stored.** The checker runs before the write, and a prompt
        that fails it is retried — `attempt_expansion` owns the loop and its budget, shared
        with the sweep so the two paths cannot drift — and only when every attempt fails is
        the last one returned with its problems rather than saved. Storing it would put a
        broken prompt in the manifest that the *next render* would submit, which is exactly the
        outcome checking before a render exists to prevent — and the failure would surface as a
        bad take rather than as a message.
        """
        project = get_project(project_id)
        shot = next((held for held in project.shots if held.id == shot_id), None)
        if shot is None:
            raise HTTPException(status_code=404, detail="Shot not found")

        label = shot_label(project, shot)
        if reason := expansion_write_refusal(shot):
            wording = (
                EXPAND_PROMPT_LOCKED if reason == "locked" else EXPAND_PROMPT_RENDERED
            )
            raise HTTPException(status_code=422, detail=wording.format(shot=label))
        if prompt_is_missing(shot):
            raise HTTPException(
                status_code=422, detail=EXPAND_PROMPT_WITHOUT_INTENT.format(shot=label)
            )

        mode = resolve_shot_mode(shot)
        try:
            outcome = await attempt_expansion(project, shot, director=director)
        except DirectorUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if outcome.kind == "failed":
            raise HTTPException(status_code=502, detail=outcome.detail)
        if outcome.kind == "malformed":
            return ShotExpansionResult(
                project=project,
                applied=False,
                problems=list(outcome.problems),
                prompt=outcome.text,
                note=EXPAND_PROMPT_MALFORMED,
                attempts=outcome.attempts,
            )

        # Re-checked pure so the advisory problems ride along with an applied answer, exactly
        # as they always have: `attempt_expansion` only reports problems for a refusal.
        # A song-audio reference shot's outcome is deterministic prose, not a document —
        # the H3 checker would only report the fields it deliberately does not have.
        advisory: list[str] = []
        if not (shot.use_song_audio and mode == "references"):
            advisory = [
                problem.message
                for problem in h3_check(
                    outcome.text,
                    duration=shot.duration,
                    expect_instruction=mode in H3_KEYFRAME_MODES,
                    forbid_dialogue=shot.use_song_audio,
                    # The under-citation half of the reference bounds surfaces here and only
                    # here: it is advisory, so it rides along with an applied answer rather
                    # than refusing one.
                    reference_slots=reference_slot_counts(project, shot),
                ).problems
            ]

        # Re-read after the await for the reason `director_chat` documents: the Shot may have
        # been locked, rendered or deleted while the model was thinking, and the answer was
        # written against a snapshot that no longer describes it.
        project = get_project(project_id)
        current = next((held for held in project.shots if held.id == shot_id), None)
        if current is None:
            raise HTTPException(status_code=404, detail="Shot not found")
        if reason := expansion_write_refusal(current):
            wording = (
                EXPAND_PROMPT_LOCKED if reason == "locked" else EXPAND_PROMPT_RENDERED
            )
            raise HTTPException(
                status_code=422,
                detail=wording.format(shot=shot_label(project, current)),
            )

        # The same song-audio field normalization the sweep applies; see
        # `normalize_audio_fields`.
        current.h3_prompt = (
            normalize_audio_fields(outcome.text, audio_tag=song_audio_tag(project, current))
            if current.use_song_audio
            else outcome.text
        )
        store.save(project)
        return ShotExpansionResult(
            project=project,
            applied=True,
            problems=advisory,
            prompt=outcome.text,
            attempts=outcome.attempts,
        )

    @app.post("/api/projects/{project_id}/shots/expand-prompts", response_model=Project)
    async def expand_plan_prompts(project_id: str) -> Project:
        """Expand every shot in the plan into H3's format. Pass two, over the whole plan.

        **N sequential model calls, one per shot** — `expand_shots` is what makes that true and
        says why. This route is the plan-wide half of `expand_shot_prompt` above, and the two share
        every rule: the same refusals in the same order, the same format check before any write,
        and the same field.

        No body. Every shot is judged, including the ones nothing can be written to, because "why
        did nothing happen to that shot" is the question a sweep has to answer — a locked shot the
        sweep silently skipped is indistinguishable to the Director from one it forgot.

        **Nothing is persisted until every shot has been judged.** There is one terminal
        `store.save`, and `apply_expansions` commits in one pass after the loop, so a failure
        part-way through leaves the manifest and the in-memory project untouched rather than
        half-applied. Phase one's own mutation testing established that the terminal save is what
        makes that structural, rather than the staging in front of it.

        The project is re-read after the sweep for `director_chat`'s reason, and here it matters
        more than anywhere else in this module: the sweep is many model calls long, so a shot can
        be locked, rendered or deleted several times over while it runs.
        """
        # The one snapshot every payload is built from, so every call sees a consistent plan.
        snapshot = get_project(project_id)
        if not snapshot.shots:
            raise HTTPException(status_code=422, detail=EXPAND_PROMPTS_WITHOUT_SHOTS)
        # Song order, which is the order the Director watches the video in and the order the
        # neighbours' intents make sense in — "rinse and repeat for the next", in their own words.
        swept = ordered_shots(snapshot)
        try:
            outcomes = await expand_shots(snapshot, swept, director=director)
        except DirectorUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

        project = get_project(project_id)
        # Re-checked rather than assumed from the snapshot, exactly as expansion re-checks: saving
        # a reply about a plan that no longer has any shots would leave the thread asserting a
        # sweep of nothing.
        if not project.shots:
            raise HTTPException(status_code=422, detail=EXPAND_PROMPTS_WITHOUT_SHOTS)
        committed = apply_expansions(project, outcomes)
        labels = {shot.id: shot_label(project, shot) for shot in project.shots}
        project.messages.append(
            assistant_reply(
                EXPAND_PROMPTS_MESSAGE, expansion_sweep_notices(committed, labels)
            )
        )
        return store.save(project)

    @app.post("/api/projects/{project_id}/assistant/fill", response_model=Project)
    async def assistant_fill(project_id: str, request: AssistantRequest) -> Project:
        """Fill in the selected Shots from one plain-language request. Assistant ProducerBot.

        The Director's language model with two tools. `fill_shots`' arguments are the shot taxonomy
        itself — `ShotMode`, `AssetRole`, `SingingState` — so a malformed answer is a validation
        error at the edge rather than a plausible string in the manifest. `expand_prompts` is how a
        conversational request reaches the H3 expansion specialist: ProducerBot is the surface and
        the specialists are in its box, so the specialist has no chat of its own. It costs one model
        call *per shot named*, runs after the fills so it expands the intent this turn wrote, and
        passes through `expand_shots` — every refusal a Director's own click meets, unchanged.

        Six properties are load-bearing, and each has a test that breaks if it stops holding:

        * **The selection is the scope.** `request.shot_ids` decides what the model is shown and
          what it may write to. A tool call naming anything else is refused and reported, including
          a real, unlocked Shot elsewhere in the plan. This is what stops tool-calling from widening
          what the assistant can act *on* while it widens what it can do.
        * **Every guard a Director's own click meets.** The lock and the render-provenance rules are
          `shot_write_refusal`, shared with expansion; the prompt gate is `batch.prompt_rejection`
          through `assistant_prompt_rejection`; the mode rules are `mode_specification_problems`;
          the library check is `dangling_citations`. Nothing here reimplements any of them.
        * **No GPU time, on every path.** Nothing in this route touches `comfy`, sets a `status`,
          queues a job, generates an image or promotes an asset. The Director's own description puts
          image generation *after* this, as their next act.
        * **All or nothing per Shot.** A Shot's answer is judged whole and applied whole. A refused
          prompt or an invented asset id discards that Shot's mode and citations with it, because
          a Shot carrying half of an answer looks filled in and is not.
        * **Nothing is persisted until every Shot has been judged.** There is one terminal
          `store.save`, and candidates are built first and committed second, so a failure part-way
          through leaves both the manifest and the in-memory project untouched rather than
          half-written.
        * **Every selected Shot is named in the reply.** Applied, locked, carrying provenance,
          refused, discarded, omitted or answered-for-and-empty: a Shot the Director explicitly
          picked and heard nothing about is the silence this feature is forbidden to have.

        Nothing infers `singing`. The model may *set* it, which is a visible act reported in the
        applied notice; a `None` from the tool leaves whatever the Shot already says, and no branch
        here derives it from a mode, a citation or a prompt.

        The empty-selection refusal, the re-read after the await, the 503/502 mapping and the single
        terminal save all follow `expand_shot_prompts`.
        """
        # Built from the pre-await snapshot, exactly as the chat and expansion prompts are.
        snapshot = get_project(project_id)
        held = {shot.id: shot for shot in snapshot.shots}
        # Deduplicated with order kept: a client that sends one id twice must not make the model
        # answer about it twice, and `dict.fromkeys` is the codebase's stable dedupe.
        requested = list(dict.fromkeys(request.shot_ids))
        selected = [held[shot_id] for shot_id in requested if shot_id in held]
        if not selected:
            raise HTTPException(status_code=422, detail=ASSISTANT_WITHOUT_SHOTS)
        # Refused *before* the call when nothing in the selection could be written to, on
        # EXPANSION_WITHOUT_SHOTS' argument: the model would spend the Director's seconds to be
        # told what this sentence already says. The wordings are the ones the reply would have
        # carried, so the refusal before the call and the notice after it agree.
        blocked: dict[str, list[str]] = {"locked": [], "rendered": []}
        for shot in selected:
            if reason := shot_write_refusal(shot):
                blocked[reason].append(shot_label(snapshot, shot))
        if len(blocked["locked"]) + len(blocked["rendered"]) == len(selected):
            reasons = " ".join(
                wording.format(shots=", ".join(names))
                for wording, names in (
                    (EXPANSION_LOCKED_NOTICE, blocked["locked"]),
                    (EXPANSION_RENDERED_NOTICE, blocked["rendered"]),
                )
                if names
            )
            raise HTTPException(
                status_code=422,
                detail=ASSISTANT_WITHOUT_WRITABLE_SHOTS.format(reasons=reasons),
            )
        try:
            # The requested ids verbatim, not the resolved Shots: `assistant_input` skips the ones
            # this project no longer has, and a test that asserts the route sent the builder's
            # output has to be asserting about a call the builder could have been given directly.
            turn = await director.assist(
                message=request.message,
                assistant_input=assistant_input(snapshot, shot_ids=requested),
            )
        except DirectorUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except DirectorError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        # Re-read after the await, for the reason `director_chat` documents, and here it is also
        # what makes the selection meaningful: a Shot deleted, locked or rendered while the model
        # was thinking is in this project and not in the snapshot the answer was written against.
        project = get_project(project_id)
        shots_by_id = {shot.id: shot for shot in project.shots}
        position = {shot.id: index for index, shot in enumerate(project.shots)}
        present = [shot_id for shot_id in requested if shot_id in shots_by_id]
        # Re-checked rather than assumed from the snapshot, exactly as expansion re-checks: saving
        # a reply about shots that no longer exist would leave the thread asserting a fill of
        # nothing.
        if not present:
            raise HTTPException(status_code=422, detail=ASSISTANT_WITHOUT_SHOTS)
        missing_targets = [shot_id for shot_id in requested if shot_id not in shots_by_id]
        labels = {shot_id: shot_label(project, shots_by_id[shot_id]) for shot_id in present}
        # Swept over the *selection* rather than over the model's answer, which is the difference
        # between this and expansion's equivalent lists. Expansion leaves a locked Shot the model
        # never mentioned unreported, because nothing was going to be written for it either way;
        # here the Director explicitly picked it, so "why did nothing happen to the shot I chose"
        # is a question the reply has to answer whether or not the model addressed it.
        locked: list[str] = []
        rendered: list[str] = []
        writable: list[str] = []
        for shot_id in present:
            reason = shot_write_refusal(shots_by_id[shot_id])
            (locked if reason == "locked" else rendered if reason == "rendered" else writable).append(
                shot_id
            )
        open_to_writing = set(writable)

        staged: list[tuple[int, Shot]] = []
        summaries: list[str] = []
        answered: set[str] = set()
        duplicated: list[str] = []
        out_of_scope: list[str] = []
        empty_fills: list[str] = []
        unknown_assets: list[MessageNotice] = []
        rejected: list[MessageNotice] = []
        specification: list[str] = []
        # The identity-sheet rule is populate's, applied here for the reason it is one function
        # in `models` rather than a branch in one route: this is the *other* writer of citations
        # from a model's answer, and it has the same defect — the assistant is offered the source
        # picture and the sheet promoted from it as two library rows, and a shot conditioned on
        # the single frame is using the weaker of the two. `substituted` collects the shots it
        # changed, because a substitution the reply does not mention is one the Director would
        # have to diff the manifest to find.
        sheets = identity_sheet_ids(project)
        substituted: list[str] = []
        for fill in turn.fills:
            if fill.shot_id not in open_to_writing:
                # A Shot the selection already reports on — locked, or carrying provenance — is not
                # reported a second time as out of scope: it *was* in scope, and the reply already
                # says in the Director's own vocabulary why nothing happened to it.
                if fill.shot_id in labels:
                    continue
                if fill.shot_id not in out_of_scope:
                    out_of_scope.append(fill.shot_id)
                continue
            # First answer wins, before any other check, on `expand_shot_prompts`' argument:
            # last-write-wins would let one Shot be reported as both refused and filled in.
            if fill.shot_id in answered:
                if fill.shot_id not in duplicated:
                    duplicated.append(fill.shot_id)
                continue
            answered.add(fill.shot_id)
            shot = shots_by_id[fill.shot_id]
            # `None` means leave it alone, never clear it. A model that names only a mode must not
            # thereby blank the prompt a Director wrote by hand, so the change set is built from
            # the keys that are actually present.
            changes: dict[str, Any] = {}
            redirected = False
            if fill.mode is not None:
                changes["mode"] = fill.mode
            # Set, never inferred: this is only reached because the tool call carried a value, and
            # the applied notice says so out loud. See `models.SingingState`.
            if fill.singing is not None:
                changes["singing"] = fill.singing
            if fill.citations is not None:
                asked = [
                    AssetCitation(**citation.model_dump()) for citation in fill.citations
                ]
                preferred = prefer_identity_sheets(asked, sheets)
                redirected = preferred != asked
                changes["citations"] = [
                    citation.model_dump() for citation in preferred
                ]
            if fill.prompt is not None:
                reason = assistant_prompt_rejection(fill.prompt)
                if reason:
                    # The whole answer for this Shot goes, not just its prompt. Applying the mode
                    # and the citations from an answer whose prompt was refused would leave a Shot
                    # that reads as filled in and cannot be rendered — and the refused text travels
                    # in `raw`, which `DIRECTOR_CONTEXT_EXCLUDE` keeps out of the next call.
                    rejected.append(
                        rejection_notice(
                            EXPANSION_REJECTED_NOTICE,
                            EXPANSION_REJECTED_EMPTY_NOTICE,
                            raw=fill.prompt,
                            shot=labels[fill.shot_id],
                            reason=reason,
                        )
                    )
                    continue
                changes["prompt"] = fill.prompt
            if not changes:
                empty_fills.append(fill.shot_id)
                continue
            # Validated as a whole Shot rather than assigned field by field, which is what makes
            # the citation/`asset_ids` reconciliation run and what turns anything the tool schema
            # somehow let through into an error here rather than into a stored manifest.
            candidate = Shot.model_validate({**shot.model_dump(), **changes})
            # Only the ids *this answer* introduced. A citation that was already dangling — an
            # asset deleted out from under a Shot yesterday — is the inspector's report to make,
            # and refusing today's answer for it would make an unrelated stale reference into a
            # permanent block on the Shot.
            already_missing = set(dangling_citations(project, shot))
            introduced = [
                asset_id
                for asset_id in dangling_citations(project, candidate)
                if asset_id not in already_missing
            ]
            if introduced:
                unknown_assets.append(
                    MessageNotice(
                        kind="refusal",
                        text=ASSISTANT_UNKNOWN_ASSET_NOTICE.format(
                            shot=labels[fill.shot_id],
                            count=len(introduced),
                            assets=", ".join(_short(asset_id) for asset_id in introduced),
                        ),
                    )
                )
                continue
            if problems := mode_specification_problems(candidate):
                # Reported, never a refusal: a mode with no adapter and a section laid out before
                # its images exist are both real planning work, and the refusal that matters
                # happens where GPU time would be spent.
                specification.append(f"{labels[fill.shot_id]}: {' '.join(problems)}")
            staged.append((position[fill.shot_id], candidate))
            # Recorded at the commit point, never at the substitution: a fill whose prompt was
            # refused is discarded whole, and reporting a substitution on a shot nothing was
            # written to would be reporting a change that did not happen.
            if redirected:
                substituted.append(fill.shot_id)
            summaries.append(f"{labels[fill.shot_id]}: {assistant_fill_summary(changes)}")
        # Nothing above this line has written to the project. What makes "a failure mid-sequence
        # leaves nothing half-applied" structural is the single terminal `store.save` below —
        # nothing is persisted until every Shot has been judged. Committing in one pass here is the
        # second half of it: the in-memory project a later reader sees is never half-written either.
        for index, candidate in staged:
            project.shots[index] = candidate
        omitted = [shot_id for shot_id in writable if shot_id not in answered]

        # The second tool, and it is a second *act* rather than a second field to assign: each shot
        # named here costs its own model call to the expansion specialist. It runs after the fills
        # are committed above, so a shot the model filled in and asked to expand in one turn is
        # expanded from the intent it has just written rather than from the one it replaced.
        #
        # The scope rule is `fill_shots`' own, and it has to be: a tool that could reach a shot the
        # Director did not select would widen what the assistant can act *on*, which is the guard
        # the whole selection-as-consent design exists to hold. The write refusal and the prompt
        # gate are not re-implemented here either — `expand_shots` applies both, in the order phase
        # one pinned — so `open_to_writing` is only about scope.
        wanted: list[str] = []
        for asked in turn.expansions:
            if asked.shot_id not in open_to_writing:
                # A shot the selection already reports on — locked, or carrying provenance — is not
                # reported again as out of scope, on the fill loop's argument exactly.
                if asked.shot_id in labels:
                    continue
                if asked.shot_id not in out_of_scope:
                    out_of_scope.append(asked.shot_id)
                continue
            # First mention wins. Expanding one shot twice in a turn would spend two model calls to
            # keep the second answer, which is a coin toss the Director is paying for.
            if asked.shot_id not in wanted:
                wanted.append(asked.shot_id)
        expansions: list[ShotExpansionOutcome] = []
        if wanted:
            # Rebuilt after the staged fills landed: the map above holds the Shot objects those
            # candidates replaced, so a payload built from it would describe the pre-fill shot.
            current = {shot.id: shot for shot in project.shots}
            try:
                expansions = await expand_shots(
                    project, [current[shot_id] for shot_id in wanted], director=director
                )
            except DirectorUnavailable as error:
                # Reported per shot rather than raised, unlike the sweep route. `director.assist`
                # has already answered, so this is all but unreachable — and raising here would
                # throw away a whole turn of good fills over the expansion half of it.
                expansions = [
                    ShotExpansionOutcome(shot_id, "failed", detail=str(error))
                    for shot_id in wanted
                ]
            expansions = apply_expansions(project, expansions)

        notices: list[MessageNotice] = []
        if staged:
            notices.append(
                MessageNotice(
                    kind="change",
                    text=ASSISTANT_APPLIED_NOTICE.format(
                        count=len(staged), details="\n".join(summaries)
                    ),
                )
            )
        # The lock and the provenance wordings are `expand_shot_prompts`' own, reused rather than
        # reworded. The frozen matrix asks for a refusal "in the same words a Director's click
        # gets", and these are the words every other automated write to a Shot already uses —
        # a second wording for one rule is how the two start describing different rules.
        for reported, wording, kind in (
            (locked, EXPANSION_LOCKED_NOTICE, "refusal"),
            (rendered, EXPANSION_RENDERED_NOTICE, "refusal"),
            (missing_targets, ASSISTANT_MISSING_TARGET_NOTICE, "refusal"),
        ):
            if reported:
                notices.append(
                    MessageNotice(
                        kind=kind,
                        text=wording.format(
                            shots=", ".join(
                                labels.get(shot_id, _short(shot_id)) for shot_id in reported
                            )
                        ),
                    )
                )
        # Beside the applied notice, because it is part of what was written rather than a refusal:
        # these shots were filled in, and the citations they got are not the ids the model named.
        if substituted:
            notices.append(
                MessageNotice(
                    kind="change",
                    text=ASSISTANT_IDENTITY_SHEET_NOTICE.format(
                        shots=", ".join(labels[shot_id] for shot_id in substituted)
                    ),
                )
            )
        notices.extend(unknown_assets)
        notices.extend(rejected)
        if out_of_scope:
            notices.append(
                MessageNotice(
                    kind="refusal",
                    text=ASSISTANT_OUT_OF_SCOPE_NOTICE.format(
                        count=len(out_of_scope),
                        shots=", ".join(_short(shot_id) for shot_id in out_of_scope),
                    ),
                )
            )
        if turn.malformed:
            notices.append(
                rejection_notice(
                    ASSISTANT_MALFORMED_NOTICE,
                    ASSISTANT_MALFORMED_EMPTY_NOTICE,
                    raw="\n".join(turn.malformed),
                    count=len(turn.malformed),
                )
            )
        for reported, wording in (
            (omitted, ASSISTANT_OMITTED_NOTICE),
            (empty_fills, ASSISTANT_EMPTY_FILL_NOTICE),
            (duplicated, ASSISTANT_DUPLICATE_NOTICE),
        ):
            if reported:
                notices.append(
                    MessageNotice(
                        kind="flag",
                        text=wording.format(
                            shots=", ".join(labels[shot_id] for shot_id in reported)
                        ),
                    )
                )
        if specification:
            notices.append(
                MessageNotice(
                    kind="flag",
                    text=ASSISTANT_SPECIFICATION_NOTICE.format(details="\n".join(specification)),
                )
            )
        # The expansion half of the turn, as one block after the fill's report. Its own ordering is
        # `expansion_sweep_notices`' — what was written, then what was refused, then what is worth
        # a look — and it reads after the fill because that is the order the two acts happened in.
        notices.extend(expansion_sweep_notices(expansions, labels))
        # Said only when the model produced nothing at all to act on. A turn that called a tool
        # and had every call refused is a different failure, and every one of those refusals is
        # already its own sentence above. `expansions` counts: a turn that only asked for
        # expansions called a tool, and telling it that it answered in prose would be false.
        if not turn.fills and not turn.expansions and not turn.malformed:
            notices.append(MessageNotice(kind="flag", text=ASSISTANT_WITHOUT_TOOL_CALL_NOTICE))
        message = turn.message.strip() or ASSISTANT_EMPTY_MESSAGE
        # The user's own turn is recorded, unlike expansion's — this one *was* a question, and the
        # thread is the audit trail for what the Director asked as well as for what was written.
        project.messages.append(TreatmentMessage(role="user", content=request.message))
        project.messages.append(assistant_reply(message, notices))
        return store.save(project)

    @app.get(
        "/api/projects/{project_id}/render-status", response_model=RenderStatusReport
    )
    async def read_render_status(project_id: str) -> RenderStatusReport:
        """AD-1's poll endpoint: one reconciliation tick, then the fixed report shape.

        A GET the browser calls on a two-second interval while the project has open jobs, so
        every property that matters here is about cost and quiet: an idle project makes no
        ComfyUI request at all, one tick fetches `/queue` once however many jobs are open,
        the manifest is rewritten only when something actually moved, and a dead ComfyUI is a
        200 with `comfy_online: false` rather than a 502 — a poll loop must never turn a
        ComfyUI restart into a toast every two seconds.

        Live percentages ride this same answer. The listener's map is *read* here and nothing
        more: no request is made for it, no branch depends on it, and — the point — it is never
        folded into the project, so `outcome.changed` is exactly what it was before and a tick
        that learned only "the sampler is on step 7" still writes no manifest. A percentage that
        moved `updated_at` twice a second would collide with every optimistic-concurrency check
        the Director's own edits ride on.
        """
        project = get_project(project_id)
        outcome = await reconcile_render_jobs(project, comfy)
        if outcome.changed:
            try:
                store.save(project)
            except ProjectChangedDuringSave:
                # The one save in this application that must not become a 409. This tick read
                # the project before the Director's own edit landed, so refusing it is right —
                # the alternative is the 2026-08-19 revert — but the poll is a loop, and the
                # next tick two seconds from now re-reads and re-derives exactly this same
                # reconciliation from ComfyUI, so there is nothing to recover and nothing to
                # tell anyone. Reporting it would put an error toast on the Director's screen
                # for a race the Director caused by typing. The report below still describes
                # what the tick actually learned; only the manifest write was dropped.
                logger.info(
                    "Render-status tick for %s lost a save race; the next tick redoes it",
                    project_id,
                )
        return render_status_report(
            project,
            comfy_online=outcome.comfy_online,
            progress=render_progress.snapshot(),
        )

    @app.get("/api/projects/{project_id}/jobs/{job_id}", response_model=RenderJob)
    async def read_job(project_id: str, job_id: str) -> RenderJob:
        """One job, refreshed. Delegates its mutation to `batch.apply_job_history`.

        The completion logic used to live inline here, which is exactly the double ownership
        AD-1 forbids once a polling endpoint exists: two hand-written copies of "what does a
        finished job do to the project" is how the manual refresh and the poll start telling
        different stories about one Shot. This route keeps its own transport contract — a
        dead ComfyUI is this caller's 502, where the poll degrades quietly — and its
        history-first shape, which the smoke scripts drive one job at a time.
        """
        project = get_project(project_id)
        job = next((item for item in project.jobs if item.id == job_id), None)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.prompt_id and job.status not in TERMINAL_JOB_STATUSES:
            try:
                history = await comfy.history(job.prompt_id)
            except ComfyError as error:
                raise HTTPException(status_code=502, detail=str(error)) from error
            apply_job_history(project, job, history)
            if job.status == "queued":
                # History is empty for both waiting and executing prompts. Only the live
                # queue distinguishes them, so a running render is not reported as queued.
                try:
                    located = await comfy.queue_state(job.prompt_id)
                except ComfyError:
                    located = "absent"
                if located == "running":
                    job.status = "running"
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
