from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, computed_field

SongSource = Literal["imported", "generated"]
AssetKind = Literal["character", "setting", "prop", "style", "image", "audio", "video"]
ShotStatus = Literal["draft", "ready", "queued", "running", "complete", "error", "approved"]
JobStatus = Literal["queued", "running", "complete", "error", "cancelled"]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def now_utc() -> datetime:
    return datetime.now(UTC)


class Song(BaseModel):
    title: str
    source: SongSource
    path: str = ""
    duration: float = Field(default=0, ge=0)
    lyrics: str = ""
    caption: str = ""
    prompt_id: str = ""


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


class TreatmentMessage(BaseModel):
    id: str = Field(default_factory=lambda: new_id("msg"))
    role: Literal["user", "assistant", "system"]
    content: str
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
    song: Song | None = None
    assets: list[Asset] = Field(default_factory=list)
    shots: list[Shot] = Field(default_factory=list)
    messages: list[TreatmentMessage] = Field(default_factory=list)
    jobs: list[RenderJob] = Field(default_factory=list)
