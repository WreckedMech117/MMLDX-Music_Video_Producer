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
    PREVIEW_ABANDONED_DETAIL,
    PREVIEW_NO_TAKE_REFUSAL,
    PREVIEW_SUPERSEDED_REFUSAL,
    create_app,
)
from music_video_producer.assembly import (
    ASSEMBLY_FPS,
    ASSEMBLY_OFFSET_NEGATIVE_REFUSAL,
    ASSEMBLY_OFFSET_OVERRUN_REFUSAL,
    EXPORT_PRESETS,
    PREVIEW_PRESET,
)
from music_video_producer.comfy import ComfyError
from music_video_producer.config import Settings
from music_video_producer.effects import EFFECT_CATALOGUE
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
