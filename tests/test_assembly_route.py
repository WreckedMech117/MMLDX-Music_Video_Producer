"""The assembly route, driven with real ffmpeg on synthesized media. No GPU, no ComfyUI.

These tests build what the manifest would hold after real renders — tiny colour-source
takes under the ComfyUI output root, a measured WAV as the imported song — then drive the
real routes: approve (which snapshots windows), assemble (which trims, joins, muxes and
verifies). The strongest claim here is the happy path's: the export exists, ffprobe
measures it within one frame of the song, the takes are byte-identical afterwards, and
`comfy.prompts` stayed empty the whole way.
"""

import asyncio
import json
import math
import struct
import subprocess
import wave
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient

# The read-modify-write window, held open on purpose. The same gate `test_api` drives the four
# other sites of this defect with, so the export's job-record save is provably the same test.
from race_support import Interleaved

from music_video_producer import app as app_module
from music_video_producer.app import (
    ASSEMBLY_BUSY_REFUSAL,
    ASSEMBLY_NO_SONG_REFUSAL,
    ASSEMBLY_ORPHANED_ERROR,
    ASSEMBLY_RENDERS_OPEN_REFUSAL,
    ASSEMBLY_SONG_FILE_REFUSAL,
    BINDING_WITHOUT_ENVELOPE_REFUSAL,
    SHOT_EFFECT_STACK_LIMIT,
    SHOT_EFFECTS_TOO_MANY_REFUSAL,
    SONG_ENVELOPE_SONG_CHANGED,
    create_app,
)
from music_video_producer.assembly import (
    ASSEMBLY_GAP_REFUSAL,
    ASSEMBLY_STALE_REFUSAL,
    ASSEMBLY_UNAPPROVED_REFUSAL,
)
from music_video_producer.batch import render_timing_summary
from music_video_producer.comfy import ComfyError
from music_video_producer.config import Settings
from music_video_producer.models import EffectSpec, Project, RenderJob, shot_label
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


def tone_wav_bytes(seconds: float, amplitude: float = 0.02, rate: int = 44100) -> bytes:
    """A quiet 220 Hz sine as a WAV: a song with a *level* to normalize, which digital
    silence does not have. Deliberately far below full scale, so a loudness pass that runs
    has somewhere to move it and one that does not leaves it where it is."""
    content = BytesIO()
    with wave.open(content, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        frames = bytearray()
        for index in range(int(seconds * rate)):
            value = int(amplitude * 32767 * math.sin(2 * math.pi * 220 * index / rate))
            frames += struct.pack("<h", value)
        target.writeframes(bytes(frames))
    return content.getvalue()


def project_with_two_approved_takes(
    client, store, tmp_path: Path, *, song_seconds=8.0, song_bytes: bytes | None = None
):
    """A project whose plan tiles an 8 s song with two approved, on-disk, snapshotted takes."""
    project_id = client.post("/api/projects", json={"name": "Assembly"}).json()["id"]
    upload = client.post(
        f"/api/projects/{project_id}/songs/upload",
        data={"title": "Assembly Song", "duration": "0"},
        files={
            "file": (
                "song.wav",
                wav_bytes(song_seconds) if song_bytes is None else song_bytes,
                "audio/wav",
            )
        },
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


def test_a_manifest_edited_past_the_stack_cap_is_refused_by_the_cap_and_not_by_ffmpeg(
    tmp_path: Path,
):
    """The third door. Both write routes cap a stack; a manifest edited by hand does not.

    `replace_shot_effects` caps before it validates, and `_adopt_shot_effects` caps a stack
    arriving on a Shot the store does not hold — so no client can build one of these. A manifest
    edited by hand can, and so can one written before either cap existed, and the failure it used
    to produce was the least useful in this application: the chain becomes a single `-vf`
    argument, Windows refuses a command line past 32,767 characters, and the `FileNotFoundError`
    that comes back was reported as *"ffmpeg is not installed or not on PATH"* — sending a
    Director to reinstall a binary that was working the whole time, over an argv this application
    built.

    Measured 2026-08-25: 985 grain cards build 32,725 characters and export; 1,200 build 40,060
    and do not. The cap is 32, so this is three orders of magnitude past anything a Director can
    reach through the interface.

    The check registers into `EXPORT_PLAN_CHECKS`, which story 9.6 built to be appended to — so
    it joins the one report every other plan fault joins rather than raising alone. Asserted here
    beside a *second* fault, because being told one thing at a time is what that registry exists
    to prevent.
    """
    client, store, _comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)

    # Hand-edit the manifest the way nothing routed can: past the cap, on the first Shot.
    project = store.get(project_id)
    oversized = [
        EffectSpec(effect="grain", parameters={"strength": 10.0})
        for _ in range(SHOT_EFFECT_STACK_LIMIT + 1)
    ]
    project.shots[0].effects = oversized
    # And a second, independent fault, so the report has to carry both.
    project.shots[1].effects = [EffectSpec(effect="nope_not_an_effect", parameters={})]
    store.save(project)

    refused = client.post(f"/api/projects/{project_id}/assemble", json={})
    assert refused.status_code == 422, refused.text
    detail = refused.json()["detail"]

    # The cap's own sentence, naming the Shot as the timeline names it.
    assert SHOT_EFFECTS_TOO_MANY_REFUSAL.format(
        limit=SHOT_EFFECT_STACK_LIMIT, count=SHOT_EFFECT_STACK_LIMIT + 1
    ) in detail
    assert shot_label(store.get(project_id), store.get(project_id).shots[0]) in detail

    # Both faults in one answer, not the first one alone.
    assert "nope_not_an_effect" in detail, detail

    # Nothing ffmpeg said, and nothing about an installation.
    assert "not installed" not in detail
    assert "not on PATH" not in detail

    # And nothing was written or queued.
    assert store.get(project_id).jobs == []


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


def integrated_lufs(path: Path) -> float:
    """EBU R128 integrated loudness of a file's first audio stream, in LUFS.

    `loudnorm` in analysis-only form: `print_format=json` writes the measurement to stderr
    and `-f null -` throws the audio away, so this measures without normalizing anything.
    `input_i` is the integrated figure — the one a loudness *target* would move, and
    therefore the one that says whether the export re-mastered the song.
    """
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-i", path.as_posix(),
            "-map", "0:a:0", "-af", "loudnorm=print_format=json", "-f", "null", "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    import re as _re

    match = _re.search(r'"input_i"\s*:\s*"(-?[\d.]+)"', result.stderr)
    assert match, result.stderr[-800:]
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


# ------------------------------------------------------------------------------------------
# Export presets and progress (Phase 4.2).
# ------------------------------------------------------------------------------------------


def test_an_unknown_preset_is_refused_before_any_ffmpeg_process_can_exist(
    tmp_path: Path, monkeypatch
):
    """The `Literal` does this, and doing it there is the point: request validation runs
    before the route body, so the refusal cannot be reached by an ffmpeg invocation however
    the route is later rearranged. Proven by making *every* subprocess launch an exception
    and asking for a preset that does not exist — the 422 still comes back, no job is
    written, no export directory appears, and nothing is queued on ComfyUI."""
    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)

    def refuse_to_run(*args, **kwargs):
        raise AssertionError("a process was launched for an unknown preset")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse_to_run)

    for unknown in ["final", "MASTER", "", "draft ", "high-quality"]:
        response = client.post(
            f"/api/projects/{project_id}/assemble", json={"preset": unknown}
        )
        assert response.status_code == 422, (unknown, response.text)
        assert "preset" in response.text

    assert store.get(project_id).jobs == []
    assert comfy.prompts == []
    assert not (tmp_path / "projects" / project_id / "media" / "exports").exists()


def test_the_default_request_is_draft_and_runs_the_drafts_own_commands(
    tmp_path: Path, monkeypatch
):
    """What an existing Assemble click produces, asserted at the argv the route actually
    passes. A body-less request and an explicit `draft` both build with `DRAFT_PRESET`, and
    the commands that reach ffmpeg carry veryfast/CRF 18 and no loudness filter anywhere."""
    import music_video_producer.app as app_module
    from music_video_producer.assembly import DRAFT_PRESET, MASTER_PRESET

    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)

    presets: list[object] = []
    commands: list[list[str]] = []
    real_trim, real_concat = app_module.trim_args, app_module.concat_args

    def record_trim(*args, preset=DRAFT_PRESET, **kwargs):
        presets.append(preset)
        built = real_trim(*args, preset=preset, **kwargs)
        commands.append(built)
        return built

    def record_concat(*args, preset=DRAFT_PRESET, **kwargs):
        presets.append(preset)
        built = real_concat(*args, preset=preset, **kwargs)
        commands.append(built)
        return built

    monkeypatch.setattr(app_module, "trim_args", record_trim)
    monkeypatch.setattr(app_module, "concat_args", record_concat)

    bodyless = client.post(f"/api/projects/{project_id}/assemble")
    assert bodyless.status_code == 200, bodyless.text
    assert bodyless.json()["preset"] == "draft"
    assert presets == [DRAFT_PRESET, DRAFT_PRESET, DRAFT_PRESET]
    for command in commands:
        assert command[command.index("-c:v") + 1] in {"libx264", "copy"}
        assert not any("loudnorm" in argument for argument in command)
    trims = [command for command in commands if "-frames:v" in command]
    assert len(trims) == 2
    for trim in trims:
        assert trim[trim.index("-preset") + 1] == "veryfast"
        assert trim[trim.index("-crf") + 1] == "18"

    presets.clear()
    named = client.post(f"/api/projects/{project_id}/assemble", json={"preset": "draft"})
    assert named.status_code == 200, named.text
    assert named.json()["preset"] == "draft"
    assert presets == [DRAFT_PRESET, DRAFT_PRESET, DRAFT_PRESET]

    # And the chosen preset reaches *both* builders — the trims and the join. A route that
    # honoured the preset only at the join would still produce a loudness-normalized file
    # while encoding every frame of it at the draft's settings, and the export would look
    # exactly like a master from the outside.
    presets.clear()
    commands.clear()
    delivered = client.post(
        f"/api/projects/{project_id}/assemble", json={"preset": "master"}
    )
    assert delivered.status_code == 200, delivered.text
    assert delivered.json()["preset"] == "master"
    assert presets == [MASTER_PRESET, MASTER_PRESET, MASTER_PRESET]
    delivered_trims = [command for command in commands if "-frames:v" in command]
    assert len(delivered_trims) == 2
    for trim in delivered_trims:
        assert trim[trim.index("-preset") + 1] == "slow"
        assert trim[trim.index("-crf") + 1] == "16"
    assert comfy.prompts == []


def test_a_failed_stage_still_carries_ffmpegs_own_words_through_the_progress_reader(
    tmp_path: Path, monkeypatch
):
    """The other half of the drain: what it reads has to reach the Director.

    The progress branch reads stdout itself, so stderr is collected by a task rather than by
    `communicate()`, and a branch that collected nothing would look perfectly healthy right
    up until something failed — at which point the 502 and the job's `error` would say "no
    error output" about a run ffmpeg explained in detail. Forced here by pointing a trim at a
    file that is not there and asserting ffmpeg's own sentence comes back."""
    import music_video_producer.app as app_module

    client, store, comfy, app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    real_trim = app_module.trim_args

    def broken_trim(source, dest, *args, **kwargs):
        return real_trim(tmp_path / "nothing-here.mp4", dest, *args, **kwargs)

    monkeypatch.setattr(app_module, "trim_args", broken_trim)

    response = client.post(f"/api/projects/{project_id}/assemble")

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "trim" in detail
    assert "no error output" not in detail
    assert "nothing-here.mp4" in detail  # ffmpeg's own stderr, not a summary of it
    jobs = store.get(project_id).jobs
    assert [job.status for job in jobs] == ["error"]
    assert jobs[0].error == detail
    assert comfy.prompts == []
    assert app.state.live_assemblies == set()


def test_the_export_reader_drains_stderr_concurrently_with_the_progress_stream():
    """The deadlock guard, asserted on the code because its failure is a hang rather than a
    wrong answer. ffmpeg writes diagnostics to stderr throughout a run — a 4 s trim at debug
    verbosity produces ~60 KB, which is the order of a pipe buffer — so a parent that reads
    stdout to exhaustion before touching stderr stops a long export dead, with neither side
    able to move. The drain task must therefore be created *before* the stdout loop, not
    awaited after it as a second sequential read.

    **The reader is found by name across the package, not sliced out of `create_app` between two
    markers.** This used to cut from `    async def run_tool(` to the next `\\n    @app.`, which
    made what got read depend on the file the helper sits in and on whichever route happened to
    be declared after it; delete that neighbouring decorator and the slice grows to the end of
    the factory, where `communicate()` and both of the calls indexed below appear in other
    routes and this passes while proving nothing about the reader. `function_source` returns the
    one definition of this name anywhere in `src/music_video_producer/`, and fails outright if
    the package grows a second one."""
    from package_source import function_source

    reader = function_source("run_tool")

    create = reader.index("asyncio.create_task(process.stderr.read())")
    loop = reader.index("await process.stdout.readline()")
    assert create < loop, "stderr is drained after the stdout loop, which can deadlock"
    # And the no-progress branch keeps `communicate()`, which does the same drain itself.
    assert "await process.communicate()" in reader


def test_the_master_preset_exports_a_verified_file_and_preserves_the_songs_loudness(
    tmp_path: Path,
):
    """The one claim worth making live, and it costs no GPU: a real master export of real
    (synthetic) clips over a song with a measurable level.

    **The Director's ruling of 2026-08-20**: the export's audio track *is* their master song,
    so a delivery build must not re-master it. This preset used to carry
    `loudnorm=I=-16:TP=-1.5:LRA=11` and this test used to assert the lift it produced; the
    assertion is inverted deliberately, as a spec change. Measured on the written files, both
    presets now land the export within a fraction of a LU of the song that went in — where
    the old master preset would have hauled this -38 LUFS tone up to -16.

    **The verification is unchanged** — the export is still within one frame of the song and
    still carries exactly one video and one audio stream. The 48 kHz conform is the master's
    only audio filter, and it is measured *as a rate*, not as a level: the draft, which has no
    audio filter at all, comes out at the song's own 44.1 kHz.
    """
    client, store, comfy, app = make_client(tmp_path)
    project_id, shots_dir = project_with_two_approved_takes(
        client, store, tmp_path, song_bytes=tone_wav_bytes(8.0)
    )
    take_bytes_before = (shots_dir / "shot_a-h3_00001-audio.mp4").read_bytes()
    song_file = tmp_path / "projects" / project_id / store.get(project_id).song.path
    song_lufs = integrated_lufs(song_file)
    # The tone is deliberately far below any broadcast target (it measures about -38 LUFS),
    # so a normalizer that ran would be unmissable: the old -16 LUFS target was more than
    # 20 LU away from where this song sits.
    assert song_lufs < -25, song_lufs

    draft = client.post(f"/api/projects/{project_id}/assemble", json={"preset": "draft"})
    assert draft.status_code == 200, draft.text
    draft_export = tmp_path / "projects" / project_id / "media" / draft.json()["export"]

    master = client.post(f"/api/projects/{project_id}/assemble", json={"preset": "master"})

    assert master.status_code == 200, master.text
    body = master.json()
    assert body["preset"] == "master"
    export = tmp_path / "projects" / project_id / "media" / body["export"]
    assert export.is_file()

    # The existing verification, re-run by hand on the master's own file.
    song_seconds = store.get(project_id).song.duration
    measured = float(probe(export, "format=duration"))
    assert abs(measured - 8.0) <= 1 / 24, measured
    assert abs(measured - song_seconds) <= 1 / 24, (measured, song_seconds)
    assert probe(export, "stream=codec_type").splitlines() == ["video", "audio"]
    assert (body["width"], body["height"]) == (192, 108)
    assert body["total_frames"] == 192
    # Delivery details: faststart is a container fact, and the conform lands 48 kHz — while
    # the filterless draft carries the song's own rate through, which is what says the
    # conform is a deliberate delivery choice rather than a leftover of a filter chain.
    assert probe(export, "stream=sample_rate").splitlines()[0] == "48000"
    assert probe(draft_export, "stream=sample_rate").splitlines()[0] == "44100"

    # The ruling, measured. Tolerance is 0.5 LU: the AAC re-encode both presets have always
    # performed moves the integrated figure by a few hundredths, and a loudness *target* of
    # any kind would move it by whole LUs. Numbers are carried into the failure message so a
    # regression reports how far it drifted rather than merely that it did.
    master_lufs = integrated_lufs(export)
    draft_lufs = integrated_lufs(draft_export)
    assert abs(master_lufs - song_lufs) <= 0.5, (song_lufs, master_lufs)
    assert abs(draft_lufs - song_lufs) <= 0.5, (song_lufs, draft_lufs)
    assert abs(master_lufs - draft_lufs) <= 0.5, (draft_lufs, master_lufs)
    # And the same claim in the cruder unit, which is immune to loudnorm's own analysis
    # being the thing measuring it: the master is not louder than the draft.
    assert mean_volume_db(export) < mean_volume_db(draft_export) + 1.0

    # AD-9, on both new paths.
    assert comfy.prompts == []
    assert app.state.live_assemblies == set()
    assert (shots_dir / "shot_a-h3_00001-audio.mp4").read_bytes() == take_bytes_before


def test_the_export_writes_its_own_progress_onto_the_local_job(tmp_path: Path):
    """The bar's whole data source. The route holds its request open for the length of the
    export and the AD-1 poll deliberately ignores a job with no prompt id, so the only thing
    that can report is the job itself — and reporting means writing *during* the run, not
    only at the end. Every manifest write is captured and the progress values read back."""
    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)

    seen: list[int] = []
    real_save = store.save

    def recording_save(project):
        for job in project.jobs:
            if job.kind == "post" and not job.prompt_id:
                seen.append(job.progress)
        return real_save(project)

    store.save = recording_save
    try:
        response = client.post(f"/api/projects/{project_id}/assemble")
    finally:
        store.save = real_save

    assert response.status_code == 200, response.text
    assert response.json()["job"]["progress"] == 100
    assert store.get(project_id).jobs[-1].progress == 100

    # Monotonic, bounded, and it moved before the end: a bar that only ever reads 0 and then
    # 100 is a bar that reports completion, not progress.
    assert seen == sorted(seen), seen
    assert max(seen) == 100 and min(seen) == 0, seen
    assert any(0 < value < 100 for value in seen), seen
    # Both stages reported, each inside its own share: a reading from the trims (below the
    # 90 % they own) and a reading from the join (above it, below the settlement's 100).
    assert any(0 < value < 90 for value in seen), seen
    assert any(90 < value < 100 for value in seen), seen
    # Whole-percent throttling: ffmpeg reports several times a second and every save is a
    # whole-manifest write with an fsync behind it, so the bar is capped at one write per
    # percentage point plus the job's own creation and settlement writes.
    assert len(seen) <= 110, len(seen)
    assert comfy.prompts == []


def test_a_stalled_progress_line_cannot_break_an_export(tmp_path: Path, monkeypatch):
    """The drain and the parser, together: ffmpeg is asked to report, the parser is fed a
    line it cannot use for every single reading, and the export still completes and verifies.
    A reader that raised on a line it did not understand would take the export with it."""
    import music_video_producer.app as app_module

    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    monkeypatch.setattr(app_module, "parse_progress_us", lambda line: None)

    response = client.post(f"/api/projects/{project_id}/assemble", json={"preset": "draft"})

    assert response.status_code == 200, response.text
    export = tmp_path / "projects" / project_id / "media" / response.json()["export"]
    assert abs(float(probe(export, "format=duration")) - 8.0) <= 1 / 24
    assert probe(export, "stream=codec_type").splitlines() == ["video", "audio"]
    # Nothing was reported, and the settlement still states the finished number.
    assert response.json()["job"]["progress"] == 100
    assert comfy.prompts == []


# --- Startup healing (Phase 1.3) --------------------------------------------------------
#
# The gap: an assembly job left `running` by a crash was healed only at the *next assemble*,
# so a Director reopening the project after a crash saw an export in progress that nothing
# would ever finish, and every gate counting open local work went on refusing, until they
# happened to assemble again. Boot is the event that made the verdict true, so boot is where
# it is now delivered -- by the same function the assemble path calls, handed the empty
# registry a just-started process actually has.
#
# The boundary these tests exist to hold: a **local** job (empty `prompt_id`) cannot survive
# a restart, because the in-process registry was the only thing that could ever settle it. A
# **ComfyUI** job may well still be executing on the Director's GPU -- ComfyUI is
# user-managed and outlives this process -- so healing one on the strength of our own restart
# would throw away a render being paid for in GPU minutes. Those stay for the reconciler.


def crashed_project(store: ProjectStore, name: str, jobs: list[RenderJob]) -> str:
    """A manifest on disk carrying `jobs`, as a crash would have left it. No app involved."""
    project = store.create(Project(name=name))
    project.jobs = list(jobs)
    store.save(project)
    return project.id


def test_startup_heals_an_orphaned_local_job_and_leaves_a_live_comfy_job_alone(tmp_path: Path):
    """The whole rule in one boot: the local job settles, the ComfyUI job does not move."""
    store = ProjectStore(tmp_path)
    project_id = crashed_project(
        store,
        "Crashed mid-export",
        [
            RenderJob(id="job_local", kind="post", status="running", target_id="assembly",
                      progress=42),
            RenderJob(id="job_comfy", kind="h3", status="running", prompt_id="p-live",
                      target_id="shot_a", missing_ticks=1),
            RenderJob(id="job_comfy_post", kind="post", status="queued", prompt_id="p-audio",
                      target_id="shot_a"),
            RenderJob(id="job_local_h3", kind="h3", status="running", target_id="shot_a"),
            RenderJob(id="job_done", kind="post", status="complete", target_id="assembly",
                      output_files=["media/exports/one.mp4"]),
        ],
    )

    _client, _store, comfy, app = make_client(tmp_path)

    assert app.state.startup_healed_jobs == 1
    jobs = {job.id: job for job in ProjectStore(tmp_path).get(project_id).jobs}
    assert jobs["job_local"].status == "error"
    assert jobs["job_local"].error == ASSEMBLY_ORPHANED_ERROR
    # The live ComfyUI render is untouched in every field -- still open, still counting its
    # unknown ticks, still the reconciler's to settle by asking ComfyUI.
    assert jobs["job_comfy"].status == "running"
    assert jobs["job_comfy"].error == ""
    assert jobs["job_comfy"].missing_ticks == 1
    # And so is the *audio restoration*, which is `kind="post"` like the assembly job and is
    # told apart from it by the one marker that matters: it carries a prompt id.
    assert jobs["job_comfy_post"].status == "queued"
    assert jobs["job_comfy_post"].error == ""
    # And a job of some other kind carrying no prompt id is left alone too, which is the
    # `kind == "post"` clause carried over verbatim from the assemble path rather than
    # widened on the way. Not because such a job is healthy -- nothing can reconcile it
    # either -- but because the only sentence this rule knows how to write is *the
    # assembly's*, and stamping "This assembly was interrupted" onto a shot render would be
    # a false record of what happened. Widening this is a decision with its own wording,
    # not a side effect of moving the rule.
    assert jobs["job_local_h3"].status == "running"
    assert jobs["job_local_h3"].error == ""
    # A settled job is not re-settled.
    assert jobs["job_done"].status == "complete"
    assert jobs["job_done"].output_files == ["media/exports/one.mp4"]
    # Nothing was submitted, asked or cancelled on ComfyUI on the way through boot.
    assert comfy.prompts == []


def test_startup_healing_and_the_assemble_paths_healing_agree_on_the_same_input(tmp_path: Path):
    """One rule, asserted as one rule rather than as two matching transcriptions.

    The same manifest is put through boot in one data root and through the assemble path's
    heal in another, and the resulting job records are compared field for field. A second
    opinion at either end -- a widened kind, a dropped `prompt_id` clause -- shows up here as
    a disagreement rather than as a silent divergence nobody notices until a render is lost.
    """
    crash = [
        RenderJob(id="job_local", kind="post", status="running", target_id="assembly"),
        RenderJob(id="job_local_queued", kind="post", status="queued", target_id="assembly"),
        RenderJob(id="job_comfy", kind="h3", status="queued", prompt_id="p-live",
                  target_id="shot_a"),
        RenderJob(id="job_flux", kind="flux", status="running", prompt_id="p-flux",
                  target_id="asset_a"),
        RenderJob(id="job_cancelled", kind="post", status="cancelled", target_id="assembly"),
    ]

    # Boot: the manifest is on disk before `create_app` runs.
    boot_root = tmp_path / "boot"
    boot_store = ProjectStore(boot_root)
    boot_id = crashed_project(boot_store, "Boot", crash)
    make_client(boot_root)
    booted = ProjectStore(boot_root).get(boot_id).jobs

    # The assemble path: the manifest is written *after* the app exists, and the route's own
    # heal runs on the way in. It refuses afterwards (open renders), which is fine -- the
    # heal happens before the refusal, exactly as it always has.
    route_root = tmp_path / "route"
    route_store = ProjectStore(route_root)
    client, _store, _comfy, _app = make_client(route_root)
    route_id = crashed_project(route_store, "Route", crash)
    assert client.post(f"/api/projects/{route_id}/assemble").status_code == 409
    routed = ProjectStore(route_root).get(route_id).jobs

    def comparable(jobs):
        return [
            {"id": job.id, "status": job.status, "error": job.error, "kind": job.kind,
             "prompt_id": job.prompt_id}
            for job in jobs
        ]

    assert comparable(booted) == comparable(routed)
    # And they agree on something, rather than agreeing by both doing nothing.
    assert [job["status"] for job in comparable(booted)] == [
        "error", "error", "queued", "running", "cancelled"
    ]


def test_startup_survives_no_projects_a_corrupt_manifest_and_a_project_with_no_jobs(
    tmp_path: Path,
):
    """Boot must not be able to fail. Three shapes of nothing-to-do, one of them broken.

    A manifest that cannot be parsed is invisible to `ProjectStore.list`, so it is skipped
    rather than fatal -- and it is left exactly as it was on disk, because a startup pass
    has no business rewriting a file it could not read.
    """
    # No projects at all: the directory is empty and the app still boots and serves.
    empty_root = tmp_path / "empty"
    client, _store, comfy, app = make_client(empty_root)
    assert app.state.startup_healed_jobs == 0
    assert client.get("/api/projects").status_code == 200

    # A corrupt manifest beside a healthy crashed one, plus a project holding no jobs at all.
    mixed_root = tmp_path / "mixed"
    store = ProjectStore(mixed_root)
    healthy_id = crashed_project(
        store, "Healthy", [RenderJob(id="job_local", kind="post", status="running",
                                     target_id="assembly")]
    )
    jobless_id = crashed_project(store, "Jobless", [])
    broken_dir = mixed_root / "projects" / "project_deadbeefcafe"
    (broken_dir / "media").mkdir(parents=True)
    broken = broken_dir / "project.json"
    broken.write_text('{"name": "Half-written', encoding="utf-8")
    broken_bytes = broken.read_bytes()

    client, _store, comfy, app = make_client(mixed_root)

    # The readable crash healed; boot did not raise; the broken file is byte-identical.
    assert app.state.startup_healed_jobs == 1
    assert ProjectStore(mixed_root).get(healthy_id).jobs[0].status == "error"
    assert ProjectStore(mixed_root).get(jobless_id).jobs == []
    assert broken.read_bytes() == broken_bytes
    assert client.get("/api/projects").status_code == 200
    assert comfy.prompts == []


def test_startup_does_not_rewrite_a_manifest_it_had_nothing_to_heal(tmp_path: Path):
    """Every save is a whole-manifest write with an fsync behind it, and it bumps
    `updated_at` -- which the optimistic-concurrency check on `PUT /projects/{id}` compares.
    A boot that touched every project would invalidate every client's snapshot for nothing."""
    store = ProjectStore(tmp_path)
    project_id = crashed_project(
        store,
        "Untouched",
        [RenderJob(id="job_comfy", kind="h3", status="running", prompt_id="p-live",
                   target_id="shot_a")],
    )
    before = (tmp_path / "projects" / project_id / "project.json").read_bytes()

    _client, _store, _comfy, app = make_client(tmp_path)

    assert app.state.startup_healed_jobs == 0
    assert (tmp_path / "projects" / project_id / "project.json").read_bytes() == before


class RaisingListStore(ProjectStore):
    """A store whose `list` fails outright -- a permission error, a mangled data root."""

    def list(self):
        raise RuntimeError("the projects directory could not be read")


class RaisingSaveStore(ProjectStore):
    """A store that reads fine and cannot write. One unwritable project, mid-pass."""

    def save(self, project):
        if project.name == "Unwritable":
            raise OSError("read-only project directory")
        return super().save(project)


def test_boot_survives_a_store_that_raises_on_list_and_on_save(tmp_path: Path):
    """The two guards that exist so startup cannot fail, each driven by a real exception.

    Neither is reachable through `ProjectStore` itself -- `list` already skips a manifest it
    cannot parse -- so they are driven through a store that raises, which is the shape a
    permission error or a mangled data root actually has. Without them the application does
    not start at all, which is a far worse outcome than a job left saying `running`.
    """
    settings = Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy")

    # `list` raises: the pass reports nothing healed and the app is still built and serving.
    listing = create_app(
        settings=settings, store=RaisingListStore(tmp_path), comfy=FakeComfy(),
        director=object(),
    )
    assert listing.state.startup_healed_jobs == 0
    # Served through a route that does not itself list projects -- this store cannot answer
    # that question at all, and the claim here is that *boot* survived it.
    assert TestClient(listing).get("/api/health").status_code == 200

    # `save` raises for one project: the others are still healed, and boot still completes.
    store = ProjectStore(tmp_path)
    orphan = [RenderJob(id="job_local", kind="post", status="running", target_id="assembly")]
    unwritable_id = crashed_project(store, "Unwritable", orphan)
    writable_id = crashed_project(store, "Writable", orphan)

    saving = create_app(
        settings=settings, store=RaisingSaveStore(tmp_path), comfy=FakeComfy(),
        director=object(),
    )

    assert saving.state.startup_healed_jobs == 1
    fresh = ProjectStore(tmp_path)
    assert fresh.get(writable_id).jobs[0].status == "error"
    # The unwritable one is honestly still `running` on disk -- nothing pretended otherwise.
    assert fresh.get(unwritable_id).jobs[0].status == "running"
    assert TestClient(saving).get("/api/projects").status_code == 200


# ----------------------------------------------------------------------------------------------
# Render timing on the local-work settle paths (2026-08-21).
#
# `settle` is also this route's *progress* writer, called up to a hundred times per export, so the
# stamp goes in the two terminal patches and nowhere else: if a progress tick moved `updated_at`,
# it would stop meaning "when this ended" and the duration would evaporate. An assembly is also
# the one job kind whose `created_at` really is a start -- local work begins the moment its record
# exists -- so its `record`-sourced span is an exact export time rather than an upper bound.
# ----------------------------------------------------------------------------------------------


def test_a_finished_export_records_how_long_it_took_and_a_progress_tick_does_not(tmp_path: Path):
    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)

    stamps: list[tuple[int, str]] = []
    real_save = store.save

    def recording_save(project):
        for job in project.jobs:
            if job.kind == "post" and not job.prompt_id:
                stamps.append((job.progress, str(job.updated_at)))
        return real_save(project)

    store.save = recording_save
    try:
        response = client.post(f"/api/projects/{project_id}/assemble")
    finally:
        store.save = real_save

    assert response.status_code == 200, response.text
    settled = store.get(project_id).jobs[-1]
    assert settled.status == "complete"
    assert settled.render_seconds > 0
    assert settled.render_seconds_source == "record"
    assert settled.updated_at > settled.created_at
    # The *sentence*, read rather than assumed. This assertion did not exist, and in its absence
    # a completed export was described as "ComfyUI reported no execution clock for this prompt,
    # so the wait in the queue is included" -- about a job that never went near ComfyUI and was
    # never in any queue, which made an exact number look like an upper bound. See the comment
    # at the `complete` patch in the assemble route, which argues precisely the opposite.
    line = render_timing_summary(settled)
    assert "start to finish" in line
    assert "local work that never went to ComfyUI" in line
    assert "ComfyUI reported no execution clock" not in line
    assert "queue" not in line.replace("never went to ComfyUI", "")
    # Every write before the settlement carried the *same* `updated_at`: a hundred progress
    # ticks moved the percentage and nothing else, which is what keeps the duration a duration.
    during = {stamp for percent, stamp in stamps if percent < 100}
    assert len(during) == 1, during
    assert during != {str(settled.updated_at)}
    assert comfy.prompts == []


def test_a_failed_export_is_stamped_too_and_is_never_called_a_render(
    tmp_path: Path, monkeypatch
):
    """A failure after forty minutes is exactly as interesting as a success after forty --
    but it produced no video, so the surfaced line says what it is. The verification verdict
    is forced the way `test_a_failed_verification_is_reported_with_numbers` forces it."""
    client, store, _comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    import music_video_producer.app as app_module

    monkeypatch.setattr(
        app_module,
        "verification_problems",
        lambda song, measured, streams: ["forced, so the failure path is the one under test"],
    )

    response = client.post(f"/api/projects/{project_id}/assemble")

    assert response.status_code == 502
    settled = store.get(project_id).jobs[-1]
    assert settled.status == "error"
    assert settled.render_seconds_source == "record"
    assert settled.updated_at > settled.created_at
    assert "not render time" in render_timing_summary(settled)


def test_healing_an_orphaned_export_at_boot_stamps_the_record_it_settles(tmp_path: Path):
    """The crash path. The span runs from the export being enqueued to the boot that noticed
    the crash, which is not how long the export ran -- and the surfaced line says so rather
    than reporting a machine that was switched off overnight as a very slow render."""
    store = ProjectStore(tmp_path)
    project_id = crashed_project(
        store,
        "Crashed and stamped",
        [
            RenderJob(id="job_local", kind="post", status="running", target_id="assembly",
                      progress=42),
        ],
    )
    orphan = store.get(project_id).jobs[0]
    assert orphan.updated_at == orphan.created_at, "the fixture must start unstamped"

    make_client(tmp_path)

    healed = ProjectStore(tmp_path).get(project_id).jobs[0]
    assert healed.status == "error"
    assert healed.error == ASSEMBLY_ORPHANED_ERROR
    assert healed.updated_at > healed.created_at
    assert healed.created_at == orphan.created_at, "a settle must not move the enqueue time"
    assert healed.render_seconds_source == "record"
    assert "not render time" in render_timing_summary(healed)


# ------------------------------------------------------------------------------------------
# Slice C1 — the Effect Stack reaching the export.
#
# **Asserted as argv, never as pixels** (R-20). Eight renders of one identical grained chain
# through this project's own `libx264 -preset veryfast` produced two distinct pictures on
# 2026-08-25, while the filter graph's own frames were bit-identical across ten runs either way:
# multi-threaded libx264 is not bit-exact on high-entropy input, and grain is what makes an
# export entropic enough to show it. So "an empty stack exports byte-identically to today" is a
# claim about the command, and it is checked as one.
# ------------------------------------------------------------------------------------------


def recorded_trims(client, monkeypatch, project_id: str) -> tuple[list[list[str]], object]:
    """Run the export with `trim_args` recorded, and hand back one argv per clip.

    The real builder still runs and its real output is still what ffmpeg gets — this wraps it
    rather than replacing it, so the argv asserted below is the argv the export was actually
    driven with and not a reconstruction of it.
    """
    import music_video_producer.app as app_module

    commands: list[list[str]] = []
    real_trim = app_module.trim_args

    def record_trim(*args, **kwargs):
        built = real_trim(*args, **kwargs)
        commands.append(built)
        return built

    monkeypatch.setattr(app_module, "trim_args", record_trim)
    response = client.post(f"/api/projects/{project_id}/assemble")
    return commands, response


def write_stack(client, project_id: str, shot_id: str, stack: list[dict]):
    response = client.put(
        f"/api/projects/{project_id}/shots/{shot_id}/effects", json={"effects": stack}
    )
    assert response.status_code == 200, response.text
    return response


def test_a_shot_with_no_effects_exports_the_argv_this_route_has_always_built(
    tmp_path: Path, monkeypatch
):
    """The Ask-First boundary of this whole slice, asserted at the route's own call site.

    `tests/test_assembly.py` pins the *builder*'s answer against a written-out constant; this
    pins what the export hands it. The two together are the claim: an unstyled project's
    command is argument-for-argument the one it produced before an Effect Stack existed, and
    the empty groups the route now passes are the empty groups `trim_args` already defaulted to.

    Written out by hand rather than derived, because a filter chain compared against one the
    code composed would agree with a chain that had grown a stage nobody asked for.
    """
    from music_video_producer.assembly import trim_args

    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    assert all(shot.effects == [] for shot in store.get(project_id).shots)

    commands, response = recorded_trims(client, monkeypatch, project_id)
    assert response.status_code == 200, response.text
    assert len(commands) == 2

    # The normalization target is the larger of the two takes; the windows are 4 s at 24 fps.
    expected_chain = (
        "scale=192:108:force_original_aspect_ratio=decrease,"
        "pad=192:108:(ow-iw)/2:(oh-ih)/2,fps=24,setsar=1,format=yuv420p"
    )
    for command in commands:
        assert command[command.index("-vf") + 1] == expected_chain
        assert command[command.index("-frames:v") + 1] == "96"
        # And the whole argv is what the builder produces when nothing is passed for either
        # group at all — the identity this slice was told not to move.
        source = Path(command[command.index("-i") + 1])
        dest = Path(command[-1])
        assert command == trim_args(source, dest, frames=96, width=192, height=108)

    assert comfy.prompts == []


def test_a_shots_effect_stack_reaches_the_export_at_the_two_insertion_points(
    tmp_path: Path, monkeypatch
):
    """AD-17's two splice points, seen from the route: geometry before `scale`, treatment
    before `pad`.

    The stack is written **out of order** on purpose — the texture card first, the geometry
    card second — because storage order is not load-bearing (AD-31) and the chain has to come
    out in the fixed family order regardless. And the second shot carries nothing, so the same
    export proves the two cases side by side: one clip graded, its neighbour byte-identical to
    what it was before this slice.

    Ordering matters here for a reason that is invisible in a still: geometry ahead of `scale`
    is what makes a punch-in sample the take's own pixels instead of resampling an
    already-scaled frame, and every treatment ahead of `pad` is what keeps the letterbox bars
    at pure black (measured 2026-08-21: after `pad` the bar samples RGB (1,1,5)).
    """
    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    write_stack(
        client,
        project_id,
        "shot_a",
        [
            {"effect": "grain", "parameters": {"strength": 8, "seed": 7}},
            {"effect": "punch_in", "parameters": {"zoom": 1.2}},
        ],
    )

    commands, response = recorded_trims(client, monkeypatch, project_id)
    assert response.status_code == 200, response.text
    assert len(commands) == 2

    assert commands[0][commands[0].index("-vf") + 1] == (
        "crop=w=iw/1.2:h=ih/1.2:x=(iw-ow)/2:y=(ih-oh)/2,"
        "scale=192:108:force_original_aspect_ratio=decrease,"
        "noise=alls=8:allf=t+u:all_seed=7,"
        "pad=192:108:(ow-iw)/2:(oh-ih)/2,fps=24,setsar=1,format=yuv420p"
    )
    assert commands[1][commands[1].index("-vf") + 1] == (
        "scale=192:108:force_original_aspect_ratio=decrease,"
        "pad=192:108:(ow-iw)/2:(oh-ih)/2,fps=24,setsar=1,format=yuv420p"
    )
    # Nothing else about the command moved. The chain and the two file paths are the only
    # things that legitimately differ between two clips of one export; with those three taken
    # out, the graded clip's command is the unstyled clip's, argument for argument.

    def shape(command: list[str]) -> list[str]:
        chain = command.index("-vf")
        stripped = command[:chain] + command[chain + 2 :]
        stripped[stripped.index("-i") + 1] = "<source>"
        stripped[-1] = "<dest>"
        return stripped

    assert shape(commands[0]) == shape(commands[1])

    # The export really ran and really carries both clips.
    assert Path(response.json()["export"]).name.startswith("assembly_")
    assert comfy.prompts == []


def test_a_disabled_effect_is_kept_in_the_manifest_and_composes_no_stage(
    tmp_path: Path, monkeypatch
):
    """The matrix's own row, and the reason a stack stores a flag rather than a deletion.

    A Director switching a card off is not throwing it away — the numbers they dialled in have
    to be there when they switch it back on — so the entry stays in the manifest and simply
    contributes nothing to the chain. Both halves are asserted, because either alone reads as
    an accident: the argv is the unstyled one, and the stack is still on the Shot afterwards.
    """
    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    write_stack(
        client,
        project_id,
        "shot_a",
        [
            {"effect": "grain", "enabled": False, "parameters": {"strength": 8, "seed": 7}},
            {"effect": "punch_in", "enabled": False, "parameters": {"zoom": 1.2}},
        ],
    )

    commands, response = recorded_trims(client, monkeypatch, project_id)
    assert response.status_code == 200, response.text
    unstyled = (
        "scale=192:108:force_original_aspect_ratio=decrease,"
        "pad=192:108:(ow-iw)/2:(oh-ih)/2,fps=24,setsar=1,format=yuv420p"
    )
    assert [command[command.index("-vf") + 1] for command in commands] == [unstyled, unstyled]

    kept = ProjectStore(tmp_path).get(project_id).shots[0]
    assert [(spec.effect, spec.enabled, spec.parameters) for spec in kept.effects] == [
        ("grain", False, {"strength": 8, "seed": 7}),
        ("punch_in", False, {"zoom": 1.2}),
    ]
    assert comfy.prompts == []


def test_an_export_refuses_a_stack_it_cannot_compose_and_names_the_shot(
    tmp_path: Path, monkeypatch
):
    """Validity is re-derived at the moment of composing, never remembered from the write.

    Two ways a stack that was agreed at write time stops being composable, and each takes a
    different path through the route:

    * a value that is out of the catalogue's bounds — which a hand-edited manifest can hold,
      since nothing re-validates on load — joins `assembly_refusals`' comprehensive report, so
      a Director is told about it in the same answer as everything else wrong with the plan;
    * a look whose `.cube` has left the folder since it was chosen, which only the composer can
      see. Refused before the job record is written, rather than left to fail inside ffmpeg
      with a message naming `clut` and mentioning neither the path nor the problem.

    Both name the Shot, because `EffectRefusal` is a pure function of a stack and has no idea
    which clip carries it — and both carry the chain's own sentence whole beside that name.

    **AD-21 in one test:** nothing stored says "this stack is valid". The write said so at the
    time and the export asks again.
    """
    from music_video_producer.effects import EFFECT_LUT_FILE_MISSING_REFUSAL, lut_directory

    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)

    # 1. Two manifests edited past the bounds the route enforces — and a third thing wrong
    #    with the plan that has nothing to do with effects.
    #
    #    All three come back in **one** answer, which is what makes the check belong beside
    #    `assembly_refusals` rather than only inside the composer. The unapproved shot is what
    #    proves it: the plan is never laid out at all when a refusal stands, so a stack judged
    #    only at composition would stay silent here and the Director would fix the approval,
    #    run again, and *then* be told about the first of the two stacks. Being sent back three
    #    times for three faults is the failure `assembly_refusals` exists against.
    project = store.get(project_id)
    project.shots[0].effects = [EffectSpec(effect="punch_in", parameters={"zoom": 9.0})]
    project.shots[1].effects = [EffectSpec(effect="nope")]
    store.save(project)
    assert client.post(
        f"/api/projects/{project_id}/shots/shot_a/unapprove"
    ).status_code == 200

    refused = client.post(f"/api/projects/{project_id}/assemble")
    assert refused.status_code == 422, refused.text
    detail = refused.json()["detail"]
    assert (
        "SHOT 01 (shot_a): punch_in's zoom is 9, above its maximum of 2. Nothing was composed."
    ) in detail
    assert (
        "SHOT 02 (shot_b): There is no effect called 'nope' in the catalogue. "
        "Nothing was composed."
    ) in detail
    assert ASSEMBLY_UNAPPROVED_REFUSAL.format(shot="SHOT 01 (shot_a)") in detail
    assert store.get(project_id).jobs == [], "a refused export wrote a job record"

    # 2. A look that has left the folder since it was chosen — which only the composer can see,
    #    because the id is still one the server discovered and the file is what has gone.
    project = store.get(project_id)
    project.shots[0].effects = []
    project.shots[1].effects = []
    store.save(project)
    assert client.post(f"/api/projects/{project_id}/shots/shot_a/approve").status_code == 200
    looks = client.get("/api/effects/catalogue").json()["looks"]
    chosen = looks[0]["lut_id"]
    write_stack(client, project_id, "shot_a", [{"effect": "lut_look", "parameters": {"lut": chosen}}])
    gone = next(
        path
        for path in lut_directory(tmp_path).iterdir()
        if path.stem == looks[0]["name"]
    )
    gone.unlink()

    vanished = client.post(f"/api/projects/{project_id}/assemble")
    assert vanished.status_code == 422, vanished.text
    detail = vanished.json()["detail"]
    assert detail.startswith("SHOT 01 (shot_a): ")
    assert detail.endswith(
        EFFECT_LUT_FILE_MISSING_REFUSAL.format(lut=chosen, path=gone.as_posix())
    )
    assert store.get(project_id).jobs == []
    assert comfy.prompts == []


def test_an_export_names_every_missing_look_in_one_answer(tmp_path: Path):
    """The composition loop's own comprehensive report, which it did not have.

    The pre-pass above it collects `validate_stack` refusals into a list precisely so a Director
    is not sent back three times for three faults. The composition loop below it raised on the
    **first** `EffectRefusal` — and it is the only place a missing *file* can be seen, since
    `validate_stack` compares ids against the discovered listing and never touches the disk. So
    two shots whose `.cube` files had both gone named only `SHOT 01`: restore that one, run
    again, and be told about `SHOT 02`.

    Both looks are deleted before a single export runs, which is the whole point — the Director
    did not delete them one at a time either.
    """
    from music_video_producer.effects import EFFECT_LUT_FILE_MISSING_REFUSAL, lut_directory

    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)

    looks = client.get("/api/effects/catalogue").json()["looks"]
    chosen = [looks[0]["lut_id"], looks[1]["lut_id"]]
    for shot_id, lut in zip(("shot_a", "shot_b"), chosen, strict=True):
        write_stack(client, project_id, shot_id, [{"effect": "lut_look", "parameters": {"lut": lut}}])
    gone = []
    for name in (looks[0]["name"], looks[1]["name"]):
        path = lut_directory(tmp_path) / f"{name}.cube"
        path.unlink()
        gone.append(path)

    refused = client.post(f"/api/projects/{project_id}/assemble")
    assert refused.status_code == 422, refused.text
    detail = refused.json()["detail"]

    # One answer, both shots, each carrying the chain's own sentence whole beside the name the
    # timeline gives it.
    assert detail.splitlines() == [
        "SHOT 01 (shot_a): "
        + EFFECT_LUT_FILE_MISSING_REFUSAL.format(lut=chosen[0], path=gone[0].as_posix()),
        "SHOT 02 (shot_b): "
        + EFFECT_LUT_FILE_MISSING_REFUSAL.format(lut=chosen[1], path=gone[1].as_posix()),
    ]
    assert store.get(project_id).jobs == [], "a refused export wrote a job record"
    assert comfy.prompts == []


def test_the_export_composes_a_stack_against_the_plans_geometry_and_not_the_takes(
    tmp_path: Path, monkeypatch
):
    """The one thing about the call site that no other route-level export test can see.

    `build_effect_stages(stack, width=plan.width, height=plan.height, ...)` — mutate that to
    `width=1, height=1` and the entire suite still passed. Only `chroma_split` reads
    `context.width`, and nothing exported one, so a stack graded at 1920 and exported at 1056
    would have shifted by the wrong number of pixels with nothing noticing.

    `shot_a`'s own take is **128** wide and the export's delivery geometry is **192** — the
    largest take present — so the two candidate widths give two different answers for the same
    stored fraction: `0.02 × 128` rounds to 3 pixels and `0.02 × 192` rounds to 4. The chain has
    to say 4. That is what a treatment stage is composed for (see `effects.StageContext`): the
    fraction is stored so the look survives a change of export size, and it is turned into
    pixels against the size actually being delivered.
    """
    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    write_stack(
        client, project_id, "shot_a", [{"effect": "chroma_split", "parameters": {"shift": 0.02}}]
    )

    commands, response = recorded_trims(client, monkeypatch, project_id)
    assert response.status_code == 200, response.text
    assert commands[0][commands[0].index("-vf") + 1] == (
        "scale=192:108:force_original_aspect_ratio=decrease,"
        "chromashift=cbh=4:crh=-4,"
        "pad=192:108:(ow-iw)/2:(oh-ih)/2,fps=24,setsar=1,format=yuv420p"
    )
    # 3 is what this shot's *own* take would have produced, and 0 is what a geometry of 1 would
    # have produced — the mutation that survived. Named here so the assertion above cannot be
    # read as "some number came out".
    assert "cbh=3" not in commands[0][commands[0].index("-vf") + 1]
    assert "chromashift" not in commands[1][commands[1].index("-vf") + 1]
    assert comfy.prompts == []


# ----------------------------------------------------------------------------------------------
# Story 9.6 — honest export with effects. Three of the four acceptance groups already held; these
# are the four that did not: the provenance record (FX-25), the pre-flight as a registered list
# of checks (FX-24), the untouched-neighbour claim (FX-NFR-2), and "nothing about an Effect lives
# only in the interface" (FX-23), which was true and unasserted.
# ----------------------------------------------------------------------------------------------


def test_an_export_records_the_look_it_ran_and_the_record_outlives_the_shot(
    tmp_path: Path, monkeypatch
):
    """FX-25. `inputs` said which takes went in; nothing said what was done to them, so an export
    six months old could not be told from one made before an Effect existed.

    The record is taken from the composition the export is about to be **driven with**, not
    re-derived from the Shots afterwards, and this test is written so that the difference shows:
    the stack is rewritten and then deleted after the export finishes, and the record of what ran
    is unmoved. That is the whole of the second property the story asks of it — readable long
    after the Shot has moved on, because it holds what the export used rather than a pointer to
    state that will have changed.

    Three further things the shape has to get right, each asserted here rather than argued:

    * The values are the **resolved** ones. `grain`'s `seed` is in the record and the Director
      never typed it; a record of only what was typed would not answer what the picture was.
    * Chain order, not storage order. The stack is written texture-first and comes back
      geometry-first, because that is the order `build_effect_stages` ran the filters in and
      storage order is not load-bearing (AD-31).
    * A Shot carrying no look contributes no entry, so an unstyled project's record is empty
      rather than a row of nothings.

    And `bindings` and `transitions` are empty on **this** record, because no Shot here carries
    either. *Corrected 2026-08-28: this sentence said they are empty "on the record this build
    writes", which stopped being true of `bindings` when Epic 10 filled it* --
    `test_an_export_records_the_bindings_that_drove_it` asserts a real one a few hundred lines
    below. `transitions` is Epic 11's and is empty on every record this build can write.
    """
    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    write_stack(
        client,
        project_id,
        "shot_a",
        [
            {"effect": "grain", "parameters": {"strength": 0.2}},
            {"effect": "punch_in", "parameters": {"zoom": 1.4}},
            {"effect": "vignette", "enabled": False, "parameters": {"angle": 1.0}},
        ],
    )

    commands, response = recorded_trims(client, monkeypatch, project_id)
    assert response.status_code == 200, response.text

    job = store.get(project_id).jobs[-1]
    assert job.kind == "post" and job.status == "complete"
    # `inputs` untouched: the takes half of the same question, exactly as it was.
    assert job.inputs == [
        f"shot_a=music-video-producer/{project_id}/shots/shot_a-h3_00001-audio.mp4",
        f"shot_b=music-video-producer/{project_id}/shots/shot_b-h3_00001-audio.mp4",
    ]
    # Chain order, resolved values, the disabled entry absent, and `shot_b` contributing nothing.
    assert job.look.effects == [
        'shot_a=punch_in:{"zoom":1.4}',
        'shot_a=grain:{"seed":0,"strength":0.2}',
    ]
    assert job.look.bindings == [] and job.look.transitions == []
    # And what it claims is what ran: every effect named in the record is in the argv the export
    # was actually driven with, and the shot that carries none got the chain it always got.
    graded = commands[0][commands[0].index("-vf") + 1]
    assert "crop=" in graded and "noise=" in graded
    assert commands[1][commands[1].index("-vf") + 1] == (
        "scale=192:108:force_original_aspect_ratio=decrease,"
        "pad=192:108:(ow-iw)/2:(oh-ih)/2,fps=24,setsar=1,format=yuv420p"
    )

    # The Shot moves on: regraded, then stripped entirely. The export's record does not move.
    write_stack(client, project_id, "shot_a", [{"effect": "posterize", "parameters": {"levels": 4}}])
    write_stack(client, project_id, "shot_a", [])
    reread = ProjectStore(tmp_path).get(project_id).jobs[-1]
    assert reread.look.effects == [
        'shot_a=punch_in:{"zoom":1.4}',
        'shot_a=grain:{"seed":0,"strength":0.2}',
    ]
    assert comfy.prompts == []


def test_an_export_made_before_the_look_was_recorded_reads_as_carrying_none(tmp_path: Path):
    """The first property the story states of the record, said in a test rather than assumed.

    A manifest written before this field existed has no `look` key at all — not an empty one, no
    key — and it must load as an export that applied **no** look rather than as one whose look is
    unknown. That is deliberately not `sampling_bundle`'s `None`-means-unknown convention, and
    the difference is which mistake each field can make: a bundle defaulted to a name would claim
    a sampling nobody performed, while an effects list defaulted to empty claims only that
    nothing was applied, which is what every export in this application's history before Epic 9
    genuinely was.

    Driven through the real store rather than by constructing the model, because the claim is
    about a file on disk that nobody is going to rewrite.
    """
    store = ProjectStore(tmp_path)
    project = store.create(Project(name="Before the record"))
    project.jobs = [
        RenderJob(
            id="job_old",
            kind="post",
            status="complete",
            target_id="assembly",
            inputs=["shot_a=takes/one.mp4"],
            output_files=["exports/assembly_00001.mp4"],
        )
    ]
    store.save(project)

    # The manifest as this application wrote them until 2026-08-25: every key a job has ever
    # carried, and no `look` at all. Not an empty one — no key.
    manifest = tmp_path / "projects" / project.id / "project.json"
    body = json.loads(manifest.read_text(encoding="utf-8"))
    before = body["jobs"][0].pop("look")
    assert before == {"effects": [], "bindings": [], "transitions": []}
    manifest.write_text(json.dumps(body), encoding="utf-8")

    loaded = ProjectStore(tmp_path).get(project.id).jobs[0]
    assert loaded.look.effects == []
    assert loaded.look.bindings == []
    assert loaded.look.transitions == []
    # And it round-trips with one more key and no other change, so reading an old manifest and
    # saving it is not a rewrite of anything else.
    ProjectStore(tmp_path).save(ProjectStore(tmp_path).get(project.id))
    after = json.loads(manifest.read_text(encoding="utf-8"))
    assert set(after["jobs"][0]) - set(body["jobs"][0]) == {"look"}
    assert after["jobs"][0] == {**body["jobs"][0], "look": before}


def test_the_recorded_look_cannot_be_blanked_or_forged_through_the_whole_project_put(
    tmp_path: Path,
):
    """The guard, landed in the same commit as the field, on a real export's own record.

    `tests/test_api.py` runs every name in `JOB_RECORDED_FIELDS` through the generic `PUT` and
    requires each to come back unmoved; this is the same guard met at the surface it protects,
    with a look a real export actually composed rather than a constructed one.

    Both directions, because both are how this route has been the hole twelve times before. The
    **blank** is the ordinary case and the dangerous one: `ExportLook` is defaulted and every
    client that exists omits it, so one save from any of them would erase the record of what
    every export in the project looked like. The **forgery** is the other: `Shot.effects` is
    already server-owned here, so an unguarded `look` would be the way to claim a grade the
    manifest itself refuses to hold.
    """
    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    write_stack(client, project_id, "shot_a", [{"effect": "punch_in", "parameters": {"zoom": 1.2}}])
    assert client.post(f"/api/projects/{project_id}/assemble").status_code == 200
    recorded = store.get(project_id).jobs[-1].look.effects
    assert recorded == ['shot_a=punch_in:{"zoom":1.2}']

    # 1. The save every existing client sends: the whole manifest with no `look` on any job.
    body = json.loads(store.get(project_id).model_dump_json())
    for job in body["jobs"]:
        job.pop("look")
    assert client.put(f"/api/projects/{project_id}", json=body).status_code == 200
    assert store.get(project_id).jobs[-1].look.effects == recorded

    # 2. A look for a grade nobody applied.
    body = json.loads(store.get(project_id).model_dump_json())
    body["jobs"][-1]["look"] = {
        "effects": ['shot_b=lut_look:{"interp":"tetrahedral","lut":"never-chosen"}'],
        "bindings": ["shot_a.zoom=envelope"],
        "transitions": ["shot_a>shot_b=dissolve"],
    }
    assert client.put(f"/api/projects/{project_id}", json=body).status_code == 200
    settled = store.get(project_id).jobs[-1].look
    assert settled.effects == recorded
    assert settled.bindings == [] and settled.transitions == []
    assert comfy.prompts == []


def test_a_clip_carrying_no_effects_is_encoded_once_and_identically_whatever_its_neighbour_wears(
    tmp_path: Path, monkeypatch
):
    """FX-NFR-2, which the acceptance criteria state and nothing measured: a clip carrying no
    Effects is not re-encoded a second time on account of anything elsewhere in the timeline.

    Determined rather than asserted, by running the same two-shot project twice — once with
    neither shot graded, once with the *neighbour* heavily graded — and comparing what the
    unstyled clip's ffmpeg was driven with. The claim has two halves and both are here:

    * **Once.** One trim per clip, both times. There is no second pass over the joined video and
      no re-encode of a clip on account of a neighbour, so the count is the count of clips.
    * **Identically.** `shot_b`'s argv is byte-for-byte the same in both runs, including the
      `-vf` chain and the frame count, so a Director who grades one shot pays for that shot.

    The comparison is between two real runs rather than against a written-out constant on
    purpose: a constant would answer the question "is this the chain we expected", and the
    question here is "did the neighbour change anything".
    """
    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)

    recording, first = recorded_trims(client, monkeypatch, project_id)
    assert first.status_code == 200, first.text
    # Snapshotted before the second run: `recorded_trims` wraps whatever `trim_args` is at the
    # time, so the second call's recorder sits on top of this one and keeps feeding its list.
    plain = [list(command) for command in recording]

    write_stack(
        client,
        project_id,
        "shot_a",
        [
            {"effect": "punch_in", "parameters": {"zoom": 1.8}},
            {"effect": "grain", "parameters": {"strength": 0.4}},
            {"effect": "posterize", "parameters": {"levels": 6}},
        ],
    )
    graded, second = recorded_trims(client, monkeypatch, project_id)
    assert second.status_code == 200, second.text

    # One ffmpeg per clip, both runs. A second encode of anything would show up as a third argv.
    assert len(plain) == 2 and len(graded) == 2
    # The graded neighbour really did change — otherwise the comparison below proves nothing.
    assert graded[0][graded[0].index("-vf") + 1] != plain[0][plain[0].index("-vf") + 1]
    # And the untouched clip's command is the same command, argument for argument, apart from
    # the scratch directory each run writes its intermediates into.
    def without_paths(command):
        return [part for part in command if "\\.work-" not in part and "/.work-" not in part]

    assert without_paths(graded[1]) == without_paths(plain[1])
    assert comfy.prompts == []


def test_an_exports_look_is_reproducible_from_the_manifest_alone(tmp_path: Path, monkeypatch):
    """FX-23, which was true and unasserted: nothing about an Effect lives only in the interface.

    The test that would fail if any part of a look were held in the client. A second
    `ProjectStore` opened on the same folder reads the manifest as a cold reader would — no
    request, no session, nothing the browser sent — and from that manifest and the export's own
    delivery geometry it recomposes the chain. It must equal the `-vf` the export was driven
    with, character for character.

    A LUT is in the stack deliberately, because a look chosen from a picker is the part most
    likely to have been carried only on the wire: the client names it, the server discovers the
    file, and the filter argument is a path neither of them typed. If any of that lived only in
    the interface, the recomposition would not produce the same `lut3d`.

    `interp` is never sent by this test at all, which is the second half of the same claim: the
    catalogue's default is part of the look, it is on the server, and a manifest holding only
    what was typed still recomposes the whole chain.
    """
    from music_video_producer.effects import build_effect_stages, discover_luts

    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    chosen = client.get("/api/effects/catalogue").json()["looks"][0]["lut_id"]
    write_stack(
        client,
        project_id,
        "shot_a",
        [
            {"effect": "lut_look", "parameters": {"lut": chosen}},
            {"effect": "dutch_tilt", "parameters": {"angle": 3.5}},
        ],
    )

    commands, response = recorded_trims(client, monkeypatch, project_id)
    assert response.status_code == 200, response.text
    ran = commands[0][commands[0].index("-vf") + 1]

    # The cold reader: a different store object, a different `discover_luts` call, and nothing
    # in hand but the folder on disk.
    cold = ProjectStore(tmp_path).get(project_id)
    shot = next(item for item in cold.shots if item.id == "shot_a")
    width, height = response.json()["width"], response.json()["height"]
    stages = build_effect_stages(
        [spec.model_dump() for spec in shot.effects],
        width=width,
        height=height,
        luts=discover_luts(tmp_path),
    )
    rebuilt = ",".join(
        [
            *stages.geometry,
            f"scale={width}:{height}:force_original_aspect_ratio=decrease",
            *stages.treatment,
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
            "fps=24",
            "setsar=1",
            "format=yuv420p",
        ]
    )
    assert rebuilt == ran
    assert "lut3d=file=" in ran and "interp=tetrahedral" in ran and "rotate=" in ran
    assert comfy.prompts == []


def test_a_check_registered_into_the_pre_flight_reports_without_the_route_being_touched(
    tmp_path: Path, monkeypatch
):
    """FX-24. The refusal report is a list of checks that Epic 10 and Epic 11 append to, and this
    is that claim executed: two checks nobody has written yet are registered from a test, and
    both of their sentences come back in the export's one answer with not a line of the route
    changed.

    Both stages, because the two coming epics need different ones. A binding refusal is a fact
    about the stack and the song and needs no geometry, so it registers into the plan stage; a
    transition is composed against the plan's own frame counts and delivery size, so it registers
    into the composition stage and is handed the `ExportComposition` it would fill in.

    The plan-stage check runs first and its refusal stands alone, which is not a quirk of the
    registry but the ordering the report has always had: the composition stage cannot run until
    the plan exists, and the plan does not exist while anything is refusing it.
    """
    import music_video_producer.app as app_module

    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    write_stack(client, project_id, "shot_a", [{"effect": "punch_in", "parameters": {"zoom": 1.1}}])

    # 1. The composition stage: a check that sees the plan, and can write into what the export
    #    will run. Epic 11's shape.
    seen = {}

    def transition_check(subject, composition):
        seen["width"] = subject.plan.width
        seen["clips"] = len(subject.plan.clips)
        seen["stages"] = dict(composition.effect_stages)
        return ["SHOT 02 (shot_b): a transition longer than the clip it lands on."]

    monkeypatch.setattr(
        app_module,
        "EXPORT_COMPOSITION_CHECKS",
        (*app_module.EXPORT_COMPOSITION_CHECKS, transition_check),
    )
    refused = client.post(f"/api/projects/{project_id}/assemble")
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == (
        "SHOT 02 (shot_b): a transition longer than the clip it lands on."
    )
    # It was handed the real plan, and the chains the checks before it had already composed.
    assert seen == {"width": 192, "clips": 2, "stages": seen["stages"]}
    # Keyed by the clip's index in the plan rather than by its Shot's id (story 9.7): shot_a is
    # the first clip, and a Shot that another nests inside would be two entries here, not one.
    assert list(seen["stages"]) == [0]
    assert store.get(project_id).jobs == [], "a refused export wrote a job record"

    # 2. The plan stage: a check that runs before any geometry exists, beside the ones already
    #    registered, and lands in the same report as them. Epic 10's shape.
    def binding_check(subject):
        assert subject.plan is None
        return [f"SHOT 01 (shot_a): {len(subject.stacks)} stack(s) bind to an unmeasured song."]

    monkeypatch.setattr(
        app_module, "EXPORT_PLAN_CHECKS", (*app_module.EXPORT_PLAN_CHECKS, binding_check)
    )
    assert client.post(f"/api/projects/{project_id}/shots/shot_b/unapprove").status_code == 200
    refused = client.post(f"/api/projects/{project_id}/assemble")
    assert refused.status_code == 422, refused.text
    # One answer: the registered check's sentence beside the window check's own, in registration
    # order, and the composition stage never reached because the plan never existed.
    assert refused.json()["detail"].splitlines() == [
        ASSEMBLY_UNAPPROVED_REFUSAL.format(shot="SHOT 02 (shot_b)"),
        "SHOT 01 (shot_a): 1 stack(s) bind to an unmeasured song.",
    ]
    assert comfy.prompts == []


# ------------------------------------------------------------------------------------------
# Story 9.7 — the seam. A Shot that another nests inside becomes two clips, and the two are
# no longer handed the same filter text.
# ------------------------------------------------------------------------------------------


def project_with_a_nested_shot(client, store, tmp_path: Path):
    """An 8 s song under one Shot that covers all of it, with a second Shot laid over its
    middle two seconds.

    `assembly_plan` resolves that into **three** clips carrying **two** shot ids: the
    underneath from 0 to 3, the overlay from 3 to 5, and the underneath again from 5 to 8 with
    its take offset advanced by exactly the five seconds it skipped. It is the only shape in
    this application where one Shot's Effect Stack is composed more than once, and therefore
    the only shape where the two compositions can disagree.
    """
    project_id = client.post("/api/projects", json={"name": "Seam"}).json()["id"]
    upload = client.post(
        f"/api/projects/{project_id}/songs/upload",
        data={"title": "Seam Song", "duration": "0"},
        files={"file": ("song.wav", wav_bytes(8.0), "audio/wav")},
    )
    assert upload.status_code == 200, upload.text

    shots_dir = (
        tmp_path / "comfy" / "output" / "music-video-producer" / project_id / "shots"
    )
    synthesize_take(shots_dir / "under-h3_00001-audio.mp4", 8.5, colour="red")
    synthesize_take(
        shots_dir / "over-h3_00001-audio.mp4", 2.5, size="192x108", colour="blue"
    )
    prefix = f"music-video-producer/{project_id}/shots"
    shots = [
        {
            "id": "under",
            "start": 0,
            "duration": 8.0,
            "prompt": "The whole song",
            "status": "complete",
            "latest_output": f"{prefix}/under-h3_00001-audio.mp4",
        },
        {
            "id": "over",
            "start": 3.0,
            "duration": 2.0,
            "prompt": "Laid over the middle",
            "status": "complete",
            "latest_output": f"{prefix}/over-h3_00001-audio.mp4",
        },
    ]
    saved = client.put(f"/api/projects/{project_id}/shots", json={"shots": shots})
    assert saved.status_code == 200, saved.text
    for shot_id in ("under", "over"):
        approved = client.post(f"/api/projects/{project_id}/shots/{shot_id}/approve")
        assert approved.status_code == 200, approved.text
    return project_id, shots_dir


def test_the_two_clips_of_one_shot_differ_by_exactly_the_second_clips_offset(
    tmp_path: Path, monkeypatch
):
    """Story 9.7's measurement, taken through the route rather than argued about.

    Until this story the export keyed its composed chains by **shot id**, so the two clips a
    nested overlay carves out of one Shot received byte-identical filter text — and `trim_args`
    prepends `setpts=PTS-STARTPTS` to the second of them, which restarts the graph's clock at
    zero. A shake snapped back to phase zero five seconds into a Shot nobody cut, and grain ran
    the same noise sequence over again, on the exact frame the eye is already looking at.

    The assertion is the whole claim in one line: the second clip's chain is the first clip's
    chain, with the trim pair in front of it and **the offset substituted** — `(t+0)` become
    `(t+5)` wherever a stage reads the clock, and grain's seed advanced by the same five seconds
    in milliseconds. Nothing else in it moves, because nothing else about the Shot changed.

    Three stages are exercised on purpose and they fail three different ways. `handheld_shake`
    reads `t` directly. `slow_zoom` reads it as a fraction of the Shot's whole length, which is
    the one that would restart rather than jump — and it is a *branched* filtergraph, so this is
    also the branch reaching a real export argv. `grain` has no expression to offset at all and
    carries the seam in its seed instead.
    """
    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_a_nested_shot(client, store, tmp_path)
    write_stack(
        client,
        project_id,
        "under",
        [
            {"effect": "slow_zoom", "parameters": {"zoom": 1.4, "direction": "in"}},
            {"effect": "handheld_shake", "parameters": {"amplitude": 0.02, "frequency": 3}},
            {"effect": "grain", "parameters": {"strength": 8, "seed": 7}},
        ],
    )

    commands, response = recorded_trims(client, monkeypatch, project_id)
    assert response.status_code == 200, response.text
    body = response.json()
    # One Shot became two clips: three trims for two Shots.
    assert body["clip_count"] == 3
    assert len(commands) == 3

    chains = [command[command.index("-vf") + 1] for command in commands]
    first, middle, last = chains

    # The overlay carries no stack at all, so its chain is the one this route always built —
    # no branch, no guard, no treatment. A Shot with no effects is untouched by any of this.
    assert middle == (
        "scale=192:108:force_original_aspect_ratio=decrease,"
        "pad=192:108:(ow-iw)/2:(oh-ih)/2,fps=24,setsar=1,format=yuv420p"
    )

    # Each of the three stages named on its own, on both sides of the seam. The substitution
    # below cannot tell a term that *moved* from one that was never written: a stage that
    # quietly stopped reading the offset would leave both clips alike and pass it. So the shake
    # reads the clock directly, the zoom reads it as a fraction of the Shot's whole length, and
    # grain — which has no expression to offset — carries the seam in its seed.
    assert "sin(2*PI*3*(t+0))" in first and "sin(2*PI*3*(t+5))" in last
    assert r"min((t+0)/8\,1)" in first and r"min((t+5)/8\,1)" in last
    assert "all_seed=7" in first and "all_seed=5007" in last
    assert "(t+5)" not in first

    # The measurement. `trim=start_frame=120` is the second clip's own cut, five seconds at
    # 24 fps into the same take; everything after it is the first clip's chain with the offset
    # moved through it.
    assert last == "trim=start_frame=120,setpts=PTS-STARTPTS," + first.replace(
        "(t+0)", "(t+5)"
    ).replace("all_seed=7", "all_seed=5007")

    # And the state this story replaced: the two are no longer the same text.
    assert last.removeprefix("trim=start_frame=120,setpts=PTS-STARTPTS,") != first

    # The branch reached the real argv, and the guard came with it.
    assert first.startswith("tpad=stop=1:stop_mode=clone,")
    assert "split=2[fx0a][fx0b];" in first

    # The frame rule, on the file this export actually wrote: 8 s at 24 fps, branch and all.
    export = tmp_path / "projects" / project_id / "media" / body["export"]
    assert body["total_frames"] == 192
    counted = subprocess.run(
        [
            "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,nb_read_frames", "-of", "csv=p=0",
            export.as_posix(),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert counted == "192,108,192"
    assert comfy.prompts == []


def test_the_export_job_record_does_not_revert_a_shot_edited_while_it_probed(tmp_path: Path):
    """**Grading is exactly what a Director does while an export churns.**

    This route re-reads the manifest before it judges the plan, then awaits an ffprobe per
    sourced shot -- two for a shot that mixes its take's audio -- and every one of those is a
    window a shot edit can land in. The job record was the one write on the route that saved
    the object read before them, so the record of the export reverted the work being watched:
    a prompt typed, a document, a nudge, all silently back to where they stood when Export was
    clicked, with the route answering 200. `settle` states the rule for every other write here.

    Nothing about the export itself changes: the plan was validated above the probes and
    `job.inputs` records the takes it ran on.
    """
    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    gate = Interleaved()
    real_probe = app_module.probe_take_args

    def probing(source):
        args = real_probe(source)
        gate.pause()
        return args

    app_module.probe_take_args = probing
    try:
        stored = store.get(project_id)
        edited = [shot.model_dump(mode="json") for shot in stored.shots]
        edited[0]["prompt"] = "Typed while the export was probing"
        response, saved = gate.run(
            lambda: client.post(f"/api/projects/{project_id}/assemble"),
            lambda: client.put(
                f"/api/projects/{project_id}/shots", json={"shots": edited}
            ),
        )
    finally:
        app_module.probe_take_args = real_probe

    assert gate.fired == [True], "the shot edit never landed inside the probing"
    assert saved.status_code == 200, saved.text
    assert response.status_code == 200, response.text
    stored = store.get(project_id)
    assert [shot.prompt for shot in stored.shots] == [
        "Typed while the export was probing",
        "Blue room",
    ]
    # And the export still happened, settled on the same fresh manifest.
    assert [job.status for job in stored.jobs] == ["complete"]
    assert stored.jobs[0].output_files == ["exports/assembly_00001.mp4"]
    assert comfy.prompts == []
# ------------------------------------------------------------------------------------------
# Epic 10: a Parameter Binding reaches the export.
#
# The load-bearing claim in this block is one a grep can never make. A `sendcmd` aimed at a target
# that is not in the graph is discarded at rc 0, with no warning even at `-v warning`, and the
# frames come back byte-identical -- so a compiled script sitting on disk beside the intermediates
# proves nothing whatever about whether the picture moved. Every claim here that a binding drove
# an export is a comparison of **frame checksums** against the undriven render of the same chain,
# and never the existence of the script.
# ------------------------------------------------------------------------------------------


def beaty_wav_bytes(seconds: float = 8.0, rate: int = 22050) -> bytes:
    """A song with transients in it: a 60 Hz burst decaying every half second.

    `punch` measures level *above its own running average*, so a track that is loud everywhere and
    one that is silent everywhere both drive nothing -- and the digital silence every other
    fixture in this file uses would compile a script that writes the resting value 120 times and
    produce a picture identical to the undriven one. That would be a test that could not fail.
    """
    content = BytesIO()
    with wave.open(content, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        frames = bytearray()
        for index in range(int(seconds * rate)):
            moment = index / rate
            decay = math.exp(-(moment % 0.5) * 25.0)
            value = int(28000 * decay * math.sin(2 * math.pi * 60.0 * moment))
            frames += struct.pack("<h", max(-32767, min(32767, value)))
        target.writeframes(bytes(frames))
    return content.getvalue()


def frame_checksums(path: Path) -> list[str]:
    """One md5 per decoded frame of a file, ffmpeg's own `framemd5`.

    Read per frame rather than over the file because of R-20: multi-threaded libx264 is not
    bit-exact, so two runs of one identical chain can differ as whole files. Frame by frame, a
    difference is localised to the frames the drive was active over instead of being smeared
    across a container, and the frames the drive was *not* active over are available as the
    control.
    """
    output = subprocess.run(
        # `-map 0:v:0`, which is not optional: an export carries a song, and `framemd5` over a
        # file with two streams interleaves the audio packets' hashes with the pictures'. Read
        # without it, "the frames that moved" is a list of positions in a mixture and a video
        # frame's index in that list depends on how the audio packed -- which is how a first
        # draft of this test read a bound Shot's drive as reaching into its unbound neighbour.
        ["ffmpeg", "-v", "error", "-i", path.as_posix(), "-map", "0:v:0", "-f", "framemd5", "-"],
        check=True, capture_output=True, text=True,
    ).stdout
    return [
        line.split(",")[-1].strip()
        for line in output.splitlines()
        if line and not line.startswith("#")
    ]


def bind_exposure(client, project_id: str, shot_id: str = "shot_a", index: int = 0, **settings):
    """One binding written the only way one can be: through its own route."""
    binding = {"parameter": "amount", "drive": "punch", "depth": 0.8, "band_centre": 0.0,
               "band_width": 0.3, "band_softness": 0.35, "floor": 0.0}
    binding.update(settings)
    return client.put(
        f"/api/projects/{project_id}/shots/{shot_id}/effects/{index}/bindings",
        json={"effect": "exposure", "bindings": [binding]},
    )


def a_project_ready_to_be_bound(client, store, tmp_path: Path):
    """An 8 s project, measured, whose first Shot carries Exposure at a resting 0.2.

    The resting value is deliberately **not** the identity. At 0 the card composes no stage at all
    unless it is bound, so the undriven comparison would be "no `eq` in the chain" against "an
    `eq` driven by the music", and a difference in the frames would prove only that an `eq` ran.
    At 0.2 both chains carry the identical `eq=brightness=0.2` stage and the only difference
    between them is the `sendcmd` -- which is what "the undriven render of the same chain" has to
    mean for the comparison to say anything at all.
    """
    project_id, shots_dir = project_with_two_approved_takes(
        client, store, tmp_path, song_bytes=beaty_wav_bytes(8.0)
    )
    analysed = client.post(f"/api/projects/{project_id}/song/analyze")
    assert analysed.status_code == 200, analysed.text
    graded = client.put(
        f"/api/projects/{project_id}/shots/shot_a/effects",
        json={"effects": [{"effect": "exposure", "parameters": {"amount": 0.2}}]},
    )
    assert graded.status_code == 200, graded.text
    return project_id, shots_dir


def test_a_binding_written_through_its_own_route_drives_the_exported_frames(tmp_path: Path):
    """The slice's first acceptance criterion, proved on the pictures and not on the script.

    Two exports of one project, in one run, from one identical chain -- once with the binding on
    and once with it off. The bound Shot's frames differ; its unbound neighbour, which shares the
    export, the encoder and the preset, does not move at all.
    """
    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = a_project_ready_to_be_bound(client, store, tmp_path)
    media = tmp_path / "projects" / project_id / "media"

    undriven = client.post(f"/api/projects/{project_id}/assemble", json={"preset": "draft"})
    assert undriven.status_code == 200, undriven.text
    before = frame_checksums(media / undriven.json()["export"])

    assert bind_exposure(client, project_id).status_code == 200
    driven = client.post(f"/api/projects/{project_id}/assemble", json={"preset": "draft"})
    assert driven.status_code == 200, driven.text
    after = frame_checksums(media / driven.json()["export"])

    # 8 s at 24 fps on the cumulative grid: 96 video frames each for shot_a and shot_b.
    assert len(before) == len(after) == 192, (len(before), len(after))
    moved = [index for index, (a, b) in enumerate(zip(before, after)) if a != b]
    assert moved, (
        "the export's frames are byte-identical with the binding on, which is exactly what a "
        "mistargeted sendcmd looks like: rc 0, no warning, and nothing driven"
    )
    # shot_a is the first 96 frames of the export and shot_b is the rest. Only the bound Shot
    # moved -- so what changed is this Shot's chain and not the encoder having a different day.
    assert max(moved) < 96, moved[-5:]
    # And it did not move from its own first frame. `punch` measures a transient against a running
    # average that starts cold, so the opening frames sit at the resting value and are identical
    # to the undriven render; a drive that moved *every* frame would be a constant offset wearing
    # a binding's clothes.
    assert before[0] == after[0]

    assert comfy.prompts == []


def test_the_compiled_script_reaches_ffmpeg_as_a_bare_relative_name_in_its_own_directory(
    tmp_path: Path, monkeypatch
):
    """R-30's remedy at the export, and `run_tool`'s new `cwd` with it.

    The script goes into the export's own `workdir`, beside `clips.txt` and the intermediates, and
    the chain names it with no path at all -- so nothing the filtergraph splits on can appear in
    it and no absolute path can reach the composed chain, which is `preview_fingerprint`'s fourth
    input. The trim that reads it is spawned with that directory as its working directory; every
    other invocation in the same export is spawned with none.
    """
    client, store, _comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = a_project_ready_to_be_bound(client, store, tmp_path)
    assert bind_exposure(client, project_id).status_code == 200

    spawned: list[dict] = []
    real_exec = asyncio.create_subprocess_exec

    async def watched(*args, **kwargs):
        cwd = kwargs.get("cwd")
        spawned.append({
            "args": [str(part) for part in args],
            "cwd": cwd,
            # Read now: the route deletes `workdir` in its own `finally`, and the moment the
            # trim runs is the only moment this claim is about.
            "beside": sorted(p.name for p in Path(cwd).glob("*.cmds")) if cwd else [],
        })
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", watched)
    response = client.post(f"/api/projects/{project_id}/assemble", json={"preset": "draft"})
    assert response.status_code == 200, response.text

    driven = [call for call in spawned if any("sendcmd=f=" in part for part in call["args"])]
    assert len(driven) == 1, [call["args"] for call in spawned]
    chain = next(part for part in driven[0]["args"] if "sendcmd=f=" in part)
    name = chain.split("sendcmd=f=", 1)[1].split(",")[0]
    assert name.startswith("exposure-amount-b0-") and name.endswith(".cmds"), name
    # Bare and relative: no drive letter, no separator, nothing a filtergraph splits on.
    assert not (set(name) & set(":/\\,;=&'")), name
    # And the file of that exact name was sitting in the directory ffmpeg was standing in.
    assert driven[0]["cwd"] is not None
    assert driven[0]["beside"] == [name], driven[0]["beside"]
    assert Path(driven[0]["cwd"]).name.startswith(".work-"), driven[0]["cwd"]
    # Every other process in this export -- the probes, the unbound trim, the concat, the
    # verification -- is spawned with no working directory at all.
    assert [call["cwd"] for call in spawned if call is not driven[0]].count(None) == len(
        spawned
    ) - 1


def test_an_export_records_the_bindings_that_drove_it(tmp_path: Path):
    """FX-25's second reserved slot, filled by the epic that reserved it.

    `effects` alone cannot tell a look that surged on the kick from one that sat still: both
    record the same resting numbers. What is stored is the **agreed** binding, every setting the
    Director never touched filled in from the catalogue, for the reason `exported_look` stores
    resolved values -- the manifest's own copy is sparse, and a record taken from it would stop
    being readable the day a default moved.
    """
    client, store, _comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = a_project_ready_to_be_bound(client, store, tmp_path)
    assert bind_exposure(client, project_id).status_code == 200

    response = client.post(f"/api/projects/{project_id}/assemble", json={"preset": "draft"})
    assert response.status_code == 200, response.text
    look = response.json()["job"]["look"]

    assert look["effects"] == ['shot_a=exposure:{"amount":0.2}']
    assert look["bindings"] == [
        (
            'shot_a=exposure.amount:{"band_centre":0,"band_softness":0.35,"band_width":0.3,'
            '"depth":0.8,"drive":"punch","floor":0,"hold":0.8,"sustain":1.5}'
        )
    ]
    assert look["transitions"] == []


def test_an_export_whose_envelope_stopped_describing_the_song_refuses_by_name(tmp_path: Path):
    """Story 10.4's export criterion, which is the half of that story this slice ships.

    `SongAnalysis` derives validity at read time from the song's own bytes, so a song replaced by
    a route that never thought about bindings leaves every binding pointing at a measurement of a
    track this project no longer has -- with nothing stored saying so. The state is reached here
    the way a hand-edited manifest reaches it, which is the same state and is reachable without a
    second song file.

    It refuses rather than rendering undriven, and that is the whole point: an undriven export
    succeeds, at rc 0, and says nothing. The binding is untouched by the refusal, which is the
    other half of the story -- re-analyze and it is live again with its stored values.
    """
    client, store, _comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = a_project_ready_to_be_bound(client, store, tmp_path)
    assert bind_exposure(client, project_id).status_code == 200
    stored = [
        spec.model_dump() for spec in store.get(project_id).shots[0].effects
    ]

    project = store.get(project_id)
    project.song.analysis.song_fingerprint = "12-notthesongthatisonthedisk"
    store.save(project)

    refused = client.post(f"/api/projects/{project_id}/assemble", json={"preset": "draft"})

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == BINDING_WITHOUT_ENVELOPE_REFUSAL.format(
        shot="SHOT 01 (shot_a)", reason=SONG_ENVELOPE_SONG_CHANGED
    )
    # Retained and reported unresolvable, never dropped and never silently zeroed (FX-15).
    assert [spec.model_dump() for spec in store.get(project_id).shots[0].effects] == stored
    # And nothing was written: no job record for an export that never started.
    assert store.get(project_id).jobs == []

    assert client.post(f"/api/projects/{project_id}/song/analyze").status_code == 200
    assert client.post(
        f"/api/projects/{project_id}/assemble", json={"preset": "draft"}
    ).status_code == 200


def test_an_export_with_no_binding_spawns_exactly_the_process_it_always_spawned(
    tmp_path: Path, monkeypatch
):
    """R-20 and this slice's seventh constraint, asserted on the argv and never on the mp4 --
    which is the whole of what that ruling says a determinism claim may be about.

    The Shot here carries a real Effect Stack and no binding. No `sendcmd` stage appears in any
    command line, no `.cmds` file is written anywhere under the export's workdir, and no
    invocation is handed a working directory -- so the process this export spawns is the process
    it spawned before this epic existed, environment and command line alike.
    """
    client, store, _comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = a_project_ready_to_be_bound(client, store, tmp_path)

    spawned: list[dict] = []
    real_exec = asyncio.create_subprocess_exec

    async def watched(*args, **kwargs):
        spawned.append({"args": [str(part) for part in args], "cwd": kwargs.get("cwd")})
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", watched)
    response = client.post(f"/api/projects/{project_id}/assemble", json={"preset": "draft"})
    assert response.status_code == 200, response.text

    assert spawned, "no ffmpeg process was spawned"
    assert [call["cwd"] for call in spawned] == [None] * len(spawned)
    for call in spawned:
        assert not any("sendcmd" in part for part in call["args"]), call["args"]
    assert not list((tmp_path / "projects" / project_id / "media").rglob("*.cmds"))
def test_two_shots_with_one_binding_are_driven_by_their_own_stretches_of_the_song(
    tmp_path: Path, monkeypatch
):
    """The one piece of arithmetic this epic adds, and the one the artefacts do not address.

    The drive's clock is the **song's** and the filter graph's is the **clip's**: `trim_args`
    prepends `setpts=PTS-STARTPTS`, so ffmpeg's `t` is zero at the first frame of every clip. A
    binding therefore cannot simply be handed the song's own times -- `build_effect_stages` is
    given the Shot's start and the clip's offset inside it, adds them, and measures every compiled
    time back from there.

    Two Shots carrying the character-for-character identical binding, four seconds apart in one
    song, are what makes that visible. They compile **two different scripts**, because they are
    listening to two different stretches of one measurement -- and because a script's filename
    carries a digest of its own text, two different scripts are two different files. Handed the
    song's start for both, they would compile one script, share one file, and the second Shot
    would flash on the first Shot's beats.
    """
    client, store, _comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = a_project_ready_to_be_bound(client, store, tmp_path)
    assert client.put(
        f"/api/projects/{project_id}/shots/shot_b/effects",
        json={"effects": [{"effect": "exposure", "parameters": {"amount": 0.2}}]},
    ).status_code == 200
    assert bind_exposure(client, project_id, "shot_a").status_code == 200
    assert bind_exposure(client, project_id, "shot_b").status_code == 200

    scripts: list[dict[str, str]] = []
    real_exec = asyncio.create_subprocess_exec

    async def watched(*args, **kwargs):
        cwd = kwargs.get("cwd")
        if cwd is not None:
            # Read at the moment the trim runs: the route deletes `workdir` in its own `finally`.
            scripts.append({
                path.name: path.read_text(encoding="utf-8")
                for path in Path(cwd).glob("*.cmds")
            })
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", watched)
    response = client.post(f"/api/projects/{project_id}/assemble", json={"preset": "draft"})
    assert response.status_code == 200, response.text

    assert len(scripts) == 2, "both bound Shots should have been trimmed with a working directory"
    first, second = scripts
    # The second trim sees both files, because the first one wrote its own into the shared
    # workdir and nothing cleans up between clips.
    assert len(second) == 2, sorted(second)
    assert len(set(second.values())) == 2, (
        "the two Shots compiled the identical script, so the later one is being driven by the "
        "opening of the song rather than by its own four seconds of it"
    )
    # Both start at clip-local zero and both address their own labelled stage: the times are the
    # clip's, and only the values differ.
    for text in second.values():
        assert text.startswith("0 eq@b0 brightness "), text[:60]
        assert next(iter(first.values())).splitlines()[0].split()[1] == "eq@b0"
def test_a_disabled_bound_card_neither_drives_an_export_nor_refuses_one(tmp_path: Path):
    """A card the Director switched off composes no stage, so nothing addressed it and nothing
    was driven -- the rule the look record already applies, applied to the question of whether an
    export needs a measurement at all.

    Without it a Shot whose bound card was switched off would refuse its own export over an
    envelope that could not have reached the picture, and would pay for reading one on every
    export that did succeed.
    """
    client, store, _comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = a_project_ready_to_be_bound(client, store, tmp_path)
    assert bind_exposure(client, project_id).status_code == 200
    switched_off = client.put(
        f"/api/projects/{project_id}/shots/shot_a/effects",
        json={"effects": [{
            "effect": "exposure", "enabled": False, "parameters": {"amount": 0.2},
            "bindings": [{"parameter": "amount", "drive": "punch", "depth": 0.8,
                          "band_centre": 0.0, "band_width": 0.3, "band_softness": 0.35,
                          "floor": 0.0}],
        }]},
    )
    assert switched_off.status_code == 200, switched_off.text

    project = store.get(project_id)
    project.song.analysis.song_fingerprint = "12-notthesongthatisonthedisk"
    store.save(project)

    response = client.post(f"/api/projects/{project_id}/assemble", json={"preset": "draft"})

    assert response.status_code == 200, response.text
    # And the record says the same thing the picture does: nothing composed, nothing driving.
    assert response.json()["job"]["look"] == {
        "effects": [], "bindings": [], "transitions": []
    }
    # The binding is still there, waiting for the card to be switched back on.
    assert store.get(project_id).shots[0].effects[0].bindings != []


def a_project_whose_bound_shot_is_buried(client, store, tmp_path: Path):
    """Two Shots on the identical window over a 4 s song, the bound one underneath.

    `assembly_plan` resolves an overlap as layers, later on top — so the second Shot in the
    manifest covers the first completely and the first contributes **no frames**. It is still in
    `ExportSubject.clips`, which is every Shot the manifest holds, and not in `plan.clips`, which
    is what the export will cut.

    The windows are moved through the store rather than through `PUT .../shots`, which refuses a
    save that would change an approval, and the snapshots are moved with them so
    `assembly_refusals`' staleness check is not what answers.
    """
    project_id, _shots_dir = project_with_two_approved_takes(
        client, store, tmp_path, song_bytes=beaty_wav_bytes(4.0)
    )
    project = store.get(project_id)
    for shot in project.shots:
        shot.start = 0.0
        shot.duration = 4.0
        shot.approved_start = 0.0
        shot.approved_duration = 4.0
    store.save(project)
    assert client.post(f"/api/projects/{project_id}/song/analyze").status_code == 200
    graded = client.put(
        f"/api/projects/{project_id}/shots/shot_a/effects",
        json={"effects": [{"effect": "exposure", "parameters": {"amount": 0.2}}]},
    )
    assert graded.status_code == 200, graded.text
    assert bind_exposure(client, project_id).status_code == 200
    return project_id


def test_a_bound_shot_that_renders_no_frame_still_refuses_the_export(tmp_path: Path):
    """A5, and it is a **ruling** rather than a repair: the answer is that the refusal stands.

    A Shot buried under a later one contributes nothing to the export, so refusing over it looks
    like refusing over nothing. Three things settle it the other way, and the second is the one
    that could only be had by running it.

    * The check cannot see the plan. `ExportSubject.plan` is `None` for every plan-stage check by
      construction — they run *before* `assembly_plan` — so "skip the buried Shot" means moving
      this check to the composition stage and leaving the two stack checks behind, disagreeing.
    * **Measured in the test below: the checks that share this clip source already refuse over a
      buried Shot**, and the oldest of them, `assembly_refusals`, has done so since before any
      effect existed in this application. The binding check is the last member of a consistent
      family, not the one exception.
    * The refusal's own remedy is *analyse the song again* — one gesture that clears every bound
      Shot in the project — and not *unbind this one*. A Director is never held by a Shot they
      cannot see.

    The mirror is the state that would be worse: an export that quietly ignores a buried Shot
    succeeds today and refuses tomorrow, when the covering Shot is dragged aside and a binding
    nobody touched surfaces over a song replaced weeks ago.

    The fixture proves the burial rather than assuming it: the healthy export cuts **one** clip,
    names one take, and records an empty look — the bound Shot is in none of the three.
    """
    client, store, _comfy, _app = make_client(tmp_path)
    project_id = a_project_whose_bound_shot_is_buried(client, store, tmp_path)

    healthy = client.post(f"/api/projects/{project_id}/assemble", json={"preset": "draft"})
    assert healthy.status_code == 200, healthy.text
    body = healthy.json()
    assert body["clip_count"] == 1, "shot_a is not actually buried, so this test proves nothing"
    assert body["job"]["inputs"] == [
        f"shot_b=music-video-producer/{project_id}/shots/shot_b-h3_00001-audio.mp4"
    ]
    # And the composition stage, which iterates `plan.clips`, records nothing for the buried Shot
    # — its look never composed and never ran.
    assert body["job"]["look"] == {"effects": [], "bindings": [], "transitions": []}

    project = store.get(project_id)
    project.song.analysis.song_fingerprint = "12-notthesongthatisonthedisk"
    store.save(project)

    refused = client.post(f"/api/projects/{project_id}/assemble", json={"preset": "draft"})

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == BINDING_WITHOUT_ENVELOPE_REFUSAL.format(
        shot="SHOT 01 (shot_a)", reason=SONG_ENVELOPE_SONG_CHANGED
    )


def test_every_plan_check_answers_the_same_way_about_a_buried_shot(tmp_path: Path):
    """The measurement A5's ruling rests on, and the guard that keeps the four in step.

    Each of these faults is put on the **buried** Shot, one at a time, and each is expected to
    refuse the export by that Shot's name. If a later change decides a buried Shot should be
    skipped, this test says out loud that the decision is about all of them and not about one.
    """
    client, store, _comfy, _app = make_client(tmp_path)
    project_id = a_project_whose_bound_shot_is_buried(client, store, tmp_path)
    manifest = store.manifest_path(project_id)
    label = "SHOT 01 (shot_a)"

    def assembled():
        return client.post(f"/api/projects/{project_id}/assemble", json={"preset": "draft"})

    # `assembly_refusals`, which predates every effect here: no approved take.
    project = store.get(project_id)
    approved = project.shots[0].approved_output
    project.shots[0].approved_output = ""
    store.save(project)
    unapproved = assembled()
    assert unapproved.status_code == 422, unapproved.text
    assert ASSEMBLY_UNAPPROVED_REFUSAL.format(shot=label) in unapproved.json()["detail"]

    # `_effect_stack_refusals`: a value the catalogue will not agree to, hand-edited past the
    # write route the way a manifest actually goes wrong.
    project = store.get(project_id)
    project.shots[0].approved_output = approved
    store.save(project)
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    sound_effects = raw["shots"][0]["effects"]
    raw["shots"][0]["effects"] = [
        {"effect": "exposure", "enabled": True, "parameters": {"amount": 99.0}, "bindings": []}
    ]
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    impossible = assembled()
    assert impossible.status_code == 422, impossible.text
    assert impossible.json()["detail"].startswith(f"{label}: exposure's amount is 99")

    # `_oversized_stack_refusals`: past the card limit.
    raw["shots"][0]["effects"] = [
        {"effect": "grain", "enabled": True, "parameters": {"strength": 8}, "bindings": []}
    ] * (SHOT_EFFECT_STACK_LIMIT + 1)
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    oversized = assembled()
    assert oversized.status_code == 422, oversized.text
    assert oversized.json()["detail"] == f"{label}: " + SHOT_EFFECTS_TOO_MANY_REFUSAL.format(
        limit=SHOT_EFFECT_STACK_LIMIT, count=SHOT_EFFECT_STACK_LIMIT + 1
    )

    # `_binding_envelope_refusals`: the last of them, and the one A5 asked about.
    raw["shots"][0]["effects"] = sound_effects
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    project = store.get(project_id)
    project.song.analysis.song_fingerprint = "12-notthesongthatisonthedisk"
    store.save(project)
    bound = assembled()
    assert bound.status_code == 422, bound.text
    assert bound.json()["detail"] == BINDING_WITHOUT_ENVELOPE_REFUSAL.format(
        shot=label, reason=SONG_ENVELOPE_SONG_CHANGED
    )


def test_a_sidecar_element_that_is_not_a_number_refuses_the_export_by_name(tmp_path: Path):
    """B3 at the export, which was the third of the three routes that answered 500.

    `song_measurement_verdict` reads `band_count`, `analysis_rate` and `len(bands)` and nothing
    inside the rows, so a poisoned sidecar is *current* and the export walks straight into the
    compiler. Before 2026-08-28 a string or a `null` came back out as `ValueError`/`TypeError`
    past `except EffectRefusal`, and a `NaN` came back out as a **successful export** whose
    binding had silently collapsed to its resting value.
    """
    from music_video_producer.effects import BINDING_NO_ENVELOPE_REFUSAL

    client, store, _comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = a_project_ready_to_be_bound(client, store, tmp_path)
    assert bind_exposure(client, project_id).status_code == 200
    sidecar = store.song_envelope_path(project_id)
    sound = json.loads(sidecar.read_text(encoding="utf-8"))
    refusal = BINDING_NO_ENVELOPE_REFUSAL.format(effect="exposure", parameter="amount")

    for poison in ("loud", None, True, float("nan"), float("inf")):
        payload = json.loads(json.dumps(sound))
        payload["bands"][0][7] = poison
        sidecar.write_text(json.dumps(payload), encoding="utf-8")

        refused = client.post(f"/api/projects/{project_id}/assemble", json={"preset": "draft"})

        assert refused.status_code == 422, (repr(poison), refused.text)
        assert refused.json()["detail"].endswith(refusal), (repr(poison), refused.text)
        # Nothing half-started: the refusal happens before the job record is written.
        assert store.get(project_id).jobs == [], repr(poison)
