from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

# The one mapping of kind → tag name, imported rather than re-spelled. `h3_prompt` is a leaf
# module — it imports nothing from this package, by design, because it describes MiniMax's prompt
# format and not this application's data — so this direction is the only one that cannot become a
# cycle. **Nothing may ever import `models` from `h3_prompt`.** The names live there because that
# is where the *checker* reads them, and the writer below must use the same three or a tag could
# be written under a name the bounds check never counts.
from .h3_prompt import REFERENCE_TAG_NAMES

SongSource = Literal["imported", "generated"]
AssetKind = Literal["character", "setting", "prop", "style", "image", "audio", "video"]
ShotStatus = Literal["draft", "ready", "queued", "running", "complete", "error", "approved"]
JobStatus = Literal["queued", "running", "complete", "error", "cancelled"]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def now_utc() -> datetime:
    return datetime.now(UTC)


#: Who sings this track, as the Director's own declaration. The Director's list, verbatim
#: (2026-08-21): "Instrumental, Female sung, Male sung, Duet, 3+, Choir".
#:
#: `"unstated"` is the default and is **not** `"instrumental"`. Every manifest written before this
#: field existed loads as unstated, and unstated means *nobody has said* — it is the one value that
#: asserts nothing, which is what makes adding the field to a library of existing work safe.
#: Instrumental is a real declaration with real consequences (no sung line to attribute, the
#: treatment carrying the whole story), and reading it off a manifest that predates the question
#: would be inventing a cast. Nothing in this codebase infers this field: no route derives it from
#: the lyric sheet or from the library, no vision inspection writes it, and no tool schema exposes
#: it to a model. `PUT .../song/vocal-type` is its one writer.
VocalType = Literal[
    "unstated", "instrumental", "female", "male", "duet", "ensemble", "choir"
]


@dataclass(frozen=True, slots=True)
class LineTagOption:
    """One entry of a vocal type's per-line dropdown: which singers, and how it reads.

    `slots` are `Asset.character_slot` numbers, and they are also H3's own speaker ids — the
    notation written into the sheet is `(S1)`, `(S1, S2)`, which `h3_prompt._SPEAKER` already
    parses and validates. Empty `slots` is the "Untagged" entry: choosing it removes the mark
    rather than writing a different one.
    """

    slots: tuple[int, ...]
    label: str

    @property
    def notation(self) -> str:
        return speaker_notation(self.slots)


def speaker_notation(slots: Sequence[int]) -> str:
    """`(S1)`, `(S1, S2)` — H3's speaker id spelling, and `""` for no singers named.

    The single construction of the mark, so the writer, the parser and the dropdown cannot
    disagree about a space after the comma. `h3_prompt._SPEAKER` is the checker that already
    understood this spelling before this feature existed, which is why the mark is spelled this
    way and not invented fresh.
    """
    return f"({', '.join(f'S{slot}' for slot in slots)})" if slots else ""


@dataclass(frozen=True, slots=True)
class VocalTypeSpec:
    """Everything a vocal type declares about itself, as a table row rather than as branches.

    `slots` are the `Asset.character_slot` numbers this type's per-line marks can name, and
    therefore exactly the character assets a plan needs slotted before a mark resolves to a
    reference. It is empty for every type that offers no per-line tagging, which is what makes
    "how many characters does this declaration need" one lookup rather than a second table.

    `line_tags` is the dropdown offered at each lyric line, and it is empty for most types **on
    purpose**:

    * `unstated` — nothing has been declared, so there is no cast to choose from. Offering a
      dropdown here would be asking the Director to attribute lines to singers they have not said
      exist.
    * `instrumental` — there is no sung line to attribute. See `INSTRUMENTAL_NOTE`.
    * `female`, `male` — one voice sings every line, and the song-level declaration already says
      which. A dropdown on every line of a solo song is a control whose answer never varies:
      noise on the Director's screen, and a second place for the same fact to be wrong.
    * `choir` — a choir is a mass voice, not a cast. There is no Char 1 to distinguish from a
      Char 2, so the per-line question has no answer to offer; the declaration itself is the whole
      of what a choir has to say. If a choir song turns out to have named soloists, that is a
      `duet`/`ensemble` song with a choir behind it, and the Director declares it as one.

    Only `duet` and `ensemble` name a cast, and only they are tagged.
    """

    label: str
    slots: tuple[int, ...]
    line_tags: tuple[LineTagOption, ...]


#: The taxonomy as data. Adding a fourth voice to `ensemble` is a slot number and a `LineTagOption`
#: on this row; nothing anywhere branches on a vocal type name.
#:
#: `ensemble` stops at three, and that is a stated bound rather than an oversight: the Director's
#: own wording is "Char1/Char2/Char3+/Both/All", so three is the roster they named, and a fourth
#: is one entry here plus a `character_slot` bound that widens with it (`CHARACTER_SLOT_LIMIT` is
#: derived from this table, not typed).
VOCAL_TYPE_SPECS: dict[VocalType, VocalTypeSpec] = {
    "unstated": VocalTypeSpec(label="Not stated", slots=(), line_tags=()),
    "instrumental": VocalTypeSpec(label="Instrumental", slots=(), line_tags=()),
    "female": VocalTypeSpec(label="Female sung", slots=(), line_tags=()),
    "male": VocalTypeSpec(label="Male sung", slots=(), line_tags=()),
    "duet": VocalTypeSpec(
        label="Duet",
        slots=(1, 2),
        line_tags=(
            LineTagOption(slots=(), label="Untagged"),
            LineTagOption(slots=(1,), label="Char 1"),
            LineTagOption(slots=(2,), label="Char 2"),
            LineTagOption(slots=(1, 2), label="Both"),
        ),
    ),
    "ensemble": VocalTypeSpec(
        label="3+ voices",
        slots=(1, 2, 3),
        line_tags=(
            LineTagOption(slots=(), label="Untagged"),
            LineTagOption(slots=(1,), label="Char 1"),
            LineTagOption(slots=(2,), label="Char 2"),
            LineTagOption(slots=(3,), label="Char 3"),
            LineTagOption(slots=(1, 2, 3), label="All"),
        ),
    ),
    "choir": VocalTypeSpec(label="Choir", slots=(), line_tags=()),
}

#: The largest slot number any vocal type can name, and therefore the bound on
#: `Asset.character_slot`. Derived from the table above rather than typed, so a fourth voice added
#: to `ensemble` widens the bound in the same edit — a hand-typed `3` here would let a Director
#: choose "Char 4" in a dropdown and then be refused when they tried to slot an asset to it.
CHARACTER_SLOT_LIMIT = max(
    (max(spec.slots, default=0) for spec in VOCAL_TYPE_SPECS.values()), default=0
)


def line_tag_options(vocal_type: VocalType) -> tuple[LineTagOption, ...]:
    """The per-line dropdown for one vocal type. Empty means *no dropdown at all*.

    A function rather than a direct table read because "which types are tagged" is the question
    three surfaces ask (the renderer, the populate check, the contract test), and an empty tuple
    is the answer that has to mean "draw nothing" everywhere rather than "draw an empty select".
    """
    return VOCAL_TYPE_SPECS[vocal_type].line_tags


class Song(BaseModel):
    """A track and what it is: the audio, its timing spine, and the two context fields.

    The context fields carry the largest hand-authored text this application accepts, so each
    has the single-slot recovery `Project.treatment`/`style_bible` gained in Story 2.1 — one
    previous version, no history stack, and a restore that swaps rather than pops.

    The slots are `str | None`, which is the one place this deliberately does *not* mirror the
    document slots. Those are `str = ""` and so cannot tell "no version was ever kept" from "the
    version kept was blank"; the document restore route resolves that by refusing an empty slot,
    which is defensible there because a first draft into a blank document is not a replacement.
    It is not defensible here. A Director who pasted a lyric sheet over a blank field has a real
    previous version — the blank — and wanting it back is an ordinary undo, not a corner case.
    `None` means no save has ever displaced anything; `""` means a save displaced a blank.

    Both are excluded from the Director's context by `app.SONG_DIRECTOR_WITHHELD`, which is
    enforced by classification rather than by a path — see that constant for why.
    """

    title: str
    source: SongSource
    path: str = ""
    duration: float = Field(default=0, ge=0)
    lyrics: str = ""
    caption: str = ""
    prompt_id: str = ""
    lyrics_previous: str | None = None
    caption_previous: str | None = None
    #: Where the track actually carries a voice, as [start, end] second pairs, sorted and
    #: non-overlapping. Measured (Whisper word timestamps, merged at 0.75 s gaps), never
    #: inferred from the lyric sheet — the sheet has no clock. Empty means *unmeasured*,
    #: not silent: every guard that reads this must treat `[]` as "no evidence" and stand
    #: aside. What it exists to stop, live on 2026-08-19: five shots marked `singing`
    #: whose windows sit in the intro and outro — told to sing over an instrumental
    #: reference, H3 invents its own words and lipsyncs to them.
    vocal_spans: list[tuple[float, float]] = Field(default_factory=list)
    #: Every word Whisper heard in the track, as (text, start, end) — the raw measurement
    #: `vocal_spans` and the lyric-to-time alignment both derive from, kept so re-deriving
    #: never costs another transcription. Empty means unmeasured, exactly as above. The
    #: Director's uses, in their own words (2026-08-20): "knowing where words are and
    #: arent is useful for knowing which Shots have words, when the cuts should happen,
    #: when the chorus and verses are".
    lyric_words: list[tuple[str, float, float]] = Field(default_factory=list)
    #: Who sings this track. See `VocalType` — `"unstated"` is the default and asserts nothing.
    #:
    #: **Server-owned, and set by `PUT .../song/vocal-type` alone**, exactly as
    #: `Project.default_setting_id` and `Asset.consistency_prompt` are, and for the reason those
    #: two record: the generic full-project `PUT` binds a whole client-supplied `Song`, so a body
    #: that simply omits this field — which is what every client written before it existed sends —
    #: arrives carrying the default, and one ordinary save would silently un-declare the
    #: Director's cast. That route re-adopts the stored value rather than trusting a body.
    #:
    #: The per-line singer marks are deliberately **not** a second field beside this one. They
    #: live inline in `lyrics`, as `(S1)` at the head of a line, because a parallel structure keyed
    #: on line number drifts the moment the sheet is edited and drifts *silently*. See
    #: `timeline.lyric_line_tags`.
    vocal_type: VocalType = "unstated"


class SongSection(BaseModel):
    """One window of the song's structure: Intro, Verse, Chorus, Bridge, Outro…

    The Director's ask, verbatim intent (2026-08-19): a Section row under References,
    "marked with its own window and prompt … that whole section would share
    characteristics, but shot for shot there is some variance." Sections are the layer
    between the treatment and the shots: populate tiles within them, and the expansion
    reads a shot's section to know which lyric block its words come from — the fix for
    the wrong-verse lipsync found on the first full batch (a shot at 30 s was expanded
    with the song's opening line, because unaligned lyrics left the model guessing).

    `label` is free text but pairs with the lyric sheet's `[Tag]` blocks **by order of
    appearance**: the Nth section whose label matches a sheet tag (case-insensitive,
    "Verse 2" matches `[Verse]`) is paired with the Nth such block, so the sheet's own
    structure carries the timing the tags themselves lack. Nothing infers sections; the
    Director marks them by ear, or accepts populate's proposal once one exists.
    """

    id: str = Field(default_factory=lambda: new_id("section"))
    label: str = Field(min_length=1, max_length=60)
    start: float = Field(ge=0)
    duration: float = Field(gt=0)
    #: The section's shared characteristics, layered under every shot prompt inside it —
    #: "standing at the mic" for verses, "on the bed, glamour angles" for choruses.
    prompt: str = ""

    @computed_field
    @property
    def end(self) -> float:
        return self.start + self.duration


class VisionInspectionRecord(BaseModel):
    model: str = ""
    summary: str
    identity: list[str] = Field(default_factory=list)
    environment: list[str] = Field(default_factory=list)
    continuity_cues: list[str] = Field(default_factory=list)
    prompt_cues: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    analyzed_at: datetime = Field(default_factory=now_utc)


class Asset(BaseModel):
    id: str = Field(default_factory=lambda: new_id("asset"))
    name: str
    kind: AssetKind
    path: str
    source: str = "upload"
    parent_id: str | None = None
    prompt: str = ""
    prompt_id: str = ""
    #: The Director's own appearance anchor for this asset — a short stored phrase naming what
    #: it looks like ("a woman in a red leather jacket and black boots"), carried into every
    #: place a description of this asset is consumed: the reference map's tag lines, the H3
    #: expansion specialist's per-reference block, and the assistant's asset library.
    #:
    #: **It is user-owned, and it wins.** Where an asset has both a `prompt` (what was asked
    #: for) and a `vision` summary (what a model saw), this is what the Director says is true,
    #: and it outranks both — `timeline._asset_description` is the one place that ordering is
    #: written down. Nothing in this codebase infers it: no route derives it from `prompt`, no
    #: vision inspection writes it, and no tool schema exposes it to a model. It is set by an
    #: explicit `PUT` and by nothing else, which is the recorded rule for a mechanical field
    #: (2026-08-19: a local model silently omitted the boolean fields it claimed to have set).
    #:
    #: Empty is the default and means *no anchor stored*, not "this asset looks like nothing".
    #: Every consumer must produce byte-identical output for an empty anchor — that is what
    #: makes the field safe to add to a manifest full of existing work.
    consistency_prompt: str = ""
    #: Which singer of the song this character *is*, as a slot number, or `0` for unslotted.
    #:
    #: The Director's decision (2026-08-21): a character asset links to a per-line tag "by a slot
    #: number on the Asset — `(S1)` resolves to whichever character asset holds slot 1". The link
    #: is a number on the Asset rather than an asset id on the tag because the tag lives in the
    #: lyric sheet, and a sheet carrying asset ids would be a sheet that breaks when an asset is
    #: replaced. A slot survives the swap: re-slot the new character and every `(S1)` in the sheet
    #: follows.
    #:
    #: `0` is the default and means *unslotted*, which is what every manifest written before this
    #: field existed loads as and what every asset that is not a singer keeps forever. Bounded by
    #: `CHARACTER_SLOT_LIMIT`, which is derived from `VOCAL_TYPE_SPECS`, so no asset can hold a
    #: slot no dropdown can name.
    #:
    #: Meaningful only on `kind == "character"`; the route refuses any other kind by name rather
    #: than storing a number that resolves nothing. **Server-owned, set by
    #: `PUT .../assets/{id}/character-slot` alone**, on `consistency_prompt`'s exact rule and for
    #: its exact reason: the generic full-project `PUT` re-adopts the stored value per asset id,
    #: because a defaulted `int` that any pre-existing client omits arrives as `0` and one
    #: ordinary save would un-slot the whole cast at once. Nothing infers it — no route derives it
    #: from the library holding exactly one character, and no tool schema exposes it to a model.
    character_slot: int = Field(default=0, ge=0, le=CHARACTER_SLOT_LIMIT)
    vision: VisionInspectionRecord | None = None
    created_at: datetime = Field(default_factory=now_utc)


#: What kind of shot this is, as the Shot's own declaration rather than something read off its
#: attachments. Each value is one row of the taxonomy in
#: `_bmad-output/planning-artifacts/shot-modes-and-pre-generation-planning.md`, and each row there
#: was read from the reachable subgraph of the Director's per-mode export rather than from its
#: title. Two of those readings are worth repeating here because a title would mislead:
#:
#: * MiniMax H3's "image editing" export is a `MiniMaxH3ReferenceToVideo` run at `length: 5` with
#:   `ref_image_size: "max"`. It is a five-frame *reference* render, so it is a parameter of
#:   `references` and not a shot kind of its own. It is deliberately absent from this list.
#: * LTX's first/middle/last takes the middle frame's **position** as a parameter (`0.51`), not a
#:   fixed centre. That is an adapter input; what the mode declares here is only that a middle
#:   frame is cited at all.
#:
#: Also deliberately absent: `enhance`, `slice` and `audio_replace`. Each of those operates on an
#: existing take or file rather than describing what a shot *is* — the enhancer already has its
#: own route, which writes nothing to the Shot — so making them modes would put an operation in a
#: field that answers "what is this shot".
ShotMode = Literal[
    "text_to_video",
    "image_to_video",
    "first_last",
    "first_middle_last",
    "references",
    "extend",
]

#: The three strings `Shot.mode` carried before it meant anything, mapped to "undeclared" on load.
#:
#: `mode` existed on this model and was written by the inspector's dropdown, but **nothing ever
#: read it**: `generate_h3` branched on `shot.asset_ids or shot.use_song_audio`, so a Shot saying
#: `"text"` while carrying an Asset still rendered as a reference shot. Reading those stored values
#: as declarations now would change what an existing Shot renders — exactly what this change is
#: forbidden to do — so they are recognised as the legacy vocabulary they are and resolved by
#: behaviour instead, which is what they already meant.
#:
#: This is why the new vocabulary shares no spelling with the old one. `"reference"` and
#: `"references"` are different strings on purpose: it makes "a value nobody decided" and "a value
#: the Director chose" distinguishable forever, rather than only until someone re-uses a name.
LEGACY_SHOT_MODES = frozenset({"text", "image", "reference"})

#: What an Asset is *for* in one Shot. The role is on the citation and never on the Asset, because
#: the Director's plan reuses one wolf, location or character across many shots — the wolf is a
#: middle frame *in this shot* and a plain reference in another. A role on the Asset would force a
#: duplicate per part, which is the reuse the whole design exists to keep.
AssetRole = Literal["reference", "first", "middle", "last", "source_video"]

#: How each role reads in a sentence the Director is shown. Singular; callers add the count.
ASSET_ROLE_LABELS: dict[str, str] = {
    "reference": "reference",
    "first": "first frame",
    "middle": "middle frame",
    "last": "last frame",
    "source_video": "source video",
}

#: Whether the performer is singing in this shot — a property of the *performance*, not of the
#: mode. A references shot may or may not be a singing shot, and so may a first/last shot.
#:
#: `"unknown"` is the default and is **not** `"not_singing"`. The LTX enhancer measurably moves lip
#: position (`ManualSigmas` starts at 0.909375 and the graph re-generates rather than refines), so
#: a shot wrongly recorded as not singing loses its lip-sync to a tool that was applied on the
#: strength of a value nobody set — and a shot wrongly recorded as singing is refused an
#: enhancement that was pure gain on it. Both are worse than an honest absence, so nothing in this
#: codebase may infer this field.
SingingState = Literal["unknown", "singing", "not_singing"]


@dataclass(frozen=True, slots=True)
class RoleRequirement:
    """How many Assets one mode cites in one role. `maximum` is inclusive."""

    role: AssetRole
    minimum: int
    maximum: int


@dataclass(frozen=True, slots=True)
class ShotModeSpec:
    """Everything a mode declares about itself, as a table entry rather than as branches.

    `adapter` is the name of the graph builder that renders this mode, or `""` for a mode this
    application cannot yet render. A mode with no adapter is deliberately still *plannable*: a
    Director laying out a first/middle/last section before that adapter exists is doing useful
    work, and refusing at render with a reason is more honest than pretending the mode does not
    exist. What must never happen is a mode that looks renderable and is not, which is why the
    route reads this field rather than a list of its own.

    `song_audio` is whether the mode takes the master song as an audio reference. Only the
    reference graph has a slot for one, so declaring it here is what stops a text-to-video shot
    from being submitted with `use_song_audio` set and having it silently dropped.

    `workflow` is the workflow the adapter renders through, under the name the Director knows
    it by, or `""` for a mode with no adapter. It exists because "Text to video" alone told
    the Director nothing about *which* graph a render would spend GPU minutes on — the mode
    select prints it, so which MiniMax workflow a mode employs is readable before the click
    rather than discovered in ComfyUI's console. It lives here, on the one table everything
    derives mode facts from, so nobody hand-types a parallel mode→workflow list; the invariant
    that a mode names a workflow exactly when it has an adapter is pinned by a test.
    """

    label: str
    roles: tuple[RoleRequirement, ...]
    song_audio: bool
    adapter: str
    workflow: str = ""


#: The taxonomy as data. Adding a mode is a row here; nothing branches on a mode name.
SHOT_MODE_SPECS: dict[ShotMode, ShotModeSpec] = {
    "text_to_video": ShotModeSpec(
        label="Text to video",
        roles=(),
        song_audio=False,
        adapter="h3-director",
        workflow="the MiniMax H3 Director graph",
    ),
    # Both keyframe modes render through one adapter over `MiniMaxH3ImageToVideo`, whose live
    # schema (read 2026-08-18) declares `first_frame` and `last_frame` both optional and offers
    # **no reference-audio input anywhere** — so `song_audio=False` here is the node's fact, not
    # this table's policy: a keyframe shot cannot be conditioned on the master song and cannot
    # lip-sync to it; its audio is generated by the sampler like the text-only path's. Routing
    # `image_to_video` through H3 is the Director's ruling, not a discovery — the LTX 2.5 I2V
    # evidence (`ltx25-i2v-user-export.json`) stays imported and becomes the alternative path's
    # evidence if fidelity ever argues for it (ROADMAP wording is the Director's).
    "image_to_video": ShotModeSpec(
        label="Image to video",
        roles=(RoleRequirement("first", 1, 1),),
        song_audio=False,
        adapter="h3-keyframe",
        workflow="MiniMax H3 I2V-FLframe (first frame only, no song lip-sync)",
    ),
    "first_last": ShotModeSpec(
        label="First / last frame",
        roles=(RoleRequirement("first", 1, 1), RoleRequirement("last", 1, 1)),
        song_audio=False,
        adapter="h3-keyframe",
        workflow="MiniMax H3 I2V-FLframe (first and last frames, no song lip-sync)",
    ),
    "first_middle_last": ShotModeSpec(
        label="First / middle / last",
        roles=(
            RoleRequirement("first", 1, 1),
            RoleRequirement("middle", 1, 1),
            RoleRequirement("last", 1, 1),
        ),
        song_audio=False,
        adapter="",
    ),
    "references": ShotModeSpec(
        label="References to video",
        # Nine pictures, three videos and three audios, which is the node's own arity. The count
        # is a ceiling on citations and nothing more: the *per-kind* limits are enforced where the
        # kinds are known, in `workflows.build_h3_reference_payload`, and are not restated here —
        # a second copy of a limit is a second thing to keep true.
        #
        # `first` and `last` ride this mode too, per MiniMax's guide §2.2.2: a reference picture
        # *is* a shot's first frame, keyframe or last frame when the structured prompt declares it
        # so, on the very node that takes the windowed master song — which is how a keyframe and
        # lip-sync combine at all. The picture travels as an ordinary reference slot and counts
        # against the 9-picture ceiling; only the prompt knows its role, because H3's media slots
        # are anonymous and the prompt is where a slot becomes "the first frame".
        roles=(
            RoleRequirement("reference", 0, 15),
            RoleRequirement("first", 0, 1),
            RoleRequirement("last", 0, 1),
        ),
        song_audio=True,
        adapter="h3-reference",
        workflow=(
            "MiniMax H3 References-to-Video (with the sampling profiles; a first or last "
            "keyframe may ride as a reference picture, with song lip-sync)"
        ),
    ),
    "extend": ShotModeSpec(
        label="Extend an existing video",
        roles=(RoleRequirement("source_video", 1, 1),),
        song_audio=False,
        adapter="",
    ),
}


class AssetCitation(BaseModel):
    """One library Asset, cited by one Shot, in one role.

    A citation and never a copy. The Director's plan reuses the same wolf, location or character
    across many shots — one character in their verse, another in theirs, both together for a duet —
    and a Shot that copied an Asset would make that plan unrevisable: fixing the wolf would mean
    finding every copy of it. `asset_id` therefore names a row of `Project.assets` and carries
    nothing from it. Two Shots citing one Asset in different roles both hold, and the Asset itself
    is untouched by either.

    `order` orders citations *within* a role, which is what FR-19's deterministic numbering needs
    once one list holds several roles. It is the sort key; list position is the tie-break, because
    the sort is stable. A client that never sets it therefore gets exactly list order, which is
    what every citation migrated from `asset_ids` has.
    """

    asset_id: str
    role: AssetRole = "reference"
    order: int = 0


class Shot(BaseModel):
    """One window of the song, and what it is meant to be built from.

    `mode` is the Shot's own declaration and `None` means it has never made one. An undeclared Shot
    resolves through `resolve_shot_mode` to the mode it already behaves as, which is what every
    Shot saved before this field meant anything gets — see `LEGACY_SHOT_MODES`.

    `citations` and `asset_ids` are one fact stored twice, and the validator below keeps them that
    way rather than letting them drift. `citations` is the truth; `asset_ids` is its projection
    onto the reference role, kept because it is what the whole existing render path, the inspector
    and every saved manifest already speak.
    """

    id: str = Field(default_factory=lambda: new_id("shot"))
    start: float = Field(ge=0)
    duration: float = Field(gt=0)
    prompt: str = ""
    # The H3-format expansion of `prompt`, in the structure MiniMax's prompt guide documents:
    # an optional instruction line and three named fields, with shot markers, cut times, speaker
    # ids and reference tags inside the first. See `h3_prompt.py`.
    #
    # Deliberately a *second* field rather than a replacement. `prompt` holds the short intent a
    # human wrote or the whole-plan expansion produced; this holds the long machine-facing form
    # derived from it. Overwriting `prompt` would destroy the readable, editable thing the plan
    # pass wrote and leave nothing to re-expand from — the expansion has to be repeatable, because
    # the first one will not be the good one.
    #
    # Empty means "not expanded", which is a real state and not a defect: a Shot is plannable long
    # before it is expanded, and the render path falls back to `prompt` exactly as it always did.
    h3_prompt: str = ""
    # The reference map `h3_prompt` was expanded against — `app.reference_map_sentence`'s string,
    # written by every writer of `h3_prompt` and by nothing else.
    #
    # It exists because a citation change leaves the stored expansion naming the *old* references
    # while the render wires the new ones, and that staleness is undecidable from the expansion's
    # own text for a document-mode prompt: the specialist weaves `<Picture 2>` into prose and never
    # writes the map's sentences down, so nothing in the manifest said which map it had been handed.
    # The live defect (2026-08-20): a shot whose take had invented a woman in a wedding dress got
    # the character sheet attached, and `h3_prompt` went on naming only the bed. A song-audio
    # reference shot's expansion *is* the map, verbatim, so that one is decided from the text and
    # this field is only read for the document modes — but it is written for both, because
    # `use_song_audio` can be turned off on a shot that already has an expansion.
    #
    # `""` means "not recorded", which is what every expansion written before this field existed
    # has and is deliberately **not** read as stale: nothing knows what map that prompt was built
    # from, and refusing a render on a guess is worse than the render. The next expansion records
    # one. Plan content rather than take provenance — it travels with `h3_prompt` and a duplicate
    # that carried the prompt without the map would claim an unverifiable expansion.
    h3_prompt_map: str = ""
    # Defaulted to `None` — undeclared — rather than to any mode. A default here is a declaration
    # nobody made, and the one thing this field exists to end is a Shot whose kind was decided by
    # something other than the Director.
    mode: ShotMode | None = None
    asset_ids: list[str] = Field(default_factory=list)
    citations: list[AssetCitation] = Field(default_factory=list)
    reference_labels: dict[str, str] = Field(default_factory=dict)
    # Nothing infers this. See `SingingState`.
    singing: SingingState = "unknown"
    use_song_audio: bool = False
    seed: int = Field(default=0, ge=0)
    status: ShotStatus = "draft"
    prompt_id: str = ""
    latest_output: str = ""
    latest_review: VisionInspectionRecord | None = None
    approved_output: str = ""
    # AD-13's window snapshot: the `start`/`duration` this Shot had at the moment its take
    # was approved, written by the approve route and cleared by un-approve, nothing else.
    # The approval is an editorial decision about one take *in one window* — assembly trims
    # the take to the window, so a window edited after approval makes the approved file the
    # wrong length for the plan, silently. The snapshot is what makes that staleness
    # decidable: assembly compares these to the live values and refuses on any difference.
    #
    # `approved_duration == 0` means "never snapshotted": `duration` itself is constrained
    # `gt=0`, so zero is unrepresentable as a real window and safely marks approvals made
    # before these fields existed (they refuse assembly with re-approve wording, not the
    # stale wording). Defaults keep every existing manifest loading unchanged.
    approved_start: float = 0
    approved_duration: float = 0
    # The over-render pair (spec-monitor-and-over-render). Takes are rendered ~half a
    # second longer than their window; these two decide which slice of the take the
    # timeline's window shows.
    #
    # `latest_take_lead` is how far before the window the take begins — the sync-correct
    # offset for a song-audio take, written at submission by `generate_h3` alongside
    # `prompt_id` (it describes the take the submitted job will produce) and by nothing
    # else. Recorded rather than derived, because a pre-margin take and a post-margin one
    # are indistinguishable by arithmetic on their lengths; every take rendered before the
    # margin existed correctly reads 0.
    #
    # `trim_nudge` is the Director's fine-tune on top of the lead: seconds added to the
    # cut point, negative allowed up to the lead. Effective offset = lead + nudge, one
    # rule read by the Monitor, the inspector and assembly alike. Deliberately NOT
    # snapshotted at approval and still editable on an approved shot: it selects a slice
    # of the approved file — the "fine tune with the extra added length" the ruling asks
    # for — while the file itself stays immovable.
    latest_take_lead: float = Field(default=0, ge=0)
    # The window the take was rendered *for*, snapshotted at submission beside
    # `latest_take_lead` — AD-13's `approved_start`/`approved_duration` idiom, for the
    # same reason and with the same "0 means never snapshotted" reading (`duration` is
    # `gt=0`, so no real window is 0 seconds long).
    #
    # A take is fixed at render time and `start`/`duration` go on being edited afterwards
    # — dragging a rendered clip's left edge moves `start` while `trim_nudge` compensates,
    # and the take still begins where it always did. Without this snapshot the only
    # description of the take was the *live* window, so `restore_song_audio` laid the
    # master over the take at an offset the take had never been performed against and by
    # a frame count the render never asked for, silently. Recorded rather than derived,
    # exactly as the lead is: nothing in the take's own bytes says which window produced
    # it. Takes rendered before 2026-08-21 read 0 here and are reported as undescribed
    # rather than described wrongly.
    latest_take_start: float = Field(default=0, ge=0)
    latest_take_duration: float = Field(default=0, ge=0)
    trim_nudge: float = 0
    # Whether this shot's take audio is *accepted* into the mix (spec-take-audio-mix):
    # the Monitor plays it over the master and assembly mixes its window-slice under the
    # song. Default muted — the Director's rule is "only the main music track and
    # accepted audio from videos would come through" — so an untouched project sounds
    # exactly as the 2026-08-16 song-only ruling shipped. Nothing infers it.
    mix_take_audio: bool = False
    # AD-5's re-render mark: the Director flags a shot whose take fell short, and the
    # flagged set resubmits as its own batch scope. Independent of render state; cleared
    # only by that shot's successful resubmission or by hand — never by the batch
    # draining, and nothing infers it.
    flagged: bool = False
    locked: bool = False

    @field_validator("mode", mode="before")
    @classmethod
    def _drop_legacy_mode(cls, value: object) -> object:
        """A legacy `mode` string is not a declaration. See `LEGACY_SHOT_MODES`.

        Done in a `before` validator rather than by widening the `Literal`, so the legacy strings
        are unrepresentable on the model afterwards: nothing downstream has to remember that
        `"reference"` and `"references"` are different, because only one of them can exist.
        """
        return None if isinstance(value, str) and value in LEGACY_SHOT_MODES else value

    @model_validator(mode="after")
    def _reconcile_citations(self) -> Shot:
        """Make `citations` and `asset_ids` agree, with `citations` as the truth.

        Two directions, and both are migrations rather than conveniences:

        * a Shot with `asset_ids` and no `citations` is one saved before roles existed, or one
          written by a client that still speaks only the flat list. Its Assets become citations in
          the `reference` role, in list order, which is exactly what they already were;
        * a Shot with `citations` has its `asset_ids` rewritten to the reference-role citations in
          order, so the field cannot claim an attachment the citations do not have — including
          claiming one that has since been given a different role.

        The consequence worth stating: after this runs, iterating the reference-role citations and
        iterating `asset_ids` produce the same ids in the same order, for every Shot that has ever
        been saved. That is what lets the render path move onto citations without moving a byte of
        any payload it builds.
        """
        if self.citations:
            self.asset_ids = [
                citation.asset_id for citation in citations_in_role(self, "reference")
            ]
        elif self.asset_ids:
            self.citations = [
                AssetCitation(asset_id=asset_id, role="reference", order=index)
                for index, asset_id in enumerate(self.asset_ids)
            ]
        return self

    @computed_field
    @property
    def end(self) -> float:
        return self.start + self.duration


#: What a Shot made from another Shot inherits: the plan, and only the plan.
#:
#: Duplicate `structuredClone`d a Shot and reset `status` alone, so the copy arrived owning the
#: original's take — it played that take in the Monitor, offered it in the takes strip, and read
#: as approved where the original was. A brand-new Shot has rendered nothing and must not claim
#: otherwise; and a copy carrying `prompt_id`/`latest_output`/`approved_output` also reads as
#: `shot_render_provenance`, so the automated writers refuse to touch it for a render nobody ran.
#:
#: The three sets below partition `Shot.model_fields` exactly (with `id`, which is minted fresh),
#: and `tests/test_frontend_contract.py` asserts that partition against the model itself: a field
#: added to `Shot` and classified by nobody fails the suite instead of riding into every copy.
#: This is the one classification; the client mirrors `SHOT_PLAN_CONTENT_FIELDS` to do the copy
#: and the same test holds the two lists identical.
SHOT_PLAN_CONTENT_FIELDS = frozenset(
    {
        "start",
        "duration",
        "prompt",
        "h3_prompt",
        "h3_prompt_map",
        "mode",
        "asset_ids",
        "citations",
        "reference_labels",
        "singing",
        "use_song_audio",
        "seed",
    }
)

#: What names a take, a render, or a decision about one — never inherited.
#:
#: `status` is here because every value but the default describes a render; a new Shot is a
#: `draft`, which is what `Shot.status`'s own default already says. `trim_nudge` and
#: `latest_take_lead` select a slice of one specific file, and on a Shot with no file the pair
#: would silently cut a future take at an offset nobody chose. `latest_take_start` and
#: `latest_take_duration` are that same file's window snapshot — a copy would claim to hold a
#: take rendered for a window it has never rendered anything for. `mix_take_audio` accepts *this
#: take's* audio into the mix. `latest_review` is a vision report about a take that is not the
#: copy's. `flagged` is AD-5's re-render mark — "a shot whose take fell short" — and inheriting
#: it enlarges the flagged batch scope, which is GPU minutes spent on a shot nobody flagged.
SHOT_TAKE_PROVENANCE_FIELDS = frozenset(
    {
        "status",
        "prompt_id",
        "latest_output",
        "latest_review",
        "approved_output",
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

#: Neither plan nor provenance: a hands-off the Director placed on *one* Shot.
#:
#: `shot_write_refusal` already keeps these apart — "a lock is a decision the Director made and
#: provenance is a fact about media" — so `locked` is classified as what it is rather than filed
#: under a heading that does not fit it. It is not inherited either: the copy exists to be worked
#: on, and one that arrived locked would be refused by the sweeps, fills and re-renders the
#: Director reaches for next, giving a reason they never set on it. Unlocked is the cheap,
#: visible state; an inherited lock is invisible protection nobody asked for.
SHOT_UNINHERITED_DECISION_FIELDS = frozenset({"locked"})


def citations_in_role(shot: Shot, role: AssetRole) -> list[AssetCitation]:
    """This Shot's citations in one role, ordered.

    Sorted by `AssetCitation.order` with a **stable** sort, so citations that share an order — the
    default, and what every migrated Shot has — keep their list position. FR-19's determinism
    survives a role being added to a list that never had one.
    """
    return sorted(
        (citation for citation in shot.citations if citation.role == role),
        key=lambda citation: citation.order,
    )


def citations_in_prompt_order(shot: Shot) -> list[AssetCitation]:
    """Every citation this Shot holds, in the one order a prompt may number them.

    **This is the single definition of that order**, and every prompt channel reads it through
    `numbered_references` below, which is the single definition of the *numbering* laid over it:
    the reference render numbers its tags and appends its media in this walk, and
    `timeline.shot_expansion_input` hands the expansion specialist the tags that walk assigned.
    They must agree byte for byte, because the tag a prompt declares as "the first frame" points
    at whichever anonymous media slot holds the same number — a numbering that drifted between the
    two would render, plausibly, with the wrong picture pinned.

    Keyed on `(role, order)` — exactly the sort `shot_expansion_input` has always used — with a
    **stable** sort, so a reference-only Shot's sequence is `citations_in_role(shot,
    "reference")`'s sequence exactly: same key once every role compares equal, same tie-break.
    That equality is what keeps every pre-keyframe references payload byte-identical. Roles sort
    alphabetically, which puts `first` before `last` before `reference`; the order is arbitrary
    but it is *this* one, everywhere, forever.
    """
    return sorted(shot.citations, key=lambda citation: (citation.role, citation.order))


#: The citation roles whose tag names a concrete *frame*, and which therefore number into the
#: Picture series whatever the cited Asset turns out to be. `app.REFERENCE_MAP_ROLE_TAGS` writes
#: the sentence for each of these, and the submit route refuses a video or an audio cited in one
#: of them (`REFERENCE_KEYFRAME_NOT_IMAGE`) before anything is numbered — so a keyframe slot is a
#: picture slot by construction. A test pins this set equal to that mapping's keys, because a role
#: added to one and not the other would number a frame into a series no payload fills.
KEYFRAME_TAG_ROLES = frozenset({"first", "last"})


@dataclass(frozen=True, slots=True)
class NumberedReference:
    """One of a Shot's citations, carrying the tag the render will wire it into.

    `asset` is `None` for a citation whose Asset this project does not hold. Such a citation is
    still *numbered* — the render refuses it by name and never reaches a payload, but the two
    prompt surfaces have to agree about what the surviving tags are called, and a walk that
    silently dropped it would number them one way here and another way there.
    """

    citation: AssetCitation
    asset: Asset | None
    kind: str
    number: int

    @property
    def tag(self) -> str:
        """`<Picture 2>`, `<Video 1>`, `<Audio 3>` — what a prompt names this slot by."""
        return f"<{REFERENCE_TAG_NAMES[self.kind]} {self.number}>"


def citation_slot_kind(citation: AssetCitation, asset: Asset | None) -> str:
    """Which of H3's three media series one citation is wired into: picture, video or audio.

    Factored out of `numbered_references` rather than written twice, because the per-kind
    *ceilings* (`workflows.H3_REFERENCE_LIMITS`) are counted against the same classification the
    numbering uses. A budget counted one way and wired another is how a shot ends up with a
    tenth picture nobody counted.

    The rule is unchanged and stated in `numbered_references`: a keyframe role names a frame and
    a frame is a picture; a video or audio Asset is itself; **everything else is a picture** —
    `character`, `setting`, `prop`, `style` and `image` alike — and so is a citation whose Asset
    this project no longer holds, because nothing is known about it.
    """
    if citation.role in KEYFRAME_TAG_ROLES or asset is None:
        return "picture"
    if asset.kind in ("video", "audio"):
        return asset.kind
    return "picture"


def numbered_references(project: Project, shot: Shot) -> list[NumberedReference]:
    """**The** reference numbering. Every citation this Shot holds, tagged as the payload wires it.

    One rule, read by every surface that names a slot: `app.reference_map_tag_lines`, the submit
    route's own walk, `app.reference_slot_counts` and `timeline.shot_expansion_input`. It is one
    function because it was four, and two of them drifted: the expansion input numbered *every*
    citation into the `<Picture N>` series while the route numbered per kind, so a shot citing a
    video handed its specialist `<Picture 2>` for a slot the payload wires as `<Video 1>` — a
    prompt naming a slot that does not hold what it claims, rendered plausibly and wrongly because
    H3's media slots are anonymous (found 2026-08-20, fixed by deleting the second implementation).

    Three counters, never one. H3 wires pictures, videos and audios into three separate per-kind
    slot lists, so `<Picture 2>` and `<Video 2>` are different slots and neither is "the second
    reference" — see `h3_prompt.REFERENCE_TAG_NAMES`.

    The walk is `citations_in_prompt_order`, which is the single definition of the order a prompt
    may number citations in, and the same walk the payload appends its media by. Kind is the
    route's own classification: a keyframe role is a picture (see `KEYFRAME_TAG_ROLES`), then
    videos and audios are themselves, and **everything else is a picture** — `character`,
    `setting`, `prop`, `style` and `image` Assets all travel as pictures, and so does a citation
    whose Asset is missing, because nothing is known about it and the picture series is where this
    builder has always put it.
    """
    held = {asset.id: asset for asset in project.assets}
    numbers = dict.fromkeys(REFERENCE_TAG_NAMES, 0)
    numbered: list[NumberedReference] = []
    for citation in citations_in_prompt_order(shot):
        asset = held.get(citation.asset_id)
        kind = citation_slot_kind(citation, asset)
        numbers[kind] += 1
        numbered.append(
            NumberedReference(citation=citation, asset=asset, kind=kind, number=numbers[kind])
        )
    return numbered


def song_audio_tag(project: Project, shot: Shot) -> int:
    """The `<Audio N>` number the master song holds in this Shot's reference payload.

    The render appends the song *after* every cited asset, so its number is one past the
    audio assets the Shot cites — today always 1, since Shots cite only pictures, but
    counted rather than assumed so a future audio citation cannot silently shift the tag
    the expansion and the normalized `non_diegetic_music` field both point at. Counted off
    `numbered_references`, so "which citations are audios" is decided once, in the same
    place the cited audios get their own numbers: one numbering, everywhere.
    """
    return sum(1 for entry in numbered_references(project, shot) if entry.kind == "audio") + 1


def reference_slot_totals(project: Project, shot: Shot) -> dict[str, int]:
    """How many slots of each kind this Shot's render wires, master song included.

    The counts `h3_prompt.check_reference_bounds` bounds a prompt against, and they are taken
    from the numbering itself rather than counted a second time: the bound is "`<Picture N>` is
    valid exactly when `N <= totals['picture']`", so the total has to *be* the highest number the
    walk assigned or the check would be bounding against a different rule than the one that wrote
    the tags. The master song's slot is `song_audio_tag`'s number for the same reason.
    """
    totals = dict.fromkeys(REFERENCE_TAG_NAMES, 0)
    for entry in numbered_references(project, shot):
        totals[entry.kind] = max(totals[entry.kind], entry.number)
    if shot.use_song_audio:
        totals["audio"] = song_audio_tag(project, shot)
    return totals


def resolve_shot_mode(shot: Shot) -> ShotMode:
    """The mode this Shot renders as: its declaration, or the mode it already behaves as.

    The inference is the branch `generate_h3` used to make inline — assets or the master song mean
    the reference graph, nothing means the text-only one — kept here as the *fallback* rather than
    as the rule. That is the whole migration: a Shot saved before modes were declarable is
    unchanged, because the answer it gets is the answer it was already getting.

    A declaration wins over the attachments, including when they disagree. That is the point of
    declaring: what renders is what the Shot says it is, not what its attachments imply, and the
    disagreement is reported by `mode_specification_problems` rather than resolved silently here.
    """
    if shot.mode is not None:
        return shot.mode
    return "references" if shot.citations or shot.use_song_audio else "text_to_video"


def mode_specification_problems(shot: Shot) -> list[str]:
    """Everything this Shot is missing or carrying wrongly for its mode, named. `[]` when it fits.

    Every sentence is derived from `SHOT_MODE_SPECS`, so a new mode is a row in that table and not
    a wording written here. Reported rather than repaired: a first/middle/last Shot missing its
    middle image is a thing for the Director to fix, and inventing which of the two remaining
    images is the middle one is exactly the guess a role exists to stop.
    """
    spec = SHOT_MODE_SPECS[resolve_shot_mode(shot)]
    counted = Counter(citation.role for citation in shot.citations)
    problems: list[str] = []
    for requirement in spec.roles:
        held = counted.get(requirement.role, 0)
        label = ASSET_ROLE_LABELS[requirement.role]
        if held < requirement.minimum:
            problems.append(
                f"{spec.label} needs {requirement.minimum} {label}, and this shot cites {held}."
            )
        elif held > requirement.maximum:
            problems.append(
                f"{spec.label} takes at most {requirement.maximum} {label}, and this shot "
                f"cites {held}."
            )
    declared = {requirement.role for requirement in spec.roles}
    for role, label in ASSET_ROLE_LABELS.items():
        if role in declared or not counted.get(role):
            continue
        problems.append(
            f"{spec.label} has no {label} role, and this shot cites {counted[role]}."
        )
    if shot.use_song_audio and not spec.song_audio:
        problems.append(
            f"{spec.label} has no slot for the master song, so the audio reference this shot "
            f"asks for would not be sent."
        )
    return problems


def shot_label(project: Project, shot: Shot) -> str:
    """Name a Shot the way the Director sees it on the timeline, plus its id.

    Lives here rather than in `batch.py`, where it was written, because three modules now need it
    and the dependency graph forbids two of them from reaching that one: `batch` may import
    `timeline` and never the reverse, and `timeline.assistant_input` names Shots to the model under
    exactly the name the reply's notices then use. A second spelling of the same scheme is how the
    model is told about `SHOT 03` and the Director is told about a different one. `batch.shot_label`
    re-exports this, so every existing importer is unaffected.

    The clip is drawn as `SHOT 01` from the Shot's position in the **manifest** — that is what
    `renderTimeline` numbers by — while the readiness report is ordered by position in the song. The
    two orderings differ for any plan whose manifest order is not its time order, so a refusal that
    carried only a number would point at the wrong clip; one that carried only the id would name
    something that appears nowhere on screen. Both, therefore, exactly as `expansion_shot_label`
    carries both for the same reason.

    A Shot that is not in this project falls back to its bare id rather than claiming a position
    it does not have.
    """
    position = next(
        (index + 1 for index, item in enumerate(project.shots) if item.id == shot.id), 0
    )
    return f"SHOT {position:02d} ({shot.id})" if position else shot.id


def dangling_citations(project: Project, shot: Shot) -> list[str]:
    """The Asset ids this Shot cites that the project's library no longer holds, in shot order.

    Reported, never silently dropped. A citation whose Asset was deleted is the Director's decision
    to make — re-point it, or drop it — and a plan that quietly renders without it is a plan that
    rendered something nobody asked for.
    """
    library = {asset.id for asset in project.assets}
    return [
        citation.asset_id for citation in shot.citations if citation.asset_id not in library
    ]


#: The `Asset.source` values that mark a **promoted identity sheet**: a child Asset generated from
#: another Asset for the sole purpose of being that subject's multi-view reference.
#:
#: A set rather than an `== "krea-multiview"`, so a second turnaround workflow is one name added
#: here instead of a branch added at every reader. Membership is the *promotion* that made the
#: child, never its kind: `generate_multiview` deliberately gives the sheet its source's `kind`
#: (a ship's sheet is a prop), so kind cannot tell a sheet from the thing it depicts.
IDENTITY_SHEET_SOURCES = frozenset({"krea-multiview"})


def identity_sheet_ids(project: Project) -> dict[str, str]:
    """Map each Asset id to the promoted identity sheet that should be cited **in its place**.

    The defect this answers, measured on a live 30-shot plan (2026-08-20): the uploaded source
    frame was cited 22 times and the four-view sheet promoted from it 8, so most shots were
    conditioned on the weaker of the two references — the single frame — while the sheet that
    exists precisely to be the identity reference sat unused. The sheet won those 8 only because
    the model happened to type its display name into the prose.

    Absent from the map means *no substitution*, which is the whole no-op guarantee: a project
    with no promotions returns `{}` and every caller's output is byte-for-byte what it was.

    Two conditions, both necessary:

    * ``source`` is one of `IDENTITY_SHEET_SOURCES` and ``parent_id`` names the Asset it was
      promoted from. Nothing else in this application makes that promise about a child — an
      `edit_asset` result is a *different* picture of the subject, not another view of the same
      one — and the promotion route is where the promise is written down.
    * ``path`` is set. A sheet whose render has not landed (or never landed) is a row in the
      manifest with no file behind it, and substituting one would swap a real reference for
      nothing. Emptiness is the manifest's own statement here; a file deleted out from under a
      path is the render path's refusal to make, not this rule's — this module does no I/O.

    Chains resolve to their end. A sheet promoted from a sheet is still that subject's newest
    sheet, so citing either the source or the intermediate lands on the last one; a `parent_id`
    cycle (which nothing here can create, and a hand-edited manifest can) stops at the first
    repeat rather than spinning. The last promotion in manifest order wins when a source has
    several, because manifest order is append order and the newest sheet is the current one.
    """
    direct: dict[str, str] = {}
    for asset in project.assets:
        if asset.source in IDENTITY_SHEET_SOURCES and asset.parent_id and asset.path:
            direct[asset.parent_id] = asset.id
    resolved: dict[str, str] = {}
    for start in direct:
        seen = {start}
        current = start
        while (following := direct.get(current)) is not None and following not in seen:
            seen.add(following)
            current = following
        if current != start:
            resolved[start] = current
    return resolved


def citable_assets(project: Project) -> list[Asset]:
    """The library as a *model* should be offered it: identity sheets hidden behind their sources.

    Once `prefer_identity_sheets` makes citing a source mean citing its sheet, the sheet has no
    separate identity to offer — and offering it anyway is what produced the naming leak the
    Director reported: `"Close up on eyes of HarderFaster · multiview with flickering light"`, an
    internal asset label sitting in the creative prose of a shot, because citation correctness
    depended on the model typing that label. A name the model is never shown is a name it cannot
    write into a prompt.

    A sheet whose source the library no longer holds stays listed: it is then the only Asset for
    that subject, and hiding it would hide the subject.

    Byte-identity: a project with no promotions returns `project.assets` entry for entry, in
    order.
    """
    held = {asset.id for asset in project.assets}
    hidden = {
        asset.id
        for asset in project.assets
        if asset.source in IDENTITY_SHEET_SOURCES and asset.parent_id in held
    }
    return [asset for asset in project.assets if asset.id not in hidden]


def prefer_identity_sheets(
    citations: Sequence[AssetCitation], sheets: Mapping[str, str]
) -> list[AssetCitation]:
    """Re-point every `reference` citation at the identity sheet of what it cites.

    **Instead of the source, never in addition to it**, and that is a decision with a cost on both
    sides. H3 wires at most nine pictures (`workflows.H3_REFERENCE_LIMITS`); citing both spends two
    of them on one subject and conditions the render on two pictures of one face, which is the same
    subject argued twice rather than a subject and its context. Substitution can only *shrink* a
    citation list — a shot that named both the source and its sheet collapses to one — so no caller
    can exceed a slot budget by applying this that would not have exceeded it already.

    The cost it does not remove: `docs/ROADMAP.md` records a live artefact where a reference
    sheet's four-panel layout bled into the shot's composition ("the face plate is visible on the
    left, the vertical panel bands persist"), at 4 steps with ``ref_image_size="match"``, and that
    is **unresolved**. Preferring the sheet is therefore not free — it is the better identity
    reference and the one with a known composition risk, and the answer to that risk is a render
    parameter, not a citation policy. Every citation this writes stays visible in the inspector's
    cited-asset rows and removable per shot.

    Only the `reference` role is touched. `first`, `last` and `middle` name a concrete *frame* the
    render pins the picture to; substituting a four-panel contact sheet for a shot's first frame
    would make the first frame a contact sheet, which is the opposite of what the role asks for.

    Duplicates collapse, keeping the first occurrence's `order` — `order` sorts within a role and
    is not a dense index, so a gap left by a collapse changes no ordering. An empty `sheets`
    returns the same citations, field for field.
    """
    kept: list[AssetCitation] = []
    seen: set[tuple[str, str]] = set()
    for citation in citations:
        asset_id = (
            sheets.get(citation.asset_id, citation.asset_id)
            if citation.role == "reference"
            else citation.asset_id
        )
        key = (asset_id, citation.role)
        if key in seen:
            continue
        seen.add(key)
        kept.append(
            AssetCitation(asset_id=asset_id, role=citation.role, order=citation.order)
        )
    return kept


#: How short a display name has to be before the prose scan stops trusting it.
#:
#: Named rather than spelled `4` at the one call site it had, because the scan is now the
#: *fallback* half of a two-part rule and the number is the whole of what makes it safe: it is
#: a plain case-insensitive substring test, so a two-character name ("AJ") matches inside a
#: dozen ordinary words and a three-character one ("Bed") matches "Bedroom". Structural
#: citation has no such limit — an exact id or an exact name is exact at any length — which is
#: precisely why the fallback needs one and the primary does not.
NAME_SCAN_MIN_LENGTH = 4


def assets_for_proposal(
    library: Sequence[Asset], *, declared: Sequence[str] = (), prose: str = ""
) -> list[Asset]:
    """Which of `library` a planned shot cites: what it *declared*, then what its prose names.

    The Director's defect, measured on a live plan (2026-08-21): 24 of 30 shot prompts echoed
    an asset's display name, because the only way to attach a picture to a shot was to type
    that picture's label into the shot's creative writing. "Extreme close up of Crimson Lips
    Close-up while HarderFaster performs" is not a director's sentence, it is a manifest with
    verbs. `director.PlannedShot.assets` is the structural answer, and this is where the two
    halves meet.

    **Declared first, prose second, and the prose half is a fallback and never a primary.**
    That ordering is the decision worth defending. A model that names an asset in the prose
    and omits it from the field is a model whose *intent* is unambiguous, and dropping the
    citation would mean rendering a shot without the reference it asked for — a silent,
    permanent loss. An awkward sentence is a thing a human can read and fix; a missing
    reference is a thing nobody sees until the render comes back wrong. So losing a citation
    is the worse failure of the two, and the scan stays.

    Matching is by **id or exact display name** (case-insensitive, surrounding space
    stripped), because those are the two spellings the model was actually shown — the populate
    instruction's roster is by name, the project context dump beside it carries ids. An entry
    matching neither is dropped: it cannot invent an asset, and guessing which one a near-miss
    meant is how a shot ends up conditioned on the wrong face.

    Order is citation order, and it is deterministic: declared entries in the order the model
    listed them, then the un-declared prose matches in **library** order. Duplicates collapse
    to their first appearance, which is what stops a name that is both declared and written
    from being cited twice.

    Byte-identity, and it is the whole compatibility argument: with `declared` empty this
    returns exactly `[asset for asset in library if len(asset.name) >= NAME_SCAN_MIN_LENGTH
    and asset.name.lower() in prose.lower()]` — the same assets, in the same order, which
    become the same citations with the same `order` values populate has always written.
    """
    by_id = {asset.id: asset for asset in library}
    # First occurrence wins, so a library holding two assets under one display name resolves a
    # by-name entry to the earlier one rather than to whichever happened to be enumerated last.
    by_name: dict[str, Asset] = {}
    for asset in library:
        by_name.setdefault(asset.name.strip().casefold(), asset)
    chosen: list[Asset] = []
    seen: set[str] = set()
    for entry in declared:
        text = entry.strip()
        asset = by_id.get(text) or by_name.get(text.casefold())
        if asset is None or asset.id in seen:
            continue
        seen.add(asset.id)
        chosen.append(asset)
    lowered = prose.lower()
    for asset in library:
        if asset.id in seen:
            continue
        if len(asset.name) >= NAME_SCAN_MIN_LENGTH and asset.name.lower() in lowered:
            seen.add(asset.id)
            chosen.append(asset)
    return chosen


#: The recorded consequence of declaring a song instrumental, said once where the Director reads
#: it. `docs/ROADMAP.md` (stage 1 of the user workflow): "**instrumental songs lean on the
#: Treatment much harder** — environments/instruments carry the video when there is no singer to
#: associate." That is the whole of what the declaration means today, and saying it is what makes
#: the declaration coherent rather than decorative: there is no per-line tag to set, no character
#: slot to fill, and nothing for a singer reference to attach to.
#:
#: What it deliberately does **not** do is touch a single shot. Declaring instrumental does not
#: sweep `singing` to `not_singing`, because nothing in this codebase infers a singing state and a
#: declaration about the song is not a measurement of a window. The guard that *does* act on this
#: is the measured one and it is untouched: `Song.vocal_spans` is Whisper's own voice activity,
#: and a truly instrumental track measures voiceless everywhere, so populate already downgrades
#: every window of it through `shot_vocal_overlap` — by measurement, on the track, not on a label.
INSTRUMENTAL_NOTE = (
    "This song is declared instrumental, so no shot's words come from a singer and no character "
    "slot is needed. The treatment carries the whole story — environments and instruments have to "
    "do the work a performance would. Shots are untouched: whether a window is sung is still "
    "decided by what Whisper measured on the track, never by this declaration."
)

#: "Duet declared, one character slotted" — the Director's own example of the thing to flag.
VOCAL_CAST_SHORTFALL = (
    "{label} declared, and {held} of the {needed} character slot(s) it needs are filled — "
    "{missing} unfilled. A line tagged for a singer with no slotted character has no reference to "
    "reach, so give each singer's character asset a slot number in the Assets tab."
)


def character_slot_assets(project: Project) -> dict[int, Asset]:
    """Slot number → the character Asset holding it. Unslotted assets are absent, not `None`.

    **The single resolution of a `(S1)` mark**, so the populate check, the Assets tab and whatever
    pass 2 builds all answer "who is S1" the same way. Two conditions, both necessary and both the
    rule `default_setting_asset` already established for a pointer that must never resolve to a
    fabrication:

    * ``kind == "character"``. A slot names a singer; a prop with a slot number resolves nothing,
      and the route refuses to write one — this re-validates on read so a hand-edited manifest
      cannot smuggle one in either.
    * ``character_slot`` is non-zero. Zero is *unslotted*, which is what every existing asset is.

    The **first** asset in manifest order wins a contested slot. Nothing in this application can
    create a contest — the route refuses a slot another asset already holds — so this only decides
    a hand-edited manifest, and deciding it by append order is both stable and the same tie-break
    `assets_for_proposal` uses for a duplicated display name.
    """
    held: dict[int, Asset] = {}
    for asset in project.assets:
        if asset.kind == "character" and asset.character_slot:
            held.setdefault(asset.character_slot, asset)
    return held


def vocal_cast_problems(project: Project) -> list[str]:
    """What the declared vocal type needs and the library does not have. `[]` when it is satisfied.

    The Director's ask, verbatim (2026-08-21): "Assets that we have in our Character section would
    then also need to be linked to these character references (this is something that could be
    flagged if the user labeled the song as a duet but they only have one character asset if the
    user clicks Populate Timeline)."

    **Flagged, never refused.** A plan with an incomplete cast is still a plan worth laying out —
    the slots are set in another tab, and refusing the button would send the Director away from
    the thing they asked for to fix a thing that costs them nothing yet. It is reported at
    populate because that is the moment the Director named, and it is computed from the manifest
    alone: no model, no I/O, no clock.

    Silent for every project that has declared nothing, which is every project that existed before
    this feature — and silent for solo and choir by the *same* rule rather than by an exemption of
    their own: a type with no per-line marks names no slots, so nothing can be missing from a set
    that is empty. There is deliberately no early return for "this type needs no slots"; it would
    be a second spelling of what the `missing` loop already says, and a branch that only restates
    a later one is a branch that can drift from it. See `VocalTypeSpec` for why those types are
    untagged.

    Instrumental is the one type that says something while needing nothing, because "there is no
    singer" is a fact about the plan the Director should read once. See `INSTRUMENTAL_NOTE`.
    """
    if project.song is None:
        return []
    spec = VOCAL_TYPE_SPECS[project.song.vocal_type]
    if project.song.vocal_type == "instrumental":
        return [INSTRUMENTAL_NOTE]
    held = character_slot_assets(project)
    missing = [slot for slot in spec.slots if slot not in held]
    if not missing:
        return []
    return [
        VOCAL_CAST_SHORTFALL.format(
            label=spec.label,
            held=len(spec.slots) - len(missing),
            needed=len(spec.slots),
            missing=", ".join(f"S{slot}" for slot in missing),
        )
    ]


def default_setting_asset(project: Project) -> Asset | None:
    """The location this project declared, or `None`. **Never a guess.**

    `Project.default_setting_id` is set by one explicit route and by nothing else, so `None` here
    means the Director has not named a location — not that this function could not find one. It
    deliberately does not fall back to "the only setting Asset in the library": a project can hold
    several settings (the live one holds two), and picking one for the Director is exactly the
    invisible attachment this feature is not allowed to make.

    Re-validated on every read rather than trusted: the id must still name an Asset this project
    holds, that Asset must still be a `setting`, and it must still have a file. A pointer left
    dangling by a deletion, or aimed at a prop by a hand-edited manifest, is a no-op — this
    codebase refuses fabricated values, and a citation of an Asset that is not there is one.
    """
    if not project.default_setting_id:
        return None
    return next(
        (
            asset
            for asset in project.assets
            if asset.id == project.default_setting_id
            and asset.kind == "setting"
            and asset.path
        ),
        None,
    )


def with_default_setting(
    project: Project, citations: Sequence[AssetCitation], *, picture_limit: int
) -> list[AssetCitation]:
    """Give a shot the project's declared location when it has named no location of its own.

    The second half of the Director's report: on a 30-shot plan whose brief specifies a location,
    the setting Asset was cited 5 times, so 25 shots went to render with no environment reference
    at all. Naming the location was left entirely to whether the model spelled a display name.

    Three refusals to act, in order, and each is a no-op rather than a repair:

    * no declared location (`default_setting_asset` is `None`) — nothing is invented;
    * the shot already cites a `setting` Asset — adding a second location would put the shot in
      two places, and the one it named is the one it meant. Kind is checked rather than id, so a
      shot citing a *different* setting, or the identity sheet promoted from one (which inherits
      the setting kind), is left alone;
    * the picture budget is full. H3 wires `picture_limit` pictures and no more, counted through
      `citation_slot_kind` so the count is the numbering's own, and an over-budget shot keeps the
      citations it has rather than trading one away for the location.
    """
    setting = default_setting_asset(project)
    if setting is None:
        return list(citations)
    held = {asset.id: asset for asset in project.assets}
    pictures = 0
    for citation in citations:
        asset = held.get(citation.asset_id)
        if asset is not None and asset.kind == "setting":
            return list(citations)
        if citation_slot_kind(citation, asset) == "picture":
            pictures += 1
    if pictures >= picture_limit:
        return list(citations)
    return [
        *citations,
        AssetCitation(asset_id=setting.id, role="reference", order=len(citations)),
    ]


#: How much model-controlled output one notice keeps for inspection.
#:
#: Enforced by the model below rather than by the routes that build notices. The argument for
#: the cap is about *persistence* — the thread is written to the manifest and read back on every
#: load — and a rule argued in this file and applied in another is a rule the next writer, or a
#: hand-edited manifest, silently escapes. `ExpandedShot.prompt` has no upper bound at all, so
#: nothing outside this constraint bounds what a notice could carry.
NOTICE_RAW_LIMIT = 400

#: What a notice is *about*, which decides how it is rendered rather than only how it reads.
#:
#: Every notice used to look alike, so "Prompts written for 4 shot(s)" — the confirmation that
#: the thing the Director asked for happened — carried the same caution chrome as a refusal. A
#: warning that fires on the success path is one the Director stops reading, which is the exact
#: failure this story exists to fix rather than to reproduce.
#:
#: * `change` — something was written. Good news, and the thing to review.
#: * `refusal` — something was deliberately not written, and the notice says why.
#: * `flag` — it was written, or there was nothing to write, and something is worth a look.
NoticeKind = Literal["change", "refusal", "flag"]


class MessageNotice(BaseModel):
    """One thing a reply reports about itself, as data rather than as a text convention.

    `text` is the sentence the Director reads. It is *also* concatenated into
    `TreatmentMessage.content`, because that string is what every saved project already holds and
    what two client helpers still scan for markers — the notices are the structure the renderer
    splits by, not a replacement for the joined text. It is constrained non-empty for that
    reason: an empty sentence would contribute nothing to the joined tail the client strips,
    so every notice after it in the same reply would render twice.

    `raw` is the model output the notice is about, and it is the whole reason this model exists.
    The document rejection used to paste 400 characters of degraded output straight into
    `content`, and `director_chat` ships the thread back to the model as context on the next
    turn — so the guard that catches "JSON in context begets JSON" was the thing supplying it.
    `app.DIRECTOR_CONTEXT_EXCLUDE` drops every notice from that dump, which is what makes this a
    field the model never sees.

    `kind` deliberately has **no default**, against this file's usual rule. A default is what a
    new construction site inherits without deciding, and the whole defect being fixed is a notice
    wearing the wrong chrome — so forgetting it has to fail loudly at construction rather than
    quietly on screen. The manifest-compatibility argument the other defaults exist for does not
    apply: `notices` itself is new in this change, so no saved project carries a notice at all,
    let alone one without a kind.
    """

    kind: NoticeKind
    text: str = Field(min_length=1)
    raw: str = ""

    @field_validator("raw", mode="before")
    @classmethod
    def _bounded_raw(cls, value: object) -> object:
        """Cap the kept output, and store "nothing" as nothing.

        The cap does not collapse whitespace the way `app._short` does: the point of this field
        is to show what the model actually returned, and a reflowed blob is a different artefact
        from the one being inspected. It renders inside a disclosure of its own, where a newline
        costs nothing.

        Blank in means blank out, because a notice whose raw is `"   "` opens a disclosure onto
        an empty box — and the sentence that offers it would be claiming there is something to
        see. Both rejection wordings pick their final sentence off this field, so this is what
        makes that choice honest.
        """
        if not isinstance(value, str):
            return value
        if not value.strip():
            return ""
        return value if len(value) <= NOTICE_RAW_LIMIT else f"{value[:NOTICE_RAW_LIMIT]}…"


class TreatmentMessage(BaseModel):
    id: str = Field(default_factory=lambda: new_id("msg"))
    role: Literal["user", "assistant", "system"]
    content: str
    # Defaulted, like every other field added after the fact, so a manifest written before
    # notices existed loads unchanged and simply carries none.
    notices: list[MessageNotice] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now_utc)


class RenderJob(BaseModel):
    id: str = Field(default_factory=lambda: new_id("job"))
    # `edit` is the H3 image-edit (AI Mod) — an asset-producing GPU render like `flux`
    # and `multiview`, and adopted onto its target asset by the same completion writer.
    kind: Literal["music", "flux", "multiview", "edit", "h3", "ltx", "post"]
    # AD-5: a batch is the set of jobs sharing this id, active iff any member is
    # non-terminal — always derived, never stored as a status. Empty for every job
    # submitted outside a batch.
    batch_id: str = ""
    status: JobStatus = "queued"
    prompt_id: str = ""
    target_id: str = ""
    seed: int = 0
    output_files: list[str] = Field(default_factory=list)
    # FR-24 adapted for local work (AD-9): what this job consumed, so an assembly is
    # rebuildable from its record. Shot IDs paired with the take paths they contributed,
    # as `"<shot_id>=<approved_output>"` strings. Empty for every ComfyUI job — their
    # inputs are the submitted graph, which `prompt_id` already names.
    inputs: list[str] = Field(default_factory=list)
    error: str = ""
    #: Percent complete, 0-100, for the local work that can actually measure itself: assembly
    #: reads ffmpeg's own `-progress` clock and writes it here as the export runs (AD-9). A
    #: ComfyUI job never sets it — its progress lives on the ComfyUI queue, which the
    #: reconciler reads instead — so 0 on a `flux`/`h3` job means "not reported", not "not
    #: started". Defaulted, so every manifest written before this field existed loads
    #: unchanged and round-trips through `store.py` with one more key and no other change.
    progress: int = Field(default=0, ge=0, le=100)
    #: Consecutive reconcile ticks on which ComfyUI knew nothing about this job's prompt —
    #: absent from the queue AND from history. Reset to 0 the moment either answers. At
    #: `batch.MISSING_TICKS_LIMIT` the reconciler settles the job as its prompt having died
    #: with the queue (a crash, a restart, or a hand-cleared queue), which is what ended the
    #: stuck-"queued"-forever jobs met three times live on 2026-08-19/20. A small counter
    #: rather than an immediate verdict because one absent tick is also what the seconds of
    #: a ComfyUI restart look like, and inventing an error there would be a lie.
    missing_ticks: int = 0
    #: The id of the job that replaced this one for the same target, or "" — which is what
    #: every job carries and what every manifest written before this field existed loads as.
    #: Written only by `batch.supersede_target_jobs`, when a new render is accepted for a
    #: target that still had an unsettled record: the leftover is settled as `cancelled` and
    #: this names its successor, so a settled-by-supersession job stays distinguishable from
    #: one the Director cancelled by hand and from one that failed. Nothing reads it for a
    #: decision — it is provenance — and its `prompt_id` is deliberately left in place, so the
    #: file an already-executing ComfyUI prompt still writes remains traceable to this record.
    superseded_by: str = ""
    #: How long this job took, in seconds, or `0.0` for one that has never settled — which is
    #: also what every manifest written before this field existed loads as, and what a job
    #: settled by a build older than the instrumentation keeps forever.
    #:
    #: **This application had no render timings at all until 2026-08-21.** `updated_at` was set
    #: by its `default_factory` and never written again, so every settled job in the Director's
    #: live manifest carried `updated_at == created_at` to the microsecond, and the render costs
    #: that justify `app.POPULATE_MAX_WINDOW_SECONDS` had to be reconstructed by hand from the
    #: mtimes of ComfyUI's output files. This field, `render_seconds_source` and `render_frames`
    #: exist so that reconstruction never has to happen again.
    #:
    #: Written **once**, by the settle path that ended the job (`batch.stamp_job_settled`), and
    #: never re-measured: a job that settles is terminal, and a second stamp could only be a
    #: later clock overwriting the real one. For a `complete` job it is how long the render took;
    #: for a `cancelled` or superseded one it is how long the record stood open before it was
    #: closed, which is **not** a render time. Read `status` before quoting it as one.
    render_seconds: float = Field(default=0.0, ge=0.0)
    #: Where `render_seconds` was measured, and therefore what it *includes*:
    #:
    #: * ``""`` — not measured. Never settled, or settled by a build older than this field.
    #: * ``"comfy"`` — ComfyUI's own execution clock, read out of `/history`'s
    #:   ``status.messages``: ``execution_start`` to ``execution_success``/``execution_error``.
    #:   This is the render **alone**; queue wait is excluded, so it is the number to compare
    #:   against another render's.
    #: * ``"record"`` — this record's `created_at` to its settle. `created_at` is **enqueue**
    #:   time, not start time, so for a job that waited behind others in a batch this is queue
    #:   wait *plus* render and is only an **upper bound** on the render. Every surface that
    #:   shows it must say so; `batch.render_timing_summary` is the one that does.
    #:
    #: A plain `str` rather than a `Literal` on `superseded_by`'s precedent: a manifest carrying
    #: a value some future build wrote must still load, and the closed set is enforced by there
    #: being exactly one writer, not by the schema refusing to read.
    render_seconds_source: str = ""
    #: The frames the graph this job submitted was built for, or `0` where the job's kind has no
    #: single frame count this application computes (every kind but `h3` today).
    #:
    #: Persisted rather than re-derived because it cannot be re-derived: `over_render_frames`
    #: reads `Shot.duration`, and the Director edits a window after the render — dragging a
    #: clip's edge, snapping a cut — so a duration read later describes a different render.
    #: Without this, a recorded duration is uninterpretable, which is half of why the mtime
    #: reconstruction was needed at all.
    #:
    #: **Server-owned.** `POST .../shots/{id}/render` is its one writer, and the generic
    #: full-project `PUT` re-adopts it from the store (`app._adopt_job_measurements`) rather than
    #: trusting a body — a defaulted `int` that any pre-existing client omits arrives as `0`, and
    #: one ordinary save would otherwise erase the frame count off every job at once. That hole
    #: has been found in that route seven times; these three fields are not the eighth.
    render_frames: int = Field(default=0, ge=0)
    #: Enqueue time — when the *record* was created, which for a ComfyUI job is a moment before
    #: its graph was submitted, not a moment when the GPU started work. See `render_seconds`.
    created_at: datetime = Field(default_factory=now_utc)
    #: When this job **settled**, and only that. Written by every settle path — completion,
    #: failure, cancellation, supersession, and the `missing_ticks` death — and deliberately
    #: **not** by a progress tick or a queued→running transition: assembly writes `progress` up
    #: to a hundred times per export, and if those moved this stamp then `updated_at -
    #: created_at` would stop being a duration. Equal to `created_at` means "never settled", or
    #: "settled before this application recorded anything", which is every job written before
    #: 2026-08-21.
    updated_at: datetime = Field(default_factory=now_utc)


class Project(BaseModel):
    id: str = Field(default_factory=lambda: new_id("project"))
    name: str
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)
    creative_brief: str = ""
    treatment: str = ""
    style_bible: str = ""
    # Single-slot recovery per AD-14: the one value an applied Director replacement
    # overwrote, and nothing older. A rejected or locked candidate leaves these alone —
    # capturing on attempt rather than on apply would let a refused candidate destroy the
    # only copy of the document the refusal exists to protect.
    treatment_previous: str = ""
    style_bible_previous: str = ""
    # Per-document locks, mirroring `Shot.locked`: the Director's "do not touch this" for a
    # creative document. Every field here is defaulted so manifests written before this
    # existed load unchanged.
    treatment_locked: bool = False
    style_bible_locked: bool = False
    song: Song | None = None
    # The song's structure, the Director's own marks (see `SongSection`). Defaulted so
    # every existing manifest loads unchanged; empty means unmarked, and everything that
    # reads sections treats absence as unknown rather than inventing boundaries.
    sections: list[SongSection] = Field(default_factory=list)
    #: The video's location, as the Director's own declaration: the id of one `setting` Asset in
    #: `assets`, or `""` for none. Read by `default_setting_asset`, which re-validates it on every
    #: read, so a dangling or mis-aimed pointer is a no-op rather than a fabricated citation.
    #:
    #: **Server-owned, and set by `PUT /projects/{id}/default-setting` alone**, exactly as
    #: `Asset.consistency_prompt` is — the generic full-project `PUT` re-adopts the stored value
    #: rather than trusting a body, because a defaulted `str` that any pre-existing client simply
    #: omits arrives as `""`, and one ordinary save would otherwise clear the Director's choice.
    #: That hole has been found in that route three times; this field is not the fourth.
    #:
    #: Defaulted, so every manifest written before it existed loads unchanged and means *no
    #: location declared* — which is a real state, and the state in which every consumer must
    #: produce byte-identical output to what it produced before this field existed.
    default_setting_id: str = ""
    assets: list[Asset] = Field(default_factory=list)
    shots: list[Shot] = Field(default_factory=list)
    messages: list[TreatmentMessage] = Field(default_factory=list)
    jobs: list[RenderJob] = Field(default_factory=list)
