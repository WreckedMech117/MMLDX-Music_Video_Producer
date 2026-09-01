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
from music_video_producer.effects import BRANCH_FRAME_GUARD
from music_video_producer.models import (
    EffectSpec,
    Project,
    RenderJob,
    Shot,
    TransitionSpec,
    shot_label,
)
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


def synthesize_detailed_take(path: Path, seconds: float, size: str = "128x72"):
    """A take with **detail** in it, which a blur needs and a colour source does not have.

    `synthesize_take` writes a flat colour field, and every other test here is right to: it is
    what a real take is in miniature, and a flat picture makes a fade, a grade and a `sendcmd`
    ramp all measurable. A **blur** is the one treatment it cannot measure -- a Gaussian blur of a
    uniform field is that same uniform field, at any sigma -- and the first draft of
    `test_a_one_sided_blur_ramp_...` read a working ramp as "the ramp was discarded" for exactly
    that reason, which is the fixture-makes-its-own-defect-impossible shape in its other
    direction: here the fixture made a *pass* impossible, and it could as easily have hidden a
    real failure behind a control that also did nothing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=24",
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


#: Where the overlay Shot sits inside `shot_a`'s window, in seconds of song.
#:
#: Both ends are chosen and neither is arbitrary. **On the 24 fps export grid and off the 30 Hz
#: analysis grid**: 1.25 s and 2.75 s are exactly 30 and 66 frames, so the split clip is cut the
#: same length the unsplit Shot's own frames measure and the two are comparable line for line —
#: while 2.75 x 30 is 82.5, half way between two analysis ticks, which is the state every binding
#: fixture in this repository used to avoid by accident. And 2.75 s is **not** a whole number of
#: `beaty_wav_bytes`' half-second bursts, so the stretch of song a doubled offset would reach is
#: half a beat out of phase with the one the clip actually plays over rather than a copy of it.
OVERLAY_START = 1.25
OVERLAY_END = 2.75


def an_overlay_laid_over_the_bound_shots_middle(store, project_id: str, shots_dir: Path):
    """A third Shot covering the middle of `shot_a`, so `shot_a` resolves to **two clips**.

    `assembly_plan` cuts a Shot around any later-starting Shot nested inside it and the
    underneath one resumes when the overlay ends -- so this is the ordinary way one Shot becomes
    two clips, and it is the case `build_effect_stages`' `shot_start` plus `clip_offset`
    arithmetic exists for.

    Laid in through the store for the reason the buried fixture gives: `PUT .../shots` is not the
    gesture under test here, and the overlay needs an approved take with its own window snapshot.
    """
    synthesize_take(shots_dir / "shot_c-h3_00001-audio.mp4", 4.458, colour="green")
    output = f"music-video-producer/{project_id}/shots/shot_c-h3_00001-audio.mp4"
    project = store.get(project_id)
    project.shots.append(
        Shot(
            id="shot_c",
            start=OVERLAY_START,
            duration=OVERLAY_END - OVERLAY_START,
            prompt="Green room",
            status="complete",
            latest_output=output,
            approved_output=output,
            approved_start=OVERLAY_START,
            approved_duration=OVERLAY_END - OVERLAY_START,
        )
    )
    store.save(project)


def scripts_each_bound_clip_compiled(client, monkeypatch, project_id: str) -> list[str]:
    """One export, and the `sendcmd` script each bound **clip** was handed, in clip order.

    Every clip of one export shares one working directory and a script's filename is a digest of
    its own text, so "this clip's script" is the file that was not there before this clip's trim
    ran. Read at the moment the trim spawns, because the route deletes the directory in its own
    `finally`; a clip that compiled no script is handed no working directory at all and does not
    appear here.
    """
    compiled: list[str] = []
    seen: set[str] = set()
    real_exec = asyncio.create_subprocess_exec

    async def watched(*args, **kwargs):
        cwd = kwargs.get("cwd")
        if cwd is not None:
            written = {
                path.name: path.read_text(encoding="utf-8")
                for path in Path(cwd).glob("*.cmds")
            }
            fresh = sorted(name for name in written if name not in seen)
            seen.update(fresh)
            assert len(fresh) == 1, (fresh, sorted(seen))
            compiled.append(written[fresh[0]])
        return await real_exec(*args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(asyncio, "create_subprocess_exec", watched)
        response = client.post(f"/api/projects/{project_id}/assemble", json={"preset": "draft"})
    assert response.status_code == 200, response.text
    return compiled


def driven_values(script: str) -> list[str]:
    """Every command's argument in a script, in order -- the drive, as the export writes it."""
    return [line[:-1].split(" ")[3] for line in script.splitlines()]


def test_a_shot_split_by_an_overlay_drives_its_second_clip_from_its_own_place_in_the_song(
    tmp_path: Path, monkeypatch
):
    """The export's own clip arithmetic, exercised where its two terms are not both zero.

    `app.py` hands `build_effect_stages` the Shot's `approved_start` and the clip's offset inside
    it, and the compiler adds them to get the song second this clip's first frame lands on. Until
    an overlay splits a Shot, **every bound Shot in every route-level test resolves to exactly
    one clip** -- so `clip.start == clip.approved_start`, `clip_offset` is 0, and the two terms
    collapse into each other. Handing the compiler `clip.start` instead compiles the identical
    text for every other test in this file, and would drive a split Shot's second clip from
    *twice* its own offset into the song: 5.5 s here rather than 2.75 s, half a beat out of
    phase.

    The proof is the one `test_sendcmd` makes on the compiler directly, made here on what the
    **route** hands it, and it needs no second derivation of the drive: the same Shot is exported
    twice, once whole and once split. Whole, it compiles one script over its four seconds of
    song. Split, its two clips must compile the **head** and the **tail** of exactly that script
    -- the same values, at the same places in the song, re-timed to each clip's own zero. A clip
    driven from anywhere else in the song is a run of values that is nowhere in the whole Shot's.

    Two more things are true here because of where the overlay ends, and both are stated so a
    reader knows what a diff means. 2.75 s is **half way between two analysis ticks** at 30 Hz,
    so the tick that covers the second clip's first frame begins 0.0167 s *before* that frame:
    the walk must open on that tick -- `floor`, not `ceil` -- and must stamp it at zero rather
    than at a negative second `sendcmd` would reject. Both are asserted below, and neither could
    fail on a Shot whose start lands on a tick.
    """
    client, store, _comfy, _app = make_client(tmp_path)
    project_id, shots_dir = a_project_ready_to_be_bound(client, store, tmp_path)
    assert bind_exposure(client, project_id).status_code == 200

    whole = scripts_each_bound_clip_compiled(client, monkeypatch, project_id)
    assert len(whole) == 1, "shot_a should be one clip before the overlay is laid over it"

    an_overlay_laid_over_the_bound_shots_middle(store, project_id, shots_dir)
    split = scripts_each_bound_clip_compiled(client, monkeypatch, project_id)

    assert len(split) == 2, "the overlay did not split shot_a, so this test proves nothing"
    head, tail = split
    assert head != tail
    # Each clip's own clock starts at zero, whichever tick it opens on (`setpts=PTS-STARTPTS`).
    assert head.startswith("0 eq@b0 brightness ")
    assert tail.startswith("0 eq@b0 brightness ")
    assert all(not line.startswith("-") for line in tail.splitlines()), tail[:80]

    # The two clips are the two ends of the one script, value for value: the first clip plays the
    # song from where the Shot starts, and the second picks it up where the overlay lets go.
    whole_values = driven_values(whole[0])
    head_values = driven_values(head)
    tail_values = driven_values(tail)
    assert head_values == whole_values[: len(head_values)]
    assert tail_values == whole_values[-len(tail_values):]
    # And the tail is a real stretch of drive rather than a run of one repeated number, which is
    # the one way the comparison above could hold while proving nothing.
    assert len(set(tail_values)) > 1, tail_values
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
    # The card's own id travels with the write, which is what the panel does and what a bound
    # Shot's stack write has had to do since R-33: the bindings are adopted from the card of that
    # id, and a body naming none on a bound Shot is refused rather than quietly losing one.
    card = store.get(project_id).shots[0].effects[0].id
    switched_off = client.put(
        f"/api/projects/{project_id}/shots/shot_a/effects",
        json={"effects": [{
            "id": card,
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


# ------------------------------------------------------------------------------------------
# Transitions, through real ffmpeg (story 11.1). **This is the half a pinned argv cannot do.**
#
# The failure this slice is built around is a *short render at rc 0*: `xfade` with legs of
# unequal length silently truncates to the shorter one, and `-frames:v` caps from above only. No
# argv assertion can see it, and neither can an exit code. So the frame count is counted on the
# written file, against `clip_frames_on_grid` — which is exactly what
# `effects.BRANCH_FRAME_GUARD`'s own docstring says has to be done for a branched graph, and an
# `xfade` graph is two branched legs.
# ------------------------------------------------------------------------------------------


def rendered_frames(path: Path) -> int:
    """The frames the file actually holds, counted rather than read off a header.

    `nb_read_frames` decodes; `nb_frames` is a container field a muxer may write from an
    intention. The whole point here is to disbelieve the intention.
    """
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
            "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", path.as_posix(),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(result.stdout.strip())


def kept_intermediates(monkeypatch, keep_root: Path) -> list[Path]:
    """Every intermediate the export wrote, copied out before the route deletes its workdir.

    The route builds its clips inside `exports/.work-<job id>/` and `shutil.rmtree`s it in a
    `finally`, so an intermediate cannot be looked for after the request. `run_tool` is a closure
    inside `create_app` and is not patchable by name, so the interception is at the deletion:
    the tree is copied, then removed exactly as it would have been.

    Returns the list the copies land in — `clip_000.mp4`, `clip_001.mp4`, … in plan order, which
    is the order `plan.clips` is in, so the transition segment is at its own plan index.

    **The `.cmds` scripts are copied too, under their own names** (story 11.4). A compiled drive
    script and a one-sided blur's ramp are both read by ffmpeg as a bare relative name with the
    workdir as its cwd, so a test that wants to re-run a recorded argv needs the file that argv
    names sitting beside it — and the workdir is gone by the time the request returns.
    """
    import shutil as shutil_module

    copies: list[Path] = []
    real_rmtree = shutil_module.rmtree

    def keep(path, *args, **kwargs):
        source = Path(path)
        if source.is_dir():
            for item in sorted(source.glob("clip_*.mp4")):
                copy = keep_root / f"kept-{item.name}"
                copy.write_bytes(item.read_bytes())
                copies.append(copy)
            for script in sorted(source.glob("*.cmds")):
                (keep_root / script.name).write_bytes(script.read_bytes())
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil_module, "rmtree", keep)
    return copies


def set_transition(client, project_id: str, shot_id: str, type_: str | None):
    return client.put(
        f"/api/projects/{project_id}/shots/{shot_id}/transitions",
        json={"transition_out": None if type_ is None else {"type": type_}},
    )


def overlap_the_two_shots(client, store, tmp_path: Path, project_id: str, overlap: float = 0.5):
    """Drag the second Shot back over the first by `overlap` seconds, and re-approve both.

    Re-approved because AD-13's snapshot is what `assembly_refusals` compares the live window
    against, and moving a window after approval is a refusal by design. The plan still tiles the
    8 s song: the first Shot keeps its window and the second grows by the overlap.

    **The second take is re-synthesized longer, and that is not fixture housekeeping.** Widening
    a window past what its take holds is refused by name — `ASSEMBLY_OFFSET_OVERRUN_REFUSAL`,
    which is what the first run of these tests got — and a Director who drags a clip across its
    neighbour genuinely does widen its window. The take grows with it by the same margin the
    fixture's takes already carry (0.458 s of over-render), so this stays a fixture describing a
    plan that could exist rather than one whose only fault has been hidden.
    """
    shots_dir = (
        tmp_path / "comfy" / "output" / "music-video-producer" / project_id / "shots"
    )
    synthesize_take(
        shots_dir / "shot_b-h3_00001-audio.mp4",
        4.458 + overlap,
        size="192x108",
        colour="blue",
    )
    project = store.get(project_id)
    shots = [shot.model_dump(mode="json") for shot in project.shots]
    shots[1]["start"] = round(4.0 - overlap, 6)
    shots[1]["duration"] = round(4.0 + overlap, 6)
    saved = client.put(f"/api/projects/{project_id}/shots", json={"shots": shots})
    assert saved.status_code == 200, saved.text
    for shot_id in ("shot_a", "shot_b"):
        client.post(f"/api/projects/{project_id}/shots/{shot_id}/unapprove")
        approved = client.post(f"/api/projects/{project_id}/shots/{shot_id}/approve")
        assert approved.status_code == 200, approved.text


def test_the_rendered_transition_segment_holds_exactly_the_overlaps_frames(
    tmp_path: Path, monkeypatch
):
    """**The acceptance this slice exists for, and the only form of it that is worth anything.**

    Given two Shots whose windows overlap and a transition set on the pair, the middle entry of
    the plan is `clip_frames_on_grid(overlap_start, overlap_end)` frames — and the *written file*
    holds that many. The second claim is not implied by the first: `xfade` with legs of unequal
    length silently truncates to the shorter one at rc 0, and `-frames:v` caps from above only, so
    both the argv and the exit code agree with a segment that is a frame short.

    And the segment is a blend rather than either leg: its frames differ, frame by frame, from a
    control render of the outgoing leg alone and from one of the incoming leg alone. A segment
    that had silently rendered one input would have the right count and the wrong picture.

    The segment is also concat-identical to an ordinary intermediate — same codec, profile, pixel
    format, geometry, rate and SAR — which is what keeps the join at `-c:v copy` (FX-NFR-2).
    **Measured, because it is not free:** with no `format` pinned after the `xfade`, this file
    comes out `yuv444p` / High 4:4:4 Predictive while every other intermediate is `yuv420p` /
    High, at rc 0 and with the right frame count, and `ffmpeg -f concat -c copy` accepts the
    mismatch and writes a container declaring the first stream's format.
    """
    import music_video_producer.app as app_module
    from music_video_producer.assembly import (
        clip_frames_on_grid,
        transition_segment_args,
        trim_args,
    )

    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    overlap_the_two_shots(client, store, tmp_path, project_id, overlap=0.5)
    assert set_transition(client, project_id, "shot_a", "dissolve").status_code == 200

    segments: list[list[str]] = []
    trims: list[list[str]] = []
    real_segment = app_module.transition_segment_args
    real_trim = app_module.trim_args

    def record_segment(*args, **kwargs):
        segments.append(real_segment(*args, **kwargs))
        return segments[-1]

    def record_trim(*args, **kwargs):
        trims.append(real_trim(*args, **kwargs))
        return trims[-1]

    monkeypatch.setattr(app_module, "transition_segment_args", record_segment)
    monkeypatch.setattr(app_module, "trim_args", record_trim)
    survivors = kept_intermediates(monkeypatch, tmp_path)

    response = client.post(f"/api/projects/{project_id}/assemble")
    assert response.status_code == 200, response.text

    # Three entries across the boundary, and the middle one is the Overlap's frames on the grid.
    assert response.json()["clip_count"] == 3
    expected = clip_frames_on_grid(3.5, 4.0)
    assert expected == 12
    assert len(segments) == 1 and len(survivors) == 3
    # Plan order, so the blend is the middle entry: A truncated, the segment, B from the
    # Overlap's end. That is AD-18's three, on disk.
    segment = survivors[1]

    # **The rendered count.** Not the argv's cap, not the plan's arithmetic: the frames the file
    # decodes to.
    assert rendered_frames(segment) == expected

    # Concat-identical to every other intermediate, which is what `-c:v copy` needs. Compared
    # against an ordinary `trim_args` output built from the same take at the same geometry, so
    # this is the real neighbour's shape rather than a list of values written down here.
    shape = "stream=codec_name,profile,pix_fmt,width,height,r_frame_rate,sample_aspect_ratio"
    neighbour = tmp_path / "neighbour.mp4"
    subprocess.run(
        trim_args(
            Path(trims[1][trims[1].index("-i") + 1]), neighbour, expected, 192, 108
        ),
        check=True,
        capture_output=True,
    )
    assert probe(segment, shape) == probe(neighbour, shape)
    assert "yuv420p" in probe(segment, shape)

    # And it is a blend: neither leg alone, frame for frame.
    legs = []
    for source, offset in (
        (Path(trims[0][trims[0].index("-i") + 1]), 3.5),
        (Path(trims[1][trims[1].index("-i") + 1]), 0.0),
    ):
        control = tmp_path / f"control-{len(legs)}.mp4"
        subprocess.run(
            transition_segment_args(
                source, source, control, expected, 192, 108, "fade",
                before_offset=offset, after_offset=offset,
            ),
            check=True,
            capture_output=True,
        )
        legs.append(frame_checksums(control))
    blend = frame_checksums(segment)
    assert len(blend) == expected
    for index, control in enumerate(legs):
        assert blend != control, f"the segment rendered leg {index} rather than a blend"
    assert comfy.prompts == []


def project_with_three_detailed_takes(client, tmp_path: Path):
    """Three Shots over a 12 s song, blending at an **interior** boundary, on takes with detail.

    Built because the two-Shot fixtures beside it cannot fail in the way the transition legs fail,
    and all four reasons are this epic's own documented failure mode:

    * **every transition fixture puts its Shots at song second 0 or writes no lead at all.**
      `timeline.over_render_lead` is 0.25 s for every interior Shot and exactly 0.0 only at second
      0, so a take offset dropped from a leg is the identity there. Three mutations on
      `_paired_transitions`' offset terms survived the whole suite on that alone. Here the two
      Shots that blend are both interior and carry **different** leads, so a leg reading the other
      leg's offset fails too;
    * **`synthesize_take` writes a uniform colour field**, and reading a uniform field at the wrong
      second is a byte-identical picture. `testsrc2` moves every frame, which is F2's own lesson --
      *a fixture must contain the thing under test* -- learned there for `gblur` and not carried
      across to `trim`.

    The plan tiles the song and the boundary is a real 0.5 s Overlap between the second and third
    Shots. Each take runs longer than its window by its own lead plus a margin, the way a real
    over-rendered take does.
    """
    project_id = client.post("/api/projects", json={"name": "Detailed"}).json()["id"]
    upload = client.post(
        f"/api/projects/{project_id}/songs/upload",
        data={"title": "Detailed Song", "duration": "0"},
        files={"file": ("song.wav", wav_bytes(12.0), "audio/wav")},
    )
    assert upload.status_code == 200, upload.text
    shots_dir = (
        tmp_path / "comfy" / "output" / "music-video-producer" / project_id / "shots"
    )
    prefix = f"music-video-producer/{project_id}/shots"
    windows = (
        # id, start, duration, recorded lead, take seconds
        ("shot_a", 0.0, 4.0, 0.0, 4.5),
        ("shot_b", 4.0, 4.0, 0.25, 4.75),
        ("shot_c", 7.5, 4.5, 0.375, 5.25),
    )
    for shot_id, _start, _duration, _lead, seconds in windows:
        synthesize_detailed_take(shots_dir / f"{shot_id}-h3_00001-audio.mp4", seconds)
    saved = client.put(
        f"/api/projects/{project_id}/shots",
        json={
            "shots": [
                {
                    "id": shot_id,
                    "start": start,
                    "duration": duration,
                    "prompt": f"Detail in {shot_id}",
                    "status": "complete",
                    "latest_output": f"{prefix}/{shot_id}-h3_00001-audio.mp4",
                    "latest_take_lead": lead,
                }
                for shot_id, start, duration, lead, _seconds in windows
            ]
        },
    )
    assert saved.status_code == 200, saved.text
    for shot_id, *_rest in windows:
        approved = client.post(f"/api/projects/{project_id}/shots/{shot_id}/approve")
        assert approved.status_code == 200, approved.text
    return project_id, shots_dir


def test_every_take_offset_a_transition_splits_is_the_shots_own_lead_advanced(
    tmp_path: Path, monkeypatch
):
    """**The three terms three surviving mutations were free to delete**, each asserted against a
    number that is not zero, and then shown on the decoded picture.

    A blend splits one boundary into three entries and every one of them reads a take at an offset:
    the outgoing leg at the Shot's own recorded lead **plus** the seconds from its start to the
    Overlap's; the incoming leg at the incoming Shot's own lead, because the Overlap begins where
    that Shot does; and the remainder at that lead advanced by the blend's own length. Drop any one
    of the three `offset` terms and every fixture in this repository went on passing, because every
    one of them put its Shots where `timeline.over_render_lead` is 0.0.

    The last two assertions are the fixture proving it can fail: the shipped segment differs, frame
    for frame, from the identical argv rendered at offset zero. On the flat colour field
    `synthesize_take` writes, those two files are byte-identical whatever the offsets say.
    """
    import music_video_producer.app as app_module
    from music_video_producer.assembly import clip_frames_on_grid, transition_segment_args

    client, _store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_three_detailed_takes(client, tmp_path)
    assert set_transition(client, project_id, "shot_b", "dissolve").status_code == 200

    segment_calls: list[tuple[tuple, dict]] = []
    trim_calls: list[tuple[tuple, dict]] = []
    real_segment = app_module.transition_segment_args
    real_trim = app_module.trim_args

    def record_segment(*args, **kwargs):
        segment_calls.append((args, kwargs))
        return real_segment(*args, **kwargs)

    def record_trim(*args, **kwargs):
        trim_calls.append((args, kwargs))
        return real_trim(*args, **kwargs)

    monkeypatch.setattr(app_module, "transition_segment_args", record_segment)
    monkeypatch.setattr(app_module, "trim_args", record_trim)
    survivors = kept_intermediates(monkeypatch, tmp_path)

    response = client.post(f"/api/projects/{project_id}/assemble")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job"]["look"]["transitions"] == ["shot_b=dissolve"]
    # A truncated, B truncated, the blend, C from the Overlap's end.
    assert body["clip_count"] == 4 and len(survivors) == 4
    frames = clip_frames_on_grid(7.5, 8.0)
    assert frames == 12

    assert len(segment_calls) == 1
    _args, kwargs = segment_calls[0]
    # `shot_b`'s own lead plus the 3.5 s from its start to the Overlap's. Without the lead it is
    # 3.5; with the wrong Shot's lead it is 3.875.
    assert kwargs["before_offset"] == 3.75
    # `shot_c`'s own lead and nothing added: the Overlap begins where `shot_c` does.
    assert kwargs["after_offset"] == 0.375
    # And the remainder is that lead advanced by the blend's own length. Without the lead it is
    # 0.5, which is the mutation that survived.
    assert [call[1]["offset"] for call in trim_calls] == [0.0, 0.25, 0.875]

    # **And the picture moves when the offset does.** Same argv, offsets zeroed: on a take with
    # detail in it these are different files, and on a flat colour field they are the same one.
    control = tmp_path / "control-at-zero.mp4"
    subprocess.run(
        transition_segment_args(
            *_args[:7], before_offset=0.0, after_offset=0.0, preset=kwargs["preset"]
        )[:-1]
        + [control.as_posix()],
        check=True,
        capture_output=True,
    )
    assert rendered_frames(control) == frames
    assert frame_checksums(survivors[2]) != frame_checksums(control)
    assert comfy.prompts == []


def test_a_transition_between_two_branched_looks_still_renders_every_frame(
    tmp_path: Path, monkeypatch
):
    """**An `xfade` graph is two branched legs**, and a branched chain loses a frame at rc 0.

    `effects.BRANCH_FRAME_GUARD` exists for exactly that — `tpad=stop=1:stop_mode=clone` at the
    head of a chain whose `fps` stage would otherwise emit one fewer than it received — and
    `build_effect_stages` prepends it per chain. Composing each leg through that builder is what
    makes the guard ride along on both.

    Measured 2026-08-28 with the guard suppressed on both legs of this very shape: thirteen frames
    asked for, **twelve written**, rc 0, nothing at `-v warning`, and `-frames:v` blind to it. So
    this is the count on the file and not the count in the argv.

    It also exercises R-41's leg namespace end to end: both Shots carry a branching effect, both
    legs start at chain slot 0, and two branches named alike in one `-filter_complex` is an ffmpeg
    error. A 200 here is the label prefix working.
    """
    import music_video_producer.app as app_module
    from music_video_producer.assembly import clip_frames_on_grid

    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    overlap_the_two_shots(client, store, tmp_path, project_id, overlap=0.5)
    for shot_id in ("shot_a", "shot_b"):
        write_stack(
            client, project_id, shot_id,
            [{"effect": "bloom", "enabled": True, "parameters": {"intensity": 0.5}}],
        )
    assert set_transition(client, project_id, "shot_a", "dissolve").status_code == 200

    graphs: list[str] = []
    real_segment = app_module.transition_segment_args

    def record_segment(*args, **kwargs):
        built = real_segment(*args, **kwargs)
        graphs.append(built[built.index("-filter_complex") + 1])
        return built

    monkeypatch.setattr(app_module, "transition_segment_args", record_segment)
    survivors = kept_intermediates(monkeypatch, tmp_path)

    response = client.post(f"/api/projects/{project_id}/assemble")
    assert response.status_code == 200, response.text
    assert len(graphs) == 1

    # R-41: each leg's branch labels carry its own leg, so one graph holds two namespaces.
    assert "[fxA0a]" in graphs[0] and "[fxB0a]" in graphs[0]
    assert "[fx0a]" not in graphs[0], "both legs would claim one label"
    # And the guard is on both legs, which is what the count below depends on.
    assert graphs[0].count(BRANCH_FRAME_GUARD) == 2

    assert rendered_frames(survivors[1]) == clip_frames_on_grid(3.5, 4.0)
    assert comfy.prompts == []


def test_the_export_with_a_transition_matches_the_song_and_records_what_it_blended(
    tmp_path: Path
):
    """FX-NFR-1 on the written file, and FX-25's third slot filled by the epic that reserved it.

    The whole export, verified the way `verification_problems` verifies it: duration within one
    frame of the song. Three entries, one of them a blend, and the frame total is the song's — the
    third entry costs the plan nothing, which is the structural rather than arithmetic argument
    AD-19 makes.

    `ExportLook.transitions` was declared empty before this epic and `test_stated_constraints.py`
    predicted the day it stopped being: *"the day Epic 11 fills it, this comment goes false with
    nothing to say so."* It reads like its two siblings — one `"<shot_id>=<value>"` line — and the
    shot id is the **outgoing** Shot's, because AD-30 makes `transition_out` authoritative.
    """
    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    overlap_the_two_shots(client, store, tmp_path, project_id, overlap=0.5)
    assert set_transition(client, project_id, "shot_a", "blur_wipe").status_code == 200

    response = client.post(f"/api/projects/{project_id}/assemble")
    assert response.status_code == 200, response.text
    body = response.json()
    export = tmp_path / "projects" / project_id / "media" / body["export"]

    assert body["clip_count"] == 3
    assert body["total_frames"] == 192
    assert abs(float(probe(export, "format=duration")) - 8.0) <= 1 / 24
    assert probe(export, "stream=codec_type").splitlines() == ["video", "audio"]
    assert body["job"]["look"]["transitions"] == ["shot_a=blur_wipe"]
    # Both takes went into the segment, so both are named among the inputs it consumed.
    assert body["job"]["inputs"].count(
        f"shot_a=music-video-producer/{project_id}/shots/shot_a-h3_00001-audio.mp4"
    ) == 2
    assert comfy.prompts == []


def test_a_blend_whose_incoming_shot_is_used_up_by_it_renders_and_the_empty_entry_costs_nothing(
    tmp_path: Path
):
    """**The Director's ruling of 2026-08-31, on the written file rather than on the plan.**

    `A[0,4] B[3,6] C[4,8]` is the geometry the ruling was taken on: `B` is truncated at `C`'s
    start, so after the blend it has no frames of its own and appears **only inside the blend**.
    The rule that shipped on 2026-08-30 refused it; it composes now, and the zero-length entry the
    split leaves falls through to the drop `assembly_plan` already makes.

    **Decoded, because in this pipeline ffmpeg's exit code is evidence of nothing** -- seven wrong
    outputs at rc 0 across three epics, `-frames:v -1` silently ignored and `-frames:v 0` writing
    a 261-byte file with no video stream among them. So the claims are made against pixels and a
    probed duration:

    * three entries and 192 frames for an 8 s song, with the empty one gone rather than written
      as a stream-less intermediate;
    * at 1.0 s the picture is `A`'s;
    * at 3.5 s -- the middle of the Overlap -- it is **neither** `A` nor `B` but a mixture of the
      two, which is the blend actually running. Refuse the geometry again and this second is pure
      `B`, because the plan is then a hard cut at 3.0 s;
    * at 6.0 s it is `C`'s, so `B` really does end inside the blend and nothing of it was written
      afterwards.

    `job.inputs` names both legs' takes, and `ExportLook.transitions` records the blend rather
    than a refusal -- the record and the picture agreeing is the whole of FX-25.
    """
    client, _store, comfy, _app = make_client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "Used up"}).json()["id"]
    assert client.post(
        f"/api/projects/{project_id}/songs/upload",
        data={"title": "Used Up Song", "duration": "0"},
        files={"file": ("song.wav", wav_bytes(8.0), "audio/wav")},
    ).status_code == 200

    shots_dir = tmp_path / "comfy" / "output" / "music-video-producer" / project_id / "shots"
    prefix = f"music-video-producer/{project_id}/shots"
    windows = (("shot_a", 0.0, 4.0, "red"), ("shot_b", 3.0, 3.0, "green"),
               ("shot_c", 4.0, 4.0, "blue"))
    for shot_id, _start, duration, colour in windows:
        synthesize_take(
            shots_dir / f"{shot_id}-h3_00001-audio.mp4", duration + 0.458, colour=colour
        )
    assert client.put(
        f"/api/projects/{project_id}/shots",
        json={
            "shots": [
                {
                    "id": shot_id, "start": start, "duration": duration,
                    "prompt": f"Room {shot_id}", "status": "complete",
                    "latest_output": f"{prefix}/{shot_id}-h3_00001-audio.mp4",
                }
                for shot_id, start, duration, _colour in windows
            ]
        },
    ).status_code == 200
    for shot_id, *_rest in windows:
        assert client.post(
            f"/api/projects/{project_id}/shots/{shot_id}/approve"
        ).status_code == 200
    assert set_transition(client, project_id, "shot_a", "dissolve").status_code == 200

    response = client.post(f"/api/projects/{project_id}/assemble")
    assert response.status_code == 200, response.text
    body = response.json()
    export = tmp_path / "projects" / project_id / "media" / body["export"]

    # The plan: three entries, not four. The fourth is `shot_b`'s own head after the blend, which
    # is zero frames long and is dropped -- and the drop is sum-neutral by construction.
    assert body["clip_count"] == 3
    assert body["total_frames"] == 192
    assert body["job"]["look"]["transitions"] == ["shot_a=dissolve"]
    assert body["job"]["inputs"].count(
        f"shot_b=music-video-producer/{project_id}/shots/shot_b-h3_00001-audio.mp4"
    ) == 1

    # The artefact. `ffprobe` reports both streams and the duration the song has -- an
    # intermediate written at `-frames:v 0` carries no video stream at all and `concat` accepts
    # it, so this is the check that says the file is a video rather than that ffmpeg was happy.
    assert probe(export, "stream=codec_type").splitlines() == ["video", "audio"]
    assert abs(float(probe(export, "format=duration")) - 8.0) <= 1 / 24

    opening = first_pixel(export, 1.0)
    assert opening[0] > 150 and opening[1] < 90, opening
    # The middle of the Overlap: both pictures are on screen at once, which is the blend. A plan
    # that refused this geometry cuts hard at 3.0 s and answers pure green here.
    blended = first_pixel(export, 3.5)
    assert blended[0] > 60 and blended[1] > 60, ("not a mixture of the two legs", blended)
    assert blended[0] < 200 and blended[1] < 200, ("one leg alone, not a blend", blended)
    # And after it, `C` -- `shot_b` contributes nothing outside the blend, which is what "used up"
    # means and what the dropped entry was.
    tail = first_pixel(export, 6.0)
    assert tail[2] > 150 and tail[1] < 90, tail
    assert comfy.prompts == []


def test_a_shot_with_no_transition_exports_exactly_what_it_exported_before(
    tmp_path: Path, monkeypatch
):
    """Constraint 5, asserted on the argv and the composed chain and **never on the mp4**.

    R-20: multi-threaded libx264 is not bit-exact on high-entropy input, so a determinism claim
    belongs on the filter graph and the command line rather than on the encoded file. What is
    claimed here is that a project with no transition anywhere runs the same two `trim_args` it
    always ran, builds no segment, joins with the same `concat_args`, and records an empty
    `transitions` slot — the state every export in this application's history was in.
    """
    import music_video_producer.app as app_module
    from music_video_producer.assembly import concat_args, trim_args

    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    assert all(shot.transition_out is None for shot in store.get(project_id).shots)

    segments: list[list[str]] = []
    joins: list[tuple] = []
    real_segment = app_module.transition_segment_args
    real_concat = app_module.concat_args

    def record_segment(*args, **kwargs):
        segments.append(real_segment(*args, **kwargs))
        return segments[-1]

    def record_concat(list_file, song, dest, overlays=None, **kwargs):
        joins.append((list_file, song, dest, list(overlays or []), kwargs))
        return real_concat(list_file, song, dest, overlays, **kwargs)

    monkeypatch.setattr(app_module, "transition_segment_args", record_segment)
    monkeypatch.setattr(app_module, "concat_args", record_concat)
    commands, response = recorded_trims(client, monkeypatch, project_id)

    assert response.status_code == 200, response.text
    assert segments == [], "a project with no transition must build no segment"
    assert len(commands) == 2
    for command in commands:
        source = Path(command[command.index("-i") + 1])
        assert command == trim_args(source, Path(command[-1]), 96, 192, 108)
    # The join is the song-only argv this route has always built: no overlays, default preset,
    # and the argv rebuilt from the very arguments the route passed rather than from a
    # reconstruction of them.
    assert len(joins) == 1
    list_file, song, dest, overlays, extra = joins[0]
    assert overlays == []
    assert concat_args(list_file, song, dest, overlays, **extra) == concat_args(
        list_file, song, dest
    )
    assert response.json()["job"]["look"]["transitions"] == []
    assert comfy.prompts == []


def test_accepted_take_audio_under_a_transition_is_the_mix_it_always_was(
    tmp_path: Path, monkeypatch
):
    """**Constraint 6, stated and executed: the mix does not move.**

    `AudioOverlay` stops the outgoing take's audio at the incoming Shot's start, because
    `assembly_plan` truncates the outgoing clip there. The Overlap's seconds are seconds the
    incoming Shot has already begun, so they were its audio before this epic and they stay its
    audio now: **a transition entry contributes the incoming leg's overlay and only it.**

    Had it contributed none, the incoming Shot's accepted audio would have lost exactly the head
    of itself the day a Director set a dissolve — silently, at 200, in a mix nobody was watching.

    What changes is the shape and not the content, and this is the assertion that says which:
    the same source, the same take seconds, at the same timeline positions, in two contiguous
    pieces instead of one. The outgoing Shot's audio does **not** come in under the blend, which
    is the same rule read consistently — it stopped at the incoming Shot's start before, and this
    epic moved no clip's start.
    """
    import music_video_producer.app as app_module
    from music_video_producer.assembly import ASSEMBLY_FPS

    def overlays_for(root: Path, transition: str | None):
        client, store, _comfy, _app = make_client(root)
        project_id, shots_dir = project_with_two_approved_takes(client, store, root)
        overlap_the_two_shots(client, store, root, project_id, overlap=0.5)
        # An acceptance needs something to accept: `ASSEMBLY_NO_AUDIO_TO_MIX_REFUSAL` refuses a
        # take with no audio stream, and the fixture's colour sources carry none.
        synthesize_toned_take(shots_dir / "shot_a-h3_00001-audio.mp4", 4.458)
        synthesize_toned_take(
            shots_dir / "shot_b-h3_00001-audio.mp4", 4.958, size="192x108"
        )
        project = store.get(project_id)
        for shot in project.shots:
            shot.mix_take_audio = True
        store.save(project)
        for shot_id in ("shot_a", "shot_b"):
            client.post(f"/api/projects/{project_id}/shots/{shot_id}/unapprove")
            client.post(f"/api/projects/{project_id}/shots/{shot_id}/approve")
        if transition:
            assert set_transition(client, project_id, "shot_a", transition).status_code == 200
        seen: list[list] = []
        real_concat = app_module.concat_args

        def record(list_file, song, dest, overlays=None, **kwargs):
            seen.append(list(overlays or []))
            return real_concat(list_file, song, dest, overlays, **kwargs)

        monkeypatch.setattr(app_module, "concat_args", record)
        response = client.post(f"/api/projects/{project_id}/assemble")
        assert response.status_code == 200, response.text
        monkeypatch.undo()
        return seen[0]

    def covered(overlays):
        """Which source seconds land at which timeline seconds, as one flat span list."""
        return [
            (
                Path(overlay.source).name,
                round(overlay.offset_seconds, 6),
                round(overlay.offset_seconds + overlay.window_seconds, 6),
                round(overlay.delay_seconds, 6),
            )
            for overlay in overlays
        ]

    without = overlays_for(tmp_path / "plain", None)
    with_blend = overlays_for(tmp_path / "blended", "dissolve")

    # Two clips become three, so the second Shot's one overlay becomes two.
    assert len(without) == 2 and len(with_blend) == 3

    # The outgoing Shot's contribution is untouched — same file, same take seconds, same delay.
    assert covered(without)[0] == covered(with_blend)[0]

    # And the incoming Shot's is the same span in two pieces: the second piece starts where the
    # first ends, on the timeline and in the take, and the pair covers what the single one did.
    first, second = with_blend[1], with_blend[2]
    whole = without[1]
    assert Path(first.source).name == Path(second.source).name == Path(whole.source).name
    assert first.offset_seconds == whole.offset_seconds
    assert first.delay_seconds == whole.delay_seconds
    assert round(first.delay_seconds + first.window_seconds, 9) == round(
        second.delay_seconds, 9
    )
    assert round(first.window_seconds + second.window_seconds, 9) == round(
        whole.window_seconds, 9
    )
    # The take offset of the second piece is where the first stopped, to within the half frame
    # `assembly_plan`'s own `replace(clip, offset=...)` has produced for every nested overlay
    # since the layers ruling — real seconds against grid seconds, and the established convention.
    assert abs(
        (first.offset_seconds + first.window_seconds) - second.offset_seconds
    ) <= 1 / (2 * ASSEMBLY_FPS)


def test_more_than_two_clips_over_one_instant_still_exports_and_says_what_it_refused(
    tmp_path: Path
):
    """R-37 at the route: the **transition** is refused, never the export.

    A third Shot dragged across the Overlap makes three clips cover one instant. There is no pair
    of legs to blend, so the boundary stays the hard cut it already is — and the export runs,
    because refusing it would be stricter than `assembly_plan` itself and would cost a Director a
    render over one geometry.

    The sentence is not lost. `ExportLook.transitions` carries it whole, prefixed `refused:`,
    which is the only place saying a transition the manifest holds did not run: a record listing
    only the successes would make a refused transition indistinguishable from one nobody set.
    """
    from music_video_producer.assembly import TRANSITION_CROWDED_REFUSAL

    client, store, comfy, _app = make_client(tmp_path)
    project_id, shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    # Three shots tiling the song, the middle one overlapping both of its neighbours.
    synthesize_take(shots_dir / "shot_c-h3_00001-audio.mp4", 5.208, colour="green")
    prefix = f"music-video-producer/{project_id}/shots"
    shots = [
        {"id": "shot_a", "start": 0.0, "duration": 3.5, "status": "complete",
         "latest_output": f"{prefix}/shot_a-h3_00001-audio.mp4"},
        {"id": "shot_b", "start": 3.0, "duration": 1.0, "status": "complete",
         "latest_output": f"{prefix}/shot_b-h3_00001-audio.mp4"},
        {"id": "shot_c", "start": 3.25, "duration": 4.75, "status": "complete",
         "latest_output": f"{prefix}/shot_c-h3_00001-audio.mp4"},
    ]
    # Un-approved first: `_require_approval_unchanged` refuses a whole-shot write that moves an
    # approved Shot, which is the guard doing its job rather than a fixture inconvenience.
    for shot_id in ("shot_a", "shot_b"):
        client.post(f"/api/projects/{project_id}/shots/{shot_id}/unapprove")
    assert client.put(
        f"/api/projects/{project_id}/shots", json={"shots": shots}
    ).status_code == 200
    for shot_id in ("shot_a", "shot_b", "shot_c"):
        assert client.post(
            f"/api/projects/{project_id}/shots/{shot_id}/approve"
        ).status_code == 200
    assert set_transition(client, project_id, "shot_a", "dissolve").status_code == 200

    response = client.post(f"/api/projects/{project_id}/assemble")
    assert response.status_code == 200, response.text
    body = response.json()
    recorded = body["job"]["look"]["transitions"]
    assert len(recorded) == 1 and recorded[0].startswith("refused: ")
    assert recorded[0].removeprefix("refused: ") == TRANSITION_CROWDED_REFUSAL.format(
        before=shot_label(store.get(project_id), store.get(project_id).shots[0]),
        after=shot_label(store.get(project_id), store.get(project_id).shots[1]),
        start=3.0,
        end=3.5,
        count=3,
    )
    export = tmp_path / "projects" / project_id / "media" / body["export"]
    assert abs(float(probe(export, "format=duration")) - 8.0) <= 1 / 24
    assert comfy.prompts == []


def test_a_stored_transition_the_catalogue_does_not_know_refuses_the_export_by_name(
    tmp_path: Path
):
    """AD-21 applied to the other catalogue: nothing stored says a transition is valid.

    The route validated at the time; a manifest is hand-editable and the catalogue is not stored
    beside it, so the export asks again. **This one does refuse the export**, unlike R-37's
    geometry refusal, and the difference is that there is no picture to render from an unknown
    type at all — `transition_definition` is the only thing that turns a type into an `xfade`
    name. That is `_effect_stack_refusals`' rule, one catalogue over.
    """
    from music_video_producer.app import ASSEMBLY_TRANSITION_REFUSAL
    from music_video_producer.effects import TRANSITION_CATALOGUE, TRANSITION_UNKNOWN_REFUSAL

    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    overlap_the_two_shots(client, store, tmp_path, project_id, overlap=0.5)
    project = store.get(project_id)
    project.shots[0].transition_out = TransitionSpec(type="crossfade")
    store.save(project)

    response = client.post(f"/api/projects/{project_id}/assemble")
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == ASSEMBLY_TRANSITION_REFUSAL.format(
        shot=shot_label(project, project.shots[0]),
        detail=TRANSITION_UNKNOWN_REFUSAL.format(
            transition="crossfade", known=", ".join(sorted(TRANSITION_CATALOGUE))
        ),
    )
    assert store.get(project_id).jobs == []
    assert comfy.prompts == []


# ------------------------------------------------------------------------------------------
# One-sided transitions, through real ffmpeg (story 11.4). **This is again the half a pinned argv
# cannot do**, and for a nastier reason than the paired case: a treatment that composed cleanly
# and did nothing is rc 0, the right frame count, the right pixel format and a byte-identical
# picture. Identical checksums are the *default* outcome of getting this wrong, so every test
# below compares against a control render of the same chain with only the treatment removed, and
# does it on the **filter graph's own frames** rather than on an encoded file -- R-20, and
# measured again here: comparing the encoded intermediates instead reports 21 frames moved where
# 11 did, because libx264's lookahead spreads a change backwards.
# ------------------------------------------------------------------------------------------


def graph_frames(command: list[str], cwd: Path | None = None) -> list[str]:
    """One md5 per frame of what a recorded export argv's **filter graph** produced.

    The argv is re-run with its encoder and output replaced by `framemd5`, so what comes back is
    the chain's frames rather than a file libx264 wrote -- which is the only surface a
    "bit-identical outside the treatment" claim may be made on (R-20, `docs/BUILD-HANDOFF.md`).
    Everything before `-c:v` is kept exactly as the export ran it, `-frames:v` included.
    """
    kept = command[: command.index("-c:v")]
    output = subprocess.run(
        [*kept, "-f", "framemd5", "-"],
        check=True, capture_output=True, text=True,
        cwd=None if cwd is None else cwd.as_posix(),
    ).stdout
    return [
        line.split(",")[-1].strip()
        for line in output.splitlines()
        if line and not line.startswith("#")
    ]


def without_the_treatment(command: list[str], *stages: str) -> list[str]:
    """The same argv with named stages cut out of its `-vf` chain, and nothing else touched.

    The control for every claim below. Built by *removing* from the argv the export actually ran
    rather than by composing a second chain, because a control composed separately would agree
    with an export whose chain had grown a stage nobody asked for -- which is exactly what these
    tests are looking for.
    """
    spot = command.index("-vf") + 1
    remaining = [stage for stage in command[spot].split(",") if stage not in stages]
    assert len(remaining) == len(command[spot].split(",")) - len(stages), (
        f"a stage named for removal was not in the chain: {command[spot]}"
    )
    return [*command[:spot], ",".join(remaining), *command[spot + 1:]]


def test_a_one_sided_transition_treats_its_own_final_frames_and_changes_no_count(
    tmp_path: Path, monkeypatch
):
    """**Story 11.4's whole acceptance, on the written file and on the graph's frames.**

    Two Shots that do not overlap, a transition out on the first: its own last frames are treated
    and then the cut happens. The claims, in the order they can go wrong:

    * **the plan is untouched.** Two entries, not three, and the frame total is the song's. A
      one-sided transition consumes no timeline length and borrows nothing from its neighbour
      (FX-18, FX-NFR-1), which here means `assembly_plan` never hears about it at all;
    * **the clip's own count is unchanged.** 96 frames asked for, 96 decoded off the intermediate
      the export actually wrote -- not the argv's cap, which caps from above only;
    * **it is still concat-identical**, so the join stays `-c:v copy`: same codec, profile, pixel
      format, geometry, rate and SAR as an ordinary `trim_args` intermediate built from the same
      take, compared against one rather than against values written down here;
    * **and it changed the picture.** The last twelve frames differ from a control render of the
      identical chain with only the `fade` stage removed, and **every frame before them is
      bit-identical** -- which is the half that catches a treatment that ran over the whole clip
      instead of its tail.

    The neighbour is the control for the other direction: shot_b carries no transition and its
    argv is the argv this route has always built, character for character.
    """
    from music_video_producer.assembly import trim_args
    from music_video_producer.effects import ONE_SIDED_TRANSITION_FRAMES

    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    assert set_transition(client, project_id, "shot_a", "fade_black").status_code == 200

    survivors = kept_intermediates(monkeypatch, tmp_path)
    commands, response = recorded_trims(client, monkeypatch, project_id)
    assert response.status_code == 200, response.text
    body = response.json()

    # The plan is what it was: two clips, the song's frames, no segment anywhere.
    assert body["clip_count"] == 2
    assert body["total_frames"] == 192
    assert len(commands) == 2 and len(survivors) == 2

    treated, plain = commands
    fade = f"fade=t=out:start_frame={96 - ONE_SIDED_TRANSITION_FRAMES}:nb_frames=6:color=black"
    assert fade in treated[treated.index("-vf") + 1].split(",")
    # The Shot with no transition is untouched, which is R-20's guarantee still holding.
    assert plain == trim_args(
        Path(plain[plain.index("-i") + 1]), Path(plain[-1]), 96, 192, 108
    )

    # The count on the file, decoded.
    assert rendered_frames(survivors[0]) == 96
    shape = "stream=codec_name,profile,pix_fmt,width,height,r_frame_rate,sample_aspect_ratio"
    neighbour = tmp_path / "neighbour.mp4"
    subprocess.run(
        trim_args(Path(treated[treated.index("-i") + 1]), neighbour, 96, 192, 108),
        check=True, capture_output=True,
    )
    assert probe(survivors[0], shape) == probe(neighbour, shape)

    # The picture, against the same chain with only the treatment taken out.
    control = graph_frames(without_the_treatment(treated, fade))
    treated_frames = graph_frames(treated)
    assert len(control) == len(treated_frames) == 96
    moved = [index for index in range(96) if control[index] != treated_frames[index]]
    assert moved, "the transition composed and changed nothing, which is rc 0 and wrong"
    assert min(moved) >= 96 - ONE_SIDED_TRANSITION_FRAMES
    assert control[: 96 - ONE_SIDED_TRANSITION_FRAMES] == (
        treated_frames[: 96 - ONE_SIDED_TRANSITION_FRAMES]
    )

    # And the export says what it did, with the length it ran for.
    assert body["job"]["look"]["transitions"] == [
        f"shot_a=fade_black one-sided over {ONE_SIDED_TRANSITION_FRAMES} frames"
    ]
    export = tmp_path / "projects" / project_id / "media" / body["export"]
    assert abs(float(probe(export, "format=duration")) - 8.0) <= 1 / 24
    assert comfy.prompts == []


def test_a_one_sided_blur_ramp_addresses_a_label_in_its_own_chain_and_leaves_the_rest_alone(
    tmp_path: Path, monkeypatch
):
    """**Epic 10's discipline, inherited whole** (R-25, `DriveScript.target`).

    A one-sided blur wipe is the one form that is driven rather than composed: `gblur` has no
    ramp of its own, so its `sigma` is moved by a compiled `sendcmd` script. Every target must
    appear as an `@label` in the chain composed by the same call, because a command aimed at a
    target that is not in the graph is **discarded in silence at rc 0**.

    Reproduced while writing this, on this machine's ffmpeg 7.0: addressing the bare instance name
    `xo` instead of `gblur@xo` gives *"Command reply for command #0: ret:Function not
    implemented"* at `-v verbose`, nothing at all at `-v error`, rc 0, the right frame count, and
    a picture byte-identical to the undriven chain. `avfilter_graph_send_command` matches a target
    against the filter's own name and answers `ENOSYS` when nothing matched.

    So three things are asserted and none of them is the exit code: the target string appears
    verbatim in the composed chain; the file that chain names by a bare relative name is sitting
    in the directory ffmpeg was standing in; and the frames outside the ramp are bit-identical to
    a control while the frames inside it are not.
    """
    from music_video_producer.effects import (
        ONE_SIDED_TRANSITION_FRAMES,
        ONE_SIDED_TRANSITION_LABEL,
        one_sided_transition_stages,
    )

    client, store, comfy, _app = make_client(tmp_path)
    project_id, shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    # Detail, because a blur of a flat colour field is that flat colour field. Replacing the take
    # in place moves no window, so the approval snapshot still describes it and nothing is stale.
    synthesize_detailed_take(shots_dir / "shot_a-h3_00001-audio.mp4", 4.458)
    assert set_transition(client, project_id, "shot_a", "blur_wipe").status_code == 200

    survivors = kept_intermediates(monkeypatch, tmp_path)
    commands, response = recorded_trims(client, monkeypatch, project_id)
    assert response.status_code == 200, response.text
    assert len(commands) == 2 and len(survivors) == 2

    treated = commands[0]
    chain = treated[treated.index("-vf") + 1]
    composed = one_sided_transition_stages("blur_wipe", clip_frames=96, fps=24)
    script = composed.scripts[0]

    # The target, in the chain produced by the same call. Nothing else can catch a typo in it.
    assert script.target == f"gblur@{ONE_SIDED_TRANSITION_LABEL}"
    # The init string is read off the same call rather than rebuilt here: it carries the
    # `sigmaV=0` pin R-46 made load-bearing, and a test that spells it out by hand would go
    # stale the next time the composer changes what it declares -- which is what happened.
    declared = composed.treatment[0]
    assert declared.startswith(f"{script.target}=sigma=0")
    assert declared in chain.split(",")
    assert f"sendcmd=f={script.filename}" in chain.split(",")
    # A bare relative name, and the file of that name was in the directory ffmpeg stood in.
    assert "/" not in script.filename and ":" not in script.filename
    kept_script = tmp_path / script.filename
    assert kept_script.is_file()
    assert kept_script.read_text(encoding="utf-8") == script.text

    assert rendered_frames(survivors[0]) == 96
    control = graph_frames(
        without_the_treatment(
            treated, f"sendcmd=f={script.filename}", declared
        ),
        cwd=tmp_path,
    )
    treated_frames = graph_frames(treated, cwd=tmp_path)
    moved = [index for index in range(96) if control[index] != treated_frames[index]]
    assert moved, "the ramp was discarded, which is exactly what rc 0 will not tell you"
    assert min(moved) >= 96 - ONE_SIDED_TRANSITION_FRAMES
    assert control[: 96 - ONE_SIDED_TRANSITION_FRAMES] == (
        treated_frames[: 96 - ONE_SIDED_TRANSITION_FRAMES]
    )
    # The ramp's own first frame is the identity, so it grows from nothing rather than snapping
    # on: `sigma=0` is a measured no-op and the first command sets exactly that.
    assert control[96 - ONE_SIDED_TRANSITION_FRAMES] == (
        treated_frames[96 - ONE_SIDED_TRANSITION_FRAMES]
    )
    assert comfy.prompts == []


def test_a_pair_only_type_left_one_sided_is_refused_with_its_reason_and_nothing_substituted(
    tmp_path: Path, monkeypatch
):
    """FX-18 and FX-19 at the export: **a type with no one-sided form does not degrade.**

    Reachable without a hand-edited manifest, which is why it is refused here rather than only at
    the write. A Director authors "Wipe left" across an Overlap -- the write route agrees, because
    there are two pictures to move across each other -- and then drags the clips apart. FX-16 and
    R-36 keep the stored type, so the boundary now holds a wipe with nothing to wipe onto.

    The export runs (R-37's rule: a perfectly good hard cut is already there, and refusing costs a
    Director a render over one geometry), the argv is the argv a Shot with no transition gets, and
    the sentence is the one the write route already says -- `TRANSITION_PAIR_ONLY_REFUSAL`, reused
    rather than reworded, because two wordings for one condition is how a Director learns that the
    application has two opinions.
    """
    from music_video_producer.app import TRANSITION_REFUSED_RECORD
    from music_video_producer.assembly import trim_args
    from music_video_producer.effects import (
        TRANSITION_CATALOGUE,
        TRANSITION_PAIR_ONLY_REFUSAL,
    )

    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    overlap_the_two_shots(client, store, tmp_path, project_id, overlap=0.5)
    assert set_transition(client, project_id, "shot_a", "wipe_left").status_code == 200
    # And now the Overlap goes away, exactly as dragging the clip would take it away. Read back
    # *after* the un-approvals, because a whole-shots write carries the manifest's version and one
    # dumped before them is a 409 rather than a fixture that happens to work.
    for shot_id in ("shot_a", "shot_b"):
        client.post(f"/api/projects/{project_id}/shots/{shot_id}/unapprove")
    shots = [shot.model_dump(mode="json") for shot in store.get(project_id).shots]
    shots[1]["start"] = 4.0
    shots[1]["duration"] = 4.0
    assert client.put(
        f"/api/projects/{project_id}/shots", json={"shots": shots}
    ).status_code == 200
    for shot_id in ("shot_a", "shot_b"):
        assert client.post(
            f"/api/projects/{project_id}/shots/{shot_id}/approve"
        ).status_code == 200
    assert store.get(project_id).shots[0].transition_out.type == "wipe_left"

    commands, response = recorded_trims(client, monkeypatch, project_id)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["clip_count"] == 2 and body["total_frames"] == 192
    # Nothing was substituted: both argvs are the ones a project with no transition builds.
    for command in commands:
        assert command == trim_args(
            Path(command[command.index("-i") + 1]), Path(command[-1]), 96, 192, 108
        )
    stored = store.get(project_id)
    assert body["job"]["look"]["transitions"] == [
        TRANSITION_REFUSED_RECORD.format(
            shot=TRANSITION_PAIR_ONLY_REFUSAL.format(
                label="Wipe left",
                shot=shot_label(stored, stored.shots[0]),
                # The export composes from `transition_out` alone (AD-30).
                neighbour="after",
                alternatives=", ".join(
                    sorted(
                        entry.label
                        for entry in TRANSITION_CATALOGUE.values()
                        if not entry.pair_only
                    )
                ),
            )
        )
    ]
    assert comfy.prompts == []


def set_transition_in(client, project_id: str, shot_id: str, type_: str | None):
    """`set_transition`'s mirror: the incoming half of the pair, which AD-30 usually writes."""
    return client.put(
        f"/api/projects/{project_id}/shots/{shot_id}/transitions",
        json={"transition_in": None if type_ is None else {"type": type_}},
    )


def test_the_first_shot_opens_with_its_own_frames_treated_and_changes_no_count(
    tmp_path: Path, monkeypatch
):
    """**R-45's whole acceptance, on the written file and on the graph's frames** (story 11.f8).

    FX-18 says a one-sided transition treats a Shot's own final *or opening* frames, and only the
    final ones shipped. The first Shot of the plan carries a `transition_in`: its **opening**
    frames are treated and the video begins. The claims, in the order they can go wrong:

    * **the plan is untouched.** Two entries, not three, and the frame total is the song's. An
      opening treatment consumes no timeline length and borrows nothing (FX-18, FX-NFR-1), which
      here means `assembly_plan` never hears about it at all;
    * **the clip's own count is unchanged.** 96 frames asked for, 96 decoded off the intermediate
      the export actually wrote;
    * **and it changed the picture, at the right end.** The **first** frames differ from a control
      render of the identical chain with only the `fade` stage removed, and **every frame after
      them is bit-identical** -- which is the half that catches a treatment composed at the tail,
      or one that ran over the whole clip, or one that composed and changed no pixel at all. That
      last is this pipeline's signature failure: ffmpeg's exit code is evidence of nothing here.

    The neighbour is the control for the other direction: `shot_b` carries no transition of its
    own and its argv is the argv this route has always built, character for character. It is also
    the control for R-45 itself -- nothing composes for a Shot that is not the first.
    """
    from music_video_producer.assembly import trim_args
    from music_video_producer.effects import ONE_SIDED_TRANSITION_FRAMES

    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    assert set_transition_in(client, project_id, "shot_a", "fade_black").status_code == 200
    # Nothing was mirrored onto anything: there is no Shot before the first one to mirror to,
    # which is the whole reason this boundary has no outgoing field to be authoritative with.
    stored = store.get(project_id)
    assert stored.shots[0].transition_out is None
    assert stored.shots[1].transition_in is None

    survivors = kept_intermediates(monkeypatch, tmp_path)
    commands, response = recorded_trims(client, monkeypatch, project_id)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["clip_count"] == 2
    assert body["total_frames"] == 192
    assert len(commands) == 2 and len(survivors) == 2

    treated, plain = commands
    ramp = ONE_SIDED_TRANSITION_FRAMES // 2
    fade = f"fade=t=in:start_frame={ramp}:nb_frames={ramp}:color=black"
    assert fade in treated[treated.index("-vf") + 1].split(",")
    assert plain == trim_args(
        Path(plain[plain.index("-i") + 1]), Path(plain[-1]), 96, 192, 108
    )

    assert rendered_frames(survivors[0]) == 96

    control = graph_frames(without_the_treatment(treated, fade))
    treated_frames = graph_frames(treated)
    assert len(control) == len(treated_frames) == 96
    moved = [index for index in range(96) if control[index] != treated_frames[index]]
    assert moved, "the transition composed and changed nothing, which is rc 0 and wrong"
    # The **opening** frames and no others: the treatment is over by
    # `ONE_SIDED_TRANSITION_FRAMES`, and everything from there to the cut is the untreated take.
    assert max(moved) < ONE_SIDED_TRANSITION_FRAMES
    assert 0 in moved, "the video's first frame is the black it fades up from"
    assert control[ONE_SIDED_TRANSITION_FRAMES:] == treated_frames[ONE_SIDED_TRANSITION_FRAMES:]

    assert body["job"]["look"]["transitions"] == [
        f"shot_a=fade_black opening over {ONE_SIDED_TRANSITION_FRAMES} frames"
    ]
    export = tmp_path / "projects" / project_id / "media" / body["export"]
    assert abs(float(probe(export, "format=duration")) - 8.0) <= 1 / 24
    assert comfy.prompts == []


def test_an_opening_blur_settle_addresses_its_own_label_and_leaves_the_rest_alone(
    tmp_path: Path, monkeypatch
):
    """The driven opening form, with Epic 10's discipline inherited whole (R-25).

    `blur_settle` is the one opening form that is ramped by a compiled `sendcmd` rather than by a
    filter that knows how to ramp itself, so every target must appear as an `@label` in the chain
    composed by the same call -- a command aimed at a target that is not in the graph is discarded
    in silence at rc 0.

    **Its own label, and that is what this test is really about.** The Shot that opens the plan
    carries a `transition_in` *and* a `transition_out` with no Overlap under it, so both a head
    ramp and a tail ramp are spliced into one chain. Under one instance name the two `gblur`
    filters would share a `sendcmd` target and each script would drive both, which is rc 0 with a
    picture nobody authored. So: two labels, two scripts, both files on disk beside the render,
    and the frames in the **middle** of the clip bit-identical to a control with neither ramp in
    it -- which is the only assertion that can tell two ramps from one ramp applied twice.
    """
    from music_video_producer.effects import (
        ONE_SIDED_TRANSITION_FRAMES,
        ONE_SIDED_TRANSITION_LABEL,
        OPENING_TRANSITION_LABEL,
        one_sided_transition_stages,
        opening_transition_stages,
    )

    client, store, comfy, _app = make_client(tmp_path)
    project_id, shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    # Detail, because a blur of a flat colour field is that flat colour field.
    synthesize_detailed_take(shots_dir / "shot_a-h3_00001-audio.mp4", 4.458)
    assert set_transition_in(client, project_id, "shot_a", "blur_wipe").status_code == 200
    assert set_transition(client, project_id, "shot_a", "blur_wipe").status_code == 200

    survivors = kept_intermediates(monkeypatch, tmp_path)
    commands, response = recorded_trims(client, monkeypatch, project_id)
    assert response.status_code == 200, response.text
    treated = commands[0]
    chain = treated[treated.index("-vf") + 1].split(",")

    head = opening_transition_stages("blur_wipe", clip_frames=96, fps=24)
    tail = one_sided_transition_stages("blur_wipe", clip_frames=96, fps=24)
    assert head.scripts[0].target == f"gblur@{OPENING_TRANSITION_LABEL}"
    assert tail.scripts[0].target == f"gblur@{ONE_SIDED_TRANSITION_LABEL}"
    assert head.scripts[0].target != tail.scripts[0].target
    for composed in (head, tail):
        script = composed.scripts[0]
        assert f"sendcmd=f={script.filename}" in chain
        assert composed.treatment[0] in chain
        kept = tmp_path / script.filename
        assert kept.is_file()
        assert kept.read_text(encoding="utf-8") == script.text

    assert rendered_frames(survivors[0]) == 96
    control = graph_frames(
        without_the_treatment(
            treated,
            f"sendcmd=f={head.scripts[0].filename}",
            f"sendcmd=f={tail.scripts[0].filename}",
            head.treatment[0],
            tail.treatment[0],
        ),
        cwd=tmp_path,
    )
    treated_frames = graph_frames(treated, cwd=tmp_path)
    moved = [index for index in range(96) if control[index] != treated_frames[index]]
    assert moved, "both ramps were discarded, which is exactly what rc 0 will not tell you"
    # The head ramp moved the first frames and the tail ramp moved the last, and the stretch
    # between them is the untreated picture -- one ramp driving both filters would move it.
    assert 0 in moved and 95 in moved
    assert control[ONE_SIDED_TRANSITION_FRAMES:96 - ONE_SIDED_TRANSITION_FRAMES] == (
        treated_frames[ONE_SIDED_TRANSITION_FRAMES:96 - ONE_SIDED_TRANSITION_FRAMES]
    )
    # The head ramp's own **last** frame is `sigma=0`, the measured no-op, so it settles into the
    # picture rather than snapping out of the blur. `blur_ramp`'s first frame, mirrored.
    assert control[ONE_SIDED_TRANSITION_FRAMES - 1] == (
        treated_frames[ONE_SIDED_TRANSITION_FRAMES - 1]
    )
    assert comfy.prompts == []


def test_a_stored_transition_in_composes_nothing_on_any_shot_but_the_one_that_opens(
    tmp_path: Path, monkeypatch
):
    """R-45's second acceptance: **everywhere else a stored `transition_in` composes nothing.**

    `shot_b` holds the type. It is the ordinary state of nearly every project that carries a
    transition at all, because AD-30's mirror writes it whenever a Director sets `transition_out`
    on the Shot in front -- so a composer that read this field at every boundary would fade `shot_a`
    out and `shot_b` in from **one** gesture, which is the picture `Fade through black` is already
    called and the substitution FX-18 exists to forbid.

    Asserted on the argv rather than on the record, because a treatment that composes into a chain
    is a defect whether or not anything writes it down: `shot_b`'s argv is the one a project with
    no transitions builds, character for character.
    """
    from music_video_producer.assembly import trim_args

    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    # Through the shipped route and through the shipped gesture: a `transition_out` on `shot_a`
    # is what puts the mirror on `shot_b`, which is the state this test is about.
    assert set_transition(client, project_id, "shot_a", "dissolve").status_code == 200
    assert store.get(project_id).shots[1].transition_in.type == "dissolve"

    commands, response = recorded_trims(client, monkeypatch, project_id)
    assert response.status_code == 200, response.text
    body = response.json()

    plain = commands[1]
    assert plain == trim_args(
        Path(plain[plain.index("-i") + 1]), Path(plain[-1]), 96, 192, 108
    )
    # One treatment for that boundary, and it is the outgoing Shot's tail.
    assert body["job"]["look"]["transitions"] == ["shot_a=dissolve one-sided over 12 frames"]
    assert comfy.prompts == []


def test_the_first_shot_can_open_and_still_blend_into_the_overlap_after_it(
    tmp_path: Path, monkeypatch
):
    """R-45's fourth acceptance: **a first Shot with both an opening and a following Overlap.**

    They are two boundaries and two entries of the plan. The opening rides the outgoing Shot's own
    frames *before* the Overlap -- which `_paired_transitions` guarantees exist, because its rule
    refuses a blend whose outgoing stretch is empty -- and the blend is a `TransitionClip` of its
    own with its own two legs. So the head is treated, the blend is unaffected, and the plan is
    the three entries a blended boundary already produces.
    """
    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    overlap_the_two_shots(client, store, tmp_path, project_id, overlap=0.5)
    assert set_transition(client, project_id, "shot_a", "dissolve").status_code == 200
    assert set_transition_in(client, project_id, "shot_a", "fade_white").status_code == 200

    commands, response = recorded_trims(client, monkeypatch, project_id)
    assert response.status_code == 200, response.text
    body = response.json()

    # Three entries: the head, the blend, the remainder. The blend still composed.
    assert body["clip_count"] == 3
    assert body["total_frames"] == 192
    assert "shot_a=dissolve" in body["job"]["look"]["transitions"]
    assert body["job"]["look"]["transitions"] == [
        "shot_a=dissolve", "shot_a=fade_white opening over 12 frames"
    ]
    # And the treatment is on the **head** entry, which is the clip that lays the first frame.
    head = commands[0]
    assert "fade=t=in:start_frame=6:nb_frames=6:color=white" in (
        head[head.index("-vf") + 1].split(",")
    )
    assert comfy.prompts == []


def test_a_pair_only_type_on_the_opening_is_refused_by_name_with_nothing_substituted(
    tmp_path: Path, monkeypatch
):
    """R-45's third acceptance, and FX-19 in the direction nothing had asked it in.

    **Reachable without a hand-edited manifest**, which is why the fixture takes the long way
    round: a Director authors a "Wipe left" across an Overlap on `shot_b`'s incoming half -- the
    write route agrees, because there are two pictures to move across each other -- and then drags
    `shot_b` in front of `shot_a`. `shot_b` is now the Shot that opens the plan and holds a
    pair-only type on the one boundary that can never have a second picture.

    Nothing is substituted: the argv is the argv a Shot with no transition gets. And the sentence
    is **not** the one the tail's refusal says, because that one names a remedy this boundary does
    not have -- *"drag the two clips across each other"*, when nothing can be put in front of the
    first Shot. It is `TRANSITION_PAIR_ONLY_OPENING_REFUSAL`, the same sentence the write route
    says at the moment a Director picks one there.
    """
    from music_video_producer.app import TRANSITION_REFUSED_RECORD
    from music_video_producer.assembly import trim_args
    from music_video_producer.effects import (
        TRANSITION_CATALOGUE,
        TRANSITION_PAIR_ONLY_OPENING_REFUSAL,
    )

    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    overlap_the_two_shots(client, store, tmp_path, project_id, overlap=0.5)
    assert set_transition_in(client, project_id, "shot_b", "wipe_left").status_code == 200

    # And now `shot_b` moves in front of `shot_a`, exactly as dragging the clips would move it.
    for shot_id in ("shot_a", "shot_b"):
        client.post(f"/api/projects/{project_id}/shots/{shot_id}/unapprove")
    shots = [shot.model_dump(mode="json") for shot in store.get(project_id).shots]
    shots[0]["start"], shots[0]["duration"] = 4.0, 4.0
    shots[1]["start"], shots[1]["duration"] = 0.0, 4.0
    assert client.put(
        f"/api/projects/{project_id}/shots", json={"shots": shots}
    ).status_code == 200
    for shot_id in ("shot_a", "shot_b"):
        assert client.post(
            f"/api/projects/{project_id}/shots/{shot_id}/approve"
        ).status_code == 200
    # The mirror AD-30 wrote on `shot_a` is cleared, so the only thing left in the manifest is the
    # pair-only type on the opening -- which is what this test is about and nothing else.
    assert set_transition(client, project_id, "shot_a", None).status_code == 200
    stored = store.get(project_id)
    assert stored.shots[1].transition_in.type == "wipe_left"
    assert stored.shots[0].transition_out is None

    commands, response = recorded_trims(client, monkeypatch, project_id)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["clip_count"] == 2 and body["total_frames"] == 192
    for command in commands:
        assert command == trim_args(
            Path(command[command.index("-i") + 1]), Path(command[-1]), 96, 192, 108
        )
    assert body["job"]["look"]["transitions"] == [
        TRANSITION_REFUSED_RECORD.format(
            shot=TRANSITION_PAIR_ONLY_OPENING_REFUSAL.format(
                label="Wipe left",
                shot=shot_label(stored, stored.shots[1]),
                alternatives=", ".join(
                    sorted(
                        entry.label
                        for entry in TRANSITION_CATALOGUE.values()
                        if not entry.pair_only
                    )
                ),
            )
        )
    ]
    assert comfy.prompts == []


def test_the_shot_that_lays_the_first_frame_is_not_always_the_first_shot_by_start(
    tmp_path: Path, monkeypatch
):
    """**The geometry R-45's two definitions part on**, and neither Shot may be treated in it.

    `shot_b` starts within half a frame of `shot_a`, so `assembly_plan`'s resolution loop drops
    `shot_a`'s head whole -- later on top -- and the plan opens with `shot_b`. Two readings of
    *"the first Shot of the plan in song order"* are available and both are wrong here:

    * **first by `start`** is `shot_a`, whose own opening frames are not in the export at all. Its
      first surviving clip begins where `shot_b` ends, so treating it would treat frames in the
      middle of the video -- at a cut `shot_b`'s outgoing field already owns, which is the
      two-treatments-for-one-boundary this slice exists to make impossible;
    * **the first entry of the plan** is `shot_b`, which has a predecessor. R-45's own clause
      excludes it, and AD-30's mirror means a Director who set anything on `shot_a` has already
      written `shot_b`'s incoming field -- so composing there would make one gesture fade one Shot
      out and the next in.

    So the treatment composes where the two **agree**, and here they do not. Both fields are set
    and the export composes neither, which is asserted on the argv as well as on the record.
    """
    client, store, comfy, _app = make_client(tmp_path)
    project_id, shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    # `shot_a` covers the whole song and `shot_b` sits on top of its head, so the take has to hold
    # the wider window -- widening a window past its take is refused by name, and a fixture that
    # met that refusal would be a fixture whose only fault was hidden.
    synthesize_take(shots_dir / "shot_a-h3_00001-audio.mp4", 8.458, colour="red")
    for shot_id in ("shot_a", "shot_b"):
        client.post(f"/api/projects/{project_id}/shots/{shot_id}/unapprove")
    shots = [shot.model_dump(mode="json") for shot in store.get(project_id).shots]
    # Half a frame is 1/48 s; `shot_b` starts inside that band, so the head is not merely short,
    # it is a boundary written twice and the resolution loop discards it.
    shots[0]["start"], shots[0]["duration"] = 0.0, 8.0
    shots[1]["start"], shots[1]["duration"] = 0.01, 4.0
    assert client.put(
        f"/api/projects/{project_id}/shots", json={"shots": shots}
    ).status_code == 200
    for shot_id in ("shot_a", "shot_b"):
        assert client.post(
            f"/api/projects/{project_id}/shots/{shot_id}/approve"
        ).status_code == 200
    project = store.get(project_id)
    project.shots[0].transition_in = TransitionSpec(type="fade_black")
    project.shots[1].transition_in = TransitionSpec(type="fade_white")
    store.save(project)

    commands, response = recorded_trims(client, monkeypatch, project_id)
    assert response.status_code == 200, response.text
    body = response.json()

    # The plan really is the geometry this test claims: `shot_b` first, then what is left of
    # `shot_a`. If the resolution ever stops dropping that head, this fails here rather than
    # passing for a reason that has gone away.
    assert body["clip_count"] == 2 and body["total_frames"] == 192
    # `shot_b` is what the export writes first, which is the whole claim: the plan opens with the
    # Shot that is **not** first by `start`.
    assert "shot_b" in commands[0][commands[0].index("-i") + 1]
    assert "shot_a" in commands[1][commands[1].index("-i") + 1]
    # Nothing was composed for either Shot: no head treatment, no tail treatment, no drive. The
    # chains are the ones a project carrying no transition at all builds.
    for command in commands:
        chain = command[command.index("-vf") + 1] if "-vf" in command else ""
        assert "fade=t=in" not in chain and "fade=t=out" not in chain, chain
        assert "sendcmd" not in chain, chain
    assert body["job"]["look"]["transitions"] == []
    assert comfy.prompts == []


#: Every arrangement the one-treatment sweep is run over, as `(start, duration)` per Shot.
#:
#: **Chosen for the shapes that put two treatments near one instant**, not for coverage of the
#: timeline: Shots that start together, Shots inside the half-frame band where a head is dropped
#: whole, Shots nested inside their neighbour, Overlaps the split refuses, and windows short enough
#: that a treatment's clamp is the clip rather than the ceiling. A lattice of pleasant four-second
#: Shots would sweep thousands of plans and never reach the question.
OPENING_SWEEP_WINDOWS: tuple[tuple[float, float], ...] = (
    (4.0, 0.5, 0.02),
    (0.0, 0.005, 0.01, 0.02, 0.25, 2.0, 3.5, 4.0),
    (0.5, 4.0),
    (2.0, 4.0, 6.0, 7.5, 8.0),
)


def opening_sweep_plans():
    """Every `(windows, transitions_out, transitions_in)` the two sweeps below are run over.

    **`transitions_in` is built the way the shipped route builds it** (AD-30): a `transition_out`
    on a Shot mirrors onto the *next* Shot in song order, so the incoming field is not a second
    thing a Director sets -- it is what one gesture produces. A sweep that set the two
    independently would be sweeping a manifest no route can write, and would miss the only shape
    that matters here: the mirror sitting on a boundary the outgoing Shot already owns.

    The first Shot in song order also gets a directly written incoming type, which is the one an
    interface can actually put there, and `None` alongside it so the no-opening case is swept too.
    """
    from music_video_producer.effects import TRANSITION_CATALOGUE

    types = (None, "dissolve", "wipe_left")
    assert TRANSITION_CATALOGUE["dissolve"].one_sided_in, "the sweep needs a type with a form"
    assert TRANSITION_CATALOGUE["wipe_left"].pair_only, "and one with none in either direction"

    a_durations, b_starts, b_durations, c_starts = OPENING_SWEEP_WINDOWS
    for a_duration in a_durations:
        for b_start in b_starts:
            for b_duration in b_durations:
                for c_start in c_starts:
                    windows = [
                        ("shot_a", 0.0, a_duration),
                        ("shot_b", b_start, b_duration),
                        ("shot_c", c_start, 4.0),
                    ]
                    order = [item[0] for item in sorted(windows, key=lambda w: w[1])]
                    for first in types:
                        for out_a in types:
                            for out_b in types:
                                for out_c in types:
                                    chosen = dict(
                                        zip(("shot_a", "shot_b", "shot_c"),
                                            (out_a, out_b, out_c))
                                    )
                                    out = {k: v for k, v in chosen.items() if v}
                                    mirrored = {}
                                    for spot, shot_id in enumerate(order[:-1]):
                                        if out.get(shot_id):
                                            mirrored[order[spot + 1]] = out[shot_id]
                                    if first:
                                        mirrored[order[0]] = first
                                    yield windows, out, mirrored


def composed_treatments(windows, out_types, in_types, checks=None):
    """Every treatment one plan composes, keyed by **the boundary it treats**, or `None`.

    `None` where `assembly_plan` will not build a plan at all, which is a geometry with nothing to
    say rather than a result.

    **The key is the boundary and not the record's own word**, and that is the whole of what makes
    this sweep able to fail. A head treatment sits at the seam in front of its Shot: for the Shot
    that opens the plan that seam is the video's first frame and belongs to nobody, and for every
    other Shot it is the cut its predecessor's `transition_out` owns. Keying a head by "opening"
    would put it in a namespace of its own, where it could never collide with the tail that owns
    the same cut -- a sweep that cannot produce the disagreement it is cited as ruling out.

    Refusals and divergences are not treatments and are skipped: they are what the export records
    when it composed **nothing**.
    """
    from music_video_producer.app import (
        EXPORT_COMPOSITION_CHECKS,
        ExportComposition,
        ExportSubject,
    )
    from music_video_producer.assembly import (
        AssemblyGeometryError,
        ClipWindow,
        TransitionChoice,
        assembly_plan,
    )

    clips = [
        ClipWindow(
            shot_id=shot_id, label=shot_id.upper(), start=start, duration=duration,
            approved_output=f"{shot_id}.mp4", approved_start=start,
            approved_duration=duration, source=Path(f"{shot_id}.mp4"),
        )
        for shot_id, start, duration in windows
    ]
    song = max(clip.end for clip in clips)
    choices = {
        shot_id: TransitionChoice(transition_id=stored, xfade=stored)
        for shot_id, stored in out_types.items()
    }
    try:
        plan = assembly_plan(
            clips, song, {clip.shot_id: (640, 384) for clip in clips}, choices
        )
    except AssemblyGeometryError:
        return None
    subject = ExportSubject(
        clips=tuple(clips), song_seconds=song, stacks={}, looks=lambda **_kw: [],
        plan=plan, transitions=out_types, transitions_in=in_types,
    )
    composition = ExportComposition()
    for check in checks or EXPORT_COMPOSITION_CHECKS:
        check(subject, composition)

    order = [clip.shot_id for clip in sorted(clips, key=lambda clip: clip.start)]
    place = {shot_id: spot for spot, shot_id in enumerate(order)}
    owned = []
    for line in composition.look.transitions:
        if line.startswith(("refused: ", "diverged: ")):
            continue
        shot_id, _, rest = line.partition("=")
        spot = place[shot_id]
        if " opening over " in rest:
            # The seam in front of this Shot: the video's own first frame when nothing precedes
            # it, and otherwise the cut its predecessor owns.
            owned.append(
                ("the video opens",) if spot == 0 else ("cut", order[spot - 1], shot_id)
            )
        else:
            # A tail and a blend both treat the seam *after* this Shot, and they are two ways of
            # treating one boundary rather than two boundaries -- which is why they share a key.
            owned.append((
                "cut", shot_id,
                order[spot + 1] if spot + 1 < len(order) else "the end of the song",
            ))
    return owned


def test_no_plan_composes_two_treatments_for_one_boundary(request):
    """**R-45's fifth acceptance, over a sweep rather than an argument.**

    Every worst defect in this epic has been two answers to one question, so this asks the
    question of every plan a lattice of degenerate geometries can build, with the transitions
    assigned the way the shipped route assigns them -- **through AD-30's mirror**, because that is
    what makes "compose both halves" one gesture rather than two fields a Director set.

    A boundary carrying two treatments is the failure. It is reported by name and not counted, and
    the sweep's own size is asserted so that a lattice quietly narrowed to nothing fails here.

    **What this sweep could not vary, stated rather than left to be discovered.** Three Shots and
    one song; two transition types, one with a one-sided form in both directions and one with none
    in either; and no Effect Stack on any Shot. What it *does* vary is the only thing the rule is
    about: which Shot lays the first frame, whether a boundary blends, and which fields the mirror
    writes. `test_the_one_treatment_sweep_can_see_the_design_r_45_rejected` is the check that it
    can fail at all.

    Measured on 2026-08-31: **19,440 plans, 18,009 composed treatments, 0 boundaries treated
    twice.** The same lattice against the rejected composer collides on 5,544 plans.
    """
    collisions = []
    plans = 0
    treated = 0
    for windows, out_types, in_types in opening_sweep_plans():
        owned = composed_treatments(windows, out_types, in_types)
        if owned is None:
            continue
        plans += 1
        treated += len(owned)
        if len(owned) != len(set(owned)):
            collisions.append((windows, out_types, in_types, owned))
    assert not collisions[:8], collisions[:8]
    assert plans > 4000, plans
    assert treated > 4000, treated


def test_the_one_treatment_sweep_can_see_the_design_r_45_rejected(request):
    """The sweep above, run against the composer R-45 turned down, which it must catch.

    **A sweep that has never produced a single disagreement has not been shown to be capable of
    one.** This repository has already put a 5,675-boundary sweep into a spec as measured fact when
    the harness could not produce the disagreement it was cited as ruling out, so the guard above
    is worth exactly as much as this test is.

    The control is R-36's original ruling as code: *"both transitions now treat their own frames --
    A's tail fades, B's head fades"*, composed at every boundary with no Overlap instead of at the
    plan's first frame only. AD-30's mirror writes the incoming field on the neighbour whenever a
    `transition_out` is set, so one Dissolve on a hard cut becomes a fade out **and** a fade in --
    the picture `Fade through black` is named for, which is the substitution FX-18 forbids and the
    distinction R-34 spent a measurement keeping.

    Measured on 2026-08-31: **5,544 of the same 19,440 plans** carry a boundary treated twice
    under it, against 0 under what shipped.
    """
    from music_video_producer.app import (
        EXPORT_COMPOSITION_CHECKS,
        TRANSITION_OPENING_RECORD,
        _boundary_is_overlapped,
    )
    from music_video_producer.assembly import ASSEMBLY_FPS, ClipWindow
    from music_video_producer.effects import opening_transition_stages

    def compose_both_halves(subject, composition):
        """R-36 as it was originally ruled: every unoverlapped boundary treats both sides."""
        plan = subject.plan
        ordered = sorted(subject.clips, key=lambda clip: clip.start)
        for position, clip in enumerate(ordered):
            stored = subject.transitions_in.get(clip.shot_id)
            if stored is None:
                continue
            if position and _boundary_is_overlapped(ordered, position - 1):
                continue
            index = next(
                (
                    spot
                    for spot, entry in enumerate(plan.clips)
                    if isinstance(entry, ClipWindow) and entry.shot_id == clip.shot_id
                ),
                None,
            )
            if index is None:
                continue
            composed = opening_transition_stages(
                stored, clip_frames=plan.frames[index], fps=ASSEMBLY_FPS
            )
            if composed is None:
                continue
            composition.look.transitions.append(
                TRANSITION_OPENING_RECORD.format(
                    shot=clip.shot_id, transition=stored, frames=composed.frames
                )
            )
        return []

    rejected = (*EXPORT_COMPOSITION_CHECKS[:-2], compose_both_halves)
    collisions = 0
    plans = 0
    for windows, out_types, in_types in opening_sweep_plans():
        owned = composed_treatments(windows, out_types, in_types, checks=rejected)
        if owned is None:
            continue
        plans += 1
        if len(owned) != len(set(owned)):
            collisions += 1
    assert plans > 4000, plans
    # Named as a floor rather than an equality: the number is a property of the lattice, and a
    # lattice that grows should not have to be re-counted. What may not change is that it is many.
    assert collisions > 500, collisions


def test_the_window_rule_and_the_plan_agree_about_what_opens(request):
    """`_opening_clip_frames` against `assembly_plan` itself, over the same lattice.

    The export reads `plan.clips[0]`; the Shot preview and the browser have no plan to read and
    use the window rule instead. **A port is only honest while something asks both sides the same
    question and compares the answers**, and the **number** is compared rather than the verdict:
    two engines agreeing on `0` for different reasons is two engines.

    The plan's answer is both halves of R-45 at once -- the first entry, and its Shot being the
    first in song order -- because that is what the composer does with it.

    Measured on 2026-08-31: **19,440 plans, 6,480 of them opening, 0 disagreements.** The middle
    number is the one that matters -- a table where the first Shot always opens would never ask
    the rule to say no, so it is asserted rather than reported.
    """
    from music_video_producer.app import _opening_clip_frames
    from music_video_producer.assembly import (
        AssemblyGeometryError,
        ClipWindow,
        TransitionChoice,
        assembly_plan,
    )

    disagreed = []
    asked = 0
    opened = 0
    for windows, out_types, _in_types in opening_sweep_plans():
        clips = [
            ClipWindow(
                shot_id=shot_id, label=shot_id.upper(), start=start, duration=duration,
                approved_output=f"{shot_id}.mp4", approved_start=start,
                approved_duration=duration, source=Path(f"{shot_id}.mp4"),
            )
            for shot_id, start, duration in windows
        ]
        song = max(clip.end for clip in clips)
        try:
            plan = assembly_plan(
                clips,
                song,
                {clip.shot_id: (640, 384) for clip in clips},
                {
                    shot_id: TransitionChoice(transition_id=stored, xfade=stored)
                    for shot_id, stored in out_types.items()
                },
            )
        except AssemblyGeometryError:
            continue
        ordered = sorted(clips, key=lambda clip: clip.start)
        opening = plan.clips[0]
        planned = (
            plan.frames[0]
            if isinstance(opening, ClipWindow) and opening.shot_id == ordered[0].shot_id
            else 0
        )
        asked += 1
        opened += bool(planned)
        if _opening_clip_frames(ordered) != planned:
            disagreed.append((windows, out_types, _opening_clip_frames(ordered), planned))
    assert not disagreed[:8], disagreed[:8]
    assert asked > 500, asked
    # The lattice reaches both answers, which is what stops this passing on a table where the
    # first Shot always opens and the rule is never asked to say no.
    assert 0 < opened < asked, (opened, asked)


def test_an_opening_treatment_is_refused_when_the_plan_opens_with_a_blend():
    """`_opening_clip_index`' type guard, pinned at the only boundary that state exists at.

    A `TransitionClip` at index 0 is **not reachable through `assemble_project`**:
    `_paired_transitions` refuses any boundary whose outgoing stretch is empty, so the outgoing
    Shot's own frames always sit in front of a blend. The guard is written anyway, for
    `assembly._split_frames`' own reason -- this function is handed a plan by a caller that may one
    day build one differently, and composing an *opening* treatment onto a blend would write a
    `fade=t=in` into a clip that is two Shots at once, driven by a field neither of them owns.

    Asked directly, because no route can construct the state.
    """
    from music_video_producer.app import ExportSubject, _opening_clip_index
    from music_video_producer.assembly import (
        AssemblyPlan,
        ClipWindow,
        TransitionChoice,
        TransitionClip,
    )

    def window(shot_id: str, start: float, duration: float) -> ClipWindow:
        return ClipWindow(
            shot_id=shot_id, label=shot_id.upper(), start=start, duration=duration,
            approved_output=f"{shot_id}.mp4", approved_start=start,
            approved_duration=duration, source=Path(f"{shot_id}.mp4"),
        )

    clips = [window("shot_a", 0.0, 4.0), window("shot_b", 4.0, 4.0)]
    blend = TransitionClip(
        before=window("shot_a", 0.0, 1.0),
        after=window("shot_b", 0.0, 1.0),
        choice=TransitionChoice(transition_id="dissolve", xfade="fade"),
    )
    geometry = {"width": 640, "height": 384, "song_seconds": 8.0}
    subject = lambda entries, frames: ExportSubject(
        clips=tuple(clips), song_seconds=8.0, stacks={}, looks=lambda **_kw: [],
        plan=AssemblyPlan(clips=entries, frames=frames, **geometry),
    )

    # The blend first: nothing may be treated, because the frames that open the video are two
    # Shots blended and neither of them owns that.
    assert _opening_clip_index(subject([blend, clips[1]], [24, 96])) is None
    # The identical plan with a plain window in front answers `0`, which is what makes the
    # assertion above about the guard rather than about the arithmetic.
    assert _opening_clip_index(subject([clips[0], blend, clips[1]], [72, 24, 96])) == 0
    # And an empty plan is `None` rather than an index error.
    assert _opening_clip_index(subject([], [])) is None


def test_an_unknown_type_on_the_opening_shots_incoming_field_refuses_the_export_by_name(
    tmp_path: Path
):
    """AD-21 applied to the field R-45 made the export build a picture from.

    Nothing stored says a transition is valid, and the plan stage asks the catalogue again --
    which it did only of `transition_out` until this slice, because that was the only field an
    export read. `_compose_opening_transition` now builds from the first Shot's `transition_in`,
    so a stored value the catalogue cannot name is the same fault there: there is no `xfade` name
    and no form to compose, and rendering the clip untreated would be an export quietly doing
    something the manifest did not ask for.

    **Asked of the first Shot in song order and of no other**, which is the whole of the blast
    radius: every other Shot's `transition_in` is AD-30's mirror and composes nothing, so a value
    stored there is not something this export builds from and does not refuse it. `shot_b` carries
    the identical unknown type and the export runs.
    """
    from music_video_producer.app import ASSEMBLY_TRANSITION_REFUSAL
    from music_video_producer.effects import (
        TRANSITION_CATALOGUE,
        TRANSITION_UNKNOWN_REFUSAL,
    )

    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    # Past the route, which refuses an unknown type at the write -- the state is reachable from a
    # hand edit or a manifest written by a build whose catalogue held one more entry.
    project = store.get(project_id)
    project.shots[1].transition_in = TransitionSpec.model_construct(type="crossfade")
    store.save(project)
    assert client.post(f"/api/projects/{project_id}/assemble").status_code == 200

    project = store.get(project_id)
    project.shots[0].transition_in = TransitionSpec.model_construct(type="crossfade")
    store.save(project)
    refused = client.post(f"/api/projects/{project_id}/assemble")
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == ASSEMBLY_TRANSITION_REFUSAL.format(
        shot=shot_label(store.get(project_id), store.get(project_id).shots[0]),
        detail=TRANSITION_UNKNOWN_REFUSAL.format(
            transition="crossfade", known=", ".join(sorted(TRANSITION_CATALOGUE))
        ),
    )
    assert comfy.prompts == []


def test_a_pair_that_disagrees_across_an_overlap_is_reported_once_and_never_refuses(
    tmp_path: Path
):
    """**Story 11.3's third criterion and AD-30's second half**, which had no code until now.

    A manifest whose pair disagrees -- hand-edited here, and a partially-applied write in the
    wild -- exports. The outgoing Shot's `transition_out` is what runs, which is the read path
    story 11.1 shipped; this is the half that stops the disagreement being swallowed.

    **Once** is the load-bearing word, so the count is asserted rather than the presence. The walk
    is over consecutive Shots in song order, so one diverging pair is one line however many clips
    either Shot resolves into.
    """
    from music_video_producer.app import TRANSITION_DIVERGED_RECORD

    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    overlap_the_two_shots(client, store, tmp_path, project_id, overlap=0.5)
    assert set_transition(client, project_id, "shot_a", "dissolve").status_code == 200
    # The mirror, edited behind the route's back -- which is the only way to reach this state,
    # and precisely why AD-30 says an editable manifest must not make an export undecidable.
    project = store.get(project_id)
    assert project.shots[1].transition_in.type == "dissolve"
    project.shots[1].transition_in = TransitionSpec(type="fade_white")
    store.save(project)

    response = client.post(f"/api/projects/{project_id}/assemble")
    assert response.status_code == 200, response.text
    body = response.json()
    stored = store.get(project_id)

    diverged = [
        line for line in body["job"]["look"]["transitions"] if line.startswith("diverged: ")
    ]
    assert len(diverged) == 1, body["job"]["look"]["transitions"]
    assert diverged[0] == TRANSITION_DIVERGED_RECORD.format(
        before=shot_label(stored, stored.shots[0]),
        after=shot_label(stored, stored.shots[1]),
        out="dissolve",
        incoming="fade_white",
    )
    # The outgoing Shot's type is what ran, and the blend is still in the plan beside the report.
    assert "shot_a=dissolve" in body["job"]["look"]["transitions"]
    assert body["clip_count"] == 3
    export = tmp_path / "projects" / project_id / "media" / body["export"]
    assert abs(float(probe(export, "format=duration")) - 8.0) <= 1 / 24
    assert comfy.prompts == []


def test_an_unset_or_agreeing_mirror_is_not_a_divergence(tmp_path: Path):
    """The narrow half of the definition, which is where this report can go wrong.

    Three states that are **not** divergences and must produce no line at all:

    * a mirror that agrees, which is every pair the route itself writes;
    * a mirror that is simply **unset** -- the ordinary state of a one-sided transition and of any
      pair a client wrote one end of. Reporting it would make the report fire on the ordinary
      state, which is the failure mode of every report nobody reads;
    * two fields that differ with **no Overlap** between the Shots: there is no pair there to
      disagree, only the outgoing Shot's own one-sided treatment and an incoming field the export
      never reads.

    The third is the one a reading of AD-30 alone would get wrong, so it is asserted with the two
    fields genuinely holding different types.
    """
    client, store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, store, tmp_path)
    overlap_the_two_shots(client, store, tmp_path, project_id, overlap=0.5)
    assert set_transition(client, project_id, "shot_a", "dissolve").status_code == 200

    def transitions_after_export() -> list[str]:
        response = client.post(f"/api/projects/{project_id}/assemble")
        assert response.status_code == 200, response.text
        return response.json()["job"]["look"]["transitions"]

    # Agreeing, which is what the route writes.
    assert transitions_after_export() == ["shot_a=dissolve"]

    # Unset, which is a one-sided transition rather than a disagreement.
    #
    # **The other Shot keeps a mirror, and that is not decoration.** A first draft cleared the
    # only `transition_in` in the project, which empties the whole mapping — and the report's own
    # "this project mirrors nothing" early return then answered before the unset case was ever
    # reached. Mutating `incoming is None` out of the predicate **survived** against that fixture:
    # it made its own defect impossible, which is the shape this repository has now met roughly
    # twenty times. `shot_a` carries an incoming type **no pair points at** (it is the first Shot)
    # purely so the mapping is non-empty while this pair's mirror is absent.
    #
    # *Amended 2026-08-31 by R-45.* That parenthesis read "an incoming type nothing reads", and
    # since story 11.f8 the first Shot's own opening frames are treated from it: `shot_a` lays the
    # plan's first frame, so the record below is its opening. The fixture's job is unchanged --
    # the mapping is non-empty and this pair's mirror is unset -- and the extra line is asserted
    # rather than filtered out, so this test cannot go blind to a treatment appearing here.
    project = store.get(project_id)
    project.shots[0].transition_in = TransitionSpec(type="fade_black")
    project.shots[1].transition_in = None
    store.save(project)
    assert transitions_after_export() == [
        "shot_a=dissolve", "shot_a=fade_black opening over 12 frames"
    ]

    # Differing, with the Overlap dragged away: not a pair, so not a divergence.
    project = store.get(project_id)
    project.shots[1].transition_in = TransitionSpec(type="wipe_up")
    store.save(project)
    for shot_id in ("shot_a", "shot_b"):
        client.post(f"/api/projects/{project_id}/shots/{shot_id}/unapprove")
    shots = [shot.model_dump(mode="json") for shot in store.get(project_id).shots]
    shots[1]["start"] = 4.0
    shots[1]["duration"] = 4.0
    assert client.put(
        f"/api/projects/{project_id}/shots", json={"shots": shots}
    ).status_code == 200
    for shot_id in ("shot_a", "shot_b"):
        assert client.post(
            f"/api/projects/{project_id}/shots/{shot_id}/approve"
        ).status_code == 200
    recorded = transitions_after_export()
    assert not [line for line in recorded if line.startswith("diverged: ")], recorded
    # `shot_a`'s own two ends, at two boundaries: the tail into `shot_b` from `transition_out`,
    # and the video's opening from the `transition_in` set above (R-45). `shot_b`'s `wipe_up` is
    # the mirror this block is about and composes nothing, which is what the two lines say.
    assert recorded == [
        "shot_a=dissolve one-sided over 12 frames",
        "shot_a=fade_black opening over 12 frames",
    ]
    assert comfy.prompts == []
