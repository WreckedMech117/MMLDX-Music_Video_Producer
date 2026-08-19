import copy
import hashlib
import json
import re
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
    APPROVE_IN_FLIGHT_REFUSAL,
    APPROVE_NO_TAKE_REFUSAL,
    CHAT_EMPTY_MESSAGE,
    DIRECTOR_CONTEXT_EXCLUDE,
    DOCUMENT_LABELS,
    DOCUMENT_LOCK_NOTICE,
    DOCUMENT_REJECTED_EMPTY_NOTICE,
    DOCUMENT_REJECTED_NOTICE,
    ENHANCE_PREFIX_SUFFIX,
    EXPAND_PROMPTS_WITHOUT_SHOTS,
    EXPANSION_ATTEMPTS,
    EXPANSION_REJECTED_EMPTY_NOTICE,
    H3_ADAPTERS,
    MARK_READY_ALREADY_RENDERED_REFUSAL,
    MARK_READY_APPROVED_REFUSAL,
    MARK_READY_IN_FLIGHT_REFUSAL,
    MARK_READY_LOCKED_REFUSAL,
    MARK_READY_STATUSES,
    MULTIVIEW_SUBJECTS,
    RENDER_AGAIN_APPROVED_REFUSAL,
    RENDER_AGAIN_IN_FLIGHT_REFUSAL,
    RENDER_AGAIN_LOCKED_REFUSAL,
    RENDER_AGAIN_STATUSES,
    RESTORE_AUDIO_IN_FLIGHT_REFUSAL,
    RESTORE_AUDIO_MISSING_SONG_REFUSAL,
    RESTORE_AUDIO_MISSING_TAKE_REFUSAL,
    RESTORE_AUDIO_NO_SONG_REFUSAL,
    RESTORE_AUDIO_NO_TAKE_REFUSAL,
    RESTORE_AUDIO_NOT_SONG_AUDIO_REFUSAL,
    RESTORE_AUDIO_PREFIX_SUFFIX,
    SHOT_CLAIM_MISMATCH_NOTICE,
    SHOT_CLAIM_WITHOUT_ANY_SHOTS_NOTICE,
    SHOT_DIRECTOR_VISIBLE,
    SHOT_DIRECTOR_WITHHELD,
    SHOT_PLAN_EMPTY_NOTICE,
    SHOT_WINDOW_NOTICE,
    SONG_CAPTION_LIMIT,
    SONG_CONTEXT_FIELD_NAMES,
    SONG_CONTEXT_LABELS,
    SONG_CONTEXT_RESTORE_REFUSAL,
    SONG_CONTEXT_WITHOUT_SONG,
    SONG_DIRECTOR_VISIBLE,
    SONG_DIRECTOR_WITHHELD,
    SONG_LYRICS_LIMIT,
    TAKE_MISSING_FILE_REFUSAL,
    TAKE_NOT_RENDERED_REFUSAL,
    UNAPPROVE_NOT_APPROVED_REFUSAL,
    DirectorRequest,
    DocumentName,
    SongContextField,
    SongContextRequest,
    _withheld_fields,
    create_app,
    document_change_notice,
    document_first_draft_notice,
    multiview_refusal,
    prose_claims_shots,
    reference_prompt,
)
from music_video_producer.batch import PLACEHOLDER_PROMPT, readiness_refusal, shot_label
from music_video_producer.comfy import ComfyError
from music_video_producer.config import Settings
from music_video_producer.director import (
    DirectorBudgetExhausted,
    DirectorError,
    DirectorResult,
    DirectorUnavailable,
    ExpandedShot,
    PlannedShot,
    ShotExpansion,
)
from music_video_producer.h3_expansion_prompt import (
    system_prompt as h3_expansion_system_prompt,
)
from music_video_producer.models import (
    ASSET_ROLE_LABELS,
    LEGACY_SHOT_MODES,
    NOTICE_RAW_LIMIT,
    SHOT_MODE_SPECS,
    Asset,
    AssetCitation,
    AssetKind,
    AssetRole,
    MessageNotice,
    NoticeKind,
    Project,
    RenderJob,
    Shot,
    ShotMode,
    ShotStatus,
    SingingState,
    Song,
    SongSection,
    TreatmentMessage,
    VisionInspectionRecord,
    citations_in_prompt_order,
    citations_in_role,
    dangling_citations,
    mode_specification_problems,
    resolve_shot_mode,
)
from music_video_producer.store import ProjectStore
from music_video_producer.timeline import expansion_input, shot_expansion_input
from music_video_producer.workflows import (
    H3_ASPECT_RATIOS,
    H3_DIRECTOR_DEFAULT_HEIGHT,
    H3_DIRECTOR_DEFAULT_WIDTH,
    H3_DIRECTOR_MAX_FRAMES,
    H3_REFERENCE_MAX_FRAMES,
    LTX25_ENHANCE_DETAILER_STRENGTH,
    LTX25_ENHANCE_SEED,
    LTX25_ENHANCE_SIGMAS,
    build_h3_reference_payload,
    song_audio_window,
)


class FakeComfy:
    def __init__(self):
        self.prompts = []
        self.uploads = []
        self.history_error = False
        self.submit_error = False
        # The /queue answer the reconciliation endpoint reads once per tick. Empty by default,
        # which sends every open job to `history` -- exactly what the per-job refresh always did,
        # so no existing test changes meaning. `queue_calls` counts the reads so a test can
        # assert the endpoint's once-per-tick promise and the idle project's zero.
        self.queue_payload = {"queue_running": [], "queue_pending": []}
        self.queue_error = False
        self.queue_calls = 0
        self.history_calls = 0

    async def health(self):
        return {"online": True, "url": "http://fake"}

    async def submit(self, prompt, client_id=None):
        if self.submit_error:
            raise ComfyError("ComfyUI is unreachable")
        self.prompts.append(prompt)
        return type("Submission", (), {"prompt_id": "p-101", "number": 1})()

    async def queue(self):
        self.queue_calls += 1
        if self.queue_error:
            raise ComfyError("ComfyUI is unreachable")
        return self.queue_payload

    async def history(self, prompt_id):
        self.history_calls += 1
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


# A lyric sheet with the three things a real one has and nothing here may touch: section tags,
# interior blank lines, and indentation. Written for this suite rather than taken from a real
# song — a copyrighted sheet is not test data, and nothing here depends on the words.
IMPORTED_LYRIC_SHEET = (
    "[Verse 1]\n"
    "Cold rail, the platform hums\n"
    "\n"
    "    a paper cup goes over the edge\n"
    "\n"
    "[Chorus]\n"
    "Hold the line, hold the line\n"
    "\n"
    "[Bridge]\n"
    "    counting sodium lights"
)
IMPORTED_SONG_STYLE = "Downtempo industrial pop, close female vocal, tape saturation, no live drums."


def import_song(
    client: TestClient,
    project_id: str,
    *,
    title: str = "Imported master",
    filename: str = "master.wav",
    **context: str,
):
    """One import, carrying whatever song context the caller passes and nothing it does not.

    `context` is spread into the form rather than defaulted to `""`, so a test asking for "an
    import that carries neither field" really sends no such field — which is the only way to
    prove an import written before these fields existed behaves exactly as it did.
    """
    return client.post(
        f"/api/projects/{project_id}/songs/upload",
        data={"title": title, "duration": "12.5", **context},
        files={"file": (filename, wav_bytes(0.5), "audio/wav")},
    )


def test_an_import_carries_its_lyric_sheet_and_style_onto_the_song(tmp_path: Path):
    """The gap itself: an imported Song can finally say what it is.

    Re-read through a fresh ProjectStore, because the response body is the in-memory object the
    handler just built and the claim is that the manifest carries it.
    """
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Context on import"))

    response = import_song(
        client, project.id, lyrics=IMPORTED_LYRIC_SHEET, caption=IMPORTED_SONG_STYLE
    )

    assert response.status_code == 200
    song = response.json()["song"]
    assert song["lyrics"] == IMPORTED_LYRIC_SHEET
    assert song["caption"] == IMPORTED_SONG_STYLE
    restarted = ProjectStore(tmp_path).get(project.id).song
    assert restarted.lyrics == IMPORTED_LYRIC_SHEET
    assert restarted.caption == IMPORTED_SONG_STYLE
    # The import is otherwise exactly the import it always was.
    assert restarted.source == "imported"
    assert restarted.duration == 12.5
    assert restarted.path.endswith("master.wav")
    assert restarted.prompt_id == ""


def test_an_import_carrying_neither_field_behaves_exactly_as_it_did(tmp_path: Path):
    """Both fields are optional, and "optional" means the form key is absent, not empty."""
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="No context"))

    response = import_song(client, project.id)

    assert response.status_code == 200
    assert response.json()["song"]["lyrics"] == ""
    assert response.json()["song"]["caption"] == ""
    stored = ProjectStore(tmp_path).get(project.id).song
    assert stored.lyrics == ""
    assert stored.caption == ""
    assert stored.duration == 12.5


@pytest.mark.parametrize("field", ["lyrics", "caption"])
def test_one_song_context_field_alone_leaves_the_other_empty(tmp_path: Path, field: str):
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name=f"Only {field}"))
    other = "caption" if field == "lyrics" else "lyrics"

    response = import_song(client, project.id, **{field: "Supplied on its own."})

    assert response.status_code == 200
    assert response.json()["song"][field] == "Supplied on its own."
    assert response.json()["song"][other] == ""


def test_interior_lyric_structure_survives_the_import_exactly(tmp_path: Path):
    """Only leading and trailing whitespace goes.

    The section tags, the blank lines between verses and the indentation are the structure of the
    sheet, and this is the same contract the known-lyrics generation path already keeps. Asserted
    as equality against the original string rather than by substring, because "the tags are still
    in there somewhere" is exactly what a reflow would also satisfy.
    """
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Interior structure"))

    response = import_song(
        client,
        project.id,
        lyrics=f"\n\n  {IMPORTED_LYRIC_SHEET}  \n\t\n",
        caption=f"  {IMPORTED_SONG_STYLE}\n",
    )

    assert response.status_code == 200
    stored = ProjectStore(tmp_path).get(project.id).song
    assert stored.lyrics == IMPORTED_LYRIC_SHEET
    assert stored.caption == IMPORTED_SONG_STYLE
    # Said again as the thing that actually matters, so a future "tidy the sheet" cannot pass by
    # trimming every line and still matching some looser assertion.
    assert "\n\n" in stored.lyrics
    assert "\n    counting sodium lights" in stored.lyrics


def test_whitespace_only_song_context_is_stored_as_absent(tmp_path: Path):
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Blank context"))

    response = import_song(client, project.id, lyrics="   \n\t\n  ", caption="\n\n")

    assert response.status_code == 200
    assert response.json()["song"]["lyrics"] == ""
    assert response.json()["song"]["caption"] == ""


@pytest.mark.parametrize(
    ("field", "limit"), [("lyrics", SONG_LYRICS_LIMIT), ("caption", SONG_CAPTION_LIMIT)]
)
def test_an_oversized_field_is_refused_before_any_audio_is_written(
    tmp_path: Path, field: str, limit: int
):
    """422 with a plain message, and a refusal that wrote nothing.

    The check sits ahead of the copy for the same reason the replacement gate does: a refusal that
    has already written a file and left the project's Song half-replaced is not a refusal. The
    ceiling itself is asserted from the constant, so the bound is one number rather than two.
    """
    client, store, _ = make_client(tmp_path)
    project = Project(name="Oversized")
    project.song = Song(
        title="Previous song", source="imported", path="media/songs/previous.wav", duration=187.5
    )
    store.create(project)

    response = import_song(client, project.id, **{field: "x" * (limit + 1)})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert str(limit) in detail
    assert str(limit + 1) in detail
    # A plain sentence, not a validation-error blob.
    assert isinstance(detail, str)
    stored = ProjectStore(tmp_path).get(project.id)
    assert stored.song.title == "Previous song"
    assert stored.song.duration == 187.5
    assert not list((store.media_dir(project.id) / "songs").glob("*"))

    # Exactly the limit is accepted, so the bound is a bound rather than an off-by-one.
    accepted = import_song(client, project.id, **{field: "x" * limit})
    assert accepted.status_code == 200
    assert accepted.json()["song"][field] == "x" * limit


def test_song_context_can_be_corrected_after_the_import(tmp_path: Path):
    """A Director who imported yesterday is not made to re-import to say what the song is.

    The audio, its measured length and its provenance are the things this must not touch, so they
    are compared field by field against what the import wrote — and the file itself is still on
    disk afterwards.
    """
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Corrected later"))
    import_song(client, project.id)
    imported = ProjectStore(tmp_path).get(project.id).song
    audio = store.project_dir(project.id) / imported.path

    response = client.put(
        f"/api/projects/{project.id}/song/context",
        json={"lyrics": f"\n{IMPORTED_LYRIC_SHEET}\n ", "caption": IMPORTED_SONG_STYLE},
    )

    assert response.status_code == 200
    corrected = ProjectStore(tmp_path).get(project.id).song
    assert corrected.lyrics == IMPORTED_LYRIC_SHEET
    assert corrected.caption == IMPORTED_SONG_STYLE
    assert corrected.path == imported.path
    assert corrected.duration == imported.duration
    assert corrected.source == imported.source
    assert corrected.prompt_id == imported.prompt_id
    assert corrected.title == imported.title
    assert audio.is_file()


def test_song_context_can_be_cleared_and_is_bounded_on_the_edit_too(tmp_path: Path):
    """Clearing a wrong sheet has to be possible, and the edit's ceiling is the import's.

    The oversized case also proves the two assignments are not half-applied: `lyrics` is valid and
    `caption` is not, and neither reaches the stored Song.
    """
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Cleared"))
    import_song(client, project.id, lyrics=IMPORTED_LYRIC_SHEET, caption=IMPORTED_SONG_STYLE)

    refused = client.put(
        f"/api/projects/{project.id}/song/context",
        json={"lyrics": "A replacement sheet.", "caption": "y" * (SONG_CAPTION_LIMIT + 1)},
    )
    assert refused.status_code == 422
    unchanged = ProjectStore(tmp_path).get(project.id).song
    assert unchanged.lyrics == IMPORTED_LYRIC_SHEET
    assert unchanged.caption == IMPORTED_SONG_STYLE

    cleared = client.put(f"/api/projects/{project.id}/song/context", json={"lyrics": "  "})
    assert cleared.status_code == 200
    emptied = ProjectStore(tmp_path).get(project.id).song
    assert emptied.lyrics == ""
    assert emptied.caption == ""
    assert emptied.duration == 12.5


def test_song_context_edit_without_a_song_is_refused_and_says_what_to_do(tmp_path: Path):
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Songless"))

    response = client.put(
        f"/api/projects/{project.id}/song/context", json={"lyrics": IMPORTED_LYRIC_SHEET}
    )

    assert response.status_code == 404
    assert response.json()["detail"] == SONG_CONTEXT_WITHOUT_SONG
    assert ProjectStore(tmp_path).get(project.id).song is None


def test_the_edit_route_cannot_carry_audio_duration_or_provenance(tmp_path: Path):
    """The audio is not editable text, so nothing that could overwrite it is on the wire.

    A body inventing a path or a duration is ignored rather than honoured — the route binds a model
    with exactly two fields, and the Song it writes to is the stored one rather than a rebuilt one.
    """
    assert set(SongContextRequest.model_fields) == {"lyrics", "caption"}
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="No smuggling"))
    import_song(client, project.id)

    response = client.put(
        f"/api/projects/{project.id}/song/context",
        json={
            "lyrics": "The words.",
            "caption": "The sound.",
            "path": "media/songs/somewhere-else.wav",
            "duration": 999,
            "source": "generated",
            "prompt_id": "p-forged",
            "title": "Renamed",
        },
    )

    assert response.status_code == 200
    stored = ProjectStore(tmp_path).get(project.id).song
    assert stored.lyrics == "The words."
    assert stored.path.endswith("master.wav")
    assert stored.duration == 12.5
    assert stored.source == "imported"
    assert stored.prompt_id == ""
    assert stored.title == "Imported master"


def test_a_generated_song_still_writes_its_own_context(tmp_path: Path):
    """Neither generation path is touched by this change: they already set both fields."""
    client, store, _ = make_client(tmp_path)
    music = store.create(Project(name="Direct Music 3"))
    planner = store.create(Project(name="SongPlanner"))

    client.post(
        f"/api/projects/{music.id}/generate/music",
        json={
            "title": "Night Wire",
            "caption": "industrial synth rock",
            "lyrics": IMPORTED_LYRIC_SHEET,
            "duration": 8,
            "seed": 9,
        },
    )
    client.post(
        f"/api/projects/{planner.id}/generate/songplanner",
        json={
            "title": "Night Wire (Cover)",
            "idea": "faithful synthwave cover",
            "lyrics": IMPORTED_LYRIC_SHEET,
            "duration": 90,
            "seed": 3,
        },
    )

    generated = store.get(music.id).song
    assert generated.source == "generated"
    assert generated.lyrics == IMPORTED_LYRIC_SHEET
    assert generated.caption == "industrial synth rock"
    assert generated.prompt_id == "p-101"
    covered = store.get(planner.id).song
    assert covered.lyrics == IMPORTED_LYRIC_SHEET
    assert covered.caption == "faithful synthwave cover"


def test_a_song_saved_before_this_change_loads_with_both_fields_empty(tmp_path: Path):
    """A manifest whose Song predates these fields carrying any value at all.

    Written by removing the keys from a real manifest rather than by constructing a `Song`, because
    the claim is about JSON on disk: `Song` would supply the defaults itself and prove nothing.
    """
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Older manifest"))
    import_song(client, project.id, lyrics=IMPORTED_LYRIC_SHEET, caption=IMPORTED_SONG_STYLE)
    manifest = store.manifest_path(project.id)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    del payload["song"]["lyrics"]
    del payload["song"]["caption"]
    manifest.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    response = client.get(f"/api/projects/{project.id}")

    assert response.status_code == 200
    assert response.json()["song"]["lyrics"] == ""
    assert response.json()["song"]["caption"] == ""
    assert response.json()["song"]["duration"] == 12.5
    assert ProjectStore(tmp_path).get(project.id).song.lyrics == ""


def save_context(client: TestClient, project_id: str, **fields: str):
    """One song-context save, defaulting each field to what is already stored.

    The route assigns both fields from the body, so a test that means "edit the lyrics" has to send
    the stored caption back with it or it is also testing a deletion. Reading the stored values
    here is what keeps every test below about the one field it names.
    """
    song = client.get(f"/api/projects/{project_id}").json()["song"]
    body = {field: song[field] for field in ("lyrics", "caption")} | fields
    return client.put(f"/api/projects/{project_id}/song/context", json=body)


def restore_context(client: TestClient, project_id: str, field: str):
    return client.post(f"/api/projects/{project_id}/song/context/{field}/restore")


def test_a_context_edit_keeps_the_version_it_replaced(tmp_path: Path):
    """The gap this closes: the largest hand-authored text here finally has a way back.

    Re-read through a fresh ProjectStore, because the response body is the in-memory object the
    handler just built and the claim is that recovery survives a restart.
    """
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Kept"))
    import_song(client, project.id, lyrics=IMPORTED_LYRIC_SHEET, caption=IMPORTED_SONG_STYLE)

    response = save_context(
        client, project.id, lyrics="A different sheet entirely.", caption="A different sound."
    )

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id).song
    assert saved.lyrics == "A different sheet entirely."
    assert saved.caption == "A different sound."
    assert saved.lyrics_previous == IMPORTED_LYRIC_SHEET
    assert saved.caption_previous == IMPORTED_SONG_STYLE
    # Single-slot, exactly as AD-14 has it for the documents: a second edit keeps only the version
    # immediately before it, never a stack.
    save_context(client, project.id, lyrics="A third sheet.")
    again = ProjectStore(tmp_path).get(project.id).song
    assert again.lyrics_previous == "A different sheet entirely."
    assert IMPORTED_LYRIC_SHEET not in (again.lyrics, again.lyrics_previous)


def test_a_no_op_save_does_not_spend_the_slot(tmp_path: Path):
    """The most likely accidental path there is: open the editor, click save, change nothing.

    Spending the one slot on that would overwrite the genuinely recoverable version with a copy of
    the live text — the feature destroying the thing it exists to protect, silently, on a click the
    Director had no reason to think was destructive. Asserted for a save that repeats the whole
    context, for one that repeats it with the whitespace a paste leaves behind (the route normalises
    both sides the same way, so that is still a no-op), and for the untouched half of a real edit.
    """
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="No-op"))
    import_song(client, project.id, lyrics=IMPORTED_LYRIC_SHEET, caption=IMPORTED_SONG_STYLE)
    save_context(client, project.id, lyrics="The replacement worth keeping a way back from.")
    kept = ProjectStore(tmp_path).get(project.id).song
    assert kept.lyrics_previous == IMPORTED_LYRIC_SHEET

    identical = save_context(client, project.id)

    assert identical.status_code == 200
    after = ProjectStore(tmp_path).get(project.id).song
    assert after.lyrics == "The replacement worth keeping a way back from."
    # The whole assertion: the slot still holds the sheet, not a second copy of the live text.
    assert after.lyrics_previous == IMPORTED_LYRIC_SHEET
    assert after.caption_previous is None

    padded = save_context(
        client, project.id, lyrics="\n\n  The replacement worth keeping a way back from.  \n\t"
    )

    assert padded.status_code == 200
    assert ProjectStore(tmp_path).get(project.id).song.lyrics_previous == IMPORTED_LYRIC_SHEET


def test_only_the_edited_fields_slot_moves(tmp_path: Path):
    """Two fields, two slots, one save button — and the button is a fact about the screen.

    Editing the lyric sheet must not spend the style description's slot: they are separate pieces
    of work, and a Director correcting a typo in the lyrics would otherwise lose the way back to a
    style description they were not even looking at.
    """
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Independent slots"))
    import_song(client, project.id, lyrics=IMPORTED_LYRIC_SHEET, caption=IMPORTED_SONG_STYLE)
    save_context(client, project.id, caption="A style description worth getting back.")

    save_context(client, project.id, lyrics="Only the lyrics changed here.")

    saved = ProjectStore(tmp_path).get(project.id).song
    assert saved.lyrics_previous == IMPORTED_LYRIC_SHEET
    # Untouched by the second save, so the earlier style description is still recoverable.
    assert saved.caption_previous == IMPORTED_SONG_STYLE
    assert saved.caption == "A style description worth getting back."


def test_restore_swaps_rather_than_pops_so_it_is_itself_undoable(tmp_path: Path):
    """The document restore's shape exactly, because the asymmetry would be the surprise.

    A Director who has used Restore on the Treatment knows a mis-click costs nothing there; a pop
    here would make the identical-looking button one-way and the identical-looking click final.
    """
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Swap"))
    import_song(client, project.id, lyrics=IMPORTED_LYRIC_SHEET, caption=IMPORTED_SONG_STYLE)
    save_context(client, project.id, lyrics="The paste nobody wanted.")

    response = restore_context(client, project.id, "lyrics")

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id).song
    assert saved.lyrics == IMPORTED_LYRIC_SHEET
    assert saved.lyrics_previous == "The paste nobody wanted."
    # The other field is untouched by a restore of this one.
    assert saved.caption == IMPORTED_SONG_STYLE
    assert saved.caption_previous is None

    back = restore_context(client, project.id, "lyrics")

    assert back.status_code == 200
    swapped = ProjectStore(tmp_path).get(project.id).song
    assert swapped.lyrics == "The paste nobody wanted."
    assert swapped.lyrics_previous == IMPORTED_LYRIC_SHEET


def test_an_empty_previous_version_is_a_real_one_and_restores(tmp_path: Path):
    """A blank is a legitimate recovery target, and this is where the document shape is wrong here.

    The document slots are `str = ""`, so they cannot tell "nothing was ever kept" from "what was
    kept was blank", and their restore route refuses an empty slot. A Director who pasted a sheet
    into a field that was empty has a real previous version — the blank — and wanting it back is an
    ordinary undo. `None` is what means nothing was kept; `""` restores.
    """
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Blank before"))
    import_song(client, project.id)
    before = ProjectStore(tmp_path).get(project.id).song
    assert before.lyrics == ""
    assert before.lyrics_previous is None

    save_context(client, project.id, lyrics=IMPORTED_LYRIC_SHEET)
    filled = ProjectStore(tmp_path).get(project.id).song
    assert filled.lyrics == IMPORTED_LYRIC_SHEET
    # The slot holds a blank, and it is a slot rather than an absence.
    assert filled.lyrics_previous == ""

    response = restore_context(client, project.id, "lyrics")

    assert response.status_code == 200
    restored = ProjectStore(tmp_path).get(project.id).song
    assert restored.lyrics == ""
    # And the swap still holds, so the sheet is not lost by getting the blank back.
    assert restored.lyrics_previous == IMPORTED_LYRIC_SHEET


@pytest.mark.parametrize("field", ["lyrics", "caption"])
def test_song_context_restore_with_nothing_kept_is_refused_and_changes_nothing(
    tmp_path: Path, field: str
):
    """An empty slot must refuse rather than blank the live text — that is the loss, not the fix.

    Deliberately says nothing about *which* code comes back; that is
    `test_both_restore_routes_answer_an_empty_slot_with_one_code`'s job. The two are separate on
    purpose: this one is the guarantee that an empty slot refuses at all and that the live text
    survives, and it has to keep failing if the `previous is None` check is dropped by someone who
    leaves the number beside it untouched.
    """
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Nothing kept"))
    import_song(client, project.id, lyrics=IMPORTED_LYRIC_SHEET, caption=IMPORTED_SONG_STYLE)

    response = restore_context(client, project.id, field)

    assert not response.is_success, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert SONG_CONTEXT_LABELS[field] in detail
    assert detail == SONG_CONTEXT_RESTORE_REFUSAL.format(field=SONG_CONTEXT_LABELS[field])
    saved = ProjectStore(tmp_path).get(project.id).song
    assert saved.lyrics == IMPORTED_LYRIC_SHEET
    assert saved.caption == IMPORTED_SONG_STYLE
    assert saved.lyrics_previous is None
    assert saved.caption_previous is None


def test_both_restore_routes_answer_an_empty_slot_with_one_code(tmp_path: Path):
    """Two restores, one question — "nothing was kept" — and one status code between them.

    The song-context restore answered 422 for an empty slot until 2026-08-18 while the older
    document restore answered 409 for the identical condition. The Director renegotiated it to 409:
    one application answering the same question two ways is the defect, and the older route is the
    precedent. Nothing about *when* either refuses moved with it.

    Asserted as an equality between the two routes as well as against the literal. The literal
    alone pins today's behaviour; the equality is what fails the next time one of them is edited
    without the other, which is exactly how the two drifted apart in the first place.
    """
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Neither has kept anything"))
    import_song(client, project.id, lyrics=IMPORTED_LYRIC_SHEET, caption=IMPORTED_SONG_STYLE)

    codes = {
        "song/lyrics": restore_context(client, project.id, "lyrics").status_code,
        "song/caption": restore_context(client, project.id, "caption").status_code,
        "document/treatment": client.post(
            f"/api/projects/{project.id}/documents/treatment/restore"
        ).status_code,
        "document/style_bible": client.post(
            f"/api/projects/{project.id}/documents/style_bible/restore"
        ).status_code,
    }

    assert set(codes.values()) == {409}, codes
    # Both refusals are still refusals of the same thing: nothing was written by either.
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.song.lyrics == IMPORTED_LYRIC_SHEET
    assert saved.song.lyrics_previous is None
    assert saved.treatment == ""
    assert saved.treatment_previous == ""


def test_song_context_restore_needs_a_song_and_a_known_field(tmp_path: Path):
    client, store, _ = make_client(tmp_path)
    songless = store.create(Project(name="Songless"))

    missing = restore_context(client, songless.id, "lyrics")
    assert missing.status_code == 404
    assert missing.json()["detail"] == SONG_CONTEXT_WITHOUT_SONG

    project = store.create(Project(name="Unknown field"))
    import_song(client, project.id, lyrics=IMPORTED_LYRIC_SHEET)
    assert restore_context(client, project.id, "title").status_code == 422
    assert restore_context(client, "project_missing", "lyrics").status_code == 404


def test_the_restore_route_touches_nothing_but_the_field_it_names(tmp_path: Path):
    """Recovery must not become a second door onto the audio, its length or its provenance."""
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Restore touches nothing"))
    import_song(client, project.id, lyrics=IMPORTED_LYRIC_SHEET)
    imported = ProjectStore(tmp_path).get(project.id).song
    save_context(client, project.id, lyrics="Replaced.")

    assert restore_context(client, project.id, "lyrics").status_code == 200

    restored = ProjectStore(tmp_path).get(project.id).song
    assert restored.path == imported.path
    assert restored.duration == imported.duration
    assert restored.source == imported.source
    assert restored.prompt_id == imported.prompt_id
    assert restored.title == imported.title
    assert (store.project_dir(project.id) / imported.path).is_file()


def test_an_import_writes_no_recovery_slot(tmp_path: Path):
    """A new song has nothing prior to keep, so an import that invented a slot would be lying.

    Asserted for both doors a song arrives through, and for a *replacement* import as well as a
    first one — the previous song's sheet is the previous song's, not this one's history.
    """
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Import keeps nothing"))

    import_song(client, project.id, lyrics=IMPORTED_LYRIC_SHEET, caption=IMPORTED_SONG_STYLE)

    first = ProjectStore(tmp_path).get(project.id).song
    assert first.lyrics_previous is None
    assert first.caption_previous is None

    save_context(client, project.id, lyrics="An edit that does keep a version.")
    assert ProjectStore(tmp_path).get(project.id).song.lyrics_previous == IMPORTED_LYRIC_SHEET

    replaced = import_song(
        client, project.id, filename="second.wav", lyrics="The new song's own sheet."
    )

    assert replaced.status_code == 200
    after = ProjectStore(tmp_path).get(project.id).song
    assert after.lyrics == "The new song's own sheet."
    assert after.lyrics_previous is None
    assert after.caption_previous is None


def song_with_kept_context(client: TestClient, store: ProjectStore, name: str) -> Project:
    """A project with a song, a shot depending on it, and both recovery slots occupied.

    The shot is what makes every song-changing route demand confirmation, so these tests exercise
    the confirmed path rather than the frictionless one — a slot that survives a *refused* change
    is not a bug, and a slot that survives a confirmed one is.
    """
    project = store.create(Project(name=name))
    import_song(client, project.id, lyrics=IMPORTED_LYRIC_SHEET, caption=IMPORTED_SONG_STYLE)
    save_context(client, project.id, lyrics="A replacement sheet.", caption="A replacement style.")
    project = store.get(project.id)
    project.shots = [Shot(start=0, duration=4, prompt="Held wide")]
    store.save(project)
    kept = store.get(project.id).song
    assert kept.lyrics_previous == IMPORTED_LYRIC_SHEET
    assert kept.caption_previous == IMPORTED_SONG_STYLE
    return store.get(project.id)


def test_no_slot_outlives_the_song_it_describes(tmp_path: Path):
    """A slot is about one track. Carried onto the next, it is a lie the Director can click.

    "Restore" on a song imported ten minutes ago would hand back a lyric sheet belonging to a track
    that is no longer in the project, presented as this song's own previous version. All five
    song-changing routes are covered here, on their confirmed path, because that is the one that
    actually replaces the song.
    """
    client, store, _ = make_client(tmp_path)

    uploaded = song_with_kept_context(client, store, "Replaced by import")
    assert (
        client.post(
            f"/api/projects/{uploaded.id}/songs/upload",
            data={"title": "Second", "duration": "9", "confirm_song_replacement": "true"},
            files={"file": ("second.wav", wav_bytes(0.5), "audio/wav")},
        ).status_code
        == 200
    )
    replaced_by_import = ProjectStore(tmp_path).get(uploaded.id).song
    assert replaced_by_import.lyrics_previous is None
    assert replaced_by_import.caption_previous is None

    music = song_with_kept_context(client, store, "Replaced by Music 3")
    assert (
        client.post(
            f"/api/projects/{music.id}/generate/music",
            json={
                "title": "Night Wire",
                "caption": "industrial synth rock",
                "duration": 8,
                "confirm_song_replacement": True,
            },
        ).status_code
        == 202
    )
    assert ProjectStore(tmp_path).get(music.id).song.lyrics_previous is None

    planner = song_with_kept_context(client, store, "Replaced by SongPlanner")
    assert (
        client.post(
            f"/api/projects/{planner.id}/generate/songplanner",
            json={
                "title": "Night Wire (Cover)",
                "idea": "faithful synthwave cover",
                "duration": 90,
                "confirm_song_replacement": True,
            },
        ).status_code
        == 202
    )
    assert ProjectStore(tmp_path).get(planner.id).song.caption_previous is None

    removed = song_with_kept_context(client, store, "Removed")
    assert (
        client.delete(
            f"/api/projects/{removed.id}/song?confirm_song_replacement=true"
        ).status_code
        == 200
    )
    assert ProjectStore(tmp_path).get(removed.id).song is None

    # And the whole-manifest PUT, which is the sibling write path every one of these stories has
    # had to close: it carries a Song in its body, so it can replace one too.
    put = song_with_kept_context(client, store, "Replaced by PUT")
    body = client.get(f"/api/projects/{put.id}").json()
    body["song"] = {"title": "Another track", "source": "imported", "path": "media/songs/x.wav", "duration": 42}
    response = client.put(
        f"/api/projects/{put.id}?confirm_song_replacement=true", json=body
    )
    assert response.status_code == 200
    swapped = ProjectStore(tmp_path).get(put.id).song
    assert swapped.title == "Another track"
    assert swapped.lyrics_previous is None
    assert swapped.caption_previous is None


def test_the_whole_project_put_neither_wipes_nor_invents_a_song_recovery_slot(tmp_path: Path):
    """The generic save is the guard hole every previous story here left open once.

    Three failures in one route, all from binding a client-supplied `Song`. A client written before
    the slots existed omits them, so an ordinary save arrives carrying `None` and deletes both kept
    versions. A client that *invents* one is worse — it plants text the restore route then swaps
    into the live lyric sheet as "the version you had before". And because the route decides a song
    was replaced by comparing the bodies, the slots have to be adopted from storage *before* that
    comparison, or the omission alone makes an ordinary save look like a song replacement and
    demand a confirmation for a change nobody made.
    """
    client, store, _ = make_client(tmp_path)
    project = song_with_kept_context(client, store, "Ordinary save")

    body = client.get(f"/api/projects/{project.id}").json()
    del body["song"]["lyrics_previous"]
    del body["song"]["caption_previous"]
    body["name"] = "Renamed by an old client"

    omitted = client.put(f"/api/projects/{project.id}", json=body)

    # No confirmation was asked for and none was sent: this is not a song change.
    assert omitted.status_code == 200, omitted.json()
    saved = ProjectStore(tmp_path).get(project.id)
    assert saved.name == "Renamed by an old client"
    assert saved.song.lyrics_previous == IMPORTED_LYRIC_SHEET
    assert saved.song.caption_previous == IMPORTED_SONG_STYLE

    forged = client.get(f"/api/projects/{project.id}").json()
    forged["song"]["lyrics_previous"] = "Text the Director never wrote and never stored."

    planted = client.put(f"/api/projects/{project.id}", json=forged)

    assert planted.status_code == 200, planted.json()
    unplanted = ProjectStore(tmp_path).get(project.id).song
    assert unplanted.lyrics_previous == IMPORTED_LYRIC_SHEET
    # And the restore route cannot be made to serve it.
    assert restore_context(client, project.id, "lyrics").status_code == 200
    assert ProjectStore(tmp_path).get(project.id).song.lyrics == IMPORTED_LYRIC_SHEET


def test_a_song_saved_before_recovery_existed_loads_with_no_slots(tmp_path: Path):
    """A manifest whose Song predates the slots must load unchanged and simply keep nothing.

    Written by removing the keys from a real manifest rather than by constructing a `Song`, because
    the claim is about JSON on disk. `None` rather than `""`: an older song has kept nothing, which
    is a different statement from having kept a blank, and the restore route must refuse it.
    """
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Older manifest, no slots"))
    import_song(client, project.id, lyrics=IMPORTED_LYRIC_SHEET, caption=IMPORTED_SONG_STYLE)
    manifest = store.manifest_path(project.id)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    del payload["song"]["lyrics_previous"]
    del payload["song"]["caption_previous"]
    manifest.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    loaded = client.get(f"/api/projects/{project.id}")

    assert loaded.status_code == 200
    assert loaded.json()["song"]["lyrics_previous"] is None
    assert loaded.json()["song"]["lyrics"] == IMPORTED_LYRIC_SHEET
    assert restore_context(client, project.id, "lyrics").status_code == 409


def test_song_context_mapping_field_names_and_labels_cannot_drift():
    """One mapping, and the route's path segment, the slots and the labels all derived from it.

    The save loop and the restore route reach the fields by interpolation
    (`f"{field}{RECOVERY_SLOT_SUFFIX}"`), so a key in `SONG_CONTEXT_LABELS` that `Song` does not
    carry is an `AttributeError` at request time rather than a startup failure.
    """
    assert set(SONG_CONTEXT_LABELS) == set(get_args(SongContextField))
    for field in SONG_CONTEXT_LABELS:
        assert field in Song.model_fields, field
        assert f"{field}_previous" in Song.model_fields, field
        # The slot is `str | None`, which is what lets "kept a blank" differ from "kept nothing".
        assert Song.model_fields[f"{field}_previous"].default is None, field
        assert Song(title="t", source="imported").model_dump()[f"{field}_previous"] is None
    # The two spellings of the same two things — mid-sentence for a length refusal, sentence-initial
    # for a restore — must name the same fields, or one refusal contradicts the other.
    for field, label in SONG_CONTEXT_LABELS.items():
        assert label.lower() in SONG_CONTEXT_FIELD_NAMES[field].lower(), field


def test_neither_recovery_slot_reaches_the_director(tmp_path: Path):
    """The correctness rule, proven against what the model was actually handed.

    A slot leaking into the dump means the Director reads back the lyric sheet the Director
    deliberately discarded — the model working from text that was superseded on purpose, presented
    as current. That a field is on the Song proves only that it was stored, so the recording double
    is the only witness that can settle this.
    """
    director = RevisingDirector()
    client, store = make_client_with_director(tmp_path, director)
    project = store.create(Project(name="Slots stay out"))
    import_song(client, project.id, lyrics=IMPORTED_LYRIC_SHEET, caption=IMPORTED_SONG_STYLE)
    client.put(
        f"/api/projects/{project.id}/song/context",
        json={"lyrics": "The sheet the model is meant to read.", "caption": "The current sound."},
    )
    kept = store.get(project.id).song
    assert kept.lyrics_previous == IMPORTED_LYRIC_SHEET

    client.post(f"/api/projects/{project.id}/director/chat", json={"message": "What is this?"})

    context = director.contexts[0]
    serialised = json.dumps(context)
    for field in SONG_CONTEXT_LABELS:
        assert f"{field}_previous" not in context["song"], field
    # And the discarded text itself is nowhere in what was encoded into the prompt — a key present
    # but empty would satisfy the check above on its own.
    assert "counting sodium lights" not in serialised
    assert IMPORTED_SONG_STYLE not in serialised
    # The live context is still there, which is the whole reason these fields exist.
    assert context["song"]["lyrics"] == "The sheet the model is meant to read."
    assert context["song"]["caption"] == "The current sound."


def test_a_new_song_field_cannot_be_added_without_deciding_what_the_director_sees():
    """The nested-path hazard, answered by making forgetting loud instead of silent.

    `DIRECTOR_CONTEXT_EXCLUDE` strips whole top-level keys, and its own comment warns that a nested
    path stops covering a field renamed or added beside it — silently. These two slots live inside
    `song`, so a nested path is exactly the wrong shape: `{"song": {"lyrics_previous"}}` would stay
    valid, match nothing new, and leak a third slot into every prompt with the whole suite green.

    So the exclusion is not a path but a classification, and this test is the proof. A hypothetical
    new `Song` field is introduced here — the drift that would happen for real — and the guard is
    shown to refuse to build an exclusion at all rather than build an incomplete one. Both
    directions of drift are covered: a field the model declares and nobody classified, and a
    classification of a field the model no longer declares.
    """

    class SongWithANewField(Song):
        bpm_previous: str | None = None

    with pytest.raises(RuntimeError) as unclassified:
        _withheld_fields(
            SongWithANewField,
            visible=SONG_DIRECTOR_VISIBLE,
            withheld=SONG_DIRECTOR_WITHHELD,
            family="SONG",
        )

    # It names the field and says what to do about it, because a loud failure nobody can act on is
    # only a different kind of obstacle.
    assert "bpm_previous" in str(unclassified.value)
    assert "SONG_DIRECTOR_WITHHELD" in str(unclassified.value)

    # Classified as withheld, the same hypothetical field is covered without another edit anywhere:
    # this is the "the guard still covers it" half of the claim.
    covered = _withheld_fields(
        SongWithANewField,
        visible=SONG_DIRECTOR_VISIBLE,
        withheld=SONG_DIRECTOR_WITHHELD | {"bpm_previous"},
        family="SONG",
    )
    assert "bpm_previous" in covered
    song = SongWithANewField(title="t", source="imported", bpm_previous="128")
    assert "bpm_previous" not in song.model_dump(exclude=covered)

    # Classified as visible, it reaches the model — the classification is a decision, not a filter
    # that only ever hides things.
    shown = _withheld_fields(
        SongWithANewField,
        visible=SONG_DIRECTOR_VISIBLE | {"bpm_previous"},
        withheld=SONG_DIRECTOR_WITHHELD,
        family="SONG",
    )
    assert "bpm_previous" in song.model_dump(exclude=shown)

    # Drift the other way: a classification naming a field the model no longer declares covers
    # nothing, and saying so is the difference between a renamed slot and a leaked one.
    with pytest.raises(RuntimeError) as stale:
        _withheld_fields(
            Song,
            visible=SONG_DIRECTOR_VISIBLE,
            withheld=SONG_DIRECTOR_WITHHELD | {"lyrics_backup"},
            family="SONG",
        )
    assert "lyrics_backup" in str(stale.value)

    # And a field classified as both is a decision that was never actually made.
    with pytest.raises(RuntimeError) as overlap:
        _withheld_fields(
            Song,
            visible=SONG_DIRECTOR_VISIBLE | {"lyrics_previous"},
            withheld=SONG_DIRECTOR_WITHHELD,
            family="SONG",
        )
    assert "lyrics_previous" in str(overlap.value)

    # The live classification is complete right now, which is what makes the import at the top of
    # this module succeed at all — asserted rather than assumed, so a future edit that silences the
    # guard by widening a set to `Song.model_fields` fails here.
    assert _withheld_fields(
        Song, visible=SONG_DIRECTOR_VISIBLE, withheld=SONG_DIRECTOR_WITHHELD, family="SONG"
    ) == {"lyrics_previous", "caption_previous"}
    assert not SONG_DIRECTOR_VISIBLE & SONG_DIRECTOR_WITHHELD


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
    """30 and 300 are the M3SongPlanner floor and ceiling; both must reach the payload.

    300 s needs a headroom of 1.0 to be submittable at all: at the default 1.5 the encoder's
    ceiling would be 450 s, past the 360 s the node accepts. The duration range is unchanged —
    both endpoints still reach the planner — but the headroom the Director chooses now decides
    which of them can be asked for, which is exactly the choice the field exists to expose.
    """
    client, store, comfy = make_client(tmp_path)

    for duration, headroom in ((30, 1.5), (300, 1.0)):
        project = store.create(Project(name=f"Bound {duration}"))
        response = client.post(
            f"/api/projects/{project.id}/generate/songplanner",
            json={
                "title": "Bounded",
                "idea": "an idea",
                "duration": duration,
                "duration_headroom": headroom,
            },
        )

        assert response.status_code == 202, duration
        planner = next(
            node for node in comfy.prompts[-1].values() if node["class_type"] == "M3SongPlanner"
        )
        assert planner["inputs"]["duration_seconds"] == duration


def test_songplanner_headroom_moves_only_the_encoder_ceiling(tmp_path: Path):
    """The planner is asked for the length that was asked for; only the ceiling is multiplied.

    `M3SongPlanner.duration_seconds` says how long a song to write and
    `MiniMaxMusic3TextEncode.max_duration` caps the encoder's latent length; the route used to
    hand the same number to both, so a song whose lyrics ran slightly long lost its ending.
    Both variants are checked because a supplied lyric sheet can overrun just as easily.
    """
    client, store, comfy = make_client(tmp_path)
    lyrics = "[Verse]\nStatic in the wires"
    cases = {
        # Omitted headroom is the creator's documented 1.5: 60 s asked for, 90 s of room.
        "default": ({"title": "Default", "idea": "an idea", "duration": 60}, 90.0),
        "explicit": (
            {"title": "Explicit", "idea": "an idea", "duration": 60, "duration_headroom": 2.5},
            150.0,
        ),
        "none wanted": (
            {"title": "None", "idea": "an idea", "duration": 60, "duration_headroom": 1.0},
            60.0,
        ),
        "known lyrics": (
            {"title": "Cover", "idea": "an idea", "duration": 60, "lyrics": lyrics},
            90.0,
        ),
    }

    for label, (body, expected_ceiling) in cases.items():
        project = store.create(Project(name=label))
        response = client.post(
            f"/api/projects/{project.id}/generate/songplanner", json=body
        )

        assert response.status_code == 202, (label, response.text)
        payload = comfy.prompts[-1]
        planner = next(node for node in payload.values() if node["class_type"] == "M3SongPlanner")
        encoder = next(
            node for node in payload.values() if node["class_type"] == "MiniMaxMusic3TextEncode"
        )
        assert planner["inputs"]["duration_seconds"] == 60, label
        assert encoder["inputs"]["max_duration"] == expected_ceiling, label
        # What is stored is the song that was asked for, not the room it was given.
        assert store.get(project.id).song.duration == 60, label


def test_songplanner_refuses_a_headroom_that_leaves_the_encoder_schema(tmp_path: Path):
    """450 s of ceiling is refused here, naming both numbers, rather than as an opaque 502.

    The product of a duration the route accepts and a headroom the route accepts can still be
    outside `MiniMaxMusic3TextEncode`'s 0.04-360 s range, and ComfyUI would reject the whole
    prompt at `/prompt` validation. Refusing locally costs no GPU time and says which of the
    two numbers to lower; clamping the ceiling instead would silently reintroduce the
    truncation the headroom exists to prevent.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Past the ceiling"))

    response = client.post(
        f"/api/projects/{project.id}/generate/songplanner",
        json={"title": "Too much room", "idea": "an idea", "duration": 300},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "450" in detail and "360" in detail, detail
    assert "300" in detail and "1.5" in detail, detail
    assert comfy.prompts == []
    saved = store.get(project.id)
    assert saved.jobs == []
    assert saved.song is None


def test_songplanner_refuses_a_headroom_below_one_or_past_the_field_bound(tmp_path: Path):
    """A ceiling under the target can only truncate; above 12 no accepted duration is legal."""
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Headroom bounds"))
    invalid = (0.99, 0.0, -1.0, 12.01, 1000.0)

    for headroom in invalid:
        response = client.post(
            f"/api/projects/{project.id}/generate/songplanner",
            json={
                "title": "T",
                "idea": "an idea",
                "duration": 60,
                "duration_headroom": headroom,
            },
        )
        assert response.status_code == 422, headroom
        assert any(
            "duration_headroom" in item["loc"] for item in response.json()["detail"]
        ), headroom
    # 12.0 is the bound itself, and 30 s is the only duration it can legally multiply.
    accepted = client.post(
        f"/api/projects/{project.id}/generate/songplanner",
        json={"title": "T", "idea": "an idea", "duration": 30, "duration_headroom": 12.0},
    )
    assert accepted.status_code == 202
    encoder = next(
        node
        for node in comfy.prompts[-1].values()
        if node["class_type"] == "MiniMaxMusic3TextEncode"
    )
    assert encoder["inputs"]["max_duration"] == 360.0
    assert len(comfy.prompts) == 1


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


def upload_asset(client, project_id: str, name: str, kind: str, filename: str) -> dict:
    """One uploaded reference Asset, returned as its own record rather than the whole list."""
    response = client.post(
        f"/api/projects/{project_id}/assets/upload",
        data={"name": name, "kind": kind},
        files={"file": (filename, f"{name}-bytes".encode(), "application/octet-stream")},
    )
    assert response.status_code == 200, response.text
    return response.json()["assets"][-1]


def reference_shot(store, project_id: str, **fields) -> str:
    """A ready, prompted Shot with the given reference wiring. Returns its id."""
    project = store.get(project_id)
    shot = Shot(
        start=0,
        duration=5,
        prompt="The vocalists perform the chorus together.",
        status="ready",
        **fields,
    )
    project.shots = [shot]
    store.save(project)
    return shot.id


def submit_h3(client, project_id: str, shot_id: str, **body):
    return client.post(
        f"/api/projects/{project_id}/shots/{shot_id}/generate/h3", json=body
    )


def test_h3_reference_tags_are_numbered_per_kind_in_attachment_order(tmp_path: Path):
    """FR-19's determinism at the route: a fixed attachment order gives fixed tags.

    Mixed kinds interleaved on purpose — the picture after the video is `<Picture 2>`, not
    `<Picture 3>` — because one counter shared across kinds would number the prompt's tags
    differently from the per-kind slots the conditioner is wired to, and the model would be
    told to look at media it was never handed.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Numbering"))
    lead = upload_asset(client, project.id, "Lead vocalist", "character", "lead.png")
    pan = upload_asset(client, project.id, "Camera pan", "video", "pan.mp4")
    stage = upload_asset(client, project.id, "Stage", "setting", "stage.png")
    room = upload_asset(client, project.id, "Room tone", "audio", "room.flac")
    client.post(
        f"/api/projects/{project.id}/songs/upload",
        data={"title": "Duet", "duration": "5"},
        files={"file": ("duet.flac", b"fLaCfake", "audio/flac")},
    )
    order = [lead["id"], pan["id"], stage["id"], room["id"]]
    shot_id = reference_shot(store, project.id, asset_ids=order, use_song_audio=True)

    assert submit_h3(client, project.id, shot_id).status_code == 202

    conditioner = comfy.prompts[-1]["mvp:condition"]["inputs"]
    assert conditioner["prompt"].startswith(
        "Reference map: <Picture 1> is Lead vocalist; <Video 1> is Camera pan; "
        "<Picture 2> is Stage; <Audio 1> is Room tone; "
        "<Audio 2> is the master song for synchronization."
    )
    assert conditioner["ref_images.ref_image_0"] == ["mvp:split", 0]
    assert conditioner["ref_images.ref_image_1"] == ["mvp:split", 1]
    assert conditioner["ref_videos.ref_video_0"] == ["mvp:split", 9]
    assert conditioner["ref_audios.ref_audio_0"] == ["mvp:split", 15]
    assert conditioner["ref_audios.ref_audio_1"] == ["mvp:split", 16]

    # Reordering the attachments reorders the tags: the numbering is a fact about the
    # attachment order, not about the Asset library's order or the ids.
    reordered = reference_shot(
        store, project.id, asset_ids=[stage["id"], lead["id"], *order[1:2], room["id"]],
        use_song_audio=True,
    )
    assert submit_h3(client, project.id, reordered).status_code == 202
    assert comfy.prompts[-1]["mvp:condition"]["inputs"]["prompt"].startswith(
        "Reference map: <Picture 1> is Stage; <Picture 2> is Lead vocalist; "
        "<Video 1> is Camera pan; <Audio 1> is Room tone; "
        "<Audio 2> is the master song for synchronization."
    )


def test_h3_routes_to_the_reference_payload_only_when_something_is_attached(tmp_path: Path):
    """FR-20's routing rule, as the pair it is — each asserting the other's marker is absent.

    The route now branches on `resolve_shot_mode`, and this is the *undeclared* half of it: both
    Shots here carry a legacy `mode` string, which resolves to "no declaration was ever made", so
    each routes on what it behaves as. That is the migration, and it is why the Shot saying
    `mode="text"` while carrying an Asset still renders as a reference shot — exactly as it did
    before the mode became declarable.

    Asserting only that the expected node is present would pass for a payload that carried both
    branches' nodes.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Routing"))
    lead = upload_asset(client, project.id, "Lead", "character", "lead.png")

    attached = reference_shot(store, project.id, asset_ids=[lead["id"]], mode="text")
    assert submit_h3(client, project.id, attached).status_code == 202
    reference_payload = comfy.prompts[-1]
    classes = {node["class_type"] for node in reference_payload.values()}
    assert "MiniMaxH3ReferenceToVideo" in classes
    assert "MiniMaxH3MediaLoader" in classes
    assert "MiniMaxH3DirectorCS" not in classes

    bare = reference_shot(store, project.id, mode="reference")
    assert submit_h3(client, project.id, bare).status_code == 202
    text_payload = comfy.prompts[-1]
    classes = {node["class_type"] for node in text_payload.values()}
    assert "MiniMaxH3DirectorCS" in classes
    assert "MiniMaxH3ReferenceToVideo" not in classes
    assert "MiniMaxH3MediaLoader" not in classes


def test_h3_song_only_shot_routes_to_the_reference_payload(tmp_path: Path):
    """`use_song_audio` alone, with no Assets: the song is a reference like any other."""
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Song only"))
    client.post(
        f"/api/projects/{project.id}/songs/upload",
        data={"title": "Duet", "duration": "5"},
        files={"file": ("duet.flac", b"fLaCfake", "audio/flac")},
    )
    shot_id = reference_shot(store, project.id, asset_ids=[], use_song_audio=True)

    assert submit_h3(client, project.id, shot_id).status_code == 202

    payload = comfy.prompts[-1]
    conditioner = payload["mvp:condition"]["inputs"]
    media = json.loads(payload["mvp:references"]["inputs"]["media_state"])
    assert [item["kind"] for item in media] == ["audio"]
    assert media[0]["label"] == "master song"
    assert conditioner["ref_audios.ref_audio_0"] == ["mvp:split", 15]
    assert "<Audio 1> is the master song for synchronization" in conditioner["prompt"]


def test_h3_refuses_a_reference_whose_file_is_gone_before_anything_is_submitted(tmp_path: Path):
    """Both file resolutions, in the wording they already use. Nothing reaches ComfyUI."""
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Missing media"))
    lead = upload_asset(client, project.id, "Lead vocalist", "character", "lead.png")
    client.post(
        f"/api/projects/{project.id}/songs/upload",
        data={"title": "Duet", "duration": "5"},
        files={"file": ("duet.flac", b"fLaCfake", "audio/flac")},
    )
    shot_id = reference_shot(store, project.id, asset_ids=[lead["id"]], use_song_audio=True)
    project = store.get(project.id)
    asset_file = store.project_dir(project.id) / project.assets[0].path
    song_file = store.project_dir(project.id) / project.song.path
    asset_file.unlink()

    missing_asset = submit_h3(client, project.id, shot_id)

    assert missing_asset.status_code == 404
    assert missing_asset.json()["detail"] == "Asset media was not found: Lead vocalist"

    asset_file.write_bytes(b"restored")
    song_file.unlink()
    missing_song = submit_h3(client, project.id, shot_id)

    assert missing_song.status_code == 404
    assert missing_song.json()["detail"] == "Song media was not found"
    assert comfy.prompts == []


def test_h3_refuses_more_references_than_the_node_has_slots_for(tmp_path: Path):
    """The per-kind limit as a 422 through the route, not as a 502 from ComfyUI."""
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Too many"))
    assets = [
        upload_asset(client, project.id, f"Picture {index}", "image", f"picture-{index}.png")
        for index in range(10)
    ]
    shot_id = reference_shot(store, project.id, asset_ids=[asset["id"] for asset in assets])

    response = submit_h3(client, project.id, shot_id)

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "H3 accepts at most 9 picture references per shot and this one has 10"
    )
    assert comfy.prompts == []


def test_h3_counts_the_master_song_against_the_audio_limit_and_says_so(tmp_path: Path):
    """The boundary the route actually produces: three audio Assets *plus* the song.

    The song is appended as a fourth `audio` reference, so this is the one over-limit
    case a Director can reach while looking at exactly three attached audio Assets. A
    refusal reading "at most 3 standalone audios" sends them counting the wrong things,
    which is why the message says what was counted.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Song over the limit"))
    assets = [
        upload_asset(client, project.id, f"Stem {index}", "audio", f"stem-{index}.flac")
        for index in range(3)
    ]
    client.post(
        f"/api/projects/{project.id}/songs/upload",
        data={"title": "Master", "duration": "5"},
        files={"file": ("master.flac", b"fLaCfake", "audio/flac")},
    )
    ids = [asset["id"] for asset in assets]

    # Three attached audio Assets and no song is exactly at the limit and must submit.
    at_limit = reference_shot(store, project.id, asset_ids=ids)
    assert submit_h3(client, project.id, at_limit).status_code == 202

    over = reference_shot(store, project.id, asset_ids=ids, use_song_audio=True)
    response = submit_h3(client, project.id, over)

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "H3 accepts at most 3 audio references per shot and this one has 4 "
        "(the master song counts as one)"
    )
    assert len(comfy.prompts) == 1


def test_every_asset_kind_maps_to_the_reference_kind_the_graph_expects(tmp_path: Path):
    """The `video`/`audio`/else mapping, over every kind the model actually allows.

    Driven off `AssetKind` rather than a hand-written list: the fallback sends anything
    that is not video or audio into a *picture* slot, so a kind added to the model later
    would silently burn one — and a picture slot spent on a non-image is a reference the
    model is told to look at and cannot use. This fails the day a new kind appears.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Kinds"))
    expected = {"video": "video", "audio": "audio"}
    files = {"video": "clip.mp4", "audio": "stem.flac"}
    for kind in get_args(AssetKind):
        asset = upload_asset(
            client, project.id, f"{kind} reference", kind, files.get(kind, "still.png")
        )
        shot_id = reference_shot(store, project.id, asset_ids=[asset["id"]])

        assert submit_h3(client, project.id, shot_id).status_code == 202, kind

        media = json.loads(comfy.prompts[-1]["mvp:references"]["inputs"]["media_state"])
        assert [item["kind"] for item in media] == [expected.get(kind, "picture")], kind


def test_h3_refuses_a_window_past_the_node_frame_ceiling(tmp_path: Path):
    """A Shot longer than the node's `length` maximum, refused locally and named."""
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Too long"))
    lead = upload_asset(client, project.id, "Lead", "character", "lead.png")
    project = store.get(project.id)
    shot = Shot(
        start=0,
        duration=200,  # 4800 frames at 24 fps, well past the 3600 the node accepts
        prompt="A very long take.",
        status="ready",
        asset_ids=[lead["id"]],
    )
    project.shots = [shot]
    store.save(project)

    response = submit_h3(client, project.id, shot.id)

    assert response.status_code == 422
    assert f"{H3_REFERENCE_MAX_FRAMES}-frame maximum" in response.json()["detail"]
    assert comfy.prompts == []


def test_h3_refuses_a_reference_size_the_node_does_not_offer(tmp_path: Path):
    """Refused by `H3Request`'s `Literal`, before the route body runs.

    Asserting only the status code would pass with the builder's own guard deleted *and*
    with the `Literal` widened to `str`, because a 422 is also what a dozen other refusals
    return. The detail is what says which of them happened: this one names the field and
    the two values, and never reaches `build_h3_reference_payload` at all — the builder's
    matching guard is covered in `tests/test_workflows.py`, where it is reachable.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Bad size"))
    lead = upload_asset(client, project.id, "Lead", "character", "lead.png")
    shot_id = reference_shot(store, project.id, asset_ids=[lead["id"]])

    response = submit_h3(client, project.id, shot_id, ref_image_size="2048")

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["body", "ref_image_size"]
    assert set(detail[0]["ctx"]["expected"].split(" or ")) == {"'match'", "'max'"}
    assert comfy.prompts == []


def test_h3_refuses_a_text_only_window_past_the_director_nodes_maxima(tmp_path: Path):
    """The text-only branch had no ceiling, and it is the one with live render evidence.

    Both refusals are 422s naming the value that would have been sent, rather than the
    opaque 502 a `/prompt` validation failure arrives as after the submission round-trip.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Long text shot"))
    project.shots = [
        Shot(start=0, duration=500, prompt="A very long take.", status="ready"),
        Shot(start=400, duration=20, prompt="A take that ends past the ceiling.", status="ready"),
        Shot(start=500, duration=5, prompt="A shot far down the song.", status="ready"),
    ]
    store.save(project)

    refusals = [
        submit_h3(client, project.id, shot.id).json()["detail"] for shot in project.shots
    ]

    assert all(f"maximum of {H3_DIRECTOR_MAX_FRAMES}" in detail for detail in refusals), refusals
    # Each names the literal that would have gone out — over-render margin included, since
    # that is what would actually be sent — which is what tells a Director whether the
    # window is too long or merely too far down the song.
    assert "duration_frames=12024" in refusals[0]
    assert "end_frame=10098" in refusals[1]
    assert "start_frame=12000" in refusals[2]
    assert comfy.prompts == []


def test_a_window_that_is_not_a_finite_number_is_refused_rather_than_raising(tmp_path: Path):
    """`1e999` parses to `inf`, clears `gt=0`, and then raises inside `round()`.

    `OverflowError` is not `TimelineError`, so the compile route's translation missed it and
    the client got a 500 for a window its own request model accepted. This is the one route
    that takes a window straight from the request body, which makes it the reachable case;
    both builders carry the same guard, unit-tested in `tests/test_workflows.py`, because a
    stored Shot cannot hold `inf` — pydantic serialises it to `null` and the manifest then
    refuses to load, which is a separate hole in a separate story.
    """
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Infinite window"))
    project.shots = [Shot(start=0, duration=5, prompt="A take.", status="ready")]
    store.save(project)

    infinite = client.post(
        f"/api/projects/{project.id}/timeline/compile",
        content=json.dumps({"window_start": 0, "window_duration": 1e999}),
        headers={"content-type": "application/json"},
    )
    finite = client.post(
        f"/api/projects/{project.id}/timeline/compile",
        json={"window_start": 0, "window_duration": 5},
    )

    assert infinite.status_code == 422
    assert infinite.json()["detail"] == "Timeline window must be a finite number of seconds"
    assert finite.status_code == 200


def test_h3_translates_every_reference_builder_refusal_into_a_422(tmp_path: Path, monkeypatch):
    """The blanket `except ValueError` around the builder, checked against the real refusals.

    Each message is produced by *calling* the builder rather than by copying its wording, so
    a reworded refusal cannot leave this test asserting a sentence the code no longer says.
    Two of the four are unreachable through the route as it stands — the route never passes an
    empty reference list or a kind it did not itself map — and that is exactly why they are
    driven this way: the translation is what is under test, not the reachability.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Refusals"))
    lead = upload_asset(client, project.id, "Lead", "character", "lead.png")
    shot_id = reference_shot(store, project.id, asset_ids=[lead["id"]])
    valid = {
        "prompt": "p", "duration": 5, "width": 1280, "height": 720,
        "steps": 20, "seed": 0, "prefix": "p",
    }
    refusals = (
        {**valid, "references": [{"kind": "picture"}] * 10},
        {**valid, "references": []},
        {**valid, "references": [{"kind": "picture"}], "ref_image_size": "2048"},
        {**valid, "references": [{"kind": "sculpture"}]},
        {**valid, "references": [{"kind": "picture"}], "duration": 200},
    )

    for arguments in refusals:
        with pytest.raises(ValueError) as raised:
            build_h3_reference_payload(**arguments)
        message = str(raised.value)

        def refusing_builder(*_, _message=message, **__):
            raise ValueError(_message)

        monkeypatch.setattr(
            "music_video_producer.app.build_h3_reference_payload", refusing_builder
        )
        response = submit_h3(client, project.id, shot_id)

        assert response.status_code == 422, message
        assert response.json()["detail"] == message
    assert comfy.prompts == []


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


def render_again(client, project_id: str, shot_id: str, **kwargs):
    """The purpose-built re-open. No body, by design — see `render_again` in app.py."""
    return client.post(f"/api/projects/{project_id}/shots/{shot_id}/render-again", **kwargs)


def land_take(client, comfy, project_id: str, job_id: str, filename: str):
    """Refresh one job as a completed render that wrote `filename`, the way ComfyUI reports it.

    The numbering is the point of the argument. ComfyUI's savers derive the output name from the
    workflow's `filename_prefix` and append a five-digit counter, so a second render of one shot
    writes `…_00002` beside `…_00001` rather than over it. Verified against this installation
    rather than taken from the spec: one shot's prefix under the ComfyUI output root carries
    `_00001`, `_00002` and `_00003` side by side, all three produced by re-renders of that shot.
    """

    async def completed_history(prompt_id):
        return type(
            "History",
            (),
            {
                "prompt_id": prompt_id,
                "status": "complete",
                "outputs": [
                    {"subfolder": f"music-video-producer/{project_id}/shots", "filename": filename}
                ],
                "error": "",
            },
        )()

    comfy.history = completed_history
    response = client.get(f"/api/projects/{project_id}/jobs/{job_id}")
    assert response.status_code == 200, response.text
    return response.json()


def rendered_shot(client, store, comfy, name: str, *, filename: str = "shot_take-h3_00001.mp4"):
    """A project whose single Shot has actually been through a render, start to finish.

    Built by driving the real routes rather than by writing `status="complete"` onto a Shot,
    because every interesting thing about re-rendering is about what a *completed* render left
    behind — the job record, the prompt id and the output pointer — and a hand-built Shot has
    none of it. Returns the project id, the shot id and the first job's id.
    """
    project = store.create(Project(name=name))
    project.shots = [
        Shot(
            id="shot_take",
            start=0,
            duration=5,
            prompt="A singer turns toward camera",
            mode="text",
            status="ready",
        )
    ]
    store.save(project)
    submitted = submit_h3(client, project.id, "shot_take")
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["id"]
    land_take(client, comfy, project.id, job_id, filename)
    assert store.get(project.id).shots[0].status == "complete"
    return project.id, "shot_take", job_id


def test_render_again_reopens_a_completed_shot_so_the_interface_alone_can_queue_it(tmp_path: Path):
    """The whole point: comparing two takes without an API client.

    Both halves are asserted, because either alone is a half-built feature. The status has to
    become `ready` — that is the state the batch button filters on and the state the submission
    route requires — and a submission through the ordinary route then has to be accepted, which
    is what makes "a new render can be queued through the interface alone" true rather than
    merely plausible.
    """
    client, store, comfy = make_client(tmp_path)
    project_id, shot_id, _ = rendered_shot(client, store, comfy, "Second take")

    response = render_again(client, project_id, shot_id)

    assert response.status_code == 200, response.text
    reopened = ProjectStore(tmp_path).get(project_id).shots[0]
    assert reopened.status == "ready"
    # Re-opening is not itself a render, and the take already there is still this Shot's take
    # until a new one lands. Nothing was spent and nothing was displaced by the click.
    assert reopened.latest_output.endswith("shot_take-h3_00001.mp4")
    assert len(comfy.prompts) == 1

    assert submit_h3(client, project_id, shot_id).status_code == 202
    assert len(comfy.prompts) == 2
    assert ProjectStore(tmp_path).get(project_id).shots[0].status == "queued"


def test_render_again_writes_the_status_and_nothing_else(tmp_path: Path):
    """The reason this is its own action rather than the shots write it replaces.

    `PUT /shots` is the generic full-project-shaped route — it takes the whole Shot list from the
    client — so using it to walk one status back also reasserts every prompt, window, reference
    and lock the client happens to be holding, from however long ago it loaded them. This route
    binds no body at all, so there is nothing on the wire that could do that.

    Asserted as a whole-manifest comparison rather than by checking a few fields: a field-by-field
    check only catches the fields somebody thought of, and the hazard here is precisely the field
    nobody thought of. A body is posted too, and must be ignored — a handler that grew one later
    would fail this.
    """
    client, store, comfy = make_client(tmp_path)
    project_id, shot_id, _ = rendered_shot(client, store, comfy, "Narrow write")
    stored = store.get(project_id)
    stored.shots.append(
        Shot(id="shot_other", start=10, duration=5, prompt="A second shot", locked=True)
    )
    store.save(stored)
    before = ProjectStore(tmp_path).get(project_id).model_dump(mode="json")

    response = render_again(
        client,
        project_id,
        shot_id,
        json={"shots": [{"id": "shot_other", "start": 0, "duration": 1, "prompt": "OVERWRITTEN"}]},
    )

    assert response.status_code == 200, response.text
    after = ProjectStore(tmp_path).get(project_id).model_dump(mode="json")
    # The one intended difference, isolated by neutralising it and comparing everything else.
    assert before["shots"][0]["status"] == "complete"
    assert after["shots"][0]["status"] == "ready"
    after["shots"][0]["status"] = before["shots"][0]["status"]
    after["updated_at"] = before["updated_at"]
    assert after == before


@pytest.mark.parametrize(
    "prompt",
    ["", "   \n\t", "New shot", "  new   SHOT  "],
    ids=["blank", "whitespace", "placeholder", "collapsed-placeholder"],
)
def test_render_again_refuses_a_shot_whose_prompt_was_emptied_after_it_rendered(
    tmp_path: Path, prompt: str
):
    """The design note, executed: the gate is asked again rather than remembered.

    This Shot has already satisfied the readiness gate — it rendered, successfully, with a real
    prompt — and the tempting implementation reads that as permission: it rendered once, so its
    prompt is evidently fine. It is not. A prompt can be deleted, or pasted over with the
    `"New shot"` placeholder a duplicate carries, between one render and the next, and the gate
    exists to stop a full GPU pass returning noise rather than to count renders.

    Both emptiness cases and both of their spacing variants, because the collapse is what catches
    a placeholder that picked up stray spacing on the way through a duplicate.
    """
    client, store, comfy = make_client(tmp_path)
    project_id, shot_id, _ = rendered_shot(client, store, comfy, "Emptied after rendering")
    stored = store.get(project_id)
    stored.shots[0].prompt = prompt
    store.save(stored)

    response = render_again(client, project_id, shot_id)

    assert response.status_code == 422
    # The same sentence a first render would have refused with: one rule, one wording.
    assert response.json()["detail"] == readiness_refusal([f"SHOT 01 ({shot_id})"])
    # And it was genuinely not re-opened, rather than refused after the write.
    assert ProjectStore(tmp_path).get(project_id).shots[0].status == "complete"


def test_render_again_refuses_an_approved_take_and_says_why_rather_than_that_it_refused(
    tmp_path: Path,
):
    """The one refusal here that is about meaning, and it has to read that way.

    Nothing technical stops a second render over an approved take. What stops it is that an
    approval is a decision about one specific piece of media, and afterwards the decision would
    be attached to something nobody approved. So the message is asserted to actually say that,
    not merely to be a 422 — a refusal that says "refused" leaves the Director with no idea that
    clearing the approval is the thing they are being asked to decide.

    Both approval signals, because they can be set independently and a Shot carrying either is
    a Shot somebody has made a decision about. `approved_output` is the one AGENTS.md calls the
    editorial decision; the `approved` status is reachable by hand and must not be a way past it.
    """
    client, store, comfy = make_client(tmp_path)
    project_id, shot_id, _ = rendered_shot(client, store, comfy, "Approved")
    stored = store.get(project_id)
    stored.shots[0].approved_output = stored.shots[0].latest_output
    store.save(stored)

    response = render_again(client, project_id, shot_id)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail == RENDER_AGAIN_APPROVED_REFUSAL.format(shot=f"SHOT 01 ({shot_id})")
    # The approval is named as the reason, and the consequence is stated: this is the sentence
    # that has to survive a rewrite, not the status code.
    assert "approved take" in detail
    assert "no longer exists" in detail
    assert "Clear the approval" in detail
    assert ProjectStore(tmp_path).get(project_id).shots[0].status == "complete"

    # The status half of the same decision, with no approved_output at all.
    stored = store.get(project_id)
    stored.shots[0].approved_output = ""
    stored.shots[0].status = "approved"
    store.save(stored)

    assert render_again(client, project_id, shot_id).status_code == 422
    assert ProjectStore(tmp_path).get(project_id).shots[0].status == "approved"


def test_render_again_refuses_a_locked_shot(tmp_path: Path):
    """Consistent with everything else that respects `Shot.locked`.

    A lock is a deliberate hands-off, and re-opening a Shot for another render is exactly the
    class of change it exists to refuse — the same argument `expand_shot_prompts` makes when it
    leaves a locked Shot's prompt alone. The remedy is stated for the same reason it is there:
    a lock stops an action, it does not stop the human who set it from unsetting it.
    """
    client, store, comfy = make_client(tmp_path)
    project_id, shot_id, _ = rendered_shot(client, store, comfy, "Locked")
    stored = store.get(project_id)
    stored.shots[0].locked = True
    store.save(stored)

    response = render_again(client, project_id, shot_id)

    assert response.status_code == 422
    assert response.json()["detail"] == RENDER_AGAIN_LOCKED_REFUSAL.format(
        shot=f"SHOT 01 ({shot_id})"
    )
    assert "Unlock" in response.json()["detail"]
    assert ProjectStore(tmp_path).get(project_id).shots[0].status == "complete"

    stored = store.get(project_id)
    stored.shots[0].locked = False
    store.save(stored)
    assert render_again(client, project_id, shot_id).status_code == 200


def test_render_again_refuses_a_shot_with_a_render_still_in_flight(tmp_path: Path):
    """Two renders of one shot would race on its output, so the second is refused.

    The second case is the one that matters and the one a status-only check misses. `Shot.status`
    is exactly what the generic shots write can rewrite by hand — that is how this whole story
    started — so a Shot can read `complete` on screen while ComfyUI is still working on it. The
    job record is what still knows, and it is keyed by `target_id`.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="In flight"))
    project.shots = [
        Shot(id="shot_live", start=0, duration=5, prompt="A singer turns", mode="text",
             status="ready")
    ]
    store.save(project)
    submitted = submit_h3(client, project.id, "shot_live")
    assert submitted.status_code == 202

    # The ordinary case: the Shot itself says a render is out.
    queued = render_again(client, project.id, "shot_live")
    assert queued.status_code == 409
    assert queued.json()["detail"] == RENDER_AGAIN_IN_FLIGHT_REFUSAL.format(
        shot="SHOT 01 (shot_live)"
    )
    assert "race" in queued.json()["detail"]

    # ...and the same Shot with its status walked back by hand while the job is still out.
    stored = store.get(project.id)
    stored.shots[0].status = "complete"
    store.save(stored)

    hidden = render_again(client, project.id, "shot_live")
    assert hidden.status_code == 409, "a hand-edited status hid a live render from the guard"
    assert ProjectStore(tmp_path).get(project.id).shots[0].status == "complete"

    # Once the job has actually landed, the same request is accepted.
    land_take(client, comfy, project.id, submitted.json()["id"], "shot_live-h3_00001.mp4")
    assert render_again(client, project.id, "shot_live").status_code == 200
    assert ProjectStore(tmp_path).get(project.id).shots[0].status == "ready"


def test_render_again_reopens_a_shot_whose_render_failed(tmp_path: Path):
    """A failed render is the likeliest thing anyone wants to try again."""
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Errored"))
    project.shots = [
        Shot(id="shot_bad", start=0, duration=5, prompt="A singer turns", mode="text",
             status="error", latest_output="")
    ]
    store.save(project)

    assert render_again(client, project.id, "shot_bad").status_code == 200
    assert ProjectStore(tmp_path).get(project.id).shots[0].status == "ready"


@pytest.mark.parametrize("status_before", ["draft", "ready"])
def test_render_again_leaves_a_shot_that_never_rendered_exactly_as_it_was(
    tmp_path: Path, status_before: str
):
    """Nothing to render again, so nothing happens — and it is not an error.

    `draft` carrying the `"New shot"` placeholder is what every Shot the interface creates is,
    and by far the commonest state in the application. Refusing it here would turn that state
    into a failure and would refuse it for its *prompt*, which is not the reason: it was never
    being re-opened. The no-op is therefore checked before any refusal, and this pins that order.
    """
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Never rendered"))
    project.shots = [
        Shot(id="shot_new", start=0, duration=5, prompt="New shot", status=status_before)
    ]
    store.save(project)
    before = ProjectStore(tmp_path).get(project.id).model_dump(mode="json")

    response = render_again(client, project.id, "shot_new")

    assert response.status_code == 200, response.text
    assert ProjectStore(tmp_path).get(project.id).model_dump(mode="json") == before


def test_a_second_take_moves_only_the_pointer_and_the_first_take_stays_named(tmp_path: Path):
    """What "the previous take's record is not silently lost" honestly means here.

    Take management is out of scope, so this is not a promise that the application keeps takes.
    It is the two things that are true instead, and they are worth pinning because the docs
    say them: ComfyUI numbers its outputs from the filename prefix, so the second render writes
    `_00002` beside `_00001` rather than over it, and `RenderJob.output_files` is written per
    submission and never rewritten, so the job that produced the first take goes on naming it.
    What moves is the single `Shot.latest_output` pointer.

    The numbering itself is ComfyUI's behaviour and is verified on the installation rather than
    here — see `land_take` — so what this drives is the half that is this application's: two job
    records, two names, one pointer.
    """
    client, store, comfy = make_client(tmp_path)
    project_id, shot_id, first_job = rendered_shot(client, store, comfy, "Two takes")

    assert render_again(client, project_id, shot_id).status_code == 200
    second = submit_h3(client, project_id, shot_id)
    assert second.status_code == 202
    second_job = second.json()["id"]
    land_take(client, comfy, project_id, second_job, "shot_take-h3_00002.mp4")

    saved = ProjectStore(tmp_path).get(project_id)
    assert first_job != second_job
    jobs = {job.id: job for job in saved.jobs}
    # The first take is still named, by the job that produced it, under its own number.
    assert jobs[first_job].output_files == [
        f"music-video-producer/{project_id}/shots/shot_take-h3_00001.mp4"
    ]
    assert jobs[second_job].output_files == [
        f"music-video-producer/{project_id}/shots/shot_take-h3_00002.mp4"
    ]
    # And the pointer -- the only thing that moved -- names the new one.
    assert saved.shots[0].latest_output.endswith("shot_take-h3_00002.mp4")
    assert saved.shots[0].status == "complete"


def test_a_new_take_drops_the_vision_review_of_the_take_it_displaces(tmp_path: Path):
    """A review is an inspection of one file, so it must not outlive that file being the latest.

    Carrying it across would leave "Inspect latest take" reporting on the previous render under
    the new take's name — a stale answer that reads as a fresh one, which is worse than no answer.
    It becomes reachable with this story: before it, re-rendering a shot needed an API client.

    The second half is the guard against overcorrecting. A job refresh that reports the same
    output must not clear a review of that same file.
    """
    client, store, comfy = make_client(tmp_path)
    project_id, shot_id, _ = rendered_shot(client, store, comfy, "Stale review")
    stored = store.get(project_id)
    stored.shots[0].latest_review = VisionInspectionRecord(summary="Take one: silver jacket.")
    store.save(stored)

    assert render_again(client, project_id, shot_id).status_code == 200
    second = submit_h3(client, project_id, shot_id)
    land_take(client, comfy, project_id, second.json()["id"], "shot_take-h3_00002.mp4")

    displaced = ProjectStore(tmp_path).get(project_id).shots[0]
    assert displaced.latest_output.endswith("shot_take-h3_00002.mp4")
    assert displaced.latest_review is None

    # A completion that lands the same file again leaves the review it belongs to alone.
    stored = store.get(project_id)
    stored.shots[0].latest_review = VisionInspectionRecord(summary="Take two: silver jacket.")
    store.save(stored)
    assert render_again(client, project_id, shot_id).status_code == 200
    third = submit_h3(client, project_id, shot_id)
    land_take(client, comfy, project_id, third.json()["id"], "shot_take-h3_00002.mp4")

    kept = ProjectStore(tmp_path).get(project_id).shots[0]
    assert kept.latest_review is not None
    assert kept.latest_review.summary == "Take two: silver jacket."


def test_render_again_404s_for_a_shot_and_a_project_that_do_not_exist(tmp_path: Path):
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Missing"))

    assert render_again(client, project.id, "shot_nope").status_code == 404
    assert render_again(client, "no-such-project", "shot_nope").status_code == 404


def test_render_again_statuses_are_exactly_the_settled_ones(tmp_path: Path):
    """The list the route and the control both key on, pinned against the status vocabulary.

    A status added to `ShotStatus` later is either settled or it is not, and this is what makes
    that a decision somebody has to make rather than one that gets made by omission.
    """
    settled = set(RENDER_AGAIN_STATUSES)
    assert settled == {"complete", "error", "approved"}
    assert settled <= set(get_args(ShotStatus))
    # The in-flight statuses and the settled ones must not overlap, or a Shot could be both.
    assert settled.isdisjoint({"queued", "running"})


def shot_status_writes(payload) -> list[str]:
    """Every Shot `status` value carried by a request body, however deeply it is nested.

    A Shot is recognised by carrying a `prompt` alongside its `status`, which is what tells a Shot
    dict apart from the `RenderJob` and `Song` dicts that also carry a `status` and travel in the
    same whole-project body. The recursion is the point: `PUT /projects/{id}` binds a whole
    `Project`, so a status write can arrive several levels down from the key anyone would grep for.
    """
    found: list[str] = []
    if isinstance(payload, dict):
        value = payload.get("status")
        if isinstance(value, str) and "prompt" in payload:
            found.append(value)
        for item in payload.values():
            found.extend(shot_status_writes(item))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(shot_status_writes(item))
    return found


class WithoutStatusWrites:
    """A client that fails the instant a request body writes a Shot status other than `draft`.

    The journey below is only evidence if it really is the journey. A single `"status": "ready"`
    slipped into a shots save would make every assertion in it pass while proving nothing — and
    that shortcut is not hypothetical, it is how every live render in this project has actually
    been driven. So the constraint is enforced mechanically rather than promised in a docstring.

    `"draft"` is allowed through because `app.js` stamps it onto every Shot it creates and sends it
    back on every whole-list save; forbidding it would forbid the interface's own behaviour. What
    is forbidden is a body that *arms* a Shot, which is the thing the application had no way to do.
    """

    def __init__(self, client):
        self._client = client
        self.statuses: list[str] = []

    def _guard(self, kwargs: dict) -> None:
        for written in shot_status_writes(kwargs.get("json")):
            self.statuses.append(written)
            assert written == "draft", (
                f"this journey wrote status={written!r} directly, which is the API-client "
                "shortcut it exists to prove is no longer needed"
            )

    def post(self, url, **kwargs):
        self._guard(kwargs)
        return self._client.post(url, **kwargs)

    def put(self, url, **kwargs):
        self._guard(kwargs)
        return self._client.put(url, **kwargs)

    def get(self, url, **kwargs):
        return self._client.get(url, **kwargs)


def interface_shot(shot_id: str, prompt: str) -> dict:
    """One Shot exactly as `app.js` puts it on the wire, field for field.

    Copied from the `#add-shot` handler rather than built from the `Shot` model, because the point
    is what the *interface* sends: it stamps `status: "draft"` and the `"New shot"` placeholder,
    and it re-sends every field it holds on every whole-list save.
    """
    return {
        "id": shot_id,
        "start": 0,
        "duration": 5,
        "prompt": prompt,
        "mode": "text",
        "asset_ids": [],
        "reference_labels": {},
        "use_song_audio": False,
        "seed": 0,
        "status": "draft",
        "prompt_id": "",
        "latest_output": "",
        "approved_output": "",
        "locked": False,
    }


def test_a_shot_reaches_its_first_render_through_the_interface_alone(tmp_path: Path):
    """The story's centrepiece: creation to a render, with nothing writing `status` by hand.

    This is the test whose absence hid the gap. Every guard around rendering was tested on its own
    and every one of them passed — the readiness gate, the status gate, the batch pre-flight, the
    queue button's filter — while the path *through* them was never walked end to end. A journey
    with no way to start it looks fully covered when you only ever test the doors.

    `WithoutStatusWrites` is what makes it a journey rather than a demonstration. Before this
    story the only assignment of `"ready"` anywhere on the server was `render_again`, which
    requires a shot to have already rendered, so every step below that is not a hand-written
    status had to exist for this to pass at all.

    The two refusals in the middle are deliberate and are not padding: a shot with a placeholder,
    and then a shot with a real prompt, are both refused *before* the mark. Writing a prompt is not
    a decision to render, and the story's "no auto-marking" rule is exactly that assertion.
    """
    client, store, comfy = make_client(tmp_path)
    guarded = WithoutStatusWrites(client)

    # A fresh project, as the New project dialog creates one.
    created = guarded.post("/api/projects", json={"name": "First render"})
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]

    def add_or_edit(prompt: str):
        """The whole-list shots save every timeline edit goes through."""
        return guarded.put(
            f"/api/projects/{project_id}/shots",
            json={"shots": [interface_shot("shot_first", prompt)]},
        )

    # Add a shot: the placeholder prompt and the draft status the interface stamps.
    assert add_or_edit(PLACEHOLDER_PROMPT).status_code == 200
    assert store.get(project_id).shots[0].status == "draft"
    # Nothing the interface has done so far can submit it, and the refusal is about the prompt.
    placeholder = submit_h3(guarded, project_id, "shot_first")
    assert placeholder.status_code == 422
    assert placeholder.json()["detail"] == readiness_refusal(["SHOT 01 (shot_first)"])

    # Write a prompt in the shot inspector.
    assert add_or_edit("A singer turns toward camera under sodium light").status_code == 200
    # Still not submittable, and now for the status rather than the prompt: writing a prompt is
    # not a decision to spend a GPU pass, and nothing may arm the shot on the Director's behalf.
    assert store.get(project_id).shots[0].status == "draft"
    prompted = submit_h3(guarded, project_id, "shot_first")
    assert prompted.status_code == 422
    assert "ready" in prompted.json()["detail"]
    assert not comfy.prompts

    # Mark it ready. This is the step the application did not have.
    marked = guarded.post(f"/api/projects/{project_id}/shots/shot_first/mark-ready")
    assert marked.status_code == 200, marked.text
    assert store.get(project_id).shots[0].status == "ready"
    # The queue button's own filter, applied to the stored plan. `renderJobs` decides what the
    # batch may submit with exactly this expression, and until now nothing could put a shot in it.
    assert [
        shot.id for shot in store.get(project_id).shots if shot.status == "ready"
    ] == ["shot_first"]
    # The batch pre-flight the button runs before it submits agrees.
    report = guarded.get(f"/api/projects/{project_id}/readiness").json()
    assert report["ready"] is True
    assert report["blocking"] == []

    # And the render reaches ComfyUI.
    queued = submit_h3(guarded, project_id, "shot_first")
    assert queued.status_code == 202, queued.text
    assert len(comfy.prompts) == 1
    assert store.get(project_id).shots[0].status == "queued"

    # Nothing on the way here wrote a status the interface does not write.
    assert set(guarded.statuses) == {"draft"}


def mark_ready(client, project_id: str, shot_id: str, **kwargs):
    """Commit one Shot to the queue. No body, by design — see `_set_shot_commitment` in app.py."""
    return client.post(f"/api/projects/{project_id}/shots/{shot_id}/mark-ready", **kwargs)


def mark_draft(client, project_id: str, shot_id: str, **kwargs):
    """Take one Shot back out of the queue. No body, for the same reason."""
    return client.post(f"/api/projects/{project_id}/shots/{shot_id}/mark-draft", **kwargs)


def drafted_shot(store: ProjectStore, name: str, **fields) -> Project:
    """A project with one drafted, prompted Shot — the state the mark acts on."""
    project = store.create(Project(name=name))
    defaults = {
        "id": "shot_first",
        "start": 0,
        "duration": 5,
        "prompt": "A singer turns toward camera",
        "mode": "text",
        "status": "draft",
    }
    project.shots = [Shot(**{**defaults, **fields})]
    store.save(project)
    return store.get(project.id)


def test_marking_ready_arms_a_drafted_shot_and_submission_then_accepts_it(tmp_path: Path):
    """Both halves, because either alone is a half-built feature.

    The status has to become `ready` — that is what the queue button filters on and what
    `generate_h3` requires — and a submission through the ordinary route then has to be accepted,
    which is what makes "the interface alone can start a render" true rather than plausible.
    """
    client, store, comfy = make_client(tmp_path)
    project = drafted_shot(store, "Armed")

    response = mark_ready(client, project.id, "shot_first")

    assert response.status_code == 200, response.text
    assert ProjectStore(tmp_path).get(project.id).shots[0].status == "ready"
    # Marking is not rendering: nothing was sent and no GPU time was spent by the click itself.
    assert not comfy.prompts

    assert submit_h3(client, project.id, "shot_first").status_code == 202
    assert len(comfy.prompts) == 1


def test_marking_writes_the_status_and_nothing_else(tmp_path: Path):
    """The reason this is its own action rather than the shots write it replaces.

    `PUT /shots` is the generic full-project-shaped route — it takes the whole Shot list from the
    client — so using it to arm one Shot also reasserts every prompt, window, reference and lock
    the client happens to be holding, from however long ago it loaded them. These routes bind no
    body at all, so there is nothing on the wire that could do that.

    Asserted as a whole-manifest comparison rather than field by field: a field-by-field check
    only catches the fields somebody thought of, and the hazard here is precisely the field nobody
    thought of. A body is posted too, and must be ignored — a handler that grew one later fails
    this rather than quietly gaining the shots write's blast radius.
    """
    client, store, _ = make_client(tmp_path)
    project = drafted_shot(store, "Narrow write")
    stored = store.get(project.id)
    stored.shots.append(
        Shot(id="shot_other", start=10, duration=5, prompt="A second shot", locked=True)
    )
    store.save(stored)
    before = ProjectStore(tmp_path).get(project.id).model_dump(mode="json")

    response = mark_ready(
        client,
        project.id,
        "shot_first",
        json={"shots": [{"id": "shot_other", "start": 0, "duration": 1, "prompt": "OVERWRITTEN"}]},
    )

    assert response.status_code == 200, response.text
    after = ProjectStore(tmp_path).get(project.id).model_dump(mode="json")
    # The one intended difference, isolated by neutralising it and comparing everything else.
    assert before["shots"][0]["status"] == "draft"
    assert after["shots"][0]["status"] == "ready"
    after["shots"][0]["status"] = before["shots"][0]["status"]
    after["updated_at"] = before["updated_at"]
    assert after == before


@pytest.mark.parametrize(
    "prompt",
    ["", "   \n\t", "New shot", "  new   SHOT  "],
    ids=["blank", "whitespace", "placeholder", "collapsed-placeholder"],
)
def test_marking_ready_refuses_a_shot_with_nothing_worth_rendering(tmp_path: Path, prompt: str):
    """The prompt gate, run at the mark and in `batch`'s own words.

    Not a second opinion about what counts as a prompt: this asks `prompt_rejection` through
    `prompt_is_missing`, which is the same function `readiness_report` asks per Shot, so the
    placeholder and the whitespace collapse come free rather than being re-decided here. Two
    implementations of "is this prompt worth a GPU pass" is how the mark starts arming shots the
    render then refuses.

    The refusal is `readiness_refusal`'s sentence, so the Director reads the same instruction here
    as at submission — and it names the prompt, because that is the thing they can act on.
    """
    client, store, _ = make_client(tmp_path)
    project = drafted_shot(store, "Nothing to render", prompt=prompt)

    response = mark_ready(client, project.id, "shot_first")

    assert response.status_code == 422
    assert response.json()["detail"] == readiness_refusal(["SHOT 01 (shot_first)"])
    assert "no prompt" in response.json()["detail"]
    # And it was genuinely not armed, rather than refused after the write.
    assert ProjectStore(tmp_path).get(project.id).shots[0].status == "draft"


def test_marking_back_to_draft_un_commits_a_shot_the_queue_would_have_taken(tmp_path: Path):
    """How a Director changes their mind, and it must cost nothing.

    A commitment nobody can walk back is a commitment people avoid making, so this is asserted to
    leave the Shot otherwise untouched — the prompt they wrote is still there — and to actually
    remove it from what the queue button submits.
    """
    client, store, _ = make_client(tmp_path)
    project = drafted_shot(store, "Un-commit", status="ready")

    response = mark_draft(client, project.id, "shot_first")

    assert response.status_code == 200, response.text
    reverted = ProjectStore(tmp_path).get(project.id).shots[0]
    assert reverted.status == "draft"
    assert reverted.prompt == "A singer turns toward camera"
    # The queue button's filter no longer sees it, and the submission route refuses it again.
    assert not [
        shot for shot in ProjectStore(tmp_path).get(project.id).shots if shot.status == "ready"
    ]
    assert submit_h3(client, project.id, "shot_first").status_code == 422


def test_marking_back_to_draft_is_allowed_even_with_no_prompt_left(tmp_path: Path):
    """The prompt gate runs in one direction only, and this is why.

    `draft` is the un-armed state. Refusing to disarm a Shot whose prompt was emptied would trap
    it armed on the strength of the very problem the Director is presumably about to fix — exactly
    backwards, and the one way this gate could make a plan less safe than no gate at all.
    """
    client, store, _ = make_client(tmp_path)
    project = drafted_shot(store, "Emptied and armed", status="ready", prompt="")

    assert mark_draft(client, project.id, "shot_first").status_code == 200
    assert ProjectStore(tmp_path).get(project.id).shots[0].status == "draft"


@pytest.mark.parametrize("state", ["draft", "ready"])
def test_marking_a_shot_into_the_state_it_is_already_in_changes_nothing(tmp_path: Path, state: str):
    """A no-op, not an error — and not a write either.

    The manifest is compared whole, because "nothing happened" has to include `updated_at`: a save
    on a request that changed nothing would bump the timestamp `PUT /projects` compares against,
    so a Director who double-clicked would be told their project changed since it was loaded.
    """
    client, store, _ = make_client(tmp_path)
    project = drafted_shot(store, "Already there", status=state)
    before = ProjectStore(tmp_path).get(project.id).model_dump(mode="json")

    call = mark_ready if state == "ready" else mark_draft
    response = call(client, project.id, "shot_first")

    assert response.status_code == 200, response.text
    assert ProjectStore(tmp_path).get(project.id).model_dump(mode="json") == before


@pytest.mark.parametrize("target", ["ready", "draft"])
def test_marking_refuses_a_locked_shot_in_either_direction(tmp_path: Path, target: str):
    """Consistent with everything else that respects `Shot.locked`.

    A lock is a deliberate hands-off, and committing a shot to a queue that spends GPU minutes is
    exactly the class of change it exists to refuse — the same argument `expand_shot_prompts` makes
    when it leaves a locked Shot's prompt alone, and the same one `render_again` makes. Both
    directions, because a lock that stopped only the expensive one would still let an automated
    caller silently un-commit a plan the Director locked down.

    The remedy is stated for the reason it is there: a lock stops an action, it does not stop the
    human who set it from unsetting it.
    """
    client, store, _ = make_client(tmp_path)
    was = "draft" if target == "ready" else "ready"
    project = drafted_shot(store, "Locked", status=was, locked=True)

    call = mark_ready if target == "ready" else mark_draft
    response = call(client, project.id, "shot_first")

    assert response.status_code == 422
    assert response.json()["detail"] == MARK_READY_LOCKED_REFUSAL.format(
        shot="SHOT 01 (shot_first)"
    )
    assert "Unlock" in response.json()["detail"]
    assert ProjectStore(tmp_path).get(project.id).shots[0].status == was

    stored = store.get(project.id)
    stored.shots[0].locked = False
    store.save(stored)
    assert call(client, project.id, "shot_first").status_code == 200


@pytest.mark.parametrize("settled", ["complete", "error", "approved"])
@pytest.mark.parametrize("target", ["ready", "draft"])
def test_marking_refuses_a_shot_that_has_rendered_and_names_the_action_that_owns_it(
    tmp_path: Path, settled: str, target: str
):
    """The other side of the first render belongs to `render_again`, and the refusal says so.

    Two actions, one for each side of a Shot's first render, and neither may reach across: a shot
    that has produced a take carries a job record, a prompt id and an output pointer, and every
    argument `render_again` makes about approvals and previous takes applies to it. Walking its
    status here would be that route's guard order bypassed by a route that never learned it.

    The direction is asserted rather than only the code, because a refusal that says "no" and
    stops leaves a Director looking at a completed shot with no idea that the button one row down
    is the one they want.
    """
    client, store, _ = make_client(tmp_path)
    project = drafted_shot(store, "Settled", status=settled, latest_output="takes/one.mp4")

    call = mark_ready if target == "ready" else mark_draft
    response = call(client, project.id, "shot_first")

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail == MARK_READY_ALREADY_RENDERED_REFUSAL.format(shot="SHOT 01 (shot_first)")
    assert "Render again" in detail
    assert ProjectStore(tmp_path).get(project.id).shots[0].status == settled


def test_marking_refuses_a_shot_carrying_an_approved_take(tmp_path: Path):
    """`approved_output` is settable independently of `status`, so it needs its own refusal.

    An approval is an editorial decision about one specific take. A Shot carrying one is not a Shot
    waiting for its first render whatever its status field says, and without this the `approved`
    status could be walked to `draft` through the generic shots write and the approval then
    stepped over here — `render_again`'s approval argument bypassed by a route that never saw it.
    """
    client, store, _ = make_client(tmp_path)
    project = drafted_shot(store, "Approved", status="draft", approved_output="takes/one.mp4")

    response = mark_ready(client, project.id, "shot_first")

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail == MARK_READY_APPROVED_REFUSAL.format(shot="SHOT 01 (shot_first)")
    assert "Clear the approval" in detail
    assert ProjectStore(tmp_path).get(project.id).shots[0].status == "draft"


def test_marking_refuses_a_shot_with_a_render_still_in_flight(tmp_path: Path):
    """The second case is the one a status-only check misses, and it is the dangerous one.

    `Shot.status` is exactly what the generic shots write can rewrite by hand, so a Shot can read
    `draft` on screen while ComfyUI is still working on it. The job record is what still knows, and
    it is keyed by `target_id` — so arming that Shot and submitting it would put two renders of one
    shot in flight, racing on the same output prefix.

    Deliberately says nothing about *which* code comes back. That is
    `test_the_two_first_render_actions_answer_one_live_render_with_one_code`'s job, and separating
    them is the point: this test is the guarantee that a live render refuses at all and that the
    status stays where it was, and it has to keep failing if the check is dropped even by someone
    who leaves the number in the source untouched.
    """
    client, store, comfy = make_client(tmp_path)
    project = drafted_shot(store, "In flight", status="ready")
    submitted = submit_h3(client, project.id, "shot_first")
    assert submitted.status_code == 202

    # The ordinary case: the Shot itself says a render is out.
    queued = mark_draft(client, project.id, "shot_first")
    assert not queued.is_success, queued.text
    assert queued.json()["detail"] == MARK_READY_IN_FLIGHT_REFUSAL.format(
        shot="SHOT 01 (shot_first)"
    )
    # ...and the same Shot with its status walked back by hand while the job is still out.
    stored = store.get(project.id)
    stored.shots[0].status = "draft"
    store.save(stored)

    hidden = mark_ready(client, project.id, "shot_first")
    assert not hidden.is_success, "a hand-edited status hid a live render from the guard"
    assert hidden.json()["detail"] == MARK_READY_IN_FLIGHT_REFUSAL.format(
        shot="SHOT 01 (shot_first)"
    )
    assert ProjectStore(tmp_path).get(project.id).shots[0].status == "draft"
    assert len(comfy.prompts) == 1


def test_the_two_first_render_actions_answer_one_live_render_with_one_code(tmp_path: Path):
    """One live render, two routes that can be asked about it, and one status code between them.

    `mark_ready_refusal` answered 422 for a `queued`/`running` Shot until 2026-08-18 while
    `render_again` answered 409 for that same render, which meant a client asking "may I touch this
    shot" got a different class of answer depending on which side of the first render it asked
    from. The Director renegotiated it to 409: a live render is a state conflict — the identical
    request succeeds once the job lands — where every other refusal these routes give is a fact
    about the Shot that waiting does not change.

    Asserted as an equality between the two routes as well as against the literal, because the
    literal alone would let them drift apart again the next time one of them is edited. Both mark
    directions are asked: the refusal is reached before the direction is looked at, and a guard
    that answered one way for arming and another for disarming would be the same defect one level
    down.
    """
    client, store, _ = make_client(tmp_path)
    project = drafted_shot(store, "One live render", status="ready")
    assert submit_h3(client, project.id, "shot_first").status_code == 202

    codes = {
        "mark-ready": mark_ready(client, project.id, "shot_first").status_code,
        "mark-draft": mark_draft(client, project.id, "shot_first").status_code,
        "render-again": render_again(client, project.id, "shot_first").status_code,
    }

    assert codes == {"mark-ready": 409, "mark-draft": 409, "render-again": 409}, codes
    assert len(set(codes.values())) == 1, codes
    # And the alignment is only the in-flight row. Everything else the mark refuses is
    # unprocessable content rather than a conflict, and stays 422 — a lock, an approval, a settled
    # status and an unrenderable prompt are all true for as long as the Shot is in that state.
    locked = drafted_shot(store, "Locked", locked=True)
    approved = drafted_shot(store, "Approved", approved_output="takes/one.mp4")
    settled = drafted_shot(store, "Settled", status="complete", latest_output="takes/one.mp4")
    unprompted = drafted_shot(store, "Unprompted", prompt="")
    others = {
        "locked": mark_ready(client, locked.id, "shot_first").status_code,
        "approved": mark_ready(client, approved.id, "shot_first").status_code,
        "settled": mark_ready(client, settled.id, "shot_first").status_code,
        "prompt": mark_ready(client, unprompted.id, "shot_first").status_code,
    }
    assert others == {"locked": 422, "approved": 422, "settled": 422, "prompt": 422}, others


def test_marking_ready_is_not_a_certificate_the_render_gate_asks_again(tmp_path: Path):
    """The design note, executed: the check is asked at both points and remembered at neither.

    The tempting implementation reads `status == "ready"` as evidence that the prompt was fine —
    it was checked at the mark, after all. It is not evidence about anything later. A prompt can be
    deleted, or pasted over with the `"New shot"` placeholder a duplicate carries, between the mark
    and the submission, and the gate exists to stop a GPU pass returning noise rather than to count
    approvals.

    Both emptiness cases, because the collapse is what catches a placeholder that picked up stray
    spacing on the way through a duplicate.
    """
    client, store, comfy = make_client(tmp_path)
    project = drafted_shot(store, "Emptied after marking")
    assert mark_ready(client, project.id, "shot_first").status_code == 200

    for emptied in ("", "  New   Shot  "):
        stored = store.get(project.id)
        stored.shots[0].prompt = emptied
        store.save(stored)

        response = submit_h3(client, project.id, "shot_first")

        assert response.status_code == 422, emptied
        assert response.json()["detail"] == readiness_refusal(["SHOT 01 (shot_first)"])
        # Still `ready` — the mark stands, and the render refused it on the prompt it has now.
        assert ProjectStore(tmp_path).get(project.id).shots[0].status == "ready"
    assert not comfy.prompts


def test_nothing_marks_a_shot_ready_as_a_side_effect(tmp_path: Path):
    """Auto-marking is forbidden, not merely unimplemented.

    It would be easy — and it would look helpful — to have expansion arm every shot it prompted,
    or the Director's shot plan arm every shot it created. That would mean a Director who ran
    expansion to see what the model suggested had silently armed a whole plan for rendering, and
    the queue button is one confirm dialog away from spending real GPU minutes on it.

    So every write in the application that touches Shots is driven here and the statuses are read
    afterwards. The list is the point: the routes are enumerated rather than sampled, because the
    hazard is a *future* write path that arms shots as a convenience, and a test that watched one
    route would not see it.
    """
    director = ExpandingDirector()
    client, store, comfy = make_client(tmp_path, director)
    project = planned_project(store, "No side effects")

    def statuses() -> list[str]:
        return [shot.status for shot in ProjectStore(tmp_path).get(project.id).shots]

    # The Director applying a shot plan, which creates and rewrites Shots wholesale.
    applied = client.post(
        f"/api/projects/{project.id}/director/chat",
        json={"message": "Plan the video", "apply_shots": True},
    )
    assert applied.status_code == 200, applied.text
    assert set(statuses()) == {"draft"}, "applying a shot plan armed a shot"

    # Expansion writing a real prompt onto every Shot — the likeliest place this would creep in,
    # because after it every Shot genuinely would pass the prompt gate.
    expanded = client.post(f"/api/projects/{project.id}/director/expand")
    assert expanded.status_code == 200, expanded.text
    assert all(shot.prompt for shot in ProjectStore(tmp_path).get(project.id).shots)
    assert set(statuses()) == {"draft"}, "expansion armed the shots it prompted"

    # The whole-list shots save every timeline edit goes through.
    saved = ProjectStore(tmp_path).get(project.id)
    assert client.put(
        f"/api/projects/{project.id}/shots",
        json={"shots": saved.model_dump(mode="json")["shots"]},
    ).status_code == 200
    assert set(statuses()) == {"draft"}

    # And the generic full-project write, which defaults every field it is not sent.
    body = ProjectStore(tmp_path).get(project.id).model_dump(mode="json")
    assert client.put(f"/api/projects/{project.id}", json=body).status_code == 200
    assert set(statuses()) == {"draft"}

    # Nothing above reached ComfyUI either, which is the cost this rule is protecting.
    assert not comfy.prompts


def test_marking_404s_for_a_shot_and_a_project_that_do_not_exist(tmp_path: Path):
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Missing"))

    for call in (mark_ready, mark_draft):
        assert call(client, project.id, "shot_nope").status_code == 404
        assert call(client, "no-such-project", "shot_nope").status_code == 404


def test_every_shot_status_belongs_to_exactly_one_of_the_two_actions(tmp_path: Path):
    """The two actions partition the status vocabulary, and this is what keeps them partitioning it.

    A status added to `ShotStatus` later is on one side of a Shot's first render or the other. If
    it falls through both lists it becomes a status no action can move a Shot out of — reachable
    only by an API client, which is the exact hole this story closed. Making that a decision
    somebody has to make is the whole value of asserting it.
    """
    unrendered = set(MARK_READY_STATUSES)
    settled = set(RENDER_AGAIN_STATUSES)
    in_flight = {"queued", "running"}

    assert unrendered == {"draft", "ready"}
    assert unrendered.isdisjoint(settled)
    assert unrendered.isdisjoint(in_flight)
    assert unrendered | settled | in_flight == set(get_args(ShotStatus))


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


def test_promotion_records_its_parent_and_leaves_the_source_asset_untouched(tmp_path: Path):
    """FR-18: the new Asset points back at its source, and the source is *unchanged*.

    Promotion is additive by design — a new Asset with `parent_id` set — so the source keeps
    its own name, path, prompt and id. Asserting only `parent_id` would pass just as happily
    for a route that moved the source's path onto the child or rewrote its name, which is why
    this reads the persisted record before and after and compares the serialized bytes.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Promotion"))
    uploaded = client.post(
        f"/api/projects/{project.id}/assets/upload",
        data={"name": "Mara", "kind": "character"},
        files={"file": ("mara.png", b"png-data", "image/png")},
    ).json()
    asset_id = uploaded["assets"][0]["id"]
    manifest = store.manifest_path(project.id)
    before = json.dumps(json.loads(manifest.read_text(encoding="utf-8"))["assets"][0])
    source_file = store.project_dir(project.id) / uploaded["assets"][0]["path"]
    source_bytes = source_file.read_bytes()

    response = client.post(
        f"/api/projects/{project.id}/assets/{asset_id}/multiview",
        json={"prompt": "Preserve Mara in face, front, side and back views", "seed": 77},
    )

    assert response.status_code == 202
    persisted = json.loads(manifest.read_text(encoding="utf-8"))["assets"]
    assert len(persisted) == 2
    assert json.dumps(persisted[0]) == before
    assert source_file.read_bytes() == source_bytes
    child = persisted[1]
    assert child["parent_id"] == asset_id
    assert child["source"] == "krea-multiview"
    assert child["id"] != asset_id
    # The source is what was uploaded to ComfyUI, and it was read rather than moved.
    assert comfy.uploads == [source_bytes]


class PlanningDirector:
    """A director double whose plan is chosen by the test, with the request recorded."""

    def __init__(self, shots=None, message="Laid out.", sections=None):
        self.shots = shots or []
        self.sections = sections or []
        self.message = message
        self.requests = []

    async def plan(self, *, message, project_context):
        self.requests.append({"message": message, "context": project_context})
        shot = lambda start, duration, prompt: type(
            "PlannedShot", (), {"start": start, "duration": duration, "prompt": prompt}
        )()
        return type(
            "DirectorResult",
            (),
            {
                "message": self.message,
                "treatment": "",
                "style_bible": "",
                "shots": [shot(*entry) for entry in self.shots],
                "sections": [
                    type("PlannedSection", (), {
                        "label": label, "start": start, "duration": duration, "prompt": prompt,
                    })()
                    for label, start, duration, prompt in self.sections
                ],
            },
        )()


def populate(client, project_id: str, confirm: bool = True):
    return client.post(
        f"/api/projects/{project_id}/timeline/populate",
        json={"confirm_replace": confirm},
    )


def test_populate_timeline_lays_out_the_whole_song_from_the_models_shape(tmp_path: Path):
    """Stage 4 end to end: the model's sloppy layout becomes a contiguous, H3-range plan
    covering exactly the song, prompts drawn from the proposal whose span each window
    falls in, old drafts replaced, and the instruction carrying the song length and the
    asset roster by name."""
    director = PlanningDirector(shots=[
        (0, 3, "Open wide on the empty warehouse."),
        (10, 30, "She sings at the standing microphone."),
        (45, 10, "Glamour angles on the canopy bed."),
    ])
    client, store, comfy = make_client(tmp_path, director=director)
    project = store.create(Project(name="Populate"))
    project.song = Song(title="Harder", source="imported", path="media/h.mp3", duration=60.0)
    project.assets = [Asset(name="HarderFaster", kind="character", path="media/a.png")]
    project.shots = [Shot(id="shot_old", start=0, duration=5, prompt="Old draft")]
    store.save(project)

    unconfirmed = populate(client, project.id, confirm=False)
    assert unconfirmed.status_code == 422
    assert "every existing shot is replaced" in unconfirmed.json()["detail"]
    assert store.get(project.id).shots[0].id == "shot_old"

    response = populate(client, project.id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["proposed"] == 3
    assert body["created"] >= 10  # 60 s at <=6 s per window (the enforced speed ceiling)
    saved = store.get(project.id)
    assert all(shot.id != "shot_old" for shot in saved.shots)
    cursor = 0.0
    for shot in sorted(saved.shots, key=lambda item: item.start):
        assert shot.start == pytest.approx(cursor, abs=1e-6)
        # POPULATE_MAX_WINDOW_SECONDS: the enforced speed ceiling, tighter than H3's
        # 15 s legality — 9 s windows are the measured 2.2-hour cliff.
        assert 4.0 - 1e-9 <= shot.duration <= 6.0 + 1e-9
        assert shot.status == "draft"
        assert shot.prompt
        assert shot.seed > 0  # distinct seeds; a shared seed correlates NaN failures
        cursor = shot.start + shot.duration
    assert cursor == pytest.approx(60.0, abs=1e-6)
    assert len({shot.seed for shot in saved.shots}) == len(saved.shots)
    # The last window sits in the last proposal's proportional span.
    last = max(saved.shots, key=lambda item: item.start)
    assert last.prompt == "Glamour angles on the canopy bed."
    # The instruction told the model what only the server knows.
    sent = director.requests[0]["message"]
    assert "60.0 seconds" in sent
    assert "HarderFaster (character)" in sent
    assert comfy.prompts == []  # populate renders nothing


def test_populate_timeline_refuses_what_it_must_not_replace(tmp_path: Path):
    director = PlanningDirector(shots=[(0, 10, "A shot.")])
    client, store, _comfy = make_client(tmp_path, director=director)

    project = store.create(Project(name="No song"))
    assert populate(client, project.id).status_code == 422

    protected = store.create(Project(name="Protected"))
    protected.song = Song(title="S", source="imported", path="m.mp3", duration=30.0)
    protected.shots = [
        Shot(id="shot_lock", start=0, duration=5, prompt="p", locked=True),
        Shot(id="shot_appr", start=5, duration=5, prompt="p", status="approved",
             approved_output="takes/a.mp4", approved_start=5, approved_duration=5),
    ]
    store.save(protected)
    refusal = populate(client, protected.id)
    assert refusal.status_code == 422
    assert "shot_lock" in refusal.json()["detail"]
    assert "shot_appr" in refusal.json()["detail"]
    assert director.requests == []  # refused before the model was ever asked

    busy = store.create(Project(name="Busy"))
    busy.song = Song(title="S", source="imported", path="m.mp3", duration=30.0)
    busy.jobs = [RenderJob(kind="h3", status="running", prompt_id="p-1")]
    store.save(busy)
    assert populate(client, busy.id).status_code == 409

    empty_plan = store.create(Project(name="Empty plan"))
    empty_plan.song = Song(title="S", source="imported", path="m.mp3", duration=30.0)
    store.save(empty_plan)
    hollow = PlanningDirector(shots=[], message="I could not decide.")
    hollow_client, hollow_store, _ = make_client(tmp_path / "hollow", director=hollow)
    hollow_project = hollow_store.create(Project(name="Hollow"))
    hollow_project.song = Song(title="S", source="imported", path="m.mp3", duration=30.0)
    hollow_store.save(hollow_project)
    no_shots = populate(hollow_client, hollow_project.id)
    assert no_shots.status_code == 502
    assert "I could not decide." in no_shots.json()["detail"]
    assert hollow_store.get(hollow_project.id).shots == []


def batch_plan_project(store, shots: list[Shot], name: str = "Batch") -> str:
    project = store.create(Project(name=name))
    project.shots = shots
    store.save(project)
    return project.id


def generate_batch(client, project_id: str, **body):
    return client.post(f"/api/projects/{project_id}/generate/batch", json=body)


def test_generate_batch_submits_every_ready_shot_after_one_confirmation(tmp_path: Path):
    """FR-4's happy path: one server-enforced confirmation naming the count, then every
    ready shot in timeline order — each its own job, all sharing one freshly-minted
    batch_id, the draft untouched because arming is not this route's act."""
    client, store, comfy = make_client(tmp_path)
    project_id = batch_plan_project(store, [
        Shot(id="shot_late", start=8, duration=4, prompt="A crane shot", status="ready"),
        Shot(id="shot_early", start=0, duration=4, prompt="A wide open", status="ready"),
        Shot(id="shot_draft", start=4, duration=4, prompt="Unarmed", status="draft"),
    ])

    unconfirmed = generate_batch(client, project_id)
    assert unconfirmed.status_code == 422
    assert "2 H3 render(s)" in unconfirmed.json()["detail"]
    assert comfy.prompts == []

    response = generate_batch(client, project_id, confirm_gpu=True)
    assert response.status_code == 202, response.text
    body = response.json()
    assert [entry["shot_id"] for entry in body["submitted"]] == ["shot_early", "shot_late"]
    assert body["skipped"] == []
    assert body["batch_id"].startswith("batch_")
    assert len(comfy.prompts) == 2

    saved = ProjectStore(tmp_path).get(project_id)
    jobs = {job.target_id: job for job in saved.jobs}
    assert set(jobs) == {"shot_early", "shot_late"}
    assert all(job.kind == "h3" for job in jobs.values())
    assert {job.batch_id for job in jobs.values()} == {body["batch_id"]}
    assert {shot.id: shot.status for shot in saved.shots}["shot_draft"] == "draft"
    assert {shot.id: shot.status for shot in saved.shots}["shot_early"] == "queued"


def test_generate_batch_skips_a_blocked_shot_and_submits_the_rest(tmp_path: Path):
    """FR-4's consequence, verbatim: 'A Shot that fails validation is reported and
    skipped without blocking the rest of the batch.' The skip carries the single-shot
    route's own sentence — no second wording exists to drift."""
    client, store, comfy = make_client(tmp_path)
    project_id = batch_plan_project(store, [
        Shot(id="shot_good", start=0, duration=4, prompt="A real prompt", status="ready"),
        # Hand-walked to ready with no prompt — unreachable through the UI, exactly the
        # state the per-shot gate exists to catch at submission.
        Shot(id="shot_blank", start=4, duration=4, prompt="", status="ready"),
    ])

    response = generate_batch(client, project_id, confirm_gpu=True)

    assert response.status_code == 202
    body = response.json()
    assert [entry["shot_id"] for entry in body["submitted"]] == ["shot_good"]
    assert len(body["skipped"]) == 1
    assert body["skipped"][0]["shot_id"] == "shot_blank"
    assert "no prompt" in body["skipped"][0]["reason"]
    assert len(comfy.prompts) == 1


def test_generate_batch_replace_existing_reopens_settled_and_names_the_protected(
    tmp_path: Path,
):
    client, store, _comfy = make_client(tmp_path)
    project_id = batch_plan_project(store, [
        Shot(id="shot_ready", start=0, duration=4, prompt="p", status="ready"),
        Shot(id="shot_done", start=4, duration=4, prompt="p2", status="complete",
             latest_output="takes/a.mp4"),
        Shot(id="shot_err", start=8, duration=4, prompt="p3", status="error"),
        Shot(id="shot_appr", start=12, duration=4, prompt="p4", status="approved",
             latest_output="takes/b.mp4", approved_output="takes/b.mp4",
             approved_start=12, approved_duration=4),
        Shot(id="shot_lock", start=16, duration=4, prompt="p5", status="complete",
             latest_output="takes/c.mp4", locked=True),
    ])

    # Without the tick, settled shots are not the batch's business.
    only_ready = generate_batch(client, project_id, confirm_gpu=True)
    assert [e["shot_id"] for e in only_ready.json()["submitted"]] == ["shot_ready"]

    replaced = generate_batch(
        client, project_id, confirm_gpu=True, replace_existing=True
    )
    assert replaced.status_code == 202, replaced.text
    body = replaced.json()
    # shot_ready is queued from the first call — already rendering, so it is deliberately
    # absent from both lists (the queue panel is its surface) — while the two settled
    # unprotected shots re-open and go, and the two protections are named.
    assert [e["shot_id"] for e in body["submitted"]] == ["shot_done", "shot_err"]
    skipped = {e["shot_id"]: e["reason"] for e in body["skipped"]}
    assert set(skipped) == {"shot_appr", "shot_lock"}
    assert "approved take" in skipped["shot_appr"]
    assert "locked" in skipped["shot_lock"]
    saved = ProjectStore(tmp_path).get(project_id)
    statuses = {shot.id: shot.status for shot in saved.shots}
    assert statuses["shot_done"] == "queued" and statuses["shot_err"] == "queued"
    assert statuses["shot_appr"] == "approved" and statuses["shot_lock"] == "complete"


def test_generate_batch_flagged_scope_resubmits_and_clears_the_flag_only_on_success(
    tmp_path: Path,
):
    """AD-5's words: the flag is cleared by successful resubmission of that shot or by
    hand, never by the batch draining — so a flagged shot whose resubmission refuses
    keeps its flag, and unflagged shots are not the flagged scope's business."""
    client, store, comfy = make_client(tmp_path)
    project_id = batch_plan_project(store, [
        Shot(id="shot_flag", start=0, duration=4, prompt="p", status="complete",
             latest_output="takes/a.mp4", flagged=True),
        Shot(id="shot_flag_blank", start=4, duration=4, prompt="", status="complete",
             latest_output="takes/b.mp4", flagged=True),
        Shot(id="shot_plain", start=8, duration=4, prompt="p3", status="ready"),
    ])

    empty = generate_batch(client, project_id, confirm_gpu=True, scope="flagged")
    assert empty.status_code == 202
    body = empty.json()
    assert [e["shot_id"] for e in body["submitted"]] == ["shot_flag"]
    assert body["skipped"][0]["shot_id"] == "shot_flag_blank"
    assert len(comfy.prompts) == 1  # the ready-but-unflagged shot is untouched

    saved = ProjectStore(tmp_path).get(project_id)
    flags = {shot.id: shot.flagged for shot in saved.shots}
    assert flags["shot_flag"] is False   # cleared by success
    assert flags["shot_flag_blank"] is True  # kept by refusal
    assert saved.jobs[-1].batch_id.startswith("batch_")

    none_left = generate_batch(client, project_id, confirm_gpu=True, scope="flagged")
    # The refused shot is still flagged, so the scope is not empty — it reports the same
    # skip again rather than lying that nothing is flagged.
    assert none_left.status_code == 202
    assert none_left.json()["submitted"] == []


class StageManagingDirector:
    """A director double whose Stage Manager proposals are chosen by the test."""

    def __init__(self, assets=None, message="Assessed."):
        self.assets = assets or []
        self.message = message
        self.requests = []

    async def stage_manager(self, *, project_context, count):
        self.requests.append({"context": project_context, "count": count})
        proposal = lambda kind, name, prompt: type(
            "AssetProposal", (), {"kind": kind, "name": name, "prompt": prompt}
        )()
        return type(
            "StageManagerResult",
            (),
            {"message": self.message, "assets": [proposal(*entry) for entry in self.assets]},
        )()


def fill_assets(client, project_id: str, count: int = 8, confirm: bool = True):
    return client.post(
        f"/api/projects/{project_id}/assets/fill",
        json={"count": count, "confirm_gpu": confirm},
    )


def test_asset_fill_queues_one_flux_render_per_stage_manager_proposal(tmp_path: Path):
    """Stage 3's last ask end to end: each proposal becomes an ordinary generated asset —
    the exact shape generate_flux creates, so a landed one is keep/delete/AI Mod like any
    other — with one flux job each, distinct seeds, and the model's message relayed."""
    director = StageManagingDirector(assets=[
        ("character", "HarderFaster · red boots full body",
         "A tall female rock singer, red leather boots, full body, dark warehouse."),
        ("setting", "Warehouse corner mezzanine",
         "A dark warehouse mezzanine corner, moonlight shafts, 35mm grain."),
        ("prop", "Standing microphone",
         "A vintage chrome standing microphone on a dark stage, amber rim light."),
    ])
    client, store, comfy = make_client(tmp_path, director=director)
    project = store.create(Project(name="Fill"))
    store.save(project)

    unconfirmed = fill_assets(client, project.id, count=5, confirm=False)
    assert unconfirmed.status_code == 422
    assert "5 Flux image render(s)" in unconfirmed.json()["detail"]
    assert director.requests == []

    response = fill_assets(client, project.id, count=5)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["message"] == "Assessed."
    assert [entry["kind"] for entry in body["submitted"]] == ["character", "setting", "prop"]
    assert len(comfy.prompts) == 3
    assert director.requests[0]["count"] == 5

    saved = store.get(project.id)
    assert [asset.source for asset in saved.assets] == ["stage-manager"] * 3
    assert all(asset.path == "" and asset.prompt_id for asset in saved.assets)
    assert [job.kind for job in saved.jobs] == ["flux"] * 3
    assert [job.seed for job in saved.jobs] == [0, 1, 2]

    # The eviction guard now correctly refuses a second fill over its own open flux jobs.
    assert fill_assets(client, project.id, count=2).status_code == 409

    # Truncation: the count is a hard cap however eager the model was.
    fresh = store.create(Project(name="Trim"))
    store.save(fresh)
    trimmed = fill_assets(client, fresh.id, count=2)
    assert trimmed.status_code == 202
    assert len(trimmed.json()["submitted"]) == 2


def test_asset_fill_refuses_while_renders_are_open_and_an_empty_assessment(tmp_path: Path):
    """The FR-9 eviction guard fires before the model is ever asked — Flux interleaved
    into an H3 batch costs ~150 s per eviction — and a proposal-less answer is a 502
    carrying the model's own message rather than an empty success."""
    director = StageManagingDirector(assets=[], message="The library is complete.")
    client, store, _comfy = make_client(tmp_path, director=director)
    busy = store.create(Project(name="Busy"))
    busy.jobs = [RenderJob(kind="h3", status="running", prompt_id="p-1")]
    store.save(busy)

    blocked = fill_assets(client, busy.id)
    assert blocked.status_code == 409
    assert "evict the resident video model" in blocked.json()["detail"]
    assert director.requests == []

    idle = store.create(Project(name="Idle"))
    store.save(idle)
    hollow = fill_assets(client, idle.id)
    assert hollow.status_code == 502
    assert "The library is complete." in hollow.json()["detail"]
    assert store.get(idle.id).assets == []


def test_ai_mod_creates_a_child_asset_and_the_completion_adopts_the_edit(tmp_path: Path):
    """The Director's stage-3 ask end to end: instruction in, new asset beside the source,
    the landed file adopted by the one completion writer, the source untouched.

    The submitted graph is checked for the adapter's distinctive facts rather than trusted:
    the image VAE conditioning seat, the wrapped prompt carrying the instruction, and the
    resolved source path inside `media_state` — the reference path's way, no upload.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="AI Mod"))
    uploaded = client.post(
        f"/api/projects/{project.id}/assets/upload",
        data={"name": "HarderFaster", "kind": "character"},
        files={"file": ("harderfaster.png", b"png-data", "image/png")},
    ).json()
    asset_id = uploaded["assets"][0]["id"]
    manifest = store.manifest_path(project.id)
    before = json.dumps(json.loads(manifest.read_text(encoding="utf-8"))["assets"][0])

    response = client.post(
        f"/api/projects/{project.id}/assets/{asset_id}/edit",
        json={"instruction": "Change her boots to bright red leather boots.", "profile": "turbo"},
    )

    assert response.status_code == 202, response.text
    job = response.json()
    assert job["kind"] == "edit"
    payload = comfy.prompts[-1]
    assert payload["mvp:condition"]["inputs"]["vae"] == ["mvp:image_vae", 0]
    assert payload["mvp:save"]["class_type"] == "SaveImage"
    prompt = payload["mvp:condition"]["inputs"]["prompt"]
    assert prompt.startswith("subject_definitions:")
    assert "Change her boots to bright red leather boots." in prompt
    assert "(HarderFaster)" in prompt
    media = json.loads(payload["mvp:references"]["inputs"]["media_state"])
    assert media[0]["file"].endswith("harderfaster.png")
    # Turbo is the turbo export's own bundle.
    assert payload["mvp:scheduler"]["inputs"]["steps"] == 8
    assert payload["mvp:lora"]["inputs"]["lora_name"].startswith("minimax_h3_turbo_v4_step600")

    persisted = json.loads(manifest.read_text(encoding="utf-8"))["assets"]
    assert len(persisted) == 2
    assert json.dumps(persisted[0]) == before
    child = persisted[1]
    assert child["parent_id"] == asset_id
    assert child["source"] == "h3-image-edit"
    assert child["kind"] == "character"
    assert child["path"] == ""

    # The completion lands the edited image onto the child — `apply_job_history`, the one
    # writer, through the ordinary per-job refresh.
    async def completed_history(prompt_id):
        return type(
            "History",
            (),
            {
                "prompt_id": prompt_id,
                "status": "complete",
                "outputs": [
                    {
                        "subfolder": f"music-video-producer/{project.id}/assets",
                        "filename": f"{child['id']}-edit_00001_.png",
                    }
                ],
                "error": "",
            },
        )()

    comfy.history = completed_history
    refreshed = client.get(f"/api/projects/{project.id}/jobs/{job['id']}")
    assert refreshed.status_code == 200
    landed = store.get(project.id).assets[-1]
    assert landed.path.endswith(f"{child['id']}-edit_00001_.png")
    # An edited child is an ordinary image asset: a second mod on it is accepted. Its
    # file lives under the ComfyUI output root, where the route resolves rendered assets.
    rendered = (
        tmp_path / "comfy" / "output" / "music-video-producer" / project.id / "assets"
        / f"{child['id']}-edit_00001_.png"
    )
    rendered.parent.mkdir(parents=True, exist_ok=True)
    rendered.write_bytes(b"edited-png")
    again = client.post(
        f"/api/projects/{project.id}/assets/{landed.id}/edit",
        json={"instruction": "Now make the jacket white."},
    )
    assert again.status_code == 202


def test_ai_mod_refuses_what_it_cannot_edit(tmp_path: Path):
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Refusals"))
    uploaded = client.post(
        f"/api/projects/{project.id}/assets/upload",
        data={"name": "Master track", "kind": "audio"},
        files={"file": ("song.mp3", b"mp3-data", "audio/mpeg")},
    ).json()
    audio_id = uploaded["assets"][0]["id"]

    wrong_kind = client.post(
        f"/api/projects/{project.id}/assets/{audio_id}/edit",
        json={"instruction": "Make it red."},
    )
    assert wrong_kind.status_code == 422
    assert "audio" in wrong_kind.json()["detail"]

    stored = store.get(project.id)
    stored.assets.append(Asset(id="asset_unrendered", name="Pending", kind="character", path=""))
    store.save(stored)
    unrendered = client.post(
        f"/api/projects/{project.id}/assets/asset_unrendered/edit",
        json={"instruction": "Make it red."},
    )
    assert unrendered.status_code == 422
    assert "no image yet" in unrendered.json()["detail"]

    blank = client.post(
        f"/api/projects/{project.id}/assets/asset_unrendered/edit",
        json={"instruction": "   "},
    )
    assert blank.status_code == 422

    missing = client.post(
        f"/api/projects/{project.id}/assets/asset_absent/edit",
        json={"instruction": "Make it red."},
    )
    assert missing.status_code == 404
    assert comfy.prompts == []

    # A structured instruction travels verbatim — never double-wrapped.
    imaged = client.post(
        f"/api/projects/{project.id}/assets/upload",
        data={"name": "Sheet", "kind": "character"},
        files={"file": ("sheet.png", b"png-data", "image/png")},
    ).json()["assets"][-1]
    structured = "subject_definitions:\n<Picture 1> is x.\n\ndetailed_description:\nY."
    verbatim = client.post(
        f"/api/projects/{project.id}/assets/{imaged['id']}/edit",
        json={"instruction": structured},
    )
    assert verbatim.status_code == 202
    assert comfy.prompts[-1]["mvp:condition"]["inputs"]["prompt"] == structured


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
        json={"prompt": "front, side and back views", "seed": 1},
    )

    assert response.status_code == 404
    assert comfy.uploads == []


@pytest.mark.parametrize("kind", ["prop", "setting"])
def test_an_object_is_promoted_without_being_labelled_a_character(tmp_path: Path, kind: str):
    """The refusal was ours, not Krea's.

    A probe promoted a Flux cargo ship through this exact route by uploading it as a
    character, and got back a clean, consistent sheet — front, three-quarter, side, rear,
    hull markings and proportions holding. The only thing it had to fake was the label. So
    the gate opens for the kinds a sheet can be *of*, and the ship stays a prop: the child
    the promotion creates carries the source's kind, and nothing in the manifest says a
    spaceship is a person.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Objects"))
    uploaded = client.post(
        f"/api/projects/{project.id}/assets/upload",
        data={"name": "Cargo hauler", "kind": kind},
        files={"file": ("ship.png", b"png-data", "image/png")},
    ).json()
    asset_id = uploaded["assets"][0]["id"]

    response = client.post(
        f"/api/projects/{project.id}/assets/{asset_id}/multiview",
        json={"prompt": "Preserve the exact design of this object", "seed": 5},
    )

    assert response.status_code == 202
    saved = store.get(project.id)
    child = saved.assets[-1]
    assert child.parent_id == asset_id
    assert child.kind == kind, "a promoted object must not be filed as a character"
    assert child.source == "krea-multiview"
    assert saved.jobs[-1].kind == "multiview"
    # The same graph as a character's: the subject picks the prompt and nothing else.
    assert comfy.prompts[-1]["127"]["inputs"]["lora_name"].endswith("QuadView_krea2_v1.safetensors")
    assert comfy.prompts[-1]["119"]["inputs"]["prompt"].startswith("Preserve the exact design")


def test_a_promoted_character_sheet_is_still_a_character(tmp_path: Path):
    """The kind the child carries is now the source's, and for a character that is the same.

    Worth its own test rather than trusting the parametrized one above: `parent_id` is read
    by the library and the child's `kind` by the reference path, so a change that made every
    sheet inherit correctly *except* the case that already existed would be the one nobody
    noticed until an old project stopped listing its sheets under characters.
    """
    client, store, _comfy = make_client(tmp_path)
    project = store.create(Project(name="Character"))
    uploaded = client.post(
        f"/api/projects/{project.id}/assets/upload",
        data={"name": "Mara", "kind": "character"},
        files={"file": ("mara.png", b"png-data", "image/png")},
    ).json()

    response = client.post(
        f"/api/projects/{project.id}/assets/{uploaded['assets'][0]['id']}/multiview",
        json={"prompt": "Preserve Mara", "seed": 1},
    )

    assert response.status_code == 202
    assert store.get(project.id).assets[-1].kind == "character"


@pytest.mark.parametrize("kind", ["style", "image", "audio", "video"])
def test_a_kind_with_no_template_is_refused_by_a_message_naming_what_can_be(
    tmp_path: Path, kind: str
):
    """A refusal that only says no leaves the Director guessing which asset to reach for.

    The sentence is built from the same mapping the gate reads, so the kinds it names are
    the kinds that actually pass — a hardcoded list would go stale in the direction that
    sends a Director to upload a prop the route then refuses.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Unsupported"))
    suffix, media = {
        "audio": (".wav", "audio/wav"),
        "video": (".mp4", "video/mp4"),
    }.get(kind, (".png", "image/png"))
    uploaded = client.post(
        f"/api/projects/{project.id}/assets/upload",
        data={"name": "Not a subject", "kind": kind},
        files={"file": (f"media{suffix}", b"data", media)},
    ).json()

    response = client.post(
        f"/api/projects/{project.id}/assets/{uploaded['assets'][0]['id']}/multiview",
        json={"prompt": "anything", "seed": 0},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail == multiview_refusal()
    # Every kind that *would* have worked is named. Asserted against the mapping rather
    # than against the sentence, so adding a subject without extending the refusal fails.
    for promotable in MULTIVIEW_SUBJECTS:
        assert promotable in detail, detail
    # Refused before anything reached the GPU or the manifest.
    assert comfy.prompts == []
    assert len(store.get(project.id).assets) == 1


def test_a_promotable_kind_with_no_image_yet_is_refused_the_same_way(tmp_path: Path):
    """The matrix's "wrong media" row: a prop that has not rendered is not promotable yet."""
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Pending"))
    project.assets.append(Asset(name="Unrendered hauler", kind="prop", path="", source="flux"))
    store.save(project)

    response = client.post(
        f"/api/projects/{project.id}/assets/{project.assets[0].id}/multiview",
        json={"prompt": "Preserve the exact design of this object", "seed": 0},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == multiview_refusal()
    assert comfy.prompts == []


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
            # A *declared* mode in the current vocabulary. `"image"` was the legacy value here,
            # and it is now resolved to "undeclared" on load — see `LEGACY_SHOT_MODES` — so
            # asserting it survived would have asserted the migration did not happen. What this
            # test is about is that a Director shot application does not reset a declaration the
            # Director made, which needs a declaration that can be made.
            mode="image_to_video",
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
    assert shot.mode == "image_to_video"
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


def test_an_imported_songs_lyrics_and_style_reach_the_directors_context(tmp_path: Path):
    """The entire reason for storing them, asserted against what the model was actually handed.

    Not against the stored project: that a field is on the Song proves only that it was saved, and
    the claim is that it reaches the prompt. `DIRECTOR_CONTEXT_EXCLUDE` strips whole keys, so a
    later exclusion added for the song — or for a field beside it — would silently take this away
    with every Song assertion in the suite still green. The recording double is the only witness.

    Both are read out of the dump *and* out of its serialisation, because the dump is what is
    encoded into the prompt: a lyric sheet present as a key but empty would satisfy the first check
    alone.
    """
    director = RevisingDirector()
    client, store = make_client_with_director(tmp_path, director)
    project = store.create(Project(name="Song context reaches the model"))
    import_song(
        client, project.id, lyrics=IMPORTED_LYRIC_SHEET, caption=IMPORTED_SONG_STYLE
    )

    client.post(
        f"/api/projects/{project.id}/director/chat", json={"message": "What is this song about?"}
    )

    context = director.contexts[0]
    assert context["song"]["lyrics"] == IMPORTED_LYRIC_SHEET
    assert context["song"]["caption"] == IMPORTED_SONG_STYLE
    serialised = json.dumps(context)
    assert "counting sodium lights" in serialised
    assert "tape saturation" in serialised
    # There *is* now an exclusion under `song` — the two recovery slots — so the rule this asserts
    # is no longer "nothing is excluded" but "exactly the withheld set is, and nothing beside it".
    # Read off the classification rather than restated, so a field moved from one side to the other
    # cannot leave this test asserting the old answer.
    assert DIRECTOR_CONTEXT_EXCLUDE["song"] == set(SONG_DIRECTOR_WITHHELD)
    for shown in SONG_DIRECTOR_VISIBLE:
        assert shown in context["song"], shown
    # The same holds after a correction: the edit route is the other door into the same fields.
    client.put(
        f"/api/projects/{project.id}/song/context",
        json={"lyrics": "Corrected words only.", "caption": "Corrected sound only."},
    )
    client.post(f"/api/projects/{project.id}/director/chat", json={"message": "And now?"})
    assert director.contexts[1]["song"]["lyrics"] == "Corrected words only."
    assert director.contexts[1]["song"]["caption"] == "Corrected sound only."


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


def test_an_imported_songs_lyrics_and_style_reach_the_model_on_the_expansion_path(tmp_path: Path):
    """The claim the docs used to get wrong, asserted against what the model was actually handed.

    The reach used to be the chat route only, and expansion — the planning act most likely to
    want the words — wrote every prompt without them. That a field is on the Song proves it was
    saved; that it is in `expansion_input`'s return proves the builder emits it. Neither proves
    the model saw it, because the route builds the payload from a pre-await snapshot and hands
    it across a boundary of its own. The recording double is the only witness, exactly as
    `test_an_imported_songs_lyrics_and_style_reach_the_directors_context` is for chat.

    Read out of the recorded input *and* out of its serialisation, because serialising is what
    the client then encodes into the prompt: a lyric sheet present as a key but empty would
    satisfy the key check alone.
    """
    director = ExpandingDirector()
    client, store, _ = make_client(tmp_path, director)
    project = planned_project(store, "Expansion sees the song")
    import_song(client, project.id, lyrics=IMPORTED_LYRIC_SHEET, caption=IMPORTED_SONG_STYLE)

    assert client.post(f"/api/projects/{project.id}/director/expand").status_code == 200

    sent = director.inputs[0]
    assert sent["song"]["lyrics"] == IMPORTED_LYRIC_SHEET
    assert sent["song"]["caption"] == IMPORTED_SONG_STYLE
    serialised = json.dumps(sent)
    assert "counting sodium lights" in serialised
    assert "tape saturation" in serialised
    # Whole and unaltered: the interior structure of the sheet is what a summariser or an
    # excerpter inserted anywhere along this path would be the first thing to lose.
    assert "\n    counting sodium lights" in sent["song"]["lyrics"]
    assert "[Chorus]" in sent["song"]["lyrics"]
    # The correction route is the other door into the same two fields, and it reaches expansion
    # for the same reason — the payload is built per call, from the stored Song.
    client.put(
        f"/api/projects/{project.id}/song/context",
        json={"lyrics": "Corrected words only.", "caption": "Corrected sound only."},
    )
    assert client.post(f"/api/projects/{project.id}/director/expand").status_code == 200
    assert director.inputs[1]["song"]["lyrics"] == "Corrected words only."
    assert director.inputs[1]["song"]["caption"] == "Corrected sound only."
    # And a song that says neither is a song block of title and duration, absent not empty —
    # the payload an expansion built before either field existed.
    bare = planned_project(store, "Expansion without the words")
    import_song(client, bare.id)
    assert client.post(f"/api/projects/{bare.id}/director/expand").status_code == 200
    assert set(director.inputs[2]["song"]) == {"title", "duration"}


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
    """A 4s window plus the over-render margin is 4.5s = 108 frames, off H3's 17k+5 grid,
    and must round up to 124 — the take always runs longer than the window that asked."""
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Grid"))
    project.shots = [Shot(start=0, duration=4, prompt="Off-grid", mode="text", status="ready")]
    store.save(project)

    client.post(f"/api/projects/{project.id}/shots/{project.shots[0].id}/generate/h3", json={})

    inputs = comfy.prompts[-1]["2343"]["inputs"]
    assert inputs["duration_frames"] == 124
    assert (inputs["duration_frames"] - 5) % 17 == 0
    # The ruling itself: never exact or lesser than the window.
    assert inputs["duration_frames"] / 24 >= 4 + 0.5


# --- H3 sampling profiles at the route -------------------------------------------------
#
# The builder owns which graph each profile emits (`tests/test_workflows.py`); these own
# the half between the Director and the builder: the profile reaches it, an unknown one
# never gets that far, and an explicit step count still wins.
#
# `H3Request` and the profile table are imported here rather than at the top of the file
# to keep this block self-contained.


def h3_reference_project(client, store, tmp_path: Path) -> tuple[str, str]:
    """A project with one ready reference Shot, returned as `(project id, shot id)`."""
    project = store.create(Project(name="Profiles"))
    lead = upload_asset(client, project.id, "Lead vocalist", "character", "lead.png")
    return project.id, reference_shot(store, project.id, asset_ids=[lead["id"]])


def rearm_shot(store, project_id: str, shot_id: str) -> None:
    """Put a submitted Shot back to `ready` so the same one can be submitted again.

    A successful submission sets `queued`, and the route refuses anything but `ready`.
    These tests deliberately submit *one* Shot under several profiles: two graphs built
    from the same Shot are comparable, two built from two Shots differ by their ids.
    """
    project = store.get(project_id)
    shot = next(item for item in project.shots if item.id == shot_id)
    shot.status = "ready"
    shot.prompt_id = ""
    store.save(project)


def test_the_h3_profile_reaches_the_builder(tmp_path: Path):
    """The turbo profile submitted is the turbo graph, LoRA and all.

    Asserting the submitted payload rather than a mocked call, because the route's job is
    to put the Director's choice into the graph that ComfyUI receives — a profile that
    reached the builder and was then dropped would satisfy anything weaker.
    """
    from music_video_producer.workflows import H3_REFERENCE_PROFILES

    client, store, comfy = make_client(tmp_path)
    project_id, shot_id = h3_reference_project(client, store, tmp_path)
    profile = H3_REFERENCE_PROFILES["turbo"]

    assert submit_h3(client, project_id, shot_id, profile="turbo").status_code == 202

    payload = comfy.prompts[-1]
    assert payload["mvp:lora"]["inputs"]["lora_name"] == profile.lora
    assert payload["mvp:lora"]["inputs"]["strength_model"] == profile.lora_strength
    assert payload["mvp:shift"]["inputs"]["model"] == ["mvp:lora", 0]
    assert payload["mvp:scheduler"]["inputs"]["scheduler"] == profile.scheduler
    assert payload["mvp:scheduler"]["inputs"]["steps"] == profile.steps
    assert payload["mvp:sampler"]["inputs"]["sampler_name"] == profile.sampler


def test_an_omitted_h3_profile_submits_the_default_graph(tmp_path: Path):
    """The existing caller's payload, unchanged: no LoRA, `simple`/`res_multistep`, 20 steps.

    This is the route half of the story's central promise. A body of `{}` is what every
    shipped client sends today.
    """
    from music_video_producer.workflows import H3_REFERENCE_PROFILES

    client, store, comfy = make_client(tmp_path)
    project_id, shot_id = h3_reference_project(client, store, tmp_path)
    profile = H3_REFERENCE_PROFILES["default"]

    assert submit_h3(client, project_id, shot_id).status_code == 202

    payload = comfy.prompts[-1]
    assert all(node["class_type"] != "LoraLoaderModelOnly" for node in payload.values())
    assert payload["mvp:shift"]["inputs"]["model"] == ["mvp:model", 0]
    assert payload["mvp:scheduler"]["inputs"]["scheduler"] == profile.scheduler
    assert payload["mvp:scheduler"]["inputs"]["steps"] == profile.steps == 20
    assert payload["mvp:sampler"]["inputs"]["sampler_name"] == profile.sampler
    # Naming the default explicitly submits the same graph as omitting it.
    rearm_shot(store, project_id, shot_id)
    assert submit_h3(client, project_id, shot_id, profile="default").status_code == 202
    assert comfy.prompts[-1] == payload


def test_an_unknown_h3_profile_is_refused_before_any_submission(tmp_path: Path):
    """422 from validation, and nothing queued.

    "Nothing queued" is the assertion that matters: a profile rejected after the payload
    reached ComfyUI would still have cost GPU minutes for a render nobody asked for.
    """
    client, store, comfy = make_client(tmp_path)
    project_id, shot_id = h3_reference_project(client, store, tmp_path)

    for unknown in ("fast", "TURBO", "turbo ", "", None, 4):
        response = submit_h3(client, project_id, shot_id, profile=unknown)
        assert response.status_code == 422, (unknown, response.text)
        # A *list* detail is FastAPI's request-validation shape; the builder's own refusal
        # arrives as a string. Asserting the list is what pins "refused before a payload is
        # built" rather than "refused somewhere on the way to ComfyUI" — widening the field
        # to a plain `str` would still 422 through the builder and satisfy anything weaker.
        assert isinstance(response.json()["detail"], list), (unknown, response.text)
    assert comfy.prompts == []


def test_an_explicit_step_count_overrides_the_h3_profile_default(tmp_path: Path):
    """The profile chooses the graph; the Director chooses the effort.

    Both profiles, because "explicit steps win" is only interesting where the profile has
    an opinion — and a route that honoured the override on one profile and not the other
    would be the harder bug to see.
    """
    from music_video_producer.workflows import H3_REFERENCE_PROFILES

    client, store, comfy = make_client(tmp_path)
    project_id, shot_id = h3_reference_project(client, store, tmp_path)

    for name, profile in H3_REFERENCE_PROFILES.items():
        rearm_shot(store, project_id, shot_id)
        assert submit_h3(client, project_id, shot_id, profile=name, steps=12).status_code == 202
        assert comfy.prompts[-1]["mvp:scheduler"]["inputs"]["steps"] == 12, name
        # The override changes the effort and nothing else the profile decided.
        assert comfy.prompts[-1]["mvp:scheduler"]["inputs"]["scheduler"] == profile.scheduler
        assert ("mvp:lora" in comfy.prompts[-1]) is (profile.lora is not None), name

        rearm_shot(store, project_id, shot_id)
        assert submit_h3(client, project_id, shot_id, profile=name).status_code == 202
        assert comfy.prompts[-1]["mvp:scheduler"]["inputs"]["steps"] == profile.steps, name


def test_the_text_only_h3_path_keeps_its_step_default_without_a_profile(tmp_path: Path):
    """`H3Request.steps` became optional; the Director graph must not have lost its 20.

    That path takes no profile — different checkpoint pair, no live evidence for a turbo
    bundle — so an omitted count has to keep meaning what it meant before profiles
    existed, rather than falling through to `None` or to a reference profile's number.
    """
    from music_video_producer.workflows import H3_DIRECTOR_DEFAULT_STEPS

    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Text only"))
    project.shots = [Shot(start=0, duration=5, prompt="A singer", mode="text", status="ready")]
    store.save(project)

    assert submit_h3(client, project.id, project.shots[0].id).status_code == 202

    payload = comfy.prompts[-1]
    assert payload["2346"]["inputs"]["steps"] == H3_DIRECTOR_DEFAULT_STEPS == 20
    assert all(node["class_type"] != "LoraLoaderModelOnly" for node in payload.values())
    # An explicit count still reaches it.
    rearm_shot(store, project.id, project.shots[0].id)
    assert submit_h3(client, project.id, project.shots[0].id, steps=7).status_code == 202
    assert comfy.prompts[-1]["2346"]["inputs"]["steps"] == 7


def test_the_route_offers_every_profile_the_builder_defines(tmp_path: Path):
    """The `Literal` and the profile table must not drift apart.

    A profile added to `H3_REFERENCE_PROFILES` and not offered here is unreachable per
    render — the story's whole point — and a name offered here that the builder does not
    know would be a 500 on submission instead of a 422 on validation.
    """
    from music_video_producer.app import H3Request
    from music_video_producer.workflows import H3_DEFAULT_PROFILE, H3_REFERENCE_PROFILES

    offered = set(get_args(H3Request.model_fields["profile"].annotation))

    assert offered == set(H3_REFERENCE_PROFILES)
    assert H3Request().profile == H3_DEFAULT_PROFILE
    assert H3Request().steps is None


def test_a_profile_on_a_text_only_shot_is_refused_rather_than_ignored(tmp_path: Path):
    """The field is on the request model *both* branches bind, and only one honours it.

    Before this refusal, `{"profile": "turbo"}` on a Shot with no references returned 202
    and rendered the 20-step no-LoRA Director graph — a full-price GPU job, logged under a
    configuration that was never applied and indistinguishable afterwards from a default
    render. Nothing recorded that the request was not honoured, which is the part that
    makes it worse than a refusal: only the refusal is visible.

    Refused rather than accepted-and-proven-inert, because the Director asking for turbo
    on a text-only Shot is asking for something this project has no evidence for — a
    different checkpoint pair through `MiniMaxH3DirectorCS` and a LoRA that is not the
    `ref2v` one. Saying so costs nothing; rendering the wrong thing costs GPU minutes.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Text only profile"))
    project.shots = [Shot(start=0, duration=5, prompt="A singer", mode="text", status="ready")]
    store.save(project)
    shot_id = project.shots[0].id

    response = submit_h3(client, project.id, shot_id, profile="turbo")

    assert response.status_code == 422
    detail = response.json()["detail"]
    # The builder's/route's own refusal, not a validation error: `turbo` is a real profile,
    # so the message has to explain *why it does not apply here* rather than list options.
    assert isinstance(detail, str), detail
    assert "reference shots only" in detail and "turbo" in detail
    # Named as the timeline names it, like every other refusal on this route.
    from music_video_producer.batch import shot_label

    saved = store.get(project.id)
    assert shot_label(saved, saved.shots[0]) in detail
    assert comfy.prompts == []
    # The same Shot still renders when the profile is dropped or explicitly default, so
    # the refusal is about the profile and not about the Shot.
    assert submit_h3(client, project.id, shot_id).status_code == 202
    rearm_shot(store, project.id, shot_id)
    assert submit_h3(client, project.id, shot_id, profile="default").status_code == 202
    assert len(comfy.prompts) == 2
    assert all("2343" in payload for payload in comfy.prompts)

    # And attaching a reference makes the very same profile submittable, which is the line
    # the refusal is actually drawn on.
    lead = upload_asset(client, project.id, "Lead vocalist", "character", "lead.png")
    reference_id = reference_shot(store, project.id, asset_ids=[lead["id"]])
    assert submit_h3(client, project.id, reference_id, profile="turbo").status_code == 202
    assert "mvp:lora" in comfy.prompts[-1]


# --- LTX 2.5 enhancement -----------------------------------------------------------------
#
# The route's own tests. Every row of the spec's I/O matrix that a route can answer is here;
# the two it cannot are elsewhere by design — "model absent" is the pre-flight's
# (`tests/preflight_ltx25_enhance.py`), and "frame count is measured, never assumed" is a live
# `ffprobe` reading plus the source guard in `tests/test_workflows.py`.


def enhanced_shot_project(
    store, tmp_path: Path, *, take: str = "takes/shot-h3-reference_00001-audio.mp4", **shot
):
    """A project whose one Shot has a take on disk under ComfyUI's output directory.

    The Shot is `not_singing` by default because that is the only state the singing gate
    passes — the Director ruled that `singing` refuses outright and `unknown` refuses with
    the fix named, since the enhancer measurably moves lip position. Tests exercising the
    gate itself override this.
    """
    project = store.create(Project(name="Enhance"))
    if take:
        output = tmp_path / "comfy" / "output" / Path(take)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"rendered-take")
    shot.setdefault("singing", "not_singing")
    project.shots = [
        Shot(
            start=0,
            duration=5,
            prompt="Lantern light across the corridor",
            latest_output=take,
            status="complete",
            **shot,
        )
    ]
    store.save(project)
    return project


def enhance(client, project, shot=None):
    shot_id = (shot or project.shots[0]).id
    return client.post(f"/api/projects/{project.id}/shots/{shot_id}/enhance/ltx25")


def test_enhancing_a_take_submits_it_as_input_and_never_re_runs_h3(tmp_path: Path):
    """The matrix's first row, and the frozen "Always" that goes with it.

    The take is submitted as the graph's input, the payload has no MiniMax node in it at all,
    and the output prefix is not the render's — so ComfyUI numbers the enhanced file in its
    own series rather than as the next take.
    """
    client, store, comfy = make_client(tmp_path)
    project = enhanced_shot_project(store, tmp_path)

    response = enhance(client, project)

    assert response.status_code == 202
    job = response.json()
    assert job["kind"] == "ltx"
    assert job["target_id"] == project.shots[0].id
    assert len(comfy.prompts) == 1
    payload = comfy.prompts[0]
    source = payload["mvp:source"]["inputs"]["video"]
    assert source.endswith("takes/shot-h3-reference_00001-audio.mp4")
    assert Path(source).is_file()
    # Nothing on this path regenerates the take.
    assert not any("MiniMaxH3" in node["class_type"] for node in payload.values())
    # A different prefix from any render's, which is what puts the enhanced file beside the
    # take instead of in the middle of its numbered series.
    prefix = payload["mvp:save"]["inputs"]["filename_prefix"]
    assert prefix.endswith(ENHANCE_PREFIX_SUFFIX)
    assert "-h3" not in prefix.rsplit("/", 1)[-1].replace(ENHANCE_PREFIX_SUFFIX, "")


def test_enhancing_a_take_carries_the_sources_own_audio_and_synthesises_none(tmp_path: Path):
    """The matrix's audio rows, at the route.

    Both of them, because the graph answers both the same way: the only audio input the saver
    has is the source loader's own third output, so a take with audio keeps it and a silent
    take has nothing to invent one from. There is no audio VAE, no audio decode, and no branch
    here that behaves differently for a silent source.
    """
    client, store, comfy = make_client(tmp_path)
    project = enhanced_shot_project(store, tmp_path)

    assert enhance(client, project).status_code == 202

    payload = comfy.prompts[0]
    assert payload["mvp:save"]["inputs"]["audio"] == ["mvp:source", 2]
    assert not any("audio" in node["class_type"].lower() for node in payload.values())
    assert not any(
        isinstance(value, str) and "audio-vae" in value
        for node in payload.values()
        for value in node["inputs"].values()
    )


def test_a_silent_take_is_enhanced_without_a_second_code_path(tmp_path: Path):
    """The "source without audio" row: handled, and handled by *not* being a special case.

    The route does not probe the file, so the graph it submits for a silent take is the graph
    it submits for one with a soundtrack. `VHS_VideoCombine` reads `audio['waveform']` inside a
    try and writes video only when there is none — nothing here can invent a track because
    nothing here can produce one.
    """
    client, store, comfy = make_client(tmp_path)
    with_audio = enhanced_shot_project(store, tmp_path)
    assert enhance(client, with_audio).status_code == 202

    silent_client, silent_store, silent_comfy = make_client(tmp_path / "silent")
    silent = enhanced_shot_project(
        silent_store, tmp_path / "silent", take="takes/shot-h3-reference_00001.mp4"
    )
    assert enhance(silent_client, silent).status_code == 202

    def without_paths(payload: dict) -> dict:
        stripped = copy.deepcopy(payload)
        stripped["mvp:source"]["inputs"]["video"] = ""
        stripped["mvp:save"]["inputs"]["filename_prefix"] = ""
        return stripped

    assert without_paths(silent_comfy.prompts[0]) == without_paths(comfy.prompts[0])


def test_a_shot_that_never_rendered_is_refused_before_a_payload_is_built(tmp_path: Path):
    """The matrix's "no take" row. Enhancement improves a take; it cannot make one."""
    client, store, comfy = make_client(tmp_path)
    project = enhanced_shot_project(store, tmp_path, take="")
    project.shots[0].status = "draft"
    store.save(project)

    response = enhance(client, project)

    assert response.status_code == 422
    assert "has not produced a take" in response.json()["detail"]
    assert comfy.prompts == []


def test_a_take_whose_file_is_gone_is_refused_and_the_path_is_named(tmp_path: Path):
    """The matrix's "missing file" row, including the part that makes it actionable.

    Naming the path is the point: a manifest pointing at nothing is usually a moved or cleared
    output directory, and the Director cannot tell which without seeing where this looked.
    """
    client, store, comfy = make_client(tmp_path)
    project = enhanced_shot_project(store, tmp_path)
    (tmp_path / "comfy" / "output" / project.shots[0].latest_output).unlink()

    response = enhance(client, project)

    assert response.status_code == 422
    assert project.shots[0].latest_output in response.json()["detail"]
    assert comfy.prompts == []


def test_a_take_pointer_that_escapes_the_output_directory_is_refused(tmp_path: Path):
    """`latest_output` is manifest data, and a manifest can be edited or arrive stale.

    Without the containment check a `..` in that field would hand an arbitrary file on the
    machine to a node that opens it by path.
    """
    client, store, comfy = make_client(tmp_path)
    outside = tmp_path / "elsewhere.mp4"
    outside.write_bytes(b"not-a-take")
    project = enhanced_shot_project(store, tmp_path, take="")
    project.shots[0].latest_output = "../../elsewhere.mp4"
    store.save(project)

    response = enhance(client, project)

    assert response.status_code == 422
    assert comfy.prompts == []


def test_an_enhancement_already_in_flight_refuses_a_second_one(tmp_path: Path):
    """The matrix's 409 row, and the harm it names.

    Read off the job records alone, because an enhancement deliberately writes nothing to the
    Shot — so `Shot.status` cannot know about one and the jobs are the only evidence.
    """
    client, store, comfy = make_client(tmp_path)
    project = enhanced_shot_project(store, tmp_path)
    assert enhance(client, project).status_code == 202
    assert store.get(project.id).jobs[-1].status == "queued"

    second = enhance(client, project)

    assert second.status_code == 409
    assert "has not finished" in second.json()["detail"]
    assert len(comfy.prompts) == 1


def test_a_render_in_flight_also_refuses_an_enhancement(tmp_path: Path):
    """A take that is about to be superseded is not worth GPU minutes.

    Answered 409 rather than 422 for `mark_ready_refusal`'s reason: a live job is a state
    conflict, and the same request succeeds once it lands.
    """
    client, store, comfy = make_client(tmp_path)
    project = enhanced_shot_project(store, tmp_path)
    project.shots[0].status = "running"
    store.save(project)

    response = enhance(client, project)

    assert response.status_code == 409
    assert comfy.prompts == []


def test_enhancing_writes_nothing_to_the_shot_and_the_take_survives_completion(tmp_path: Path):
    """The matrix's last row, across the whole lifecycle rather than only at submission.

    The take is still on disk *and still identified*: `latest_output` goes on naming it after
    the enhancement completes, because `read_job` has no branch for `kind="ltx"`. The enhanced
    file is not lost — it stays on the job that produced it, which is where a take that is not
    tracked is recovered from.
    """
    client, store, comfy = make_client(tmp_path)
    project = enhanced_shot_project(store, tmp_path)
    take = tmp_path / "comfy" / "output" / project.shots[0].latest_output
    before = store.get(project.id).shots[0].model_dump(mode="json")

    job = enhance(client, project).json()

    assert store.get(project.id).shots[0].model_dump(mode="json") == before
    enhanced = f"music-video-producer/{project.id}/shots/{project.shots[0].id}-ltx25-enhance"
    comfy.history = completed_history_for(
        [{"subfolder": enhanced.rsplit("/", 1)[0], "filename": "s-ltx25-enhance_00001-audio.mp4"}]
    )

    refreshed = client.get(f"/api/projects/{project.id}/jobs/{job['id']}")

    assert refreshed.status_code == 200
    assert refreshed.json()["status"] == "complete"
    saved = ProjectStore(tmp_path).get(project.id)
    # The shot is byte-identical to what it was before the enhancement was ever submitted.
    assert saved.shots[0].model_dump(mode="json") == before
    assert saved.shots[0].latest_output == before["latest_output"]
    assert take.is_file()
    # And the enhanced file is reachable, on the job rather than on the shot.
    assert saved.jobs[-1].output_files == [
        f"{enhanced.rsplit('/', 1)[0]}/s-ltx25-enhance_00001-audio.mp4"
    ]


def test_an_enhancement_seed_is_the_one_the_graph_fixes_not_the_shots(tmp_path: Path):
    """The job records what was sampled, which is the export's seed rather than the Shot's.

    A job carrying the Shot's seed would be a record of a number this graph never used.
    """
    client, store, comfy = make_client(tmp_path)
    project = enhanced_shot_project(store, tmp_path, seed=4242)

    job = enhance(client, project).json()

    assert job["seed"] == LTX25_ENHANCE_SEED
    assert comfy.prompts[0]["mvp:noise"]["inputs"]["noise_seed"] == LTX25_ENHANCE_SEED


def test_enhancing_an_unknown_shot_is_a_404_and_a_downstream_failure_is_a_502(tmp_path: Path):
    client, store, comfy = make_client(tmp_path)
    project = enhanced_shot_project(store, tmp_path)

    assert client.post(f"/api/projects/{project.id}/shots/shot_nope/enhance/ltx25").status_code == 404

    comfy.submit_error = True
    failed = enhance(client, project)

    assert failed.status_code == 502
    # Nothing was recorded for a submission that never happened.
    assert store.get(project.id).jobs == []


def test_the_enhancement_route_takes_no_body_and_exposes_no_sampling_controls(tmp_path: Path):
    """The Ask First fields are not reachable, including by a client that sends them.

    The sigmas, the detailer strength and the prompt are marked Ask First and nothing has been
    asked, so the route has no request model at all — a body naming any of them is ignored and
    the submitted graph is the export's.
    """
    client, store, comfy = make_client(tmp_path)
    project = enhanced_shot_project(store, tmp_path)

    response = client.post(
        f"/api/projects/{project.id}/shots/{project.shots[0].id}/enhance/ltx25",
        json={"sigmas": "1, 0", "strength": 0.1, "prompt": "make it cinematic"},
    )

    assert response.status_code == 202
    payload = comfy.prompts[0]
    assert payload["mvp:sigmas"]["inputs"]["sigmas"] == LTX25_ENHANCE_SIGMAS
    assert payload["mvp:detailer"]["inputs"]["strength_model"] == LTX25_ENHANCE_DETAILER_STRENGTH
    assert payload["mvp:prompt"]["inputs"]["text"] == ""


# --- The song audio window and the frame it renders into -------------------------------
#
# Two fixes that arrived together and are tested together, because the live comparison
# render exercises both: a shot at roughly 12 s, at 0.6 MP, driven by the seconds of the
# song it actually occupies.


def windowed_project(store, client, *, start: float, duration: float):
    """One ready reference Shot at `start`, with a picture and the master song.

    The Song's `duration` is 154 s -- the real master track's length -- so the refusal
    boundary these tests probe is the one the live render will meet.
    """
    project = store.create(Project(name="Window"))
    lead = upload_asset(client, project.id, "Lead vocalist", "character", "lead.png")
    client.post(
        f"/api/projects/{project.id}/songs/upload",
        data={"title": "Harder Faster (Female Cover)", "duration": "154"},
        files={"file": ("harder.mp3", b"ID3fake-mp3-bytes", "audio/mpeg")},
    )
    project = store.get(project.id)
    project.shots = [
        Shot(
            start=start,
            duration=duration,
            prompt="The vocalist sings to camera under one amber light.",
            asset_ids=[lead["id"]],
            use_song_audio=True,
            status="ready",
            seed=7,
        )
    ]
    store.save(project)
    return store.get(project.id)


def submitted_media(comfy) -> list[dict]:
    return json.loads(comfy.prompts[-1]["mvp:references"]["inputs"]["media_state"])


def test_a_reference_shot_hears_its_own_part_of_the_song(tmp_path: Path):
    """The window is the Shot's own `start` and `duration`, the two the timeline draws.

    Before this the route handed the loader the whole file with no offset, so H3 was
    conditioned on the opening of the track wherever the Shot sat. On this master the
    lyrics do not begin until about 8 s, so a 0-3.75 s window contains no sung words at
    all -- which is what "voices but no phonetics" was.
    """
    client, store, comfy = make_client(tmp_path)
    project = windowed_project(store, client, start=12.0, duration=3.75)

    assert submit_h3(client, project.id, project.shots[0].id).status_code == 202

    song = next(item for item in submitted_media(comfy) if item["label"] == "master song")
    # The shot's own 12.0-15.75 window, extended by the over-render margin: a 0.25 s lead
    # ahead of the window and the grid's tail behind it, so the whole 107-frame picture is
    # performed against real song seconds and editable room exists at either end.
    assert song["trim"] == {"start": 11.75, "end": 11.75 + 107 / 24}
    # The visual span and the audio span are still the same span: `length` is the frame
    # count for exactly the trimmed seconds, so nothing lets the two diverge silently.
    assert comfy.prompts[-1]["mvp:condition"]["inputs"]["length"] == 107


def test_submission_records_the_takes_lead_on_the_shot(tmp_path: Path):
    """`latest_take_lead` is written at the moment of truth, with `prompt_id`.

    The lead cannot be derived later — a pre-margin take and a post-margin one are
    indistinguishable by arithmetic on their lengths — so the submission write is the one
    record of where the sync-correct cut sits. A song-audio shot mid-song records the
    quarter-second lead its trim actually carried; a text-only shot records 0, because its
    take starts at the window and all margin is tail.
    """
    client, store, comfy = make_client(tmp_path)
    project = windowed_project(store, client, start=12.0, duration=3.75)

    assert submit_h3(client, project.id, project.shots[0].id).status_code == 202
    recorded = ProjectStore(tmp_path).get(project.id).shots[0]
    assert recorded.latest_take_lead == 0.25
    # The recorded lead and the submitted trim agree — one is the record of the other.
    song = next(item for item in submitted_media(comfy) if item["label"] == "master song")
    assert song["trim"]["start"] == 12.0 - recorded.latest_take_lead

    text = store.create(Project(name="Text lead"))
    text.shots = [Shot(start=12.0, duration=3.75, prompt="A wide shot.", status="ready")]
    store.save(text)
    assert submit_h3(client, text.id, text.shots[0].id).status_code == 202
    assert ProjectStore(tmp_path).get(text.id).shots[0].latest_take_lead == 0.0


def test_a_moved_shot_renders_its_new_window_with_no_stale_offset(tmp_path: Path):
    """The window is derived per submission, so there is nowhere for an old one to live."""
    client, store, comfy = make_client(tmp_path)
    project = windowed_project(store, client, start=12.0, duration=3.75)
    shot_id = project.shots[0].id
    assert submit_h3(client, project.id, shot_id).status_code == 202
    assert submitted_media(comfy)[-1]["trim"] == {"start": 11.75, "end": 11.75 + 107 / 24}

    moved = store.get(project.id)
    moved.shots[0].start = 96.5
    store.save(moved)
    rearm_shot(store, project.id, shot_id)

    assert submit_h3(client, project.id, shot_id).status_code == 202
    assert submitted_media(comfy)[-1]["trim"] == {"start": 96.25, "end": 96.25 + 107 / 24}


def test_a_shot_at_zero_seconds_is_windowed_like_any_other(tmp_path: Path):
    """The 0 s shot is no longer a special case, and this asserts the *difference*.

    This test previously claimed the opposite -- byte-identity with the pre-fix payload at
    0 s -- on the spec's recorded premise that the conditioner already trimmed a whole-file
    reference to the render window, making 0 s the case where the bug and the correct
    behaviour coincided. That premise is false:
    `MiniMaxH3ReferenceToVideo._encode_ref_audio` VAE-encodes the entire waveform and never
    truncates. Byte-identity at 0 s therefore preserved the *defect* -- a 3.75 s shot riding
    154 seconds of song through every sampling step -- and preserved it for the shot most
    likely to exist in a fresh project. Renegotiated by the Director on 2026-08-18, and the
    name is changed with the claim so it stays honest about what it proves.

    The consequence is stated here rather than left to be inferred: **no reference payload
    carrying the master song is byte-identical to a pre-fix one any more.** Every one of them
    was conditioned on the whole track, so there is nothing left worth preserving. That is
    what the digest comparison below now demonstrates -- inequality, hashed, rather than
    equality.
    """
    client, store, comfy = make_client(tmp_path)
    project = windowed_project(store, client, start=0.0, duration=3.75)

    assert submit_h3(client, project.id, project.shots[0].id).status_code == 202

    submitted = comfy.prompts[-1]
    song = next(item for item in submitted_media(comfy) if item["label"] == "master song")
    # At 0 s the over-render lead has nowhere to go — the song starts here — so all the
    # margin is tail: the trim covers the full 107-frame picture from 0.
    assert song["trim"] == {"start": 0.0, "end": 107 / 24}

    # What the route used to send: the same graph with no window on the song at all.
    shipped = build_h3_reference_payload(
        prompt=submitted["mvp:condition"]["inputs"]["prompt"],
        references=[
            {key: value for key, value in item.items() if key not in {"enabled", "trim"}}
            for item in submitted_media(comfy)
        ],
        duration=3.75,
        seed=7,
        width=submitted["mvp:condition"]["inputs"]["width"],
        height=submitted["mvp:condition"]["inputs"]["height"],
        prefix=submitted["mvp:save"]["inputs"]["filename_prefix"],
    )

    assert submitted != shipped
    assert (
        hashlib.sha256(json.dumps(submitted, separators=(",", ":")).encode()).hexdigest()
        != hashlib.sha256(json.dumps(shipped, separators=(",", ":")).encode()).hexdigest()
    )
    # And the difference is exactly the window, nothing else: adding it back to the rebuild
    # closes the gap. A change that also moved a sampler or a loader would fail here.
    with_window = build_h3_reference_payload(
        prompt=submitted["mvp:condition"]["inputs"]["prompt"],
        references=submitted_media(comfy),
        duration=3.75,
        seed=7,
        width=submitted["mvp:condition"]["inputs"]["width"],
        height=submitted["mvp:condition"]["inputs"]["height"],
        prefix=submitted["mvp:save"]["inputs"]["filename_prefix"],
    )
    assert submitted == with_window


def test_a_window_past_the_end_of_the_song_costs_no_gpu_time(tmp_path: Path):
    """Refused before submission, naming both numbers, with nothing queued.

    The node would clamp instead: `media_io._slice_audio` ends at `min(total, ...)`, so
    the render would proceed against fewer seconds than were asked for and nothing would
    record the difference.
    """
    client, store, comfy = make_client(tmp_path)
    project = windowed_project(store, client, start=152.0, duration=3.75)

    response = submit_h3(client, project.id, project.shots[0].id)

    assert response.status_code == 422
    assert "155.75s" in response.json()["detail"]
    assert "154s" in response.json()["detail"]
    assert comfy.prompts == []
    # And nothing was written: the Shot is still `ready`, not `queued`.
    assert store.get(project.id).shots[0].status == "ready"

    # A shot longer than the whole song is the same refusal with both numbers named.
    long_project = windowed_project(store, client, start=0.0, duration=200.0)
    long_response = submit_h3(client, long_project.id, long_project.shots[0].id)
    assert long_response.status_code == 422
    assert "200s" in long_response.json()["detail"]
    assert "154s" in long_response.json()["detail"]
    assert comfy.prompts == []


def test_the_text_only_director_path_keeps_its_own_window_untouched(tmp_path: Path):
    """The path that was already correct is not touched, and grows no `trim`.

    `MiniMaxH3DirectorCS` takes `start_second`/`end_second` directly; the reference path's
    window lives in the media loader's `media_state`. Two different nodes expressing the
    same idea two different ways, and this asserts neither borrowed the other's.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Text only"))
    project.shots = [
        Shot(start=12.0, duration=3.75, prompt="A wide establishing shot.", status="ready")
    ]
    store.save(project)

    assert submit_h3(client, project.id, project.shots[0].id).status_code == 202

    payload = comfy.prompts[-1]
    assert "mvp:references" not in payload
    director = payload["2343"]["inputs"]
    # The window's end carries the over-render margin: 3.75 s renders 107 frames
    # (4.4583 s), so the Director node's own second-markers run to 12 + 107/24. The text
    # path has no audio to lead, so all margin is tail and the start is untouched.
    assert (director["start_second"], director["end_second"]) == (12.0, 12.0 + 107 / 24)
    # The key, not the substring: `VHS_VideoCombine.trim_to_audio` contains "trim" and
    # always has.
    assert all("trim" not in node["inputs"] for node in payload.values())


def test_a_reference_render_defaults_to_the_directors_own_frame(tmp_path: Path):
    """No geometry in the request selects 0.6 MP / 16:9 / 32 -- 1056x608.

    The measured H3 base from the 2026-08-17 boundary run, and the size the Director's own
    `ResolutionSelector` produces. What the route sent before was 1344x768, which nothing
    ever measured; what the smoke pinned was 640x384, which was chosen to save GPU time.
    """
    client, store, comfy = make_client(tmp_path)
    project = windowed_project(store, client, start=12.0, duration=3.75)
    shot_id = project.shots[0].id

    assert submit_h3(client, project.id, shot_id).status_code == 202
    conditioner = comfy.prompts[-1]["mvp:condition"]["inputs"]
    assert (conditioner["width"], conditioner["height"]) == (1056, 608)

    # An explicit frame is honoured exactly, unchanged from today.
    rearm_shot(store, project.id, shot_id)
    assert submit_h3(client, project.id, shot_id, width=640, height=384).status_code == 202
    explicit = comfy.prompts[-1]["mvp:condition"]["inputs"]
    assert (explicit["width"], explicit["height"]) == (640, 384)

    # The selector's own three inputs reach the same frame by name.
    rearm_shot(store, project.id, shot_id)
    named_response = submit_h3(
        client,
        project.id,
        shot_id,
        megapixels=0.6,
        aspect_ratio="16:9 (Widescreen)",
        multiple=32,
    )
    assert named_response.status_code == 202
    named = comfy.prompts[-1]["mvp:condition"]["inputs"]
    assert (named["width"], named["height"]) == (1056, 608)


def test_a_request_naming_a_frame_two_ways_is_refused_rather_than_resolved(tmp_path: Path):
    """Both kinds of geometry in one request is a 422, and nothing is queued.

    Silently preferring either one produces a render nobody asked for while looking like a
    render somebody did -- the same argument the sampling-profile refusal makes one field
    over, and for the same reason: only the refusal is visible afterwards.
    """
    client, store, comfy = make_client(tmp_path)
    project = windowed_project(store, client, start=12.0, duration=3.75)
    shot_id = project.shots[0].id

    response = submit_h3(client, project.id, shot_id, width=640, height=384, megapixels=0.6)

    assert response.status_code == 422
    assert "not both" in response.json()["detail"]
    assert comfy.prompts == []
    assert store.get(project.id).shots[0].status == "ready"

    # Out of the selector's declared range is refused by the request model itself, before
    # any payload exists.
    assert submit_h3(client, project.id, shot_id, megapixels=99.0).status_code == 422
    assert submit_h3(client, project.id, shot_id, multiple=7).status_code == 422
    assert submit_h3(client, project.id, shot_id, aspect_ratio="16:9").status_code == 422
    assert comfy.prompts == []


def test_the_selector_fields_are_refused_on_a_text_only_shot(tmp_path: Path):
    """The same refusal `profile` gets, one field over and for the same reason.

    `ResolutionSelector` is node `115` of the *reference* chain; this branch builds
    `MiniMaxH3DirectorCS`, which sizes its own frame and has never been measured at
    0.6 MP. Accepting the field and resolving it anyway would queue a full-price render at
    a size this path has no evidence for, logged as though it had been chosen.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Text only"))
    project.shots = [
        Shot(start=0, duration=3.75, prompt="A wide establishing shot.", status="ready")
    ]
    store.save(project)
    shot_id = project.shots[0].id

    response = submit_h3(client, project.id, shot_id, megapixels=0.6)

    assert response.status_code == 422
    assert "megapixels" in response.json()["detail"]
    assert comfy.prompts == []

    # Its own default is unchanged: an omitted frame here is still 1344x768.
    assert submit_h3(client, project.id, shot_id).status_code == 202
    director = comfy.prompts[-1]["2343"]["inputs"]
    assert (director["custom_width"], director["custom_height"]) == (
        H3_DIRECTOR_DEFAULT_WIDTH,
        H3_DIRECTOR_DEFAULT_HEIGHT,
    )


def test_the_route_offers_every_aspect_ratio_the_selector_knows(tmp_path: Path):
    """The `Literal` and the adapter's table must not drift apart.

    A ratio in the table and not offered here is unreachable per render; one offered here
    that the table does not know would be a 500 on submission instead of a 422 on
    validation.
    """
    from music_video_producer.app import H3Request

    annotation = H3Request.model_fields["aspect_ratio"].annotation
    offered = {value for arg in get_args(annotation) for value in get_args(arg)}

    assert offered == set(H3_ASPECT_RATIOS)
    assert H3Request().aspect_ratio is None
    assert H3Request().megapixels is None
    assert H3Request().multiple is None
    # The size fields default to unset, which is what lets an omission select the
    # Director's frame rather than looking like a caller who asked for 1344x768.
    assert H3Request().width is None
    assert H3Request().height is None
# --------------------------------------------------------------------------------------------
# Shot mode and asset roles.
# --------------------------------------------------------------------------------------------

# The two payloads this application built at commit f281606, digested. These are the load-bearing
# numbers of the whole shot-mode change: it touched the branch that decides what every render *is*,
# and the only convincing evidence that it is safe is that the same Shot still produces the same
# bytes. They were captured from `git show HEAD:` before a line of it was written.
#
# Taken from **f281606 and not from anything older**, deliberately. That commit windowed the song
# audio a reference shot is conditioned on, so a Shot with `use_song_audio` is knowingly no longer
# byte-identical to a commit before it. Baselining further back would have frozen the bug this
# project had just fixed and reported the fix as the regression.
#
# Re-pinned 2026-08-19 for the over-render margin — a renegotiation, not a drift: the
# Director ruled every take renders at least half a second longer than its window, which
# moves `length`/`duration_frames` and (for song-audio shots) the audio trim in every H3
# payload at once. The two shapes' *structure* is unchanged; only those literals moved,
# verified by eye on the diff before re-pinning.
H3_REFERENCE_PAYLOAD_DIGEST = "b59a93ddcc4f8cbf3e51504d47581a70fd3e95f2c758b34c1ff6c384e9fe7c60"
H3_TEXT_PAYLOAD_DIGEST = "d7d657a7b20c6d85b3895e23a8bd65304ef3fe014d8376ccff8d03a8c56dac8d"

DIGEST_PROJECT_ID = "project_deadbeef0001"
DIGEST_SHOT_REFERENCE = "shot_deadbeef0002"
DIGEST_SHOT_TEXT = "shot_deadbeef0003"
DIGEST_ASSET_LEAD = "asset_deadbeef0004"
DIGEST_ASSET_PAN = "asset_deadbeef0005"


def payload_digest(payload: dict, root: Path) -> str:
    """One H3 payload as a stable SHA-256, with the machine's temporary directory taken out.

    Every other input is pinned by the fixture below — ids, seeds, the song's duration — so the
    only thing that varies between two runs is where pytest put `tmp_path`. That path is embedded
    in the payload twice over: once as a plain string and once more inside `media_state`, which is
    itself a JSON string, so the separators come back doubly escaped. All four spellings are
    normalised, longest first, and getting that wrong is not hypothetical: it produced a digest
    that changed on every run and looked exactly like a regression.
    """
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    base = str(root.resolve())
    forms = {
        base,
        base.replace("\\", "/"),
        base.replace("\\", "\\\\"),
        base.replace("\\", "\\\\\\\\"),
    }
    for form in sorted(forms, key=len, reverse=True):
        text = text.replace(form, "<root>")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_project(tmp_path: Path):
    """The two Shot shapes that existed before modes were declarable, with everything pinned.

    Both Shots are written the way a manifest saved before this change writes them: a flat
    `asset_ids` list, no `citations`, no `mode`. That is the point — the digests must be produced
    by Shots that made no declaration, because those are the Shots this change promised not to move.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(id=DIGEST_PROJECT_ID, name="Digest"))
    media = store.media_dir(DIGEST_PROJECT_ID)
    (media / "lead.png").write_bytes(b"lead-png")
    (media / "pan.mp4").write_bytes(b"pan-mp4")
    (media / "duet.flac").write_bytes(b"fLaCfake")
    project.assets = [
        Asset(id=DIGEST_ASSET_LEAD, name="Lead vocalist", kind="character", path="media/lead.png"),
        Asset(id=DIGEST_ASSET_PAN, name="Camera pan", kind="video", path="media/pan.mp4"),
    ]
    project.song = Song(title="Duet", source="imported", path="media/duet.flac", duration=30.0)
    project.shots = [
        Shot(
            id=DIGEST_SHOT_REFERENCE,
            start=4.0,
            duration=5.0,
            prompt="The two vocalists perform the chorus together.",
            asset_ids=[DIGEST_ASSET_LEAD, DIGEST_ASSET_PAN],
            reference_labels={DIGEST_ASSET_LEAD: "lead vocalist"},
            use_song_audio=True,
            seed=1234,
            status="ready",
        ),
        Shot(
            id=DIGEST_SHOT_TEXT,
            start=12.0,
            duration=6.0,
            prompt="A grey wolf walks through a wet forest.",
            seed=5678,
            status="ready",
        ),
    ]
    store.save(project)
    return client, store, comfy


def test_both_existing_shot_shapes_still_build_the_byte_identical_payload(tmp_path: Path):
    """The one test this whole change had to earn: the same Shot, the same bytes.

    `generate_h3` used to decide what a render *was* by asking whether `asset_ids` happened to be
    non-empty. That branch is now `resolve_shot_mode`, and every behavioural assertion in this file
    would still pass if the reference path had quietly started sending its media in a different
    order, numbering its tags differently, or resolving one file by another route. A digest cannot.

    Both shapes, because the change had two ways to go wrong and they are not the same way: the
    reference branch moved from iterating `asset_ids` to iterating the reference-role citations, and
    the text branch is reached through a new gate that could refuse it or route past it.
    """
    client, _, comfy = digest_project(tmp_path)

    for shot_id in (DIGEST_SHOT_REFERENCE, DIGEST_SHOT_TEXT):
        assert submit_h3(client, DIGEST_PROJECT_ID, shot_id).status_code == 202

    assert payload_digest(comfy.prompts[0], tmp_path) == H3_REFERENCE_PAYLOAD_DIGEST
    assert payload_digest(comfy.prompts[1], tmp_path) == H3_TEXT_PAYLOAD_DIGEST


def test_migrating_a_shot_to_citations_does_not_move_the_payload_either(tmp_path: Path):
    """The same two digests from Shots whose citations were written out in full.

    The migration has two ends: a manifest that carries only `asset_ids` and a client that has since
    saved the same Shot back with its `citations` populated. Both must render identically, or the
    first save a Director makes after this change silently re-renders every shot differently.
    """
    client, store, comfy = digest_project(tmp_path)
    project = store.get(DIGEST_PROJECT_ID)
    # Round-tripped through the wire, which is how the migration actually reaches the manifest.
    body = json.loads(project.model_dump_json())["shots"]
    assert [citation["role"] for citation in body[0]["citations"]] == ["reference", "reference"]
    assert client.put(f"/api/projects/{DIGEST_PROJECT_ID}/shots", json={"shots": body}).status_code == 200

    for shot_id in (DIGEST_SHOT_REFERENCE, DIGEST_SHOT_TEXT):
        assert submit_h3(client, DIGEST_PROJECT_ID, shot_id).status_code == 202

    assert payload_digest(comfy.prompts[0], tmp_path) == H3_REFERENCE_PAYLOAD_DIGEST
    assert payload_digest(comfy.prompts[1], tmp_path) == H3_TEXT_PAYLOAD_DIGEST


def test_a_legacy_mode_string_is_not_a_declaration(tmp_path: Path):
    """`"text"`, `"image"` and `"reference"` load as "nobody declared anything".

    The inspector wrote one of these onto every Shot it ever created and **nothing read it**, so
    the stored value records a dropdown position rather than a decision. Reading it as a
    declaration now would change what an existing Shot renders — a Shot saying `"text"` while
    carrying an Asset would stop being a reference shot — which is the one thing this change was
    forbidden to do. They are resolved by behaviour instead, which is what they already meant.
    """
    for legacy in sorted(LEGACY_SHOT_MODES):
        shot = Shot(start=0, duration=5, mode=legacy)
        assert shot.mode is None, legacy

    # And the new vocabulary shares no spelling with the old, which is what keeps the two
    # distinguishable forever rather than only until someone re-uses a name.
    assert not LEGACY_SHOT_MODES & set(get_args(ShotMode))

    # A declared mode survives the same construction untouched.
    assert Shot(start=0, duration=5, mode="first_middle_last").mode == "first_middle_last"


def test_an_undeclared_shot_resolves_to_the_mode_it_already_behaves_as():
    """The migration, as the pure function the route asks. Both matrix rows for existing Shots."""
    with_assets = Shot(start=0, duration=5, asset_ids=["asset_lead"])
    song_only = Shot(start=0, duration=5, use_song_audio=True)
    bare = Shot(start=0, duration=5)

    assert resolve_shot_mode(with_assets) == "references"
    assert resolve_shot_mode(song_only) == "references"
    assert resolve_shot_mode(bare) == "text_to_video"

    # A declaration wins over the attachments, in both directions. That is what declaring is for.
    assert resolve_shot_mode(Shot(start=0, duration=5, asset_ids=["a"], mode="text_to_video")) == "text_to_video"
    assert resolve_shot_mode(Shot(start=0, duration=5, mode="extend")) == "extend"


def test_a_shot_cites_assets_and_never_copies_them():
    """One Asset, two Shots, two roles — and the Asset untouched by either.

    The Director's plan reuses the same wolf, location or character across many shots, so a Shot
    that copied an Asset would make the plan unrevisable. The role is therefore on the citation:
    the wolf is a middle frame *in this shot* and a plain reference in another.
    """
    wolf = Asset(id="asset_wolf", name="Grey wolf", kind="prop", path="media/wolf.png")
    before = wolf.model_dump()
    middle = Shot(
        start=0, duration=5, mode="first_middle_last",
        citations=[AssetCitation(asset_id="asset_wolf", role="middle")],
    )
    reference = Shot(
        start=5, duration=5, mode="references",
        citations=[AssetCitation(asset_id="asset_wolf", role="reference")],
    )

    assert citations_in_role(middle, "middle")[0].asset_id == "asset_wolf"
    assert citations_in_role(reference, "reference")[0].asset_id == "asset_wolf"
    assert citations_in_role(middle, "reference") == []
    # Nothing about the Asset knows either role, which is the whole design: a role on the Asset
    # would force a duplicate of the wolf per part it plays.
    assert wolf.model_dump() == before
    assert not hasattr(wolf, "role")


def test_ordering_within_a_role_is_preserved():
    """FR-19's determinism, surviving a list that now holds more than one role.

    The sort is stable and keyed on `order`, so citations that share an order — the default, and
    what every migrated Shot has — keep their list position rather than being reshuffled between
    two reads of the same manifest.
    """
    shot = Shot(
        start=0, duration=5,
        citations=[
            AssetCitation(asset_id="asset_c", role="reference"),
            AssetCitation(asset_id="asset_a", role="middle"),
            AssetCitation(asset_id="asset_b", role="reference"),
        ],
    )

    assert [item.asset_id for item in citations_in_role(shot, "reference")] == ["asset_c", "asset_b"]
    # `asset_ids` is the projection of exactly that, in exactly that order, which is what lets the
    # render path move onto citations without moving a byte.
    assert shot.asset_ids == ["asset_c", "asset_b"]

    # An explicit order overrides list position, and only within the role.
    reordered = Shot(
        start=0, duration=5,
        citations=[
            AssetCitation(asset_id="asset_c", role="reference", order=2),
            AssetCitation(asset_id="asset_b", role="reference", order=1),
        ],
    )
    assert reordered.asset_ids == ["asset_b", "asset_c"]


def test_citations_and_asset_ids_are_reconciled_in_both_directions():
    """One fact, stored twice, kept that way rather than allowed to drift.

    `citations` is the truth and `asset_ids` is its projection onto the reference role. The
    direction that matters most is the second one: a Shot whose wolf has been given the middle-frame
    role must stop claiming it as a reference attachment, or the render would go on sending it as
    reference picture three under a mode that says it is the middle frame.
    """
    migrated = Shot(start=0, duration=5, asset_ids=["asset_a", "asset_b"])
    assert [(item.asset_id, item.role, item.order) for item in migrated.citations] == [
        ("asset_a", "reference", 0), ("asset_b", "reference", 1)
    ]

    reroled = Shot(
        start=0, duration=5, mode="first_last",
        asset_ids=["asset_a", "asset_b"],
        citations=[
            AssetCitation(asset_id="asset_a", role="first"),
            AssetCitation(asset_id="asset_b", role="last"),
        ],
    )
    assert reroled.asset_ids == []


def test_a_shot_reports_what_its_mode_is_missing():
    """The matrix's "mode and assets disagree" row: named, not resolved.

    Every sentence is built from `SHOT_MODE_SPECS`, so a mode added to that table is described
    without a wording being written for it. Reported rather than repaired, because inventing which
    of two images is the middle one is exactly the guess a role exists to stop.
    """
    one_image = Shot(
        start=0, duration=5, mode="first_last",
        citations=[AssetCitation(asset_id="asset_a", role="first")],
    )
    problems = mode_specification_problems(one_image)

    assert len(problems) == 1
    assert "First / last frame needs 1 last frame, and this shot cites 0." == problems[0]

    # A role the mode does not have is a problem in the other direction, and so is asking for the
    # master song on a mode with no slot for one — which would otherwise be dropped in silence.
    stray = Shot(
        start=0, duration=5, mode="text_to_video",
        citations=[AssetCitation(asset_id="asset_a", role="reference")],
        use_song_audio=True,
    )
    assert mode_specification_problems(stray) == [
        "Text to video has no reference role, and this shot cites 1.",
        (
            "Text to video has no slot for the master song, so the audio reference this shot "
            "asks for would not be sent."
        ),
    ]

    # And nothing at all to say about the two shapes that already exist, which is what makes the
    # refusal in `generate_h3` unreachable for every Shot saved before this change.
    assert mode_specification_problems(Shot(start=0, duration=5, asset_ids=["a"], use_song_audio=True)) == []
    assert mode_specification_problems(Shot(start=0, duration=5)) == []


def test_a_deleted_asset_is_reported_and_never_silently_dropped(tmp_path: Path):
    """The matrix's deleted-asset row, at the model and at the route.

    Two halves, because a report nobody reaches is not a report: the pure function names the
    citation, and the route refuses the submission rather than rendering a shot short of what it
    cites. Silently rendering without it would spend a full GPU pass on something nobody asked for.
    """
    project = Project(name="Deleted")
    project.assets = [Asset(id="asset_kept", name="Kept", kind="image", path="media/kept.png")]
    shot = Shot(start=0, duration=5, asset_ids=["asset_kept", "asset_gone"])

    assert dangling_citations(project, shot) == ["asset_gone"]

    client, store, comfy = make_client(tmp_path)
    live = store.create(Project(name="Deleted live"))
    lead = upload_asset(client, live.id, "Lead", "character", "lead.png")
    shot_id = reference_shot(store, live.id, asset_ids=[lead["id"], "asset_gone"])

    refused = submit_h3(client, live.id, shot_id)

    assert refused.status_code == 422
    assert "asset_gone" in refused.json()["detail"]
    assert comfy.prompts == []


def test_a_mode_with_no_adapter_is_plannable_and_refused_at_render(tmp_path: Path):
    """The matrix's no-adapter row. Saved without complaint, refused before any GPU time.

    Plannable *and* unrenderable is the deliberate pair. A Director laying out a first/middle/last
    section before that adapter exists is doing real work, and a mode that vanished from the plan
    until its adapter landed would make that work impossible. What must never happen is the other
    failure — a mode that looks renderable and is not — which is what the refusal prevents.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Unbuilt"))
    lead = upload_asset(client, project.id, "Lead", "character", "lead.png")
    shot_id = reference_shot(
        store, project.id, mode="first_middle_last",
        citations=[
            AssetCitation(asset_id=lead["id"], role="first"),
            AssetCitation(asset_id=lead["id"], role="middle"),
            AssetCitation(asset_id=lead["id"], role="last"),
        ],
    )

    # It saved, it loaded, and it is still the mode the Director chose.
    assert store.get(project.id).shots[0].mode == "first_middle_last"

    refused = submit_h3(client, project.id, shot_id)

    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert "First / middle / last" in detail
    # It names what *does* render, so the refusal is actionable rather than only true — and it
    # names it from the table, so a mode that gains an adapter is described without an edit here.
    assert "References to video" in detail and "Text to video" in detail
    assert comfy.prompts == []


def test_a_declared_mode_that_does_not_fit_its_citations_is_refused_before_the_render(tmp_path: Path):
    """A declaration the attachments contradict costs nothing, because it never reaches ComfyUI.

    The alternative is what this branch used to do by omission: build the payload the attachments
    imply and log the render under a mode that was never applied. A GPU job recorded as one thing
    and rendered as another is invisible afterwards.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Disagreement"))
    lead = upload_asset(client, project.id, "Lead", "character", "lead.png")
    shot_id = reference_shot(store, project.id, asset_ids=[lead["id"]], mode="text_to_video")

    refused = submit_h3(client, project.id, shot_id)

    assert refused.status_code == 422
    assert "not fully specified" in refused.json()["detail"]
    assert "Text to video has no reference role" in refused.json()["detail"]
    assert comfy.prompts == []


def keyframe_shot(client, store, project_id: str, *, mode: str, roles: dict, **fields) -> tuple[str, dict]:
    """A ready keyframe Shot citing one uploaded image per role. Returns (shot_id, assets).

    The citations are deliberately listed in **reverse** role order — last before first —
    so any test rendering through this helper is also asserting that the route resolves
    frames by role and never by list position.
    """
    assets = {
        role: upload_asset(client, project_id, name, "image", f"{role}.png")
        for role, name in roles.items()
    }
    shot_id = reference_shot(
        store, project_id, mode=mode,
        citations=[
            AssetCitation(asset_id=assets[role]["id"], role=role)
            for role in reversed(list(roles))
        ],
        **fields,
    )
    return shot_id, assets


def test_a_first_last_shot_renders_through_the_keyframe_graph_with_frames_resolved_by_role(
    tmp_path: Path,
):
    """The Director's own flow, at the route: two cited frames reach ComfyUI through the
    keyframe graph, on the `fl2va` checkpoint, each in the role its citation names.

    The citations are stored last-before-first on purpose: a route that resolved
    positionally would render the shot backwards while looking exactly as correct.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Keyframes"))
    shot_id, _ = keyframe_shot(
        client, store, project.id, mode="first_last",
        roles={"first": "Opening frame", "last": "Closing frame"},
    )

    response = submit_h3(client, project.id, shot_id, width=640, height=384)

    assert response.status_code == 202
    payload = comfy.prompts[-1]
    assert payload["mvp:model"]["inputs"]["unet_name"] == (
        "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
    )
    condition = payload["mvp:condition"]["inputs"]
    assert payload["mvp:condition"]["class_type"] == "MiniMaxH3ImageToVideo"
    assert condition["first_frame"] == ["mvp:split", 0]
    assert condition["last_frame"] == ["mvp:split", 1]
    assert (condition["width"], condition["height"]) == (640, 384)
    media = json.loads(payload["mvp:frames"]["inputs"]["media_state"])
    # Entry 0 is the *first*-role file and entry 1 the *last*-role file, despite the
    # citation list holding them the other way round.
    assert [item["label"] for item in media] == ["first frame", "last frame"]
    assert media[0]["file"].endswith("first.png")
    assert media[1]["file"].endswith("last.png")
    assert payload["mvp:save"]["inputs"]["filename_prefix"].endswith(
        f"{shot_id}-h3-keyframe"
    )
    saved = store.get(project.id)
    assert saved.shots[0].status == "queued"
    assert saved.jobs[-1].kind == "h3"


def test_an_image_to_video_shot_renders_first_frame_only_with_last_frame_absent(
    tmp_path: Path,
):
    """The single-frame shape the schema permits: `last_frame` is not sent at all."""
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="I2V"))
    shot_id, _ = keyframe_shot(
        client, store, project.id, mode="image_to_video", roles={"first": "Opening frame"},
    )

    assert submit_h3(client, project.id, shot_id).status_code == 202

    payload = comfy.prompts[-1]
    condition = payload["mvp:condition"]["inputs"]
    assert condition["first_frame"] == ["mvp:split", 0]
    assert "last_frame" not in condition
    media = json.loads(payload["mvp:frames"]["inputs"]["media_state"])
    assert [item["label"] for item in media] == ["first frame"]
    # An omitted geometry takes the same measured default the reference path takes.
    assert (condition["width"], condition["height"]) == (1056, 608)


def test_a_keyframe_shot_missing_a_role_is_refused_before_any_payload(tmp_path: Path):
    """The matrix's missing-role row, in `mode_specification_problems`' own words."""
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Missing role"))
    shot_id, _ = keyframe_shot(
        client, store, project.id, mode="first_last", roles={"first": "Opening frame"},
    )

    refused = submit_h3(client, project.id, shot_id)

    assert refused.status_code == 422
    assert "First / last frame needs 1 last frame, and this shot cites 0." in (
        refused.json()["detail"]
    )
    assert comfy.prompts == []


def test_a_keyframe_citation_whose_file_is_gone_is_refused_with_the_path_named(
    tmp_path: Path,
):
    """The matrix's cited-file-gone row: 422, the path named, nothing submitted."""
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Gone frame"))
    shot_id, assets = keyframe_shot(
        client, store, project.id, mode="first_last",
        roles={"first": "Opening frame", "last": "Closing frame"},
    )
    saved = store.get(project.id)
    gone = next(item for item in saved.assets if item.id == assets["last"]["id"])
    (store.project_dir(project.id) / gone.path).unlink()

    refused = submit_h3(client, project.id, shot_id)

    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert "last frame" in detail
    assert gone.path in detail
    assert comfy.prompts == []


def test_a_keyframe_citation_that_is_not_an_image_or_not_an_asset_is_refused(
    tmp_path: Path,
):
    """A frame travels as a picture; an audio or video Asset cited as one is refused by
    name, and an id no Asset carries is refused the way the reference path refuses one."""
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Wrong kind"))
    song = upload_asset(client, project.id, "Room tone", "audio", "room.flac")
    shot_id = reference_shot(
        store, project.id, mode="image_to_video",
        citations=[AssetCitation(asset_id=song["id"], role="first")],
    )

    refused = submit_h3(client, project.id, shot_id)
    assert refused.status_code == 422
    assert "A first frame must be an image, and Room tone is an audio." in (
        refused.json()["detail"]
    )

    unknown = reference_shot(
        store, project.id, mode="image_to_video",
        citations=[AssetCitation(asset_id="asset_missing", role="first")],
    )
    refused = submit_h3(client, project.id, unknown)
    assert refused.status_code == 422
    assert "asset_missing" in refused.json()["detail"]
    assert comfy.prompts == []


def test_a_keyframe_expansion_is_submitted_alone(tmp_path: Path):
    """`reference_prompt`'s rule on the keyframe branch: an H3-format expansion goes out
    exactly as written, and a shot without one sends its intent exactly as written."""
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Expansion"))
    expansion = "Cut to <Picture 1> at 0s.\nDescription: the wolf turns.\n"
    shot_id, _ = keyframe_shot(
        client, store, project.id, mode="image_to_video",
        roles={"first": "Opening frame"}, h3_prompt=expansion,
    )

    assert submit_h3(client, project.id, shot_id).status_code == 202
    assert comfy.prompts[-1]["mvp:condition"]["inputs"]["prompt"] == expansion

    plain_id, _ = keyframe_shot(
        client, store, project.id, mode="image_to_video", roles={"first": "Opening frame"},
    )
    assert submit_h3(client, project.id, plain_id).status_code == 202
    assert comfy.prompts[-1]["mvp:condition"]["inputs"]["prompt"] == (
        "The vocalists perform the chorus together."
    )


def test_song_audio_on_a_keyframe_shot_is_refused_because_the_node_has_no_slot(
    tmp_path: Path,
):
    """The lip-sync answer at the route: `MiniMaxH3ImageToVideo` takes no reference audio,
    so `use_song_audio` on a keyframe shot is refused in the mode table's words rather than
    silently dropped — the shot cannot lip-sync to the master song and nothing pretends
    otherwise."""
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="No slot"))
    client.post(
        f"/api/projects/{project.id}/songs/upload",
        data={"title": "Master", "duration": "30"},
        files={"file": ("master.flac", b"fLaCfake", "audio/flac")},
    )
    shot_id, _ = keyframe_shot(
        client, store, project.id, mode="first_last",
        roles={"first": "Opening frame", "last": "Closing frame"},
        use_song_audio=True,
    )

    refused = submit_h3(client, project.id, shot_id)

    assert refused.status_code == 422
    assert "has no slot for the master song" in refused.json()["detail"]
    assert comfy.prompts == []


def test_a_profile_or_reference_size_on_a_keyframe_shot_is_refused_not_dropped(
    tmp_path: Path,
):
    """Both request fields that name reference-graph machinery are refused on this branch,
    for the text-only branch's reason: a job logged under a configuration that was never
    applied is worse than a refusal, because only the refusal is visible."""
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="No profile"))
    shot_id, _ = keyframe_shot(
        client, store, project.id, mode="first_last",
        roles={"first": "Opening frame", "last": "Closing frame"},
    )

    profiled = submit_h3(client, project.id, shot_id, profile="turbo")
    assert profiled.status_code == 422
    assert "no evidenced profile" in profiled.json()["detail"]

    sized = submit_h3(client, project.id, shot_id, ref_image_size="max")
    assert sized.status_code == 422
    assert "no such input" in sized.json()["detail"]
    assert comfy.prompts == []


def test_whether_the_performer_is_singing_is_expressible_and_nothing_infers_it(tmp_path: Path):
    """Three states, and `unknown` is not `not_singing`.

    The Director's constraint is per shot, not global: a performer laying on a bed has no lip-sync
    to protect and the LTX enhancer is pure gain on that shot, while a singing shot has lip position
    to lose — the enhancement measurably moves it. So this is a property of the performance, not of
    the mode, and it must be independently expressible from both.

    Nothing may infer it. A shot whose state was never set is `unknown`, which is not the same as
    "not singing", and a destructive default in either direction is worse than an honest absence.
    """
    assert set(get_args(SingingState)) == {"unknown", "singing", "not_singing"}
    assert Shot(start=0, duration=5).singing == "unknown"

    # Independent of the mode in both directions: a references shot may or may not be singing, and
    # so may a first/last one.
    for mode in ("references", "first_last", None):
        for singing in get_args(SingingState):
            shot = Shot(start=0, duration=5, mode=mode, singing=singing)
            assert shot.singing == singing, (mode, singing)

    # Nothing in the source infers it. Grepped rather than argued, because the failure mode is a
    # helpful default added later by someone who did not read this docstring.
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("src/music_video_producer").rglob("*.py")
    )
    for guess in ('singing = "singing"', 'singing = "not_singing"', 'singing="singing"'):
        assert guess not in source, guess
    # The one permitted write: populate maps the plan model's own `performance` field --
    # a dedicated strict-schema declaration the instruction explicitly asks for, reviewed
    # per shot in the inspector (run-2 audit, 2026-08-19). Pinned to its one blessed
    # spelling and site; the greps above still forbid every looser form.
    app_source = Path("src/music_video_producer/app.py").read_text(encoding="utf-8")
    assert app_source.count('declared_singing: SingingState = "singing" if performing else "not_singing"') == 1

    # And it is durable: it survives the wire, the manifest and a reload.
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Performance"))
    shot = Shot(start=0, duration=5, prompt="A singer turns", singing="not_singing")
    project.shots = [shot]
    store.save(project)
    assert store.get(project.id).shots[0].singing == "not_singing"
    reloaded = client.get(f"/api/projects/{project.id}").json()["shots"][0]
    assert reloaded["singing"] == "not_singing"


def test_modes_roles_and_singing_survive_every_write_path_that_takes_a_shot(tmp_path: Path):
    """The round-trip row, over **both** sibling write paths rather than only the obvious one.

    `PUT /shots` is the one the workspace uses; `PUT /projects/{id}` takes a whole client-supplied
    `Project` whose every field is defaulted, and it is twice now been the guard hole left open —
    a whole-manifest save writes every defaulted field, including the ones a client that predates
    them omits. Both are asserted here so a new Shot field cannot survive one and be erased by the
    other.
    """
    client, store, _ = make_client(tmp_path)
    project = store.create(Project(name="Round trip"))
    project.shots = [
        Shot(
            id="shot_roundtrip", start=0, duration=5, prompt="A wolf crosses the clearing",
            mode="first_middle_last", singing="singing",
            citations=[
                AssetCitation(asset_id="asset_a", role="first", order=0),
                AssetCitation(asset_id="asset_b", role="middle", order=0),
                AssetCitation(asset_id="asset_c", role="last", order=0),
            ],
        )
    ]
    store.save(project)

    def assert_intact(shot: Shot) -> None:
        assert shot.mode == "first_middle_last"
        assert shot.singing == "singing"
        assert [(item.asset_id, item.role) for item in shot.citations] == [
            ("asset_a", "first"), ("asset_b", "middle"), ("asset_c", "last")
        ]

    assert_intact(store.get(project.id).shots[0])

    body = client.get(f"/api/projects/{project.id}").json()
    assert client.put(f"/api/projects/{project.id}/shots", json={"shots": body["shots"]}).status_code == 200
    assert_intact(store.get(project.id).shots[0])

    whole = client.get(f"/api/projects/{project.id}").json()
    assert client.put(f"/api/projects/{project.id}", json=whole).status_code == 200
    assert_intact(store.get(project.id).shots[0])


def test_a_manifest_written_before_modes_existed_loads_without_being_rewritten(tmp_path: Path):
    """Migration by resolution, not by rewriting files nobody touched.

    The manifest on disk is the one this project wrote yesterday, byte for byte, until something
    saves it. Reading it produces the mode it already behaved as; the file is unchanged.
    """
    _, store, _ = make_client(tmp_path)
    project = store.create(Project(id="project_legacyaaaa01", name="Legacy"))
    manifest = store.manifest_path(project.id)
    manifest.write_text(
        json.dumps(
            {
                "id": "project_legacyaaaa01",
                "name": "Legacy",
                "shots": [
                    {"id": "shot_old", "start": 0, "duration": 5, "prompt": "A corridor",
                     "mode": "text", "asset_ids": ["asset_lead"], "seed": 0, "status": "draft"}
                ],
            }
        ),
        encoding="utf-8",
    )
    before = manifest.read_bytes()

    shot = store.get(project.id).shots[0]

    assert shot.mode is None
    assert resolve_shot_mode(shot) == "references"
    assert shot.asset_ids == ["asset_lead"]
    assert [(item.asset_id, item.role) for item in shot.citations] == [("asset_lead", "reference")]
    assert shot.singing == "unknown"
    # Nothing wrote. A migration that rewrote every manifest on load would touch files whose
    # contents nobody changed, and would do it before anyone had asked for anything.
    assert manifest.read_bytes() == before


def test_a_new_shot_field_cannot_be_added_without_deciding_what_the_director_sees():
    """The guard `Song` had, extended to `Shot` — and the answer to whether it was there already.

    It was not. Only `Song` was classified, so every field ever added to `Shot` entered the
    Director's prompt the moment it was declared, with nobody deciding that it should. This change
    added three at once, which is exactly the situation the guard exists for.

    Seven fields are withheld, none of them a removal — each was classified withheld at the
    moment it was declared, so withholding adds nothing to the prompt rather than subtracting
    something from it. The over-render pair (`latest_take_lead`/`trim_nudge`) is render
    bookkeeping and the human's own editorial fine-tune, neither a plan fact a chat turn
    writes or reads. `h3_prompt` on the numbers: a thirty-shot plan of H3-format expansions
    would add many thousands of tokens to *every* chat turn, and rich context is this project's
    recorded cause of Director degradation. The AD-13 window snapshot pair
    (`approved_start`/`approved_duration`) as staleness bookkeeping: copies of `start`/`duration`
    taken at approval for assembly's refusal, near-duplicate numbers the chat Director — who
    already sees the live window and `approved_output` — has no decision to make from.

    What the classification buys either way is that the *next* field cannot arrive without the
    decision being made.
    """

    class ShotWithANewField(Shot):
        director_notes_previous: str = ""

    with pytest.raises(RuntimeError) as unclassified:
        _withheld_fields(
            ShotWithANewField,
            visible=SHOT_DIRECTOR_VISIBLE,
            withheld=SHOT_DIRECTOR_WITHHELD,
            family="SHOT",
        )

    assert "director_notes_previous" in str(unclassified.value)
    # It names the Shot pair and not the Song pair, which is the whole reason `family` is passed
    # rather than derived: a message sending the next writer to the wrong constant is worse than
    # no message, because they will edit the wrong one and the guard will still be green.
    assert "SHOT_DIRECTOR_WITHHELD" in str(unclassified.value)
    assert "SONG_DIRECTOR_WITHHELD" not in str(unclassified.value)

    # The live classification is complete right now — which is what makes importing `app` succeed
    # at all — and every field is on exactly one side.
    assert _withheld_fields(
        Shot, visible=SHOT_DIRECTOR_VISIBLE, withheld=SHOT_DIRECTOR_WITHHELD, family="SHOT"
    ) == {
        "h3_prompt",
        "approved_start",
        "approved_duration",
        "latest_take_lead",
        "trim_nudge",
        "mix_take_audio",
        "flagged",
    }
    assert not SHOT_DIRECTOR_VISIBLE & SHOT_DIRECTOR_WITHHELD
    assert {"mode", "citations", "singing", "prompt"} <= SHOT_DIRECTOR_VISIBLE

    # The withheld set, and the exclusion it produces. The key exists *because* something is
    # withheld — it is derived from the classification rather than written by hand, so a field
    # classified withheld cannot fail to be excluded, and a field classified visible cannot be
    # excluded by a stale path someone forgot to update.
    assert SHOT_DIRECTOR_WITHHELD == {
        "h3_prompt",
        "approved_start",
        "approved_duration",
        "latest_take_lead",
        "trim_nudge",
        "mix_take_audio",
        "flagged",
    }
    assert DIRECTOR_CONTEXT_EXCLUDE["shots"] == {
        "__all__": {
            "h3_prompt",
            "approved_start",
            "approved_duration",
            "latest_take_lead",
            "trim_nudge",
            "mix_take_audio",
            "flagged",
        }
    }

    # And the intent is still shown. Withholding the expansion is only defensible because the thing
    # it was expanded *from* still reaches the Director: a chat turn can still see what each shot is
    # meant to be, in the short readable form, which is the form a conversation can work with.
    assert "prompt" not in SHOT_DIRECTOR_WITHHELD


def test_an_h3_expansion_never_reaches_the_directors_context(tmp_path):
    """The classification says withheld; this proves the dump honours it.

    Asserted against what the model was actually handed rather than against the exclusion mapping,
    because the mapping being right and the dump being right are two different claims. A key
    present but empty, or a nested path that stopped matching after a rename, would both satisfy
    the classification test above and still ship the expansion into every chat turn.

    The expansion text is searched for in the *serialised* context, not just checked key by key:
    the point is that this text is nowhere in what gets encoded into the prompt.
    """
    director = RevisingDirector()
    client, store = make_client_with_director(tmp_path, director)
    project = store.create(Project(name="Expansions stay out"))

    expansion = (
        "integrated_multimodal_description: [Shot 1] A grey wolf paces through birch trunks.\n"
        "overall_soundscape: Wind moves through branches over dry needles underfoot.\n"
        "non_diegetic_music: N/A"
    )
    shot = Shot(start=0.0, duration=4.0, prompt="Wolf B-roll", h3_prompt=expansion)
    client.put(
        f"/api/projects/{project.id}/shots",
        json={"shots": [json.loads(shot.model_dump_json())]},
    )
    assert store.get(project.id).shots[0].h3_prompt == expansion

    client.post(f"/api/projects/{project.id}/director/chat", json={"message": "What is this?"})

    context = director.contexts[0]
    serialised = json.dumps(context)
    assert "h3_prompt" not in context["shots"][0]
    assert "integrated_multimodal_description" not in serialised
    assert "paces through birch trunks" not in serialised
    # The intent is still there, which is the only reason withholding the expansion is defensible.
    assert context["shots"][0]["prompt"] == "Wolf B-roll"


def test_every_mode_that_claims_an_adapter_has_a_branch_that_builds_it():
    """The silent hole: a table entry the route accepts and then renders as something else.

    `generate_h3` picks the reference branch on one adapter name and falls through to the text-only
    graph otherwise. A mode given a *third* adapter name — the next mode to be built — would pass
    the "can this render" gate and then render as text-to-video, logged as though its own adapter
    had run. That failure has no symptom at the point it happens; the application refusing to start
    does.
    """
    assert H3_ADAPTERS == {"h3-director", "h3-reference", "h3-keyframe"}
    for mode, spec in SHOT_MODE_SPECS.items():
        assert not spec.adapter or spec.adapter in H3_ADAPTERS, mode

    # The three the route actually has, named by the modes that use them, so a rename of any is
    # caught here rather than in a live render. Both keyframe modes share one adapter over
    # `MiniMaxH3ImageToVideo` — the Director's ruling routes `image_to_video` through H3, and
    # the LTX I2V evidence stays imported as the alternative path's evidence.
    assert SHOT_MODE_SPECS["references"].adapter == "h3-reference"
    assert SHOT_MODE_SPECS["text_to_video"].adapter == "h3-director"
    assert SHOT_MODE_SPECS["image_to_video"].adapter == "h3-keyframe"
    assert SHOT_MODE_SPECS["first_last"].adapter == "h3-keyframe"
    # Everything else is plannable and unrenderable, which is the state this story leaves them in.
    assert {mode for mode, spec in SHOT_MODE_SPECS.items() if not spec.adapter} == {
        "first_middle_last", "extend"
    }


def test_the_mode_table_covers_every_declared_mode_and_role():
    """No mode without a spec, and no role without a label. A table entry is the unit of extension."""
    assert set(SHOT_MODE_SPECS) == set(get_args(ShotMode))
    assert set(ASSET_ROLE_LABELS) == set(get_args(AssetRole))
    for mode, spec in SHOT_MODE_SPECS.items():
        assert spec.label, mode
        for requirement in spec.roles:
            assert requirement.role in ASSET_ROLE_LABELS, mode
            assert 0 <= requirement.minimum <= requirement.maximum, mode
    # Only the reference graph has a slot for the master song, which is what makes asking for one
    # anywhere else a refusal rather than a silent drop.
    assert {mode for mode, spec in SHOT_MODE_SPECS.items() if spec.song_audio} == {"references"}
    # The references mode is the only one that can be fully specified with nothing cited, which is
    # what makes every Shot that exists today unable to trip the specification refusal.
    assert SHOT_MODE_SPECS["references"].roles[0].minimum == 0


def test_declaring_references_on_an_empty_shot_routes_to_the_reference_graph(tmp_path: Path):
    """The one shape where the declaration and the old inference genuinely disagree.

    A Shot that cites nothing and does not use the song is exactly what the old condition called
    text-to-video, and it is what a Director gets the moment they pick "References to video" on a
    new shot and have not attached anything yet. Under the declaration it is a reference shot, and
    the reference adapter refuses it in its own words rather than quietly rendering a text-only
    take under a mode that says otherwise.

    Asserted through the refusal because there is no payload to inspect — which is the point: the
    old condition would have produced one, from the wrong graph.

    Nothing that exists today can reach this. `references` is only *declarable*, never inferred for
    an empty Shot, so no manifest written before this change can be in this state.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Empty references"))
    shot_id = reference_shot(store, project.id, mode="references")

    # It is fully specified for its mode — the reference role's minimum is zero, because a
    # song-only shot is a real and valid references shot.
    assert mode_specification_problems(store.get(project.id).shots[0]) == []

    refused = submit_h3(client, project.id, shot_id)

    assert refused.status_code == 422
    assert refused.json()["detail"] == "At least one H3 reference is required"
    assert comfy.prompts == []


# --- Keyframes riding the references mode ---------------------------------------------------
#
# MiniMax's guide §2.2.2 uses a reference picture *as* a shot's first frame, keyframe or last
# frame, declared in the structured prompt, on the very node that takes the windowed master song.
# The picture rides as an ordinary reference slot; only the prompt knows its role. These are the
# spec's matrix rows for that shape — the byte-identity rows live above, in the digest tests,
# which is what makes every pre-existing shot's payload the same bytes it always was.


def test_a_references_shot_declares_its_first_frame_in_the_guides_wording(tmp_path: Path):
    """References + first frame + the master song: the combination that is the point.

    The picture travels as a plain reference picture — same kind, same slot family, nothing new
    in the graph — and the map's line for it is the guide's own sentence shape, with the
    `fully_preserved` retention marker riding beside the shot anchor. The audio is windowed
    exactly as today.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Pinned opening"))
    portrait = upload_asset(client, project.id, "Lead portrait", "character", "lead.png")
    stage = upload_asset(client, project.id, "Stage", "setting", "stage.png")
    client.post(
        f"/api/projects/{project.id}/songs/upload",
        data={"title": "Harder Faster", "duration": "30"},
        files={"file": ("song.flac", b"fLaCfake", "audio/flac")},
    )
    shot_id = reference_shot(
        store,
        project.id,
        mode="references",
        citations=[
            AssetCitation(asset_id=stage["id"], role="reference"),
            AssetCitation(asset_id=portrait["id"], role="first"),
        ],
        reference_labels={portrait["id"]: "the singer mid-breath"},
        use_song_audio=True,
    )

    assert submit_h3(client, project.id, shot_id).status_code == 202

    payload = comfy.prompts[-1]
    prompt = payload["mvp:condition"]["inputs"]["prompt"]
    # The first-frame picture numbers first — `citations_in_prompt_order` puts keyframe roles
    # ahead of references — and its line is the guide's, not the plain label line.
    assert prompt.startswith(
        "Reference map: <Picture 1> is the first frame of [Shot 1] (fully_preserved), "
        "showing the singer mid-breath; <Picture 2> is Stage; "
        "<Audio 1> is the master song for synchronization."
    )
    # The payload's media order is the same walk, so <Picture 1> really is the portrait.
    media = json.loads(payload["mvp:references"]["inputs"]["media_state"])
    assert [item["kind"] for item in media] == ["picture", "picture", "audio"]
    assert media[0]["file"].endswith("lead.png")
    assert media[1]["file"].endswith("stage.png")
    # And the song is windowed to the shot exactly as a plain references shot's is — the
    # over-rendered span: at 0 s the lead has nowhere to go, so the 141-frame picture
    # (5 s + margin, grid-snapped) is all tail.
    assert media[2]["trim"] == {"start": 0.0, "end": 141 / 24}


def test_a_references_shot_declares_both_keyframes_and_ties_the_last_to_the_final_shot(
    tmp_path: Path,
):
    """First and last both riding: `[Shot 1]` for the first, the final shot for the last.

    An un-expanded intent declares no shot numbers, so the last frame is tied to "the final
    shot" — the guide's own alignment language ("the last frame must be reached by the final
    [Shot N]") — rather than to an index this route would have to invent.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Both ends"))
    opening = upload_asset(client, project.id, "Opening pose", "character", "open.png")
    closing = upload_asset(client, project.id, "Closing pose", "character", "close.png")
    shot_id = reference_shot(
        store,
        project.id,
        mode="references",
        citations=[
            AssetCitation(asset_id=closing["id"], role="last"),
            AssetCitation(asset_id=opening["id"], role="first"),
        ],
    )

    assert submit_h3(client, project.id, shot_id).status_code == 202

    prompt = comfy.prompts[-1]["mvp:condition"]["inputs"]["prompt"]
    assert (
        "<Picture 1> is the first frame of [Shot 1] (fully_preserved), "
        "showing Opening pose" in prompt
    )
    assert (
        "<Picture 2> is the last frame of the final shot (fully_preserved), "
        "showing Closing pose" in prompt
    )
    media = json.loads(comfy.prompts[-1]["mvp:references"]["inputs"]["media_state"])
    assert media[0]["file"].endswith("open.png")
    assert media[1]["file"].endswith("close.png")


def test_mixed_role_numbering_is_one_numbering_across_prompt_payload_and_expansion(
    tmp_path: Path,
):
    """The off-by-one that would render plausibly and wrongly, pinned from three sides.

    H3's media slots are anonymous: the prompt's `<Picture N>` *is* the Nth picture slot, so a
    declaration numbered under any other walk than the payload's would pin somebody else's
    picture as the first frame — and nothing downstream would report it. The citation list here
    is adversarial on purpose: list order disagrees with role order, and an explicit `order`
    disagrees with list position within the reference role, so a route that numbered by list
    position, or an expansion input that sorted by a different key, each produce a different
    (wrong) numbering and fail.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="One numbering"))
    wolf = upload_asset(client, project.id, "Grey wolf", "character", "wolf.png")
    stage = upload_asset(client, project.id, "Stage", "setting", "stage.png")
    portrait = upload_asset(client, project.id, "Lead portrait", "character", "lead.png")
    # The first-frame citation carries the *largest* order on purpose: under the shared
    # `(role, order)` key it still numbers first, while a walk keyed `(order, role)`, or by
    # list position, each put a reference picture into slot one — so either drift fails here
    # instead of coinciding with the right answer.
    citations = [
        AssetCitation(asset_id=stage["id"], role="reference", order=1),
        AssetCitation(asset_id=portrait["id"], role="first", order=7),
        AssetCitation(asset_id=wolf["id"], role="reference", order=0),
    ]
    shot_id = reference_shot(
        store, project.id, mode="references", citations=citations
    )

    assert submit_h3(client, project.id, shot_id).status_code == 202

    payload = comfy.prompts[-1]
    prompt = payload["mvp:condition"]["inputs"]["prompt"]
    media = json.loads(payload["mvp:references"]["inputs"]["media_state"])
    # The one walk: first frame, then references by their own order. The payload's picture
    # slots fill in list order, so slot N holds exactly the asset the map's <Picture N> names.
    assert prompt.startswith(
        "Reference map: <Picture 1> is the first frame of [Shot 1] (fully_preserved), "
        "showing Lead portrait; <Picture 2> is Grey wolf; <Picture 3> is Stage."
    )
    # `strict=True` pins the count as well as the order; uploads carry a numbered prefix, so
    # the name is matched by suffix.
    for item, name in zip(media, ["lead.png", "wolf.png", "stage.png"], strict=True):
        assert item["file"].endswith(name), (item["file"], name)

    # And the expansion specialist is handed the same numbering from the same function: the
    # tag it is told to declare a role for is the tag the payload's media order implies.
    shot = store.get(project.id).shots[0]
    handed = shot_expansion_input(store.get(project.id), shot)["shot"]["references"]
    assert [(entry["tag"], entry["role"]) for entry in handed] == [
        ("<Picture 1>", "first frame"),
        ("<Picture 2>", "reference"),
        ("<Picture 3>", "reference"),
    ]
    assert [entry["asset_id"] for entry in handed] == [
        citation.asset_id for citation in citations_in_prompt_order(shot)
    ]
    assert [entry["asset_id"] for entry in handed] == [
        portrait["id"], wolf["id"], stage["id"]
    ]


def test_a_references_shot_citing_only_a_first_frame_renders(tmp_path: Path):
    """The matrix's "keyframe role, no reference role" row: the mode's minimums are all zero."""
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="First only"))
    portrait = upload_asset(client, project.id, "Lead portrait", "character", "lead.png")
    shot_id = reference_shot(
        store,
        project.id,
        mode="references",
        citations=[AssetCitation(asset_id=portrait["id"], role="first")],
    )

    assert mode_specification_problems(store.get(project.id).shots[0]) == []
    assert submit_h3(client, project.id, shot_id).status_code == 202
    assert comfy.prompts[-1]["mvp:condition"]["inputs"]["prompt"].startswith(
        "Reference map: <Picture 1> is the first frame of [Shot 1] (fully_preserved), "
        "showing Lead portrait."
    )


def test_a_keyframe_role_on_a_references_shot_must_be_an_image(tmp_path: Path):
    """A frame is a picture: the same refusal the dedicated keyframe branch makes.

    The splitter routes media by kind, so an audio cited as the first frame would be fed to
    the picture loader under a kind it is not — and nothing downstream reports that.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Not a frame"))
    room = upload_asset(client, project.id, "Room tone", "audio", "room.flac")
    shot_id = reference_shot(
        store,
        project.id,
        mode="references",
        citations=[AssetCitation(asset_id=room["id"], role="first")],
    )

    refused = submit_h3(client, project.id, shot_id)

    assert refused.status_code == 422
    assert refused.json()["detail"] == (
        "A first frame must be an image, and Room tone is an audio."
    )
    assert comfy.prompts == []


def test_a_keyframe_picture_counts_against_the_nine_picture_ceiling(tmp_path: Path):
    """The node's arity is the node's arity: the keyframe picture is an ordinary slot.

    Nine reference pictures plus a first frame is ten pictures, and the adapter's per-kind
    limit — the autogrow maximum the pre-flight holds to the live schema — refuses it before
    ComfyUI ever sees it.
    """
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Ceiling"))
    uploads = [
        upload_asset(client, project.id, f"Still {index}", "setting", f"still-{index}.png")
        for index in range(9)
    ]
    portrait = upload_asset(client, project.id, "Lead portrait", "character", "lead.png")
    shot_id = reference_shot(
        store,
        project.id,
        mode="references",
        citations=[
            *[
                AssetCitation(asset_id=item["id"], role="reference", order=index)
                for index, item in enumerate(uploads)
            ],
            AssetCitation(asset_id=portrait["id"], role="first"),
        ],
    )

    refused = submit_h3(client, project.id, shot_id)

    assert refused.status_code == 422
    assert "picture" in refused.json()["detail"]
    assert comfy.prompts == []


def test_the_references_mode_fit_report_admits_keyframe_roles_and_bounds_them():
    """The matrix's mode-fit row: first/last no longer flagged, still at most one of each."""
    fitting = Shot(
        start=0, duration=5, mode="references",
        citations=[
            AssetCitation(asset_id="a", role="reference"),
            AssetCitation(asset_id="b", role="first"),
            AssetCitation(asset_id="c", role="last"),
        ],
    )
    assert mode_specification_problems(fitting) == []

    two_firsts = Shot(
        start=0, duration=5, mode="references",
        citations=[
            AssetCitation(asset_id="a", role="first"),
            AssetCitation(asset_id="b", role="first"),
        ],
    )
    assert mode_specification_problems(two_firsts) == [
        "References to video takes at most 1 first frame, and this shot cites 2."
    ]

    # `middle` stays undeclared — the guide's keyframe vocabulary covers it, but no evidence
    # graph demonstrates it on H3, and the spec marks it Ask First.
    middled = Shot(
        start=0, duration=5, mode="references",
        citations=[AssetCitation(asset_id="a", role="middle")],
    )
    assert mode_specification_problems(middled) == [
        "References to video has no middle frame role, and this shot cites 1."
    ]


# --- Song-audio restoration ----------------------------------------------------------------
#
# The route's own tests. Every row of the spec's I/O matrix that a route can answer is here; the
# ones it cannot are elsewhere by design — the model-dependency row is the pre-flight's
# (`tests/preflight_audio_replace.py`), the window row's arithmetic is
# `tests/test_workflows.py`, and "frame count is measured, never asserted equal to the input" is
# a live `ffprobe` reading of the two files.

#: The 12–15.75 s window of a 154 s master, which is the worked example the spec states.
RESTORE_SONG_DURATION = 154.644898
RESTORE_SHOT_START = 12.0
RESTORE_SHOT_DURATION = 3.75


def restorable_project(
    store,
    tmp_path: Path,
    *,
    take: str = "takes/shot-h3-reference_00001-audio.mp4",
    song: str | None = "master.mp3",
    song_duration: float = RESTORE_SONG_DURATION,
    **shot,
):
    """A project whose one Shot rode the master song and has a take on disk.

    `song=None` leaves the project songless; a `song` naming a file that is never written
    leaves the manifest pointing at nothing, which is the other half of the song refusals.
    """
    project = store.create(Project(name="Restore"))
    if take:
        output = tmp_path / "comfy" / "output" / Path(take)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"rendered-take-with-generated-audio")
    if song:
        media = tmp_path / "projects" / project.id / "media" / "songs"
        media.mkdir(parents=True, exist_ok=True)
        if song != "missing.mp3":
            (media / song).write_bytes(b"master-song")
        project.song = Song(
            title="Master",
            source="imported",
            path=f"media/songs/{song}",
            duration=song_duration,
        )
    fields = {
        "start": RESTORE_SHOT_START,
        "duration": RESTORE_SHOT_DURATION,
        "prompt": "Lantern light across the corridor",
        "latest_output": take,
        "status": "complete",
        "use_song_audio": True,
        **shot,
    }
    project.shots = [Shot(**fields)]
    store.save(project)
    return project


def restore_audio(client, project, shot=None):
    shot_id = (shot or project.shots[0]).id
    return client.post(f"/api/projects/{project.id}/shots/{shot_id}/restore-song-audio")


def test_restoring_a_take_takes_the_masters_own_window_and_leaves_the_take_alone(
    tmp_path: Path,
):
    """The matrix's first row, the frozen "Always", and the frozen "Never", in one call.

    The submitted graph reads the take and the master, slices the master at the shot's own
    seconds, and writes a sibling. Nothing in the payload generates, and nothing in the
    manifest moves.
    """
    client, store, comfy = make_client(tmp_path)
    project = restorable_project(store, tmp_path)
    take_path = tmp_path / "comfy" / "output" / "takes/shot-h3-reference_00001-audio.mp4"
    before = hashlib.sha256(take_path.read_bytes()).hexdigest()
    shot_before = store.get(project.id).shots[0].model_dump(mode="json")

    response = restore_audio(client, project)

    assert response.status_code == 202
    body = response.json()
    assert body["job"]["kind"] == "post"
    assert body["job"]["target_id"] == project.shots[0].id
    assert len(comfy.prompts) == 1
    payload = comfy.prompts[0]
    # The take is the picture.
    assert payload["mvp:source"]["inputs"]["video"].endswith(
        "takes/shot-h3-reference_00001-audio.mp4"
    )
    assert Path(payload["mvp:source"]["inputs"]["video"]).is_file()
    # The master is the sound, sliced at 12–15.75 s.
    assert payload["mvp:song"]["inputs"]["audio_file"].endswith("media/songs/master.mp3")
    assert Path(payload["mvp:song"]["inputs"]["audio_file"]).is_file()
    assert payload["mvp:song"]["inputs"]["seek_seconds"] == RESTORE_SHOT_START
    assert payload["mvp:song"]["inputs"]["duration"] == RESTORE_SHOT_DURATION
    # Nothing on this path regenerates anything: no model file anywhere in the payload.
    assert not any(
        isinstance(value, str) and value.endswith(".safetensors")
        for node in payload.values()
        for value in node["inputs"].values()
    )
    # A prefix of its own, which is what makes the output a sibling of the take rather than the
    # next entry in the take's numbered series.
    prefix = payload["mvp:save"]["inputs"]["filename_prefix"]
    assert prefix.endswith(RESTORE_AUDIO_PREFIX_SUFFIX)
    assert prefix != project.shots[0].latest_output
    # The take is byte-identical, and the whole Shot is untouched.
    assert hashlib.sha256(take_path.read_bytes()).hexdigest() == before
    assert store.get(project.id).shots[0].model_dump(mode="json") == shot_before


def test_a_restoration_writes_nothing_to_the_shot_even_after_it_completes(tmp_path: Path):
    """The frozen "Never", carried past completion.

    `read_job` has no branch for `kind="post"`, so the shot goes on naming the take that was
    processed and the restored file is reachable on the job that produced it. That is what keeps
    the take's *generated* audio recoverable — the diagnostic the spec insists on.
    """
    client, store, comfy = make_client(tmp_path)
    project = restorable_project(store, tmp_path)
    shot_before = store.get(project.id).shots[0].model_dump(mode="json")

    take = tmp_path / "comfy" / "output" / project.shots[0].latest_output

    job = restore_audio(client, project).json()["job"]
    restored = f"music-video-producer/{project.id}/shots/{project.shots[0].id}-song-audio"
    comfy.history = completed_history_for(
        [{"subfolder": restored.rsplit("/", 1)[0], "filename": "s-song-audio_00001-audio.mp4"}]
    )

    refreshed = client.get(f"/api/projects/{project.id}/jobs/{job['id']}")

    assert refreshed.status_code == 200
    assert refreshed.json()["status"] == "complete"
    saved = ProjectStore(tmp_path).get(project.id)
    # The shot is byte-identical to what it was before the restoration was ever submitted, so
    # `latest_output` still names the take, and the take is still on disk holding H3's own audio.
    assert saved.shots[0].model_dump(mode="json") == shot_before
    assert saved.shots[0].latest_output == "takes/shot-h3-reference_00001-audio.mp4"
    assert take.is_file()
    # And the restored file is reachable, on the job rather than on the shot.
    assert saved.jobs[-1].output_files == [
        f"{restored.rsplit('/', 1)[0]}/s-song-audio_00001-audio.mp4"
    ]


def test_running_a_restoration_twice_produces_a_further_sibling_and_never_an_edit_in_place(
    tmp_path: Path,
):
    """The matrix's run-twice row.

    Both submissions carry the same filename prefix, which is what makes ComfyUI number the
    second `_00002` beside the first rather than write over it — and neither prefix is the
    take's, so neither can land on the file being read.
    """
    client, store, comfy = make_client(tmp_path)
    project = restorable_project(store, tmp_path)
    take_path = tmp_path / "comfy" / "output" / "takes/shot-h3-reference_00001-audio.mp4"
    before = hashlib.sha256(take_path.read_bytes()).hexdigest()

    assert restore_audio(client, project).status_code == 202
    # The first job has to land before a second is allowed; concurrency is refused separately.
    project = store.get(project.id)
    project.jobs[0].status = "complete"
    store.save(project)
    assert restore_audio(client, project).status_code == 202

    prefixes = [prompt["mvp:save"]["inputs"]["filename_prefix"] for prompt in comfy.prompts]
    assert prefixes[0] == prefixes[1]
    assert all(not prefix.endswith(".mp4") for prefix in prefixes)
    assert hashlib.sha256(take_path.read_bytes()).hexdigest() == before


def test_a_shot_that_never_rode_the_song_is_refused_rather_than_given_a_guessed_window(
    tmp_path: Path,
):
    """The matrix's `use_song_audio` false row, and the reason it is a refusal.

    Such a shot was never conditioned on any part of the master, so there is no window to take.
    Any window this route picked would put the picture out of sync with the sound that produced
    it — which the frozen spec calls worse than leaving the generated audio in place.
    """
    client, store, comfy = make_client(tmp_path)
    project = restorable_project(store, tmp_path, use_song_audio=False)

    response = restore_audio(client, project)

    assert response.status_code == 422
    assert response.json()["detail"] == RESTORE_AUDIO_NOT_SONG_AUDIO_REFUSAL.format(
        shot=shot_label(store.get(project.id), store.get(project.id).shots[0])
    )
    # No GPU time, and no submission of any kind.
    assert comfy.prompts == []


def test_a_project_with_no_song_is_refused_naming_what_is_missing(tmp_path: Path):
    """The matrix's no-song row, in both shapes: no Song at all, and a Song whose file is gone."""
    client, store, comfy = make_client(tmp_path)
    songless = restorable_project(store, tmp_path, song=None)

    response = restore_audio(client, songless)

    assert response.status_code == 422
    assert "no song" in response.json()["detail"]
    assert response.json()["detail"] == RESTORE_AUDIO_NO_SONG_REFUSAL.format(
        shot=shot_label(store.get(songless.id), store.get(songless.id).shots[0])
    )

    gone = restorable_project(store, tmp_path, song="missing.mp3")

    missing = restore_audio(client, gone)

    assert missing.status_code == 422
    assert missing.json()["detail"] == RESTORE_AUDIO_MISSING_SONG_REFUSAL.format(
        shot=shot_label(store.get(gone.id), store.get(gone.id).shots[0]),
        path="media/songs/missing.mp3",
    )
    # Named, so a moved file is distinguishable from a cleared directory.
    assert "media/songs/missing.mp3" in missing.json()["detail"]
    assert comfy.prompts == []


def test_a_shot_with_no_take_and_a_take_whose_file_is_gone_are_different_refusals(
    tmp_path: Path,
):
    """The two take rows, which are different situations and get different sentences."""
    client, store, comfy = make_client(tmp_path)
    unrendered = restorable_project(store, tmp_path, take="")

    response = restore_audio(client, unrendered)

    assert response.status_code == 422
    assert response.json()["detail"] == RESTORE_AUDIO_NO_TAKE_REFUSAL.format(
        shot=shot_label(store.get(unrendered.id), store.get(unrendered.id).shots[0])
    )

    project = restorable_project(store, tmp_path)
    (tmp_path / "comfy" / "output" / "takes/shot-h3-reference_00001-audio.mp4").unlink()

    missing = restore_audio(client, project)

    assert missing.status_code == 422
    assert missing.json()["detail"] == RESTORE_AUDIO_MISSING_TAKE_REFUSAL.format(
        shot=shot_label(store.get(project.id), store.get(project.id).shots[0]),
        path="takes/shot-h3-reference_00001-audio.mp4",
    )
    assert comfy.prompts == []


def test_a_take_pointer_escaping_the_output_directory_is_refused(tmp_path: Path):
    """A `latest_output` carrying `..` may not hand an arbitrary file to the node."""
    client, store, comfy = make_client(tmp_path)
    project = restorable_project(store, tmp_path, take="../../escaped.mp4")

    response = restore_audio(client, project)

    assert response.status_code == 422
    assert comfy.prompts == []


def test_a_shot_running_past_the_end_of_the_song_is_refused_by_the_renders_own_rule(
    tmp_path: Path,
):
    """The matrix's past-the-end row, and that it is inherited rather than a second rule.

    The refusal a restoration gives is byte-identical to the one an H3 submission gives for the
    same shot, because both come from `song_audio_window`. A second rule written on this path
    would drift from that one the first time either changed.
    """
    client, store, comfy = make_client(tmp_path)
    project = restorable_project(store, tmp_path, start=152.0, duration=5.0)

    response = restore_audio(client, project)

    assert response.status_code == 422
    assert "runs past the end of the master song" in response.json()["detail"]
    assert comfy.prompts == []
    # The same sentence the shared function produces, so the two stages refuse alike.
    with pytest.raises(ValueError) as shared:
        song_audio_window(start=152.0, duration=5.0, song_duration=RESTORE_SONG_DURATION)
    assert response.json()["detail"] == str(shared.value)


def test_a_restoration_reports_both_lengths_and_never_pads_or_cuts(tmp_path: Path):
    """The matrix's length-mismatch row, in both the agreeing and the differing case.

    A 3.75 s shot is 90 frames, which is 3.75 s exactly. A 5 s shot is 124, which is 5.1667 s —
    a real mismatch, computable before submission, reported with both numbers rather than
    silently corrected. `trim_to_audio` is off in both.
    """
    client, store, comfy = make_client(tmp_path)
    exact = restore_audio(client, restorable_project(store, tmp_path)).json()

    assert exact["audio_seconds"] == 3.75
    assert exact["requested_picture_seconds"] == 3.75
    assert exact["requested_frames"] == 90
    assert exact["lengths_match"] is True

    project = restorable_project(store, tmp_path, start=0.0, duration=5.0)
    rounded = restore_audio(client, project).json()

    assert rounded["audio_seconds"] == 5.0
    assert rounded["requested_frames"] == 124
    assert rounded["requested_picture_seconds"] == pytest.approx(124 / 24)
    assert rounded["lengths_match"] is False
    # Both numbers in the sentence, and the sentence is present either way.
    for body in (exact, rounded):
        assert "5" in body["length_note"] or "3.75" in body["length_note"]
        assert "trim_to_audio is off" in body["length_note"]
        assert "ffprobe" in body["length_note"]
    assert "124 frames" in rounded["length_note"]
    assert all(
        prompt["mvp:save"]["inputs"]["trim_to_audio"] is False for prompt in comfy.prompts
    )


def test_a_restoration_in_flight_refuses_a_second_one_and_so_does_a_live_render(
    tmp_path: Path,
):
    """Concurrency, read off the job records alone.

    A restoration writes nothing to the Shot, so `Shot.status` is not evidence about it — the
    jobs are. Covers a live render and a live enhancement too: both can move the take this reads
    or the file it writes beside.
    """
    client, store, comfy = make_client(tmp_path)
    project = restorable_project(store, tmp_path)
    assert restore_audio(client, project).status_code == 202
    assert store.get(project.id).shots[0].status == "complete"

    second = restore_audio(client, store.get(project.id))

    assert second.status_code == 409
    assert second.json()["detail"] == RESTORE_AUDIO_IN_FLIGHT_REFUSAL.format(
        shot=shot_label(store.get(project.id), store.get(project.id).shots[0])
    )
    assert len(comfy.prompts) == 1

    rendering = restorable_project(store, tmp_path)
    rendering.shots[0].status = "queued"
    store.save(rendering)

    assert restore_audio(client, rendering).status_code == 409
    assert len(comfy.prompts) == 1


def test_a_restoration_never_asks_the_readiness_questions_a_render_asks(tmp_path: Path):
    """No prompt gate and no `ready` status gate: this graph has neither a prompt nor a sampler.

    Borrowing `generate_h3`'s gates here would refuse a real take for fields the work does not
    read. What must exist is a take and a window, which the refusals above check.
    """
    client, store, comfy = make_client(tmp_path)
    project = restorable_project(store, tmp_path, prompt="", status="draft")

    assert restore_audio(client, project).status_code == 202
    assert len(comfy.prompts) == 1


def test_a_shot_without_an_expansion_submits_exactly_what_it_always_did():
    """The safety argument for the whole expansion feature, as one equality.

    `reference_prompt` is the only thing between a Shot and what the reference render
    submits. For a Shot nobody has expanded it must produce the string this route built
    before the field existed — reference map, then intent — because anything else would
    silently change every render in every existing project.

    Written as the literal pre-change expression rather than a call back into the helper,
    which would pass no matter what the helper did.
    """
    shot = Shot(start=0.0, duration=3.75, prompt="The wolf paces through birch.")
    tags = ["<Picture 1> is Lucy", "<Audio 1> is the master song for synchronization"]

    assert reference_prompt(shot, tags) == (
        f"Reference map: {'; '.join(tags)}. {shot.prompt}"
    )


def test_an_expansion_is_submitted_alone_without_the_reference_map_preamble():
    """Dropping the preamble is the point, not an omission.

    An H3-format prompt is a document with a required shape — an optional instruction
    line first, then the three named fields. Prefixing prose to it would put text in
    front of the instruction line and break the very format the expansion exists to
    produce. The tags are not lost: the specialist was given them and wrote them into
    the description as <Picture 1>, which is where the guide puts them.
    """
    expansion = (
        "integrated_multimodal_description: [Shot 1] <Picture 1> stands in the warehouse.\n"
        "overall_soundscape: Distant traffic behind a low room tone.\n"
        "non_diegetic_music: N/A"
    )
    shot = Shot(start=0.0, duration=3.75, prompt="Wide on Lucy.", h3_prompt=expansion)

    submitted = reference_prompt(shot, ["<Picture 1> is Lucy"])
    assert submitted == expansion
    assert "Reference map" not in submitted
    assert submitted.startswith("integrated_multimodal_description:")


def test_an_expansion_of_only_whitespace_is_treated_as_absent():
    """Otherwise a field cleared to spaces would submit a blank prompt and render noise."""
    blank = "   " + "\n" + "  "
    shot = Shot(start=0.0, duration=3.75, prompt="Wide on Lucy.", h3_prompt=blank)
    assert reference_prompt(shot, ["<Picture 1> is Lucy"]).startswith("Reference map:")


GOOD_EXPANSION = (
    "integrated_multimodal_description: [Shot 1] A grey wolf paces through birch trunks "
    "under low amber light; the camera drifts with it, handheld.\n"
    "overall_soundscape: Dry needles compress underfoot. Wind moves through the branches.\n"
    "non_diegetic_music: A low cello figure at a slow tempo, swelling once and receding."
)


class ExpandingShotDirector(FakeDirector):
    """Returns one fixed H3 prompt and records the input it was handed."""

    def __init__(self, text: str = GOOD_EXPANSION):
        self.text = text
        self.inputs: list[dict] = []
        self.prompts: list[str] = []

    async def expand_shot(self, *, shot_input, system_prompt, **_):
        self.inputs.append(shot_input)
        self.prompts.append(system_prompt)
        return self.text


def _expandable(client, store, **shot_kwargs):
    project = store.create(Project(name="Pass two"))
    shot = Shot(start=0.0, duration=3.75, prompt="Wolf B-roll", **shot_kwargs)
    client.put(
        f"/api/projects/{project.id}/shots",
        json={"shots": [json.loads(shot.model_dump_json())]},
    )
    return project.id, shot.id


def test_expanding_a_shot_writes_only_its_h3_prompt(tmp_path: Path):
    """The intent survives, which is the whole reason this is a second field."""
    director = ExpandingShotDirector()
    client, store = make_client_with_director(tmp_path, director)
    project_id, shot_id = _expandable(client, store)

    response = client.post(f"/api/projects/{project_id}/shots/{shot_id}/expand-prompt")

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert body["problems"] == []

    stored = store.get(project_id).shots[0]
    assert stored.h3_prompt == GOOD_EXPANSION
    assert stored.prompt == "Wolf B-roll"
    assert stored.status == "draft"
    assert stored.prompt_id == ""


def test_a_malformed_expansion_is_reported_and_never_stored(tmp_path: Path):
    """The one outcome checking before a render exists to prevent.

    A malformed prompt in the manifest would be submitted by the *next* render, and the
    failure would surface as a bad take rather than as a message. So it is returned with its
    problems and the Shot is left exactly as it was.
    """
    director = ExpandingShotDirector("A grey wolf pacing through trees. 35mm, grainy.")
    client, store = make_client_with_director(tmp_path, director)
    project_id, shot_id = _expandable(client, store)

    body = client.post(
        f"/api/projects/{project_id}/shots/{shot_id}/expand-prompt"
    ).json()

    assert body["applied"] is False
    assert body["problems"]
    # The refused text comes back so the Director can read and judge it, the same argument
    # `MessageNotice.raw` makes for refused Director output.
    assert body["prompt"] == "A grey wolf pacing through trees. 35mm, grainy."
    assert store.get(project_id).shots[0].h3_prompt == ""


def test_a_locked_shot_is_refused_before_the_model_is_called(tmp_path: Path):
    director = ExpandingShotDirector()
    client, store = make_client_with_director(tmp_path, director)
    project_id, shot_id = _expandable(client, store, locked=True)

    response = client.post(f"/api/projects/{project_id}/shots/{shot_id}/expand-prompt")

    assert response.status_code == 422
    assert "locked" in response.json()["detail"]
    assert director.inputs == []


def test_a_rendered_shot_is_refused_for_its_provenance_not_its_prompt(tmp_path: Path):
    """Its prompt is the record of what produced a take, not an intention any more."""
    director = ExpandingShotDirector()
    client, store = make_client_with_director(tmp_path, director)
    project_id, shot_id = _expandable(client, store, prompt_id="abc", status="complete")

    response = client.post(f"/api/projects/{project_id}/shots/{shot_id}/expand-prompt")

    assert response.status_code == 422
    assert "render again" in response.json()["detail"]
    assert director.inputs == []


def test_a_shot_with_no_intent_is_refused_and_told_which_pass_writes_one(tmp_path: Path):
    director = ExpandingShotDirector()
    client, store = make_client_with_director(tmp_path, director)
    project = store.create(Project(name="No intent"))
    shot = Shot(start=0.0, duration=3.75, prompt="New shot")
    client.put(
        f"/api/projects/{project.id}/shots",
        json={"shots": [json.loads(shot.model_dump_json())]},
    )

    response = client.post(f"/api/projects/{project.id}/shots/{shot.id}/expand-prompt")

    assert response.status_code == 422
    assert "no intent to expand" in response.json()["detail"]
    assert director.inputs == []


def test_being_locked_is_reported_ahead_of_having_no_intent(tmp_path: Path):
    """Order matters: telling a locked Shot to write an intent first sends the Director to do
    work that would then be refused anyway."""
    director = ExpandingShotDirector()
    client, store = make_client_with_director(tmp_path, director)
    project = store.create(Project(name="Both wrong"))
    shot = Shot(start=0.0, duration=3.75, prompt="New shot", locked=True)
    client.put(
        f"/api/projects/{project.id}/shots",
        json={"shots": [json.loads(shot.model_dump_json())]},
    )

    detail = client.post(
        f"/api/projects/{project.id}/shots/{shot.id}/expand-prompt"
    ).json()["detail"]

    assert "locked" in detail
    assert "no intent" not in detail


def test_a_text_only_shot_is_not_told_to_write_an_instruction_line(tmp_path: Path):
    """The guide's checklist treats an instruction line on a text-only prompt as a mode
    confusion, so the specialist must not be asked for one."""
    director = ExpandingShotDirector()
    client, store = make_client_with_director(tmp_path, director)
    project_id, shot_id = _expandable(client, store, mode="text_to_video")

    client.post(f"/api/projects/{project_id}/shots/{shot_id}/expand-prompt")

    assert "must open with the instruction line" not in director.prompts[0]


def test_a_keyframe_shot_is_told_to_write_one(tmp_path: Path):
    director = ExpandingShotDirector()
    client, store = make_client_with_director(tmp_path, director)
    project_id, shot_id = _expandable(client, store, mode="first_last")

    client.post(f"/api/projects/{project_id}/shots/{shot_id}/expand-prompt")

    assert "must open with the instruction line" in director.prompts[0]


def test_a_references_shot_with_a_keyframe_role_gets_the_anchor_rules(tmp_path: Path):
    """The specialist is told the roles are frame anchors, only where the shape exists.

    Three prompts from three shapes: a references shot citing a first-frame picture carries
    the anchor rules; a references shot citing only plain references gets the byte-identical
    prompt it always got; the dedicated keyframe modes keep their instruction-line sentence
    and never this one — full-reference mode has no instruction line, and the two wordings
    must not blend.
    """
    director = ExpandingShotDirector()
    client, store = make_client_with_director(tmp_path, director)
    project_id, shot_id = _expandable(
        client,
        store,
        mode="references",
        citations=[AssetCitation(asset_id="asset_a", role="first")],
    )
    client.post(f"/api/projects/{project_id}/shots/{shot_id}/expand-prompt")
    with_keyframe = director.prompts[-1]

    project_id, shot_id = _expandable(
        client,
        store,
        mode="references",
        citations=[AssetCitation(asset_id="asset_a", role="reference")],
    )
    client.post(f"/api/projects/{project_id}/shots/{shot_id}/expand-prompt")
    plain = director.prompts[-1]

    assert "the shot begins from <Picture N>" in with_keyframe
    assert 'marked "first frame" is the clip\'s exact opening frame' in with_keyframe
    assert "must open with the instruction line" not in with_keyframe
    assert "opening frame" not in plain
    # Byte-identical, not merely rule-free: the plain references prompt is exactly the prompt
    # every shot without the shape has always been given.
    assert plain == h3_expansion_system_prompt()


# ---------------------------------------------------------------------------------------------
# The plan-wide sweep: pass two, one model call per shot
# ---------------------------------------------------------------------------------------------


class SweepingDirector(FakeDirector):
    """One answer per shot, keyed by the shot's intent, recording every call it was handed.

    Keyed by intent rather than returning one fixed string so a test can make exactly one shot in
    a plan malformed -- which is the only way to assert that a refusal on one does not stop the
    rest, and that the malformed one is the only one left unwritten.
    """

    def __init__(self, answers: dict[str, str] | None = None, default: str = GOOD_EXPANSION):
        self.answers = answers or {}
        self.default = default
        self.inputs: list[dict] = []
        self.prompts: list[str] = []
        #: Intents that raise instead of answering, as `{intent: exception}`.
        self.raises: dict[str, Exception] = {}

    async def expand_shot(self, *, shot_input, system_prompt, **_):
        self.inputs.append(shot_input)
        self.prompts.append(system_prompt)
        intent = shot_input["shot"]["intent"]
        if intent in self.raises:
            raise self.raises[intent]
        return self.answers.get(intent, self.default)


def _plan(client, store, shots: list[Shot]) -> str:
    project = store.create(Project(name="Sweep"))
    client.put(
        f"/api/projects/{project.id}/shots",
        json={"shots": [json.loads(shot.model_dump_json()) for shot in shots]},
    )
    return project.id


SWEEP = "/api/projects/{project}/shots/expand-prompts"


def test_the_sweep_is_one_model_call_per_shot_and_not_one_call(tmp_path: Path):
    """The whole point of pass two, and the thing the Director set out explicitly.

    A single call over the plan is pass one and already exists. If this route ever collapses into
    one call it has silently become a second copy of `director/expand`, writing the wrong field.
    """
    director = SweepingDirector()
    client, store = make_client_with_director(tmp_path, director)
    project_id = _plan(client, store, [
        Shot(start=0.0, duration=4.0, prompt="Wolf in birch"),
        Shot(start=4.0, duration=4.0, prompt="The clearing widens"),
        Shot(start=8.0, duration=4.0, prompt="Lucy turns to camera"),
    ])

    response = client.post(SWEEP.format(project=project_id))

    assert response.status_code == 200
    assert len(director.inputs) == 3
    # Each call carried one shot's own payload, not the plan.
    assert [held["shot"]["intent"] for held in director.inputs] == [
        "Wolf in birch", "The clearing widens", "Lucy turns to camera",
    ]
    stored = store.get(project_id)
    assert [shot.h3_prompt for shot in stored.shots] == [GOOD_EXPANSION] * 3
    # The intents survive. `Shot.prompt` is what re-expansion works from.
    assert [shot.prompt for shot in stored.shots] == [
        "Wolf in birch", "The clearing widens", "Lucy turns to camera",
    ]


def test_a_malformed_answer_mid_sweep_is_reported_and_the_rest_still_land(tmp_path: Path):
    """The frozen matrix's own sentence: a failure on one does not stop the rest.

    And phase one's rule applied per shot: the malformed one is not stored. Storing it would put a
    broken prompt in the manifest that the next render would submit.
    """
    director = SweepingDirector({"The clearing widens": "A clearing, 35mm, grainy."})
    client, store = make_client_with_director(tmp_path, director)
    project_id = _plan(client, store, [
        Shot(start=0.0, duration=4.0, prompt="Wolf in birch"),
        Shot(start=4.0, duration=4.0, prompt="The clearing widens"),
        Shot(start=8.0, duration=4.0, prompt="Lucy turns to camera"),
    ])

    client.post(SWEEP.format(project=project_id))

    stored = store.get(project_id)
    assert [bool(shot.h3_prompt) for shot in stored.shots] == [True, False, True]
    assert stored.shots[1].h3_prompt == ""
    reply = stored.messages[-1]
    assert "H3 prompts written for 2 shot(s)" in reply.content
    assert "not a well-formed H3 prompt" in reply.content
    # The refused text is kept for inspection, out of band. It is in `raw`, which
    # `DIRECTOR_CONTEXT_EXCLUDE` drops, and never in a sentence the next call would read.
    malformed = [notice for notice in reply.notices if "well-formed" in notice.text]
    assert len(malformed) == 1
    assert malformed[0].raw == "A clearing, 35mm, grainy."
    assert "A clearing, 35mm, grainy." not in reply.content


def test_a_transport_failure_on_one_shot_does_not_stop_the_sweep(tmp_path: Path):
    director = SweepingDirector()
    director.raises["The clearing widens"] = DirectorError("connection refused")
    client, store = make_client_with_director(tmp_path, director)
    project_id = _plan(client, store, [
        Shot(start=0.0, duration=4.0, prompt="Wolf in birch"),
        Shot(start=4.0, duration=4.0, prompt="The clearing widens"),
        Shot(start=8.0, duration=4.0, prompt="Lucy turns to camera"),
    ])

    response = client.post(SWEEP.format(project=project_id))

    assert response.status_code == 200
    # The third shot was still attempted, which is the assertion: a `raise` on the second would
    # have ended the sweep with one prompt written and two shots unnamed.
    assert len(director.inputs) == 3
    stored = store.get(project_id)
    assert [bool(shot.h3_prompt) for shot in stored.shots] == [True, False, True]
    assert "connection refused" in stored.messages[-1].content


def test_nothing_is_persisted_until_every_shot_has_been_judged(tmp_path: Path):
    """One terminal save, and it is what makes "nothing half-applied" structural.

    Driven by a failure the route does not catch: an answer for shot one is well-formed, and shot
    two raises something that is not a `DirectorError`. If the write happened per shot, shot one's
    expansion would be on disk under a request that 500'd.
    """
    director = SweepingDirector()
    director.raises["The clearing widens"] = RuntimeError("the host went away mid-sweep")
    client, store = make_client_with_director(tmp_path, director)
    project_id = _plan(client, store, [
        Shot(start=0.0, duration=4.0, prompt="Wolf in birch"),
        Shot(start=4.0, duration=4.0, prompt="The clearing widens"),
    ])

    with pytest.raises(RuntimeError):
        client.post(SWEEP.format(project=project_id))

    stored = store.get(project_id)
    assert [shot.h3_prompt for shot in stored.shots] == ["", ""]
    # And nothing was written to the thread either, so the manifest carries no claim about a sweep
    # that did not finish.
    assert stored.messages == []


def test_the_sweep_reports_every_shot_it_could_not_write_and_says_why(tmp_path: Path):
    """Applied, malformed, locked, rendered and no-intent, each named, the way `assistant_fill`
    reports. A shot the sweep silently skipped is indistinguishable from one it forgot."""
    director = SweepingDirector({"The clearing widens": "A clearing, 35mm, grainy."})
    client, store = make_client_with_director(tmp_path, director)
    project_id = _plan(client, store, [
        Shot(start=0.0, duration=4.0, prompt="Wolf in birch"),
        Shot(start=4.0, duration=4.0, prompt="The clearing widens"),
        Shot(start=8.0, duration=4.0, prompt="A take nobody may touch", locked=True),
        Shot(start=12.0, duration=4.0, prompt="Already shot", prompt_id="abc", status="complete"),
        Shot(start=16.0, duration=4.0, prompt="New shot"),
    ])

    client.post(SWEEP.format(project=project_id))

    stored = store.get(project_id)
    content = stored.messages[-1].content
    labels = {shot.id: shot_label(stored, shot) for shot in stored.shots}
    assert f"H3 prompts written for 1 shot(s): {labels[stored.shots[0].id]}" in content
    assert labels[stored.shots[1].id] in content and "well-formed" in content
    assert f"they are locked: {labels[stored.shots[2].id]}" in content
    assert labels[stored.shots[3].id] in content and "already depends on the prompt" in content
    assert f"no intent to expand from: {labels[stored.shots[4].id]}" in content
    # Every shot appears in the report, which is what "reported individually" means.
    for label in labels.values():
        assert label in content, label
    # And only the writable, well-formed one was written.
    assert [bool(shot.h3_prompt) for shot in stored.shots] == [True, False, False, False, False]
    # The model was never asked about the three that could not be written. The well-formed
    # answer cost one call; the persistently malformed one cost its whole retry budget.
    assert len(director.inputs) == 1 + EXPANSION_ATTEMPTS


def test_the_sweep_refuses_a_locked_shot_before_the_model_is_asked(tmp_path: Path):
    """A refusal that costs a model call is a refusal that costs the Director seconds."""
    director = SweepingDirector()
    client, store = make_client_with_director(tmp_path, director)
    project_id = _plan(client, store, [
        Shot(start=0.0, duration=4.0, prompt="A take nobody may touch", locked=True),
    ])

    client.post(SWEEP.format(project=project_id))

    assert director.inputs == []


def test_the_sweep_reports_a_lock_ahead_of_a_missing_intent(tmp_path: Path):
    """Phase one's refusal order, applied per shot by the sweep. A locked shot with an empty intent
    must hear that it is locked; telling it to write an intent first sends the Director to do work
    that would then be refused anyway."""
    director = SweepingDirector()
    client, store = make_client_with_director(tmp_path, director)
    project_id = _plan(client, store, [
        Shot(start=0.0, duration=4.0, prompt="New shot", locked=True),
    ])

    client.post(SWEEP.format(project=project_id))

    content = store.get(project_id).messages[-1].content
    assert "they are locked" in content
    assert "no intent to expand from" not in content


def test_a_shot_locked_while_the_sweep_ran_is_not_written_and_is_named(tmp_path: Path):
    """The sweep is many model calls long, so the project can move under it repeatedly.

    The lock is applied between the first call and the second, which is exactly the window
    `expand_shot_prompt` re-reads for after its single await.
    """
    client_box: dict = {}

    class LockingDirector(SweepingDirector):
        async def expand_shot(self, *, shot_input, system_prompt, **_):
            text = await super().expand_shot(shot_input=shot_input, system_prompt=system_prompt)
            if shot_input["shot"]["intent"] == "Wolf in birch":
                store, project_id = client_box["fixture"]
                held = store.get(project_id)
                held.shots[1].locked = True
                store.save(held)
            return text

    director = LockingDirector()
    client, store = make_client_with_director(tmp_path, director)
    project_id = _plan(client, store, [
        Shot(start=0.0, duration=4.0, prompt="Wolf in birch"),
        Shot(start=4.0, duration=4.0, prompt="The clearing widens"),
    ])
    client_box["fixture"] = (store, project_id)

    client.post(SWEEP.format(project=project_id))

    stored = store.get(project_id)
    assert stored.shots[0].h3_prompt == GOOD_EXPANSION
    # The answer was written against a project that no longer describes this shot, so it does not
    # land -- and the lock is reported rather than the write being silently dropped.
    assert stored.shots[1].h3_prompt == ""
    assert "they are locked" in stored.messages[-1].content


def test_a_shot_deleted_while_the_sweep_ran_is_reported_as_missing(tmp_path: Path):
    client_box: dict = {}

    class DeletingDirector(SweepingDirector):
        async def expand_shot(self, *, shot_input, system_prompt, **_):
            text = await super().expand_shot(shot_input=shot_input, system_prompt=system_prompt)
            if shot_input["shot"]["intent"] == "Wolf in birch":
                store, project_id = client_box["fixture"]
                held = store.get(project_id)
                held.shots = held.shots[:1]
                store.save(held)
            return text

    director = DeletingDirector()
    client, store = make_client_with_director(tmp_path, director)
    project_id = _plan(client, store, [
        Shot(start=0.0, duration=4.0, prompt="Wolf in birch"),
        Shot(start=4.0, duration=4.0, prompt="The clearing widens"),
    ])
    client_box["fixture"] = (store, project_id)

    client.post(SWEEP.format(project=project_id))

    stored = store.get(project_id)
    assert len(stored.shots) == 1
    assert stored.shots[0].h3_prompt == GOOD_EXPANSION
    assert "no longer has them" in stored.messages[-1].content


def test_an_empty_plan_is_refused_before_any_expansion_call(tmp_path: Path):
    director = SweepingDirector()
    client, store = make_client_with_director(tmp_path, director)
    project = store.create(Project(name="Nothing to sweep"))

    response = client.post(SWEEP.format(project=project.id))

    assert response.status_code == 422
    assert response.json()["detail"] == EXPAND_PROMPTS_WITHOUT_SHOTS
    assert director.inputs == []


def test_an_unconfigured_model_is_unavailable_for_the_whole_sweep(tmp_path: Path):
    """503, not one `failed` line per shot. It is a fact about this installation, identical for
    every shot, so N calls would produce N identical sentences and no information."""

    class UnconfiguredDirector(SweepingDirector):
        async def expand_shot(self, *, shot_input, system_prompt, **_):
            raise DirectorUnavailable("LLM director is not configured.")

    director = UnconfiguredDirector()
    client, store = make_client_with_director(tmp_path, director)
    project_id = _plan(client, store, [Shot(start=0.0, duration=4.0, prompt="Wolf in birch")])

    response = client.post(SWEEP.format(project=project_id))

    assert response.status_code == 503
    assert store.get(project_id).messages == []


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"locked": True}, "they are locked"),
        ({"prompt_id": "abc", "status": "complete"}, "already depends on the prompt"),
    ],
)
def test_the_sweep_and_the_single_shot_route_refuse_the_same_states(
    tmp_path: Path, kwargs: dict, expected: str
):
    """The two paths implement the same rules and must not drift.

    Phase one's route was deliberately not rebuilt onto the sweep engine -- it has its own HTTP
    contract, pinned by its own tests -- so this is the assertion that keeps the duplication
    honest: the same shot, refused for the same reason, through both doors, with no model call
    spent by either.
    """
    director = SweepingDirector()
    client, store = make_client_with_director(tmp_path, director)
    project_id = _plan(
        client, store, [Shot(start=0.0, duration=4.0, prompt="Wolf in birch", **kwargs)]
    )
    shot_id = store.get(project_id).shots[0].id

    single = client.post(f"/api/projects/{project_id}/shots/{shot_id}/expand-prompt")
    client.post(SWEEP.format(project=project_id))

    assert single.status_code == 422
    assert director.inputs == []
    assert expected in store.get(project_id).messages[-1].content
    assert store.get(project_id).shots[0].h3_prompt == ""


def test_the_sweep_never_puts_an_expansion_into_the_directors_context(tmp_path: Path):
    """The recorded cause of Director degradation, and the reason `h3_prompt` is withheld at all.

    The sweep writes a reply into the thread, and `TreatmentMessage.content` *is* in the dump. So
    the report's sentences must name shots and outcomes and never carry the expansion itself.
    """
    director = SweepingDirector({"The clearing widens": "A clearing, 35mm, grainy."})
    client, store = make_client_with_director(tmp_path, director)
    project_id = _plan(client, store, [
        Shot(start=0.0, duration=4.0, prompt="Wolf in birch"),
        Shot(start=4.0, duration=4.0, prompt="The clearing widens"),
    ])

    client.post(SWEEP.format(project=project_id))

    stored = store.get(project_id)
    context = json.dumps(
        stored.model_dump(mode="json", exclude=DIRECTOR_CONTEXT_EXCLUDE), ensure_ascii=False
    )
    assert GOOD_EXPANSION not in context
    assert "A clearing, 35mm, grainy." not in context
    # ...and the intents, which the expansion was written from, are still there. Withholding the
    # expansion is only defensible because the thing it was expanded from still reaches the model.
    assert "Wolf in birch" in context


# ---------------------------------------------------------------------------------------------
# Automatic retries: up to EXPANSION_ATTEMPTS model calls behind one expansion
# ---------------------------------------------------------------------------------------------


MALFORMED_EXPANSION = "A grey wolf pacing through trees. 35mm, grainy."


class ScriptedRetryDirector(FakeDirector):
    """Answers each `expand_shot` call from a script, recording the corrective feedback.

    The script is consumed strictly in order and an exhausted script *raises*, which is the
    trap the retry tests rely on: a loop that keeps calling past its scripted answers fails
    loudly rather than looking like a model that kept agreeing.
    """

    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[dict] = []

    async def expand_shot(
        self, *, shot_input, system_prompt, rejected="", rejected_problems=(), **_
    ):
        self.calls.append(
            {
                "shot": shot_input["shot"]["id"],
                "rejected": rejected,
                "problems": tuple(rejected_problems),
            }
        )
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def test_a_malformed_answer_is_retried_with_the_checkers_problems_fed_back(tmp_path: Path):
    """The Director's ruling after a live plan-wide run: "some failed and took a couple tries
    due to formatting, 3 auto retries per would be fine."

    The retry is a corrective follow-up turn rather than a fresh roll: the failed text and the
    checker's own sentences ride along, so the model has a target. The first well-formed answer
    is the one that lands, and the result says what it cost.
    """
    director = ScriptedRetryDirector([MALFORMED_EXPANSION, GOOD_EXPANSION])
    client, store = make_client_with_director(tmp_path, director)
    project_id, shot_id = _expandable(client, store)

    body = client.post(f"/api/projects/{project_id}/shots/{shot_id}/expand-prompt").json()

    assert body["applied"] is True
    assert body["attempts"] == 2
    assert store.get(project_id).shots[0].h3_prompt == GOOD_EXPANSION
    assert len(director.calls) == 2
    # The first call carried no feedback; the second carried the failed text and the problems.
    assert director.calls[0]["rejected"] == ""
    assert director.calls[0]["problems"] == ()
    assert director.calls[1]["rejected"] == MALFORMED_EXPANSION
    # The problems are the checker's own sentences about that text -- prose with none of the
    # three named fields -- which is what gives the retry a target.
    assert any("No core fields found" in problem for problem in director.calls[1]["problems"])


def test_retries_stop_at_the_first_well_formed_answer(tmp_path: Path):
    """One good answer, one call. The scripted director has exactly one answer, so a loop that
    kept going past a well-formed one would raise out of the empty script rather than pass."""
    director = ScriptedRetryDirector([GOOD_EXPANSION])
    client, store = make_client_with_director(tmp_path, director)
    project_id, shot_id = _expandable(client, store)

    body = client.post(f"/api/projects/{project_id}/shots/{shot_id}/expand-prompt").json()

    assert body["applied"] is True
    assert body["attempts"] == 1
    assert len(director.calls) == 1


def test_a_shot_that_never_answers_well_formed_reports_its_last_attempt_and_stores_nothing(
    tmp_path: Path,
):
    """All four attempts spent, the last attempt's text and problems reported, nothing stored.

    A malformed expansion is never stored -- that invariant predates the retries and must
    survive them: the retry loop's final answer is exactly as unstorable as a first attempt's.
    """
    script = [f"{MALFORMED_EXPANSION} (attempt {n})" for n in range(1, EXPANSION_ATTEMPTS + 1)]
    director = ScriptedRetryDirector(list(script))
    client, store = make_client_with_director(tmp_path, director)
    project_id, shot_id = _expandable(client, store)

    body = client.post(f"/api/projects/{project_id}/shots/{shot_id}/expand-prompt").json()

    assert body["applied"] is False
    assert body["attempts"] == EXPANSION_ATTEMPTS
    assert len(director.calls) == EXPANSION_ATTEMPTS
    # The report is the LAST attempt's, which is the one the Director would want to judge.
    assert body["prompt"] == script[-1]
    assert body["problems"]
    assert store.get(project_id).shots[0].h3_prompt == ""


def test_a_budget_exhaustion_is_retried_and_a_recovery_mid_budget_lands(tmp_path: Path):
    """The 1-in-6 reasoning-budget exhaustion is sampling luck, so a clean retry -- no feedback
    turn, there is no failed text to correct -- is worth its call. The sweep's notice says what
    the recovery cost, because a plan of shots taking three tries is signal about the model."""
    director = ScriptedRetryDirector(
        [DirectorBudgetExhausted("spent its whole budget reasoning"), GOOD_EXPANSION]
    )
    client, store = make_client_with_director(tmp_path, director)
    project_id = _plan(client, store, [Shot(start=0.0, duration=4.0, prompt="Wolf in birch")])

    client.post(SWEEP.format(project=project_id))

    stored = store.get(project_id)
    assert stored.shots[0].h3_prompt == GOOD_EXPANSION
    assert len(director.calls) == 2
    # A budget retry carries no corrective feedback: there is nothing to correct.
    assert director.calls[1]["rejected"] == ""
    assert "(took 2 tries)" in stored.messages[-1].content


def test_a_budget_that_never_recovers_is_a_failed_shot_after_its_whole_retry_budget(
    tmp_path: Path,
):
    director = ScriptedRetryDirector(
        [DirectorBudgetExhausted("spent its whole budget reasoning")] * EXPANSION_ATTEMPTS
    )
    client, store = make_client_with_director(tmp_path, director)
    project_id = _plan(client, store, [Shot(start=0.0, duration=4.0, prompt="Wolf in birch")])

    client.post(SWEEP.format(project=project_id))

    stored = store.get(project_id)
    assert stored.shots[0].h3_prompt == ""
    assert len(director.calls) == EXPANSION_ATTEMPTS
    content = stored.messages[-1].content
    assert "the model call for them failed" in content
    assert f"(took {EXPANSION_ATTEMPTS} tries)" in content


def test_an_unavailable_director_is_never_retried(tmp_path: Path):
    """`DirectorUnavailable` is a configuration fact, identical on every attempt. Retrying it
    would spend the Director's seconds to learn nothing, so both doors ask exactly once."""
    unavailable = DirectorUnavailable("LLM director is not configured.")
    director = ScriptedRetryDirector([unavailable] * (EXPANSION_ATTEMPTS * 2))
    client, store = make_client_with_director(tmp_path, director)
    project_id, shot_id = _expandable(client, store)

    single = client.post(f"/api/projects/{project_id}/shots/{shot_id}/expand-prompt")
    assert single.status_code == 503
    assert len(director.calls) == 1

    sweep = client.post(SWEEP.format(project=project_id))
    assert sweep.status_code == 503
    assert len(director.calls) == 2


def test_a_transport_error_is_not_retried_either(tmp_path: Path):
    """Only two failures are worth a retry: a checker-rejected answer and a budget exhaustion.
    A dead host will be exactly as dead on the next attempt, and four timeouts against a hung
    model would hold the Director's request open four times as long for the same sentence."""
    director = ScriptedRetryDirector([DirectorError("connection refused")])
    client, store = make_client_with_director(tmp_path, director)
    project_id, shot_id = _expandable(client, store)

    response = client.post(f"/api/projects/{project_id}/shots/{shot_id}/expand-prompt")

    assert response.status_code == 502
    assert "connection refused" in response.json()["detail"]
    assert len(director.calls) == 1
    assert store.get(project_id).shots[0].h3_prompt == ""


def test_a_recovery_on_a_retry_is_visible_in_the_sweeps_written_notice(tmp_path: Path):
    """The attempt count must not lie in either direction: a first-try shot carries no
    annotation, and a third-try shot says three."""
    director = ScriptedRetryDirector(
        [
            GOOD_EXPANSION,  # first shot: first try
            MALFORMED_EXPANSION,
            MALFORMED_EXPANSION,
            GOOD_EXPANSION,  # second shot: third try
        ]
    )
    client, store = make_client_with_director(tmp_path, director)
    project_id = _plan(client, store, [
        Shot(start=0.0, duration=4.0, prompt="Wolf in birch"),
        Shot(start=4.0, duration=4.0, prompt="The clearing widens"),
    ])

    client.post(SWEEP.format(project=project_id))

    stored = store.get(project_id)
    assert [shot.h3_prompt for shot in stored.shots] == [GOOD_EXPANSION] * 2
    content = stored.messages[-1].content
    labels = {shot.id: shot_label(stored, shot) for shot in stored.shots}
    assert "H3 prompts written for 2 shot(s)" in content
    assert f"{labels[stored.shots[0].id]}," in content
    assert f"{labels[stored.shots[0].id]} (took" not in content
    assert f"{labels[stored.shots[1].id]} (took 3 tries)" in content


# --------------------------------------------------------------------------------------------
# The render-status poll endpoint -- AD-1's transport, through the route.
# --------------------------------------------------------------------------------------------


def flux_job(client, store, comfy, name: str) -> tuple[str, dict]:
    """A project with one Flux render genuinely in flight, built through the shipped route."""
    project = store.create(Project(name=name))
    job = client.post(
        f"/api/projects/{project.id}/generate/flux",
        json={
            "name": "Lead singer",
            "kind": "character",
            "prompt": "portrait",
            "width": 1024,
            "height": 1024,
            "steps": 4,
            "guidance": 4,
            "seed": 1,
        },
    ).json()
    return project.id, job


def test_render_status_completes_a_flux_job_and_persists_what_it_learned(tmp_path: Path):
    """The live defect: ComfyUI finished, and nothing the Director could see ever said so.

    The poll answer has to carry the completion on every surface at once -- the job row, the
    asset's landed file -- and the manifest has to hold it afterwards, because the next full
    project load must not resurrect a RENDERING card for a render that landed."""
    client, store, comfy = make_client(tmp_path)
    project_id, _job = flux_job(client, store, comfy, "Poll completes")

    async def completed_history(prompt_id):
        return type(
            "History",
            (),
            {
                "prompt_id": prompt_id,
                "status": "complete",
                "outputs": [
                    {
                        "subfolder": f"music-video-producer\\{project_id}\\assets",
                        "filename": "Lead singer_00001_.png",
                    }
                ],
                "error": "",
            },
        )()

    comfy.history = completed_history
    response = client.get(f"/api/projects/{project_id}/render-status")

    assert response.status_code == 200
    report = response.json()
    assert report["comfy_online"] is True
    # The last open job settled on this tick, so the browser's stop signal is in the answer.
    assert report["active"] is False
    assert [item["status"] for item in report["jobs"]] == ["complete"]
    landed = f"music-video-producer/{project_id}/assets/Lead singer_00001_.png"
    assert report["assets"][0]["path"] == landed
    assert comfy.queue_calls == 1
    saved = store.get(project_id)
    assert saved.assets[0].path == landed
    assert saved.jobs[0].status == "complete"


def test_render_status_reads_the_queue_once_and_reports_running_from_it(tmp_path: Path):
    client, store, comfy = make_client(tmp_path)
    project_id, job = flux_job(client, store, comfy, "Poll queue once")
    comfy.queue_payload = {
        "queue_running": [[0, job["prompt_id"], {}]],
        "queue_pending": [],
    }
    # History would answer "complete"; a prompt still in the live queue must never reach it.
    comfy.history_calls = 0

    report = client.get(f"/api/projects/{project_id}/render-status").json()

    assert report["active"] is True
    assert report["jobs"][0]["status"] == "running"
    assert comfy.queue_calls == 1
    assert comfy.history_calls == 0


def test_render_status_on_an_idle_project_asks_comfyui_nothing(tmp_path: Path):
    """The other half of the polling contract: a project with nothing open costs nothing."""
    client, store, comfy = make_client(tmp_path)
    project = store.create(Project(name="Idle poll"))

    report = client.get(f"/api/projects/{project.id}/render-status").json()

    assert report == {
        "active": False,
        "comfy_online": True,
        "jobs": [],
        "shots": [],
        "assets": [],
        "song": None,
    }
    assert comfy.queue_calls == 0
    assert comfy.history_calls == 0


def test_render_status_degrades_to_200_when_comfyui_is_down(tmp_path: Path):
    """The poll runs every two seconds; a ComfyUI restart must not be an error every tick.

    Same outage, two contracts: the manual per-job refresh keeps its 502 -- a click deserves
    the honest failure -- while the poll answers 200 with `comfy_online: false` and the jobs
    as last known, so the queue panel keeps its last real answer instead of blanking."""
    client, store, comfy = make_client(tmp_path)
    project_id, job = flux_job(client, store, comfy, "Poll outage")
    comfy.queue_error = True
    comfy.history_error = True

    report = client.get(f"/api/projects/{project_id}/render-status")
    refresh = client.get(f"/api/projects/{project_id}/jobs/{job['id']}")

    assert report.status_code == 200
    assert report.json()["comfy_online"] is False
    assert report.json()["active"] is True
    assert report.json()["jobs"][0]["status"] == "queued"
    assert refresh.status_code == 502
    saved = store.get(project_id)
    assert saved.jobs[0].status == "queued"


def test_render_status_and_the_per_job_refresh_tell_one_story(tmp_path: Path):
    """Both routes delegate the completion to `batch.apply_job_history` -- prove it holds.

    Two projects with identical in-flight H3 renders; one reconciled by the poll, one by the
    per-job GET. The shot and job they leave behind must be field-for-field identical, because
    two hand-written copies of "what a finished job does" is the drift AD-1 forbids."""
    client, store, comfy = make_client(tmp_path)
    outcomes = {}
    for name, refresh in (("via-poll", True), ("via-job", False)):
        project = store.create(Project(name=name))
        project.shots = [
            Shot(id="shot_a", start=0, duration=5, prompt="Turn", status="ready")
        ]
        store.save(project)
        job = client.post(
            f"/api/projects/{project.id}/shots/shot_a/generate/h3", json={}
        ).json()

        async def completed_history(prompt_id):
            return type(
                "History",
                (),
                {
                    "prompt_id": prompt_id,
                    "status": "complete",
                    "outputs": [{"subfolder": "shots", "filename": "take_00001.mp4"}],
                    "error": "",
                },
            )()

        comfy.history = completed_history
        if refresh:
            client.get(f"/api/projects/{project.id}/render-status")
        else:
            client.get(f"/api/projects/{project.id}/jobs/{job['id']}")
        saved = store.get(project.id)
        outcomes[name] = (
            saved.shots[0].status,
            saved.shots[0].latest_output,
            saved.jobs[-1].status,
            saved.jobs[-1].output_files,
        )

    assert outcomes["via-poll"] == outcomes["via-job"]
    assert outcomes["via-poll"][0] == "complete"


def test_render_status_of_a_missing_project_is_404(tmp_path: Path):
    client, _, comfy = make_client(tmp_path)

    assert client.get("/api/projects/nope/render-status").status_code == 404
    assert comfy.queue_calls == 0


def test_a_singing_shot_is_refused_enhancement_outright(tmp_path: Path):
    """The Director's ruling enforced: the enhancer measurably moves lip position, so a
    singing Shot loses the one thing the H3 reference path exists to get right."""
    client, store, _comfy = make_client(tmp_path)
    project = enhanced_shot_project(store, tmp_path, singing="singing")

    response = enhance(client, project)

    assert response.status_code == 422
    assert "moves lip position" in response.json()["detail"]
    assert store.get(project.id).jobs == []


def test_an_unlabelled_shot_is_refused_enhancement_with_the_fix_named(tmp_path: Path):
    """`unknown` is not `not_singing`. In a music video an unlabelled Shot is likelier
    singing than not, and a wrong guess destroys lip-sync silently — so the refusal names
    the one-click fix rather than guessing in either direction."""
    client, store, _comfy = make_client(tmp_path)
    project = enhanced_shot_project(store, tmp_path, singing="unknown")

    response = enhance(client, project)

    assert response.status_code == 422
    assert "singing state" in response.json()["detail"]
    assert "Not singing" in response.json()["detail"]
    assert store.get(project.id).jobs == []


def test_the_singing_refusal_is_heard_before_the_missing_take_one(tmp_path: Path):
    """Mark-ready's precedent: the meaning-refusal comes before the mechanical one. Telling
    a singing Shot to render first would send the Director to spend GPU on a take this route
    would then refuse anyway."""
    client, store, _comfy = make_client(tmp_path)
    project = enhanced_shot_project(store, tmp_path, take="", singing="singing")

    detail = enhance(client, project).json()["detail"]

    assert "moves lip position" in detail
    assert "has not produced a take" not in detail


# ---------------------------------------------------------------------------------------------
# Watching and approving a take (FR-21)
# ---------------------------------------------------------------------------------------------


def get_take(client, project_id: str, shot_id: str, **kwargs):
    """The take stream. Ids only — the URL carries no path, by the route's own design."""
    return client.get(f"/api/projects/{project_id}/shots/{shot_id}/take", **kwargs)


def approve(client, project_id: str, shot_id: str, **kwargs):
    """Approve one Shot's latest take. No body, by design — see `approve_take` in app.py."""
    return client.post(f"/api/projects/{project_id}/shots/{shot_id}/approve", **kwargs)


def unapprove(client, project_id: str, shot_id: str, **kwargs):
    """Clear one Shot's approval. No body, for the same reason."""
    return client.post(f"/api/projects/{project_id}/shots/{shot_id}/unapprove", **kwargs)


def write_take_file(
    tmp_path: Path,
    project_id: str,
    filename: str = "shot_take-h3_00001.mp4",
    payload: bytes | None = None,
) -> bytes:
    """Put real bytes where `rendered_shot`'s `latest_output` resolves to.

    `make_client` pins `comfy_root` to `tmp_path / "comfy"`, and `land_take` records outputs
    under `music-video-producer/{project_id}/shots/`, so this is the one place on disk the take
    route may serve from. The payload is distinctive enough that a range assertion comparing
    slices cannot pass by accident on repeated content.
    """
    content = payload if payload is not None else bytes(range(256)) * 8
    target = (
        tmp_path / "comfy" / "output" / "music-video-producer" / project_id / "shots" / filename
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return content


def test_the_take_route_streams_the_latest_take_and_honours_range_requests(tmp_path: Path):
    """The scrub bar's whole contract, held to a real 206 rather than to a response class.

    Verified against Starlette 1.6.0 before this was written: `FileResponse` answers `Range`
    itself. The assertions are on the wire — status, `Content-Range`, the exact bytes — so a
    later change that swapped in a response type without range support fails here rather than
    shipping a player whose scrub bar silently rewinds to zero.
    """
    client, store, comfy = make_client(tmp_path)
    project_id, shot_id, _ = rendered_shot(client, store, comfy, "Watchable")
    payload = write_take_file(tmp_path, project_id)

    whole = get_take(client, project_id, shot_id)
    assert whole.status_code == 200, whole.text
    assert whole.content == payload
    assert whole.headers["content-type"] == "video/mp4"
    # Advertised on the plain 200, which is what tells the browser seeking is worth asking for.
    assert whole.headers["accept-ranges"] == "bytes"

    middle = get_take(client, project_id, shot_id, headers={"Range": "bytes=100-299"})
    assert middle.status_code == 206, middle.text
    assert middle.headers["content-range"] == f"bytes 100-299/{len(payload)}"
    assert middle.content == payload[100:300]

    # A suffix range and an open-ended one, because a scrub to the end asks in exactly these
    # shapes — and an unsatisfiable one is refused with the size rather than served empty.
    tail = get_take(client, project_id, shot_id, headers={"Range": "bytes=-64"})
    assert tail.status_code == 206
    assert tail.content == payload[-64:]
    open_ended = get_take(client, project_id, shot_id, headers={"Range": f"bytes={len(payload) - 32}-"})
    assert open_ended.status_code == 206
    assert open_ended.content == payload[-32:]
    beyond = get_take(client, project_id, shot_id, headers={"Range": f"bytes={len(payload) * 2}-"})
    assert beyond.status_code == 416

    # Watching spends nothing and writes nothing: the one render is still the only submission,
    # and the manifest did not move.
    assert len(comfy.prompts) == 1
    assert ProjectStore(tmp_path).get(project_id).shots[0].approved_output == ""


def test_the_take_route_refuses_by_name_when_there_is_nothing_to_play(tmp_path: Path):
    """Both 404 rows of the matrix, each with its own sentence rather than a broken player.

    The never-rendered case must not name a path — there is none — and the missing-file case
    must, because a manifest pointing at a file that is gone is usually a moved or cleared
    ComfyUI output directory and the path is how the Director tells which.
    """
    client, store, comfy = make_client(tmp_path)
    project = drafted_shot(store, "Never rendered")

    unrendered = get_take(client, project.id, "shot_first")
    assert unrendered.status_code == 404
    assert unrendered.json()["detail"] == TAKE_NOT_RENDERED_REFUSAL.format(
        shot="SHOT 01 (shot_first)"
    )

    # A take the manifest names and the disk does not hold.
    project_id, shot_id, _ = rendered_shot(client, store, comfy, "File gone")
    gone = get_take(client, project_id, shot_id)
    assert gone.status_code == 404
    recorded = ProjectStore(tmp_path).get(project_id).shots[0].latest_output
    assert gone.json()["detail"] == TAKE_MISSING_FILE_REFUSAL.format(
        shot=f"SHOT 01 ({shot_id})", path=recorded
    )
    assert recorded in gone.json()["detail"]

    assert get_take(client, project_id, "shot_absent").status_code == 404
    assert get_take(client, "proj_absent", shot_id).status_code == 404


def test_the_take_route_resolves_the_manifest_and_stays_inside_the_output_root(tmp_path: Path):
    """The confinement half of serve-by-ids.

    The URL cannot carry a path, so the one injection surface left is the manifest itself —
    `latest_output` is client-writable through the generic shots write. A value that walks out
    of ComfyUI's output directory must resolve to the same named 404 as a missing file, not to
    the file it walked to.
    """
    client, store, comfy = make_client(tmp_path)
    project_id, shot_id, _ = rendered_shot(client, store, comfy, "Escape attempt")
    secret = tmp_path / "secret.txt"
    secret.write_text("not a take", encoding="utf-8")

    stored = store.get(project_id)
    stored.shots[0].latest_output = "../../secret.txt"
    store.save(stored)

    response = get_take(client, project_id, shot_id)

    assert response.status_code == 404
    assert "secret.txt" in response.json()["detail"]
    assert "not a take" not in response.text


def test_approving_writes_the_watched_take_and_the_status_together_from_the_manifest(
    tmp_path: Path,
):
    """FR-21's write, and the evidence-not-claim rule in one test.

    What lands in `approved_output` is the server's own record of what rendered — byte-equal to
    `latest_output` — and nothing on the wire can substitute for it: the request carries a body
    and a query parameter that both name a different file, and both must be ignored, because the
    route binds no body at all. `approved_output` is about to become assembly's input, so a value
    a client could choose here would be a claim standing where evidence is required.
    """
    client, store, comfy = make_client(tmp_path)
    project_id, shot_id, _ = rendered_shot(client, store, comfy, "Approved")

    response = approve(
        client,
        project_id,
        shot_id,
        params={"approved_output": "takes/forged.mp4"},
        json={"approved_output": "takes/forged.mp4", "latest_output": "takes/forged.mp4"},
    )

    assert response.status_code == 200, response.text
    saved = ProjectStore(tmp_path).get(project_id).shots[0]
    assert saved.approved_output == saved.latest_output
    assert saved.latest_output.endswith("shot_take-h3_00001.mp4")
    assert saved.status == "approved"
    assert "forged" not in saved.approved_output
    # The response is the whole project, so the inspector redraws from one reply.
    assert response.json()["shots"][0]["approved_output"] == saved.approved_output
    # Approving spends nothing: the one render is still the only submission.
    assert len(comfy.prompts) == 1


def test_approving_twice_is_an_idempotent_no_op_that_rewrites_nothing(tmp_path: Path):
    """The matrix's own words: nothing rewritten.

    Byte-identical manifest, not merely equal fields — a second approve that re-saved the same
    values would move `updated_at`, and an unchanged manifest with a fresh timestamp collides
    with the next optimistic-concurrency check for no reason.
    """
    client, store, comfy = make_client(tmp_path)
    project_id, shot_id, _ = rendered_shot(client, store, comfy, "Twice")
    assert approve(client, project_id, shot_id).status_code == 200
    manifest = store.manifest_path(project_id)
    before = manifest.read_text(encoding="utf-8")

    again = approve(client, project_id, shot_id)

    assert again.status_code == 200
    assert manifest.read_text(encoding="utf-8") == before
    assert again.json()["shots"][0]["status"] == "approved"


def test_approving_refuses_a_shot_with_no_take(tmp_path: Path):
    """An approval is a decision about a specific piece of media, so no media, no decision.

    Twice: the ordinary draft Shot, and a `complete` status whose `latest_output` was emptied by
    hand — the status is not the evidence, the pointer is.
    """
    client, store, _ = make_client(tmp_path)
    project = drafted_shot(store, "Nothing rendered")

    response = approve(client, project.id, "shot_first")

    assert response.status_code == 422
    assert response.json()["detail"] == APPROVE_NO_TAKE_REFUSAL.format(
        shot="SHOT 01 (shot_first)"
    )
    assert ProjectStore(tmp_path).get(project.id).shots[0].approved_output == ""

    hollow = drafted_shot(store, "Hollow complete", status="complete", latest_output="")
    assert approve(client, hollow.id, "shot_first").status_code == 422
    assert ProjectStore(tmp_path).get(hollow.id).shots[0].status == "complete"


def test_approving_refuses_a_live_render_from_the_job_records_not_the_status(tmp_path: Path):
    """The 409, and the half of it a status-only check misses.

    The take on screen is about to be displaced, so approving now would attach the decision to
    whichever file lands next. The second case is the dangerous one: the status walked back to
    `complete` by hand through the generic shots write while the job record still says the render
    is out. The job records are the durable truth, and this must read them. Once the render
    lands, the identical request succeeds — which is what makes 409 the right class.
    """
    client, store, comfy = make_client(tmp_path)
    project = drafted_shot(store, "In flight", status="ready")
    submitted = submit_h3(client, project.id, "shot_first")
    assert submitted.status_code == 202

    # The ordinary case: the Shot itself says a render is out.
    queued = approve(client, project.id, "shot_first")
    assert queued.status_code == 409
    assert queued.json()["detail"] == APPROVE_IN_FLIGHT_REFUSAL.format(
        shot="SHOT 01 (shot_first)"
    )

    # ...and the same live render hidden behind a hand-walked status and a hand-set pointer.
    stored = store.get(project.id)
    stored.shots[0].status = "complete"
    stored.shots[0].latest_output = "takes/one.mp4"
    store.save(stored)

    hidden = approve(client, project.id, "shot_first")
    assert hidden.status_code == 409, "a hand-edited status hid a live render from the guard"
    assert hidden.json()["detail"] == APPROVE_IN_FLIGHT_REFUSAL.format(
        shot="SHOT 01 (shot_first)"
    )
    assert ProjectStore(tmp_path).get(project.id).shots[0].approved_output == ""

    # The conflict clears when the render lands, and the identical request then succeeds.
    job_id = submitted.json()["id"]
    land_take(client, comfy, project.id, job_id, "shot_first-h3_00001.mp4")
    assert approve(client, project.id, "shot_first").status_code == 200
    assert ProjectStore(tmp_path).get(project.id).shots[0].status == "approved"


def test_unapproving_clears_both_halves_and_returns_the_shot_to_complete(tmp_path: Path):
    """FR-21's reversal: `approved_output` cleared, status back to `complete`, nothing deleted."""
    client, store, comfy = make_client(tmp_path)
    project_id, shot_id, _ = rendered_shot(client, store, comfy, "Reversed")
    assert approve(client, project_id, shot_id).status_code == 200

    response = unapprove(client, project_id, shot_id)

    assert response.status_code == 200, response.text
    saved = ProjectStore(tmp_path).get(project_id).shots[0]
    assert saved.approved_output == ""
    assert saved.status == "complete"
    # The record of what rendered is not the decision, and withdrawing one keeps the other.
    assert saved.latest_output.endswith("shot_take-h3_00001.mp4")
    assert len(comfy.prompts) == 1


def test_unapproving_a_shot_that_is_not_approved_names_what_it_actually_is(tmp_path: Path):
    """The matrix's 422, with the Shot's real state in the sentence rather than a bare no."""
    client, store, comfy = make_client(tmp_path)
    project = drafted_shot(store, "Not approved")

    response = unapprove(client, project.id, "shot_first")

    assert response.status_code == 422
    assert response.json()["detail"] == UNAPPROVE_NOT_APPROVED_REFUSAL.format(
        shot="SHOT 01 (shot_first)", status="draft"
    )
    assert "draft" in response.json()["detail"]

    project_id, shot_id, _ = rendered_shot(client, store, comfy, "Complete, never approved")
    settled = unapprove(client, project_id, shot_id)
    assert settled.status_code == 422
    assert "complete" in settled.json()["detail"]
    assert ProjectStore(tmp_path).get(project_id).shots[0].status == "complete"


def test_unapproving_rescues_a_status_only_approval(tmp_path: Path):
    """The one way out of a state only hand-edits can make.

    A Shot with the `approved` status and no `approved_output` is refused by mark-ready (not its
    status class), by render-again (the approval refusal reads either signal) and by everything
    downstream — so un-approve must recognise the same either-signal definition render-again
    refuses by, or the Shot is stuck.
    """
    client, store, _ = make_client(tmp_path)
    project = drafted_shot(
        store, "Status only", status="approved", latest_output="takes/one.mp4"
    )
    assert render_again(client, project.id, "shot_first").status_code == 422

    response = unapprove(client, project.id, "shot_first")

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project.id).shots[0]
    assert saved.status == "complete"
    assert saved.approved_output == ""


def test_the_approve_route_is_the_one_writer_of_approval(tmp_path: Path):
    """AGENTS.md's rule and the spec's single-writer clause, asserted both ways.

    Behaviorally: a completion that lands a take — the likeliest place a second writer would
    creep in, because `apply_job_history` is already writing the Shot — moves `latest_output`
    and stops there. And in the source: exactly two assignments to `approved_output` exist in
    the whole package, both in `app.py`'s approve/unapprove pair, and exactly one write of the
    `approved` status. The scan is what fails when a well-meaning writer is added somewhere the
    behavioral half does not exercise.
    """
    client, store, comfy = make_client(tmp_path)
    project_id, _, _ = rendered_shot(client, store, comfy, "Completed, not approved")
    landed = ProjectStore(tmp_path).get(project_id).shots[0]
    assert landed.latest_output.endswith("shot_take-h3_00001.mp4")
    assert landed.status == "complete"
    assert landed.approved_output == ""

    package = Path("src/music_video_producer")
    assignments: dict[str, int] = {}
    status_writes: dict[str, int] = {}
    for source in package.rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        wrote = len(re.findall(r"\.approved_output\s*=[^=]", text))
        if wrote:
            assignments[source.name] = wrote
        wrote_status = len(re.findall(r"\.status\s*=\s*['\"]approved['\"]", text))
        if wrote_status:
            status_writes[source.name] = wrote_status
    assert assignments == {"app.py": 2}, assignments
    assert status_writes == {"app.py": 1}, status_writes
    # And the frontend never writes it either: the one assignment in the workspace is the empty
    # default a brand-new Shot is born with.
    workspace = (package / "web" / "assets" / "app.js").read_text(encoding="utf-8")
    contract = (package / "web" / "assets" / "api.js").read_text(encoding="utf-8")
    for name, text in (("app.js", workspace), ("api.js", contract)):
        writes = re.findall(r"approved_output\s*[:=]\s*(?!\s*['\"]['\"])", text)
        assert writes == [], (name, writes)


def test_while_approved_the_take_cannot_move_and_unapproval_is_the_way_back(tmp_path: Path):
    """The invariant, pinned end to end: `approved_output == latest_output` for the life of an
    approval — because everything that could move the pointer refuses an approved Shot — and
    un-approve is the one gate back to an ordinary re-renderable complete Shot.

    The round trip renders again after un-approving and lands a second take, which also proves
    the other single-writer half in motion: the completion moves `latest_output` and leaves the
    cleared approval cleared.
    """
    client, store, comfy = make_client(tmp_path)
    project_id, shot_id, _ = rendered_shot(client, store, comfy, "Invariant")
    assert approve(client, project_id, shot_id).status_code == 200
    approved = ProjectStore(tmp_path).get(project_id).shots[0]
    assert approved.approved_output == approved.latest_output

    # Every route that could displace the take refuses, and the refusal names the approval.
    reopen = render_again(client, project_id, shot_id)
    assert reopen.status_code == 422
    assert reopen.json()["detail"] == RENDER_AGAIN_APPROVED_REFUSAL.format(
        shot=f"SHOT 01 ({shot_id})"
    )
    assert mark_ready(client, project_id, shot_id).status_code == 422
    assert mark_draft(client, project_id, shot_id).status_code == 422
    assert submit_h3(client, project_id, shot_id).status_code == 422
    still = ProjectStore(tmp_path).get(project_id).shots[0]
    assert still.approved_output == still.latest_output
    assert still.status == "approved"
    assert len(comfy.prompts) == 1

    # Un-approve, and the same Shot is an ordinary complete Shot again: re-openable,
    # submittable, and its second take lands without resurrecting the withdrawn approval.
    assert unapprove(client, project_id, shot_id).status_code == 200
    assert render_again(client, project_id, shot_id).status_code == 200
    resubmitted = submit_h3(client, project_id, shot_id)
    assert resubmitted.status_code == 202
    assert len(comfy.prompts) == 2
    land_take(client, comfy, project_id, resubmitted.json()["id"], "shot_take-h3_00002.mp4")
    second = ProjectStore(tmp_path).get(project_id).shots[0]
    assert second.latest_output.endswith("shot_take-h3_00002.mp4")
    assert second.approved_output == ""
    assert second.status == "complete"

    # Approving now names the second take: what is approved is always the take that was watched.
    assert approve(client, project_id, shot_id).status_code == 200
    reapproved = ProjectStore(tmp_path).get(project_id).shots[0]
    assert reapproved.approved_output == second.latest_output


def test_approval_snapshots_the_window_and_unapproval_clears_it(tmp_path: Path):
    """AD-13's amendment, both halves of the write and the one-writer scan.

    The approval is a decision about one take *in one window* — assembly trims the take to
    the window, so a window edited after approval makes the approved file the wrong length
    for the plan. The snapshot is what makes that staleness decidable, and it must ride the
    same two writes approval itself rides: set at approve, cleared at un-approve, touched by
    nothing else in the package.
    """
    client, store, comfy = make_client(tmp_path)
    project_id, shot_id, _ = rendered_shot(client, store, comfy, "Snapshot")

    assert approve(client, project_id, shot_id).status_code == 200
    approved = ProjectStore(tmp_path).get(project_id).shots[0]
    assert approved.approved_start == approved.start == 0
    assert approved.approved_duration == approved.duration == 5

    # The reversal clears the snapshot with the approval it described: a snapshot outliving
    # its approval would make the *next* approval's staleness check read a window nobody
    # decided about.
    assert unapprove(client, project_id, shot_id).status_code == 200
    cleared = ProjectStore(tmp_path).get(project_id).shots[0]
    assert cleared.approved_start == 0
    assert cleared.approved_duration == 0

    # Move the window, re-approve: the snapshot is the window at the *moment of approval*,
    # not the one the first approval saw.
    stored = store.get(project_id)
    stored.shots[0].start = 2.5
    stored.shots[0].duration = 3.75
    store.save(stored)
    assert approve(client, project_id, shot_id).status_code == 200
    reapproved = ProjectStore(tmp_path).get(project_id).shots[0]
    assert reapproved.approved_start == 2.5
    assert reapproved.approved_duration == 3.75

    # The one-writer scan, mirroring the `approved_output` scan: exactly one set and one
    # clear of each snapshot field, both in app.py's approve/unapprove pair.
    package = Path("src/music_video_producer")
    writes: dict[str, int] = {}
    for source in package.rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        count = len(re.findall(r"\.approved_(?:start|duration)\s*=[^=]", text))
        if count:
            writes[source.name] = count
    assert writes == {"app.py": 4}, writes


def test_expansion_refuses_a_route_approved_shot(tmp_path: Path):
    """The downstream refusal, wired end to end rather than hand-built.

    Every existing expansion-refusal test constructs its approval by writing fields onto a Shot,
    which proves the guard reads the fields and nothing about whether the approve route writes
    the ones the guard reads. This one approves through the route and watches the same refusal
    fire on what the route wrote.
    """
    client, store, comfy = make_client(tmp_path, ExpandingDirector())
    project_id, shot_id, _ = rendered_shot(client, store, comfy, "Approved, then expanded")
    stored = store.get(project_id)
    stored.shots.append(Shot(id="shot_second", start=10, duration=6, prompt="", status="draft"))
    store.save(stored)
    assert approve(client, project_id, shot_id).status_code == 200

    response = client.post(f"/api/projects/{project_id}/director/expand")

    assert response.status_code == 200
    saved = ProjectStore(tmp_path).get(project_id)
    assert saved.shots[0].prompt == "A singer turns toward camera"
    assert saved.shots[0].approved_output == saved.shots[0].latest_output
    # The draft Shot beside it still moves, so the approval is per Shot and not a plan veto.
    assert saved.shots[1].prompt == "Prompt for shot_second at index 1"
    notice = saved.messages[-1].content
    assert "a render or a take already depends on the prompt" in notice
    assert shot_id in notice.split("already depends on the prompt")[1]
    assert len(comfy.prompts) == 1


def test_sage_attention_setting_patches_every_attention_node_at_submission(tmp_path: Path):
    """MVP_SAGE_ATTENTION's one choke point: configured, every submitted H3 payload's
    PathchSageAttentionKJ carries the configured kernel; unset (the default), the payload
    is byte-identical to the adapters' evidence value (`disabled`) — every other test in
    this file runs unset, so the whole suite is the byte-identity half."""
    settings = Settings(
        data_root=tmp_path, comfy_root=tmp_path / "comfy", sage_attention="auto"
    )
    store = ProjectStore(tmp_path)
    comfy = FakeComfy()
    app = create_app(settings=settings, store=store, comfy=comfy, director=FakeDirector())
    client = TestClient(app)
    project = store.create(Project(name="Sage"))
    media = store.media_dir(project.id)
    (media / "lead.png").write_bytes(b"png")
    project.assets = [Asset(id="asset_sage", name="Lead", kind="character", path="media/lead.png")]
    # A references shot: the text-only Director graph carries no attention node,
    # so the patch is asserted where the node exists (reference/keyframe/edit graphs).
    project.shots = [
        Shot(id="shot_sage", start=0, duration=4, prompt="p", asset_ids=["asset_sage"], status="ready")
    ]
    store.save(project)

    assert submit_h3(client, project.id, "shot_sage").status_code == 202
    attention = [
        node for node in comfy.prompts[-1].values()
        if node["class_type"] == "PathchSageAttentionKJ"
    ]
    assert attention and all(
        node["inputs"]["sage_attention"] == "auto" for node in attention
    )


def test_sections_route_sorts_refuses_overlap_and_reaches_the_expansion(tmp_path: Path):
    """The Director's section marks end to end: saved sorted, overlaps refused by name,
    and the expansion payload reads the section (label, shared prompt, lyric block) for a
    shot inside one — the fix for the wrong-verse lipsync the first batch rendered."""
    client, store, _comfy = make_client(tmp_path)
    project = store.create(Project(name="Sections"))
    project.song = Song(
        title="S", source="imported", path="m.mp3", duration=60.0,
        lyrics="[Verse]\nverse words here\n\n[Chorus]\nchorus hook words\n",
    )
    project.shots = [Shot(id="shot_c", start=27, duration=6, prompt="Glamour angle")]
    store.save(project)

    overlapping = client.put(f"/api/projects/{project.id}/sections", json={"sections": [
        {"label": "Verse", "start": 0, "duration": 30},
        {"label": "Chorus", "start": 24, "duration": 20},
    ]})
    assert overlapping.status_code == 422
    assert "may not overlap" in overlapping.json()["detail"]

    saved = client.put(f"/api/projects/{project.id}/sections", json={"sections": [
        {"label": "Chorus", "start": 24, "duration": 20, "prompt": "on the canopy bed"},
        {"label": "Verse", "start": 0, "duration": 24, "prompt": "at the mic"},
    ]})
    assert saved.status_code == 200
    labels = [s["label"] for s in saved.json()["sections"]]
    assert labels == ["Verse", "Chorus"]  # sorted by start on write

    from music_video_producer.timeline import shot_expansion_input

    stored = store.get(project.id)
    section = shot_expansion_input(stored, stored.shots[0])["shot"]["section"]
    assert section["label"] == "Chorus"
    assert section["prompt"] == "on the canopy bed"
    # Lyric text never rides the expansion payload (2026-08-19, twice-measured).
    assert "lyrics" not in section


def test_populate_adopts_the_models_sections_when_none_are_marked(tmp_path: Path):
    """Populate fills the section layer too (the Director's design): the model's
    structure proposal is repaired and adopted when the Director has marked nothing,
    the shots then tile inside those sections, and marked sections are never replaced."""
    director = PlanningDirector(
        shots=[(0, 6, "Open wide."), (30, 6, "Chorus glamour.")],
        sections=[
            ("Intro", 0, 8, ""),
            ("Verse", 8, 22, "at the standing mic"),
            ("Chorus", 30, 30, "on the canopy bed"),
        ],
    )
    client, store, _comfy = make_client(tmp_path, director=director)
    project = store.create(Project(name="Adopt"))
    project.song = Song(title="S", source="imported", path="m.mp3", duration=60.0)
    store.save(project)

    response = populate(client, project.id)
    assert response.status_code == 200, response.text
    saved = store.get(project.id)
    assert [section.label for section in saved.sections] == ["Intro", "Verse", "Chorus"]
    assert saved.sections[2].prompt == "on the canopy bed"
    # Shots tile inside sections: no shot straddles a section boundary.
    edges = {section.start for section in saved.sections} | {section.end for section in saved.sections}
    for shot in saved.shots:
        for edge in edges:
            assert not (shot.start < edge - 1e-6 < shot.start + shot.duration - 1e-6), (
                f"shot at {shot.start} straddles section edge {edge}"
            )

    # Marked sections survive a re-populate untouched.
    marked = store.get(project.id)
    marked.sections[0].prompt = "hand-edited"
    store.save(marked)
    assert populate(client, project.id).status_code == 200
    assert store.get(project.id).sections[0].prompt == "hand-edited"


def test_the_dp_pass_rides_the_expand_route_with_its_own_persona_and_input(tmp_path: Path):
    """focus=photography selects the DP persona over the identical machinery: the input is
    the camera-trimmed dp_input (sections inline, no citations, no lyrics), the system
    prompt is the DP job description, and the revised intents land through the same
    id-keyed guards — locked shots refused, story text replaced only where answered."""

    class RecordingDirector:
        def __init__(self):
            self.calls = []

        async def expand(self, *, expansion_input, system_prompt=None):
            self.calls.append({"input": expansion_input, "system": system_prompt})
            revised = type("ExpandedShot", (), {
                "shot_id": expansion_input["shots"][0]["shot_id"],
                "prompt": "Medium close-up, eye level, static shot: she holds the mic.",
            })()
            return type("ShotExpansion", (), {"message": "Composed.", "shots": [revised]})()

    from music_video_producer.dp_prompt import DP_SYSTEM_PROMPT

    director = RecordingDirector()
    client, store, _comfy = make_client(tmp_path, director=director)
    project = store.create(Project(name="DP"))
    project.song = Song(title="S", source="imported", path="m.mp3", duration=30.0)
    project.sections = [SongSection(label="Verse", start=0, duration=30, prompt="kinetic")]
    project.shots = [
        Shot(id="shot_a", start=0, duration=6, prompt="She walks to the mic."),
        Shot(id="shot_b", start=6, duration=6, prompt="Locked framing.", locked=True),
    ]
    store.save(project)

    response = client.post(f"/api/projects/{project.id}/director/expand?focus=photography")
    assert response.status_code == 200, response.text

    call = director.calls[0]
    assert call["system"] == DP_SYSTEM_PROMPT
    # The camera-trimmed input: sections inline on shots and as the plan's map; no
    # citations, no lyric sheet — the DP does not re-cast and does not need the words.
    assert call["input"]["shots"][0]["section"] == {"label": "Verse", "prompt": "kinetic"}
    assert call["input"]["sections"][0]["label"] == "Verse"
    assert "song" not in call["input"]
    assert "references" not in call["input"]["shots"][0]

    saved = store.get(project.id)
    assert saved.shots[0].prompt.startswith("Medium close-up")
    assert saved.shots[1].prompt == "Locked framing."  # the lock held

    # The story pass is untouched: no focus means the original persona and input.
    story = client.post(f"/api/projects/{project.id}/director/expand")
    assert story.status_code == 200
    assert director.calls[-1]["system"] is None
    assert "song" in director.calls[-1]["input"]
