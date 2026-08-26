"""The Monitor's preview render, driven with real ffmpeg on synthesized media.

No GPU, no ComfyUI. These tests build what a manifest holds after real renders — tiny colour
takes under the ComfyUI output root — approve them, and then drive the preview route: the
fingerprint that names the clip, the cache that serves it, the supersede rule that discards a
render a newer request replaced, and the geometry rule that takes half of the *export's* size
rather than half of the previewed take's.

The strongest claims here are the ones a grep could never make: a superseded render's file never
reaches the cache, a preview writes nothing to the manifest, and corrupting every byte of the
cache leaves the export unaffected.
"""

import asyncio
import subprocess
import sys
import threading
import time
import wave
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient

from music_video_producer.app import (
    PREVIEW_ABANDONED_DETAIL,
    PREVIEW_NO_TAKE_REFUSAL,
    PREVIEW_SUPERSEDED_REFUSAL,
    create_app,
)
from music_video_producer.assembly import EXPORT_PRESETS, PREVIEW_PRESET
from music_video_producer.comfy import ComfyError
from music_video_producer.config import Settings
from music_video_producer.store import ProjectStore


class FakeComfy:
    """The no-GPU double. A preview must never touch it; `prompts` staying empty is the claim."""

    def __init__(self):
        self.prompts = []

    async def health(self):
        return {"online": True, "url": "http://fake"}

    async def submit(self, prompt, client_id=None):
        self.prompts.append(prompt)
        raise ComfyError("a preview must not submit to ComfyUI")

    async def queue(self):
        return {"queue_running": [], "queue_pending": []}

    async def history(self, prompt_id):
        raise ComfyError("a preview must not read ComfyUI history")


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
    """A real tiny take: colour source, 24 fps, yuv420p — deliberately longer than its window."""
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


def pixel(path: Path, x: int, y: int) -> tuple[int, int, int]:
    """One RGB sample from the first frame of a clip, indexed out of the raw frame."""
    width, height = (int(part) for part in probe(path, "stream=width,height").split(","))
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", path.as_posix(),
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        check=True,
        capture_output=True,
    )
    assert len(result.stdout) >= width * height * 3, (path, width, height)
    start = (y * width + x) * 3
    return tuple(result.stdout[start:start + 3])


def project_with_two_approved_takes(
    client, tmp_path: Path, *, first_size: str = "128x72", second_size: str = "192x108"
):
    """An 8 s song tiled by two approved, on-disk, snapshotted takes of different sizes."""
    project_id = client.post("/api/projects", json={"name": "Preview"}).json()["id"]
    upload = client.post(
        f"/api/projects/{project_id}/songs/upload",
        data={"title": "Preview Song", "duration": "0"},
        files={"file": ("song.wav", wav_bytes(8.0), "audio/wav")},
    )
    assert upload.status_code == 200, upload.text

    shots_dir = tmp_path / "comfy" / "output" / "music-video-producer" / project_id / "shots"
    synthesize_take(shots_dir / "shot_a-h3_00001-audio.mp4", 4.458, first_size, "red")
    synthesize_take(shots_dir / "shot_b-h3_00001-audio.mp4", 4.458, second_size, "blue")

    prefix = f"music-video-producer/{project_id}/shots"
    shots = [
        {
            "id": "shot_a", "start": 0, "duration": 4.0, "prompt": "Red room",
            "status": "complete",
            "latest_output": f"{prefix}/shot_a-h3_00001-audio.mp4",
        },
        {
            "id": "shot_b", "start": 4.0, "duration": 4.0, "prompt": "Blue room",
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


# ------------------------------------------------------------------------------------------
# The render itself, and what it is a picture of.
# ------------------------------------------------------------------------------------------


def test_a_preview_is_the_window_at_half_the_plans_geometry_and_touches_nothing_else(
    tmp_path: Path,
):
    """The happy path, measured on the written file: the clip exists, ffprobe puts it at half
    the plan's dimensions and at the window's own frame count, the approved take is
    byte-identical afterwards, the manifest is byte-identical afterwards, and ComfyUI was never
    asked for anything."""
    client, store, comfy, _app = make_client(tmp_path)
    project_id, shots_dir = project_with_two_approved_takes(client, tmp_path)
    take = shots_dir / "shot_a-h3_00001-audio.mp4"
    take_before = take.read_bytes()
    manifest = store.manifest_path(project_id)
    manifest_before = manifest.read_bytes()

    response = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")

    assert response.status_code == 200, response.text
    body = response.json()
    clip = tmp_path / "projects" / project_id / "media" / body["preview"]
    assert body["preview"] == f"previews/{body['fingerprint']}.mp4"
    assert body["rendered"] is True
    assert clip.is_file()
    # AD-29: half of 192x108, the largest approved take in the project — never half of
    # shot_a's own 128x72, which would be 64x36.
    assert (body["width"], body["height"]) == (96, 54)
    assert probe(clip, "stream=width,height") == "96,54"
    # The exposed window on the export's own grid: 4 s at 24 fps.
    assert body["frames"] == 96
    assert body["window_seconds"] == 4.0
    # No audio: `trim_args` builds every clip with `-an`, and the preview is that argv.
    assert probe(clip, "stream=codec_type").splitlines() == ["video"]

    # FX-23, and the standing rule: a preview never writes to, replaces or modifies the
    # Approved Output — nor, being derived, the manifest that describes it.
    assert take.read_bytes() == take_before
    assert manifest.read_bytes() == manifest_before
    assert comfy.prompts == []

    # It streams through the existing project-media route.
    served = client.get(body["preview_url"])
    assert served.status_code == 200
    assert served.content[:8] == clip.read_bytes()[:8]


def test_the_effect_stack_is_actually_applied_by_the_exports_own_chain(tmp_path: Path):
    """A red take previews red; the same take with a Monochrome card previews grey. The
    difference is `build_effect_stages` composing into `trim_args`, which is the export's chain
    and not a second one."""
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, tmp_path)
    media = tmp_path / "projects" / project_id / "media"

    plain = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")
    assert plain.status_code == 200, plain.text
    red = pixel(media / plain.json()["preview"], 48, 27)
    assert red[0] > 150 and red[1] < 80 and red[2] < 80, red

    written = client.put(
        f"/api/projects/{project_id}/shots/shot_a/effects",
        json={"effects": [{"effect": "monochrome", "parameters": {"amount": 1.0}}]},
    )
    assert written.status_code == 200, written.text
    graded = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")
    assert graded.status_code == 200, graded.text
    assert graded.json()["fingerprint"] != plain.json()["fingerprint"]
    grey = pixel(media / graded.json()["preview"], 48, 27)
    assert max(grey) - min(grey) <= 8, grey


def test_a_branched_effect_previews_at_the_previews_own_geometry(tmp_path: Path):
    """The preview is the export's chain at half the size, and story 9.7 gave that chain a
    shape it had never carried: a stage that is a whole filtergraph rather than one filter.

    This is the only other place `build_effect_stages` is called, so it is the only other place
    a branch reaches ffmpeg — and it reaches it through a different preset, a different
    geometry and a different offset rule. A Slow Zoom is the branch used, because it is also the
    stage that needs the Shot's own length, which the preview reads off the Shot rather than off
    a `ClipWindow`: a preview is always the whole Shot from its own first frame, so its offset
    inside the Shot is zero and the span is the Shot's window.

    The frame count is asserted because a preview one frame short is a picture of a clip the
    export will not produce, which is the one thing a preview must never be. It is **not** a
    test of `BRANCH_FRAME_GUARD`, and saying so is the honest half: a take here runs longer than
    its window, as every real take does, so the graph never reaches its own end and the frame a
    branch costs at `fps` is never taken. Removing the guard leaves this test green — measured.
    The guard's own test is `test_the_branch_guard_is_the_frame_the_branch_would_otherwise_cost`
    in `tests/test_effects.py`, which renders a source holding exactly the frames asked for.
    """
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, tmp_path)
    media = tmp_path / "projects" / project_id / "media"

    written = client.put(
        f"/api/projects/{project_id}/shots/shot_a/effects",
        json={"effects": [{"effect": "slow_zoom", "parameters": {"zoom": 1.6}}]},
    )
    assert written.status_code == 200, written.text
    response = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")

    assert response.status_code == 200, response.text
    body = response.json()
    clip = media / body["preview"]
    assert (body["width"], body["height"]) == (96, 54)
    assert probe(clip, "stream=width,height") == "96,54"
    # Four seconds of window at 24 fps, and every frame of it: the branch cost the chain
    # nothing, which is what the guard at the head of it is for.
    assert body["frames"] == 96
    counted = subprocess.run(
        [
            "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
            "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", clip.as_posix(),
        ],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert counted == "96"


def test_a_shot_whose_aspect_differs_previews_with_the_letterbox_it_will_ship_with(
    tmp_path: Path,
):
    """AD-29's whole reason. A 4:3 take in a project whose delivery grid is 16:9 previews at the
    grid's shape with black bars, not at its own shape with none — half of *its* dimensions
    would be 72x54 and would show a frame the export will never produce."""
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(
        client, tmp_path, first_size="144x108", second_size="192x108"
    )
    media = tmp_path / "projects" / project_id / "media"

    response = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")

    assert response.status_code == 200, response.text
    body = response.json()
    clip = media / body["preview"]
    assert (body["width"], body["height"]) == (96, 54)
    assert probe(clip, "stream=width,height") == "96,54"
    # 144x108 fits inside 96x54 as 72x54, so 12 columns of bar sit either side. Near-black
    # rather than exactly black: CRF 28 is a lossy encode and the point of the assertion is that
    # a bar is there at all, where half of the take's own dimensions would have put picture.
    assert max(pixel(clip, 0, 27)) <= 16, pixel(clip, 0, 27)
    assert max(pixel(clip, 95, 27)) <= 16, pixel(clip, 95, 27)
    # And the picture between the bars is the take's own red.
    middle = pixel(clip, 48, 27)
    assert middle[0] > 150 and middle[1] < 80, middle


# ------------------------------------------------------------------------------------------
# The cache, and staleness derived rather than stored.
# ------------------------------------------------------------------------------------------


def test_an_unchanged_request_serves_the_cached_clip_and_renders_nothing(tmp_path: Path):
    """Nothing changed, so the fingerprint is the same, so the file is already there. The
    proof that nothing re-ran is the file's own modification time, not a claim in the body."""
    client, store, _comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, tmp_path)
    media = tmp_path / "projects" / project_id / "media"

    first = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")
    assert first.status_code == 200, first.text
    clip = media / first.json()["preview"]
    stamp = clip.stat().st_mtime_ns
    bytes_before = clip.read_bytes()
    manifest_before = store.manifest_path(project_id).read_bytes()

    second = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")

    assert second.status_code == 200, second.text
    assert second.json()["fingerprint"] == first.json()["fingerprint"]
    assert second.json()["rendered"] is False
    assert clip.stat().st_mtime_ns == stamp
    assert clip.read_bytes() == bytes_before
    # Derived, never stored: two reads of staleness wrote not one byte of the manifest.
    assert store.manifest_path(project_id).read_bytes() == manifest_before
    assert len(list((media / "previews").glob("*.mp4"))) == 1


def test_each_fingerprint_input_that_exists_today_makes_the_preview_due_again(tmp_path: Path):
    """Four of the eight inputs are reachable from the wire today — the stack, the window, the
    offset and the geometry. Each one moved produces a different fingerprint and a new clip; the
    old clip stays exactly where it was, which is what makes a stale entry inert rather than
    wrong."""
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id, shots_dir = project_with_two_approved_takes(client, tmp_path)
    media = tmp_path / "projects" / project_id / "media"
    prefix = f"music-video-producer/{project_id}/shots"

    def preview() -> str:
        response = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")
        assert response.status_code == 200, response.text
        return response.json()["fingerprint"]

    def write_shots(**shot_a) -> None:
        shots = [
            {
                "id": "shot_a", "start": 0, "duration": 4.0, "prompt": "Red room",
                "status": "approved",
                "latest_output": f"{prefix}/shot_a-h3_00001-audio.mp4",
                "approved_output": f"{prefix}/shot_a-h3_00001-audio.mp4",
                "approved_start": 0, "approved_duration": 4.0,
                **shot_a,
            },
            {
                "id": "shot_b", "start": 4.0, "duration": 4.0, "prompt": "Blue room",
                "status": "approved",
                "latest_output": f"{prefix}/shot_b-h3_00001-audio.mp4",
                "approved_output": f"{prefix}/shot_b-h3_00001-audio.mp4",
                "approved_start": 4.0, "approved_duration": 4.0,
            },
        ]
        saved = client.put(f"/api/projects/{project_id}/shots", json={"shots": shots})
        assert saved.status_code == 200, saved.text

    seen = {preview()}

    written = client.put(
        f"/api/projects/{project_id}/shots/shot_a/effects",
        json={"effects": [{"effect": "saturation", "parameters": {"amount": 1.4}}]},
    )
    assert written.status_code == 200, written.text
    stack_moved = preview()
    assert stack_moved not in seen
    seen.add(stack_moved)

    write_shots(duration=3.5)
    window_moved = preview()
    assert window_moved not in seen
    seen.add(window_moved)

    write_shots(duration=3.5, trim_nudge=0.25)
    offset_moved = preview()
    assert offset_moved not in seen
    seen.add(offset_moved)

    # The geometry input: a bigger take elsewhere in the project moves the delivery grid, so
    # shot_a's own preview is due again although nothing about shot_a changed.
    synthesize_take(shots_dir / "shot_b-h3_00001-audio.mp4", 4.458, "256x144", "blue")
    geometry_moved = preview()
    assert geometry_moved not in seen
    seen.add(geometry_moved)

    # Every earlier clip is still on disk, untouched and unserved. Staleness is a name that no
    # longer matches, never a file that was rewritten.
    assert {path.stem for path in (media / "previews").glob("*.mp4")} == seen


def test_a_deleted_cache_costs_a_re_render_and_nothing_else(tmp_path: Path):
    """The AC, and the way a Director recovers from a bad preview: remove the folder at any
    moment. The same fingerprint comes back and the clip is rebuilt."""
    client, store, _comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, tmp_path)
    media = tmp_path / "projects" / project_id / "media"

    first = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")
    assert first.status_code == 200, first.text
    clip = media / first.json()["preview"]
    manifest_before = store.manifest_path(project_id).read_bytes()
    clip.unlink()
    (media / "previews").rmdir()

    again = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")

    assert again.status_code == 200, again.text
    assert again.json()["fingerprint"] == first.json()["fingerprint"]
    assert again.json()["rendered"] is True
    assert clip.is_file()
    assert store.manifest_path(project_id).read_bytes() == manifest_before
    assert client.get(f"/api/projects/{project_id}").status_code == 200


def test_the_preview_cache_is_never_an_input_to_an_export(tmp_path: Path):
    """Every byte of the cache replaced by garbage, and the export still assembles and verifies.
    An export that read the cache could not survive this; one that rebuilds from the approved
    takes cannot notice it."""
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, tmp_path)
    media = tmp_path / "projects" / project_id / "media"

    for shot_id in ("shot_a", "shot_b"):
        assert client.post(
            f"/api/projects/{project_id}/shots/{shot_id}/preview"
        ).status_code == 200
    cached = sorted((media / "previews").glob("*.mp4"))
    assert len(cached) == 2
    for path in cached:
        path.write_bytes(b"not a video, not even close")

    response = client.post(f"/api/projects/{project_id}/assemble")

    assert response.status_code == 200, response.text
    body = response.json()
    export = media / body["export"]
    assert export.is_file()
    assert abs(float(probe(export, "format=duration")) - 8.0) <= 1 / 24
    assert (body["width"], body["height"]) == (192, 108)
    # And the corrupt cache is still corrupt: the export neither read it nor repaired it.
    assert all(path.read_bytes() == b"not a video, not even close" for path in cached)


# ------------------------------------------------------------------------------------------
# Supersede, do not queue.
# ------------------------------------------------------------------------------------------


def wait_for_a_render_in_flight(app, project_id: str, deadline: float = 20.0):
    """Block until the server has registered an in-flight preview for this project."""
    limit = time.monotonic() + deadline
    while time.monotonic() < limit:
        record = app.state.preview_renders.get(project_id)
        if record is not None:
            return record
        time.sleep(0.002)
    raise AssertionError("no preview render was ever registered")


def wait_for_joiners(app, project_id: str, count: int, deadline: float = 20.0):
    """Block until `count` requests have attached to this project's in-flight render.

    Sequencing a three-way race needs a fact, not a sleep: a test that fires the superseding
    request "after a moment" and hopes the joiner got there first can pass while asserting
    nothing. `PreviewRender.joiners` is that fact.
    """
    limit = time.monotonic() + deadline
    while time.monotonic() < limit:
        record = app.state.preview_renders.get(project_id)
        if record is not None and record.joiners >= count:
            return record
        time.sleep(0.002)
    raise AssertionError(f"fewer than {count} requests ever joined the render")


class RenderSpy:
    """Counts the processes the preview render actually starts, and can replace what they run.

    Counting them **directly** rather than inferring one from timings, scratch files or elapsed
    milliseconds: `asyncio.create_subprocess_exec` is the single call in this application that
    starts a process, so wrapping it is a complete census. Only the preview render's own argv is
    counted — its last argument is the dotted scratch file under `previews/`, which nothing else
    writes — so ffprobe measuring geometry and an export's own ffmpeg pass through uncounted.

    `replacement` swaps the argv of exactly those processes for another command. One test needs
    a preview render that fails *slowly*: a real ffmpeg fails on a bad take in milliseconds,
    faster than any second request can arrive, so there would be nothing to join. Everything
    around it stays real — a real subprocess, a real non-zero return, real stderr, the real
    `on_start`/`attach` handover and the real route.
    """

    def __init__(self, monkeypatch, replacement: list[str] | None = None):
        self.started: list[list[str]] = []
        self.replacement = replacement
        self._real = asyncio.create_subprocess_exec
        monkeypatch.setattr(asyncio, "create_subprocess_exec", self._spawn)

    async def _spawn(self, *args, **kwargs):
        last = str(args[-1]) if args else ""
        if "/previews/." in last and last.endswith(".mp4"):
            self.started.append([str(arg) for arg in args])
            if self.replacement is not None:
                args = tuple(self.replacement)
        return await self._real(*args, **kwargs)

    @property
    def count(self) -> int:
        return len(self.started)


def slow_stack(client, project_id: str, *, grain: float = 30.0):
    """A stack heavy enough that the render is comfortably longer than one HTTP round trip."""
    return client.put(
        f"/api/projects/{project_id}/shots/shot_a/effects",
        json={"effects": [
            {"effect": "soft_focus", "parameters": {"sigma": 6.0}},
            {"effect": "grain", "parameters": {"strength": grain, "seed": 7}},
        ]},
    )


def project_with_one_slow_shot(client, tmp_path: Path):
    """One 12 s 960x540 approved take under a heavy stack, ready for a race.

    The geometry memo is warmed by a throwaway preview whose clip is then deleted, so a request
    under test reaches the registry without an ffprobe in front of it and the race asserted on
    is the race that was set up.
    """
    project_id = client.post("/api/projects", json={"name": "Supersede"}).json()["id"]
    upload = client.post(
        f"/api/projects/{project_id}/songs/upload",
        data={"title": "Song", "duration": "0"},
        files={"file": ("song.wav", wav_bytes(12.0), "audio/wav")},
    )
    assert upload.status_code == 200, upload.text
    shots_dir = tmp_path / "comfy" / "output" / "music-video-producer" / project_id / "shots"
    synthesize_take(shots_dir / "shot_a-h3_00001-audio.mp4", 12.5, "960x540", "red")
    prefix = f"music-video-producer/{project_id}/shots"
    shots = [{
        "id": "shot_a", "start": 0, "duration": 12.0, "prompt": "Slow",
        "status": "complete",
        "latest_output": f"{prefix}/shot_a-h3_00001-audio.mp4",
    }]
    assert client.put(
        f"/api/projects/{project_id}/shots", json={"shots": shots}
    ).status_code == 200
    assert client.post(f"/api/projects/{project_id}/shots/shot_a/approve").status_code == 200
    assert slow_stack(client, project_id).status_code == 200
    assert client.post(f"/api/projects/{project_id}/shots/shot_a/preview").status_code == 200
    media = tmp_path / "projects" / project_id / "media"
    for path in (media / "previews").glob("*.mp4"):
        path.unlink()
    return project_id, shots_dir, media


def fire_preview(client, project_id: str, into: dict, shot_id: str = "shot_a"):
    """One preview request on this thread, its whole answer recorded rather than asserted."""
    response = client.post(f"/api/projects/{project_id}/shots/{shot_id}/preview")
    into["status"] = response.status_code
    into["body"] = response.json()


def test_a_newer_request_cancels_the_render_in_flight_and_its_clip_is_never_served(
    tmp_path: Path, monkeypatch,
):
    """AD-24, against a real pair of concurrent requests. The first render is deliberately
    expensive; the second is fired the instant the server registers the first, asks for a
    *different* picture, and supersedes it. The first answers with a refusal, its fingerprint
    never appears in the cache, and no scratch file is left behind — a cancelled render cannot
    land its output and then be served as current.

    R-22 narrowed supersede to a differing fingerprint and this is the differing case, so the
    process census is asserted here too: two requests, two renders, nothing joined."""
    client, _store, _comfy, app = make_client(tmp_path)
    project_id, _shots_dir, media = project_with_one_slow_shot(client, tmp_path)
    spy = RenderSpy(monkeypatch)

    first: dict = {}
    thread = threading.Thread(target=fire_preview, args=(client, project_id, first))
    thread.start()
    try:
        record = wait_for_a_render_in_flight(app, project_id)
        superseded_fingerprint = record.fingerprint
        # A change the Director could make with one drag, which is the whole scenario: the
        # replacement asks for a different picture, so it cannot be answered by the first.
        assert slow_stack(client, project_id, grain=12.0).status_code == 200
        second = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")
    finally:
        thread.join(timeout=60)

    assert second.status_code == 200, second.text
    assert first["status"] == 409, first
    assert first["body"]["detail"] == PREVIEW_SUPERSEDED_REFUSAL.format(
        shot="SHOT 01 (shot_a)"
    )
    assert second.json()["fingerprint"] != superseded_fingerprint
    # Superseded, not joined: each request ran a render of its own, and the discarded one
    # really was started rather than merely registered.
    assert spy.count == 2, spy.started
    assert record.joiners == 0
    # The discarded render landed nothing: not under its own name, and not as a scratch file
    # waiting to be mistaken for one.
    assert not (media / "previews" / f"{superseded_fingerprint}.mp4").exists()
    assert sorted(path.name for path in (media / "previews").iterdir()) == [
        f"{second.json()['fingerprint']}.mp4"
    ]
    # The registry is empty again, so the next request supersedes nothing.
    assert app.state.preview_renders == {}


# ------------------------------------------------------------------------------------------
# Join an identical render, never restart it (R-22).
# ------------------------------------------------------------------------------------------


def test_an_identical_request_joins_the_render_in_flight_and_starts_no_second_ffmpeg(
    tmp_path: Path, monkeypatch,
):
    """R-22, and the whole of it. Two requests for the same picture arrive while one render is
    underway; the second asks for exactly the work already in progress, so it waits on it rather
    than killing it. One ffmpeg process, counted at the call that starts processes, and two
    successful answers naming the same clip.

    Under the old rule this test could not pass: the second request superseded the first, the
    first answered 409, and a client that double-fires -- a retry, a poll, a re-render on window
    focus -- could never be shown anything."""
    client, _store, _comfy, app = make_client(tmp_path)
    project_id, _shots_dir, media = project_with_one_slow_shot(client, tmp_path)
    spy = RenderSpy(monkeypatch)

    first: dict = {}
    thread = threading.Thread(target=fire_preview, args=(client, project_id, first))
    thread.start()
    try:
        record = wait_for_a_render_in_flight(app, project_id)
        second = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")
    finally:
        thread.join(timeout=60)

    assert first["status"] == 200, first
    assert second.status_code == 200, second.text
    # One render, asserted at the process, not inferred from a timing.
    assert spy.count == 1, spy.started
    assert record.joiners == 1
    assert second.json()["fingerprint"] == first["body"]["fingerprint"] == record.fingerprint
    # Both were answered by a render that ran just now, so both say so.
    assert first["body"]["rendered"] is True
    assert second.json()["rendered"] is True
    # And one clip on disk under that fingerprint, with no scratch file abandoned beside it.
    clip = media / second.json()["preview"]
    assert clip.is_file()
    assert [path.name for path in sorted((media / "previews").iterdir())] == [clip.name]
    assert app.state.preview_renders == {}


def test_three_identical_requests_all_join_the_same_render(tmp_path: Path, monkeypatch):
    """Two joiners get the answer, and so do three. The count is the point: a join that only
    ever released the first waiter would pass the two-request test and strand the rest."""
    client, _store, _comfy, app = make_client(tmp_path)
    project_id, _shots_dir, media = project_with_one_slow_shot(client, tmp_path)
    spy = RenderSpy(monkeypatch)

    leader: dict = {}
    joined: list[dict] = [{}, {}, {}]
    threads = [threading.Thread(target=fire_preview, args=(client, project_id, leader))]
    threads[0].start()
    try:
        record = wait_for_a_render_in_flight(app, project_id)
        for answer in joined:
            thread = threading.Thread(
                target=fire_preview, args=(client, project_id, answer)
            )
            threads.append(thread)
            thread.start()
        wait_for_joiners(app, project_id, 3)
    finally:
        for thread in threads:
            thread.join(timeout=120)

    assert [answer["status"] for answer in [leader, *joined]] == [200, 200, 200, 200]
    assert {answer["body"]["fingerprint"] for answer in [leader, *joined]} == {
        record.fingerprint
    }
    assert spy.count == 1, spy.started
    assert record.joiners == 3
    assert len(list((media / "previews").glob("*.mp4"))) == 1


def test_a_joiner_on_a_failing_render_is_told_why_rather_than_left_waiting(
    tmp_path: Path, monkeypatch,
):
    """The render both requests are attached to returns non-zero. Neither hangs, neither is told
    it succeeded, and both carry the same named reason -- the joiner formatting it around its
    own Shot, since a fingerprint can be shared and the Shot that started the render need not be
    the Shot asking again.

    The render is replaced by a subprocess that sleeps and then fails, because a real ffmpeg
    refusing a bad take returns in milliseconds and there would be no in-flight render to join.
    Everything else is the real route, a real process and its real stderr."""
    client, _store, _comfy, app = make_client(tmp_path)
    project_id, _shots_dir, media = project_with_one_slow_shot(client, tmp_path)
    spy = RenderSpy(monkeypatch, replacement=[
        sys.executable, "-c",
        (
            "import sys, time; time.sleep(1.5);"
            " sys.stderr.write('the preview encoder gave up'); sys.exit(3)"
        ),
    ])

    first: dict = {}
    thread = threading.Thread(target=fire_preview, args=(client, project_id, first))
    thread.start()
    try:
        record = wait_for_a_render_in_flight(app, project_id)
        second = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")
    finally:
        thread.join(timeout=60)

    assert first["status"] == 502, first
    assert second.status_code == 502, second.text
    # It really was a join -- one process for two failed answers.
    assert spy.count == 1, spy.started
    assert record.joiners == 1
    for detail in (first["body"]["detail"], second.json()["detail"]):
        assert detail.startswith("SHOT 01 (shot_a)'s preview could not be rendered")
        assert "the preview encoder gave up" in detail
        # Not the placeholder a render that ended without recording anything would leave.
        assert PREVIEW_ABANDONED_DETAIL not in detail
    # Nothing published, under any name, by either request.
    assert list((media / "previews").iterdir()) == []
    assert app.state.preview_renders == {}


def test_a_joiner_is_refused_rather_than_stranded_when_its_render_is_superseded(
    tmp_path: Path, monkeypatch,
):
    """Three requests: one render, one joiner on it, and then a third asking for a different
    picture. The third supersedes the render, which means the clip the joiner is waiting for
    will never exist -- so the joiner is refused for the same reason and by the same sentence,
    rather than waiting on an event that would never be raised.

    `wait_for_joiners` is what makes this the scenario it claims to be: without it the third
    request could arrive before the second and the test would assert about two supersedes."""
    client, _store, _comfy, app = make_client(tmp_path)
    project_id, _shots_dir, media = project_with_one_slow_shot(client, tmp_path)
    spy = RenderSpy(monkeypatch)

    leader: dict = {}
    joiner: dict = {}
    threads = [threading.Thread(target=fire_preview, args=(client, project_id, leader))]
    threads[0].start()
    try:
        record = wait_for_a_render_in_flight(app, project_id)
        threads.append(
            threading.Thread(target=fire_preview, args=(client, project_id, joiner))
        )
        threads[1].start()
        wait_for_joiners(app, project_id, 1)
        assert slow_stack(client, project_id, grain=12.0).status_code == 200
        third = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")
    finally:
        for thread in threads:
            thread.join(timeout=60)

    refusal = PREVIEW_SUPERSEDED_REFUSAL.format(shot="SHOT 01 (shot_a)")
    assert leader["status"] == 409, leader
    assert leader["body"]["detail"] == refusal
    # The joiner answered at all, and answered honestly: not a 200 pointing at a clip that was
    # never written, and not a thread still parked on `done`.
    assert joiner["status"] == 409, joiner
    assert joiner["body"]["detail"] == refusal
    assert third.status_code == 200, third.text
    assert third.json()["fingerprint"] != record.fingerprint
    # Two renders for three requests: the joiner started none, and the superseded one landed
    # nothing under its own name.
    assert spy.count == 2, spy.started
    assert record.joiners == 1
    assert [path.name for path in sorted((media / "previews").iterdir())] == [
        f"{third.json()['fingerprint']}.mp4"
    ]
    assert app.state.preview_renders == {}


def test_a_preview_neither_blocks_an_export_nor_waits_on_one(tmp_path: Path):
    """A Batch or an assemble holds the busy discipline; a preview is not part of it. The
    export runs on one thread while previews are requested on another, and both answer."""
    client, _store, _comfy, app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, tmp_path)

    export: dict = {}

    def assemble() -> None:
        response = client.post(f"/api/projects/{project_id}/assemble")
        export["status"] = response.status_code
        export["body"] = response.json() if response.status_code == 200 else response.text

    thread = threading.Thread(target=assemble)
    thread.start()
    try:
        previews = [
            client.post(f"/api/projects/{project_id}/shots/{shot_id}/preview")
            for shot_id in ("shot_a", "shot_b")
        ]
    finally:
        thread.join(timeout=120)

    assert export["status"] == 200, export
    assert export["body"]["job"]["status"] == "complete"
    assert [response.status_code for response in previews] == [200, 200]
    assert app.state.preview_renders == {}
    # And the reverse direction: with previews cached, an assemble is refused by nothing.
    again = client.post(f"/api/projects/{project_id}/assemble")
    assert again.status_code == 200, again.text


# ------------------------------------------------------------------------------------------
# Refusals.
# ------------------------------------------------------------------------------------------


def test_a_shot_with_no_approved_take_is_refused_by_name(tmp_path: Path):
    """A preview is a picture of a file, and this Shot has not decided which file yet. The
    refusal names the shot and the remedy, and nothing is rendered."""
    client, _store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, tmp_path)
    assert client.post(
        f"/api/projects/{project_id}/shots/shot_a/unapprove"
    ).status_code == 200

    response = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == PREVIEW_NO_TAKE_REFUSAL.format(shot="SHOT 01 (shot_a)")
    assert not (tmp_path / "projects" / project_id / "media" / "previews").exists()
    assert comfy.prompts == []


def test_an_unknown_shot_is_a_404(tmp_path: Path):
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, tmp_path)
    response = client.post(f"/api/projects/{project_id}/shots/shot_zz/preview")
    assert response.status_code == 404, response.text


def test_a_take_that_has_gone_from_disk_is_refused_by_name(tmp_path: Path):
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id, shots_dir = project_with_two_approved_takes(client, tmp_path)
    (shots_dir / "shot_a-h3_00001-audio.mp4").unlink()

    response = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "SHOT 01 (shot_a)" in detail and "shot_a-h3_00001-audio.mp4" in detail


def test_a_look_whose_file_has_gone_refuses_by_name_and_leaves_the_stack_untouched(
    tmp_path: Path,
):
    """The stack was valid when it was written and the `.cube` has since been deleted. The
    preview refuses with the chain's own sentence — the same one the export refuses with — and
    the Effect Stack is exactly as the Director left it."""
    client, store, _comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, tmp_path)
    catalogue = client.get("/api/effects/catalogue")
    assert catalogue.status_code == 200, catalogue.text
    looks = catalogue.json()["looks"]
    assert looks, catalogue.text
    lut_id = looks[0]["lut_id"]
    written = client.put(
        f"/api/projects/{project_id}/shots/shot_a/effects",
        json={"effects": [{"effect": "lut_look", "parameters": {"lut": lut_id}}]},
    )
    assert written.status_code == 200, written.text
    stack_before = store.get(project_id).shots[0].effects
    for path in (tmp_path / "luts").glob("*.cube"):
        path.unlink()

    response = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail.startswith("SHOT 01 (shot_a): ")
    assert lut_id in detail
    assert store.get(project_id).shots[0].effects == stack_before
    previews = tmp_path / "projects" / project_id / "media" / "previews"
    assert not previews.exists() or list(previews.iterdir()) == []


def test_a_failed_render_names_its_reason_and_leaves_the_stack_untouched(tmp_path: Path):
    """ffmpeg returns non-zero on a take that is not decodable. The reason is named, the
    manifest is untouched, and no half-written file is left in the cache under any name."""
    client, store, _comfy, app = make_client(tmp_path)
    project_id, shots_dir = project_with_two_approved_takes(client, tmp_path)
    written = client.put(
        f"/api/projects/{project_id}/shots/shot_a/effects",
        json={"effects": [{"effect": "contrast", "parameters": {"amount": 1.3}}]},
    )
    assert written.status_code == 200, written.text
    manifest_before = store.manifest_path(project_id).read_bytes()
    # Readable enough to be approved and resolved, unreadable to ffmpeg. shot_b still supplies
    # the project's delivery geometry, so this reaches the render rather than a size refusal.
    (shots_dir / "shot_a-h3_00001-audio.mp4").write_bytes(b"\x00" * 4096)

    response = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")

    assert response.status_code == 502, response.text
    assert response.json()["detail"].startswith(
        "SHOT 01 (shot_a)'s preview could not be rendered"
    )
    assert store.manifest_path(project_id).read_bytes() == manifest_before
    previews = tmp_path / "projects" / project_id / "media" / "previews"
    assert not previews.exists() or list(previews.iterdir()) == []
    assert app.state.preview_renders == {}


# ------------------------------------------------------------------------------------------
# The preset.
# ------------------------------------------------------------------------------------------


def test_the_preview_preset_is_ultrafast_crf_28_and_is_not_offered_at_the_export(tmp_path: Path):
    """Not a hardware encoder, and not a delivery build: NVENC was measured slower at these clip
    lengths, and a preset a Director could name at the assemble route would ship CRF 28."""
    assert (PREVIEW_PRESET.x264_preset, PREVIEW_PRESET.crf) == ("ultrafast", "28")
    assert "preview" not in EXPORT_PRESETS
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, tmp_path)
    refused = client.post(f"/api/projects/{project_id}/assemble", json={"preset": "preview"})
    assert refused.status_code == 422, refused.text
