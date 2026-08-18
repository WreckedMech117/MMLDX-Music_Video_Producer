"""Manual live smoke of the H3 Ultra *reference* path, driven through the running app.

Not pytest-collected (like ``smoke_h3_app.py`` and ``smoke_songplanner_app.py``). It spends
**two** real GPU jobs on the user-managed ComfyUI -- one Krea multiview promotion and one
minimum-cost H3 reference render -- so it refuses to submit anything without ``--confirm-gpu``.
Run from the repo root with the app already serving and ComfyUI already up (never started or
stopped here)::

    uv run python tests/smoke_h3_reference_app.py [base_url] --confirm-gpu

This is the evidence the character-consistency path never had. Everything it renders is staged
through shipped routes -- create the project, upload the song, upload the character image,
promote it to a Reference Sheet, write the Shot -- because a hand-written manifest would assert
a provenance the application never produced. In particular the child Asset's ``source`` is
``krea-multiview`` only because ``POST .../assets/{id}/multiview`` created it, and its ``path``
is populated only by the ordinary job refresh reconciling ``output_files``. That population is
itself the evidence promotion works end to end: until it happens the child cannot be attached,
because ``resolve_asset_path`` fails ``is_file()`` and the reference branch 404s.

Order of operations is the one the other two smokes settled on, and it exists so every abort
lands ahead of the GPU spend: the cost gate before any network call at all, ``ffprobe`` located
up front, the character source image confirmed on disk, the frame arithmetic checked against
the shipped grid helper, then ``/api/health`` (abort unless ComfyUI is online), then the
``tests/preflight_h3_ultra.py`` audit against that same ComfyUI. Only then is anything
submitted. The promotion goes first and must reach ``complete`` before the render is
considered; a failed promotion spends the second job on nothing, so it does not get to.

``duration=3.75`` rather than a round 4 s: the reference builder pads to the 17k+5 grid, so
4.0 s becomes 107 frames and 4.458 s while 3.75 s is exactly 90 frames and a measured 3.750 s
the assertion needs no allowance for. The requested geometry is 640x384 at 4 steps -- the
cheapest window that still proves the path.

The staged media is the project's real asset library under ComfyUI's ``input/music-video/``
rather than anything this script invents: the character image from ``characters/`` and the
master song from ``audio/``. One picture reference plus the song is deliberate -- it is the
minimum window that exercises both the ``<Picture 1>`` tag and the ``<Audio N> is the master
song for synchronization`` tag the route appends for ``use_song_audio``, and a second
reference would change what the render proves without making it prove more. The library also
holds a location image; it is knowingly left unattached. Note that the master song runs far
longer than the Shot window and the route hands the node the whole file, which is the shipped
behaviour this run measures rather than something to work around here.

The probe target is chosen **by name, not by taking ``output_files[0]``**: a completed H3 shot
leaves three files behind (a ``.png``, a silent ``.mp4`` and a muxed ``-audio.mp4``) and only
the ``-audio.mp4`` carries the synchronized audio this run exists to measure. Which file the
app's own reconciliation wrote to ``latest_output`` is reported as a fact next to it rather
than assumed to be the same one.

Exactly one JSON block is printed and it is the only thing on stdout, the audit's own report
included: it is the record of which prompt IDs this run produced, and it is printed *before*
the assertions so a failure still leaves the mapping behind. Approval is never written here --
the finished Shot must carry ``latest_output`` with ``approved_output`` still empty, and that
is asserted rather than assumed.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

# From this script's own directory, which is sys.path[0] when it is run as a script. Importing
# it also calls `repo_src_on_path()`, which is what makes `music_video_producer` importable.
import preflight_h3_ultra

# The mature smoke's helpers rather than a second copy of each. `probe` above all: this story
# extended it to report the video stream alongside the audio one, and two probes would be two
# answers to the question this run exists to answer. `poll` brings its transient-failure
# tolerance and its one re-read past the ceiling, `resolve_output` its refusal of any path that
# escapes the ComfyUI output root, and `check_comfy` the offline gate.
from smoke_songplanner_app import (
    LOCAL_HOSTS,
    POLL_CEILING_SECONDS,
    abort,
    as_float,
    as_int,
    check_comfy,
    expect_object,
    note,
    poll,
    probe,
    request,
    resolve_output,
)

from music_video_producer.config import Settings
from music_video_producer.timeline import align_h3_frames
from music_video_producer.workflows import H3_FRAME_RATE

DEFAULT_BASE_URL = "http://127.0.0.1:8766"
PROJECT_NAME = "H3 Reference Smoke QA"

#: The project's real asset library, laid out by kind under ComfyUI's input directory. Both
#: files below are checked before any network call, because a missing source is a free refusal.
#: `locations/DaskWarehouseBed.png` lives here too and is deliberately not attached -- see the
#: module docstring on why one picture plus the song is the window this run wants.
ASSET_LIBRARY = Path(
    r"J:\Hermes-Remote\comfyui\ComfyUI_windows_portable\ComfyUI\input\music-video"
)
#: The promotion source: a 1536x1024 character image. The canonical copy, rather than the loose
#: `input/Lucy-Metal.png` duplicate of the same bytes.
CHARACTER_IMAGE = ASSET_LIBRARY / "characters" / "Lucy-Metal.png"
CHARACTER_NAME = "Lucy"
#: What the reference map calls this Asset in the prompt the route builds. Set explicitly so the
#: tag reads as direction rather than as a filename.
CHARACTER_LABEL = "Lucy, the lead singer"
MULTIVIEW_PROMPT = (
    "Character sheet of the same person in four views on a plain neutral backdrop: front, "
    "three-quarter, profile, and back. Identical face, hair, and wardrobe in every view, even "
    "studio lighting, full body, no text."
)
MULTIVIEW_SEED = 20260818

SHOT_PROMPT = (
    "The character from the reference sheet sings to camera in a dim warehouse under one amber "
    "light, slow push in, stable face and wardrobe, one continuous take."
)
SHOT_START_SECONDS = 0.0
#: 3.75 s is exactly 90 frames on the 17k+5 grid; see the module docstring.
SHOT_DURATION_SECONDS = 3.75
EXPECTED_FRAMES = 90
SHOT_SEED = 20260819
RENDER_WIDTH = 640
RENDER_HEIGHT = 384
RENDER_STEPS = 4

#: The master song: the real track from the asset library, imported through the shipped route.
#: `use_song_audio` on the Shot is what appends it as a further audio reference, so this is the
#: file the render is asked to synchronize against.
#:
#: The import route takes `title` and `duration` only, so the stored Song carries empty lyrics
#: and caption. That is a known gap in the route, tracked separately; nothing here asserts
#: otherwise, and nothing here works around it by writing the manifest by hand.
SONG_FILE = ASSET_LIBRARY / "audio" / "Harder Faster (Female Cover).mp3"
SONG_TITLE = "Harder Faster (Female Cover)"
SONG_CONTENT_TYPE = "audio/mpeg"

#: How far the measured video duration may sit from the requested one. The grid makes this
#: exact -- 90 frames at 24 fps is 3.750 s -- so this only absorbs container rounding.
DURATION_TOLERANCE_SECONDS = 0.05
#: How far the audio stream may sit from the video stream and still count as synchronized. AAC
#: quantizes to 1024-sample frames, so a small tail difference is normal; a whole second is not.
SYNC_TOLERANCE_SECONDS = 0.15


def upload(
    base_url: str,
    path: str,
    *,
    fields: dict[str, str],
    file_field: str,
    filename: str,
    content: bytes,
    content_type: str,
) -> Any:
    """One multipart POST to the app. `request` sends JSON, and the two upload routes take form
    data, so this is the same call with the body the shipped route actually accepts."""
    boundary = f"----mvp{uuid.uuid4().hex}"
    body = bytearray()
    for name, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += f"{value}\r\n".encode()
    body += f"--{boundary}\r\n".encode()
    body += (
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    body += content
    body += f"\r\n--{boundary}--\r\n".encode()
    call = urllib.request.Request(
        f"{base_url}{path}",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(call, timeout=300) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        abort(f"POST {base_url}{path} returned {error.code}: {error.read().decode()[:500]}")
    except (urllib.error.URLError, OSError, ValueError) as error:
        abort(f"cannot reach the application at {base_url}{path}: {error}")
    return None


def run_preflight(comfy_url: str) -> None:
    """Reuse the H3 audit rather than duplicating it; its `main()` reads `sys.argv`.

    Its report is printed to stdout, so it is redirected here: a passing audit's `OK` line would
    otherwise sit in front of the JSON block that is this script's whole machine-readable
    output, and a failing audit's `FAIL` lines belong with the other aborts on stderr.
    """
    saved = sys.argv
    sys.argv = [str(Path(preflight_h3_ultra.__file__)), comfy_url]
    try:
        with contextlib.redirect_stdout(sys.stderr):
            preflight_h3_ultra.main()
    except SystemExit as error:
        if error.code:
            abort("pre-flight reported a problem; nothing was submitted")
    finally:
        sys.argv = saved


def choose_probe_target(output_files: list[str]) -> tuple[str, str]:
    """Which of a completed H3 shot's outputs carries the synchronized audio, and why.

    A completed shot leaves three files -- a `.png` still, a silent `.mp4`, and the muxed
    `-audio.mp4` -- so `output_files[0]` is whichever one ComfyUI happened to list first and is
    not necessarily the one worth measuring. The choice is by name, and the reason travels with
    it into the record so a surprising pick is visible rather than silent.
    """
    muxed = [item for item in output_files if item.lower().endswith("-audio.mp4")]
    if muxed:
        return muxed[0], "named -audio.mp4, the only H3 output carrying synchronized audio"
    videos = [
        item
        for item in output_files
        if item.lower().endswith((".mp4", ".mov", ".webm", ".mkv"))
    ]
    if videos:
        return videos[0], "no -audio.mp4 was produced; measuring the first video-shaped output"
    if output_files:
        return output_files[0], "no video-shaped output was produced; measuring the first file"
    return "", "the completed job listed no output files at all"


def emit(record: dict[str, Any]) -> None:
    """Print the run's one JSON block, once. Called before the assertions so a failing run still
    leaves the prompt-ID mapping behind; the flag stops a later failure printing a second."""
    if record.pop("_emitted", False):
        return
    record["_emitted"] = True
    printable = {key: value for key, value in record.items() if not key.startswith("_")}
    print(json.dumps(printable, indent=2), flush=True)


def fail(record: dict[str, Any], message: str) -> None:
    emit(record)
    abort(message)


def create_project(base_url: str) -> dict[str, Any]:
    note(f"creating project {PROJECT_NAME!r}")
    return expect_object(
        request(base_url, "/api/projects", method="POST", payload={"name": PROJECT_NAME}),
        where="POST /api/projects",
        keys=("id",),
    )


def import_song(base_url: str, project_id: str) -> dict[str, Any]:
    """Upload the master song through the shipped import route.

    `duration` is sent as 0 deliberately: that is the browser's "I could not decode this"
    value, and it makes the server measure the file with `ffprobe` rather than trust a number
    this script supplied. The stored duration is the project's timing spine, so a zero coming
    back out is a problem worth stopping for -- and it is free to stop here.
    """
    note(f"importing the master song {SONG_FILE.name}")
    project = expect_object(
        upload(
            base_url,
            f"/api/projects/{project_id}/songs/upload",
            fields={"title": SONG_TITLE, "duration": "0"},
            file_field="file",
            filename=SONG_FILE.name,
            content=SONG_FILE.read_bytes(),
            content_type=SONG_CONTENT_TYPE,
        ),
        where=f"POST /api/projects/{project_id}/songs/upload",
        keys=("song",),
    )
    song = project.get("song") if isinstance(project.get("song"), dict) else {}
    if not song.get("path"):
        abort("the song import stored no path; nothing was submitted")
    if as_float(song.get("duration")) <= 0:
        abort(
            "the song import stored a zero duration, so the server's ffprobe fallback did not "
            "run; shot windows are seconds against this song, so nothing was submitted"
        )
    return song


def import_character(base_url: str, project_id: str) -> dict[str, Any]:
    """Upload the character source image as a `character` Asset.

    `kind` is a form field on the upload route, which is what makes a *source* character
    reachable without generating one first -- the multiview route requires `kind="character"`
    with a real file behind it.
    """
    note(f"uploading the character source {CHARACTER_IMAGE.name}")
    project = expect_object(
        upload(
            base_url,
            f"/api/projects/{project_id}/assets/upload",
            fields={"name": CHARACTER_NAME, "kind": "character"},
            file_field="file",
            filename=CHARACTER_IMAGE.name,
            content=CHARACTER_IMAGE.read_bytes(),
            content_type="image/png",
        ),
        where=f"POST /api/projects/{project_id}/assets/upload",
        keys=("assets",),
    )
    assets = project.get("assets") if isinstance(project.get("assets"), list) else []
    # The route appends, so the new Asset is the last one -- and it is checked rather than
    # searched for. Scanning backwards for "a character Asset with a path" looks equivalent and
    # is not: a promoted Reference Sheet is *also* `kind="character"` with a path, so a project
    # that already held one could hand this run somebody else's Asset to promote.
    source = assets[-1] if assets and isinstance(assets[-1], dict) else {}
    if (
        source.get("kind") != "character"
        or source.get("source") != "upload"
        or not source.get("path")
        or not source.get("id")
    ):
        abort(
            "the asset upload did not leave an uploaded character Asset last on the project: "
            f"{json.dumps(source)[:300]}; nothing was submitted"
        )
    return source


def promote(base_url: str, project_id: str, asset_id: str) -> dict[str, Any]:
    """Submit the multiview promotion. **This is the first of the two authorised GPU jobs.**"""
    note("submitting the multiview promotion -- GPU spend starts here (job 1 of 2)")
    path = f"/api/projects/{project_id}/assets/{asset_id}/multiview"
    return expect_object(
        request(
            base_url,
            path,
            method="POST",
            payload={"prompt": MULTIVIEW_PROMPT, "seed": MULTIVIEW_SEED},
        ),
        where=f"POST {path}",
        keys=("id", "prompt_id", "status"),
    )


def write_shot(base_url: str, project_id: str, child_asset_id: str) -> dict[str, Any]:
    """Write the one Shot the render submits, with the promoted sheet attached.

    `status="ready"` is set here because nothing in the shipped UI writes it and the H3 route
    refuses anything else -- the same shape `smoke_h3_app.py` uses. `use_song_audio` is what
    appends the master song as a further audio reference inside the route.
    """
    note("writing the reference Shot")
    shot = {
        "start": SHOT_START_SECONDS,
        "duration": SHOT_DURATION_SECONDS,
        "prompt": SHOT_PROMPT,
        "mode": "reference",
        "asset_ids": [child_asset_id],
        "reference_labels": {child_asset_id: CHARACTER_LABEL},
        "use_song_audio": True,
        "seed": SHOT_SEED,
        "status": "ready",
    }
    project = expect_object(
        request(
            base_url,
            f"/api/projects/{project_id}/shots",
            method="PUT",
            payload={"shots": [shot]},
        ),
        where=f"PUT /api/projects/{project_id}/shots",
        keys=("shots",),
    )
    shots = project.get("shots") if isinstance(project.get("shots"), list) else []
    if not shots or not isinstance(shots[0], dict) or not shots[0].get("id"):
        abort("the shot write returned no Shot; nothing was submitted")
    return shots[0]


def submit_reference(base_url: str, project_id: str, shot_id: str) -> dict[str, Any]:
    """Submit the reference render. **This is the second of the two authorised GPU jobs.**"""
    note(
        f"submitting the reference render at {RENDER_WIDTH}x{RENDER_HEIGHT} and "
        f"{RENDER_STEPS} steps -- GPU spend (job 2 of 2)"
    )
    path = f"/api/projects/{project_id}/shots/{shot_id}/generate/h3"
    return expect_object(
        request(
            base_url,
            path,
            method="POST",
            payload={"width": RENDER_WIDTH, "height": RENDER_HEIGHT, "steps": RENDER_STEPS},
        ),
        where=f"POST {path}",
        keys=("id", "prompt_id", "status"),
    )


def run(base_url: str, ffprobe: str, output_root: Path, record: dict[str, Any]) -> None:
    """Stage the chain, spend the two jobs, and measure the result into `record`.

    Every step writes what it learned into `record` before the next one runs, so whatever the
    run reaches is what the single JSON block reports.
    """
    project = create_project(base_url)
    record["project_id"] = project["id"]
    record["song"] = import_song(base_url, project["id"])
    source = import_character(base_url, project["id"])
    record["source_asset"] = {
        "id": source["id"],
        "name": source.get("name", ""),
        "source": source.get("source", ""),
        "path": source.get("path", ""),
        "uploaded_from": str(CHARACTER_IMAGE),
    }

    job = promote(base_url, project["id"], source["id"])
    promotion: dict[str, Any] = {
        "job_id": job["id"],
        "prompt_id": job.get("prompt_id", ""),
        "seed": job.get("seed"),
    }
    record["promotion"] = promotion
    note(f"  promotion job {job['id']} prompt {job.get('prompt_id')}")
    job, elapsed = poll(base_url, project["id"], job["id"])
    promotion.update(
        status=job["status"],
        error=job.get("error", ""),
        elapsed_seconds=round(elapsed, 1),
        output_files=job.get("output_files", []),
    )
    if job["status"] != "complete":
        fail(
            record,
            f"the multiview promotion ended {job['status']!r}: "
            f"{job.get('error') or 'no error text'}. No reference render was attempted",
        )

    # Re-read rather than trusting the job: the child's `path` is populated by the ordinary
    # reconciliation on refresh, and that population is the evidence promotion works.
    refreshed = expect_object(
        request(base_url, f"/api/projects/{project['id']}"),
        where=f"GET /api/projects/{project['id']}",
    )
    assets = refreshed.get("assets") if isinstance(refreshed.get("assets"), list) else []
    child = next(
        (
            item
            for item in assets
            if isinstance(item, dict) and item.get("parent_id") == source["id"]
        ),
        None,
    )
    if not child:
        fail(record, "the completed promotion left no child Asset on the project")
    resolved, unresolved = resolve_output(output_root, str(child.get("path") or ""))
    on_disk = bool(resolved and resolved.is_file())
    promotion.update(
        child_asset_id=child["id"],
        child_source=child.get("source", ""),
        child_path=child.get("path", ""),
        resolved_output=str(resolved) if resolved else "",
        output_check="verified-on-disk" if on_disk else f"not-found: {unresolved or resolved}",
    )
    if not child.get("path"):
        fail(
            record,
            "the promotion completed but the child Asset's path is still empty, so the "
            "reconciliation did not populate it and the sheet cannot be attached",
        )
    if not on_disk:
        fail(record, f"the promoted sheet does not resolve to a file: {unresolved or resolved}")

    shot = write_shot(base_url, project["id"], child["id"])
    job = submit_reference(base_url, project["id"], shot["id"])
    render: dict[str, Any] = {
        "shot_id": shot["id"],
        "job_id": job["id"],
        "prompt_id": job.get("prompt_id", ""),
        "seed": job.get("seed"),
        "requested": {
            "width": RENDER_WIDTH,
            "height": RENDER_HEIGHT,
            "steps": RENDER_STEPS,
            "duration_seconds": SHOT_DURATION_SECONDS,
            "frames": EXPECTED_FRAMES,
        },
    }
    record["reference_render"] = render
    note(f"  render job {job['id']} prompt {job.get('prompt_id')}")
    job, elapsed = poll(base_url, project["id"], job["id"])
    target, reason = choose_probe_target(job.get("output_files", []))
    render.update(
        status=job["status"],
        error=job.get("error", ""),
        elapsed_seconds=round(elapsed, 1),
        output_files=job.get("output_files", []),
        probe_target=target,
        probe_target_reason=reason,
    )
    if job["status"] != "complete":
        fail(
            record,
            f"the reference render ended {job['status']!r}: "
            f"{job.get('error') or 'no error text'}",
        )

    refreshed = expect_object(
        request(base_url, f"/api/projects/{project['id']}"),
        where=f"GET /api/projects/{project['id']}",
    )
    shots = refreshed.get("shots") if isinstance(refreshed.get("shots"), list) else []
    stored = next(
        (item for item in shots if isinstance(item, dict) and item.get("id") == shot["id"]),
        {},
    )
    render.update(
        shot_status=stored.get("status", ""),
        latest_output=stored.get("latest_output", ""),
        approved_output=stored.get("approved_output", ""),
        # The reconciliation takes `output_files[0]`; this run measures the file chosen by
        # name. Whether they agree is a fact about the app worth recording either way.
        latest_output_is_probe_target=stored.get("latest_output", "") == target,
    )
    resolved, unresolved = resolve_output(output_root, target)
    on_disk = bool(resolved and resolved.is_file())
    render["resolved_output"] = str(resolved) if resolved else ""
    render["output_check"] = (
        "verified-on-disk" if on_disk else f"not-found: {unresolved or resolved}"
    )
    if not on_disk:
        fail(record, f"the render completed but {render['output_check']}")

    measured = probe(ffprobe, str(resolved))
    video = measured.get("video") if isinstance(measured.get("video"), dict) else {}
    video_duration = as_float(
        video.get("duration_seconds"), as_float(measured.get("duration_seconds"))
    )
    audio_duration = as_float(measured.get("audio_duration_seconds"))
    render["ffprobe"] = measured
    render["measured"] = {
        "frames": as_int(video.get("frames")),
        "width": as_int(video.get("width")),
        "height": as_int(video.get("height")),
        "video_duration_seconds": round(video_duration, 3),
        "audio_duration_seconds": round(audio_duration, 3),
        "duration_delta_seconds": round(video_duration - SHOT_DURATION_SECONDS, 3),
        "sync_delta_seconds": round(audio_duration - video_duration, 3),
        "duration_tolerance_seconds": DURATION_TOLERANCE_SECONDS,
        "sync_tolerance_seconds": SYNC_TOLERANCE_SECONDS,
    }
    emit(record)

    if not video.get("present"):
        abort("the completed output carries no video stream")
    if not measured.get("decodable"):
        abort("the completed output carries no decodable audio stream")
    if (as_int(video.get("width")), as_int(video.get("height"))) != (RENDER_WIDTH, RENDER_HEIGHT):
        abort(
            f"the output measures {video.get('width')}x{video.get('height')}, not the "
            f"requested {RENDER_WIDTH}x{RENDER_HEIGHT}"
        )
    if as_int(video.get("frames")) != EXPECTED_FRAMES:
        abort(
            f"the output carries {video.get('frames')} frames, not the {EXPECTED_FRAMES} the "
            f"{SHOT_DURATION_SECONDS:g}s window lands on exactly"
        )
    if abs(video_duration - SHOT_DURATION_SECONDS) > DURATION_TOLERANCE_SECONDS:
        abort(
            f"the output measures {video_duration} s against a {SHOT_DURATION_SECONDS:g} s "
            f"request (tolerance +/-{DURATION_TOLERANCE_SECONDS} s)"
        )
    if audio_duration <= 0 or abs(audio_duration - video_duration) > SYNC_TOLERANCE_SECONDS:
        abort(
            f"the audio stream measures {audio_duration} s against {video_duration} s of video "
            f"(tolerance +/-{SYNC_TOLERANCE_SECONDS} s), so it is not synchronized"
        )
    if not render["latest_output"]:
        abort("the render completed but the Shot carries no latest_output")
    if render["approved_output"]:
        abort(
            "the render wrote approved_output, which is an editorial decision no completion "
            f"may make: {render['approved_output']!r}"
        )


def main() -> None:
    # The cost gate, ahead of every network call: this script bypasses the browser's own
    # confirmation and spends two GPU jobs, so refusing here is the only thing standing between
    # an idle keystroke and real GPU minutes on hardware nobody asked to spend.
    flags = {item for item in sys.argv[1:] if item.startswith("-")}
    positional = [item for item in sys.argv[1:] if not item.startswith("-")]
    unknown = flags - {"--confirm-gpu"}
    if unknown or "--confirm-gpu" not in flags:
        print(
            "usage: uv run python tests/smoke_h3_reference_app.py [base_url] --confirm-gpu\n"
            "\n"
            "This script promotes a character to a multiview Reference Sheet and renders a\n"
            "real H3 reference shot on the user-managed ComfyUI. That is TWO GPU jobs. It\n"
            "refuses to submit anything without --confirm-gpu.",
            file=sys.stderr,
        )
        if unknown:
            print(f"unrecognized option(s): {' '.join(sorted(unknown))}", file=sys.stderr)
        raise SystemExit(2)
    base_url = (positional[0] if positional else DEFAULT_BASE_URL).rstrip("/")

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        abort("ffprobe is not on PATH; the run cannot be measured, so nothing was submitted")
    for source in (CHARACTER_IMAGE, SONG_FILE):
        if not source.is_file():
            abort(f"a staging source is missing: {source}; nothing was submitted")

    # `comfy_root` is read from this machine's settings, so it only describes the app's output
    # root when the app runs on this machine -- and this smoke's whole evidence is the file on
    # disk. A remote app is refused up front rather than measured against the wrong tree.
    if urllib.parse.urlparse(base_url).hostname not in LOCAL_HOSTS:
        abort(
            f"the app at {base_url} is not local, so this machine's ComfyUI output root is not "
            "necessarily the app's and the rendered file cannot be measured; nothing was "
            "submitted"
        )
    output_root = (Settings().comfy_root / "output").resolve()

    # Checked against the shipped helper rather than restated: if the grid ever moves, the run
    # that would then measure something other than 90 frames never starts.
    aligned = align_h3_frames(max(5, round(SHOT_DURATION_SECONDS * H3_FRAME_RATE)))
    if aligned != EXPECTED_FRAMES:
        abort(
            f"a {SHOT_DURATION_SECONDS:g}s window now aligns to {aligned} frames, not "
            f"{EXPECTED_FRAMES}; the assertions this run makes are stale, so nothing was "
            "submitted"
        )

    comfy_url, comfy_version = check_comfy(base_url)
    note("running the H3 pre-flight audit")
    run_preflight(comfy_url)

    record: dict[str, Any] = {
        "base_url": base_url,
        "comfy_url": comfy_url,
        "comfyui_version": comfy_version,
        "project_name": PROJECT_NAME,
        "poll_ceiling_seconds": POLL_CEILING_SECONDS,
    }
    run(base_url, ffprobe, output_root, record)
    emit(record)
    note("both jobs completed; the reference path has live evidence")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
