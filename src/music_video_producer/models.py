from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, computed_field, field_validator

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


class Shot(BaseModel):
    id: str = Field(default_factory=lambda: new_id("shot"))
    start: float = Field(ge=0)
    duration: float = Field(gt=0)
    prompt: str = ""
    mode: Literal["text", "image", "reference"] = "reference"
    asset_ids: list[str] = Field(default_factory=list)
    reference_labels: dict[str, str] = Field(default_factory=dict)
    use_song_audio: bool = False
    seed: int = Field(default=0, ge=0)
    status: ShotStatus = "draft"
    prompt_id: str = ""
    latest_output: str = ""
    latest_review: VisionInspectionRecord | None = None
    approved_output: str = ""
    locked: bool = False

    @computed_field
    @property
    def end(self) -> float:
        return self.start + self.duration


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
    kind: Literal["music", "flux", "multiview", "h3", "ltx", "post"]
    status: JobStatus = "queued"
    prompt_id: str = ""
    target_id: str = ""
    seed: int = 0
    output_files: list[str] = Field(default_factory=list)
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
    assets: list[Asset] = Field(default_factory=list)
    shots: list[Shot] = Field(default_factory=list)
    messages: list[TreatmentMessage] = Field(default_factory=list)
    jobs: list[RenderJob] = Field(default_factory=list)
