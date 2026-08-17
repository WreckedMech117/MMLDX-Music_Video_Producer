import json
import subprocess
import wave
from io import BytesIO
from pathlib import Path
from typing import get_args

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from music_video_producer.app import (
    APPLY_DOCUMENTS_LABEL,
    CHAT_EMPTY_MESSAGE,
    DIRECTOR_CONTEXT_EXCLUDE,
    DOCUMENT_LABELS,
    DOCUMENT_LOCK_NOTICE,
    DOCUMENT_REJECTED_EMPTY_NOTICE,
    DOCUMENT_REJECTED_NOTICE,
    EXPANSION_REJECTED_EMPTY_NOTICE,
    SHOT_CLAIM_MISMATCH_NOTICE,
    SHOT_CLAIM_WITHOUT_ANY_SHOTS_NOTICE,
    SHOT_PLAN_EMPTY_NOTICE,
    SHOT_WINDOW_NOTICE,
    DirectorRequest,
    DocumentName,
    create_app,
    document_change_notice,
    document_first_draft_notice,
    prose_claims_shots,
)
from music_video_producer.batch import readiness_refusal
from music_video_producer.comfy import ComfyError
from music_video_producer.config import Settings
from music_video_producer.director import (
    DirectorError,
    DirectorResult,
    DirectorUnavailable,
    ExpandedShot,
    PlannedShot,
    ShotExpansion,
)
from music_video_producer.models import (
    NOTICE_RAW_LIMIT,
    Asset,
    MessageNotice,
    NoticeKind,
    Project,
    Shot,
    Song,
    TreatmentMessage,
    VisionInspectionRecord,
)
from music_video_producer.store import ProjectStore
from music_video_producer.timeline import expansion_input


class FakeComfy:
    def __init__(self):
        self.prompts = []
        self.uploads = []
        self.history_error = False
        self.submit_error = False

    async def health(self):
        return {"online": True, "url": "http://fake"}

    async def submit(self, prompt, client_id=None):
        if self.submit_error:
            raise ComfyError("ComfyUI is unreachable")
        self.prompts.append(prompt)
        return type("Submission", (), {"prompt_id": "p-101", "number": 1})()

    async def history(self, prompt_id):
        if self.history_error:
            raise ComfyError("history unavailable")
        return type(
            "History",
            (),
            {"prompt_id": prompt_id, "status": "complete", "outputs": [], "error": ""},
        )()

    async def upload(self, filename, content, content_type):
        assert content
        self.uploads.append(content)
        return {"name": filename, "subfolder": "", "type": "input"}


class FakeDirector:
    async def plan(self, message, project_context):
        shot = type("PlannedShot", (), {"start": 0, "duration": 5, "prompt": "A widening corridor"})()
        return type(
            "DirectorResult",
            (),
            {
                "message": "I expanded the visual release.",
                "treatment": "Confinement opens into a vast performance space.",
                "style_bible": "Sodium amber and deep blacks.",
                "shots": [shot],
            },
        )()

    async def inspect_image(self, image, mime_type, purpose):
        assert image
        assert mime_type.startswith("image/")
        return type(
            "VisionInspection",
            (),
            {
                "model_dump": lambda self: {
                    "summary": "Two visible character views.",
                    "identity": ["silver jacket"],
                    "environment": ["neutral studio"],
                    "continuity_cues": ["keep silver jacket"],
                    "prompt_cues": ["front-lit portrait"],
                    "risks": [],
                }
            },
        )()


def make_client(tmp_path: Path, director=None):
    """The default client. `director` is optional so a test can keep the FakeComfy handle —
    `make_client_with_director` does not return one, and "no render was queued" is a claim that
    needs it."""
    settings = Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy")
    store = ProjectStore(tmp_path)
    comfy = FakeComfy()
    app = create_app(
        settings=settings, store=store, comfy=comfy, director=director or FakeDirector()
    )
    return TestClient(app), store, comfy


def test_project_lifecycle_and_song_upload(tmp_path: Path):
    client, store, _ = make_client(tmp_path)
    response = client.post("/api/projects", json={"name": "Signal Bloom"})
    assert response.status_code == 201
    project_id = response.json()["id"]

    upload = client.post(
        f"/api/projects/{project_id}/songs/upload",
        data={"title": "Signal Bloom", "duration": "12.25"},
        files={"file": ("signal.flac", b"fLaCfake", "audio/flac")},
    )

    assert upload.status_code == 200
    assert upload.json()["song"]["source"] == "imported"
    assert upload.json()["song"]["duration"] == 12.25
    assert store.get(project_id).song.path.endswith("signal.flac")
    assert client.get("/api/projects").json()[0]["name"] == "Signal Bloom"


def wav_bytes(seconds: float, rate: int = 8000) -> bytes:
    """A silent mono WAV of a known length — the smallest thing ffprobe can measure."""
    content = BytesIO()
    with wave.open(content, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(b"\0\0" * int(rate * seconds))
    return content.getvalue()


def test_song_upload_probes_duration_when_browser_does_not_supply_it(tmp_path: Path):
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Duration probe"))

    response = client.post(
        f"/api/projects/{project.id}/songs/upload",
        data={"title": "One second", "duration": "0"},
        files={"file": ("one-second.wav", wav_bytes(1.0), "audio/wav")},
    )

    assert response.status_code == 200
    assert 0.99 <= response.json()["song"]["duration"] <= 1.01


def test_probed_song_duration_survives_a_restart_and_replaces_the_previous_song(
    tmp_path: Path,
):
    """FR-12's actual claim: after a restart the probed duration is still there.

    The route test above only reads the response body, which is the in-memory object
    the handler just built. Re-reading through a *fresh* ProjectStore over the same
    data root is the only thing that proves the value reached the manifest — and the
    pre-existing 187.5 s song proves the new duration is the freshly probed one
    rather than the length the frontend used to carry over from the previous song.
    """
    client, store, _ = make_client(tmp_path)
    project = Project(name="Restart survival")
    project.song = Song(
        title="Previous song",
        source="imported",
        path="media/songs/previous.wav",
        duration=187.5,
    )
    store.create(project)

    response = client.post(
        f"/api/projects/{project.id}/songs/upload",
        data={"title": "Undecodable but probeable", "duration": "0"},
        files={"file": ("two-seconds.wav", wav_bytes(2.0), "audio/wav")},
    )

    assert response.status_code == 200
    probed = response.json()["song"]["duration"]
    assert 1.99 <= probed <= 2.01

    restarted = ProjectStore(tmp_path).get(project.id)
    assert restarted.song is not None
    assert restarted.song.duration == probed
    assert restarted.song.duration != 187.5
    assert restarted.song.title == "Undecodable but probeable"


def test_song_upload_stores_zero_when_ffprobe_cannot_run(tmp_path: Path, monkeypatch):
    """A missing ffprobe must cost the duration, not the import — and never invent one.

    `_media_duration` swallows the failure and returns 0.0 by contract; what matters
    here is that 0 is what gets persisted, because a fabricated number would silently
    become the timing spine of the whole production.
    """
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="No ffprobe"))
    real_run = subprocess.run

    def missing_ffprobe(command, *args, **kwargs):
        if command and command[0] == "ffprobe":
            raise FileNotFoundError(2, "No such file or directory", "ffprobe")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr("music_video_producer.app.subprocess.run", missing_ffprobe)

    response = client.post(
        f"/api/projects/{project.id}/songs/upload",
        data={"title": "Unmeasurable", "duration": "0"},
        files={"file": ("silent.wav", wav_bytes(1.0), "audio/wav")},
    )

    assert response.status_code == 200
    assert response.json()["song"]["duration"] == 0
    assert ProjectStore(tmp_path).get(project.id).song.duration == 0


def test_uploads_enforce_size_and_asset_type_limits(tmp_path: Path):
    settings = Settings(
        data_root=tmp_path,
        comfy_root=tmp_path / "comfy",
        max_upload_bytes=8,
    )
    store = ProjectStore(tmp_path)
    app = create_app(settings=settings, store=store, comfy=FakeComfy(), director=FakeDirector())
    client = TestClient(app)
    project = store.create(Project(name="Uploads"))

    too_large = client.post(
        f"/api/projects/{project.id}/assets/upload",
        data={"name": "Large", "kind": "image"},
        files={"file": ("large.png", b"123456789", "image/png")},
    )
    wrong_type = client.post(
        f"/api/projects/{project.id}/assets/upload",
        data={"name": "Wrong", "kind": "character"},
        files={"file": ("payload.html", b"<html>", "text/html")},
    )

    assert too_large.status_code == 413
    assert wrong_type.status_code == 415
    assert list(store.media_dir(project.id).rglob("*.*")) == []


def test_document_save_does_not_replace_other_project_state(tmp_path: Path):
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Documents"))
    project.assets.append(Asset(name="Lead", kind="character", path="media/assets/lead.png"))
    store.save(project)

    response = client.put(
        f"/api/projects/{project.id}/documents",
        json={
            "creative_brief": "New brief",
            "treatment": "New treatment",
            "style_bible": "New style",
        },
    )

    assert response.status_code == 200
    saved = store.get(project.id)
    assert saved.creative_brief == "New brief"
    assert saved.assets[0].name == "Lead"


def test_full_project_replace_rejects_stale_revision(tmp_path: Path):
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Revision"))
    stale_payload = project.model_dump(mode="json")
    client.put(
        f"/api/projects/{project.id}/documents",
        json={"creative_brief": "new", "treatment": "", "style_bible": ""},
    )

    response = client.put(f"/api/projects/{project.id}", json=stale_payload)

    assert response.status_code == 409
    assert store.get(project.id).creative_brief == "new"


def test_music_and_flux_generation_submit_real_payload_shapes(tmp_path: Path):
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Generator"))

    music = client.post(
        f"/api/projects/{project.id}/generate/music",
        json={"title": "Night Wire", "caption": "industrial synth rock", "lyrics": "[Verse]\nVoltage", "duration": 8, "seed": 9},
    )
    flux = client.post(
        f"/api/projects/{project.id}/generate/flux",
        json={"name": "Lead singer", "kind": "character", "prompt": "A singer under red light", "width": 1024, "height": 1024, "steps": 20, "guidance": 4, "seed": 10},
    )

    assert music.status_code == 202
    assert flux.status_code == 202
    assert comfy.prompts[0]["45"]["class_type"] == "MiniMaxMusic3TextEncode"
    assert comfy.prompts[1]["11"]["inputs"]["text"].startswith("A singer")
    saved = store.get(project.id)
    assert saved.song.source == "generated"
    assert {job.kind for job in saved.jobs} == {"music", "flux"}
    assert saved.assets[0].kind == "character"


def test_songplanner_generation_submits_planner_payload_and_records_job(tmp_path: Path):
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Planner"))

    response = client.post(
        f"/api/projects/{project.id}/generate/songplanner",
        json={
            "title": "Night Signal",
            "idea": "sunset synthwave with airy female vocals",
            "genre_hint": "synthwave",
            "duration": 90,
            "seed": 21,
        },
    )

    assert response.status_code == 202
    payload = comfy.prompts[-1]
    planner = next(node for node in payload.values() if node["class_type"] == "M3SongPlanner")
    assert planner["inputs"]["idea"].startswith("sunset synthwave")
    assert planner["inputs"]["genre_hint"] == "synthwave"
    saved = store.get(project.id)
    job = saved.jobs[-1]
    assert job.kind == "music"
    assert job.prompt_id == "p-101"
    assert job.seed == 21
    assert job.target_id == "song"
    assert saved.song.source == "generated"
    assert saved.song.prompt_id == "p-101"
    assert saved.song.title == "Night Signal"


def test_songplanner_known_lyrics_submits_supplied_lyrics_and_records_job(tmp_path: Path):
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Cover"))
    lyrics = "[Verse]\nStatic in the wires\n\n[Chorus]\nNight signal, night signal"

    response = client.post(
        f"/api/projects/{project.id}/generate/songplanner",
        json={
            "title": "Night Signal (Cover)",
            "idea": "faithful synthwave cover, airy female vocals",
            "genre_hint": "synthwave",
            "lyrics": lyrics,
            "duration": 90,
            "seed": 21,
        },
    )

    assert response.status_code == 202
    payload = comfy.prompts[-1]
    encoder = next(
        node for node in payload.values() if node["class_type"] == "MiniMaxMusic3TextEncode"
    )
    assert encoder["inputs"]["lyrics"] == lyrics
    planner = next(node for node in payload.values() if node["class_type"] == "M3SongPlanner")
    assert planner["inputs"]["idea"].startswith("faithful synthwave cover")
    assert planner["inputs"]["genre_hint"] == "synthwave"
    saved = store.get(project.id)
    job = saved.jobs[-1]
    assert job.kind == "music"
    assert job.prompt_id == "p-101"
    assert job.seed == 21
    assert job.target_id == "song"
    assert saved.song.lyrics == lyrics
    assert saved.song.source == "generated"
    assert saved.song.prompt_id == "p-101"
    assert saved.song.title == "Night Signal (Cover)"


def test_songplanner_known_lyrics_strips_only_edge_whitespace(tmp_path: Path):
    """Edge whitespace is not lyric content; every interior character must survive."""
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Whitespace"))
    interior = (
        "[Intro]\n\n\n[Verse 1]\nStatic in the wires\n    four-space indent kept\n"
        "\tTab indent kept\n\n[Chorus]\nNight signal   \n\n[Outro]\nFade…"
    )

    response = client.post(
        f"/api/projects/{project.id}/generate/songplanner",
        json={"title": "Edges", "idea": "cover", "lyrics": f"\n\n  {interior}  \n\t\n"},
    )

    assert response.status_code == 202
    encoder = next(
        node
        for node in comfy.prompts[-1].values()
        if node["class_type"] == "MiniMaxMusic3TextEncode"
    )
    assert encoder["inputs"]["lyrics"] == interior
    assert store.get(project.id).song.lyrics == interior


def test_songplanner_rejects_blank_lyrics(tmp_path: Path):
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="BlankLyrics"))

    for lyrics in ("", "   \n\t "):
        response = client.post(
            f"/api/projects/{project.id}/generate/songplanner",
            json={"title": "T", "idea": "an idea", "lyrics": lyrics},
        )
        assert response.status_code == 422, repr(lyrics)
    assert store.get(project.id).jobs == []
    assert store.get(project.id).song is None


def test_songplanner_without_lyrics_keeps_invented_planner_wiring(tmp_path: Path):
    """Omitted and explicitly null lyrics both take the invented path, by design."""
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Invented"))
    bodies = (
        {"title": "Night Signal", "idea": "sunset synthwave"},
        {"title": "Night Signal", "idea": "sunset synthwave", "lyrics": None},
    )

    for body in bodies:
        response = client.post(
            f"/api/projects/{project.id}/generate/songplanner", json=body
        )

        assert response.status_code == 202, body
        encoder = next(
            node
            for node in comfy.prompts[-1].values()
            if node["class_type"] == "MiniMaxMusic3TextEncode"
        )
        assert encoder["inputs"]["lyrics"] == ["55", 1], body
        assert store.get(project.id).song.lyrics == ""


def test_songplanner_rejects_out_of_bounds_requests(tmp_path: Path):
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Validation"))
    invalid_bodies = (
        {"title": "T", "idea": "   "},  # blank/whitespace idea
        {"title": "   ", "idea": "an idea"},  # whitespace title
        {"title": "T", "idea": "an idea", "duration": 29},  # below M3SongPlanner's 30 s floor
        {"title": "T", "idea": "an idea", "duration": 301},  # above M3SongPlanner's 300 s ceiling
        {"title": "T", "idea": "x" * 4001},  # idea above max_length
        {"title": "T", "idea": "an idea", "genre_hint": "g" * 161},  # genre above max_length
        {"title": "T", "idea": "an idea", "seed": 2**32},  # above M3SongPlanner's 32-bit seed
        {"title": "T", "idea": "an idea", "lyrics": "x" * 8001},  # lyrics above max_length
    )

    for body in invalid_bodies:
        response = client.post(f"/api/projects/{project.id}/generate/songplanner", json=body)
        assert response.status_code == 422, body
    assert store.get(project.id).jobs == []
    assert store.get(project.id).song is None


def test_songplanner_rejects_below_floor_before_reaching_comfy(tmp_path: Path):
    """A sub-floor duration must fail locally with the bound named, not as an opaque 502."""
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Floor"))

    response = client.post(
        f"/api/projects/{project.id}/generate/songplanner",
        json={"title": "Too short", "idea": "an idea", "duration": 16},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(
        "duration" in item["loc"] and "30" in item["msg"] for item in detail
    ), detail
    assert comfy.prompts == []
    assert store.get(project.id).jobs == []


def test_songplanner_accepts_the_node_range_endpoints(tmp_path: Path):
    """30 and 300 are the M3SongPlanner floor and ceiling; both must reach the payload."""
    client, store, comfy = make_client(tmp_path)

    for duration in (30, 300):
        project = store.create(Project(name=f"Bound {duration}"))
        response = client.post(
            f"/api/projects/{project.id}/generate/songplanner",
            json={"title": "Bounded", "idea": "an idea", "duration": duration},
        )

        assert response.status_code == 202, duration
        planner = next(
            node for node in comfy.prompts[-1].values() if node["class_type"] == "M3SongPlanner"
        )
        assert planner["inputs"]["duration_seconds"] == duration


def test_songplanner_accepts_the_planner_seed_ceiling(tmp_path: Path):
    """4294967295 is M3SongPlanner's 32-bit maximum — the last seed ComfyUI accepts."""
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Seed ceiling"))

    response = client.post(
        f"/api/projects/{project.id}/generate/songplanner",
        json={"title": "Top seed", "idea": "an idea", "seed": 0xFFFFFFFF},
    )

    assert response.status_code == 202
    planner = next(
        node for node in comfy.prompts[-1].values() if node["class_type"] == "M3SongPlanner"
    )
    assert planner["inputs"]["seed"] == 0xFFFFFFFF
    assert store.get(project.id).jobs[-1].seed == 0xFFFFFFFF


def test_music_seed_is_not_narrowed_to_the_planner_ceiling(tmp_path: Path):
    """Direct Music 3 never touches M3SongPlanner; its encoder and sampler seeds are 64-bit."""
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Wide seed"))

    response = client.post(
        f"/api/projects/{project.id}/generate/music",
        json={
            "title": "Wide",
            "caption": "industrial synth rock",
            "duration": 12,
            "seed": 0xFFFFFFFFFFFFFFFF,
        },
    )

    assert response.status_code == 202
    encoder = next(
        node
        for node in comfy.prompts[-1].values()
        if node["class_type"] == "MiniMaxMusic3TextEncode"
    )
    assert encoder["inputs"]["seed"] == 0xFFFFFFFFFFFFFFFF


def test_music_rejects_a_seed_past_the_64_bit_ceiling(tmp_path: Path):
    """`MiniMaxMusic3TextEncode.seed` and `KSampler.seed` stop at 2**64-1.

    "Genuinely 64-bit" is itself a bound: past it ComfyUI refuses the prompt, which
    reaches the Director as the same opaque 502 the SongPlanner floor produced.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Overflow"))

    response = client.post(
        f"/api/projects/{project.id}/generate/music",
        json={
            "title": "Overflow",
            "caption": "industrial synth rock",
            "duration": 12,
            "seed": 2**64,
        },
    )

    assert response.status_code == 422
    assert comfy.prompts == []
    assert store.get(project.id).jobs == []


def test_flux_and_multiview_reject_a_seed_past_the_64_bit_ceiling(tmp_path: Path):
    """Same shape as the music route: RandomNoise and KSampler seeds are 64-bit, not unbounded."""
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Overflow assets"))

    flux = client.post(
        f"/api/projects/{project.id}/generate/flux",
        json={"name": "Lead", "prompt": "a singer", "seed": 2**64},
    )
    multiview = client.post(
        f"/api/projects/{project.id}/assets/missing/multiview",
        json={"prompt": "front, side, back", "seed": 2**64},
    )

    assert flux.status_code == 422
    # Validation must run before the unknown-asset lookup, or the bound is untested here.
    assert multiview.status_code == 422
    assert comfy.prompts == []


def test_songplanner_comfy_outage_leaves_project_unchanged(tmp_path: Path):
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Down"))
    comfy.submit_error = True

    response = client.post(
        f"/api/projects/{project.id}/generate/songplanner",
        json={"title": "Night Signal", "idea": "an idea"},
    )

    assert response.status_code == 502
    assert "unreachable" in response.json()["detail"]
    saved = store.get(project.id)
    assert saved.jobs == []
    assert saved.song is None


def test_completed_songplanner_job_reconciles_song_path(tmp_path: Path):
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Reconcile"))
    job = client.post(
        f"/api/projects/{project.id}/generate/songplanner",
        json={"title": "Night Signal", "idea": "an idea", "duration": 30, "seed": 5},
    ).json()

    async def completed_history(prompt_id):
        return type(
            "History",
            (),
            {
                "prompt_id": prompt_id,
                "status": "complete",
                "outputs": [
                    {
                        "subfolder": f"music-video-producer\\{project.id}\\songs",
                        "filename": "Night Signal_00001_.flac",
                    }
                ],
                "error": "",
            },
        )()

    comfy.history = completed_history
    refreshed = client.get(f"/api/projects/{project.id}/jobs/{job['id']}")

    assert refreshed.status_code == 200
    assert refreshed.json()["status"] == "complete"
    saved = store.get(project.id)
    assert saved.song.path == (
        f"music-video-producer/{project.id}/songs/Night Signal_00001_.flac"
    )
    assert "\\" not in saved.song.path


def test_job_refresh_translates_comfy_outage_to_502(tmp_path: Path):
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Jobs"))
    response = client.post(
        f"/api/projects/{project.id}/generate/flux",
        json={
            "name": "Frame",
            "kind": "image",
            "prompt": "frame",
            "width": 1024,
            "height": 1024,
            "steps": 4,
            "guidance": 4,
            "seed": 1,
        },
    )
    job_id = response.json()["id"]
    comfy.history_error = True

    refreshed = client.get(f"/api/projects/{project.id}/jobs/{job_id}")

    assert refreshed.status_code == 502
    assert "history unavailable" in refreshed.json()["detail"]


def test_timeline_compile_rejects_overlap(tmp_path: Path):
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Timeline"))
    project.shots = [
        Shot(start=0, duration=5, prompt="first"),
        Shot(start=4, duration=5, prompt="second"),
    ]
    store.save(project)

    response = client.post(
        f"/api/projects/{project.id}/timeline/compile",
        json={"window_start": 0, "window_duration": 9, "fps": 24},
    )

    assert response.status_code == 422
    assert "overlap" in response.json()["detail"]


def test_ready_shot_can_submit_h3_director_job(tmp_path: Path):
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="H3"))
    project.shots = [
        Shot(start=3, duration=5, prompt="A singer turns toward camera", mode="text", status="ready", seed=17)
    ]
    store.save(project)

    response = client.post(
        f"/api/projects/{project.id}/shots/{project.shots[0].id}/generate/h3",
        json={"width": 1344, "height": 768, "steps": 20},
    )

    assert response.status_code == 202
    payload = comfy.prompts[-1]
    assert payload["2343"]["class_type"] == "MiniMaxH3DirectorCS"
    assert payload["2343"]["inputs"]["start_second"] == 3
    assert payload["2347"]["inputs"]["noise_seed"] == 17
    saved = store.get(project.id)
    assert saved.shots[0].status == "queued"
    assert saved.shots[0].prompt_id == "p-101"
    assert saved.jobs[-1].kind == "h3"


def test_completed_h3_job_updates_shot_output(tmp_path: Path):
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="H3 complete"))
    project.shots = [Shot(start=0, duration=5, prompt="Turn", mode="text", status="ready")]
    store.save(project)
    submitted = client.post(
        f"/api/projects/{project.id}/shots/{project.shots[0].id}/generate/h3",
        json={},
    ).json()

    async def completed_history(prompt_id):
        return type(
            "History",
            (),
            {
                "prompt_id": prompt_id,
                "status": "complete",
                "outputs": [
                    {
                        "subfolder": f"music-video-producer/{project.id}/shots",
                        "filename": "take.mp4",
                    }
                ],
                "error": "",
            },
        )()

    comfy.history = completed_history
    response = client.get(f"/api/projects/{project.id}/jobs/{submitted['id']}")

    assert response.status_code == 200
    saved = store.get(project.id)
    assert saved.shots[0].status == "complete"
    assert saved.shots[0].latest_output.endswith("take.mp4")
    assert saved.shots[0].approved_output == ""


def test_h3_submission_serializes_multiple_references_and_master_audio(tmp_path: Path):
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="H3 refs"))
    first = client.post(
        f"/api/projects/{project.id}/assets/upload",
        data={"name": "Lead vocalist", "kind": "character"},
        files={"file": ("lead.png", b"lead-png", "image/png")},
    ).json()["assets"][0]
    second = client.post(
        f"/api/projects/{project.id}/assets/upload",
        data={"name": "Duet vocalist", "kind": "character"},
        files={"file": ("duet.png", b"duet-png", "image/png")},
    ).json()["assets"][1]
    client.post(
        f"/api/projects/{project.id}/songs/upload",
        data={"title": "Duet", "duration": "5"},
        files={"file": ("duet.flac", b"fLaCfake", "audio/flac")},
    )
    project = store.get(project.id)
    project.shots = [
        Shot(
            start=0,
            duration=5,
            prompt="The two vocalists perform the chorus together.",
            asset_ids=[first["id"], second["id"]],
            reference_labels={first["id"]: "lead vocalist", second["id"]: "duet vocalist"},
            use_song_audio=True,
            status="ready",
        )
    ]
    store.save(project)

    response = client.post(
        f"/api/projects/{project.id}/shots/{project.shots[0].id}/generate/h3",
        json={},
    )

    assert response.status_code == 202
    payload = comfy.prompts[-1]
    prompt = payload["mvp:condition"]["inputs"]["prompt"]
    assert "<Picture 1> is lead vocalist" in prompt
    assert "<Picture 2> is duet vocalist" in prompt
    assert "<Audio 1> is the master song" in prompt
    assert payload["mvp:condition"]["inputs"]["ref_audios.ref_audio_0"] == ["mvp:split", 15]


def unreachable_payload_builder(**kwargs):
    """A payload builder a blocked submission must never reach. See the two tests below."""
    raise AssertionError("a payload was built for a shot the readiness gate blocks")


def test_h3_refuses_an_unprompted_shot_before_a_payload_or_a_submission(tmp_path: Path, monkeypatch):
    """The text branch, refused at the route with nothing built and nothing sent.

    Both builders are replaced, not just the one this branch uses: the guard sits before the
    branch, so a guard that had drifted below it would be caught whichever way the Shot routed.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Blocked"))
    project.shots = [
        Shot(id="shot_blank", start=0, duration=5, prompt="   \n\t", mode="text", status="ready")
    ]
    store.save(project)
    monkeypatch.setattr(
        "music_video_producer.app.build_h3_director_payload", unreachable_payload_builder
    )
    monkeypatch.setattr(
        "music_video_producer.app.build_h3_reference_payload", unreachable_payload_builder
    )

    response = client.post(f"/api/projects/{project.id}/shots/shot_blank/generate/h3", json={})

    assert response.status_code == 422
    # Named the way the timeline names it. A bare `shot_blank` appears nowhere on screen, and a
    # real id is `shot_a1b2c3d4e5f6`; the clip is drawn `SHOT 01`.
    assert response.json()["detail"] == readiness_refusal(["SHOT 01 (shot_blank)"])
    assert "SHOT 01" in response.json()["detail"]
    assert comfy.prompts == []
    # No job recorded and no status advanced: a refusal must leave the plan exactly as it was.
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.jobs == []
    assert saved.shots[0].status == "ready"
    assert saved.shots[0].prompt_id == ""


def test_h3_refuses_an_unprompted_reference_shot_in_the_same_words(tmp_path: Path, monkeypatch):
    """The reference branch, where the prompt is interpolated into a non-empty string.

    `build_h3_reference_payload` is handed `f"Reference map: {tags}. {shot.prompt}"`, so an empty
    prompt arrives downstream as a populated sentence and every truthiness check past that point
    passes. This is the branch a gate placed one line too low would silently let through, and it
    is the branch that costs the most: reference shots run MiniMax Ultra.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Blocked reference"))
    asset = client.post(
        f"/api/projects/{project.id}/assets/upload",
        data={"name": "Lead vocalist", "kind": "character"},
        files={"file": ("lead.png", b"lead-png", "image/png")},
    ).json()["assets"][0]
    project = store.get(project.id)
    project.shots = [
        Shot(
            id="shot_blank",
            start=0,
            duration=5,
            prompt="",
            asset_ids=[asset["id"]],
            reference_labels={asset["id"]: "lead vocalist"},
            status="ready",
        )
    ]
    store.save(project)
    monkeypatch.setattr(
        "music_video_producer.app.build_h3_reference_payload", unreachable_payload_builder
    )

    response = client.post(f"/api/projects/{project.id}/shots/shot_blank/generate/h3", json={})

    assert response.status_code == 422
    # Identical wording to the text branch: one rule, one sentence, whichever way the Shot routes.
    assert response.json()["detail"] == readiness_refusal(["SHOT 01 (shot_blank)"])
    assert "Reference map" not in response.json()["detail"]
    assert comfy.prompts == []
    assert ProjectStore(tmp_path).get(project.id).jobs == []


def test_h3_refuses_a_draft_shot_for_its_prompt_rather_than_for_its_status(tmp_path: Path):
    """The only kind of unrenderable Shot the shipped application actually produces.

    Nothing in the frontend ever writes `status = "ready"` -- new Shots and duplicates are
    `draft` -- so `Shot(prompt="", status="draft")` is the real case, and it is the *only* input
    that can tell the two refusals apart. Both other refusal tests pin `status="ready"` to reach
    the payload branches, which makes the status check unreachable in them: with those alone, the
    readiness guard and the status guard could be swapped and the suite would stay green while
    every Shot the app makes was refused for the wrong reason.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Draft"))
    project.shots = [Shot(id="shot_draft", start=0, duration=5, prompt="", status="draft")]
    store.save(project)

    response = client.post(f"/api/projects/{project.id}/shots/shot_draft/generate/h3", json={})

    assert response.status_code == 422
    assert response.json()["detail"] == readiness_refusal(["SHOT 01 (shot_draft)"])
    # The status refusal is the one it must *not* give: it names neither the real problem nor
    # anything the Director can act on.
    assert "must be ready" not in response.json()["detail"]
    assert comfy.prompts == []


def test_h3_refuses_a_shot_still_carrying_the_new_shot_placeholder(tmp_path: Path):
    """The commonest unrenderable Shot of all, and one the first cut of this gate let through.

    `app.js` writes `"New shot"` onto every Shot it creates and duplicating one copies it, so a
    plan arrives at submission carrying the placeholder by default. Reaching `""` instead takes a
    deliberate deletion. `planned_project` is used because it is the fixture that already had one
    of each, and it counted the placeholder Shot as ready.
    """
    client, store, comfy = make_client(tmp_path)
    project = planned_project(store, "Placeholder")

    response = client.post(f"/api/projects/{project.id}/shots/shot_first/generate/h3", json={})

    assert response.status_code == 422
    assert response.json()["detail"] == readiness_refusal(["SHOT 01 (shot_first)"])
    assert comfy.prompts == []
    assert ProjectStore(tmp_path).get(project.id).jobs == []


def test_h3_blocks_only_the_unprompted_shot_and_not_its_neighbours(tmp_path: Path):
    """The gate is per Shot. A plan with one blank prompt must still be able to render the rest.

    Without this, `readiness_report(project).ready` read as the condition -- the obvious wrong
    implementation -- would refuse every submission in a plan the Director is still writing,
    which is every plan, most of the time.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Mixed"))
    project.shots = [
        Shot(id="shot_written", start=0, duration=5, prompt="A singer turns", mode="text",
             status="ready"),
        Shot(id="shot_blank", start=10, duration=5, prompt="", mode="text", status="ready"),
    ]
    store.save(project)

    response = client.post(f"/api/projects/{project.id}/shots/shot_written/generate/h3", json={})

    assert response.status_code == 202
    assert len(comfy.prompts) == 1


def test_readiness_route_reports_the_plan_and_stores_nothing(tmp_path: Path):
    """The thin delegator. Readiness is derived on demand, so the GET must not write.

    `planned_project` is the fixture the gate was written against, and it is the whole argument
    for treating the placeholder as blank: it is a plan a Director would recognise -- one Shot
    still saying `"New shot"`, one cleared to `""` -- and neither of them can be rendered.
    """
    client, store, comfy = make_client(tmp_path)
    project = planned_project(store, "Readiness")
    before = ProjectStore(tmp_path).get(project.id).model_dump(mode="json")

    response = client.get(f"/api/projects/{project.id}/readiness")

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is False
    assert [note["shot_ids"] for note in body["blocking"]] == [["shot_first"], ["shot_second"]]
    assert [note["labels"] for note in body["blocking"]] == [
        ["SHOT 01 (shot_first)"],
        ["SHOT 02 (shot_second)"],
    ]
    assert body["ready_count"] == 0
    assert body["shot_count"] == 2
    assert body["warnings"] == []
    # Two blanks are trivially identical, and the report says nothing about that -- a warning
    # that resolves itself the instant either block is acted on would only bury them.
    assert body["warnings_computed"] is True
    # Derived, never stored: the manifest is unchanged afterwards and gained no readiness field.
    after = ProjectStore(tmp_path).get(project.id).model_dump(mode="json")
    assert after == before
    # Asserted against the dump taken *after* the call. Against `before` it could never fail --
    # a snapshot taken before the route ran cannot show what the route wrote.
    assert "readiness" not in after
    assert not any("readiness" in shot for shot in after["shots"])
    assert comfy.prompts == []


def test_h3_asks_only_for_the_blocking_answer_so_a_batch_is_not_quadratic_per_shot(
    tmp_path: Path, monkeypatch
):
    """Sameness cannot change this route's answer, and the batch loop calls it once per Shot.

    Computing the pairwise pass here runs an O(N^2) comparison N times over one batch -- O(N^3)
    across the batch -- and discards the warnings every time. Asserted by recording what the route
    asked for, because the refusal is identical either way: nothing else can tell the two apart,
    which is exactly why it would drift back.
    """
    from music_video_producer import app as app_module

    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Blocking only"))
    project.shots = [
        Shot(id="shot_a", start=0, duration=5, prompt="A singer turns", mode="text",
             status="ready"),
        Shot(id="shot_b", start=10, duration=5, prompt="A singer turns", mode="text",
             status="ready"),
    ]
    store.save(project)
    asked: list[bool] = []
    real = app_module.readiness_report

    def recording_report(project, *, include_warnings=True):
        asked.append(include_warnings)
        return real(project, include_warnings=include_warnings)

    monkeypatch.setattr(app_module, "readiness_report", recording_report)

    response = client.post(f"/api/projects/{project.id}/shots/shot_a/generate/h3", json={})

    assert response.status_code == 202
    assert asked == [False], "the submission route computed variance warnings it then discarded"
    assert len(comfy.prompts) == 1


def test_readiness_route_404s_for_a_project_that_does_not_exist(tmp_path: Path):
    client, _, _ = make_client(tmp_path)

    assert client.get("/api/projects/no-such-project/readiness").status_code == 404


def test_compile_timeline_reports_readiness_without_refusing_the_dry_run(tmp_path: Path):
    """The dry run queues nothing, so it reports and does not block.

    It is the one cheap way to see what a plan would serialise -- and what it serialises for an
    unprompted Shot is `"prompt": ""`, silently, which is exactly the thing worth seeing before
    the expensive call. Both halves are asserted: the empty prompt still compiles, and the
    readiness the compile response carries names the Shot that would be refused at submission.
    """
    client, store, comfy = make_client(tmp_path)
    project = planned_project(store, "Dry run")

    response = client.post(
        f"/api/projects/{project.id}/timeline/compile",
        json={"window_start": 0, "window_duration": 16, "fps": 24},
    )

    assert response.status_code == 200
    body = response.json()
    segments = json.loads(body["timeline_data"])["segments"]
    assert [segment["prompt"] for segment in segments] == ["New shot", ""]
    assert body["readiness"]["ready"] is False
    assert [note["shot_ids"] for note in body["readiness"]["blocking"]] == [
        ["shot_first"],
        ["shot_second"],
    ]
    assert comfy.prompts == []


def test_the_compile_response_is_typed_so_its_readiness_block_is_discoverable(tmp_path: Path):
    """A field with no schema is a field no client can find and no contract can pin.

    The compile route had no `response_model` at all, so the readiness block it now reports would
    have been absent from `/openapi.json` -- indistinguishable from an accidental extra key, and
    unusable by anything written against the schema.
    """
    client, _, _ = make_client(tmp_path)

    schema = client.get("/openapi.json").json()
    compile_response = schema["paths"]["/api/projects/{project_id}/timeline/compile"]["post"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]
    name = compile_response["$ref"].rsplit("/", 1)[-1]
    properties = schema["components"]["schemas"][name]["properties"]

    assert set(properties) == {
        "timeline_data",
        "requested_frames",
        "aligned_frames",
        "warnings",
        "readiness",
    }
    # And the readiness field resolves to the report's own schema rather than a free-form object.
    readiness = schema["components"]["schemas"][
        properties["readiness"]["$ref"].rsplit("/", 1)[-1]
    ]
    assert {"ready", "blocking", "warnings", "warnings_computed"} <= set(readiness["properties"])


def test_image_asset_vision_inspection_is_persisted(tmp_path: Path):
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Vision"))
    uploaded = client.post(
        f"/api/projects/{project.id}/assets/upload",
        data={"name": "Lead", "kind": "character"},
        files={"file": ("lead.png", b"lead-png", "image/png")},
    ).json()
    asset_id = uploaded["assets"][0]["id"]

    response = client.post(f"/api/projects/{project.id}/assets/{asset_id}/analyze")

    assert response.status_code == 200
    vision = store.get(project.id).assets[0].vision
    assert vision is not None
    assert vision.identity == ["silver jacket"]
    assert vision.continuity_cues == ["keep silver jacket"]


def test_latest_take_vision_review_is_persisted_separately_from_approval(tmp_path: Path):
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Take review"))
    output = tmp_path / "comfy" / "output" / "takes" / "latest.png"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"generated-png")
    project.shots = [
        Shot(
            start=0,
            duration=5,
            prompt="Duet chorus",
            latest_output="takes/latest.png",
            status="complete",
        )
    ]
    store.save(project)

    response = client.post(
        f"/api/projects/{project.id}/shots/{project.shots[0].id}/analyze-latest"
    )

    assert response.status_code == 200
    shot = store.get(project.id).shots[0]
    assert shot.latest_review is not None
    assert shot.latest_review.summary == "Two visible character views."
    assert shot.approved_output == ""


def test_character_asset_can_be_promoted_to_krea_multiview(tmp_path: Path):
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Character"))
    uploaded = client.post(
        f"/api/projects/{project.id}/assets/upload",
        data={"name": "Mara", "kind": "character"},
        files={"file": ("mara.png", b"png-data", "image/png")},
    ).json()
    asset_id = uploaded["assets"][0]["id"]

    response = client.post(
        f"/api/projects/{project.id}/assets/{asset_id}/multiview",
        json={"prompt": "Preserve Mara in face, front, side and back views", "seed": 77},
    )

    assert response.status_code == 202
    assert comfy.prompts[-1]["127"]["inputs"]["lora_name"].endswith("QuadView_krea2_v1.safetensors")
    saved = store.get(project.id)
    assert saved.jobs[-1].kind == "multiview"
    assert saved.assets[-1].parent_id == asset_id


def test_multiview_rejects_asset_paths_outside_project_media(tmp_path: Path):
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Contained"))
    secret = tmp_path / "outside.png"
    secret.write_bytes(b"must-not-be-uploaded")
    project.assets.append(
        Asset(name="Forged", kind="character", path=str(secret), source="upload")
    )
    store.save(project)

    response = client.post(
        f"/api/projects/{project.id}/assets/{project.assets[0].id}/multiview",
        json={"prompt": "four views", "seed": 1},
    )

    assert response.status_code == 404
    assert comfy.uploads == []


def test_health_reports_standalone_identity_and_comfy_state(tmp_path: Path):
    client, _, _ = make_client(tmp_path)

    payload = client.get("/api/health").json()

    assert payload["app"] == "Music Video Producer"
    assert payload["comfy"]["online"] is True
    assert "agent" not in payload["app"].lower()


def test_director_chat_persists_treatment_and_editable_shots(tmp_path: Path):
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Directed"))

    response = client.post(
        f"/api/projects/{project.id}/director/chat",
        json={
            "message": "Make the chorus feel like release",
            "apply_shots": True,
            "apply_documents": True,
        },
    )

    assert response.status_code == 200
    saved = store.get(project.id)
    assert saved.treatment.startswith("Confinement")
    assert saved.messages[-1].role == "assistant"
    assert saved.shots[0].prompt == "A widening corridor"


def test_director_shot_application_preserves_existing_shot_provenance(tmp_path: Path):
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Edited"))
    project.shots = [
        Shot(
            start=1,
            duration=4,
            prompt="Manual draft",
            mode="image",
            asset_ids=["asset_reference"],
            seed=44,
            status="approved",
            prompt_id="render-1",
            approved_output="take.mp4",
        )
    ]
    original_id = project.shots[0].id
    store.save(project)

    response = client.post(
        f"/api/projects/{project.id}/director/chat",
        json={"message": "Refine the plan", "apply_shots": True},
    )

    assert response.status_code == 200
    shot = store.get(project.id).shots[0]
    assert shot.id == original_id
    assert shot.prompt == "A widening corridor"
    assert shot.asset_ids == ["asset_reference"]
    assert shot.mode == "image"
    assert shot.seed == 44
    assert shot.status == "approved"
    assert shot.prompt_id == "render-1"
    assert shot.approved_output == "take.mp4"


class DegradedDirector(FakeDirector):
    """Reproduces the 2026-08-16 defect: JSON returned in a prose field, empty shot list."""

    async def plan(self, message, project_context):
        return type(
            "DirectorResult",
            (),
            {
                "message": "Splitting your vision into a four-beat sequence.",
                "treatment": "A genuinely rewritten treatment of adequate length for replacement.",
                "style_bible": '[{"style":"moody","color_palette":["amber","teal"]}]',
                "shots": [],
            },
        )()


def make_client_with_director(tmp_path: Path, director):
    settings = Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy")
    store = ProjectStore(tmp_path)
    app = create_app(settings=settings, store=store, comfy=FakeComfy(), director=director)
    return TestClient(app), store


def test_director_never_overwrites_a_document_with_json(tmp_path: Path):
    client, store = make_client_with_director(tmp_path, DegradedDirector())
    project = store.create(Project(name="Guarded"))
    project.style_bible = "Sodium amber, hard backlight, 35mm grain, wardrobe continuity notes."
    project.treatment = "The original treatment the Director wrote by hand."
    store.save(project)

    response = client.post(
        f"/api/projects/{project.id}/director/chat",
        json={"message": "Make it moodier", "apply_shots": True, "apply_documents": True},
    )

    assert response.status_code == 200
    saved = store.get(project.id)
    # The style bible survives untouched; the treatment was legitimately replaced.
    assert saved.style_bible == "Sodium amber, hard backlight, 35mm grain, wardrobe continuity notes."
    assert saved.treatment.startswith("A genuinely rewritten treatment")
    notice = saved.messages[-1].content
    assert "Style bible was NOT replaced" in notice
    assert "empty shot list" in notice
    # A rejected candidate must leave the recovery slot alone as well as the document.
    # Capturing before the guard ran would have spent the single slot on the rejection,
    # overwriting the only copy of the good style bible — the guard destroying the very
    # thing it exists to protect. The applied replacement does capture, in the same call.
    assert saved.style_bible_previous == ""
    assert saved.treatment_previous == "The original treatment the Director wrote by hand."
    # And the reply now says what *did* change, not only what did not.
    # The list ends at the Treatment, so the rejected style bible is not claimed as changed.
    assert "Replaced by this reply: Treatment." in notice


def test_director_reports_shots_outside_the_h3_window(tmp_path: Path):
    class LongShotDirector(FakeDirector):
        async def plan(self, message, project_context):
            shot = type("PlannedShot", (), {"start": 0, "duration": 20, "prompt": "One long take"})()
            return type(
                "DirectorResult",
                (),
                {
                    "message": "One continuous shot.",
                    "treatment": "A single unbroken movement through the room.",
                    "style_bible": "Cold blue, handheld, 28mm.",
                    "shots": [shot],
                },
            )()

    client, store = make_client_with_director(tmp_path, LongShotDirector())
    project = store.create(Project(name="Long"))

    client.post(
        f"/api/projects/{project.id}/director/chat",
        json={"message": "one long take", "apply_shots": True},
    )

    saved = store.get(project.id)
    assert "outside MiniMax H3's reliable 4-15s window" in saved.messages[-1].content
    assert len(saved.shots) == 1  # still applied; the Director decides what to do about it
    # Carried as data as well as in the joined text, so the reply renders it as a notice block
    # rather than as another paragraph of Director prose.
    assert saved.messages[-1].notices[-1].text == SHOT_WINDOW_NOTICE.format(
        duration=20, start=0, minimum=4, maximum=15
    )
    # A flag, not a refusal: nothing about the Shot came from the model's raw output.
    assert saved.messages[-1].notices[-1].raw == ""


class RevisingDirector(FakeDirector):
    """Returns a different, guard-passing pair of documents on every call.

    Also records each `project_context` it was handed, which is the only way to assert what
    the prompt actually carried.
    """

    def __init__(self):
        self.calls = 0
        self.contexts: list[dict] = []

    async def plan(self, message, project_context):
        self.calls += 1
        self.contexts.append(project_context)
        return type(
            "DirectorResult",
            (),
            {
                "message": f"Revision {self.calls}.",
                "treatment": f"Treatment revision {self.calls}, written long enough to clear the floor.",
                "style_bible": f"Style bible revision {self.calls}, written long enough to clear it too.",
                "shots": [],
            },
        )()


class RefusingDirector(FakeDirector):
    """Fails the test if it is called at all.

    Restore must never reach the model, and neither must an expansion of a project with no
    shots — a refusal that still spent a model call is not a refusal.
    """

    def __init__(self):
        self.calls = 0

    async def plan(self, message, project_context):
        self.calls += 1
        raise AssertionError("the Director was called on a path that must not call it")

    async def expand(self, expansion_input):
        self.calls += 1
        raise AssertionError("the Director was called on a path that must not call it")


class EchoDirector(FakeDirector):
    """Returns the project's stored documents straight back — the "nothing to say" reply.

    Read out of the supplied context rather than hardcoded, so the echo is genuinely of
    whatever the project holds. `document_rejection` returns "" for this, which is why the
    guard alone treats it as an applied replacement.
    """

    async def plan(self, message, project_context):
        return type(
            "DirectorResult",
            (),
            {
                "message": "Nothing to change.",
                "treatment": project_context["treatment"],
                "style_bible": project_context["style_bible"],
                "shots": [],
            },
        )()


class ConcurrentEditDirector(FakeDirector):
    """Commits a change to the *stored* project while the LLM call is in flight.

    Stands in for the real window a local model opens: `director.plan` can be held for many
    seconds, and a lock set or a restore applied in that time is committed by another request
    before the reply is saved.
    """

    def __init__(self, store: ProjectStore, project_id: str, edit):
        self.store = store
        self.project_id = project_id
        self.edit = edit

    async def plan(self, message, project_context):
        during = self.store.get(self.project_id)
        self.edit(during)
        self.store.save(during)
        return type(
            "DirectorResult",
            (),
            {
                "message": "Revision 1.",
                "treatment": "Treatment revision 1, written long enough to clear the floor.",
                "style_bible": "Style bible revision 1, written long enough to clear it too.",
                "shots": [],
            },
        )()


def documented_project(store: ProjectStore, name: str) -> Project:
    """A project whose two creative documents both hold work worth losing."""
    project = store.create(Project(name=name))
    project.treatment = "The original treatment, written by hand over several sessions."
    project.style_bible = "Sodium amber, hard backlight, 35mm grain, wardrobe continuity notes."
    store.save(project)
    return store.get(project.id)


def test_applied_replacement_keeps_the_previous_version_and_names_what_changed(tmp_path: Path):
    """FR-16's core: a replacement the Director never asked for is now visible and reversible.

    Re-read through a *fresh* ProjectStore, because the response body is the in-memory
    object the handler just built; only the manifest proves recovery survives a restart.
    """
    director = RevisingDirector()
    client, store = make_client_with_director(tmp_path, director)
    project = documented_project(store, "Recoverable")

    response = client.post(
        f"/api/projects/{project.id}/director/chat",
        json={"message": "Make it colder", "apply_documents": True},
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.treatment == "Treatment revision 1, written long enough to clear the floor."
    assert saved.style_bible == "Style bible revision 1, written long enough to clear it too."
    assert saved.treatment_previous == project.treatment
    assert saved.style_bible_previous == project.style_bible
    # Nothing in the reply used to say a document had changed at all, which is what made a
    # plausible unrequested rewrite permanent *and* invisible.
    notice = saved.messages[-1].content
    assert "Replaced by this reply: Treatment, Style bible." in notice
    assert "can be restored" in notice


def test_repeat_replacement_keeps_only_the_immediately_previous_version(tmp_path: Path):
    """AD-14 is single-slot by decision: recovery is one step back, never a history stack."""
    director = RevisingDirector()
    client, store = make_client_with_director(tmp_path, director)
    project = documented_project(store, "Single slot")

    for message in ("Colder", "Colder still"):
        assert (
            client.post(
                f"/api/projects/{project.id}/director/chat",
                json={"message": message, "apply_documents": True},
            ).status_code
            == 200
        )

    saved = ProjectStore(tmp_path).get(project.id)
    assert director.calls == 2
    assert saved.treatment == "Treatment revision 2, written long enough to clear the floor."
    assert saved.treatment_previous == "Treatment revision 1, written long enough to clear the floor."
    # The hand-written original is gone, and deliberately so — one slot, not a stack.
    assert project.treatment not in (saved.treatment, saved.treatment_previous)


def test_locked_document_is_not_replaced_and_records_no_previous_version(tmp_path: Path):
    """FR-16's "locked fields are never modified", mirroring `Shot.locked`.

    A lock must not spend the recovery slot either: a document that was never in play has
    no previous version, and recording one would report a change that did not happen.
    """
    director = RevisingDirector()
    client, store = make_client_with_director(tmp_path, director)
    project = documented_project(store, "Locked treatment")
    project.treatment_locked = True
    store.save(project)

    response = client.post(
        f"/api/projects/{project.id}/director/chat",
        json={"message": "Rewrite everything", "apply_documents": True},
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.treatment == project.treatment
    assert saved.treatment_previous == ""
    assert saved.treatment_locked is True
    # The unlocked document still moves, so the lock is a per-document rule and not a
    # whole-reply veto.
    assert saved.style_bible == "Style bible revision 1, written long enough to clear it too."
    assert saved.style_bible_previous == project.style_bible
    notice = saved.messages[-1].content
    assert "Treatment is locked" in notice
    assert "Replaced by this reply: Style bible." in notice


def test_locked_shot_survives_a_director_result(tmp_path: Path):
    """The one lock that already existed, previously untested on this path."""
    client, store = make_client_with_director(tmp_path, FakeDirector())
    project = store.create(Project(name="Locked shot"))
    project.shots = [
        Shot(start=1.5, duration=6.25, prompt="Held wide on the corridor", locked=True),
        Shot(start=20, duration=4, prompt="Chorus release"),
    ]
    store.save(project)

    response = client.post(
        f"/api/projects/{project.id}/director/chat",
        json={"message": "Retime the opening", "apply_shots": True},
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    # FakeDirector proposes start 0 / duration 5 / "A widening corridor" for index 0.
    assert saved.shots[0].start == 1.5
    assert saved.shots[0].duration == 6.25
    assert saved.shots[0].prompt == "Held wide on the corridor"
    assert saved.shots[0].locked is True
    # The shot the plan did not reach is kept rather than dropped.
    assert saved.shots[1].prompt == "Chorus release"


def test_restore_swaps_the_document_without_calling_the_director(tmp_path: Path):
    """Recovery must not depend on the model whose output caused the problem.

    The kept version is planted directly rather than produced by a chat, so the route is
    exercised with a Director that raises if it is touched at all.
    """
    director = RefusingDirector()
    client, store = make_client_with_director(tmp_path, director)
    project = documented_project(store, "Restore")
    project.treatment = "The unwanted rewrite nobody asked for."
    project.treatment_previous = "The original treatment, worth getting back."
    store.save(project)

    response = client.post(f"/api/projects/{project.id}/documents/treatment/restore")

    assert response.status_code == 200
    assert director.calls == 0
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.treatment == "The original treatment, worth getting back."
    # A swap, not a pop: the restore is itself reversible, so a mis-click costs nothing.
    assert saved.treatment_previous == "The unwanted rewrite nobody asked for."
    assert saved.style_bible == project.style_bible
    assert saved.messages[-1].role == "system"
    assert "Treatment was restored" in saved.messages[-1].content
    assert "No Director call was made" in saved.messages[-1].content

    again = client.post(f"/api/projects/{project.id}/documents/treatment/restore")
    assert again.status_code == 200
    assert ProjectStore(tmp_path).get(project.id).treatment == "The unwanted rewrite nobody asked for."


def test_restore_with_nothing_kept_is_refused_and_changes_nothing(tmp_path: Path):
    """An empty slot must refuse, not blank the live document with "" — that is the loss."""
    client, store = make_client_with_director(tmp_path, RefusingDirector())
    project = documented_project(store, "Nothing kept")

    response = client.post(f"/api/projects/{project.id}/documents/style_bible/restore")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "No previous version of Style bible was kept" in detail
    assert "nothing to restore" in detail
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.style_bible == project.style_bible
    assert saved.style_bible_previous == ""
    assert saved.messages == []


def test_document_save_preserves_recovery_and_lock_fields_and_can_set_the_locks(tmp_path: Path):
    """The UI's ordinary save path must not defeat the feature.

    Every text field on `ProjectDocumentsRequest` defaults to "", so an omitted one blanks
    its document. A lock defaulting to False the same way would silently unlock both
    documents on every save, and a recovery slot defaulting to "" would discard the kept
    version — so locks are tri-state and the slots are not on the request model at all.
    """
    client, store = make_client_with_director(tmp_path, RefusingDirector())
    project = documented_project(store, "Ordinary save")
    project.treatment_previous = "The version kept before the last replacement."
    project.style_bible_previous = "The style bible kept before the last replacement."
    project.treatment_locked = True
    store.save(project)

    untouched = client.put(
        f"/api/projects/{project.id}/documents",
        json={
            "creative_brief": "A brief",
            "treatment": "Hand-edited treatment text.",
            "style_bible": project.style_bible,
        },
    )

    assert untouched.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.treatment == "Hand-edited treatment text."
    assert saved.treatment_previous == "The version kept before the last replacement."
    assert saved.style_bible_previous == "The style bible kept before the last replacement."
    # An omitted lock means "leave it alone", so a client that predates locks cannot unlock.
    assert saved.treatment_locked is True
    assert saved.style_bible_locked is False

    setting = client.put(
        f"/api/projects/{project.id}/documents",
        json={
            "creative_brief": "A brief",
            "treatment": "Hand-edited treatment text.",
            "style_bible": project.style_bible,
            "treatment_locked": False,
            "style_bible_locked": True,
        },
    )

    assert setting.status_code == 200
    relocked = ProjectStore(tmp_path).get(project.id)
    assert relocked.treatment_locked is False
    assert relocked.style_bible_locked is True
    assert relocked.treatment_previous == "The version kept before the last replacement."


def test_director_context_excludes_every_recovery_slot(tmp_path: Path):
    """Not an optimisation. The recorded root cause of the original document corruption was
    degradation under rich context, so echoing a second full copy of both documents into
    every prompt makes the failure `document_rejection` exists for *more* likely.

    Driven off `DOCUMENT_LABELS` rather than the two field names, so a document added to the
    mapping is covered here without anyone remembering to extend this test — which is the
    drift that would silently leak a kept copy back into the prompt.
    """
    director = RevisingDirector()
    client, store = make_client_with_director(tmp_path, director)
    project = documented_project(store, "Context")
    for field in DOCUMENT_LABELS:
        setattr(project, f"{field}_previous", f"A kept {field} that must not reach the prompt.")
    store.save(project)

    client.post(f"/api/projects/{project.id}/director/chat", json={"message": "Colder"})

    context = director.contexts[0]
    serialised = json.dumps(context)
    for field in DOCUMENT_LABELS:
        assert f"{field}_previous" not in context, field
        assert "must not reach the prompt" not in serialised, field
        # The live documents and the locks are still there: the model needs the current
        # text, and a boolean saying a document is off-limits is useful direction.
        assert context[field] == getattr(project, field), field
        assert context[f"{field}_locked"] is False, field


def test_document_mapping_field_names_and_context_exclusion_cannot_drift():
    """One mapping, and everything derived from it — asserted, not assumed.

    The guard loop reaches document fields by string interpolation (`f"{field}_previous"`),
    so a name in `DOCUMENT_LABELS` that `Project` does not carry is an `AttributeError` at
    request time rather than a startup failure, and a slot on `Project` that the mapping does
    not know about is a full second copy of a document silently added to every prompt.
    """
    assert set(DOCUMENT_LABELS) == set(get_args(DocumentName))
    for field in DOCUMENT_LABELS:
        for owned in (field, f"{field}_previous", f"{field}_locked"):
            assert owned in Project.model_fields, owned
        # The loop reads the candidate off DirectorResult by the same name.
        assert field in DirectorResult.model_fields, field
        assert DIRECTOR_CONTEXT_EXCLUDE[f"{field}_previous"] is True, field
    slots = {name for name in Project.model_fields if name.endswith("_previous")}
    assert slots == {f"{field}_previous" for field in DOCUMENT_LABELS}
    assert {name for name in DIRECTOR_CONTEXT_EXCLUDE if name.endswith("_previous")} == slots
    # The exclusion is derived from the mapping rather than transcribed beside it, which is
    # the whole reason the two cannot fall out of step.
    assert DIRECTOR_CONTEXT_EXCLUDE["jobs"] is True
    # Notices go out whole, raw output and all. The alternative — excluding the `raw` field by
    # a nested path — stops covering a field renamed or added beside it, and this is the one
    # invariant that turns the degradation guard into the source of the degradation.
    per_message = DIRECTOR_CONTEXT_EXCLUDE["messages"]["__all__"]
    assert "notices" in per_message
    assert "notices" in TreatmentMessage.model_fields
    assert set(MessageNotice.model_fields) == {"kind", "text", "raw"}
    # `kind` has no default on purpose: a new construction site must decide rather than inherit
    # whichever rendering happened to be the fallback, which is how a confirmation ends up
    # wearing caution chrome. The other two carry the constraints the persisted thread needs.
    assert MessageNotice.model_fields["kind"].is_required()
    assert set(get_args(NoticeKind)) == {"change", "refusal", "flag"}
    with pytest.raises(ValidationError):
        MessageNotice(text="No kind was decided for this one.")
    with pytest.raises(ValidationError):
        MessageNotice(kind="flag", text="")


def test_full_project_put_cannot_clear_the_slots_or_the_locks(tmp_path: Path):
    """The sibling-route hole the Song story had, in its document form.

    `replace_project` binds a whole client `Project` and every one of these four fields is
    defaulted, so a body that merely *omits* them — which is what any client written before
    they existed sends — arrives as ""/False. Trusting it means one ordinary save clears both
    kept versions and unlocks both documents. Worse, a body that *invents* a slot would be
    planting text the restore route then swaps into the live document as "the version you
    had before".
    """
    client, store = make_client_with_director(tmp_path, RefusingDirector())
    project = documented_project(store, "Full save")
    project.treatment_previous = "The kept treatment an ordinary save must not discard."
    project.style_bible_previous = "The kept style bible an ordinary save must not discard."
    project.treatment_locked = True
    project.style_bible_locked = True
    store.save(project)

    omitted = client.get(f"/api/projects/{project.id}").json()
    for field in DOCUMENT_LABELS:
        del omitted[f"{field}_previous"]
        del omitted[f"{field}_locked"]
    omitted["creative_brief"] = "Edited by a client that predates recovery."

    response = client.put(f"/api/projects/{project.id}", json=omitted)

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.creative_brief == "Edited by a client that predates recovery."
    assert saved.treatment_previous == "The kept treatment an ordinary save must not discard."
    assert saved.style_bible_previous == "The kept style bible an ordinary save must not discard."
    assert saved.treatment_locked is True
    assert saved.style_bible_locked is True

    forged = client.get(f"/api/projects/{project.id}").json()
    forged["treatment_previous"] = "Text nobody ever wrote, planted to be restored later."
    forged["treatment_locked"] = False

    assert client.put(f"/api/projects/{project.id}", json=forged).status_code == 200
    unforged = ProjectStore(tmp_path).get(project.id)
    assert unforged.treatment_previous == "The kept treatment an ordinary save must not discard."
    assert unforged.treatment_locked is True


def test_identical_candidate_neither_spends_the_slot_nor_claims_a_change(tmp_path: Path):
    """`document_rejection` returns "" for an echo, so the guard alone counts it as applied.

    Capturing it overwrites the genuinely recoverable version with a copy of the live text —
    the single slot annihilated by a reply that changed nothing — while the notice announces
    a change the Director cannot find anywhere.
    """
    client, store = make_client_with_director(tmp_path, EchoDirector())
    project = documented_project(store, "Echo")
    project.treatment_previous = "The genuinely recoverable treatment."
    project.style_bible_previous = "The genuinely recoverable style bible."
    store.save(project)

    response = client.post(
        f"/api/projects/{project.id}/director/chat",
        json={"message": "Anything to add?", "apply_documents": True},
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.treatment == project.treatment
    assert saved.style_bible == project.style_bible
    assert saved.treatment_previous == "The genuinely recoverable treatment."
    assert saved.style_bible_previous == "The genuinely recoverable style bible."
    # And the reply claims nothing: no notice block at all was added to the message.
    assert "Replaced by this reply" not in saved.messages[-1].content
    assert "\n\n---\n" not in saved.messages[-1].content


def test_first_draft_into_a_blank_document_promises_no_recovery(tmp_path: Path):
    """The guard skips its floor for an empty target, so any first draft is "applied".

    The slot it captures is empty, so a restore refuses — and the replacement wording says
    the previous version "can be restored", which would be a promise broken by the very next
    click. A first fill is reported as a first fill.
    """
    client, store = make_client_with_director(tmp_path, RevisingDirector())
    project = store.create(Project(name="Blank"))
    project.treatment = "The treatment already written by hand, long enough to keep."
    store.save(project)

    response = client.post(
        f"/api/projects/{project.id}/director/chat",
        json={"message": "Draft the look", "apply_documents": True},
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    notice = saved.messages[-1].content
    # The style bible was blank: filled, with nothing kept and nothing promised.
    assert saved.style_bible.startswith("Style bible revision 1")
    assert saved.style_bible_previous == ""
    assert "Written for the first time by this reply: Style bible." in notice
    # The treatment had text: genuinely replaced, and genuinely recoverable.
    assert saved.treatment_previous == project.treatment
    assert "Replaced by this reply: Treatment." in notice
    # The recoverable claim attaches only to the document it is true of.
    assert notice.index("Replaced by this reply") < notice.index("Written for the first time")
    assert client.post(f"/api/projects/{project.id}/documents/style_bible/restore").status_code == 409


def test_lock_notice_is_silent_unless_the_candidate_would_have_changed_something(tmp_path: Path):
    """A locked document must not carry the same paragraph on every reply forever.

    The lock check has to run before anything is written, but reporting it before any
    comparison asserted that "the replacement this reply proposed was not applied" even when
    the model echoed the current text back, or returned something the guard would have
    refused anyway.
    """
    client, store = make_client_with_director(tmp_path, EchoDirector())
    project = documented_project(store, "Locked and echoed")
    project.treatment_locked = True
    store.save(project)

    client.post(
        f"/api/projects/{project.id}/director/chat",
        json={"message": "Anything?", "apply_documents": True},
    )

    echoed = ProjectStore(tmp_path).get(project.id).messages[-1].content
    assert "is locked" not in echoed
    assert echoed == "Nothing to change."

    # Same lock, a candidate the guard would have refused as degraded: still silent, because
    # the lock is not what stopped it and nothing would have changed either way.
    client, store = make_client_with_director(tmp_path, DegradedDirector())
    project = documented_project(store, "Locked and degraded")
    project.style_bible_locked = True
    store.save(project)

    client.post(
        f"/api/projects/{project.id}/director/chat",
        json={"message": "Moodier", "apply_documents": True},
    )

    degraded = ProjectStore(tmp_path).get(project.id)
    assert "Style bible is locked" not in degraded.messages[-1].content
    assert "Style bible was NOT replaced" not in degraded.messages[-1].content
    assert degraded.style_bible == project.style_bible
    assert degraded.style_bible_previous == ""


def test_a_declined_turn_writes_nothing_and_names_what_it_proposed(tmp_path: Path):
    """The consent this story adds: asking a question must not rewrite the Treatment.

    Story 2.1 made an unrequested rewrite visible and reversible; it was still not *consented*,
    because every turn replaced both documents whatever the Director actually asked for. Shot
    application already required `apply_shots`; the more valuable artefacts did not.
    """
    client, store = make_client_with_director(tmp_path, RevisingDirector())
    project = documented_project(store, "Declined")
    project.treatment_previous = "The genuinely recoverable treatment."
    project.style_bible_previous = "The genuinely recoverable style bible."
    store.save(project)

    response = client.post(
        f"/api/projects/{project.id}/director/chat",
        json={"message": "What do you think of this idea?", "apply_documents": False},
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.treatment == project.treatment
    assert saved.style_bible == project.style_bible
    # The recovery slot is written only when a document is actually replaced, so a declined
    # proposal must not spend it -- doing so would annihilate the recoverable version over a
    # turn that wrote nothing at all.
    assert saved.treatment_previous == "The genuinely recoverable treatment."
    assert saved.style_bible_previous == "The genuinely recoverable style bible."
    notice = saved.messages[-1].content
    assert "Proposed but not applied: Treatment, Style bible." in notice
    assert "opt-in per turn" in notice
    # And it says how to apply it, by the control's real name rather than a description of it.
    assert APPLY_DOCUMENTS_LABEL in notice
    assert "Replaced by this reply" not in notice
    # Nothing new is persisted: there is no proposal slot, and the declined text is gone
    # exactly as a declined shot list is today.
    assert "Treatment revision 1" not in json.dumps(saved.model_dump(mode="json"))


def test_an_ordinary_question_that_proposed_nothing_says_nothing(tmp_path: Path):
    """Consent without noise. A reply that echoed the current text back proposed no change,
    so a declined turn must not carry a paragraph about documents it never wanted to touch.
    """
    client, store = make_client_with_director(tmp_path, EchoDirector())
    project = documented_project(store, "Ordinary question")

    response = client.post(
        f"/api/projects/{project.id}/director/chat", json={"message": "What do you think?"}
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.messages[-1].content == "Nothing to change."
    assert "\n\n---\n" not in saved.messages[-1].content


def test_an_omitted_or_null_apply_documents_is_a_decline_rather_than_a_write(tmp_path: Path):
    """A client written before this flag existed sends no `apply_documents` at all.

    Reading that as consent would leave exactly the behaviour this story removes in place for
    every such client, so the default has to be the decline -- and it is the model field's own
    default doing it, not a route-level special case.

    An explicit `null` means the same thing to the client that sent it, but Pydantic reads it as
    a type error: without normalising it the whole turn 422s and the Director's message is lost
    over a field whose absence is already the safe default.
    """
    client, store = make_client_with_director(tmp_path, RevisingDirector())

    for label, body in (
        ("omitted", {"message": "Make it colder"}),
        ("null", {"message": "Make it colder", "apply_documents": None, "apply_shots": None}),
    ):
        project = documented_project(store, f"Older client ({label})")

        response = client.post(f"/api/projects/{project.id}/director/chat", json=body)

        assert response.status_code == 200, (label, response.text)
        saved = ProjectStore(tmp_path).get(project.id)
        assert saved.treatment == project.treatment, label
        assert saved.style_bible == project.style_bible, label
        assert saved.treatment_previous == "", label
        assert saved.style_bible_previous == "", label
        assert "Proposed but not applied: Treatment, Style bible." in saved.messages[-1].content
        # The declined turn is still a turn: nothing was applied, so no shots either.
        assert saved.shots == [], label

    # Off by default on the model, mirroring `apply_shots` rather than diverging from it, and
    # `null` lands on the same default rather than on a validation error.
    request = DirectorRequest(message="Make it colder")
    assert request.apply_documents is False
    assert request.apply_shots is False
    nulled = DirectorRequest(message="Make it colder", apply_documents=None, apply_shots=None)
    assert nulled.apply_documents is False
    assert nulled.apply_shots is False
    # Nothing else is loosened: a value that is neither a boolean nor null is still refused.
    assert (
        client.post(
            f"/api/projects/{documented_project(store, 'Bad flag').id}/director/chat",
            json={"message": "Make it colder", "apply_documents": "yes please"},
        ).status_code
        == 422
    )


def test_the_two_apply_flags_are_independent(tmp_path: Path):
    """One consent per artefact kind. Ticking either must not imply the other."""
    client, store = make_client_with_director(tmp_path, FakeDirector())
    shots_only = documented_project(store, "Shots only")

    response = client.post(
        f"/api/projects/{shots_only.id}/director/chat",
        json={"message": "Plan the shots", "apply_shots": True, "apply_documents": False},
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(shots_only.id)
    assert saved.shots[0].prompt == "A widening corridor"
    assert saved.treatment == shots_only.treatment
    assert saved.style_bible == shots_only.style_bible
    assert saved.treatment_previous == ""
    assert "Proposed but not applied: Treatment, Style bible." in saved.messages[-1].content

    documents_only = documented_project(store, "Documents only")

    response = client.post(
        f"/api/projects/{documents_only.id}/director/chat",
        json={"message": "Rewrite the treatment", "apply_shots": False, "apply_documents": True},
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(documents_only.id)
    assert saved.shots == []
    assert saved.treatment.startswith("Confinement")
    assert saved.treatment_previous == documents_only.treatment
    assert "Replaced by this reply: Treatment, Style bible." in saved.messages[-1].content
    assert "Proposed but not applied" not in saved.messages[-1].content


def test_a_locked_document_reports_the_lock_even_when_the_turn_declined(tmp_path: Path):
    """Locks take precedence over consent, and keep their own sentence.

    Both mean "not written", but a lock is durable state the Director set and the flag is one
    turn: reporting a locked document as merely unrequested would relabel a protection as an
    oversight, and would tell the Director that ticking a box will apply it -- it will not.
    """
    client, store = make_client_with_director(tmp_path, RevisingDirector())
    project = documented_project(store, "Locked and declined")
    project.treatment_locked = True
    store.save(project)

    response = client.post(
        f"/api/projects/{project.id}/director/chat", json={"message": "Rewrite everything"}
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.treatment == project.treatment
    assert saved.style_bible == project.style_bible
    assert saved.treatment_previous == ""
    assert saved.style_bible_previous == ""
    notice = saved.messages[-1].content
    assert "Treatment is locked" in notice
    # The unlocked document is the only one the unrequested wording claims, so the lock is not
    # silently downgraded to "you did not ask".
    assert "Proposed but not applied: Style bible." in notice
    assert DOCUMENT_LABELS["treatment"] not in notice.split("Proposed but not applied:")[1]

    # With *every* document locked there is nothing the consent could have applied, so the
    # unrequested wording is absent entirely. The browser keys its toast on that phrase, so this
    # is what stops it blaming the flag for a lock and pointing at a box that would not apply
    # the document anyway.
    both = documented_project(store, "Both locked and declined")
    both.treatment_locked = True
    both.style_bible_locked = True
    store.save(both)

    response = client.post(
        f"/api/projects/{both.id}/director/chat", json={"message": "Rewrite everything"}
    )

    assert response.status_code == 200
    locked_notice = ProjectStore(tmp_path).get(both.id).messages[-1].content
    assert "Treatment is locked" in locked_notice
    assert "Style bible is locked" in locked_notice
    assert "Proposed but not applied" not in locked_notice


def test_a_declined_turn_is_silent_about_a_candidate_the_guard_would_have_refused(tmp_path: Path):
    """The lock notice's silence rule, applied to consent for the same reason.

    A degraded candidate would not have landed with consent either, so naming it as merely
    unrequested would invite a retry that refuses identically -- and the rejection notice's own
    "was NOT replaced ... Raw output:" dump is diagnostics about a write nobody asked for.
    """
    client, store = make_client_with_director(tmp_path, DegradedDirector())
    project = documented_project(store, "Declined and degraded")

    response = client.post(
        f"/api/projects/{project.id}/director/chat", json={"message": "Make it moodier"}
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.treatment == project.treatment
    assert saved.style_bible == project.style_bible
    notice = saved.messages[-1].content
    # The treatment would genuinely have been applied, so it is named.
    assert "Proposed but not applied: Treatment." in notice
    assert DOCUMENT_LABELS["style_bible"] not in notice
    assert "was NOT replaced" not in notice


def test_a_lock_set_during_the_llm_call_is_honoured_rather_than_reverted(tmp_path: Path):
    """The project must be re-read after the await, not carried across it.

    A local model holds `director.plan` open for many seconds. Anything committed in that
    window — a lock set, a restore applied, a document hand-edited — is reverted on save by a
    snapshot taken before the call, so the reply silently undoes work that finished first.
    """
    store = ProjectStore(tmp_path)
    project = documented_project(store, "Concurrent")

    def lock_the_treatment(during: Project) -> None:
        during.treatment_locked = True
        during.creative_brief = "Typed while the model was still thinking."

    director = ConcurrentEditDirector(ProjectStore(tmp_path), project.id, lock_the_treatment)
    settings = Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy")
    app = create_app(settings=settings, store=store, comfy=FakeComfy(), director=director)
    client = TestClient(app)

    response = client.post(
        f"/api/projects/{project.id}/director/chat",
        json={"message": "Rewrite it all", "apply_documents": True},
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    # The lock won, even though it did not exist when the call was made.
    assert saved.treatment_locked is True
    assert saved.treatment == project.treatment
    assert saved.treatment_previous == ""
    # And the concurrent hand edit survived rather than being reverted to "".
    assert saved.creative_brief == "Typed while the model was still thinking."
    # The unlocked document still moved, so this is a re-read and not a refusal to apply.
    assert saved.style_bible.startswith("Style bible revision 1")


def test_restore_over_an_empty_document_reports_itself_as_one_way(tmp_path: Path):
    """The reversibility promise has to hold, or be withdrawn where it does not.

    Displacing an empty document leaves an empty slot, which the route must refuse — so this
    restore genuinely cannot be swapped back, in exactly the case where the recovered text
    matters most.
    """
    client, store = make_client_with_director(tmp_path, RefusingDirector())
    project = store.create(Project(name="One way"))
    project.treatment_previous = "The only copy of the treatment, worth getting back."
    store.save(project)

    response = client.post(f"/api/projects/{project.id}/documents/treatment/restore")

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.treatment == "The only copy of the treatment, worth getting back."
    assert saved.treatment_previous == ""
    notice = saved.messages[-1].content
    assert "this restore is one-way" in notice
    assert "swaps back" not in notice
    # And the claim is true: there is nothing to swap back to.
    assert client.post(f"/api/projects/{project.id}/documents/treatment/restore").status_code == 409


def test_a_whitespace_only_kept_version_is_refused_like_an_empty_one(tmp_path: Path):
    """Restoring "   " over real text is data loss wearing a non-empty string."""
    client, store = make_client_with_director(tmp_path, RefusingDirector())
    project = documented_project(store, "Whitespace")
    project.treatment_previous = "   \n\t "
    store.save(project)

    response = client.post(f"/api/projects/{project.id}/documents/treatment/restore")

    assert response.status_code == 409
    assert "nothing to restore" in response.json()["detail"]
    assert ProjectStore(tmp_path).get(project.id).treatment == project.treatment


def test_restore_is_allowed_on_a_locked_document(tmp_path: Path):
    """The decided rule, pinned: a lock stops the Director, not the human who set it.

    `PUT /documents` already accepts hand edits to a locked document for the same reason —
    otherwise fixing one means unlock, save, edit, lock. The lock wording states this scope,
    so the behaviour is documented rather than merely permitted.
    """
    client, store = make_client_with_director(tmp_path, RefusingDirector())
    project = documented_project(store, "Locked restore")
    project.treatment_previous = "The version kept before the lock went on."
    project.treatment_locked = True
    store.save(project)

    response = client.post(f"/api/projects/{project.id}/documents/treatment/restore")

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.treatment == "The version kept before the lock went on."
    assert saved.treatment_previous == project.treatment
    # Restoring does not quietly unlock it.
    assert saved.treatment_locked is True
    # And the lock wording says so, rather than leaving the rule to be discovered.
    assert "restore a kept version" in DOCUMENT_LOCK_NOTICE


def test_restore_rejects_an_unknown_document_and_an_unknown_project(tmp_path: Path):
    """The path segment is a Literal, so a typo is a 422 rather than a 500 or a no-op."""
    client, store = make_client_with_director(tmp_path, RefusingDirector())
    project = documented_project(store, "Bad targets")

    unknown_document = client.post(f"/api/projects/{project.id}/documents/creative_brief/restore")
    unknown_project = client.post("/api/projects/project_deadbeef0000/documents/treatment/restore")

    assert unknown_document.status_code == 422
    assert "treatment" in json.dumps(unknown_document.json())
    assert unknown_project.status_code == 404
    assert unknown_project.json()["detail"] == "Project not found"


def contains_text(payload, needle: str) -> bool:
    """True when `needle` appears inside any string anywhere in `payload`.

    Deliberately not `needle in json.dumps(payload)`, which is what the no-feedback assertion
    used to be. `json.dumps` escapes the quotes that degraded model output is made of, so
    `'[{"style":"moody"}]'` is never a substring of a dump that contains it — the check passed
    for exactly the text it exists to catch, whether or not the text was there.
    """
    if isinstance(payload, str):
        return needle in payload
    if isinstance(payload, dict):
        return any(contains_text(value, needle) for value in payload.values()) or any(
            contains_text(key, needle) for key in payload
        )
    if isinstance(payload, list):
        return any(contains_text(item, needle) for item in payload)
    return False


class RecordingChatDirector(FakeDirector):
    """One reply, fixed at construction, and every `project_context` it was handed.

    The recorded contexts are the only place the "never fed back" invariant can actually be
    read: what the model is sent is not what is stored, and asserting on the stored message
    would prove nothing about the prompt.
    """

    def __init__(self, *, message: str, treatment=None, style_bible=None, shots=()):
        self.contexts: list[dict] = []
        self.message = message
        self.treatment = treatment
        self.style_bible = style_bible
        self.shots = list(shots)

    async def plan(self, message, project_context):
        self.contexts.append(project_context)
        return type(
            "DirectorResult",
            (),
            {
                "message": self.message,
                # `None` means "echo whatever the project holds", which the guard treats as no
                # proposal at all — so a test about one document reports only that document.
                "treatment": (
                    project_context["treatment"] if self.treatment is None else self.treatment
                ),
                "style_bible": (
                    project_context["style_bible"]
                    if self.style_bible is None
                    else self.style_bible
                ),
                "shots": self.shots,
            },
        )()


def test_a_rejected_document_keeps_its_raw_output_out_of_the_reply_and_the_next_context(
    tmp_path: Path,
):
    """The Story 2.2 invariant, extended to the chat route — the one that still had it open.

    The rejection notice used to paste 400 characters of the model's own degraded output into
    the assistant message, and this thread is dumped to the model as context on the next turn.
    So the guard that exists to stop "JSON in context begets JSON" was the thing supplying the
    JSON. Asserted against the recorded context rather than the stored project, because the
    stored project is exactly where the raw text is now *supposed* to be.
    """
    degraded = '[{"style":"moody","color_palette":["amber","teal"]}]'
    director = RecordingChatDirector(
        message="Warmer, and I left the plan alone.", style_bible=degraded
    )
    client, store = make_client_with_director(tmp_path, director)
    project = documented_project(store, "Rejected raw")
    kept = project.style_bible

    first = client.post(
        f"/api/projects/{project.id}/director/chat",
        json={"message": "Moodier", "apply_documents": True},
    )

    assert first.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.style_bible == kept
    assert saved.style_bible_previous == ""
    reply = saved.messages[-1]
    # One notice, carrying the refusal as data rather than as a text convention.
    assert [notice.text for notice in reply.notices] == [
        DOCUMENT_REJECTED_NOTICE.format(
            document=DOCUMENT_LABELS["style_bible"],
            reason="the model returned JSON instead of prose",
        )
    ]
    # Inspectable: the refused text is kept whole, in the field the context dump drops...
    assert reply.notices[0].raw == degraded
    # ...and nowhere in the joined text the thread carries.
    assert degraded not in reply.content
    assert "Raw output:" not in reply.content
    assert reply.content.endswith(reply.notices[0].text)

    second = client.post(
        f"/api/projects/{project.id}/director/chat", json={"message": "Carry on"}
    )

    assert second.status_code == 200
    context = director.contexts[1]
    # Not in the payload the model was handed, in any nesting: not the whole blob, not a
    # fragment of it, and not under a `notices` key that a future field could be added beside.
    assert not contains_text(context, degraded)
    assert not contains_text(context, '"color_palette"')
    assert all("notices" not in message for message in context["messages"])
    # The reply itself is still there, so the model keeps the conversation it is continuing —
    # the exclusion drops the raw output, not the turn.
    carried = [message["content"] for message in context["messages"]]
    assert reply.content in carried
    assert director.contexts[0] != context


def test_the_collapse_floor_is_reached_through_the_route_and_not_only_in_the_unit_test(
    tmp_path: Path,
):
    """The sub-40% floor has never been exercised by a request.

    Every existing double either echoes the stored document or returns a long replacement, so
    the floor was asserted only against `document_rejection` directly — and the route could
    have stopped consulting it for short candidates without a single test noticing.
    """
    collapsed = "Too short."
    director = RecordingChatDirector(message="Tightened it right down.", treatment=collapsed)
    client, store = make_client_with_director(tmp_path, director)
    project = documented_project(store, "Collapse floor")
    kept = project.treatment
    assert len(collapsed) < 0.4 * len(kept)

    response = client.post(
        f"/api/projects/{project.id}/director/chat",
        json={"message": "Cut it back", "apply_documents": True},
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    # Refused: the document stands and the single recovery slot was not spent on the refusal.
    assert saved.treatment == kept
    assert saved.treatment_previous == ""
    notice = saved.messages[-1].notices[0]
    assert notice.text.startswith(f"{DOCUMENT_LABELS['treatment']} was NOT replaced")
    assert f"{len(collapsed)} characters against {len(kept)} existing" in notice.text
    assert "below the 40% floor" in notice.text
    # And the refused text is inspectable rather than described.
    assert notice.raw == collapsed


def test_the_kept_raw_output_is_bounded_because_the_manifest_is_persisted(tmp_path: Path):
    """Inspectable is not unbounded. The reply is written to disk and read back on every load.

    Nothing model-controlled goes into the manifest at whatever length the model chose — the
    rule `_short` already applies to what reaches `content`. This keeps the shape of the output
    rather than collapsing it, because a collapsed blob is a different artefact from the one
    being inspected, so only the length is capped.
    """
    sprawling = '[{"style":"' + "y" * 900 + '"}]'
    director = RecordingChatDirector(message="Here it is.", style_bible=sprawling)
    client, store = make_client_with_director(tmp_path, director)
    project = documented_project(store, "Sprawling raw")

    response = client.post(
        f"/api/projects/{project.id}/director/chat",
        json={"message": "Rewrite it", "apply_documents": True},
    )

    assert response.status_code == 200
    raw = ProjectStore(tmp_path).get(project.id).messages[-1].notices[0].raw
    assert len(sprawling) > NOTICE_RAW_LIMIT
    assert raw == f"{sprawling[:NOTICE_RAW_LIMIT]}…"
    assert len(raw) == NOTICE_RAW_LIMIT + 1


def test_a_refusal_with_nothing_to_show_does_not_offer_an_inspection(tmp_path: Path):
    """A notice must not promise a disclosure that renders empty.

    A blank or whitespace-only candidate is refused by the ratio floor like any other, and
    `MessageNotice` stores blank as blank — so the wording that says the returned text is kept
    for inspection would be offering an empty box. That is the same class of false sentence this
    story rewrote `EXPANSION_REJECTED_NOTICE` to remove, and writing it here while removing it
    there would be no improvement at all.
    """
    director = RecordingChatDirector(message="Cleared it.", treatment="   \n\t ")
    client, store = make_client_with_director(tmp_path, director)
    project = documented_project(store, "Nothing to show")
    kept = project.treatment

    response = client.post(
        f"/api/projects/{project.id}/director/chat",
        json={"message": "Empty it", "apply_documents": True},
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    # Still refused, and the document still stands.
    assert saved.treatment == kept
    notice = saved.messages[-1].notices[0]
    assert notice.kind == "refusal"
    assert notice.raw == ""
    assert notice.text == DOCUMENT_REJECTED_EMPTY_NOTICE.format(
        document=DOCUMENT_LABELS["treatment"],
        reason=f"the replacement is 0 characters against {len(kept)} existing, below the 40% floor",
    )
    # The claim is the thing being pinned: nothing here offers an inspection.
    assert "inspection" not in notice.text
    assert "nothing to inspect" in notice.text


def test_an_expansion_refusal_with_nothing_to_show_makes_no_offer_either(tmp_path: Path):
    """The same rule on the other route, where the blank case is the documented one.

    `expansion_rejection` refuses a blank prompt in exactly those words, so this is the reachable
    half: `ExpandedShot` allows whitespace through `min_length`, and the route must not then
    claim to have kept it.
    """
    director = FixedExpansionDirector([("shot_first", "   \n\t ")])
    client, store, _ = make_client(tmp_path, director)
    project = planned_project(store, "Nothing to show")

    response = client.post(f"/api/projects/{project.id}/director/expand")

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    notice = saved.messages[-1].notices[-1]
    assert notice.kind == "refusal"
    assert notice.raw == ""
    assert notice.text.endswith(EXPANSION_REJECTED_EMPTY_NOTICE.split("{reason}. ")[1])
    assert "inspection" not in notice.text


def test_the_expansion_route_bounds_the_prompt_it_keeps(tmp_path: Path):
    """The cap belongs to the type, and this is the route the type has to cover.

    `ExpandedShot.prompt` carries `min_length` and no upper bound, so nothing outside
    `MessageNotice` stops a refused prompt of any length being written into a manifest that is
    read back on every load. Replacing the chat route's bounded call with an unbounded one used
    to pass the whole suite, because the only test reading this field compared against a
    63-character prompt.
    """
    sprawling = '[{"shot":"' + "y" * 900 + '"}]'
    director = FixedExpansionDirector([("shot_first", sprawling)])
    client, store, _ = make_client(tmp_path, director)
    project = planned_project(store, "Sprawling prompt")

    response = client.post(f"/api/projects/{project.id}/director/expand")

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.shots[0].prompt == "New shot"
    raw = saved.messages[-1].notices[-1].raw
    assert len(sprawling) > NOTICE_RAW_LIMIT
    assert raw == f"{sprawling[:NOTICE_RAW_LIMIT]}…"
    # And the manifest on disk carries no more than that, which is the whole argument.
    assert sprawling not in store.manifest_path(project.id).read_text(encoding="utf-8")


def test_a_reply_with_no_message_of_its_own_is_not_stored_as_a_bare_separator_either(
    tmp_path: Path,
):
    """The expansion route's guard, on the route that never had it.

    `DirectorResult.message` has no floor and deliberately keeps none — an empty sentence is not
    a reason to fail a turn that legitimately replaced a document. Without a fallback the stored
    reply begins with `\\n\\n---\\n`, and that reply is context for the next call.
    """
    director = RecordingChatDirector(message="   \n ", treatment="A genuinely new treatment, long enough to clear the ratio floor comfortably.")
    client, store = make_client_with_director(tmp_path, director)
    project = documented_project(store, "No message")

    response = client.post(
        f"/api/projects/{project.id}/director/chat",
        json={"message": "Rewrite it", "apply_documents": True},
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    reply = saved.messages[-1]
    assert reply.content.startswith(CHAT_EMPTY_MESSAGE)
    assert not reply.content.startswith("\n")
    # The turn still did its work; only the missing sentence was substituted.
    assert saved.treatment.startswith("A genuinely new treatment")
    assert [notice.kind for notice in reply.notices] == ["change"]


def test_every_notice_says_what_it_is_about_so_good_news_is_not_dressed_as_caution(
    tmp_path: Path,
):
    """A confirmation carried the same chrome as a refusal, which is how caution stops working.

    Both routes are driven here, because the worst instance is on the expansion route: "Prompts
    written for 2 shot(s)" is the thing the Director pressed the button for.
    """
    chat = RecordingChatDirector(
        message="Rewritten.",
        treatment="A genuinely new treatment, long enough to clear the ratio floor comfortably.",
        style_bible='[{"style":"moody"}]',
    )
    client, store = make_client_with_director(tmp_path, chat)
    project = documented_project(store, "Kinds")

    assert client.post(
        f"/api/projects/{project.id}/director/chat",
        json={"message": "Rework it", "apply_documents": True},
    ).status_code == 200

    replied = ProjectStore(tmp_path).get(project.id).messages[-1]
    assert [(notice.kind, notice.text.split(":")[0]) for notice in replied.notices] == [
        ("change", "Replaced by this reply"),
        ("refusal", f"{DOCUMENT_LABELS['style_bible']} was NOT replaced"),
    ]

    # The other two chat wordings, which cannot occur in the reply above: a document filled from
    # blank is a change, and a lock is a refusal, and they can only be told apart per document.
    locked_director = RecordingChatDirector(
        message="Filled the blank one.",
        treatment="A rewrite of the locked document, long enough to clear the ratio floor.",
        style_bible="The first style bible this project has ever had, written out at length.",
    )
    client, store = make_client_with_director(tmp_path, locked_director)
    mixed = store.create(Project(name="Locked and blank"))
    mixed.treatment = "The original treatment, written by hand over several sessions."
    mixed.treatment_locked = True
    store.save(mixed)

    assert client.post(
        f"/api/projects/{mixed.id}/director/chat",
        json={"message": "Both", "apply_documents": True},
    ).status_code == 200

    mixed_reply = ProjectStore(tmp_path).get(mixed.id).messages[-1]
    assert [(notice.kind, notice.text) for notice in mixed_reply.notices] == [
        ("change", document_first_draft_notice([DOCUMENT_LABELS["style_bible"]])),
        ("refusal", DOCUMENT_LOCK_NOTICE.format(document=DOCUMENT_LABELS["treatment"])),
    ]

    expanding = FixedExpansionDirector(
        [("shot_first", "A corridor, widening."), ("shot_second", '[{"json":"instead"}]')]
    )
    client, store, _ = make_client(tmp_path, expanding)
    plan = planned_project(store, "Kinds too")

    assert client.post(f"/api/projects/{plan.id}/director/expand").status_code == 200

    expanded = ProjectStore(tmp_path).get(plan.id).messages[-1]
    assert [notice.kind for notice in expanded.notices] == ["change", "refusal"]
    written = expanded.notices[0]
    assert written.text.startswith("Prompts written for 1 shot(s)")
    # The confirmation is not a caution, whatever the client does with the two.
    assert written.kind != "refusal"
    assert written.raw == ""
    # And every notice either route produces carries one of the three kinds, never a default.
    for notice in (*replied.notices, *expanded.notices):
        assert notice.kind in get_args(NoticeKind), notice


class RealResultDirector(FakeDirector):
    """Returns an actual `DirectorResult`, not a duck-typed stand-in.

    Every other double in this file builds `type("DirectorResult", (), {...})`, which is quick to
    write and cannot fail validation — so a renamed field, a newly required one, or a tightened
    constraint on `PlannedShot` would leave the whole route suite green while the real client
    raised on the first real reply.
    """

    def __init__(self, result: DirectorResult):
        self.result = result

    async def plan(self, message, project_context):
        return self.result


class RealExpansionDirector(FakeDirector):
    """The same, for `ShotExpansion` — the model the expansion route actually receives."""

    def __init__(self, expansion: ShotExpansion):
        self.expansion = expansion

    async def expand(self, expansion_input):
        return self.expansion


def test_the_chat_route_handles_the_real_result_model_and_not_only_a_stand_in(tmp_path: Path):
    """One route test per Director model, built through the model's own validation."""
    result = DirectorResult(
        message="One continuous move, and I have rewritten the treatment under it.",
        treatment="A single unbroken movement through the room, written out at length.",
        style_bible="Cold blue, handheld, 28mm, hard sodium spill through every doorway.",
        shots=[PlannedShot(start=0, duration=20, prompt="One long take")],
    )
    client, store = make_client_with_director(tmp_path, RealResultDirector(result))
    project = documented_project(store, "Real result")

    response = client.post(
        f"/api/projects/{project.id}/director/chat",
        json={"message": "One take", "apply_shots": True, "apply_documents": True},
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.treatment == result.treatment
    assert [shot.duration for shot in saved.shots] == [20]
    assert [(notice.kind, notice.text) for notice in saved.messages[-1].notices] == [
        ("change", document_change_notice([DOCUMENT_LABELS[field] for field in DOCUMENT_LABELS])),
        ("flag", SHOT_WINDOW_NOTICE.format(duration=20, start=0, minimum=4, maximum=15)),
    ]


def test_the_expansion_route_handles_the_real_expansion_model_and_not_only_a_stand_in(
    tmp_path: Path,
):
    """`ShotExpansion` and `ExpandedShot`, constructed rather than imitated."""
    expansion = ShotExpansion(
        message="Two prompts, one refused.",
        shots=[
            ExpandedShot(shot_id="shot_first", prompt="A corridor, widening ahead of the singer."),
            ExpandedShot(shot_id="shot_second", prompt='[{"camera":"push in"}]'),
        ],
    )
    client, store, _ = make_client(tmp_path, RealExpansionDirector(expansion))
    project = planned_project(store, "Real expansion")

    response = client.post(f"/api/projects/{project.id}/director/expand")

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.shots[0].prompt == expansion.shots[0].prompt
    assert saved.shots[1].prompt == ""
    assert [notice.kind for notice in saved.messages[-1].notices] == ["change", "refusal"]
    assert saved.messages[-1].notices[-1].raw == expansion.shots[1].prompt


def test_a_full_project_save_cannot_write_the_thread_or_invent_a_notice(tmp_path: Path):
    """The sibling-route hole the recovery slots had, in its thread form.

    `replace_project` binds a whole client `Project`, and the chat thread is now the carrier of
    every protective refusal this application makes. A body that *invents* a notice would be
    planting a refusal that never happened; one that rewords a real one would rewrite the reason
    a guard gave; and one that simply omits the field — which is what any client written before
    notices existed sends — would revert every notice in the project to undifferentiated prose.
    Nothing in this application posts a message: the chat, expansion and restore routes are the
    only writers.
    """
    degraded = '[{"style":"moody","color_palette":["amber"]}]'
    director = RecordingChatDirector(message="Warmer.", style_bible=degraded)
    client, store = make_client_with_director(tmp_path, director)
    project = documented_project(store, "Thread save")

    assert client.post(
        f"/api/projects/{project.id}/director/chat",
        json={"message": "Moodier", "apply_documents": True},
    ).status_code == 200

    body = client.get(f"/api/projects/{project.id}").json()
    stored = ProjectStore(tmp_path).get(project.id)
    body["messages"][-1]["notices"] = [
        {"kind": "change", "text": "Everything was applied exactly as you asked.", "raw": ""}
    ]
    body["messages"].append(
        {"id": "msg_forged", "role": "assistant", "content": "Forged.", "notices": []}
    )
    body["creative_brief"] = "Edited by a client that also rewrote the thread."

    response = client.put(f"/api/projects/{project.id}", json=body)

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    # The ordinary edit lands...
    assert saved.creative_brief == "Edited by a client that also rewrote the thread."
    # ...and nothing about the thread does.
    assert [message.id for message in saved.messages] == [
        message.id for message in stored.messages
    ]
    assert saved.messages[-1].notices == stored.messages[-1].notices
    assert saved.messages[-1].notices[0].kind == "refusal"
    assert saved.messages[-1].notices[0].raw == degraded
    assert "Forged." not in json.dumps(saved.model_dump(mode="json"))

    # And a body that predates notices entirely cannot strip them either.
    stripped = client.get(f"/api/projects/{project.id}").json()
    for message in stripped["messages"]:
        del message["notices"]
    assert client.put(f"/api/projects/{project.id}", json=stripped).status_code == 200
    assert ProjectStore(tmp_path).get(project.id).messages[-1].notices[0].raw == degraded


def test_a_reply_that_describes_shots_while_returning_none_is_reported_as_a_mismatch(
    tmp_path: Path,
):
    """FR-15's last clause, and the half of the recorded defect nothing ever reported.

    The reproduced failure returned `shots: []` under prose describing a four-beat sequence, so
    the Director was told a plan had been made and no plan existed. The check is ungated on
    `apply_shots` on purpose: the browser hardcodes that flag to `false`, so gating it there
    would put the notice where no Director can reach it.

    The wording is chosen from what the project actually holds. This one has no shots, so being
    told "the existing shots are unchanged" would describe a timeline that does not exist, in
    the one reply that has just led the Director to believe it does.
    """
    director = RecordingChatDirector(message="Splitting your vision into a four-beat sequence.")
    client, store = make_client_with_director(tmp_path, director)
    project = documented_project(store, "Claimed plan")

    response = client.post(
        f"/api/projects/{project.id}/director/chat", json={"message": "Break it into beats"}
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.shots == []
    assert [notice.text for notice in saved.messages[-1].notices] == [
        SHOT_CLAIM_WITHOUT_ANY_SHOTS_NOTICE
    ]
    # Nothing was refused and nothing was written, so it is a flag rather than a refusal: the
    # amber chrome a refusal carries would be the wrong signal here and on every reply like it.
    assert [notice.kind for notice in saved.messages[-1].notices] == ["flag"]
    # Reported, never applied, and never a block: the reply is stored as usual.
    assert saved.messages[-1].content.startswith(director.message)


def test_a_plan_that_already_exists_is_told_its_shots_are_unchanged(tmp_path: Path):
    """The other half of the wording, and the reason the check reads `project.shots` at all.

    A project with a timeline can be reassured that the timeline was not touched. A project
    without one cannot, and the previous single wording told both the same thing.
    """
    director = RecordingChatDirector(message="I have written four shots across the second verse.")
    client, store = make_client_with_director(tmp_path, director)
    project = planned_project(store, "Existing plan")
    before = shots_snapshot(project)

    response = client.post(
        f"/api/projects/{project.id}/director/chat", json={"message": "Rework the verse"}
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert [notice.text for notice in saved.messages[-1].notices] == [SHOT_CLAIM_MISMATCH_NOTICE]
    # And the sentence is true: the plan it describes as unchanged really is unchanged.
    assert shots_snapshot(saved) == before


def test_ordinary_talk_about_shots_is_not_read_as_a_claim(tmp_path: Path):
    """The boundary, exercised where it actually sits.

    The first cut matched `\\bshots?\\b` anywhere, so "I did not add any shots" and "the shots
    you have are fine" both produced the notice — on a project whose timeline the Director was
    only discussing. A caution that fires on ordinary conversation is one that gets scrolled
    past, which is the failure this whole story is about. The negative cases here all contain
    the vocabulary on purpose; a reply with no shot words in it would prove nothing.
    """
    quiet = [
        "I did not add any shots; the pacing question comes first.",
        "The shots you have are fine, so I left them alone.",
        "Nothing here writes shots — tell me when you want the plan.",
        "I have written the treatment in two parts and a short coda.",
        "Your four shots read well against the second verse.",
        "There is no shot list yet, and I would not guess at one.",
        "The style bible now covers one section on wardrobe and one on light.",
    ]
    loud = [
        "Splitting your vision into a four-beat sequence.",
        "I have written four shots for the second verse.",
        "Here are the shots, cut to the chorus.",
        "I broke the song into a six-part sequence.",
        # A denial in a *different* sentence silences that sentence and no other. Scanning the
        # whole message at once would let one unrelated "nothing" hide a real claim beside it.
        "I have written four shots for the second verse. Nothing else changed.",
    ]

    for message in quiet:
        assert prose_claims_shots(message) is False, message
    for message in loud:
        assert prose_claims_shots(message) is True, message

    # And the quiet half really does reach the route without producing a notice, rather than
    # only satisfying the predicate in isolation.
    director = RecordingChatDirector(message=quiet[1])
    client, store = make_client_with_director(tmp_path, director)
    project = planned_project(store, "Quiet talk")

    response = client.post(
        f"/api/projects/{project.id}/director/chat", json={"message": "How is the plan?"}
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.messages[-1].notices == []
    assert saved.messages[-1].content == quiet[1]


def test_the_consent_and_mismatch_notices_are_independent_facts(tmp_path: Path):
    """Both can be true of one reply, and each answers a question the other does not.

    One says the consent the Director gave produced nothing; the other says the reply
    contradicts itself. Suppressing either would leave that fact unsaid in the exact turn it is
    about — and the empty-list notice is gated on `apply_shots`, which the browser never sets,
    so the mismatch is the only one a Director reaches from the UI.
    """
    director = RecordingChatDirector(message="I have written four shots for the second verse.")
    client, store = make_client_with_director(tmp_path, director)
    project = documented_project(store, "Both notices")

    response = client.post(
        f"/api/projects/{project.id}/director/chat",
        json={"message": "Plan it", "apply_shots": True},
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert [notice.text for notice in saved.messages[-1].notices] == [
        SHOT_CLAIM_WITHOUT_ANY_SHOTS_NOTICE,
        SHOT_PLAN_EMPTY_NOTICE,
    ]
    assert saved.shots == []


def test_a_manifest_written_before_notices_existed_loads_and_saves_unchanged(tmp_path: Path):
    """Every field added after the fact is defaulted, and this is the one that reaches the thread.

    A project saved before this story has messages with no `notices` key at all. It must load,
    render as prose, and survive the next reply being appended to it.
    """
    director = RecordingChatDirector(message="Carrying on from before.")
    client, store = make_client_with_director(tmp_path, director)
    project = documented_project(store, "Old manifest")
    project.messages.append(TreatmentMessage(role="assistant", content="A reply from before."))
    store.save(project)

    manifest = store.manifest_path(project.id)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    for message in payload["messages"]:
        del message["notices"]
    manifest.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    loaded = client.get(f"/api/projects/{project.id}")
    appended = client.post(
        f"/api/projects/{project.id}/director/chat", json={"message": "Carry on"}
    )

    assert loaded.status_code == 200
    assert [message["notices"] for message in loaded.json()["messages"]] == [[]]
    assert loaded.json()["messages"][0]["content"] == "A reply from before."
    assert appended.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.messages[0].content == "A reply from before."
    assert saved.messages[0].notices == []


def expansion_result(message: str, shots: list[tuple[str, str]]):
    """A `ShotExpansion`-shaped reply, in the duck-typed style the other doubles use."""
    return type(
        "ShotExpansion",
        (),
        {
            "message": message,
            "shots": [
                type("ExpandedShot", (), {"shot_id": shot_id, "prompt": prompt})()
                for shot_id, prompt in shots
            ],
        },
    )()


class ExpandingDirector(FakeDirector):
    """Answers every Shot in the input it was handed, keyed by that Shot's own id.

    Records each input, which is the only way to assert what actually reached the model —
    the `RevisingDirector.contexts` pattern, applied to the expansion call.
    """

    def __init__(self):
        self.inputs: list[dict] = []

    async def expand(self, expansion_input):
        self.inputs.append(expansion_input)
        return expansion_result(
            "Held identity, wardrobe, palette and lens; moved action, framing and energy.",
            [
                (entry["shot_id"], f"Prompt for {entry['shot_id']} at index {entry['index']}")
                for entry in expansion_input["shots"]
            ],
        )


class FixedExpansionDirector(FakeDirector):
    """Returns a fixed result whatever the input says — the model going its own way."""

    def __init__(self, shots: list[tuple[str, str]], message: str = "Expanded the plan."):
        self.shots = shots
        self.message = message
        self.inputs: list[dict] = []

    async def expand(self, expansion_input):
        self.inputs.append(expansion_input)
        return expansion_result(self.message, self.shots)


class RecordingExpansionDirector(RevisingDirector):
    """A fixed expansion, plus `RevisingDirector`'s recording of every chat `project_context`.

    Both halves in one double because the guarantee spans them: what an expansion writes into
    the thread is what the *next* chat turn hands the model.
    """

    def __init__(self, shots: list[tuple[str, str]]):
        super().__init__()
        self.shots = shots

    async def expand(self, expansion_input):
        return expansion_result("Expanded the plan.", self.shots)


class UnavailableExpansionDirector(FakeDirector):
    async def expand(self, expansion_input):
        raise DirectorUnavailable(
            "LLM director is not configured. Set MVP_LLM_BASE_URL and MVP_LLM_MODEL."
        )


class FailingExpansionDirector(FakeDirector):
    async def expand(self, expansion_input):
        raise DirectorError("LLM director returned an invalid response: Expecting value")


def planned_project(store: ProjectStore, name: str, *, song: Song | None = None) -> Project:
    """A timed, unprompted plan: two draft Shots that expansion may legitimately write."""
    project = store.create(Project(name=name))
    project.treatment = "Three movements: the corridor, the threshold, the desert."
    project.style_bible = "Sodium amber, hard backlight, 35mm grain."
    project.song = song
    project.shots = [
        Shot(id="shot_first", start=0, duration=5, prompt="New shot", status="draft"),
        Shot(id="shot_second", start=10, duration=6, prompt="", status="draft"),
    ]
    store.save(project)
    return store.get(project.id)


def give_render_provenance(shot: Shot) -> Shot:
    """Make a Shot one whose prompt a render and an approved take already depend on."""
    shot.status = "complete"
    shot.prompt_id = "render-1"
    shot.latest_output = "takes/one.mp4"
    shot.approved_output = "takes/one.mp4"
    return shot


def test_expansion_writes_a_prompt_for_every_shot_keyed_by_id_and_queues_nothing(tmp_path: Path):
    """FR-26's core: a timed plan becomes a fully prompted one, and nothing is rendered.

    Keyed by id on both sides, so the assertion is per Shot rather than per position — the
    whole point of the result model carrying `shot_id` at all.
    """
    director = ExpandingDirector()
    client, store, comfy = make_client(tmp_path, director)
    project = planned_project(store, "Expanded", song=Song(title="Spine", source="imported", duration=120))

    response = client.post(f"/api/projects/{project.id}/director/expand")

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    prompts = {shot.id: shot.prompt for shot in saved.shots}
    assert prompts["shot_first"] == "Prompt for shot_first at index 0"
    assert prompts["shot_second"] == "Prompt for shot_second at index 1"
    # Prompts only: no window moved, no status advanced, no render id invented.
    assert [(shot.start, shot.duration) for shot in saved.shots] == [(0, 5), (10, 6)]
    assert [shot.status for shot in saved.shots] == ["draft", "draft"]
    assert [shot.prompt_id for shot in saved.shots] == ["", ""]
    # No render, ever. This is the assertion the "Never" line of the spec is about.
    assert comfy.prompts == []
    assert saved.jobs == []
    # One assistant message, saying what changed and naming the shots it changed.
    assert saved.messages[-1].role == "assistant"
    notice = saved.messages[-1].content
    assert notice.startswith("Held identity, wardrobe, palette and lens")
    assert "Prompts written for 2 shot(s)" in notice
    for shot_id in ("shot_first", "shot_second"):
        assert shot_id in notice, shot_id
    assert len([message for message in saved.messages if message.role == "user"]) == 0


def test_expansion_leaves_a_locked_shot_alone_and_reports_it(tmp_path: Path):
    """`Shot.locked` is the Director's "do not touch this", and expansion honours it."""
    director = ExpandingDirector()
    client, store, comfy = make_client(tmp_path, director)
    project = planned_project(store, "Locked")
    project.shots[0].locked = True
    project.shots[0].prompt = "The prompt the Director wrote by hand."
    store.save(project)

    response = client.post(f"/api/projects/{project.id}/director/expand")

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.shots[0].prompt == "The prompt the Director wrote by hand."
    assert saved.shots[0].locked is True
    # The unlocked shot still moves, so the lock is per Shot and not a whole-plan veto.
    assert saved.shots[1].prompt == "Prompt for shot_second at index 1"
    notice = saved.messages[-1].content
    assert "Left unchanged because they are locked" in notice
    assert "shot_first" in notice.split("Left unchanged because they are locked")[1]
    # A locked Shot the model *did* answer is not also reported as omitted.
    assert "Omitted by the model" not in notice
    assert comfy.prompts == []


def test_expansion_discards_a_prompt_addressed_to_an_unknown_shot(tmp_path: Path):
    """Reported, never guessed at and never created as a new Shot.

    Creating one would invent a window this route has no business choosing, and matching it
    positionally is the exact silent misassignment keying by id exists to prevent.
    """
    director = FixedExpansionDirector(
        [
            ("shot_first", "A written prompt for the first shot."),
            ("shot_ghost", "A prompt for a shot that does not exist."),
            ("shot_second", "A written prompt for the second shot."),
        ]
    )
    client, store, comfy = make_client(tmp_path, director)
    project = planned_project(store, "Unknown id")

    response = client.post(f"/api/projects/{project.id}/director/expand")

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert [shot.id for shot in saved.shots] == ["shot_first", "shot_second"]
    assert saved.shots[0].prompt == "A written prompt for the first shot."
    assert saved.shots[1].prompt == "A written prompt for the second shot."
    assert "does not exist" not in json.dumps(saved.model_dump(mode="json")).replace(
        saved.messages[-1].content, ""
    )
    notice = saved.messages[-1].content
    assert "Discarded" in notice
    assert "shot_ghost" in notice
    assert comfy.prompts == []


def test_expansion_reports_a_shot_the_model_omitted_and_keeps_its_prompt(tmp_path: Path):
    """An omission is reported rather than retried, and costs the Shot nothing."""
    director = FixedExpansionDirector([("shot_first", "A written prompt for the first shot.")])
    client, store, _ = make_client(tmp_path, director)
    project = planned_project(store, "Omitted")
    project.shots[1].prompt = "The prompt the second shot already had."
    store.save(project)

    response = client.post(f"/api/projects/{project.id}/director/expand")

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.shots[0].prompt == "A written prompt for the first shot."
    assert saved.shots[1].prompt == "The prompt the second shot already had."
    notice = saved.messages[-1].content
    assert "Omitted by the model" in notice
    assert "shot_second" in notice.split("Omitted by the model")[1]
    assert "Prompts written for 1 shot(s)" in notice


def test_expansion_does_not_apply_a_prompt_that_parses_as_json(tmp_path: Path):
    """The FR-15 degradation, in its prompt form: JSON in context begets JSON.

    The ratio floor is deliberately not in play — an unexpanded Shot's prompt is `""` or the
    "New shot" placeholder, so the floor is toothless there and would refuse legitimate first
    prompts elsewhere. Only the JSON-as-prose check decides.
    """
    degraded = '[{"shot":"corridor","camera":"push in"}]'
    director = FixedExpansionDirector(
        [("shot_first", degraded), ("shot_second", "Real prose about the threshold.")]
    )
    client, store, _ = make_client(tmp_path, director)
    project = planned_project(store, "Degraded prompt")

    response = client.post(f"/api/projects/{project.id}/director/expand")

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.shots[0].prompt == "New shot"
    assert saved.shots[1].prompt == "Real prose about the threshold."
    notice = saved.messages[-1].content
    assert "NOT applied to" in notice
    assert "the model returned JSON instead of prose" in notice
    assert "Prompts written for 1 shot(s)" in notice
    # The refused text is never in the reply's own words -- it travels in the notice's `raw`,
    # which the Director's context dump excludes. See the dedicated test below for why.
    assert degraded not in notice
    assert saved.messages[-1].notices[-1].raw == degraded


def test_expansion_never_rewrites_a_shot_a_render_or_take_already_depends_on(tmp_path: Path):
    """Provenance loss of the same class the document recovery slots exist to prevent.

    An approved take was produced *from* a prompt. Rewriting that prompt in place fails nothing
    and afterwards the take and the prompt beside it simply disagree — and an in-flight render's
    prompt would diverge from what was actually submitted. Each of the four markers is asserted
    on its own, because any one of them alone is enough to make the prompt a record.
    """
    for label, mark in (
        ("approved", lambda shot: setattr(shot, "approved_output", "takes/one.mp4")),
        ("rendered", lambda shot: setattr(shot, "latest_output", "takes/one.mp4")),
        ("submitted", lambda shot: setattr(shot, "prompt_id", "render-1")),
        ("ready", lambda shot: setattr(shot, "status", "ready")),
    ):
        root = tmp_path / label
        client, store, comfy = make_client(root, ExpandingDirector())
        project = planned_project(store, f"Provenance {label}")
        project.shots[0].prompt = "The prompt the approved take was produced from."
        mark(project.shots[0])
        store.save(project)

        response = client.post(f"/api/projects/{project.id}/director/expand")

        assert response.status_code == 200, label
        saved = ProjectStore(root).get(project.id)
        assert saved.shots[0].prompt == "The prompt the approved take was produced from.", label
        # The draft shot beside it still moves, so this is per Shot and not a whole-plan veto.
        assert saved.shots[1].prompt == "Prompt for shot_second at index 1", label
        notice = saved.messages[-1].content
        assert "a render or a take already depends on the prompt" in notice, label
        assert "shot_first" in notice.split("already depends on the prompt")[1], label
        assert comfy.prompts == [], label


def test_a_locked_or_rendered_shot_the_model_skipped_is_not_reported_as_an_omission(
    tmp_path: Path,
):
    """"Run expansion again if you want them written" must never be advice that cannot work.

    A locked Shot and one carrying a take are both never written, whatever the model returns —
    so listing them among the omissions tells the Director to retry for an outcome no retry can
    produce. A genuinely omitted draft Shot is still reported.
    """
    director = FixedExpansionDirector([])
    client, store, _ = make_client(tmp_path, director)
    project = planned_project(store, "Skipped and omitted")
    project.shots[0].locked = True
    project.shots.append(give_render_provenance(Shot(id="shot_taken", start=20, duration=5, prompt="Taken")))
    project.shots.append(Shot(id="shot_draft", start=30, duration=5, prompt="Draft"))
    store.save(project)

    response = client.post(f"/api/projects/{project.id}/director/expand")

    assert response.status_code == 200
    notice = ProjectStore(tmp_path).get(project.id).messages[-1].content
    omissions = notice.split("Omitted by the model")[1]
    assert "shot_second" in omissions
    assert "shot_draft" in omissions
    # Neither the lock nor the take is an omission, and neither is claimed as one.
    assert "shot_first" not in omissions
    assert "shot_taken" not in omissions
    # The model answered for nothing at all, so nothing is claimed as written either.
    assert "Prompts written for" not in notice


def test_a_shot_answered_twice_keeps_the_first_prompt_and_the_contradiction_is_reported(
    tmp_path: Path,
):
    """One Shot cannot have two prompts, and last-write-wins is not a decision.

    It also lets one Shot be reported as refused *and* written in the same reply — the reply
    contradicting itself about what it did, which is worse than either outcome.
    """
    director = FixedExpansionDirector(
        [
            ("shot_first", "The first answer, which is the one that counts."),
            ("shot_first", '[{"second":"answer"}]'),
            ("shot_first", "A third answer, later still."),
            ("shot_second", "The only answer for the second shot."),
        ]
    )
    client, store, _ = make_client(tmp_path, director)
    project = planned_project(store, "Answered twice")

    response = client.post(f"/api/projects/{project.id}/director/expand")

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.shots[0].prompt == "The first answer, which is the one that counts."
    assert saved.shots[1].prompt == "The only answer for the second shot."
    notice = saved.messages[-1].content
    assert "answered more than once" in notice
    assert "shot_first" in notice.split("answered more than once")[1]
    # Written exactly once, and never also reported as refused: the later JSON answer was
    # ignored before the guard ever saw it.
    assert notice.count("Prompts written for 2 shot(s)") == 1
    assert "NOT applied to" not in notice
    # And the contradiction is reported once, not once per repeat.
    assert notice.count("answered more than once") == 1


def test_a_repeated_unknown_id_is_reported_once_and_truncated(tmp_path: Path):
    """The thread is context for the next call, so nothing model-controlled goes in unbounded.

    A model looping on one bad id used to repeat it through the whole notice, and a `shot_id`
    is whatever the model emitted — it can be a paragraph, or carry newlines that break the
    notice apart.
    """
    sprawling = "shot_" + "x" * 300 + "\nand a second line"
    director = FixedExpansionDirector(
        [("shot_ghost", "One."), ("shot_ghost", "Two."), (sprawling, "Three.")]
    )
    client, store, _ = make_client(tmp_path, director)
    project = planned_project(store, "Sprawling id")

    response = client.post(f"/api/projects/{project.id}/director/expand")

    assert response.status_code == 200
    notice = ProjectStore(tmp_path).get(project.id).messages[-1].content
    assert "addressed to 2 id(s)" in notice
    assert notice.count("shot_ghost") == 1
    assert sprawling not in notice
    assert "…" in notice
    # The newline never reaches the thread, so the notice stays one readable paragraph.
    assert "and a second line" not in notice


def test_a_refused_prompt_is_reported_without_feeding_itself_back_into_the_next_call(
    tmp_path: Path,
):
    """The guard that catches "JSON in context begets JSON" must not be what supplies the JSON.

    The chat thread is dumped to the model as context on the next Director turn, so a rejection
    notice that quoted the degraded output would persist it into exactly the context whose
    richness produced it. Asserted end to end: refuse, then take a chat turn and read what
    actually reached the model.

    Narrowed from the whole project dump to the context dump when the notice gained a `raw`
    field: the refused text is now deliberately kept in the project, so "absent from the
    manifest" is no longer the claim. "Absent from what the model is handed" always was.
    """
    degraded = '[{"shot":"corridor","camera":"push in","palette":["amber","teal"]}]'
    director = RecordingExpansionDirector([("shot_first", degraded)])
    client, store, _ = make_client(tmp_path, director)
    project = planned_project(store, "No feedback loop")

    assert client.post(f"/api/projects/{project.id}/director/expand").status_code == 200

    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.shots[0].prompt == "New shot"
    reply = saved.messages[-1]
    # Reported, and the reason is named -- the Director still learns what happened.
    assert "NOT applied to" in reply.content
    assert "the model returned JSON instead of prose" in reply.content
    assert reply.content.endswith("produces more of it.")
    # The refused prompt is kept for inspection -- restored by Story 2.4, having been dropped
    # in 2.2 only because there was nowhere to put it that the model would not read back.
    assert reply.notices[-1].raw == degraded
    # But never in the reply's own text, which is what the thread carries into the prompt.
    assert degraded not in reply.content

    # And it is therefore not in what the next Director call is handed.
    assert client.post(
        f"/api/projects/{project.id}/director/chat", json={"message": "Carry on"}
    ).status_code == 200
    assert not contains_text(director.contexts[0], degraded)
    assert not contains_text(director.contexts[0], '"camera":"push in"')
    assert all("notices" not in message for message in director.contexts[0]["messages"])


def test_deleting_every_shot_during_the_call_is_refused_rather_than_recorded(tmp_path: Path):
    """The pre-call guard has to hold after the re-read too.

    A plan can be emptied while the model is thinking. Saving a reply about it would leave the
    thread asserting an expansion of a project that has nothing to expand — the exact state the
    422 exists to refuse.
    """
    store = ProjectStore(tmp_path)
    project = planned_project(store, "Emptied mid-call")

    class DeletingExpansionDirector(FakeDirector):
        async def expand(self, expansion_input):
            during = ProjectStore(tmp_path).get(project.id)
            during.shots = []
            ProjectStore(tmp_path).save(during)
            return expansion_result("Expanded the plan I was given.", [("shot_first", "A prompt.")])

    settings = Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy")
    client = TestClient(
        create_app(
            settings=settings,
            store=store,
            comfy=FakeComfy(),
            director=DeletingExpansionDirector(),
        )
    )

    response = client.post(f"/api/projects/{project.id}/director/expand")

    assert response.status_code == 422
    assert "no shots to expand" in response.json()["detail"]
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.shots == []
    # Nothing recorded about a plan that no longer exists, and the deletion is not undone.
    assert saved.messages == []


def test_a_reply_with_no_message_of_its_own_is_not_stored_as_a_bare_separator(tmp_path: Path):
    """`ShotExpansion.message` has no `min_length` on purpose, so the route carries the floor.

    A missing sentence is not a reason to throw away a whole set of good prompts with a 502 —
    but it must not leave the thread holding a separator and a notice with nothing above them.
    """
    director = FixedExpansionDirector([("shot_first", "A written prompt.")], message="   ")
    client, store, _ = make_client(tmp_path, director)
    project = planned_project(store, "No message")

    response = client.post(f"/api/projects/{project.id}/director/expand")

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    # The prompts still landed: the empty sentence cost nothing.
    assert saved.shots[0].prompt == "A written prompt."
    content = saved.messages[-1].content
    assert not content.startswith("\n\n---\n")
    assert content.startswith("The Director returned no summary")


def test_expansion_applies_a_shorter_prompt_rather_than_holding_it_to_the_document_floor(
    tmp_path: Path,
):
    """`expansion_rejection` passes "" as the existing text deliberately, and this pins it.

    Passing the Shot's real prompt instead would bring `document_rejection`'s 40% ratio floor
    into play, where it is exactly wrong: on an unexpanded Shot the existing text is `""` or
    `"New shot"`, so the floor is toothless, while on a Shot with a long hand-written prompt it
    would refuse a tighter, better one. Every other test here uses a placeholder prompt, so
    that mutation survives all of them.
    """
    existing = (
        "A long hand-written prompt about the corridor, its sodium amber practicals, the "
        "performer's silver jacket, the slow push on a 35mm lens, and the way the light falls "
        "across the wall as the camera moves past each doorway in turn."
    )
    tighter = "Slow push down the amber corridor, silver jacket, 35mm."
    assert len(tighter) < 0.4 * len(existing)
    director = FixedExpansionDirector([("shot_first", tighter)])
    client, store, _ = make_client(tmp_path, director)
    project = planned_project(store, "Tighter prompt")
    project.shots[0].prompt = existing
    store.save(project)

    response = client.post(f"/api/projects/{project.id}/director/expand")

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.shots[0].prompt == tighter
    assert "NOT applied to" not in saved.messages[-1].content


def test_expansion_rejection_refuses_only_what_it_must():
    """The decision itself, without a route: the JSON check and nothing else.

    The accepted branch is the one no route test reaches — prose that merely *starts* with a
    brace is not JSON, and refusing it would silently drop legitimate prompts written in a
    bracketed house style.
    """
    from music_video_producer.app import expansion_rejection

    assert "JSON" in expansion_rejection('[{"style":"moody"}]')
    assert "JSON" in expansion_rejection('  {"prompt": "a corridor"}  ')
    assert "empty" in expansion_rejection("")
    assert "empty" in expansion_rejection("   \n\t ")
    # Accepted: prose that opens with a bracket, and a short prompt against any existing text.
    assert expansion_rejection("[Opening] Slow push down the amber corridor.") == ""
    assert expansion_rejection("{not json, just a stylised opening") == ""
    assert expansion_rejection("Tight.") == ""


def test_the_reply_numbers_a_shot_the_way_the_model_was_told_to(tmp_path: Path):
    """One number per Shot, across the input and the reply.

    `expansion_input` orders by `start`; the manifest is in whatever order shots were added.
    Numbering the notices by the manifest would tell the model "index 1" for the Shot the reply
    calls something else — two schemes for one Shot, in a reply the Director reads beside the
    timeline. The project here is deliberately built out of time order.
    """
    director = ExpandingDirector()
    client, store, _ = make_client(tmp_path, director)
    project = store.create(Project(name="Out of order"))
    project.shots = [
        Shot(id="shot_late", start=90, duration=5, prompt="Desert", locked=True),
        Shot(id="shot_early", start=0, duration=5, prompt="Corridor"),
        Shot(id="shot_middle", start=30, duration=5, prompt="Threshold"),
    ]
    store.save(project)
    assert [shot.id for shot in project.shots] != ["shot_early", "shot_middle", "shot_late"]

    response = client.post(f"/api/projects/{project.id}/director/expand")

    assert response.status_code == 200
    sent = {entry["shot_id"]: entry["index"] for entry in director.inputs[0]["shots"]}
    assert sent == {"shot_early": 0, "shot_middle": 1, "shot_late": 2}
    notice = ProjectStore(tmp_path).get(project.id).messages[-1].content
    # Every Shot the reply names is numbered by the index the model was given for it.
    for shot_id, index in sent.items():
        assert f"shot index {index} at" in notice, shot_id
        assert f"({shot_id})" in notice.split(f"shot index {index} at")[1][:40], shot_id
    # The locked Shot is the one whose manifest position (0) differs from its index (2), which
    # is what makes this test able to fail.
    assert "shot index 2 at 90s (shot_late)" in notice.split("locked")[1]


def test_expansion_of_an_unknown_project_is_a_404(tmp_path: Path):
    """A missing project is 404 before anything else, as it is on every other route."""
    director = RefusingDirector()
    client, _, _ = make_client(tmp_path, director)

    response = client.post("/api/projects/project_deadbeef0000/director/expand")

    assert response.status_code == 404
    assert response.json()["detail"] == "Project not found"
    assert director.calls == 0


def test_expansion_never_blanks_a_prompt_with_an_empty_one(tmp_path: Path):
    """An empty prompt is not a prompt, and this route must not be what erases one.

    `ExpandedShot` requires a non-empty string on the wire, so this is the second half of that
    rule: whatever reaches the merge, a Shot that had a prompt keeps it.
    """
    director = FixedExpansionDirector([("shot_first", "   \n\t ")])
    client, store, _ = make_client(tmp_path, director)
    project = planned_project(store, "Blank prompt")

    response = client.post(f"/api/projects/{project.id}/director/expand")

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.shots[0].prompt == "New shot"
    notice = saved.messages[-1].content
    assert "NOT applied to" in notice
    assert "the model returned an empty prompt" in notice
    assert "Prompts written for" not in notice


def test_expansion_of_a_shotless_project_is_refused_before_any_model_call(tmp_path: Path):
    """422, naming that there is nothing to expand — and no model call to produce it."""
    director = RefusingDirector()
    client, store, _ = make_client(tmp_path, director)
    project = store.create(Project(name="No shots"))

    response = client.post(f"/api/projects/{project.id}/director/expand")

    assert response.status_code == 422
    assert director.calls == 0
    detail = response.json()["detail"]
    assert "no shots to expand" in detail
    # It also says what expansion is, so the refusal is actionable rather than a dead end.
    assert "never creates, retimes, or removes one" in detail
    assert ProjectStore(tmp_path).get(project.id).messages == []


def test_expansion_maps_an_unreachable_director_to_503_and_a_bad_reply_to_502(tmp_path: Path):
    """The chat route's mapping, and nothing written on either path."""
    for status_code, director in (
        (503, UnavailableExpansionDirector()),
        (502, FailingExpansionDirector()),
    ):
        client, store, comfy = make_client(tmp_path / str(status_code), director)
        project = planned_project(store, f"Failing {status_code}")

        response = client.post(f"/api/projects/{project.id}/director/expand")

        assert response.status_code == status_code, response.text
        saved = ProjectStore(tmp_path / str(status_code)).get(project.id)
        assert [shot.prompt for shot in saved.shots] == ["New shot", ""], status_code
        assert saved.messages == [], status_code
        assert comfy.prompts == []


def test_expansion_hands_the_pure_builders_output_verbatim_to_the_director(tmp_path: Path):
    """The acceptance criterion, asserted where it can actually fail.

    "The input includes each Shot's position in the Song" is unfalsifiable against the route
    alone — the chat dump already carries start and duration. So the position is computed by a
    named pure builder, asserted directly in tests/test_timeline.py, and asserted *here* to be
    exactly what the route sent. Anything the route added or dropped fails this.
    """
    director = ExpandingDirector()
    client, store, _ = make_client(tmp_path, director)
    project = planned_project(store, "Verbatim")
    # Built before the call: the route writes prompts, so the builder's output over the *saved*
    # project afterwards is a different payload than the one the model was handed.
    expected = expansion_input(store.get(project.id))

    assert client.post(f"/api/projects/{project.id}/director/expand").status_code == 200

    assert director.inputs == [expected]
    sent = director.inputs[0]
    assert [entry["shot_id"] for entry in sent["shots"]] == ["shot_first", "shot_second"]
    assert [entry["index"] for entry in sent["shots"]] == [0, 1]
    assert [entry["start"] for entry in sent["shots"]] == [0, 10]
    # This project has no Song, so the fraction is absent rather than a fabricated 0.0.
    assert "song" not in sent
    for entry in sent["shots"]:
        assert "song_fraction" not in entry


def test_the_expansion_input_carries_no_production_state(tmp_path: Path):
    """The recorded root cause of Director degradation is rich context.

    The chat route's dump ships every Shot's status, render id, take and review; this input is
    purpose-built and must not. Asserted by field name *and* by planted value, because a field
    renamed on the way into the payload would still leak the thing that matters.
    """
    director = ExpandingDirector()
    client, store, _ = make_client(tmp_path, director)
    project = planned_project(store, "Trimmed")
    project.shots[1].latest_review = VisionInspectionRecord(summary="A vocalist in silver.")
    store.save(project)

    assert client.post(f"/api/projects/{project.id}/director/expand").status_code == 200

    serialised = json.dumps(director.inputs[0])
    for field in ("status", "prompt_id", "latest_output", "latest_review", "approved_output"):
        assert field not in serialised, field
    for value in ("complete", "render-1", "takes/one.mp4", "A vocalist in silver"):
        assert value not in serialised, value
    # And what it does carry is the creative work the prompts have to embed.
    assert "Sodium amber" in serialised
    assert "the corridor, the threshold, the desert" in serialised


def test_a_shot_added_during_the_expansion_call_cannot_be_given_another_shots_prompt(
    tmp_path: Path,
):
    """Why the merge is keyed by id and not by position.

    A local model holds the call open for many seconds; a shot added, split or deleted in that
    window shifts every later position by one. Prompts are free text, so a positional merge
    would write the corridor's prompt onto the new opening shot and the threshold's onto the
    corridor — two plausible prompts on the wrong shots, failing nothing, forever.
    """
    store = ProjectStore(tmp_path)
    project = planned_project(store, "Concurrent add")

    class AddingExpansionDirector(FakeDirector):
        async def expand(self, expansion_input):
            during = ProjectStore(tmp_path).get(project.id)
            during.shots.insert(
                0, Shot(id="shot_inserted", start=0, duration=2, prompt="New shot")
            )
            ProjectStore(tmp_path).save(during)
            return expansion_result(
                "Expanded the plan I was given.",
                [
                    (entry["shot_id"], f"Prompt for {entry['shot_id']}")
                    for entry in expansion_input["shots"]
                ],
            )

    settings = Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy")
    comfy = FakeComfy()
    client = TestClient(
        create_app(
            settings=settings, store=store, comfy=comfy, director=AddingExpansionDirector()
        )
    )

    response = client.post(f"/api/projects/{project.id}/director/expand")

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    prompts = {shot.id: shot.prompt for shot in saved.shots}
    # Each prompt landed on the shot it was addressed to, one position off from where a
    # positional merge would have put it.
    assert prompts["shot_first"] == "Prompt for shot_first"
    assert prompts["shot_second"] == "Prompt for shot_second"
    # The shot that arrived mid-call was not written for, was not dropped, and is reported.
    assert prompts["shot_inserted"] == "New shot"
    assert "Omitted by the model" in saved.messages[-1].content
    assert "shot_inserted" in saved.messages[-1].content
    assert comfy.prompts == []


def test_running_prompt_is_not_reported_as_queued(tmp_path: Path):
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Running"))
    project.shots = [Shot(start=0, duration=5, prompt="Turn", mode="text", status="ready")]
    store.save(project)
    job = client.post(
        f"/api/projects/{project.id}/shots/{project.shots[0].id}/generate/h3", json={}
    ).json()

    async def empty_history(prompt_id):
        return type(
            "History",
            (),
            {"prompt_id": prompt_id, "status": "queued", "outputs": [], "error": ""},
        )()

    async def running_queue_state(prompt_id):
        return "running"

    comfy.history = empty_history
    comfy.queue_state = running_queue_state

    refreshed = client.get(f"/api/projects/{project.id}/jobs/{job['id']}").json()
    assert refreshed["status"] == "running"


def test_windows_output_subfolders_are_normalised(tmp_path: Path):
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Paths"))
    project.shots = [Shot(start=0, duration=5, prompt="Turn", mode="text", status="ready")]
    store.save(project)
    job = client.post(
        f"/api/projects/{project.id}/shots/{project.shots[0].id}/generate/h3", json={}
    ).json()

    async def windows_history(prompt_id):
        return type(
            "History",
            (),
            {
                "prompt_id": prompt_id,
                "status": "complete",
                "outputs": [
                    {"subfolder": r"music-video-producer\proj\shots", "filename": "take.mp4"}
                ],
                "error": "",
            },
        )()

    comfy.history = windows_history
    refreshed = client.get(f"/api/projects/{project.id}/jobs/{job['id']}").json()
    assert refreshed["output_files"] == ["music-video-producer/proj/shots/take.mp4"]


def shots_snapshot(project: Project) -> list[dict]:
    """Every persisted field of every shot, for exact before/after comparison."""
    return [shot.model_dump(mode="json") for shot in project.shots]


def project_with_song_and_shots(client: TestClient, store: ProjectStore, name: str) -> Project:
    """A project whose Song is a real file on disk, with shots carrying every field.

    The song is imported through the route so the audio genuinely exists under
    `media/songs/`, which is what lets the removal tests assert the file survives. The
    import itself needs no confirmation because the project has no shots yet — the shots
    are added afterwards, which is exactly the state the guard exists for.
    """
    project = store.create(Project(name=name))
    client.post(
        f"/api/projects/{project.id}/songs/upload",
        data={"title": "Original spine", "duration": "180"},
        files={"file": ("original.wav", wav_bytes(0.25), "audio/wav")},
    )
    project = store.get(project.id)
    project.shots = [
        Shot(
            start=0,
            duration=5,
            prompt="Opening on the corridor",
            mode="text",
            seed=11,
            status="approved",
            prompt_id="render-1",
            latest_output="takes/one.mp4",
            approved_output="takes/one.mp4",
            locked=True,
        ),
        Shot(
            start=12.5,
            duration=7.25,
            prompt="Chorus release",
            asset_ids=["asset_lead"],
            reference_labels={"asset_lead": "lead vocalist"},
            use_song_audio=True,
            seed=44,
            status="ready",
        ),
    ]
    store.save(project)
    return store.get(project.id)


def test_song_replacement_without_confirmation_is_refused_and_changes_nothing(tmp_path: Path):
    """All three replacement paths refuse, and the refusal costs nothing — no GPU, no bytes.

    The uploaded filename deliberately collides with the existing song's, because songs
    are written under their own name with no index prefix: a guard placed after the copy
    would have overwritten the audio it was refusing to replace.
    """
    client, store, comfy = make_client(tmp_path)
    project = project_with_song_and_shots(client, store, "Unconfirmed replace")
    before_shots = shots_snapshot(project)
    before_song = project.song.model_dump(mode="json")
    audio = store.project_dir(project.id) / project.song.path
    before_audio = audio.read_bytes()

    attempts = {
        "import": client.post(
            f"/api/projects/{project.id}/songs/upload",
            data={"title": "Replacement", "duration": "9"},
            files={"file": ("original.wav", b"not-audio-at-all", "audio/wav")},
        ),
        "music": client.post(
            f"/api/projects/{project.id}/generate/music",
            json={"title": "Replacement", "caption": "industrial synth rock", "duration": 12},
        ),
        "songplanner": client.post(
            f"/api/projects/{project.id}/generate/songplanner",
            json={"title": "Replacement", "idea": "sunset synthwave"},
        ),
    }

    for label, response in attempts.items():
        assert response.status_code == 409, (label, response.text)
        detail = response.json()["detail"].lower()
        assert "shot window" in detail, label
        assert "assembly synchronization" in detail, label
    assert comfy.prompts == []
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.song.model_dump(mode="json") == before_song
    assert shots_snapshot(saved) == before_shots
    assert saved.jobs == []
    assert audio.read_bytes() == before_audio


def test_confirmed_song_replacement_keeps_every_shot_field_intact(tmp_path: Path):
    """The epic's "no shot data is deleted" guarantee, by design rather than by accident.

    Each replacement is re-read through a *fresh* ProjectStore, because the response body
    is the in-memory object the handler just built; only the manifest proves the shots.
    """
    client, store, _ = make_client(tmp_path)
    project = project_with_song_and_shots(client, store, "Confirmed replace")
    before_shots = shots_snapshot(project)

    imported = client.post(
        f"/api/projects/{project.id}/songs/upload",
        data={"title": "Second spine", "duration": "0", "confirm_song_replacement": "true"},
        files={"file": ("second.wav", wav_bytes(1.0), "audio/wav")},
    )
    assert imported.status_code == 200
    after_import = ProjectStore(tmp_path).get(project.id)
    assert after_import.song.title == "Second spine"
    assert after_import.song.path.endswith("second.wav")
    assert shots_snapshot(after_import) == before_shots

    generated = client.post(
        f"/api/projects/{project.id}/generate/music",
        json={
            "title": "Third spine",
            "caption": "industrial synth rock",
            "duration": 12,
            "confirm_song_replacement": True,
        },
    )
    assert generated.status_code == 202
    after_music = ProjectStore(tmp_path).get(project.id)
    assert after_music.song.title == "Third spine"
    assert after_music.song.source == "generated"
    assert shots_snapshot(after_music) == before_shots

    planned = client.post(
        f"/api/projects/{project.id}/generate/songplanner",
        json={"title": "Fourth spine", "idea": "sunset synthwave", "confirm_song_replacement": True},
    )
    assert planned.status_code == 202
    after_planner = ProjectStore(tmp_path).get(project.id)
    assert after_planner.song.title == "Fourth spine"
    assert shots_snapshot(after_planner) == before_shots


def test_a_first_import_and_a_shotless_project_need_no_confirmation(tmp_path: Path):
    """The gate must not make ordinary work friction: nothing depends on the song yet."""
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Frictionless"))

    first = client.post(
        f"/api/projects/{project.id}/songs/upload",
        data={"title": "First", "duration": "30"},
        files={"file": ("first.wav", wav_bytes(0.25), "audio/wav")},
    )
    replaced = client.post(
        f"/api/projects/{project.id}/songs/upload",
        data={"title": "Replacement", "duration": "40"},
        files={"file": ("second.wav", wav_bytes(0.25), "audio/wav")},
    )
    generated = client.post(
        f"/api/projects/{project.id}/generate/music",
        json={"title": "Generated", "caption": "industrial synth rock", "duration": 12},
    )
    removed = client.delete(f"/api/projects/{project.id}/song")

    assert first.status_code == 200
    assert replaced.status_code == 200
    assert generated.status_code == 202
    assert removed.status_code == 200
    assert ProjectStore(tmp_path).get(project.id).song is None


def test_song_removal_requires_confirmation_and_never_touches_shots_or_media(tmp_path: Path):
    client, store, _ = make_client(tmp_path)
    project = project_with_song_and_shots(client, store, "Removal")
    before_shots = shots_snapshot(project)
    audio = store.project_dir(project.id) / project.song.path
    assert audio.is_file()

    refused = client.delete(f"/api/projects/{project.id}/song")

    assert refused.status_code == 409
    detail = refused.json()["detail"].lower()
    assert "shot window" in detail
    assert "assembly synchronization" in detail
    unchanged = ProjectStore(tmp_path).get(project.id)
    assert unchanged.song is not None
    assert shots_snapshot(unchanged) == before_shots

    removed = client.delete(f"/api/projects/{project.id}/song?confirm_song_replacement=true")

    assert removed.status_code == 200
    assert removed.json()["song"] is None
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.song is None
    assert shots_snapshot(saved) == before_shots
    # Removal detaches the song; it never destroys the media it detaches.
    assert audio.is_file()


def test_song_removal_reports_nothing_to_remove_when_there_is_no_song(tmp_path: Path):
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Nothing to remove"))
    project.shots = [Shot(start=0, duration=5, prompt="Opening")]
    store.save(project)
    before_shots = shots_snapshot(store.get(project.id))

    for query in ("", "?confirm_song_replacement=true"):
        response = client.delete(f"/api/projects/{project.id}/song{query}")

        assert response.status_code == 404, query
        assert "no song" in response.json()["detail"].lower(), query
    assert shots_snapshot(ProjectStore(tmp_path).get(project.id)) == before_shots


def test_full_project_put_gates_a_song_change_but_never_an_ordinary_save(tmp_path: Path):
    """The generic save was one HTTP call wide of the guarantee.

    `PUT /api/projects/{id}` is the normal save path for every edit in the UI, so it
    cannot be gated on *carrying* a Song — only on changing one. A body whose Song differs
    from the stored Song is a replacement or a removal however it arrived.
    """
    client, store, _ = make_client(tmp_path)
    project = project_with_song_and_shots(client, store, "Generic save")
    before_shots = shots_snapshot(project)
    before_song = project.song.model_dump(mode="json")

    swapped = project.model_dump(mode="json")
    swapped["song"] = {**before_song, "title": "Smuggled spine", "duration": 12.0}
    removed = project.model_dump(mode="json")
    removed["song"] = None

    for label, payload in (("swapped", swapped), ("removed", removed)):
        response = client.put(f"/api/projects/{project.id}", json=payload)

        assert response.status_code == 409, (label, response.text)
        detail = response.json()["detail"].lower()
        assert "shot window" in detail, label
        assert "assembly synchronization" in detail, label
        saved = ProjectStore(tmp_path).get(project.id)
        assert saved.song.model_dump(mode="json") == before_song, label
        assert shots_snapshot(saved) == before_shots, label

    # An ordinary save carrying the same Song must pass untouched — that is the entire
    # reason this route could not simply be gated.
    ordinary = project.model_dump(mode="json")
    ordinary["creative_brief"] = "An ordinary edit"
    passed = client.put(f"/api/projects/{project.id}", json=ordinary)

    assert passed.status_code == 200
    after_save = ProjectStore(tmp_path).get(project.id)
    assert after_save.creative_brief == "An ordinary edit"
    assert after_save.song.model_dump(mode="json") == before_song
    assert shots_snapshot(after_save) == before_shots

    # And an acknowledged change goes through, still without touching a shot.
    acknowledged = after_save.model_dump(mode="json")
    acknowledged["song"] = None
    confirmed = client.put(
        f"/api/projects/{project.id}?confirm_song_replacement=true", json=acknowledged
    )

    assert confirmed.status_code == 200
    final = ProjectStore(tmp_path).get(project.id)
    assert final.song is None
    assert shots_snapshot(final) == before_shots


def completed_history_for(outputs: list[dict]):
    async def history(prompt_id: str):
        return type(
            "History",
            (),
            {"prompt_id": prompt_id, "status": "complete", "outputs": outputs, "error": ""},
        )()

    return history


def test_a_completing_music_job_does_not_re_attach_audio_to_a_removed_song(tmp_path: Path):
    """A job finishing after removal must not resurrect the Song the Director removed.

    Every music job carries `target_id == "song"`, so the prompt id is the only thing that
    ties a completion to a particular Song. The produced file is not lost — it stays on the
    job's `output_files`, which is where an orphaned take is recovered from.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Removed mid-flight"))
    job = client.post(
        f"/api/projects/{project.id}/generate/music",
        json={"title": "In flight", "caption": "industrial synth rock", "duration": 12},
    ).json()
    assert client.delete(f"/api/projects/{project.id}/song").status_code == 200
    comfy.history = completed_history_for(
        [{"subfolder": f"music-video-producer/{project.id}/songs", "filename": "flight.flac"}]
    )

    refreshed = client.get(f"/api/projects/{project.id}/jobs/{job['id']}")

    assert refreshed.status_code == 200
    assert refreshed.json()["status"] == "complete"
    assert refreshed.json()["output_files"] == [
        f"music-video-producer/{project.id}/songs/flight.flac"
    ]
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.song is None
    assert saved.jobs[-1].output_files == [
        f"music-video-producer/{project.id}/songs/flight.flac"
    ]


def test_a_completing_music_job_does_not_overwrite_a_different_song(tmp_path: Path):
    """The reverse interleaving: the generated file must not be pasted onto an import.

    Overwriting `path` alone left a Song claiming `source == "imported"` while pointing at
    a ComfyUI output, which the frontend resolves through an entirely different URL.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Replaced mid-flight"))
    job = client.post(
        f"/api/projects/{project.id}/generate/music",
        json={"title": "In flight", "caption": "industrial synth rock", "duration": 12},
    ).json()
    imported = client.post(
        f"/api/projects/{project.id}/songs/upload",
        data={"title": "Imported instead", "duration": "30"},
        files={"file": ("instead.wav", wav_bytes(0.25), "audio/wav")},
    )
    assert imported.status_code == 200
    replacement = ProjectStore(tmp_path).get(project.id).song.model_dump(mode="json")
    comfy.history = completed_history_for(
        [{"subfolder": f"music-video-producer/{project.id}/songs", "filename": "flight.flac"}]
    )

    refreshed = client.get(f"/api/projects/{project.id}/jobs/{job['id']}")

    assert refreshed.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.song.model_dump(mode="json") == replacement
    assert saved.song.source == "imported"
    assert "flight.flac" not in saved.song.path
    # Still recoverable from the job rather than silently discarded.
    assert saved.jobs[-1].output_files == [
        f"music-video-producer/{project.id}/songs/flight.flac"
    ]


def test_a_completing_music_job_matches_the_song_by_prompt_id_not_by_source(tmp_path: Path):
    """Two generated songs in a row: `source` cannot tell them apart, the prompt id can.

    The Director queued one song, then queued another before the first finished. The first
    completion belongs to a Song that is no longer the project's, and must not be pasted
    onto the Song that is.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Two generations"))
    first = client.post(
        f"/api/projects/{project.id}/generate/music",
        json={"title": "First", "caption": "industrial synth rock", "duration": 12},
    ).json()

    async def second_submission(prompt, client_id=None):
        return type("Submission", (), {"prompt_id": "p-202", "number": 2})()

    comfy.submit = second_submission
    second = client.post(
        f"/api/projects/{project.id}/generate/music",
        json={"title": "Second", "caption": "colder synth rock", "duration": 12},
    )
    assert second.status_code == 202
    assert ProjectStore(tmp_path).get(project.id).song.prompt_id == "p-202"
    comfy.history = completed_history_for(
        [{"subfolder": f"music-video-producer/{project.id}/songs", "filename": "first.flac"}]
    )

    refreshed = client.get(f"/api/projects/{project.id}/jobs/{first['id']}")

    assert refreshed.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.song.prompt_id == "p-202"
    assert saved.song.title == "Second"
    # The second song has not rendered yet, so it still has no audio of its own — and the
    # first job's output is not it.
    assert saved.song.path == ""
    assert saved.jobs[0].output_files == [
        f"music-video-producer/{project.id}/songs/first.flac"
    ]


def test_a_confirmed_replacement_never_destroys_the_previous_audio(tmp_path: Path):
    """"Re-import the same file" is only an undo if the file is still there.

    Songs were written under their own name, so a replacement whose filename matched the
    previous song overwrote it. Assets avoid this with an index prefix; songs now do too.
    """
    client, store, _ = make_client(tmp_path)
    project = project_with_song_and_shots(client, store, "Non-destructive replace")
    before_shots = shots_snapshot(project)
    previous = store.project_dir(project.id) / project.song.path
    previous_bytes = previous.read_bytes()
    replacement_bytes = wav_bytes(2.0)
    assert replacement_bytes != previous_bytes

    response = client.post(
        f"/api/projects/{project.id}/songs/upload",
        data={"title": "Same name", "duration": "0", "confirm_song_replacement": "true"},
        files={"file": ("original.wav", replacement_bytes, "audio/wav")},
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id)
    current = store.project_dir(project.id) / saved.song.path
    assert current != previous
    assert current.read_bytes() == replacement_bytes
    # The audio the Director could re-import to undo this is still on disk, unmodified.
    assert previous.is_file()
    assert previous.read_bytes() == previous_bytes
    assert shots_snapshot(saved) == before_shots


def test_an_unusable_reported_duration_falls_through_to_probing(tmp_path: Path):
    """`duration > 0` admitted `inf` and 1e18 straight into the timing spine.

    `Song.duration` only constrains `ge=0`, so nothing downstream would have caught it.
    """
    client, store, _ = make_client(tmp_path)

    for label, reported in (("inf", "inf"), ("absurd", "1e18"), ("nan", "nan")):
        project = store.create(Project(name=f"Reported {label}"))
        response = client.post(
            f"/api/projects/{project.id}/songs/upload",
            data={"title": "Measured instead", "duration": reported},
            files={"file": ("one-second.wav", wav_bytes(1.0), "audio/wav")},
        )

        assert response.status_code == 200, label
        stored = ProjectStore(tmp_path).get(project.id).song.duration
        assert 0.99 <= stored <= 1.01, (label, stored)


def test_h3_payload_uses_grid_aligned_frame_count(tmp_path: Path):
    """A 4s window is 96 frames, which is off H3's 17k+5 grid and must round to 107."""
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Grid"))
    project.shots = [Shot(start=0, duration=4, prompt="Off-grid", mode="text", status="ready")]
    store.save(project)

    client.post(f"/api/projects/{project.id}/shots/{project.shots[0].id}/generate/h3", json={})

    inputs = comfy.prompts[-1]["2343"]["inputs"]
    assert inputs["duration_frames"] == 107
    assert (inputs["duration_frames"] - 5) % 17 == 0
