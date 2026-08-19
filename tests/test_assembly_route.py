"""The assembly route, driven with real ffmpeg on synthesized media. No GPU, no ComfyUI.

These tests build what the manifest would hold after real renders — tiny colour-source
takes under the ComfyUI output root, a measured WAV as the imported song — then drive the
real routes: approve (which snapshots windows), assemble (which trims, joins, muxes and
verifies). The strongest claim here is the happy path's: the export exists, ffprobe
measures it within one frame of the song, the takes are byte-identical afterwards, and
`comfy.prompts` stayed empty the whole way.
"""

import json
import subprocess
import wave
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient

from music_video_producer.app import (
    ASSEMBLY_BUSY_REFUSAL,
    ASSEMBLY_NO_SONG_REFUSAL,
    ASSEMBLY_ORPHANED_ERROR,
    ASSEMBLY_RENDERS_OPEN_REFUSAL,
    ASSEMBLY_SONG_FILE_REFUSAL,
    create_app,
)
from music_video_producer.assembly import (
    ASSEMBLY_GAP_REFUSAL,
    ASSEMBLY_STALE_REFUSAL,
    ASSEMBLY_UNAPPROVED_REFUSAL,
)
from music_video_producer.comfy import ComfyError
from music_video_producer.config import Settings
from music_video_producer.models import RenderJob
from music_video_producer.store import ProjectStore


class FakeComfy:
    """The no-GPU double. Assembly must never touch it; `prompts` staying empty is the claim."""

    def __init__(self):
        self.prompts = []

    async def health(self):
        return {"online": True, "url": "http://fake"}

    async def submit(self, prompt, client_id=None):
        self.prompts.append(prompt)
        raise ComfyError("assembly must not submit to ComfyUI")

    async def queue(self):
        return {"queue_running": [], "queue_pending": []}

    async def history(self, prompt_id):
        raise ComfyError("assembly must not read ComfyUI history")


def make_client(tmp_path: Path):
    settings = Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy")
    store = ProjectStore(tmp_path)
    comfy = FakeComfy()
    app = create_app(settings=settings, store=store, comfy=comfy, director=object())
    return TestClient(app), store, comfy, app


def wav_bytes(seconds: float, rate: int = 8000) -> bytes:
    content = BytesIO()
    with wave.open(content, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(b"\x00\x00" * int(seconds * rate))
    return content.getvalue()


def synthesize_take(path: Path, seconds: float, size: str = "128x72", colour: str = "red"):
    """A real tiny take: colour source, 24 fps, yuv420p — what a render leaves, in miniature.

    Deliberately *longer* than the window that will consume it, the way grid alignment
    makes every real take run long.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"color=c={colour}:size={size}:rate=24",
            "-t", f"{seconds}", "-pix_fmt", "yuv420p",
            path.as_posix(),
        ],
        check=True,
        capture_output=True,
    )


def probe(path: Path, entries: str) -> str:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", entries, "-of", "csv=p=0", path.as_posix()],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def project_with_two_approved_takes(client, store, tmp_path: Path, *, song_seconds=8.0):
    """A project whose plan tiles an 8 s song with two approved, on-disk, snapshotted takes."""
    project_id = client.post("/api/projects", json={"name": "Assembly"}).json()["id"]
    upload = client.post(
        f"/api/projects/{project_id}/songs/upload",
        data={"title": "Assembly Song", "duration": "0"},
        files={"file": ("song.wav", wav_bytes(song_seconds), "audio/wav")},
    )
    assert upload.status_code == 200, upload.text

    shots_dir = (
        tmp_path / "comfy" / "output" / "music-video-producer" / project_id / "shots"
    )
    synthesize_take(shots_dir / "shot_a-h3_00001-audio.mp4", 4.458, colour="red")
    synthesize_take(
        shots_dir / "shot_b-h3_00001-audio.mp4", 4.458, size="192x108", colour="blue"
    )

    prefix = f"music-video-producer/{project_id}/shots"
    shots = [
        {
            "id": "shot_a",
            "start": 0,
            "duration": 4.0,
            "prompt": "Red room",
            "status": "complete",
            "latest_output": f"{prefix}/shot_a-h3_00001-audio.mp4",
        },
        {
            "id": "shot_b",
            "start": 4.0,
            "duration": 4.0,
            "prompt": "Blue room",
            "status": "complete",
            "latest_output": f"{prefix}/shot_b-h3_00001-audio.mp4",
        },
    ]
    saved = client.put(f"/api/projects/{project_id}/shots", json={"shots": shots})
    assert saved.status_code == 200, saved.text
    for shot_id in ("shot_a", "shot_b"):
        approved = client.post(f"/api/projects/{project_id}/shots/{shot_id}/approve")
        assert approved.status_code == 200, approved.text
    return project_id, shots_dir


def test_assembly_trims_joins_muxes_and_verifies_without_touching_comfy(tmp_path: Path):
    """The happy path, measured rather than asserted: a two-shot plan over an 8 s song
    becomes one export whose duration ffprobe puts within a frame of the song, whose
    geometry is the largest take present, whose takes are byte-identical afterwards, and
    whose journey queued nothing on ComfyUI."""
    client, store, comfy, app = make_client(tmp_path)
    project_id, shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    take_bytes_before = [
        (shots_dir / "shot_a-h3_00001-audio.mp4").read_bytes(),
        (shots_dir / "shot_b-h3_00001-audio.mp4").read_bytes(),
    ]

    response = client.post(f"/api/projects/{project_id}/assemble")

    assert response.status_code == 200, response.text
    body = response.json()
    export = tmp_path / "projects" / project_id / "media" / body["export"]
    assert body["export"] == "exports/assembly_00001.mp4"
    assert export.is_file()
    # FR-22: within one frame of the song, measured on the written file.
    measured = float(probe(export, "format=duration"))
    assert abs(measured - 8.0) <= 1 / 24
    assert abs(body["duration_seconds"] - measured) < 0.05
    assert body["song_seconds"] == 8.0
    # Normalization target: the largest-area take present (192x108 over 128x72).
    assert (body["width"], body["height"]) == (192, 108)
    assert probe(export, "stream=codec_type").splitlines() == ["video", "audio"]
    # 8 s at 24 fps on the cumulative grid.
    assert body["total_frames"] == 192
    assert body["clip_count"] == 2

    # The job settled locally: kind post, empty prompt_id by design, provenance recorded.
    job = body["job"]
    assert job["kind"] == "post"
    assert job["prompt_id"] == ""
    assert job["status"] == "complete"
    assert job["output_files"] == ["exports/assembly_00001.mp4"]
    assert job["inputs"] == [
        f"shot_a=music-video-producer/{project_id}/shots/shot_a-h3_00001-audio.mp4",
        f"shot_b=music-video-producer/{project_id}/shots/shot_b-h3_00001-audio.mp4",
    ]
    stored = store.get(project_id)
    assert [j.status for j in stored.jobs] == ["complete"]

    # No approved output was modified, no ComfyUI request was made, and the registry is
    # empty again.
    assert (shots_dir / "shot_a-h3_00001-audio.mp4").read_bytes() == take_bytes_before[0]
    assert (shots_dir / "shot_b-h3_00001-audio.mp4").read_bytes() == take_bytes_before[1]
    assert comfy.prompts == []
    assert app.state.live_assemblies == set()

    # The export streams through the existing media route.
    served = client.get(body["export_url"])
    assert served.status_code == 200
    assert served.content[:8] == export.read_bytes()[:8]

    # A second assembly is a new numbered file, never an overwrite.
    again = client.post(f"/api/projects/{project_id}/assemble")
    assert again.status_code == 200, again.text
    assert again.json()["export"] == "exports/assembly_00002.mp4"
    assert export.is_file()


def test_every_blocking_reason_lands_in_one_422_and_no_job_is_written(tmp_path: Path):
    """The comprehensive report: an unapproved shot and a gap against the song arrive in
    the same detail string, nothing is written, nothing is queued."""
    client, store, comfy, _app = make_client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "Blocked"}).json()["id"]
    upload = client.post(
        f"/api/projects/{project_id}/songs/upload",
        data={"title": "Song", "duration": "0"},
        files={"file": ("song.wav", wav_bytes(8.0), "audio/wav")},
    )
    assert upload.status_code == 200
    shots = [
        {"id": "shot_a", "start": 0, "duration": 4.0, "prompt": "Never rendered"},
    ]
    assert client.put(
        f"/api/projects/{project_id}/shots", json={"shots": shots}
    ).status_code == 200

    response = client.post(f"/api/projects/{project_id}/assemble")

    assert response.status_code == 422
    detail = response.json()["detail"]
    label = "SHOT 01 (shot_a)"
    assert ASSEMBLY_UNAPPROVED_REFUSAL.format(shot=label) in detail
    assert ASSEMBLY_GAP_REFUSAL.format(
        start=4.0, end=8.0, before=label, after="the end of the song"
    ) in detail
    assert store.get(project_id).jobs == []
    assert comfy.prompts == []
    assert not (tmp_path / "projects" / project_id / "media" / "exports").exists()


def test_a_window_moved_after_approval_is_refused_stale_by_id(tmp_path: Path):
    """AD-13 end to end: the approve route snapshotted the window; moving the window makes
    assembly refuse that shot with both windows in the sentence."""
    client, store, _comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)

    stored = store.get(project_id)
    stored.shots[1].start = 4.5
    stored.shots[1].duration = 3.5
    store.save(stored)

    response = client.post(f"/api/projects/{project_id}/assemble")

    assert response.status_code == 422
    assert ASSEMBLY_STALE_REFUSAL.format(
        shot="SHOT 02 (shot_b)",
        approved_start=4.0,
        approved_duration=4.0,
        start=4.5,
        duration=3.5,
    ) in response.json()["detail"]


def test_state_conflicts_are_409s_and_an_orphaned_assembly_is_healed(tmp_path: Path):
    """The three job-shaped gates: open renders refuse, a live assembly refuses, and a
    `running` local job with no process behind it — a restart's leftover — is healed to
    `error` instead of blocking every future assembly."""
    client, store, _comfy, app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)

    # An open ComfyUI render: 409, count named.
    stored = store.get(project_id)
    stored.jobs.append(RenderJob(kind="flux", status="running", prompt_id="p-9"))
    store.save(stored)
    busy_renders = client.post(f"/api/projects/{project_id}/assemble")
    assert busy_renders.status_code == 409
    assert busy_renders.json()["detail"] == ASSEMBLY_RENDERS_OPEN_REFUSAL.format(count=1)

    # A live assembly (its id is in the in-process registry): 409.
    stored = store.get(project_id)
    stored.jobs[-1].status = "complete"
    stored.jobs.append(
        RenderJob(id="job_live", kind="post", status="running", target_id="assembly")
    )
    store.save(stored)
    app.state.live_assemblies.add("job_live")
    busy_assembly = client.post(f"/api/projects/{project_id}/assemble")
    assert busy_assembly.status_code == 409
    assert busy_assembly.json()["detail"] == ASSEMBLY_BUSY_REFUSAL

    # The same job with no live process behind it is an orphan: healed, then the request
    # proceeds to a real export.
    app.state.live_assemblies.discard("job_live")
    healed = client.post(f"/api/projects/{project_id}/assemble")
    assert healed.status_code == 200, healed.text
    jobs = {job.id: job for job in store.get(project_id).jobs}
    assert jobs["job_live"].status == "error"
    assert jobs["job_live"].error == ASSEMBLY_ORPHANED_ERROR


def test_the_song_gates_refuse_before_any_work(tmp_path: Path):
    client, store, _comfy, _app = make_client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "No song"}).json()["id"]

    no_song = client.post(f"/api/projects/{project_id}/assemble")
    assert no_song.status_code == 422
    assert no_song.json()["detail"] == ASSEMBLY_NO_SONG_REFUSAL

    upload = client.post(
        f"/api/projects/{project_id}/songs/upload",
        data={"title": "Song", "duration": "0"},
        files={"file": ("song.wav", wav_bytes(8.0), "audio/wav")},
    )
    assert upload.status_code == 200
    recorded = store.get(project_id).song.path
    (tmp_path / "projects" / project_id / recorded).unlink()

    gone = client.post(f"/api/projects/{project_id}/assemble")
    assert gone.status_code == 422
    assert gone.json()["detail"] == ASSEMBLY_SONG_FILE_REFUSAL.format(path=recorded)


def synthesize_two_part_take(path: Path, first: str, first_seconds: float, second: str,
                             second_seconds: float, size: str = "128x72"):
    """A take whose opening and body are different solid colours — what makes 'which slice
    did assembly cut' a measurable question rather than a trusted one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"color=c={first}:size={size}:rate=24",
            "-f", "lavfi", "-i", f"color=c={second}:size={size}:rate=24",
            "-filter_complex",
            (
                f"[0:v]trim=duration={first_seconds}[a];"
                f"[1:v]trim=duration={second_seconds}[b];"
                f"[a][b]concat=n=2:v=1[v]"
            ),
            "-map", "[v]", "-pix_fmt", "yuv420p", path.as_posix(),
        ],
        check=True,
        capture_output=True,
    )


def first_pixel(path: Path, at_seconds: float) -> tuple[int, int, int]:
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-ss", f"{at_seconds}", "-i", path.as_posix(),
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        check=True,
        capture_output=True,
    )
    return tuple(result.stdout[:3])


def test_the_offset_selects_which_slice_of_the_take_fills_the_window(tmp_path: Path):
    """The margin's whole point, measured in pixels: a take that opens with a one-second
    green lead before its blue body. With the recorded lead as the offset, the window is
    the blue body; nudged back to zero, the green lead is what plays. Same file, same
    approval — the cut moved, exactly as the ruling asks."""
    client, store, _comfy, _app = make_client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "Offset"}).json()["id"]
    upload = client.post(
        f"/api/projects/{project_id}/songs/upload",
        data={"title": "Song", "duration": "0"},
        files={"file": ("song.wav", wav_bytes(4.0), "audio/wav")},
    )
    assert upload.status_code == 200
    shots_dir = (
        tmp_path / "comfy" / "output" / "music-video-producer" / project_id / "shots"
    )
    synthesize_two_part_take(
        shots_dir / "shot_a-h3_00001-audio.mp4", "green", 1.0, "blue", 5.0
    )
    prefix = f"music-video-producer/{project_id}/shots"
    shots = [{
        "id": "shot_a", "start": 0, "duration": 4.0, "prompt": "Lead then body",
        "status": "complete",
        "latest_output": f"{prefix}/shot_a-h3_00001-audio.mp4",
        "latest_take_lead": 1.0,
    }]
    assert client.put(
        f"/api/projects/{project_id}/shots", json={"shots": shots}
    ).status_code == 200
    assert client.post(f"/api/projects/{project_id}/shots/shot_a/approve").status_code == 200

    synced = client.post(f"/api/projects/{project_id}/assemble")
    assert synced.status_code == 200, synced.text
    export = tmp_path / "projects" / project_id / "media" / synced.json()["export"]
    red, green, blue = first_pixel(export, 0.5)
    assert blue > 180 and green < 100, (red, green, blue)  # the body, not the lead

    # The nudge is editable after approval by design — it selects a slice of the approved
    # file, the file itself immovable. Pull the cut back to the take's very start.
    stored = json.loads(store.get(project_id).model_dump_json())["shots"]
    stored[0]["trim_nudge"] = -1.0
    assert client.put(
        f"/api/projects/{project_id}/shots", json={"shots": stored}
    ).status_code == 200

    nudged = client.post(f"/api/projects/{project_id}/assemble")
    assert nudged.status_code == 200, nudged.text
    export2 = tmp_path / "projects" / project_id / "media" / nudged.json()["export"]
    red, green, blue = first_pixel(export2, 0.5)
    assert green > 100 and blue < 100, (red, green, blue)  # the green lead now plays


def synthesize_toned_take(path: Path, seconds: float, size: str = "128x72"):
    """A take carrying a loud sine on its audio track — the thing acceptance lets through."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"color=c=red:size={size}:rate=24",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
            "-t", f"{seconds}", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            path.as_posix(),
        ],
        check=True,
        capture_output=True,
    )


def mean_volume_db(path: Path) -> float:
    result = subprocess.run(
        [
            "ffmpeg", "-v", "info", "-i", path.as_posix(),
            "-af", "volumedetect", "-f", "null", "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    import re as _re

    match = _re.search(r"mean_volume:\s*(-?[\d.]+) dB", result.stderr)
    assert match, result.stderr[-500:]
    return float(match.group(1))


def test_accepted_take_audio_reaches_the_export_and_unaccepted_audio_never_does(
    tmp_path: Path,
):
    """The Director's rule, measured in decibels: a silent song over a take with a loud
    sine. Untouched (default muted), the export is silence — byte-for-byte the song-only
    behaviour. Accepted, the sine is in the export. Same take, same approval; only the
    acceptance moved."""
    client, store, _comfy, _app = make_client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "Mix"}).json()["id"]
    upload = client.post(
        f"/api/projects/{project_id}/songs/upload",
        data={"title": "Silence", "duration": "0"},
        files={"file": ("song.wav", wav_bytes(4.0), "audio/wav")},
    )
    assert upload.status_code == 200
    shots_dir = (
        tmp_path / "comfy" / "output" / "music-video-producer" / project_id / "shots"
    )
    synthesize_toned_take(shots_dir / "shot_a-h3_00001-audio.mp4", 4.6)
    prefix = f"music-video-producer/{project_id}/shots"
    shots = [{
        "id": "shot_a", "start": 0, "duration": 4.0, "prompt": "Toned",
        "status": "complete",
        "latest_output": f"{prefix}/shot_a-h3_00001-audio.mp4",
    }]
    assert client.put(
        f"/api/projects/{project_id}/shots", json={"shots": shots}
    ).status_code == 200
    assert client.post(f"/api/projects/{project_id}/shots/shot_a/approve").status_code == 200

    muted = client.post(f"/api/projects/{project_id}/assemble")
    assert muted.status_code == 200, muted.text
    silent_export = tmp_path / "projects" / project_id / "media" / muted.json()["export"]
    assert mean_volume_db(silent_export) < -70  # digital silence, song-only

    stored = json.loads(store.get(project_id).model_dump_json())["shots"]
    stored[0]["mix_take_audio"] = True
    assert client.put(
        f"/api/projects/{project_id}/shots", json={"shots": stored}
    ).status_code == 200

    mixed = client.post(f"/api/projects/{project_id}/assemble")
    assert mixed.status_code == 200, mixed.text
    loud_export = tmp_path / "projects" / project_id / "media" / mixed.json()["export"]
    assert mean_volume_db(loud_export) > -30  # the sine came through
    # Verification still holds: one video, one audio, song-length.
    assert probe(loud_export, "stream=codec_type").splitlines() == ["video", "audio"]


def test_a_take_too_short_for_its_window_is_refused_before_any_work(tmp_path: Path):
    """A take physically shorter than its window used to surface as a mid-pipeline
    verification failure; the probe-fed offset check turns it into a plan refusal —
    422, every number named, no job written, nothing spent."""
    client, store, _comfy, _app = make_client(tmp_path)
    project_id, shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    # Replace shot_b's approved take with one that cannot fill its 4 s window. The
    # manifest still names the same path, so approval and staleness both hold.
    synthesize_take(shots_dir / "shot_b-h3_00001-audio.mp4", 2.0, size="192x108")

    response = client.post(f"/api/projects/{project_id}/assemble")

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "runs off the end of its take" in detail
    assert "2.000" in detail and "4.000" in detail  # the take and the window, named
    assert store.get(project_id).jobs == []
    assert not (tmp_path / "projects" / project_id / "media" / "exports").exists()


def test_a_failed_verification_is_reported_with_numbers_and_leaves_no_export(
    tmp_path: Path, monkeypatch
):
    """FR-22's honesty row: the wiring from a failed verification to the 502, the job's
    error, and an empty exports/. The verdict itself is unit-tested in
    `verification_problems`; here it is forced, because a take that passes the up-front
    probes yet writes a bad export takes a genuinely corrupt encoder to produce."""
    client, store, _comfy, app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    import music_video_producer.app as app_module

    monkeypatch.setattr(
        app_module,
        "verification_problems",
        lambda song, measured, streams: [
            f"The export runs {measured:.3f}s but the song runs {song:.3f}s — forced."
        ],
    )

    response = client.post(f"/api/projects/{project_id}/assemble")

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "verification" in detail
    assert "8.000" in detail  # the song's measured seconds reach the sentence
    jobs = store.get(project_id).jobs
    assert [job.status for job in jobs] == ["error"]
    assert jobs[0].error == detail
    exports = tmp_path / "projects" / project_id / "media" / "exports"
    assert list(exports.glob("*.mp4")) == []
    assert list(exports.glob(".work-*")) == []
    assert app.state.live_assemblies == set()
