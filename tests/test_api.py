import subprocess
import wave
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient

from music_video_producer.app import create_app
from music_video_producer.comfy import ComfyError
from music_video_producer.config import Settings
from music_video_producer.models import Asset, Project, Shot, Song
from music_video_producer.store import ProjectStore


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


def make_client(tmp_path: Path):
    settings = Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy")
    store = ProjectStore(tmp_path)
    comfy = FakeComfy()
    app = create_app(settings=settings, store=store, comfy=comfy, director=FakeDirector())
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
        json={"message": "Make the chorus feel like release", "apply_shots": True},
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
        json={"message": "Make it moodier", "apply_shots": True},
    )

    assert response.status_code == 200
    saved = store.get(project.id)
    # The style bible survives untouched; the treatment was legitimately replaced.
    assert saved.style_bible == "Sodium amber, hard backlight, 35mm grain, wardrobe continuity notes."
    assert saved.treatment.startswith("A genuinely rewritten treatment")
    notice = saved.messages[-1].content
    assert "Style bible was NOT replaced" in notice
    assert "empty shot list" in notice


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
