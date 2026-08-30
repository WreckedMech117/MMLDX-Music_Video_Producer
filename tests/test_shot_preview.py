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
import dataclasses
import json
import math
import struct
import subprocess
import sys
import threading
import time
import wave
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from music_video_producer.app import (
    BINDING_WITHOUT_ENVELOPE_REFUSAL,
    PREVIEW_ABANDONED_DETAIL,
    PREVIEW_NO_TAKE_REFUSAL,
    PREVIEW_SUPERSEDED_REFUSAL,
    SONG_ENVELOPE_NOT_TAKEN,
    SONG_ENVELOPE_SONG_CHANGED,
    create_app,
)
from music_video_producer.assembly import (
    ASSEMBLY_FPS,
    ASSEMBLY_OFFSET_NEGATIVE_REFUSAL,
    ASSEMBLY_OFFSET_OVERRUN_REFUSAL,
    EXPORT_PRESETS,
    PREVIEW_PRESET,
    clip_frames_on_grid,
)
from music_video_producer.comfy import ComfyError
from music_video_producer.config import Settings
from music_video_producer.effects import EFFECT_CATALOGUE, preview_fingerprint
from music_video_producer.models import SongAnalysis, TransitionSpec
from music_video_producer.store import ProjectStore
from music_video_producer.timeline import over_render_frames, over_render_lead


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
    """A real tiny take: colour source, 24 fps, yuv420p, `over_render_frames(4.0)` long — the
    124 frames H3's grid actually renders for a 4 s window, so how much of it a window consumes
    is decided by the shot's lead, exactly as it is in the app."""
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


def counted_frames(path: Path) -> int:
    """Frames actually decoded out of a clip — never the container's own claim, because the
    defect this file has to be able to see is a file whose header and whose picture disagree."""
    return int(
        subprocess.run(
            [
                "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", path.as_posix(),
            ],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    )


def nudge_shot(client, project_id: str, shot_id: str, nudge: float):
    """The Director's trim nudge, written the way the browser writes it: the whole shot list
    back through `PUT /shots`, approval fields and all."""
    project = client.get(f"/api/projects/{project_id}").json()
    shots = project["shots"]
    for shot in shots:
        if shot["id"] == shot_id:
            shot["trim_nudge"] = nudge
    saved = client.put(f"/api/projects/{project_id}/shots", json={"shots": shots})
    assert saved.status_code == 200, saved.text
    return saved


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
    """An 8 s song tiled by two approved, on-disk, snapshotted takes of different sizes.

    **`shot_b` carries the lead a real last shot carries**, and that is not decoration. Its
    window ends where the song ends, so `timeline.over_render_lead` takes its overflow branch
    and grows the lead until the take's *tail* lands on the song's last second — which spends
    the whole over-render margin ahead of the window and leaves the cut ending on the take's
    final frame. The lead is computed here from that function rather than typed as a number, so
    the fixture cannot drift from the rule that produces it.

    Until 2026-08-26 both shots had a lead of zero and both takes therefore ran a whole margin
    longer than their windows, which is a property of the fixture and not of real takes: the
    branched chain never reached its own end, so the frame a branch costs at `fps` was never
    taken and `BRANCH_FRAME_GUARD` could be deleted with every test here still green. It is
    exercised now, on the shot that exercises it in the app.
    """
    project_id = client.post("/api/projects", json={"name": "Preview"}).json()["id"]
    upload = client.post(
        f"/api/projects/{project_id}/songs/upload",
        data={"title": "Preview Song", "duration": "0"},
        files={"file": ("song.wav", wav_bytes(8.0), "audio/wav")},
    )
    assert upload.status_code == 200, upload.text

    shots_dir = tmp_path / "comfy" / "output" / "music-video-producer" / project_id / "shots"
    picture_seconds = over_render_frames(4.0) / ASSEMBLY_FPS
    synthesize_take(shots_dir / "shot_a-h3_00001-audio.mp4", picture_seconds, first_size, "red")
    synthesize_take(shots_dir / "shot_b-h3_00001-audio.mp4", picture_seconds, second_size, "blue")
    # 1.1667 s — the whole margin, because a shot ending on the song's end takes the overflow
    # branch. `shot_a` starts at 0.0 s, where there is no song to lead into, so its lead is 0.
    trailing_lead = over_render_lead(
        start=4.0, duration=4.0, picture_seconds=picture_seconds, song_duration=8.0
    )
    assert trailing_lead == pytest.approx(picture_seconds - 4.0), trailing_lead

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
            "latest_take_lead": trailing_lead,
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
    export will not produce, which is the one thing a preview must never be — and **this is a
    test of `BRANCH_FRAME_GUARD`**, which it was not until 2026-08-26. It previews `shot_b`, the
    last shot of the song, whose lead takes `over_render_lead`'s overflow branch and spends the
    whole over-render margin ahead of the window: the cut ends on the take's final frame, the
    branched graph therefore reaches its own end, and the frame `fps` drops at a branch is a
    frame there is nothing left to replace. Measured at this geometry with the guard's clone
    removed: **95 frames where 96 were asked for**. `shot_a` starts at 0.0 s, has no lead, and
    consumes 96 of its take's 124 frames, which is why the same assertion on that shot could not
    see the guard at all.
    """
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, tmp_path)
    media = tmp_path / "projects" / project_id / "media"

    written = client.put(
        f"/api/projects/{project_id}/shots/shot_b/effects",
        json={"effects": [{"effect": "slow_zoom", "parameters": {"zoom": 1.6}}]},
    )
    assert written.status_code == 200, written.text
    response = client.post(f"/api/projects/{project_id}/shots/shot_b/preview")

    assert response.status_code == 200, response.text
    body = response.json()
    clip = media / body["preview"]
    assert (body["width"], body["height"]) == (96, 54)
    assert probe(clip, "stream=width,height") == "96,54"
    # Four seconds of window at 24 fps, and every frame of it: the branch cost the chain
    # nothing, which is what the guard at the head of it is for.
    assert body["frames"] == 96
    assert counted_frames(clip) == 96


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


def test_a_correction_to_a_composer_makes_the_cached_clip_of_that_look_due_again(
    tmp_path: Path, monkeypatch
):
    """The whole of finding F2, driven through the route.

    `e4aec46` moved Scanlines' grid origin from `x=-1` to `x=-t`, removing a black left-edge
    bar measured at 26 dark columns at 1920x1080 with `lines=20`. Nothing about the Shot moved,
    so a name taken from the stored stack did not move either — and nothing in this application
    evicts `previews/`: no `unlink`, no `rmtree`, no glob, no control. Every clip cached before
    that commit went on being served with the bar in it, permanently.

    The composer is swapped rather than edited, because the claim is about what the fingerprint
    is a function of and not about Scanlines. The old clip stays exactly where it was: a stale
    entry is inert, never rewritten.
    """
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, tmp_path)
    media = tmp_path / "projects" / project_id / "media"

    written = client.put(
        f"/api/projects/{project_id}/shots/shot_a/effects",
        json={"effects": [{"effect": "scanlines", "parameters": {"strength": 0.5}}]},
    )
    assert written.status_code == 200, written.text

    first = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")
    assert first.status_code == 200, first.text
    assert first.json()["rendered"] is True
    cached = media / first.json()["preview"]
    stamp, bytes_before = cached.stat().st_mtime_ns, cached.read_bytes()
    # Nothing has changed yet, so nothing is due: the baseline the next request is measured
    # against is a cache hit, not a first request.
    assert client.post(
        f"/api/projects/{project_id}/shots/shot_a/preview"
    ).json()["rendered"] is False

    definition = EFFECT_CATALOGUE["scanlines"]

    def one_pixel_origin(values, context):
        """The `7db970c` spelling, rebuilt from the shipped stage: `drawgrid=x=-1:…`."""
        return tuple(
            "drawgrid=x=-1:" + stage.split(":", 1)[1]
            for stage in definition.compose(values, context)
        )

    monkeypatch.setitem(
        EFFECT_CATALOGUE, "scanlines", dataclasses.replace(definition, compose=one_pixel_origin)
    )

    after = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")

    assert after.status_code == 200, after.text
    assert after.json()["fingerprint"] != first.json()["fingerprint"]
    assert after.json()["rendered"] is True
    assert (media / after.json()["preview"]).is_file()
    # And the clip that named the old chain is untouched and unserved, which is what makes a
    # stale entry cost disk and nothing else.
    assert cached.stat().st_mtime_ns == stamp
    assert cached.read_bytes() == bytes_before
    assert len(list((media / "previews").glob("*.mp4"))) == 2


def test_a_corrected_catalogue_default_makes_the_cached_clip_due_again(
    tmp_path: Path, monkeypatch
):
    """The second half of F2, and the one the storage rule argues for out loud: a stack is
    stored sparsely so *"a corrected default"* can reach the projects that would benefit from
    it. It reached the export and not the preview — the manifest holds no `strength` to move, so
    a name taken from the manifest could not move. It moves now."""
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, tmp_path)

    written = client.put(
        f"/api/projects/{project_id}/shots/shot_a/effects",
        json={"effects": [{"effect": "grain", "parameters": {}}]},
    )
    assert written.status_code == 200, written.text
    # The card is stored with nothing in it. That is the point: there is no value here that a
    # corrected default could contradict.
    stored = client.get(f"/api/projects/{project_id}").json()["shots"][0]["effects"]
    assert [(spec["effect"], spec["parameters"]) for spec in stored] == [("grain", {})]

    first = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")
    assert first.status_code == 200, first.text

    definition = EFFECT_CATALOGUE["grain"]
    monkeypatch.setitem(
        EFFECT_CATALOGUE,
        "grain",
        dataclasses.replace(
            definition,
            parameters=tuple(
                dataclasses.replace(parameter, default=12.0)
                if parameter.name == "strength"
                else parameter
                for parameter in definition.parameters
            ),
        ),
    )

    after = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")

    assert after.status_code == 200, after.text
    assert after.json()["fingerprint"] != first.json()["fingerprint"]
    assert after.json()["rendered"] is True


def test_the_chain_the_route_renders_is_the_chain_it_names_the_clip_after(tmp_path: Path):
    """One request composes the chain twice — once for ffmpeg, once inside the fingerprint — and
    the two must be composed from **one** set of arguments or the name stops describing the
    picture, which is the defect this slice closes rather than a new spelling of it.

    Both call sites are watched, because they are separate name lookups: `app` holds its own
    reference to `build_effect_stages` and `effects` calls its module global from inside
    `preview_fingerprint`. A later epic that gives the preview a `clip_offset`, a different
    geometry or a different `luts` in one place and not the other fails here.

    **The stack carries a pixel-denominated card**, because that is the argument this route was
    getting wrong: `reference_width` is the *export's* width, not the preview's, and without it
    Soft Focus, Sharpen, Bloom, Pixelate and Pixel Shuffle all render at twice their relative
    size in the Monitor while the stage text stays byte-identical."""
    from music_video_producer import app as app_module
    from music_video_producer import effects as effects_module

    client, _store, _comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, tmp_path)
    written = client.put(
        f"/api/projects/{project_id}/shots/shot_a/effects",
        json={
            "effects": [
                {"effect": "saturation", "parameters": {"amount": 1.4}},
                {"effect": "pixelate", "parameters": {"size": 16}},
            ]
        },
    )
    assert written.status_code == 200, written.text

    composed = effects_module.build_effect_stages
    calls = []

    def watched(stack, **kwargs):
        calls.append((list(stack), kwargs))
        return composed(stack, **kwargs)

    app_module.build_effect_stages = watched
    effects_module.build_effect_stages = watched
    try:
        response = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")
    finally:
        app_module.build_effect_stages = composed
        effects_module.build_effect_stages = composed

    assert response.status_code == 200, response.text
    assert len(calls) == 2, calls
    assert calls[0] == calls[1]
    # And the arguments are the ones this route is entitled to: the preview's own geometry, the
    # Shot from its own first frame, and the Shot's own window as the span a ramp is measured
    # against.
    _stack, kwargs = calls[0]
    assert (kwargs["width"], kwargs["height"]) == (
        response.json()["width"], response.json()["height"]
    )
    assert kwargs["clip_offset"] == 0.0
    assert kwargs["shot_seconds"] == 4.0
    # And the grid the stack's pixel counts were written for is the **export's**, which is the
    # one argument here that is deliberately not the preview's own. Asserted as the relationship
    # the route promises rather than as a number: the preview's width is `preview_side` of this,
    # so passing the preview's own width, or nothing at all, fails.
    reference = kwargs["reference_width"]
    half = reference // 2
    assert kwargs["width"] == max(2, half - (half % 2))
    assert reference > kwargs["width"]


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



# ------------------------------------------------------------------------------------------
# The boundary preview (story 11.5, FX-21, R-35), and the one-sided treatment on a Shot's own
# preview (the seventh fingerprint slot). Both driven with real ffmpeg over synthesized media,
# and both asserted on the **decoded artefact** rather than on the exit code: five wrong outputs
# at rc 0 across three epics, three of them in Epic 11 alone.
# ------------------------------------------------------------------------------------------


def frame_pixel(path: Path, index: int) -> tuple[int, int, int]:
    """One RGB sample from the top-left of frame `index` of a clip, decoded.

    `select=eq(n,index)` rather than a seek: these clips are a second and a half long, a seek
    lands on a keyframe, and the whole point of reading a specific frame here is to say what the
    picture is doing *at* that frame of the blend.
    """
    width, height = (int(part) for part in probe(path, "stream=width,height").split(","))
    raw = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", path.as_posix(),
            "-vf", f"select=eq(n\\,{index})", "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        check=True, capture_output=True,
    ).stdout
    assert len(raw) >= width * height * 3, (path, index, len(raw))
    return tuple(raw[:3])


def project_with_an_overlapping_pair(client, tmp_path: Path, *, overlap: float = 0.5):
    """An 8 s song tiled by two approved takes that **overlap**, which is what a transition needs.

    `shot_a` runs 0 → 4 s and `shot_b` starts `overlap` seconds before that, running to the song's
    end — so the plan tiles the whole song, the export will actually assemble, and the boundary is
    a real Overlap rather than a hand-written field. The trailing lead is computed from
    `over_render_lead` rather than typed, exactly as the sibling fixture computes it, so the
    fixture cannot drift from the rule that produces it.

    **The two takes are different colours on purpose.** A blend between two identical pictures is
    indistinguishable from no blend at all, and this file has already paid once for a fixture that
    did not contain the thing under test.
    """
    project_id = client.post("/api/projects", json={"name": "Boundary"}).json()["id"]
    upload = client.post(
        f"/api/projects/{project_id}/songs/upload",
        data={"title": "Boundary Song", "duration": "0"},
        files={"file": ("song.wav", wav_bytes(8.0), "audio/wav")},
    )
    assert upload.status_code == 200, upload.text
    shots_dir = tmp_path / "comfy" / "output" / "music-video-producer" / project_id / "shots"
    b_start = 4.0 - overlap
    b_duration = 8.0 - b_start
    first_seconds = over_render_frames(4.0) / ASSEMBLY_FPS
    second_seconds = over_render_frames(b_duration) / ASSEMBLY_FPS
    synthesize_take(shots_dir / "shot_a-h3_00001-audio.mp4", first_seconds, "128x72", "red")
    synthesize_take(shots_dir / "shot_b-h3_00001-audio.mp4", second_seconds, "128x72", "blue")
    trailing_lead = over_render_lead(
        start=b_start,
        duration=b_duration,
        picture_seconds=second_seconds,
        song_duration=8.0,
    )
    prefix = f"music-video-producer/{project_id}/shots"
    shots = [
        {
            "id": "shot_a", "start": 0.0, "duration": 4.0, "prompt": "Red room",
            "status": "complete",
            "latest_output": f"{prefix}/shot_a-h3_00001-audio.mp4",
        },
        {
            "id": "shot_b", "start": b_start, "duration": b_duration, "prompt": "Blue room",
            "status": "complete",
            "latest_output": f"{prefix}/shot_b-h3_00001-audio.mp4",
            "latest_take_lead": trailing_lead,
        },
    ]
    saved = client.put(f"/api/projects/{project_id}/shots", json={"shots": shots})
    assert saved.status_code == 200, saved.text
    for shot_id in ("shot_a", "shot_b"):
        approved = client.post(f"/api/projects/{project_id}/shots/{shot_id}/approve")
        assert approved.status_code == 200, approved.text
    return project_id, shots_dir


def set_transition(client, project_id: str, shot_id: str, kind: str | None):
    written = client.put(
        f"/api/projects/{project_id}/shots/{shot_id}/transitions",
        json={"transition_out": {"type": kind} if kind else None},
    )
    return written



def store_transition(store, project_id: str, shot_id: str, kind: str):
    """One `transition_out` written straight into the manifest, past every route.

    Two states this file needs are unreachable through `PUT .../transitions` by design: an unknown
    type, which the catalogue refuses at the write, and a pair-only type on a boundary with no
    Overlap, which FX-19 refuses there. Both are reachable in a real project -- a hand edit, a
    manifest from a newer build, or simply dragging two clips apart after authoring a wipe across
    them (FX-16, R-36) -- and AD-21 is the rule that says a stored value is asked about again
    rather than trusted, so they have to be testable.
    """
    project, _generation = store.read_for_update(project_id)
    for shot in project.shots:
        if shot.id == shot_id:
            shot.transition_out = TransitionSpec.model_construct(type=kind)
    store.save(project)

def test_a_boundary_preview_spans_the_cut_and_the_blend_plays_between_the_two_shots(
    tmp_path: Path,
):
    """FX-21 and story 11.5's first acceptance criterion, measured on the decoded file.

    **One clip, and the blend plays continuously between the two Shots.** The window is the
    outgoing Shot's last frames, the transition, and the incoming Shot's first frames — so the
    assertions walk it: pure red before the blend, a mixture inside it, pure blue after it.

    **Every one of these is on the artefact, not the exit code.** `xfade` truncating to its shorter
    leg is rc 0 and silent, so the frames are *counted* out of the decoded stream rather than read
    off the container; `xfade` emitting `yuv444p` that a `concat -c copy` then mislabels is rc 0
    too, so the pixel format is probed; and the whole point of the clip is a picture, so the
    picture is sampled.
    """
    client, _store, comfy, _app = make_client(tmp_path)
    project_id, _shots = project_with_an_overlapping_pair(client, tmp_path)
    assert set_transition(client, project_id, "shot_a", "dissolve").status_code == 200

    answer = client.post(f"/api/projects/{project_id}/shots/shot_a/boundary-preview")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["shot_id"] == "shot_a" and body["after_shot_id"] == "shot_b"
    assert body["transition"] == "dissolve"
    assert body["rendered"] is True
    # The three parts of one clip: half a second of each Shot around a half-second blend.
    assert (body["lead_frames"], body["blend_frames"], body["tail_frames"]) == (12, 12, 12)
    assert body["frames"] == 36 and body["window_seconds"] == pytest.approx(1.5)
    assert body["transition_seconds"] == pytest.approx(0.5)

    clip = tmp_path / "projects" / project_id / "media" / body["preview"]
    assert clip.is_file()
    # **The decoded count, never the header.** A leg that came up short is rc 0 with a correct
    # container; this is the only assertion that can see it.
    assert counted_frames(clip) == 36
    # Half the export's geometry (AD-29), and the pixel format every intermediate carries.
    assert probe(clip, "stream=width,height") == "64,36"
    assert probe(clip, "stream=pix_fmt") == "yuv420p"

    # The picture itself, walked across the boundary.
    red_before = frame_pixel(clip, 0)
    still_red = frame_pixel(clip, body["lead_frames"] - 1)
    middle = frame_pixel(clip, body["lead_frames"] + body["blend_frames"] // 2)
    blue_after = frame_pixel(clip, body["lead_frames"] + body["blend_frames"])
    still_blue = frame_pixel(clip, 35)
    assert red_before[0] > 200 and red_before[2] < 40, red_before
    assert still_red[0] > 200 and still_red[2] < 40, still_red
    assert 60 < middle[0] < 200 and 60 < middle[2] < 200, (
        "the middle of the blend must be a mixture of the two shots, not either of them", middle
    )
    assert blue_after[2] > 200 and blue_after[0] < 40, blue_after
    assert still_blue[2] > 200 and still_blue[0] < 40, still_blue
    # Nothing went near ComfyUI, and nothing was written to the manifest.
    assert comfy.prompts == []


def test_the_previewed_transition_is_the_exports_own_by_name_and_duration(tmp_path: Path):
    """FX-NFR-3, and story 11.5's second acceptance criterion — **proved by string against the
    export's own composed graph** rather than by inspection.

    Both command lines are captured where this application actually starts a process, so what is
    compared is what ffmpeg was really handed on each path: the export's segment argv from
    `POST /assemble`, and the boundary preview's from its own route. The `xfade` clause — the
    transition's name and its duration — must be character for character the same.

    What is allowed to differ is named too, so the test says what it is *not* claiming: the offset
    (where the blend sits in the window), the geometry (half), the preset (`ultrafast` CRF 28), and
    the trims (the preview's legs are longer on their own sides). A test that asserted the two argv
    equal would be asserting the preview is the export, which it is not and must not be.
    """
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id, _shots = project_with_an_overlapping_pair(client, tmp_path)
    assert set_transition(client, project_id, "shot_a", "fade_black").status_code == 200

    started: list[list[str]] = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        started.append([str(arg) for arg in args])
        return await real(*args, **kwargs)

    graphs: dict[str, str] = {}
    import music_video_producer.app as app_module

    for name, request in (
        ("export", lambda: client.post(f"/api/projects/{project_id}/assemble")),
        ("preview", lambda: client.post(
            f"/api/projects/{project_id}/shots/shot_a/boundary-preview")),
    ):
        started.clear()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(asyncio, "create_subprocess_exec", spy)
            answer = request()
        assert answer.status_code == 200, (name, answer.text)
        complex_argvs = [argv for argv in started if "-filter_complex" in argv]
        assert len(complex_argvs) == 1, (name, "exactly one two-input graph runs on each path")
        graphs[name] = complex_argvs[0][complex_argvs[0].index("-filter_complex") + 1]

    clause = lambda graph: graph.split("xfade=")[1].split(":offset=")[0]
    assert clause(graphs["export"]) == clause(graphs["preview"]), (
        "the previewed transition is not the export's", graphs
    )
    assert clause(graphs["export"]) == "transition=fadeblack:duration=0.500000"
    # And they really are two different clips, so this is not one graph compared with itself.
    assert graphs["export"] != graphs["preview"]
    assert ":offset=0," in graphs["export"] and ":offset=0.500000," in graphs["preview"]
    assert app_module.PREVIEW_PRESET.crf == "28"


def test_a_boundary_with_nothing_to_blend_says_which_absence_it_is(tmp_path: Path):
    """Story 11.5's last acceptance criterion, at the route: four states with no blend, four
    different sentences, and never a 200 carrying a picture of a hard cut.

    A control that simply did nothing here reads as a fault. Each sentence names the state and the
    gesture that leaves it — which is this application's own rule about a refusal being worth
    saying once.
    """
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id, _shots = project_with_an_overlapping_pair(client, tmp_path)

    said: dict[str, str] = {}
    # An Overlap with no type chosen: a hard cut (UX-DR8), and the state a Director is most
    # likely to be in at the moment they want to look.
    untyped = client.post(f"/api/projects/{project_id}/shots/shot_a/boundary-preview")
    assert untyped.status_code == 422, untyped.text
    said["untyped"] = untyped.json()["detail"]

    # Nothing after the last Shot.
    assert set_transition(client, project_id, "shot_b", "dissolve").status_code == 200
    last = client.post(f"/api/projects/{project_id}/shots/shot_b/boundary-preview")
    assert last.status_code == 422, last.text
    said["last"] = last.json()["detail"]

    # A type stored on a boundary the Director then dragged apart (FX-16, R-36): one-sided, and
    # the Shot's *own* preview is the picture of it — which the sentence says.
    assert set_transition(client, project_id, "shot_a", "dissolve").status_code == 200
    project = client.get(f"/api/projects/{project_id}").json()
    for shot in project["shots"]:
        if shot["id"] == "shot_b":
            shot["start"] = 4.0
            shot["duration"] = 4.0
    assert client.put(
        f"/api/projects/{project_id}/shots", json={"shots": project["shots"]}
    ).status_code == 200
    apart = client.post(f"/api/projects/{project_id}/shots/shot_a/boundary-preview")
    assert apart.status_code == 422, apart.text
    said["apart"] = apart.json()["detail"]

    # A Shot this project does not hold at all is still a 404 rather than a sentence.
    assert client.post(
        f"/api/projects/{project_id}/shots/nobody/boundary-preview"
    ).status_code == 404

    assert len(set(said.values())) == 3, said
    assert "no transition is set" in said["untyped"]
    assert "last shot in the song" in said["last"]
    assert "do not overlap" in said["apart"]
    # Every one of them names the Shots the way a refusal in this application names one, so the
    # sentence can be read beside a timeline.
    assert all("SHOT 01 (shot_a)" in line or "SHOT 02 (shot_b)" in line for line in said.values())


def test_an_unknown_stored_type_refuses_the_boundary_preview_in_the_exports_own_words(
    tmp_path: Path,
):
    """AD-21: nothing stored says a transition is valid, and a manifest is hand-editable.

    The export refuses by name at its plan stage (`_transition_catalogue_refusals`), so a preview
    that quietly rendered the untreated picture would be predicting an export that will not run —
    which is the one thing a preview must never do. Same sentence, same catalogue, one wording.
    """
    from music_video_producer.effects import TRANSITION_UNKNOWN_REFUSAL

    client, store, _comfy, _app = make_client(tmp_path)
    project_id, _shots = project_with_an_overlapping_pair(client, tmp_path)
    # Written past the route on purpose: `replace_shot_transitions` asks the catalogue before it
    # stores a byte, so a hand-edited manifest is the only way to reach this state -- which is
    # exactly why AD-21 has the export ask again rather than trust what is stored.
    store_transition(store, project_id, "shot_a", "ripple")

    answer = client.post(f"/api/projects/{project_id}/shots/shot_a/boundary-preview")
    assert answer.status_code == 422, answer.text
    detail = answer.json()["detail"]
    assert TRANSITION_UNKNOWN_REFUSAL.format(transition="ripple", known="") .split("The")[0] in detail
    assert "ripple" in detail and "SHOT 01 (shot_a)" in detail


def test_dragging_the_overlap_longer_moves_the_previewed_blend_and_names_a_new_clip(
    tmp_path: Path,
):
    """Story 11.5's fourth acceptance criterion and its fifth constraint, at the route.

    The Overlap **is** the transition's duration (AD-19), so dragging it has to move the blend —
    and it has to move the *name*, or a Director would drag the clips and be served the previous
    picture out of the cache for ever, because nothing in this application evicts `previews/`.

    The row's readout follows the identical subtraction and is asserted in
    `test_the_overlaps_length_and_the_rows_readout_are_one_number`; what is asserted here is that
    the number the render is built from is that same one.
    """
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id, _shots = project_with_an_overlapping_pair(client, tmp_path, overlap=0.5)
    assert set_transition(client, project_id, "shot_a", "dissolve").status_code == 200
    half = client.post(f"/api/projects/{project_id}/shots/shot_a/boundary-preview").json()

    project = client.get(f"/api/projects/{project_id}").json()
    for shot in project["shots"]:
        if shot["id"] == "shot_b":
            shot["start"] = 3.0
            shot["duration"] = 5.0
    assert client.put(
        f"/api/projects/{project_id}/shots", json={"shots": project["shots"]}
    ).status_code == 200
    longer = client.post(f"/api/projects/{project_id}/shots/shot_a/boundary-preview").json()

    assert half["blend_frames"] == 12 and half["transition_seconds"] == pytest.approx(0.5)
    assert longer["blend_frames"] == 24 and longer["transition_seconds"] == pytest.approx(1.0)
    assert longer["fingerprint"] != half["fingerprint"], (
        "a longer Overlap is a different picture and must be a different clip"
    )
    assert longer["frames"] == 48 and longer["rendered"] is True
    clip = tmp_path / "projects" / project_id / "media" / longer["preview"]
    assert counted_frames(clip) == 48


def test_a_longer_outgoing_shot_moves_the_blend_without_moving_where_it_starts(tmp_path: Path):
    """The window slot's three frame counts, isolated — and this test exists because a mutation
    survived without it.

    `test_dragging_the_overlap_longer...` moves the **incoming** Shot, which moves the Overlap's
    start as well as its length — so the fingerprint moved on `window_start` alone and the three
    frame counts beside it were never load-bearing. Dropping them from the payload left every test
    green. Dragging the **outgoing** Shot's right edge instead is the same gesture from the other
    side: the Overlap starts at the same second and is twice as long, so `window_start` cannot
    explain the difference and the blend count has to.

    The lead is asserted with it because it moves under the identical rule: the frames of the
    outgoing Shot before the blend are fewer once the blend has eaten into them.
    """
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id, _shots = project_with_an_overlapping_pair(client, tmp_path, overlap=0.5)
    assert set_transition(client, project_id, "shot_a", "dissolve").status_code == 200
    half = client.post(f"/api/projects/{project_id}/shots/shot_a/boundary-preview").json()

    project = client.get(f"/api/projects/{project_id}").json()
    for shot in project["shots"]:
        if shot["id"] == "shot_a":
            shot["duration"] = 4.5
    assert client.put(
        f"/api/projects/{project_id}/shots", json={"shots": project["shots"]}
    ).status_code == 200
    longer = client.post(f"/api/projects/{project_id}/shots/shot_a/boundary-preview").json()

    assert half["blend_frames"] == 12 and longer["blend_frames"] == 24
    # The Overlap begins at the same second in both, which is what makes this the isolated case.
    assert half["lead_frames"] == 12 and longer["lead_frames"] == 12
    assert longer["fingerprint"] != half["fingerprint"], (
        "the blend doubled in length and the clip kept its name, so the cache serves the old one"
    )
    assert longer["frames"] == 48
    clip = tmp_path / "projects" / project_id / "media" / longer["preview"]
    assert counted_frames(clip) == 48


def test_an_off_grid_overlap_blends_for_the_length_the_row_states(tmp_path: Path):
    """The ruled number, end to end on the artefact: an Overlap of 0.51 s blends for 0.50 s.

    A Director drags freehand, so an Overlap is a float and a blend is that float in frames. The
    row was made to state the second one on 2026-08-30 — *the row shows what renders* — and this is
    the other end of that claim: what the route serves as `transition_seconds`, what it puts in
    `xfade`'s `duration=`, and what ffmpeg actually writes are one number, and it is the number on
    screen.

    Asserted on the **decoded** clip, not the response: `xfade` truncating to its shorter leg is
    rc 0 and silent, so a `blend_frames` field agreeing with a readout would prove nothing about
    the picture if the file came up short.
    """
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id, _shots = project_with_an_overlapping_pair(client, tmp_path, overlap=0.51)
    assert set_transition(client, project_id, "shot_a", "dissolve").status_code == 200
    body = client.post(f"/api/projects/{project_id}/shots/shot_a/boundary-preview").json()

    # 0.51 s at 24 fps is twelve frames and a fraction, and the grid keeps the twelve.
    assert body["blend_frames"] == 12
    assert body["transition_seconds"] == pytest.approx(0.5)
    assert clip_frames_on_grid(4.0 - 0.51, 4.0) == 12
    clip = tmp_path / "projects" / project_id / "media" / body["preview"]
    assert counted_frames(clip) == body["frames"] == 36
    # And the graph ffmpeg was handed carries the same half-second, not the 0.51 that was dragged.
    started: list[list[str]] = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        started.append([str(arg) for arg in args])
        return await real(*args, **kwargs)

    for path in (tmp_path / "projects" / project_id / "media" / "previews").glob("*.mp4"):
        path.unlink()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(asyncio, "create_subprocess_exec", spy)
        again = client.post(f"/api/projects/{project_id}/shots/shot_a/boundary-preview")
    assert again.status_code == 200, again.text
    graph = next(argv for argv in started if "-filter_complex" in argv)
    graph = graph[graph.index("-filter_complex") + 1]
    assert "xfade=transition=fade:duration=0.500000" in graph, graph


def test_a_boundary_preview_is_served_from_the_cache_and_renamed_when_the_type_changes(
    tmp_path: Path,
):
    """AD-23 for the second subject: the fingerprint names a file, and the file is either there or
    it is not.

    The type is the seventh input of `BOUNDARY_FINGERPRINT_INPUTS` and it is hashed with the
    `xfade` name beside it, so choosing a different transition is a different clip. An unchanged
    request re-renders nothing, which is the property `rendered` exists to make observable.
    """
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id, _shots = project_with_an_overlapping_pair(client, tmp_path)
    assert set_transition(client, project_id, "shot_a", "dissolve").status_code == 200
    first = client.post(f"/api/projects/{project_id}/shots/shot_a/boundary-preview").json()
    again = client.post(f"/api/projects/{project_id}/shots/shot_a/boundary-preview").json()
    assert again["fingerprint"] == first["fingerprint"]
    assert again["rendered"] is False, "an unchanged request must re-render nothing"

    assert set_transition(client, project_id, "shot_a", "wipe_left").status_code == 200
    wiped = client.post(f"/api/projects/{project_id}/shots/shot_a/boundary-preview").json()
    assert wiped["fingerprint"] != first["fingerprint"]
    assert wiped["rendered"] is True and wiped["transition"] == "wipe_left"
    # Both clips are on disk; the older one is inert rather than wrong (AD-23).
    previews = tmp_path / "projects" / project_id / "media" / "previews"
    assert {path.stem for path in previews.glob("*.mp4")} >= {
        first["fingerprint"], wiped["fingerprint"]
    }


def test_a_boundary_preview_renders_at_the_same_grid_the_shot_preview_does(tmp_path: Path):
    """AD-29 across the two subjects: preview geometry is a fact about the **project**.

    The boundary route asks `assembly_plan` for a plan *with* transitions in it and the Shot route
    asks for one without, and both take half of `plan.width`/`plan.height`. That is only one grid
    while the transition pass cannot move it — and it cannot, structurally: `_paired_transitions`
    runs after the resolution loop and the normalization target is taken from `resolved`, which it
    does not touch (R-39's "the grid is what stays"). Asserted rather than reasoned, because a
    preview at a different grid from the export's is FX-NFR-3 broken in the one way a Director
    cannot see: the picture looks right and the letterbox is wrong.
    """
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id, _shots = project_with_an_overlapping_pair(client, tmp_path)
    shot = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")
    assert shot.status_code == 200, shot.text
    assert set_transition(client, project_id, "shot_a", "dissolve").status_code == 200
    boundary = client.post(f"/api/projects/{project_id}/shots/shot_a/boundary-preview")
    assert boundary.status_code == 200, boundary.text
    assert (boundary.json()["width"], boundary.json()["height"]) == (
        shot.json()["width"], shot.json()["height"]
    )
    # And the Shot's own preview is unmoved by the transition existing: same grid, same clip.
    again = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")
    assert again.json()["fingerprint"] == shot.json()["fingerprint"]


def test_a_boundary_preview_writes_nothing_to_the_manifest(tmp_path: Path):
    """AD-23's load-bearing absence, for the route this story adds. No stored stale flag, no
    cached geometry, no record that a preview exists — so a state that is derived cannot outlive
    the thing it describes."""
    client, store, _comfy, _app = make_client(tmp_path)
    project_id, _shots = project_with_an_overlapping_pair(client, tmp_path)
    assert set_transition(client, project_id, "shot_a", "dissolve").status_code == 200
    before = (store.project_dir(project_id) / "project.json").read_bytes()
    assert client.post(
        f"/api/projects/{project_id}/shots/shot_a/boundary-preview"
    ).status_code == 200
    assert (store.project_dir(project_id) / "project.json").read_bytes() == before


def test_both_legs_of_a_boundary_compose_their_own_shots_effects_in_their_own_namespace(
    tmp_path: Path,
):
    """R-41, on the graph the render was actually handed.

    Two graded Shots must blend their **graded** pictures, or the segment would not match the clips
    on either side of it (FX-NFR-3). And both legs start at chain slot 0, so without a leg prefix
    two branched Shots emit the same filtergraph link label twice in one `-filter_complex` — which
    is at least loud — and two *bound* Shots emit one `sendcmd` target driving both legs, which is
    silent at rc 0.

    The fixture carries a real effect on each leg for the reason this file has learned twice: a
    fixture that does not contain the thing under test proves nothing about it.
    """
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id, _shots = project_with_an_overlapping_pair(client, tmp_path)
    assert set_transition(client, project_id, "shot_a", "dissolve").status_code == 200
    # **A branching effect on each leg, which is what the fixture has to contain.** Bloom composes
    # a `split`/treat/`blend` filtergraph with its own link labels, and those labels are the thing
    # R-41 is about; a flat stage would leave the claim untested and the test green. Two different
    # intensities, so a leg composed from the wrong Shot's stack is visible rather than inferred.
    for shot_id, intensity in (("shot_a", 0.6), ("shot_b", 0.3)):
        written = client.put(
            f"/api/projects/{project_id}/shots/{shot_id}/effects",
            json={"effects": [{
                "effect": "bloom",
                "parameters": {"intensity": intensity, "radius": 8, "threshold": 0.5},
            }]},
        )
        assert written.status_code == 200, written.text

    started: list[list[str]] = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        started.append([str(arg) for arg in args])
        return await real(*args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(asyncio, "create_subprocess_exec", spy)
        answer = client.post(f"/api/projects/{project_id}/shots/shot_a/boundary-preview")
    assert answer.status_code == 200, answer.text
    graph = next(argv for argv in started if "-filter_complex" in argv)
    graph = graph[graph.index("-filter_complex") + 1]
    # Each leg carries its **own** Shot's value, so neither is a picture of the other.
    outgoing, incoming = graph.split("[xfa];")[0], graph.split("[xfa];")[1]
    assert "all_opacity=0.6" in outgoing and "all_opacity=0.3" in incoming, graph
    # And the branch labels are in a per-leg namespace (R-41). Both legs start at chain slot 0, so
    # without the prefix each of these would appear twice in one `-filter_complex`.
    for label in ("fxA0a", "fxA0b", "fxA0c", "fxB0a", "fxB0b", "fxB0c"):
        assert graph.count("[" + label + "]") == 2, (label, "a link label is written and read once")
    assert "fxA" in outgoing and "fxB" not in outgoing, outgoing
    assert "fxB" in incoming and "fxA" not in incoming, incoming
    # And a clip really came out of it, at the right length, rather than a graph that only reads
    # correctly.
    body = answer.json()
    clip = tmp_path / "projects" / project_id / "media" / body["preview"]
    assert counted_frames(clip) == body["frames"] == 36


# ------------------------------------------------------------------------------------------
# The smaller half: a one-sided transition on a Shot's own preview, through the seventh
# fingerprint slot (R-35). It was the one thing an export did that a preview did not.
# ------------------------------------------------------------------------------------------


def test_a_one_sided_transition_previews_the_treated_frames_and_the_cut(tmp_path: Path):
    """Story 11.5's third acceptance criterion, measured on the decoded picture.

    A Shot with a transition and no Overlap treats its own last frames and then hard-cuts (FX-18,
    story 11.4). `dip_black` reaches the colour at the midpoint of the treatment and **holds** it
    to the cut, which is the measurement `ONE_SIDED_FORMS` records — so the assertions are: the
    picture untouched before the treatment starts, black by its midpoint, and still black on the
    very last frame, where the clip simply ends.
    """
    from music_video_producer.effects import ONE_SIDED_TRANSITION_FRAMES

    client, _store, _comfy, _app = make_client(tmp_path)
    project_id, _shots = project_with_two_approved_takes(client, tmp_path)
    # `shot_b` is the last Shot in the song, so its `transition_out` has no Overlap under it.
    assert set_transition(client, project_id, "shot_b", "fade_black").status_code == 200

    answer = client.post(f"/api/projects/{project_id}/shots/shot_b/preview")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    clip = tmp_path / "projects" / project_id / "media" / body["preview"]
    frames = body["frames"]
    assert counted_frames(clip) == frames, (
        "a treatment must consume no timeline length and change no frame count (FX-NFR-1)"
    )
    start = frames - ONE_SIDED_TRANSITION_FRAMES
    untouched = frame_pixel(clip, start - 1)
    midpoint = frame_pixel(clip, start + ONE_SIDED_TRANSITION_FRAMES // 2)
    last = frame_pixel(clip, frames - 1)
    assert untouched[2] > 200, ("the frames before the treatment are the Shot's own", untouched)
    assert max(midpoint) < 20, ("dip_black reaches the colour at the midpoint", midpoint)
    assert max(last) < 20, ("and holds it to the cut", last)


def test_a_one_sided_transition_moves_the_shots_preview_fingerprint_and_nothing_else_does(
    tmp_path: Path,
):
    """The seventh slot, filled — and story 11.5's constraint that no existing preview is renamed.

    Four states of one Shot are keyed here and the divisions are the whole claim. **No transition**
    and **a pair-only type with no Overlap** are one picture, because a wipe with nothing to wipe
    onto composes nothing and the export renders the clip untreated (FX-19, R-34) — so they must be
    one clip. **A one-sided type** is a different picture and a different name. And a **paired**
    transition leaves the Shot's own clip alone entirely: the blend is a `TransitionClip` of its
    own, so this Shot's preview must go back to the name it had before.
    """
    client, store, _comfy, _app = make_client(tmp_path)
    project_id, _shots = project_with_an_overlapping_pair(client, tmp_path)
    # No Overlap under `shot_b`'s outgoing boundary: it is the last Shot in the song.
    named: dict[str, str] = {}
    for state, kind in (
        ("none", None), ("pair-only", "wipe_left"), ("one-sided", "fade_black"),
    ):
        # `wipe_left` is written past the route deliberately: the write refuses a pair-only type
        # on a boundary with no Overlap (FX-19), and FX-16/R-36's own case -- a Director dragging
        # the two clips apart afterwards -- reaches the identical stored state with no refusal to
        # go through. That is the state being keyed here.
        if kind == "wipe_left":
            store_transition(store, project_id, "shot_b", kind)
        else:
            assert set_transition(client, project_id, "shot_b", kind).status_code == 200
        answer = client.post(f"/api/projects/{project_id}/shots/shot_b/preview")
        assert answer.status_code == 200, (state, answer.text)
        named[state] = answer.json()["fingerprint"]
    assert named["none"] == named["pair-only"], (
        "a wipe with nothing to wipe onto composes nothing, so it is the clip that was there"
    )
    assert named["one-sided"] != named["none"], (
        "a treatment that changes the picture must change the name, or the cache serves the old one"
    )

    # And the *outgoing* Shot of a real Overlap: its own preview is untouched by the pair.
    plain = client.post(f"/api/projects/{project_id}/shots/shot_a/preview").json()["fingerprint"]
    assert set_transition(client, project_id, "shot_a", "dissolve").status_code == 200
    paired = client.post(f"/api/projects/{project_id}/shots/shot_a/preview").json()["fingerprint"]
    assert paired == plain, (
        "a paired transition is its own clip; the outgoing Shot's own preview did not change"
    )


def test_a_shot_with_no_transition_is_named_exactly_as_it_was_at_3322ace():
    """Story 11.5's third constraint, on the function rather than on the route.

    The seventh fingerprint input was reserved and hashed empty, and filling it must rename
    nothing that was already cached — `previews/` is never evicted, so a payload that moved would
    orphan every clip in every project on this machine for pictures that did not change (R-20).

    **The two digests below were taken from `effects.py` at `3322ace`** and compared against
    today's, which is a comparison a test cannot make for itself without shelling out to git. They
    are pinned instead, and the pin is what fails if the payload ever moves.
    """
    from music_video_producer.effects import preview_fingerprint

    bare = preview_fingerprint(
        take="t.mp4", window_start=0.0, window_duration=1.0, offset=0.0, width=2, height=2
    )
    assert bare == "38b50d6884b62d5cbe7724fea84bf137665fdb88a453869437edf477fa1394a8"
    graded = {
        "take": "music-video-producer/p/shots/shot_a-h3_00001-audio.mp4",
        "window_start": 0.0, "window_duration": 4.0, "offset": 0.25,
        "stack": [{"effect": "monochrome", "parameters": {"amount": 1}}],
        "width": 64, "height": 36, "reference_width": 128,
    }
    assert preview_fingerprint(**graded) == (
        "8d46e0b13fe1d20dcd0b56a9bc1d9eddd5f9c9430029f4bb7e051234b7ba17e4"
    )
    # And passing the slot explicitly as absent is the same bytes as not passing it, which is what
    # "canonicalises to nothing when absent" has to mean.
    assert preview_fingerprint(**graded, transition=None) == preview_fingerprint(**graded)


def test_a_one_sided_blur_previews_at_the_previews_own_grid():
    """`ONE_SIDED_BLUR_SIGMA` is a count of pixels, so it scales — the same resolution-dependence
    the five pixel-denominated effect parameters carry, answered by the same arithmetic.

    An export names no reference and its ramp is the number it has always written; a preview at
    half the delivery grid halves it, or the Director would judge a blur twice as heavy as the one
    the export ships.
    """
    from music_video_producer.effects import (
        ONE_SIDED_BLUR_SIGMA,
        one_sided_transition_stages,
        pixel_scale,
    )

    export = one_sided_transition_stages("blur_wipe", clip_frames=24, fps=ASSEMBLY_FPS)
    preview = one_sided_transition_stages(
        "blur_wipe", clip_frames=24, fps=ASSEMBLY_FPS, width=480, reference_width=960
    )
    last = lambda ramp: float(ramp.scripts[0].text.strip().splitlines()[-1].split()[-1][:-1])
    assert last(export) == ONE_SIDED_BLUR_SIGMA
    assert last(preview) == ONE_SIDED_BLUR_SIGMA * 0.5
    assert pixel_scale(960, 0) == 1.0 and pixel_scale(960, 960) == 1.0
    # The two ramps are different scripts, so they cannot share a cached file.
    assert export.scripts[0].filename != preview.scripts[0].filename
    # And the `sendcmd` target is an `@label` the same call put in the chain — Epic 10's whole
    # discipline, met by a composition that is not `build_effect_stages`'.
    for ramp in (export, preview):
        assert any(ramp.scripts[0].target in stage for stage in ramp.treatment)

def wait_for_a_running_render(app, project_id: str, deadline: float = 20.0):
    """Block until this project's preview render has a live process attached to it.

    **Registered is not running, and the difference is a whole flaky test.** Until 2026-08-26
    this helper returned the moment `app.state.preview_renders[project_id]` *existed*, which is
    the line before the subprocess is spawned. Every race below then fired its second request
    and hoped the first render was still there to race — and a preview over these fixtures takes
    about 210 ms end to end, which is not reliably longer than one HTTP round trip on a loaded
    machine. Measured 2026-08-26 with a deliberate 250 ms pause in exactly the place the
    scheduler puts one: `superseded` stayed False, both requests answered 200, the supersede
    test failed, and the two join tests recorded `joiners == 0` with their second request served
    out of the cache. On an untouched tree the supersede test failed about one full run in two.

    `PreviewRender.process`, with `returncode is None`, is the fact the route already publishes
    for this: `run_tool` hands the live process to `record.attach` before its first await, so a
    record carrying an unexited process is a render that has genuinely started and has not yet
    ended. It is half the guarantee. The other half is `HeldRender`, which stops the render
    finishing at all until the test says so — because a process that is running when it is
    observed can still exit a microsecond later. Together they replace a hope with two facts.
    """
    limit = time.monotonic() + deadline
    while time.monotonic() < limit:
        record = app.state.preview_renders.get(project_id)
        process = getattr(record, "process", None)
        if record is not None and process is not None and process.returncode is None:
            return record
        time.sleep(0.002)
    raise AssertionError("no preview render was ever running")


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

    `replacement` swaps the argv of exactly those processes for another command, and is how
    every race in this section is sequenced rather than hoped for: a real ffmpeg over these
    fixtures is finished in about 210 ms, and a real ffmpeg refusing a bad take is finished in
    milliseconds, neither of which is reliably longer than the round trip the test is racing it
    against. Either a fixed argv, or a callable taking `(index, argv)` — the 1-based order in
    which this render was started, and the argv it was about to run — returning a replacement
    argv, or `None` to let that one run for real. Everything around it stays real: a real
    subprocess, a real return code, real stderr, the real `on_start`/`attach` handover, the real
    kill and the real route.
    """

    def __init__(self, monkeypatch, replacement=None):
        self.started: list[list[str]] = []
        self.replacement = replacement
        self._real = asyncio.create_subprocess_exec
        monkeypatch.setattr(asyncio, "create_subprocess_exec", self._spawn)

    async def _spawn(self, *args, **kwargs):
        last = str(args[-1]) if args else ""
        if "/previews/." in last and last.endswith(".mp4"):
            argv = [str(arg) for arg in args]
            self.started.append(argv)
            replacement = self.replacement
            if callable(replacement):
                replacement = replacement(len(self.started), argv)
            if replacement is not None:
                args = tuple(replacement)
        return await self._real(*args, **kwargs)

    @property
    def count(self) -> int:
        return len(self.started)


#: The held render, as a program. It writes a few bytes to the scratch file the render was
#: handed — so a superseded render has really landed something for the publish gate to throw
#: away, rather than "nothing was left behind" being true because nothing was ever written —
#: then waits for the gate file to appear and finally runs the argv it was given, passing that
#: command's exit code and its stderr straight through.
HELD_RENDER_SOURCE = """
import os, subprocess, sys, time
gate, scratch = sys.argv[1], sys.argv[2]
with open(scratch, "wb") as partial:
    partial.write(b"a partly written preview")
while not os.path.exists(gate):
    time.sleep(0.005)
sys.exit(subprocess.call(sys.argv[3:]))
"""


class HeldRender:
    """A `RenderSpy` replacement that holds the *first* preview render open until released.

    This is the fact these tests were missing. A race between a render and an HTTP request is
    only the race it claims to be if the render is still underway when the request lands, and
    nothing about a 210 ms ffmpeg guarantees that — the test thread only has to be descheduled
    once. So the first render is wrapped in a program that cannot finish until this object says
    so, and the tests that need it to finish say so only *after* asserting, off the record the
    route publishes, that the thing they came to observe has happened. A render that is never
    released is killed where it stands, which is precisely what a supersede is for.

    `then` replaces what runs once the gate opens; left out, the real ffmpeg argv runs, so a
    released render produces exactly the clip it always did. Only the first render is wrapped —
    the request that supersedes it, or the one that follows it, gets a real render of its own,
    which is what the process census and the clip on disk are asserted against.
    """

    def __init__(self, tmp_path: Path, *, then: list[str] | None = None):
        self.gate = tmp_path / "release-the-held-render"
        self.then = then

    def __call__(self, index: int, argv: list[str]) -> list[str] | None:
        if index != 1:
            return None
        return [
            sys.executable, "-c", HELD_RENDER_SOURCE,
            self.gate.as_posix(), argv[-1],
            *(self.then if self.then is not None else argv),
        ]

    def release(self) -> None:
        """Let the held render run to its end. Nothing else in these tests writes this file."""
        self.gate.write_bytes(b"")


def slow_stack(client, project_id: str, *, grain: float = 30.0):
    """A stack heavy enough to be worth cancelling, and a fingerprint input `grain` moves.

    It is **not** the thing that keeps the render in flight long enough to be raced — that was
    the belief this file was written under and it is wrong: measured 2026-08-26, the whole
    request takes about 210 ms, which a loaded test thread loses to often enough to have made
    the supersede test fail about one full run in two. `HeldRender` is what holds a render open
    now. This just makes the work real and gives `grain` something to change.
    """
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
    process census is asserted here too: two requests, two renders, nothing joined.

    The first render is a `HeldRender` and that is what makes this a test rather than a coin
    toss. It was neither until 2026-08-26: the second request was fired the instant the render
    was *registered* and had roughly 210 ms to arrive before the render finished on its own, at
    which point there was nothing in flight, both requests answered 200 and the test failed.
    Held, the render cannot end until it is killed — so the supersede is observed, never raced.
    Nothing else is faked: it is a real subprocess, really killed, and the bytes it wrote to its
    scratch file are really deleted by the publish gate."""
    client, _store, _comfy, app = make_client(tmp_path)
    project_id, _shots_dir, media = project_with_one_slow_shot(client, tmp_path)
    spy = RenderSpy(monkeypatch, replacement=HeldRender(tmp_path))

    first: dict = {}
    thread = threading.Thread(target=fire_preview, args=(client, project_id, first))
    thread.start()
    try:
        record = wait_for_a_running_render(app, project_id)
        superseded_fingerprint = record.fingerprint
        held = record.process
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
    # Cancelled, and cancelled by the kill rather than by ending on its own: the held render
    # would have run for as long as it was left alone, so a return code at all is the signal,
    # and one that is not zero is the signal that it did not get to finish.
    assert held.returncode not in (None, 0), held.returncode
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
    focus -- could never be shown anything.

    The render is held open until `wait_for_joiners` says the second request has actually
    attached, and only then released to finish for real. Without that hold the second request
    was racing a 210 ms ffmpeg: when it lost -- measured on a loaded machine -- the clip was
    already in the cache, the second request was served from it, `joiners` stayed 0 and
    `rendered` came back False. Those last two assertions are why that showed up as a failure
    instead of a green run asserting nothing, and the hold is why it no longer happens."""
    client, _store, _comfy, app = make_client(tmp_path)
    project_id, _shots_dir, media = project_with_one_slow_shot(client, tmp_path)
    held = HeldRender(tmp_path)
    spy = RenderSpy(monkeypatch, replacement=held)

    first: dict = {}
    second: dict = {}
    threads = [threading.Thread(target=fire_preview, args=(client, project_id, first))]
    threads[0].start()
    try:
        record = wait_for_a_running_render(app, project_id)
        threads.append(
            threading.Thread(target=fire_preview, args=(client, project_id, second))
        )
        threads[1].start()
        wait_for_joiners(app, project_id, 1)
        held.release()
    finally:
        for thread in threads:
            thread.join(timeout=60)

    assert first["status"] == 200, first
    assert second["status"] == 200, second
    # One render, asserted at the process, not inferred from a timing.
    assert spy.count == 1, spy.started
    assert record.joiners == 1
    assert second["body"]["fingerprint"] == first["body"]["fingerprint"] == record.fingerprint
    # Both were answered by a render that ran just now, so both say so.
    assert first["body"]["rendered"] is True
    assert second["body"]["rendered"] is True
    # And one clip on disk under that fingerprint, with no scratch file abandoned beside it.
    clip = media / second["body"]["preview"]
    assert clip.is_file()
    assert [path.name for path in sorted((media / "previews").iterdir())] == [clip.name]
    assert app.state.preview_renders == {}


def test_three_identical_requests_all_join_the_same_render(tmp_path: Path, monkeypatch):
    """Two joiners get the answer, and so do three. The count is the point: a join that only
    ever released the first waiter would pass the two-request test and strand the rest.

    Held for the same reason as the two-request case, and with more to lose: three requests have
    to reach the render before it ends, and `wait_for_joiners` is a deadline rather than a
    guarantee unless something is holding the render open for them to arrive at."""
    client, _store, _comfy, app = make_client(tmp_path)
    project_id, _shots_dir, media = project_with_one_slow_shot(client, tmp_path)
    held = HeldRender(tmp_path)
    spy = RenderSpy(monkeypatch, replacement=held)

    leader: dict = {}
    joined: list[dict] = [{}, {}, {}]
    threads = [threading.Thread(target=fire_preview, args=(client, project_id, leader))]
    threads[0].start()
    try:
        record = wait_for_a_running_render(app, project_id)
        for answer in joined:
            thread = threading.Thread(
                target=fire_preview, args=(client, project_id, answer)
            )
            threads.append(thread)
            thread.start()
        wait_for_joiners(app, project_id, 3)
        held.release()
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

    The render is replaced by a subprocess that fails only once the test has let it, because a
    real ffmpeg refusing a bad take returns in milliseconds and there would be no in-flight
    render to join. Until 2026-08-26 the replacement slept 1.5 s and hoped that was enough; it
    is now held on the same gate as its siblings and released after `wait_for_joiners` has
    established that the joiner is attached, so no duration is being trusted. Everything else is
    the real route, a real process, its real exit code and its real stderr."""
    client, _store, _comfy, app = make_client(tmp_path)
    project_id, _shots_dir, media = project_with_one_slow_shot(client, tmp_path)
    held = HeldRender(tmp_path, then=[
        sys.executable, "-c",
        "import sys; sys.stderr.write('the preview encoder gave up'); sys.exit(3)",
    ])
    spy = RenderSpy(monkeypatch, replacement=held)

    first: dict = {}
    second: dict = {}
    threads = [threading.Thread(target=fire_preview, args=(client, project_id, first))]
    threads[0].start()
    try:
        record = wait_for_a_running_render(app, project_id)
        threads.append(
            threading.Thread(target=fire_preview, args=(client, project_id, second))
        )
        threads[1].start()
        wait_for_joiners(app, project_id, 1)
        held.release()
    finally:
        for thread in threads:
            thread.join(timeout=60)

    assert first["status"] == 502, first
    assert second["status"] == 502, second
    # It really was a join -- one process for two failed answers.
    assert spy.count == 1, spy.started
    assert record.joiners == 1
    for detail in (first["body"]["detail"], second["body"]["detail"]):
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
    request could arrive before the second and the test would assert about two supersedes. It
    needs a render to still be there to join, though, which is what the `HeldRender` supplies —
    with a real 210 ms ffmpeg leading, a joiner that arrived late found the clip in the cache
    instead, and `wait_for_joiners` then failed on its deadline rather than on the claim. The
    leader is never released: the third request kills it, which is the point."""
    client, _store, _comfy, app = make_client(tmp_path)
    project_id, _shots_dir, media = project_with_one_slow_shot(client, tmp_path)
    spy = RenderSpy(monkeypatch, replacement=HeldRender(tmp_path))

    leader: dict = {}
    joiner: dict = {}
    threads = [threading.Thread(target=fire_preview, args=(client, project_id, leader))]
    threads[0].start()
    try:
        record = wait_for_a_running_render(app, project_id)
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


def test_a_cut_that_runs_off_the_end_of_its_take_is_refused_in_the_exports_own_words(
    tmp_path: Path,
):
    """The window asks for four seconds the take does not hold, so there is no picture to show.

    Until 2026-08-26 this route computed `frames` from the Shot's window and rendered whatever
    the take happened to hold. ffmpeg returns 0 for that — `-frames:v` is a cap, not a demand —
    so the response came back 200 saying `frames: 96` and `window_seconds: 4.0` over a file that
    held **94** frames at this nudge, and 76 at a two-second one. Worse than being wrong once:
    the short clip was published under the look's fingerprint, so every later request for the
    same look was served it from the cache without re-rendering, and the only way back was
    deleting the folder.

    A 1.25 s nudge is barely past the edge — the take runs 1.1667 s longer than its window — and
    it is the least dramatic version of the two reachable paths. The other needs no nudge at
    all: on the last shot of a song the lead has already spent the whole margin, so any forward
    nudge whatsoever runs off the end.

    The sentence is `ASSEMBLY_OFFSET_OVERRUN_REFUSAL`, unaltered, because it is the same fault
    the export refuses and a Director should not have to learn it twice. The second half of this
    test is that claim, checked rather than asserted in prose: the assemble route on the same
    manifest answers with the identical string.
    """
    client, _store, comfy, _app = make_client(tmp_path)
    project_id, shots_dir = project_with_two_approved_takes(client, tmp_path)
    take_seconds = float(probe(shots_dir / "shot_a-h3_00001-audio.mp4", "format=duration"))
    nudge_shot(client, project_id, "shot_a", 1.25)

    response = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == ASSEMBLY_OFFSET_OVERRUN_REFUSAL.format(
        shot="SHOT 01 (shot_a)",
        take=take_seconds,
        offset=1.25,
        duration=4.0,
        needed=5.25,
    )
    # Nothing rendered, nothing cached, nothing on ComfyUI: the refusal is raised before the
    # chain is composed, so there is no scratch file to leak and no folder to clean out.
    assert not (tmp_path / "projects" / project_id / "media" / "previews").exists()
    assert comfy.prompts == []

    # One rule, two callers.
    refused = client.post(f"/api/projects/{project_id}/assemble")
    assert refused.status_code == 422, refused.text
    assert response.json()["detail"] in refused.json()["detail"]


def test_a_cut_that_begins_before_its_take_does_is_refused_rather_than_quietly_ignored(
    tmp_path: Path,
):
    """The nudge reaches further back than the take’s recorded lead, so the first frame the
    window asks for was never rendered. This is the overrun's mirror and it is the worse of the
    two, because the file it produced was the **right length and the wrong picture**.

    `trim_args` writes `trim=start_frame={skip},setpts=PTS-STARTPTS` only `if skip > 0`, and
    `round(-0.5 * 24)` is not. So a negative offset was neither clamped, nor reported, nor
    honoured — it was discarded, and the preview showed the take from its own first frame as
    though that were the Shot's window. Measured 2026-08-26 on a take whose luma encodes its
    frame index: previews at nudges of 0, -0.5 and -1.0 came back as **three distinct
    fingerprints** over **one byte-identical file**, all three starting at take frame 0, each
    cached under its own name and served from there for good. A short clip at least announces
    itself to anyone who counts the frames; this one counts correctly and lies about what it
    is a picture of.

    The sentence is `ASSEMBLY_OFFSET_NEGATIVE_REFUSAL`, unaltered — `take_cut_refusal` answers
    both this and the overrun, so the export and the preview cannot drift apart on either.
    """
    client, _store, comfy, _app = make_client(tmp_path)
    project_id, _shots_dir = project_with_two_approved_takes(client, tmp_path)
    nudge_shot(client, project_id, "shot_a", -0.5)

    response = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == ASSEMBLY_OFFSET_NEGATIVE_REFUSAL.format(
        shot="SHOT 01 (shot_a)", behind=0.5
    )
    assert not (tmp_path / "projects" / project_id / "media" / "previews").exists()
    assert comfy.prompts == []

    # One rule, two callers — and the negative case needs no measured take to decide, so it is
    # the export's answer here for the same reason it is the export's answer there.
    refused = client.post(f"/api/projects/{project_id}/assemble")
    assert refused.status_code == 422, refused.text
    assert response.json()["detail"] in refused.json()["detail"]


def test_a_take_that_supplies_exactly_its_window_is_previewed_rather_than_refused(
    tmp_path: Path,
):
    """The equality is the ordinary case, not the corner one.

    `shot_b` is the last shot of the song, so `over_render_lead` takes its overflow branch and
    the whole margin goes in front of the window: the cut ends on the take's final frame and
    `offset + duration` is the take's own length exactly. An overrun check written as `<` rather
    than `<=`, or one with no half-frame slack, would refuse the last shot of every song — so
    this pins the tolerance, and the file is counted rather than trusted.
    """
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id, shots_dir = project_with_two_approved_takes(client, tmp_path)
    project = client.get(f"/api/projects/{project_id}").json()
    shot_b = next(shot for shot in project["shots"] if shot["id"] == "shot_b")
    take_seconds = float(probe(shots_dir / "shot_b-h3_00001-audio.mp4", "format=duration"))
    assert shot_b["latest_take_lead"] + 4.0 == pytest.approx(take_seconds, abs=1e-6)

    response = client.post(f"/api/projects/{project_id}/shots/shot_b/preview")

    assert response.status_code == 200, response.text
    body = response.json()
    clip = tmp_path / "projects" / project_id / "media" / body["preview"]
    assert body["frames"] == 96
    assert counted_frames(clip) == 96


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
# ------------------------------------------------------------------------------------------
# Epic 10: the preview is the export's promise, and a bound Shot's preview has to keep it.
#
# The failure this block exists against is silent on every screen. A `sendcmd` aimed at a target
# not in the graph is discarded at rc 0 with no warning and byte-identical frames, so a preview
# that rendered the undriven picture would look exactly like one that worked, would be published
# under the bound Shot's own fingerprint, and would then be served from the cache for ever --
# which is Story 9.7's defect wearing this epic's clothes. Every claim here is a comparison of
# frame checksums against the undriven render of the same chain.
# ------------------------------------------------------------------------------------------


def beaty_wav_bytes(seconds: float = 8.0, rate: int = 22050) -> bytes:
    """A song with transients in it. `wav_bytes` above is digital silence, which drives nothing:
    `punch` measures level above its own running average, so a silent track and a track pinned at
    full scale both compile a script that writes the resting value at every tick and produce a
    picture identical to the undriven one -- a test that could not fail."""
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
    """One md5 per decoded video frame, ffmpeg's own `framemd5`, mapped to the picture stream
    alone. A preview carries no audio, but the map is written anyway so this reads the same way
    its sibling in `test_assembly_route` does."""
    output = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path.as_posix(), "-map", "0:v:0", "-f", "framemd5", "-"],
        check=True, capture_output=True, text=True,
    ).stdout
    return [
        line.split(",")[-1].strip()
        for line in output.splitlines()
        if line and not line.startswith("#")
    ]


def a_measured_project_with_a_graded_shot(client, tmp_path: Path):
    """The preview fixture, with a measurable song and Exposure resting at a non-identity 0.2.

    0.2 rather than 0 for the reason the export's sibling fixture gives: at the identity the card
    composes no stage unless it is bound, so the comparison would be "no `eq`" against "a driven
    `eq`" and a difference in the frames would prove only that an `eq` ran. At 0.2 both chains
    carry the identical `eq=brightness=0.2` and the only difference is the `sendcmd`.
    """
    project_id = client.post("/api/projects", json={"name": "Drive"}).json()["id"]
    upload = client.post(
        f"/api/projects/{project_id}/songs/upload",
        data={"title": "Drive Song", "duration": "0"},
        files={"file": ("song.wav", beaty_wav_bytes(8.0), "audio/wav")},
    )
    assert upload.status_code == 200, upload.text

    shots_dir = tmp_path / "comfy" / "output" / "music-video-producer" / project_id / "shots"
    picture_seconds = over_render_frames(4.0) / ASSEMBLY_FPS
    synthesize_take(shots_dir / "shot_a-h3_00001-audio.mp4", picture_seconds, "128x72", "red")
    prefix = f"music-video-producer/{project_id}/shots"
    saved = client.put(f"/api/projects/{project_id}/shots", json={"shots": [{
        "id": "shot_a", "start": 0, "duration": 4.0, "prompt": "Red room", "status": "complete",
        "latest_output": f"{prefix}/shot_a-h3_00001-audio.mp4",
    }]})
    assert saved.status_code == 200, saved.text
    assert client.post(f"/api/projects/{project_id}/shots/shot_a/approve").status_code == 200
    assert client.post(f"/api/projects/{project_id}/song/analyze").status_code == 200
    graded = client.put(
        f"/api/projects/{project_id}/shots/shot_a/effects",
        json={"effects": [{"effect": "exposure", "parameters": {"amount": 0.2}}]},
    )
    assert graded.status_code == 200, graded.text
    return project_id


def bind_exposure(client, project_id: str, **settings):
    binding = {"parameter": "amount", "drive": "punch", "depth": 0.8, "band_centre": 0.0,
               "band_width": 0.3, "band_softness": 0.35, "floor": 0.0}
    binding.update(settings)
    return client.put(
        f"/api/projects/{project_id}/shots/shot_a/effects/0/bindings",
        json={"effect": "exposure", "bindings": [binding]},
    )


def test_a_bound_shots_preview_shows_the_driven_picture_under_a_new_name(tmp_path: Path):
    """The slice's second acceptance criterion: the fingerprint moves, and the clip it names is
    a different picture from the undriven render of the same chain.

    Both halves matter and neither implies the other. A moved fingerprint with an undriven
    picture is the silent failure; an unmoved fingerprint with a driven picture would serve the
    old clip for ever, because nothing in this application evicts `previews/`.
    """
    client, _store, comfy, _app = make_client(tmp_path)
    project_id = a_measured_project_with_a_graded_shot(client, tmp_path)
    media = tmp_path / "projects" / project_id / "media"

    undriven = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")
    assert undriven.status_code == 200, undriven.text
    before = frame_checksums(media / undriven.json()["preview"])

    assert bind_exposure(client, project_id).status_code == 200
    driven = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")
    assert driven.status_code == 200, driven.text
    assert driven.json()["rendered"] is True, "the cache answered a Shot whose look had changed"
    assert driven.json()["fingerprint"] != undriven.json()["fingerprint"]
    after = frame_checksums(media / driven.json()["preview"])

    assert len(before) == len(after) == 96, (len(before), len(after))
    moved = [index for index, (a, b) in enumerate(zip(before, after)) if a != b]
    assert moved, (
        "the preview's frames are byte-identical with the binding on, which is exactly what a "
        "mistargeted sendcmd looks like: rc 0, no warning, and nothing driven"
    )
    # Not every frame: `punch` measures a transient against a running average that starts cold,
    # so the opening of the clip sits at the resting value and matches the undriven render frame
    # for frame. A drive that moved all 96 would be a constant offset wearing a binding's clothes.
    assert before[0] == after[0]
    assert len(moved) < 96, len(moved)

    # The script it was driven by is beside the clip, named by its own content, and is what the
    # chain asked for -- bare, relative, and holding nothing a filtergraph splits on.
    scripts = sorted(path.name for path in (media / "previews").glob("*.cmds"))
    assert len(scripts) == 1, scripts
    assert scripts[0].startswith("exposure-amount-b0-") and scripts[0].endswith(".cmds")
    assert not (set(scripts[0]) & set(":/\\,;=&'")), scripts[0]
    text = (media / "previews" / scripts[0]).read_text(encoding="utf-8")
    assert text.startswith("0 eq@b0 brightness 0.2;"), text[:80]
    assert all(line.endswith(";") for line in text.splitlines()), text[:200]

    assert comfy.prompts == []


def test_an_unbound_shots_preview_is_named_and_cached_exactly_as_it_was(tmp_path: Path):
    """R-20's other half, and the one this slice could most easily have broken silently.

    Nothing about a Shot with no binding may move: not the composed chain, not the fingerprint,
    not the process ffmpeg is spawned in. The clip rendered before the feature existed is served
    from the cache after it, because the name is the whole of the staleness mechanism and a name
    that moved would have re-rendered every Shot in every project on this machine for a picture
    that did not change.
    """
    client, store, _comfy, _app = make_client(tmp_path)
    project_id = a_measured_project_with_a_graded_shot(client, tmp_path)

    first = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")
    assert first.status_code == 200, first.text
    assert first.json()["rendered"] is True

    spawned: list[object] = []
    real_exec = asyncio.create_subprocess_exec

    async def watched(*args, **kwargs):
        spawned.append(kwargs.get("cwd"))
        return await real_exec(*args, **kwargs)

    asyncio.create_subprocess_exec = watched
    try:
        again = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")
    finally:
        asyncio.create_subprocess_exec = real_exec

    assert again.status_code == 200, again.text
    assert again.json()["fingerprint"] == first.json()["fingerprint"]
    assert again.json()["rendered"] is False, "an unbound Shot's cached clip was not served"
    # And the name is the one `preview_fingerprint` gives for the **empty** fifth slot, computed
    # here independently of the route. Self-consistency across two requests cannot see this: a
    # route that hashed the per-card binding shape unconditionally would send `[[]]` where `[]`
    # belongs, agree with itself perfectly, and rename every already-cached clip in every project
    # on this machine. `bindings=()` and no envelope is what a Shot with no binding must get.
    stored = store.get(project_id)
    assert preview_fingerprint(
        take=stored.shots[0].approved_output,
        window_start=0.0,
        window_duration=4.0,
        offset=stored.shots[0].latest_take_lead + stored.shots[0].trim_nudge,
        stack=[spec.model_dump() for spec in stored.shots[0].effects],
        bindings=(),
        # **`""`, not the stored fingerprint, and that is the sixth slot's whole rule.** The
        # route gates this on `stack_is_driven`, so an unbound Shot's clip is named without any
        # reference to the song at all -- which is what lets a Director analyse or replace their
        # track without orphaning the cached preview of every graded Shot in the plan. Passing
        # the fingerprint here would name the clip the route named before 2026-08-27 and this
        # assertion would fail, which is the point of computing it independently.
        song_fingerprint="",        transition=None,
        width=first.json()["width"],
        height=first.json()["height"],
        reference_width=first.json()["width"] * 2,
    ) == first.json()["fingerprint"]
    # The cache hit spawns the probes and no render, and none of them is handed a directory.
    assert all(cwd is None for cwd in spawned), spawned
    # And no script exists anywhere: `EffectStages.scripts` is empty for every stack that carries
    # no binding, which is every stack in every project until one is bound.
    assert not list((tmp_path / "projects" / project_id / "media").rglob("*.cmds"))


def test_analysing_the_song_does_not_rename_an_unbound_shots_clip_and_does_rename_a_bound_one(
    tmp_path: Path,
):
    """The sixth fingerprint slot, gated -- and the server half of a defect that had two halves.

    Until 2026-08-27 the route passed `song_fingerprint` **unconditionally** while only the
    envelope *read* was gated on `stack_is_driven`. So analysing a song renamed the Preview Clip
    of every Shot carrying any effect at all, bound or not, and orphaned every one of their
    cached clips -- for a reason that cannot reach an unbound Shot's picture, since
    `build_effect_stages` ignores the envelope entirely for a stack with no binding and composes
    no `sendcmd` stage. Analysing is a first-class gesture, so that was a re-render sweep of the
    whole plan on a gesture about beats.

    The other half was the client's: `api.previewInputKey` carried no song at all, so it never
    re-asked for any of the clips the server had just renamed. Both sides are now gated on one
    rule; `test_the_client_and_the_server_answer_driven_identically` is what keeps them one.

    Three states in one project, because the claim is a *difference* and needs all three: the
    same Shot unbound across a re-analysis, unbound across a replaced song, and bound.
    """
    client, store, _comfy, _app = make_client(tmp_path)
    project_id = a_measured_project_with_a_graded_shot(client, tmp_path)

    def named() -> str:
        answer = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")
        assert answer.status_code == 200, answer.text
        return answer.json()["fingerprint"]

    unbound = named()
    # Re-measured. A new analysis record over the same bytes, which is the common gesture.
    assert client.post(f"/api/projects/{project_id}/song/analyze").status_code == 200
    assert named() == unbound, "a re-analysis renamed an unbound Shot's clip"

    # And a song genuinely replaced, which is the state the export refuses a bound Shot in. An
    # unbound Shot's picture is untouched by it, so its clip must be untouched too.
    stored = store.get(project_id)
    stored.song.analysis.song_fingerprint = "12-notthesongthatisonthedisk"
    store.save(stored)
    assert named() == unbound, "replacing the song renamed an unbound Shot's clip"

    # Bound, against a current measurement: a different picture, so a different name. This is the
    # half that must keep moving -- it is what makes the client re-ask.
    assert client.post(f"/api/projects/{project_id}/song/analyze").status_code == 200
    assert bind_exposure(client, project_id).status_code == 200
    assert named() != unbound

def test_a_preview_whose_envelope_stopped_describing_the_song_refuses_in_the_exports_words(
    tmp_path: Path,
):
    """Story 10.4's state, at the route that is supposed to predict the export.

    The preview refuses, in the export's own sentence, rather than rendering the undriven picture
    -- because the undriven picture is indistinguishable from a working one and would be
    published under the bound Shot's name. And the cached clip from *before* the song went stale
    is not served either: the refusal is raised before the cache is consulted, for the reason a
    deleted `.cube` is.
    """
    client, store, _comfy, _app = make_client(tmp_path)
    project_id = a_measured_project_with_a_graded_shot(client, tmp_path)
    assert bind_exposure(client, project_id).status_code == 200
    live = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")
    assert live.status_code == 200, live.text
    cached = tmp_path / "projects" / project_id / "media" / live.json()["preview"]
    assert cached.is_file()
    stored = [spec.model_dump() for spec in store.get(project_id).shots[0].effects]

    project = store.get(project_id)
    project.song.analysis.song_fingerprint = "12-notthesongthatisonthedisk"
    store.save(project)

    refused = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == BINDING_WITHOUT_ENVELOPE_REFUSAL.format(
        shot="SHOT 01 (shot_a)", reason=SONG_ENVELOPE_SONG_CHANGED
    )
    # The clip is still on disk -- a stale entry is inert, never deleted -- and was not served.
    assert cached.is_file()
    # The binding is retained, and re-measuring makes it live again with its stored values.
    assert [spec.model_dump() for spec in store.get(project_id).shots[0].effects] == stored
    assert client.post(f"/api/projects/{project_id}/song/analyze").status_code == 200
    revived = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")
    assert revived.status_code == 200, revived.text
    assert revived.json()["fingerprint"] == live.json()["fingerprint"]


def test_a_binding_on_a_project_that_was_never_analyzed_refuses_by_its_own_reason(
    tmp_path: Path,
):
    """The other absence, and it is a different sentence with a different remedy: never measured
    rather than measured-and-stale. `song_measurement_verdict` is the one function that tells
    them apart and the refusal carries whichever it reached, whole."""
    client, store, _comfy, _app = make_client(tmp_path)
    project_id = a_measured_project_with_a_graded_shot(client, tmp_path)
    assert bind_exposure(client, project_id).status_code == 200

    project = store.get(project_id)
    project.song.analysis = SongAnalysis()
    store.save(project)

    refused = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == BINDING_WITHOUT_ENVELOPE_REFUSAL.format(
        shot="SHOT 01 (shot_a)", reason=SONG_ENVELOPE_NOT_TAKEN
    )
def test_a_shot_later_in_the_song_previews_its_own_stretch_of_the_measurement(tmp_path: Path):
    """The preview's half of the arithmetic the export's sibling test proves.

    A preview is the whole Shot from its own first frame, so the filter graph's clock starts at
    zero -- but the drive's clock is the song's. Move the Shot most of four seconds along a song
    whose beats are half a second apart and the identical binding compiles a **different
    script**, because it is listening to a different stretch of one measurement. Handed the
    song's start whatever the Shot's, both would compile one script and the moved Shot would
    flash on the opening's beats.

    **It is moved to 3.95 s and not to 4 s, and the odd number is the point.** At 30 Hz a Shot
    starting at 4 s starts exactly on analysis tick 120, and on a tick boundary the two halves of
    the compiler's quantisation are both no-ops: `floor` and `ceil` pick the same tick, and that
    tick's own second minus the Shot's start is exactly zero rather than negative. 3.95 s is tick
    118.5 -- half way between two -- so the tick covering this Shot's first frame begins 0.0167 s
    *before* it. The clip must still open on that tick, stamped at zero: the assertion that every
    script starts at `0` therefore says something here that it could not say at 4 s, where it
    holds however the walk rounds and however the timestamp is signed.
    """
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id = a_measured_project_with_a_graded_shot(client, tmp_path)
    assert bind_exposure(client, project_id).status_code == 200
    previews = tmp_path / "projects" / project_id / "media" / "previews"

    assert client.post(f"/api/projects/{project_id}/shots/shot_a/preview").status_code == 200
    at_the_start = {path.name: path.read_text(encoding="utf-8") for path in previews.glob("*.cmds")}
    assert len(at_the_start) == 1, sorted(at_the_start)

    # The whole shot list back through `PUT /shots`, which is how the browser moves a clip -- and
    # which must not disturb the binding on the way past (`_adopt_shot_effects`).
    project = client.get(f"/api/projects/{project_id}").json()
    project["shots"][0]["start"] = 3.95
    moved = client.put(f"/api/projects/{project_id}/shots", json={"shots": project["shots"]})
    assert moved.status_code == 200, moved.text
    assert client.post(f"/api/projects/{project_id}/shots/shot_a/approve").status_code == 200
    assert client.post(f"/api/projects/{project_id}/shots/shot_a/preview").status_code == 200

    later = {path.name: path.read_text(encoding="utf-8") for path in previews.glob("*.cmds")}
    assert len(later) == 2, (
        "the moved Shot compiled the identical script, so it is being driven by the opening of "
        "the song rather than by the four seconds it actually occupies"
    )
    # Both are clip-local and both address the same labelled stage: only the values differ.
    for text in later.values():
        assert text.startswith("0 eq@b0 brightness "), text[:60]


# ------------------------------------------------------------------------------------------
# The Drive readout's route (story 10.3, R-27).
#
# `GET .../shots/{id}/drive` serves the compiled `sendcmd` values so the readout beneath the
# Monitor can draw them. The headline assertion is the one that could not be made any other way:
# the served numbers are compared against **the script file the preview render really handed
# ffmpeg**, read off disk after the render. A test that compared the route against the compiler
# would be comparing the compiler with itself; this compares it against the argv.
# ------------------------------------------------------------------------------------------


def written_commands(previews: Path) -> list[tuple[float, float]]:
    """Every `(second, value)` in the one compiled script beside the rendered clip, parsed.

    Parsed rather than reconstructed. Reproducing `_number`'s formatting here to build an expected
    string would be a second copy of the thing under test; reading the file back and pulling the
    numbers out of it is the comparison the acceptance criterion actually asks for.
    """
    scripts = sorted(previews.glob("*.cmds"))
    assert len(scripts) == 1, scripts
    out: list[tuple[float, float]] = []
    for line in scripts[0].read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        stamp, target, option, value = line.rstrip(";").split()
        assert (target, option) == ("eq@b0", "brightness"), line
        out.append((float(stamp), float(value)))
    return out


def test_the_drive_readout_serves_the_very_script_the_render_was_handed(tmp_path: Path):
    """R-27's ruling, checked against the file on disk.

    The preview render writes its compiled `sendcmd` script beside the clip and runs ffmpeg with
    that directory as its working directory, so the `.cmds` file read below is literally what the
    filter graph consumed. Every timestamp and every value the readout is served appears on those
    lines, in that order, and nothing else does — which is what makes the picture the argv rather
    than an illustration of it.

    **The alternative this closes is invisible.** A browser handed the raw band series and left to
    model the drive itself would draw a curve that could differ from the export's in the band
    weighting, the running average, the release, the gate or the clamp, with the suite green, ruff
    clean, the export correct and the screen wrong.
    """
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id = a_measured_project_with_a_graded_shot(client, tmp_path)
    assert bind_exposure(client, project_id).status_code == 200
    rendered = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")
    assert rendered.status_code == 200, rendered.text
    previews = tmp_path / "projects" / project_id / "media" / "previews"
    handed = written_commands(previews)

    answer = client.get(f"/api/projects/{project_id}/shots/shot_a/drive")

    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["shot_id"] == "shot_a"
    assert len(body["bindings"]) == 1
    served = body["bindings"][0]
    assert (served["effect"], served["parameter"], served["index"]) == ("exposure", "amount", 0)
    # Every line, in order. `round` to six places because that is the precision the script's own
    # formatter writes at, and a float that survives a decimal round trip is the same number.
    assert [round(second, 6) for second in served["at"]] == [second for second, _ in handed]
    assert [round(value, 6) for value in served["values"]] == [value for _, value in handed]
    # And it is a real drive rather than a flat line, so the comparison above has something in it.
    assert len(handed) > 24
    assert len({value for _, value in handed}) > 2
    # The two ends the readout is drawn between: where a shut gate leaves the parameter, and where
    # a full drive takes it. Both are on the wire because nothing on the client can clamp.
    assert served["rest"] == pytest.approx(0.2)
    assert served["reach"] == pytest.approx(1.0)
    # The window is the frame grid's, not the manifest's float — the axis of the picture above it.
    assert body["seconds"] == pytest.approx(
        clip_frames_on_grid(0.0, 4.0) / ASSEMBLY_FPS)


def test_the_readouts_window_is_the_frame_grids_and_not_the_manifests_float(tmp_path: Path):
    """The axis the readout is drawn against is the export's, not the manifest's.

    **This test exists because a mutation survived.** The first version of the assertion above
    checked `seconds` against the frame grid on a Shot whose window was exactly 4.0 s -- where the
    grid and the stored float are the same number, so replacing one with the other changed nothing
    and the guard passed over the defect. The fixture made it impossible, which is the failure mode
    this repository has recorded fourteen times across this epic's earlier slices.

    A window of 4.03 s is 96.72 frames, which the grid rounds to 97: the clip the export cuts is
    4.041666 s long and the drive is compiled over *that*. Drawn against 4.03 instead, every
    command would sit a fraction of a frame late by the end of the Shot, and the last one would be
    outside the picture the Monitor is showing.
    """
    client, store, _comfy, _app = make_client(tmp_path)
    project_id = a_measured_project_with_a_graded_shot(client, tmp_path)
    assert bind_exposure(client, project_id).status_code == 200
    project = store.get(project_id)
    project.shots[0].duration = 4.03
    store.save(project)
    grid = clip_frames_on_grid(0.0, 4.03) / ASSEMBLY_FPS
    assert grid != pytest.approx(4.03), "the fixture cannot tell the two windows apart"
    rendered = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")
    assert rendered.status_code == 200, rendered.text
    handed = written_commands(tmp_path / "projects" / project_id / "media" / "previews")

    body = client.get(f"/api/projects/{project_id}/shots/shot_a/drive").json()

    assert body["seconds"] == pytest.approx(grid)
    served = body["bindings"][0]
    # And the compiled series really is the longer one: every line the render was handed, and no
    # more. A window of 4.03 s would have stopped one tick earlier.
    assert [round(second, 6) for second in served["at"]] == [second for second, _ in handed]
    assert max(served["at"]) > 4.03


def test_a_shot_with_no_binding_is_absent_rather_than_empty(tmp_path: Path):
    """FX-22. A Shot carrying no binding answers an empty list, and the readout is not drawn at
    all — no zero-height canvas, no empty box, no flat line where an envelope would be."""
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id = a_measured_project_with_a_graded_shot(client, tmp_path)

    answer = client.get(f"/api/projects/{project_id}/shots/shot_a/drive")

    assert answer.status_code == 200, answer.text
    assert answer.json()["bindings"] == []


def test_an_unbound_shot_never_reads_the_measurement(tmp_path: Path):
    """The cost rule, as a property rather than a comment.

    Deciding whether a measurement is current hashes the whole master and parses a ~405 KB
    sidecar, and this route is read whenever a Shot is selected — so a Shot carrying no binding,
    which is every Shot in every project until one is bound, must not pay it. The sidecar reader is
    replaced by one that raises: the unbound Shot still answers 200, and the bound one still
    compiles, so the skip is real and is not simply "nothing was bound anyway".
    """
    client, store, _comfy, _app = make_client(tmp_path)
    project_id = a_measured_project_with_a_graded_shot(client, tmp_path)
    reads: list[str] = []
    original = store.read_song_envelope

    def counted(*args, **kwargs):
        reads.append("read")
        return original(*args, **kwargs)

    store.read_song_envelope = counted  # type: ignore[method-assign]
    assert client.get(f"/api/projects/{project_id}/shots/shot_a/drive").status_code == 200
    assert reads == []

    assert bind_exposure(client, project_id).status_code == 200
    bound_answer = client.get(f"/api/projects/{project_id}/shots/shot_a/drive")
    assert bound_answer.status_code == 200, bound_answer.text
    assert bound_answer.json()["bindings"], bound_answer.text
    assert reads, "a bound Shot must read the measurement it is compiled against"


def test_a_binding_on_a_card_the_director_switched_off_draws_nothing(tmp_path: Path):
    """A disabled card composes no stage and compiles no script, so the export drives nothing —
    and a readout for it would be a picture of a look the render will not produce."""
    client, store, _comfy, _app = make_client(tmp_path)
    project_id = a_measured_project_with_a_graded_shot(client, tmp_path)
    assert bind_exposure(client, project_id).status_code == 200
    assert client.get(f"/api/projects/{project_id}/shots/shot_a/drive").json()["bindings"]

    project = store.get(project_id)
    project.shots[0].effects[0].enabled = False
    store.save(project)

    assert client.get(f"/api/projects/{project_id}/shots/shot_a/drive").json()["bindings"] == []


def test_a_song_whose_measurement_has_gone_leaves_the_readout_absent(tmp_path: Path):
    """Story 10.4's state at this route. The binding is retained and reported unresolvable by the
    band panel, which already names the absence and offers the remedy; the readout has nothing
    compiled to draw and says so by not being there. Re-measuring brings it back."""
    client, store, _comfy, _app = make_client(tmp_path)
    project_id = a_measured_project_with_a_graded_shot(client, tmp_path)
    assert bind_exposure(client, project_id).status_code == 200

    project = store.get(project_id)
    project.song.analysis.song_fingerprint = "12-notthesongthatisonthedisk"
    store.save(project)

    gone = client.get(f"/api/projects/{project_id}/shots/shot_a/drive")
    assert gone.status_code == 200, gone.text
    assert gone.json()["bindings"] == []
    # The binding is not dropped: it is still on the card, and the measurement brings it back.
    assert store.get(project_id).shots[0].effects[0].bindings
    assert client.post(f"/api/projects/{project_id}/song/analyze").status_code == 200
    assert client.get(f"/api/projects/{project_id}/shots/shot_a/drive").json()["bindings"]


def test_two_bindings_come_back_in_chain_order_each_naming_its_own_card(tmp_path: Path):
    """A Shot may carry more than one binding, and the readout draws one — so the list has to say
    which card each envelope belongs to, and in the order the export drives them. Soft Focus is a
    Texture card and composes ahead of the Exposure it is stored after."""
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id = a_measured_project_with_a_graded_shot(client, tmp_path)
    stacked = client.put(
        f"/api/projects/{project_id}/shots/shot_a/effects",
        json={"effects": [
            {"effect": "exposure", "parameters": {"amount": 0.2}},
            {"effect": "soft_focus", "parameters": {"sigma": 4.0}},
        ]},
    )
    assert stacked.status_code == 200, stacked.text
    assert bind_exposure(client, project_id).status_code == 200
    focused = client.put(
        f"/api/projects/{project_id}/shots/shot_a/effects/1/bindings",
        json={"effect": "soft_focus", "bindings": [
            {"parameter": "sigma", "drive": "punch", "depth": 2.0}]},
    )
    assert focused.status_code == 200, focused.text

    served = client.get(f"/api/projects/{project_id}/shots/shot_a/drive").json()["bindings"]

    assert [(item["effect"], item["index"]) for item in served] == [
        ("soft_focus", 1), ("exposure", 0)]


def test_the_drive_route_writes_nothing(tmp_path: Path):
    """A read in the strong sense: the manifest is byte-identical afterwards, and no clip, script
    or cache entry appears. AD-23's rule for the preview, applied to the route beside it."""
    client, store, _comfy, _app = make_client(tmp_path)
    project_id = a_measured_project_with_a_graded_shot(client, tmp_path)
    assert bind_exposure(client, project_id).status_code == 200
    manifest = store.project_dir(project_id) / "project.json"
    before = manifest.read_bytes()
    media = tmp_path / "projects" / project_id / "media"
    listed = sorted(str(path.relative_to(media)) for path in media.rglob("*")) if media.exists() else []

    assert client.get(f"/api/projects/{project_id}/shots/shot_a/drive").status_code == 200

    assert manifest.read_bytes() == before
    after = sorted(str(path.relative_to(media)) for path in media.rglob("*")) if media.exists() else []
    assert after == listed


def test_the_drive_route_404s_for_a_shot_that_is_not_there(tmp_path: Path):
    client, _store, _comfy, _app = make_client(tmp_path)
    project_id = a_measured_project_with_a_graded_shot(client, tmp_path)

    assert client.get(f"/api/projects/{project_id}/shots/nobody/drive").status_code == 404


def test_a_sidecar_element_that_is_not_a_number_is_refused_rather_than_raised(tmp_path: Path):
    """B3. `effects._envelope_bands` validated the envelope's *shape* and nothing about the
    numbers in it, and `band_series`' `float()` was unguarded.

    Reproduced 2026-08-28 at `4fd9b41`. A string or a `null` in `bands` raises `ValueError` /
    `TypeError`, which is not an `EffectRefusal`, and these routes catch nothing else — so
    `GET .../drive`, the preview and the export each answered **500**.

    **`NaN` was worse and is the reason this test asserts the drive is absent rather than merely
    non-crashing.** `json.loads` accepts the bare literal, one of them poisons the weighted mean
    for every band at that tick, and `_punch_series`' running average is poisoned for the whole
    rest of the song from there — so every binding in the project collapsed to its resting value
    at rc 0 with `silenced: false`. An un-dimmed flat line and an export that ran and changed
    nothing: no exception to catch, and no evidence in the picture that anything went wrong.

    Every poison is written straight into the sidecar rather than through `write_song_envelope`,
    which refuses a non-finite float with `allow_nan=False` — the file on disk is hand-editable
    and that is the door this closes.
    """
    from music_video_producer.effects import BINDING_NO_ENVELOPE_REFUSAL

    client, store, _comfy, _app = make_client(tmp_path)
    project_id = a_measured_project_with_a_graded_shot(client, tmp_path)
    assert bind_exposure(client, project_id).status_code == 200
    drive_url = f"/api/projects/{project_id}/shots/shot_a/drive"

    healthy = client.get(drive_url).json()["bindings"]
    assert healthy and len({value for value in healthy[0]["values"]}) > 2, (
        "the fixture must drive a real signal, or every assertion below passes for the wrong "
        "reason"
    )

    sidecar = store.song_envelope_path(project_id)
    sound = json.loads(sidecar.read_text(encoding="utf-8"))
    refusal = BINDING_NO_ENVELOPE_REFUSAL.format(effect="exposure", parameter="amount")

    for poison in ("loud", None, True, float("nan"), float("inf"), 10**401):
        payload = json.loads(json.dumps(sound))
        payload["bands"][0][7] = poison
        # `allow_nan=True` is `json.dump`'s own default and writes the bare `NaN` token, which is
        # exactly what a hand-edited or foreign-written sidecar can hold and what `json.loads`
        # reads back without complaint.
        sidecar.write_text(json.dumps(payload), encoding="utf-8")
        label = repr(poison)

        served = client.get(drive_url)
        assert served.status_code == 200, (label, served.text)
        assert served.json()["bindings"] == [], (
            f"{label} in the sidecar drew a readout off a measurement nothing can read"
        )

        previewed = client.post(f"/api/projects/{project_id}/shots/shot_a/preview")
        assert previewed.status_code == 422, (label, previewed.text)
        assert previewed.json()["detail"].endswith(refusal), (label, previewed.text)

    # And a sound sidecar put back is driven again: the refusal is of the poison, not of the song.
    sidecar.write_text(json.dumps(sound), encoding="utf-8")
    assert client.get(drive_url).json()["bindings"] == healthy
