from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

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
)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class MusicRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    caption: str = Field(min_length=1)
    lyrics: str = ""
    duration: float = Field(default=120, ge=4, le=360)
    seed: int = Field(default=0, ge=0)


class FluxRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    kind: Literal["character", "setting", "prop", "style", "image"] = "image"
    prompt: str = Field(min_length=1)
    width: int = Field(default=1024, ge=256, le=2048, multiple_of=16)
    height: int = Field(default=1024, ge=256, le=2048, multiple_of=16)
    steps: int = Field(default=20, ge=1, le=100)
    guidance: float = Field(default=4, ge=0, le=20)
    seed: int = Field(default=0, ge=0)


class MultiviewRequest(BaseModel):
    prompt: str = Field(min_length=1)
    seed: int = Field(default=0, ge=0)


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


class ShotListRequest(BaseModel):
    shots: list[Shot]


class ProjectDocumentsRequest(BaseModel):
    creative_brief: str = ""
    treatment: str = ""
    style_bible: str = ""


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
    def replace_project(project_id: str, project: Project) -> Project:
        current = get_project(project_id)
        if project.id != project_id:
            raise HTTPException(status_code=422, detail="Project ID cannot be changed")
        if project.updated_at != current.updated_at:
            raise HTTPException(
                status_code=409,
                detail="Project changed since it was loaded; refresh before replacing it",
            )
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
        return store.save(project)

    @app.post("/api/projects/{project_id}/songs/upload", response_model=Project)
    async def upload_song(
        project_id: str,
        file: Annotated[UploadFile, File()],
        title: Annotated[str, Form()],
        duration: Annotated[float, Form()] = 0,
    ) -> Project:
        project = get_project(project_id)
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in {".wav", ".mp3", ".flac"}:
            raise HTTPException(status_code=415, detail="Song must be WAV, MP3, or FLAC")
        songs_dir = store.media_dir(project_id) / "songs"
        songs_dir.mkdir(parents=True, exist_ok=True)
        filename = _safe_filename(file.filename or f"song{suffix}")
        target = songs_dir / filename
        _copy_upload(file, target, settings.max_upload_bytes)
        resolved_duration = duration if duration > 0 else _media_duration(target)
        project.song = Song(
            title=title.strip() or target.stem,
            source="imported",
            path=target.relative_to(store.project_dir(project_id)).as_posix(),
            duration=resolved_duration,
        )
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
        project = get_project(project_id)
        project.messages.append(TreatmentMessage(role="user", content=request.message))
        context = project.model_dump(
            mode="json",
            exclude={"jobs": True, "messages": {"__all__": {"id", "created_at"}}},
        )
        try:
            result = await director.plan(message=request.message, project_context=context)
        except DirectorUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except DirectorError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        notices: list[str] = []
        for label, candidate, existing in (
            ("Treatment", result.treatment, project.treatment),
            ("Style bible", result.style_bible, project.style_bible),
        ):
            reason = document_rejection(candidate, existing)
            if reason:
                notices.append(f"{label} was NOT replaced: {reason}. Raw output: {candidate[:400]}")
            elif label == "Treatment":
                project.treatment = candidate
            else:
                project.style_bible = candidate
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
                elif job.kind == "music" and project.song and job.output_files:
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
