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
the assertion needs no allowance for.

**READ THIS BEFORE CHANGING A NUMBER IN THIS FILE.**

Three times now, a value chosen here to make this run cheap has been mistaken for a property
of the system, and every conclusion drawn afterwards was void:

1. **4 steps.** Hardcoded against the 20-step default profile, producing an undersampled
   frame that said nothing about picture quality. The turbo profile's LoRA was then adopted
   partly on the strength of it.
2. **``start = 0.0``.** Chosen because a shot at the start of the song is the simplest thing
   to write. It is also the one start whose correct window covers the same seconds the buggy
   whole-file reference began with -- so the reference path never sending an offset at all
   survived three live renders and a schema audit. It was found by ear, not by this file.
   Worse, on this particular master the first 3.75 s are instrumental: H3 was conditioned on
   wordless intro atmospherics and regenerated exactly that, which is what the Director heard
   as "voices but no phonetics".
3. **640x384.** 0.25 MP, chosen to save GPU minutes, against the 0.6 MP the Director's own
   pipeline uses. On a full-body framing the face occupies the same *fraction* of frame
   either way, so ours landed on tens of pixels of height. Every quality judgement this
   project recorded -- the 4-step versus 20-step comparison, the turbo assessment, the
   "coherent cinematic frame" verdict -- was made at a resolution where facial detail cannot
   survive, and none of them stands.

The pattern is the same each time and nothing in a passing test says it: **the fixture was
not representative, and the test could not tell.** So before changing any constant above, ask
the question that would have caught all three -- *which value would make this run pass even if
the code were wrong?* -- and if the answer is the value you are about to write, cover a second
one as well.

Concretely, today: the geometry is **not** pinned here, it is read from ``select_resolution``,
so this run measures the frame a Director actually gets. The start is **12 s**, past this
track's intro, not 0. The step count comes from the profile. If you find yourself adding a
``width``, a ``steps`` or a ``start`` of 0 back into the request to make a run cheaper, you
are writing item 4.

The **step count is taken from the sampling profile, not chosen here.** The first run of this
script hardcoded a 4-step override against the default profile -- a 20-step graph with no
LoRA -- and produced a badly undersampled frame that said nothing about the path's picture
quality. Four steps is what the *turbo* profile's LoRA bundle was trained for, so this run
names ``turbo`` and lets the profile supply its own count. Anyone wanting the audited
20-step graph should switch ``RENDER_PROFILE`` rather than the number: the two travel
together, and a step count typed in here is exactly the mistake that is not worth repeating.

**What was sampled is read back from ComfyUI, not asserted from the constants.** After the
job settles this reads ``/history/{prompt_id}`` and reports the LoRA, strength, scheduler,
sampler and step count out of the graph the server recorded, beside what the profile
declares. A profile that was accepted and then silently dropped would otherwise produce a
run that completes, measures correctly, and prints ``turbo`` over a render that used none.
The read is best-effort -- the render has already been measured by then, so a failed
supplementary read is recorded as a gap rather than treated as a failed run -- but a
mismatch it *can* see is fatal.

The staged media is the project's real asset library under ComfyUI's ``input/music-video/``
rather than anything this script invents: the character image from ``characters/`` and the
master song from ``audio/``. One picture reference plus the song is deliberate -- it is the
minimum window that exercises both the ``<Picture 1>`` tag and the ``<Audio N> is the master
song for synchronization`` tag the route appends for ``use_song_audio``, and a second
reference would change what the render proves without making it prove more. The library also
holds a location image; it is knowingly left unattached.

The master song runs far longer than the Shot window, and the route now hands the media loader
the Shot's own window -- ``{"trim": {"start": 12.0, "end": 15.75}}`` -- rather than the whole
file. That reference audio is **conditioning**, not the output track: ``MiniMaxH3ReferenceToVideo``
encodes it into audio conditioning tokens, and the muxed ``-audio.mp4`` this run measures is
decoded from the sampler's own latent, exactly as the canonical exports wire it. So the output
audio is a *regeneration* of the conditioned window and is not expected to resemble the master
track -- measured at ~0.01 correlation against it, and 3.4x louder. Nothing here should ever
assert that it does. Muxing the real track back over a finished cut is a separate pipeline step
(the Director's ``LTX2.5 AudioReplacer`` graph) that this application does not yet have.

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
from music_video_producer.workflows import (
    H3_FRAME_RATE,
    H3_REFERENCE_PROFILES,
    select_resolution,
    song_audio_window,
)

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
#: The count this used to open with ("in four views") is gone and the views it names are not:
#: a probe asked the QuadView LoRA for four and got six, so the number was a prediction about
#: the output rather than part of the request. Everything the sentence asks for is unchanged.
MULTIVIEW_PROMPT = (
    "Character sheet of the same person on a plain neutral backdrop: front, "
    "three-quarter, profile, and back. Identical face, hair, and wardrobe in every view, even "
    "studio lighting, full body, no text."
)
MULTIVIEW_SEED = 20260818

SHOT_PROMPT = (
    "The character from the reference sheet sings to camera in a dim warehouse under one amber "
    "light, slow push in, stable face and wardrobe, one continuous take."
)
#: **Not 0.0, and never again 0.0 without a second run that is.** 12 s is past this master
#: track's instrumental intro -- the Director places the first sung words at about 8-10 s --
#: so the reference audio handed to H3 contains phonemes to sync to. At 0.0 it did not, and
#: 0.0 is also the one start where a missing offset and a correct one produce identical
#: bytes, which is exactly why three live renders passed over the defect. See the module
#: docstring.
SHOT_START_SECONDS = 12.0
#: 3.75 s is exactly 90 frames on the 17k+5 grid; see the module docstring.
SHOT_DURATION_SECONDS = 3.75
EXPECTED_FRAMES = 90
SHOT_SEED = 20260819
#: The frame the *application* selects when nothing asks for one -- 0.6 MP at 16:9 on a
#: multiple of 32, which is 1056x608. Derived from `select_resolution` rather than typed, so
#: this run cannot pin a size the route has stopped producing: the assertion below is that
#: the render came back at the default, not that it came back at two numbers written here.
RENDER_WIDTH, RENDER_HEIGHT = select_resolution()
#: Which evidenced sampling bundle this run submits. The route takes the name; the step
#: count below is read back from the same table only so the printed record can say what was
#: requested. Nothing here sends a step count, so the server's answer and this number cannot
#: disagree -- and changing the profile changes both together, which is the whole point.
RENDER_PROFILE = "turbo"
RENDER_STEPS = H3_REFERENCE_PROFILES[RENDER_PROFILE].steps
#: The body the render submission sends, named rather than inlined so a test can assert what
#: it carries without opening a socket.
#:
#: **No geometry and no step count.** Both were once written here, both were cost-saving
#: choices, and both became the numbers every conclusion about this pipeline was drawn from.
#: The request now names only the sampling profile, so the frame is the application's own
#: selection and the step count is the profile's -- and what this run measures is what a
#: Director pressing render actually gets. A `width`, a `height` or a `steps` key added back
#: here re-creates the defect, whatever number it carries.
RENDER_REQUEST = {"profile": RENDER_PROFILE}

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


def graph_from_history(entry: Any) -> tuple[dict, str]:
    """The graph ComfyUI recorded for one prompt, or ``({}, reason)``.

    ComfyUI stores ``entry["prompt"]`` as ``[number, prompt_id, graph, extra_data,
    outputs_to_execute]``. The graph is found by *shape* -- the one member that is a
    mapping of nodes carrying ``class_type`` -- rather than by index, so a changed tuple
    layout degrades to a reported reason instead of raising, or worse, silently reading
    the wrong dict as a graph.
    """
    if not isinstance(entry, dict):
        return {}, f"history entry is {type(entry).__name__}, not an object"
    parts = entry.get("prompt")
    if not isinstance(parts, list):
        return {}, "history entry carries no prompt list"
    for part in parts:
        if isinstance(part, dict) and any(
            isinstance(node, dict) and "class_type" in node for node in part.values()
        ):
            return part, ""
    return {}, "the history entry's prompt carries no node graph"


def submitted_sampling(graph: dict) -> dict[str, Any]:
    """What was *actually* sampled, read out of the graph ComfyUI recorded.

    The point of reading it back rather than printing `H3_REFERENCE_PROFILES[...]` is that
    the constants describe what the profile *means*, not what the server built. A profile
    that never reached the builder -- silently dropped, or applied to a graph that has no
    profile -- would leave the constants saying `turbo` over a render that used none, and
    the record would be a confident lie about a GPU job. These values come from the
    submission itself, so they cannot disagree with it.

    A class appearing more than once makes its entry empty rather than picking the first:
    this graph has exactly one scheduler, one sampler and at most one LoRA, so two is a
    graph this function does not understand and must not summarise.
    """
    def only(class_type: str) -> dict:
        found = [
            node.get("inputs", {})
            for node in graph.values()
            if isinstance(node, dict) and node.get("class_type") == class_type
        ]
        return found[0] if len(found) == 1 and isinstance(found[0], dict) else {}

    lora, scheduler, sampler = only("LoraLoaderModelOnly"), only("BasicScheduler"), only("KSamplerSelect")
    return {
        "lora": lora.get("lora_name", ""),
        "lora_strength": lora.get("strength_model"),
        "scheduler": scheduler.get("scheduler", ""),
        "sampler": sampler.get("sampler_name", ""),
        "steps": scheduler.get("steps"),
    }


def graph_node(graph: dict, class_type: str) -> dict:
    """The one node of ``class_type``, or an empty mapping if there is not exactly one."""
    found = [
        node.get("inputs", {})
        for node in graph.values()
        if isinstance(node, dict) and node.get("class_type") == class_type
    ]
    return found[0] if len(found) == 1 and isinstance(found[0], dict) else {}


def submitted_geometry(graph: dict) -> dict[str, Any]:
    """The frame the server was actually given, read back for `submitted_sampling`'s reason.

    The request now sends no width or height at all, so the size is the application's
    selection rather than anything this file typed. Reading it out of the recorded graph is
    what makes the printed record a fact about the render instead of a restatement of a
    constant -- and it is the number the frame comparison is against.
    """
    conditioner = graph_node(graph, "MiniMaxH3ReferenceToVideo")
    return {
        "width": conditioner.get("width"),
        "height": conditioner.get("height"),
        "length": conditioner.get("length"),
    }


def submitted_song_window(graph: dict) -> dict[str, Any]:
    """The window the master song was handed, read out of the media loader's own state.

    This is the whole point of the run. The window lives in `MiniMaxH3MediaLoader`'s
    `media_state` -- `MiniMaxH3ReferenceToVideo` has no window input of any kind -- and a
    `trim` the loader cannot read is dropped silently, so the only way to know a window
    reached the model is to read back what the server recorded.

    An empty mapping means no master-song reference was found; `{"trim": None}` means one
    was found carrying no window, which at a non-zero start is the defect this run exists
    to catch.
    """
    loader = graph_node(graph, "MiniMaxH3MediaLoader")
    try:
        items = json.loads(loader.get("media_state") or "[]")
    except (TypeError, ValueError):
        return {}
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("label") == "master song":
            return {"file": item.get("file"), "trim": item.get("trim")}
    return {}


def profile_declares(name: str) -> dict[str, Any]:
    """The same five fields as ``submitted_sampling``, from the adapter's own constants.

    Printed next to the submitted ones so the record shows both and the comparison is
    visible rather than asserted out of view.
    """
    profile = H3_REFERENCE_PROFILES[name]
    return {
        "lora": profile.lora or "",
        "lora_strength": profile.lora_strength,
        "scheduler": profile.scheduler,
        "sampler": profile.sampler,
        "steps": profile.steps,
    }


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
        f"submitting the reference render at {RENDER_WIDTH}x{RENDER_HEIGHT} on the "
        f"{RENDER_PROFILE!r} profile ({RENDER_STEPS} steps) -- GPU spend (job 2 of 2)"
    )
    path = f"/api/projects/{project_id}/shots/{shot_id}/generate/h3"
    return expect_object(
        request(
            base_url,
            path,
            method="POST",
            # No `steps`: the profile carries its own, and an override here is what
            # undersampled the first run. See the module docstring.
            payload=RENDER_REQUEST,
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
        # Exactly the body that was POSTed, and nothing inferred. What the profile *means*
        # is reported separately below, next to what the server actually built.
        "requested": {
            **RENDER_REQUEST,
            "duration_seconds": SHOT_DURATION_SECONDS,
            "frames": EXPECTED_FRAMES,
        },
        "profile_declares": profile_declares(RENDER_PROFILE),
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

    # What the server actually built for this prompt, read back out of ComfyUI's own
    # record. `tolerant=True` because the render has already happened and been measured: a
    # supplementary read that fails is a gap in the record, not a reason to discard the
    # run -- and the reason it failed travels into the JSON block either way. Read before
    # the completion check so a *failed* render still says what was submitted, which is
    # exactly when that is worth knowing.
    comfy_url = str(record.get("comfy_url", "")).rstrip("/")
    prompt_id = str(job.get("prompt_id") or "")
    history = (
        request(comfy_url, f"/history/{prompt_id}", tolerant=True)
        if comfy_url and prompt_id
        else None
    )
    graph, why = graph_from_history(
        history.get(prompt_id) if isinstance(history, dict) else history
    )
    render["submitted_sampling"] = submitted_sampling(graph) if graph else {}
    render["submitted_sampling_source"] = (
        f"ComfyUI /history/{prompt_id}" if graph else f"unavailable: {why}"
    )
    render["submitted_geometry"] = submitted_geometry(graph) if graph else {}
    render["submitted_song_window"] = submitted_song_window(graph) if graph else {}

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

    # The profile reached the graph, or it did not. Nothing else in this run would notice:
    # a profile that was accepted and silently dropped produces a render that completes,
    # measures correctly, and is indistinguishable from the default one afterwards.
    submitted = render["submitted_sampling"]
    if not submitted:
        note(f"  sampling not confirmed: {render['submitted_sampling_source']}")
    elif submitted != render["profile_declares"]:
        abort(
            f"the {RENDER_PROFILE!r} profile was requested but ComfyUI recorded "
            f"{json.dumps(submitted)} where the profile declares "
            f"{json.dumps(render['profile_declares'])}, so the render is not the "
            f"configuration this run claims to have measured"
        )
    # The window the master song was actually handed, checked the same way and for the same
    # reason. A `trim` the loader cannot read is dropped *silently*, so a run that completed
    # and measured correctly is not evidence that the model heard the right seconds -- only
    # the recorded graph is. This assertion is the one this run exists for.
    window = render["submitted_song_window"]
    expected_window = song_audio_window(
        start=SHOT_START_SECONDS, duration=SHOT_DURATION_SECONDS, song_duration=0
    )
    if not window:
        note(f"  song window not confirmed: {render['submitted_sampling_source']}")
    elif window.get("trim") != expected_window:
        abort(
            f"the master song was submitted with trim {json.dumps(window.get('trim'))} where "
            f"a {SHOT_START_SECONDS:g}s shot needs {json.dumps(expected_window)}; the render "
            f"heard a different part of the song than the one this run claims to have measured"
        )
    geometry = render["submitted_geometry"]
    if geometry and (geometry.get("width"), geometry.get("height")) != (
        RENDER_WIDTH,
        RENDER_HEIGHT,
    ):
        abort(
            f"the conditioner was submitted at {geometry.get('width')}x{geometry.get('height')} "
            f"where the application's own selection is {RENDER_WIDTH}x{RENDER_HEIGHT}"
        )
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

    # The audio window, checked the same way and for the same reason. Every shot now carries
    # one, 0 s included, so the guard is on the *fixture* rather than on the return value: a
    # run at 0 s would still send a window and still pass, and would still tell us nothing --
    # 0 s is the start where a shot's window and the first seconds of the track are the same
    # seconds, so no offset can be observed. Song length is checked at the route.
    if SHOT_START_SECONDS <= 0:
        abort(
            "SHOT_START_SECONDS is 0, the one start whose window is indistinguishable from no "
            "offset at all, so this run would prove nothing about which part of the song was "
            "heard; nothing was submitted. See the module docstring."
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
