from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

SongSource = Literal["imported", "generated"]
AssetKind = Literal["character", "setting", "prop", "style", "image", "audio", "video"]
ShotStatus = Literal["draft", "ready", "queued", "running", "complete", "error", "approved"]
JobStatus = Literal["queued", "running", "complete", "error", "cancelled"]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def now_utc() -> datetime:
    return datetime.now(UTC)


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

    **This is the single definition of that order**, and both prompt channels read it: the
    reference render numbers its `<Picture N>` tags and appends its media in this walk, and
    `timeline.shot_expansion_input` hands the expansion specialist the same tags from the same
    walk. The two must agree byte for byte, because the tag a prompt declares as "the first
    frame" points at whichever anonymous media slot holds the same number — a numbering that
    drifted between the two would render, plausibly, with the wrong picture pinned.

    Keyed on `(role, order)` — exactly the sort `shot_expansion_input` has always used — with a
    **stable** sort, so a reference-only Shot's sequence is `citations_in_role(shot,
    "reference")`'s sequence exactly: same key once every role compares equal, same tie-break.
    That equality is what keeps every pre-keyframe references payload byte-identical. Roles sort
    alphabetically, which puts `first` before `last` before `reference`; the order is arbitrary
    but it is *this* one, everywhere, forever.
    """
    return sorted(shot.citations, key=lambda citation: (citation.role, citation.order))


def song_audio_tag(project: Project, shot: Shot) -> int:
    """The `<Audio N>` number the master song holds in this Shot's reference payload.

    The render appends the song *after* every cited asset, so its number is one past the
    audio assets the Shot cites — today always 1, since Shots cite only pictures, but
    counted rather than assumed so a future audio citation cannot silently shift the tag
    the expansion and the normalized `non_diegetic_music` field both point at. Same walk
    as `citations_in_prompt_order`, same reason: one numbering, everywhere.
    """
    assets = {asset.id: asset.kind for asset in project.assets}
    cited_audio = sum(
        1
        for citation in citations_in_prompt_order(shot)
        if assets.get(citation.asset_id) == "audio"
    )
    return cited_audio + 1


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
    created_at: datetime = Field(default_factory=now_utc)
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
    assets: list[Asset] = Field(default_factory=list)
    shots: list[Shot] = Field(default_factory=list)
    messages: list[TreatmentMessage] = Field(default_factory=list)
    jobs: list[RenderJob] = Field(default_factory=list)
