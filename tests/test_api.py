import wave
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient

from music_video_producer.app import create_app
from music_video_producer.comfy import ComfyError
from music_video_producer.config import Settings
from music_video_producer.models import Asset, Project, Shot
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


def test_song_upload_probes_duration_when_browser_does_not_supply_it(tmp_path: Path):
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Duration probe"))
    content = BytesIO()
    with wave.open(content, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(8000)
        target.writeframes(b"\0\0" * 8000)

    response = client.post(
        f"/api/projects/{project.id}/songs/upload",
        data={"title": "One second", "duration": "0"},
        files={"file": ("one-second.wav", content.getvalue(), "audio/wav")},
    )

    assert response.status_code == 200
    assert 0.99 <= response.json()["song"]["duration"] <= 1.01


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
        {"title": "T", "idea": "an idea", "duration": 300},  # duration above 200
        {"title": "T", "idea": "x" * 4001},  # idea above max_length
        {"title": "T", "idea": "an idea", "genre_hint": "g" * 161},  # genre above max_length
        {"title": "T", "idea": "an idea", "seed": 2**64},  # seed above 64-bit range
        {"title": "T", "idea": "an idea", "lyrics": "x" * 8001},  # lyrics above max_length
    )

    for body in invalid_bodies:
        response = client.post(f"/api/projects/{project.id}/generate/songplanner", json=body)
        assert response.status_code == 422, body
    assert store.get(project.id).jobs == []
    assert store.get(project.id).song is None


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
