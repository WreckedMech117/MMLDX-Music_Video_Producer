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
from pydantic import BaseModel, Field, StringConstraints

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
    "messages": {"__all__": {"id", "created_at"}},
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


class H3Request(BaseModel):
    width: int = Field(default=1344, ge=256, le=2048, multiple_of=32)
    height: int = Field(default=768, ge=256, le=2048, multiple_of=32)
    steps: int = Field(default=20, ge=1, le=100)
    ref_image_size: Literal["match", "max"] = "match"


class DirectorRequest(BaseModel):
    message: str = Field(min_length=1)
    apply_shots: bool = False
    # Per-turn consent to replace the creative documents, mirroring `apply_shots` exactly —
    # same shape, same default, and independent of it. Off by default because consent has to be
    # explicit for the turn being sent: asking "what do you think of this idea?" must not
    # rewrite the Treatment, which is what every reply did before this field existed. It is
    # deliberately not stored on `Project`, so it is neither remembered across turns nor
    # inherited by another project, and a client that omits it entirely is a decline.
    apply_documents: bool = False


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

    @app.post("/api/projects/{project_id}/timeline/compile")
    def compile_timeline(project_id: str, request: TimelineRequest) -> dict[str, Any]:
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
        return {
            "timeline_data": result.timeline_data,
            "requested_frames": result.requested_frames,
            "aligned_frames": result.aligned_frames,
            "warnings": result.warnings,
        }

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
        notices: list[str] = []
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
                    notices.append(DOCUMENT_LOCK_NOTICE.format(document=label))
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
                notices.append(f"{label} was NOT replaced: {reason}. Raw output: {candidate[:400]}")
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
            notices.insert(0, document_first_draft_notice(first_drafts))
        if replaced:
            notices.insert(0, document_change_notice(replaced))
        # One grouped statement rather than one per document: a declined turn wrote nothing, so
        # the Director needs the list and the reason once, not the same paragraph twice.
        if not_requested:
            notices.append(document_not_requested_notice(not_requested))
        if request.apply_shots and not result.shots:
            notices.append(
                "No shot plan was applied: the model returned an empty shot list. "
                "Existing shots are unchanged."
            )
        for item in result.shots:
            if item.duration < H3_MIN_SHOT_SECONDS or item.duration > H3_MAX_SHOT_SECONDS:
                notices.append(
                    f"Proposed {item.duration:g}s shot at {item.start:g}s falls outside MiniMax "
                    f"H3's reliable {H3_MIN_SHOT_SECONDS:g}-{H3_MAX_SHOT_SECONDS:g}s window; "
                    "split or trim it before rendering."
                )
        message = result.message
        if notices:
            message = message + "\n\n---\n" + "\n\n".join(notices)
        project.messages.append(TreatmentMessage(role="assistant", content=message))
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
