"""Manual live smoke of both SongPlanner adapters, driven through the running app.

Not pytest-collected (like ``smoke_h3_app.py``). It spends real GPU minutes on the
user-managed ComfyUI, so it refuses to submit anything without ``--confirm-gpu``.
Run from the repo root with the app already serving and ComfyUI already up (never
started or stopped here)::

    uv run python tests/smoke_songplanner_app.py [base_url] --confirm-gpu

Order of operations: check the cost gate, locate ``ffprobe``, read ``/api/health``
and abort unless ComfyUI is online, re-run the ``tests/preflight_songplanner.py``
audit against that ComfyUI, then submit exactly two short songs -- the invented
variant first, then the known-lyrics variant, each in its own project because every
``kind=music`` job targets ``"song"`` and a shared project's second run would
clobber the first. Each job is polled to a terminal state, the project is re-read
for the path job refresh wrote, the file is resolved under ``comfy_root/output``
and probed, and the player-facing ComfyUI ``/view`` URL is fetched and probed as
well -- that URL is what the browser plays for a generated song.

Exactly one JSON block is printed per variant and it is the only thing written to
stdout: it is the only record of which adapter produced which prompt ID, because
nothing in persisted state distinguishes a SongPlanner song from a direct Music 3
song. The stored lyric sheet is the one discriminator the script can check, so the
round trip is asserted -- empty for the invented variant, byte-equal to the
submitted sheet for the known-lyrics one.

Requested duration is not produced duration -- the encoder resolves the length
itself -- so both are reported along with their delta. The delta is recorded as a
fact rather than an error, but a measurement wildly off the request (more than
``DURATION_TOLERANCE_FRACTION`` of it) fails the variant, so a truncated output
cannot pass as clean.

Several helpers here are shared rather than private: ``smoke_h3_reference_app.py``
imports ``probe``, ``resolve_output``, ``poll``, ``check_comfy``, ``request`` and the
coercions from this module. ``probe`` in particular reports both streams -- audio for the
songs below, video dimensions and frame count for the reference render -- so the two
smokes cannot disagree about what is in a container. Change them with both callers in mind.

Anything short of a clean run (pre-flight problems, ComfyUI offline, an unexpected
response shape, a job error, the time ceiling) aborts non-zero on stderr without
spending a further generation. A timeout prints no JSON block; it reports the
elapsed time and last status on stderr, and re-reads the job once after the ceiling
so a generation finishing right at the boundary is not called a failure.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import preflight_songplanner  # from this script's own directory, still on sys.path at index 1

from music_video_producer.config import Settings

DEFAULT_BASE_URL = "http://127.0.0.1:8766"
# M3SongPlanner.duration_seconds has a schema floor of 30 s, so 30 is the
# shortest song this adapter can produce (the epic's "<=16 s" target is
# unachievable; see the spec change log).
REQUESTED_DURATION = 30
# Loose on purpose: the model resolves its own length, so the real 30 -> 29.989
# case must pass and be reported as a delta. This only catches a wildly wrong
# measurement, such as a truncated output.
DURATION_TOLERANCE_FRACTION = 0.25
POLL_INTERVAL_SECONDS = 8
POLL_CEILING_SECONDS = 2400
POLL_FAILURE_TOLERANCE = 5
TERMINAL_STATUSES = {"complete", "error", "cancelled"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

VARIANTS: list[dict[str, Any]] = [
    {
        "variant": "invented",
        "project_name": "SongPlanner Invented Smoke",
        "title": "Invented Smoke",
        "idea": (
            "A short, sparse late-night synth sketch about waiting for a train that "
            "never arrives; hushed vocal, one repeated hook."
        ),
        "genre_hint": "downtempo synth pop",
        "seed": 20260816,
        "lyrics": None,
    },
    {
        "variant": "known-lyrics",
        "project_name": "SongPlanner Known Lyrics Smoke",
        "title": "Known Lyrics Smoke",
        "idea": (
            "A short, sparse late-night synth sketch about waiting for a train that "
            "never arrives; hushed vocal, one repeated hook."
        ),
        "genre_hint": "downtempo synth pop",
        "seed": 20260817,
        "lyrics": "[verse]\nthe platform light is humming low\n[chorus]\nthe train is late again",
    },
]


def note(message: str) -> None:
    """Progress for the operator watching a live run; stdout stays pure JSON."""
    print(message, file=sys.stderr, flush=True)


def abort(message: str, *, code: int = 1) -> None:
    print(f"ABORT: {message}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def as_float(value: object, default: float = 0.0) -> float:
    """``ffprobe`` reports ``N/A`` for some containers, and ``float('N/A')`` raises."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def expect_object(payload: Any, *, where: str, keys: tuple[str, ...] = ()) -> dict[str, Any]:
    """Fail loudly on an unexpected response shape rather than with a ``KeyError``.

    A traceback here would lose the prompt-ID mapping that only this script records.
    """
    if not isinstance(payload, dict):
        abort(f"{where} returned {type(payload).__name__}, expected a JSON object: "
              f"{json.dumps(payload)[:300]}")
    missing = [key for key in keys if key not in payload]
    if missing:
        abort(f"{where} response is missing {', '.join(missing)}: {json.dumps(payload)[:300]}")
    return payload


def request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    tolerant: bool = False,
) -> Any:
    """One app call. ``tolerant`` reports a failure as ``None`` instead of aborting --
    used only while polling, where a blip must not discard an in-flight generation."""
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {} if body is None else {"Content-Type": "application/json"}
    call = urllib.request.Request(f"{base_url}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(call, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        message = f"{method} {base_url}{path} returned {error.code}: {error.read().decode()[:500]}"
    except (urllib.error.URLError, OSError, ValueError) as error:
        message = f"cannot reach the application at {base_url}{path}: {error}"
    if not tolerant:
        abort(message)
    note(f"  transient: {message}")
    return None


def count_frames(ffprobe: str, target: str) -> int:
    """Decode the first video stream and count what comes out. ``0`` if that fails.

    Only called when the container declares no ``nb_frames``, which some muxers omit.
    Counting is exact where the header is merely absent, and the alternative -- deriving
    a count from duration times frame rate -- is arithmetic dressed up as a measurement,
    which is the one thing a probe run after the GPU spend must never report.
    """
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-print_format",
            "json",
            "-show_entries",
            "stream=nb_read_frames",
            target,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return 0
    try:
        data = json.loads(result.stdout or "{}")
    except ValueError:
        return 0
    streams = data.get("streams") if isinstance(data, dict) else None
    first = streams[0] if isinstance(streams, list) and streams else {}
    return as_int(first.get("nb_read_frames") if isinstance(first, dict) else 0)


def video_facts(ffprobe: str, target: str, video: dict[str, Any]) -> dict[str, Any]:
    """What the video stream measures, or ``present: False`` when the file has none.

    Reported alongside the audio facts rather than in a second probe, because the
    reference smoke needs both from the same file and two implementations of "what is in
    this container" is two answers to the question the GPU minutes were spent to settle.
    ``frames_source`` travels with the count so a number the header supplied is never
    confused with one this process decoded.
    """
    if not video:
        return {
            "present": False,
            "codec": "",
            "width": 0,
            "height": 0,
            "frames": 0,
            "frames_source": "",
            "frame_rate": "",
            "duration_seconds": 0.0,
        }
    frames = as_int(video.get("nb_frames"))
    frames_source = "nb_frames"
    if frames <= 0:
        frames = count_frames(ffprobe, target)
        frames_source = "counted" if frames > 0 else "unavailable"
    return {
        "present": True,
        "codec": video.get("codec_name", ""),
        "width": as_int(video.get("width")),
        "height": as_int(video.get("height")),
        "frames": frames,
        "frames_source": frames_source,
        "frame_rate": str(video.get("avg_frame_rate") or ""),
        "duration_seconds": round(as_float(video.get("duration")), 3),
    }


def probe(ffprobe: str, target: str) -> dict[str, Any]:
    """Measured stream facts, or the failure text if ``ffprobe`` cannot decode it.

    Every numeric field is coerced defensively: this is the one code path whose whole
    job is to report the measurement, and it runs after the GPU minutes are spent.

    Both streams are reported. ``decodable`` still means *audio* decodable, unchanged, so
    the song variants above read it exactly as before; the video stream arrives under
    ``video`` and the audio stream's own length under ``audio_duration_seconds``, which is
    what lets the reference smoke ask whether the two are actually synchronized rather
    than merely both present.
    """
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            target,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return {"decodable": False, "error": result.stderr.strip()[:500]}
    try:
        data = json.loads(result.stdout or "{}")
    except ValueError:
        return {
            "decodable": False,
            "error": f"ffprobe exited 0 with output that is not JSON: {result.stdout[:500]!r}",
        }
    if not isinstance(data, dict):
        return {
            "decodable": False,
            "error": f"ffprobe returned {type(data).__name__}, expected an object",
        }
    container = data.get("format") if isinstance(data.get("format"), dict) else {}
    streams = data.get("streams") if isinstance(data.get("streams"), list) else []
    audio = next(
        (
            item
            for item in streams
            if isinstance(item, dict) and item.get("codec_type") == "audio"
        ),
        {},
    )
    video = next(
        (
            item
            for item in streams
            if isinstance(item, dict) and item.get("codec_type") == "video"
        ),
        {},
    )
    duration = as_float(container.get("duration"), as_float(audio.get("duration")))
    return {
        "decodable": bool(audio) and duration > 0,
        "codec": audio.get("codec_name", ""),
        "sample_rate": audio.get("sample_rate", ""),
        "channels": audio.get("channels"),
        "audio_duration_seconds": round(as_float(audio.get("duration")), 3),
        "format_name": container.get("format_name", ""),
        "duration_seconds": round(duration, 3),
        "size_bytes": as_int(container.get("size")),
        "video": video_facts(ffprobe, target, video),
    }


def check_comfy(base_url: str) -> tuple[str, str]:
    """The abort gate: ``(comfy_url, comfy_version)`` only if ComfyUI is reachable."""
    note(f"reading {base_url}/api/health")
    health = expect_object(request(base_url, "/api/health"), where="GET /api/health")
    comfy = health.get("comfy")
    comfy = comfy if isinstance(comfy, dict) else {}
    comfy_url = str(comfy.get("url") or "").rstrip("/")
    if not comfy.get("online"):
        abort(
            f"ComfyUI is not reachable at {comfy_url or 'the configured URL'}: "
            f"{comfy.get('error', 'no detail reported')}. It is user-managed -- start it "
            "yourself and re-run; nothing was submitted"
        )
    stats = comfy.get("stats") if isinstance(comfy.get("stats"), dict) else {}
    system = stats.get("system") if isinstance(stats.get("system"), dict) else {}
    version = str(system.get("comfyui_version", ""))
    note(f"ComfyUI {version or 'version unknown'} online at {comfy_url}")
    return comfy_url, version


def run_preflight(comfy_url: str) -> None:
    """Reuse the audit rather than duplicating it; its ``main()`` reads ``sys.argv``."""
    saved = sys.argv
    sys.argv = [str(Path(preflight_songplanner.__file__)), comfy_url]
    try:
        preflight_songplanner.main()
    except SystemExit as error:
        if error.code:
            abort("pre-flight reported a problem; nothing was submitted")
    finally:
        sys.argv = saved


def view_url(comfy_url: str, output_path: str) -> str:
    """The URL the browser plays a generated song from (``app.js`` ``songAudioUrl``).

    Separators are normalized first: stored output paths have mixed them before, and
    splitting on ``/`` alone would fold a backslashed subfolder into the filename.
    """
    parts = [part for part in output_path.replace("\\", "/").split("/") if part]
    filename = parts.pop() if parts else ""
    query = urllib.parse.urlencode(
        {"filename": filename, "subfolder": "/".join(parts), "type": "output"}
    )
    return f"{comfy_url.rstrip('/')}/view?{query}"


def resolve_output(output_root: Path, output_path: str) -> tuple[Path | None, str]:
    """Resolve under the ComfyUI output root, refusing any path that escapes it.

    Shared with ``smoke_h3_reference_app.py``, which resolves a promoted Reference Sheet
    and a rendered shot through it, so the refusals are worded for any stored output path
    rather than only for a song's.
    """
    if not output_path:
        return None, "the project recorded no output path"
    normalized = output_path.replace("\\", "/")
    drive_qualified = len(normalized) > 1 and normalized[1] == ":"
    if normalized.startswith("/") or drive_qualified:
        return None, f"refusing an absolute output path: {output_path!r}"
    parts = PurePosixPath(normalized).parts
    if ".." in parts:
        return None, f"refusing an output path containing '..': {output_path!r}"
    target = (output_root / Path(*parts)).resolve()
    if output_root not in target.parents:
        return None, f"resolved path escapes {output_root}: {target}"
    return target, ""


def probe_view_url(ffprobe: str, url: str) -> dict[str, Any]:
    """Fetch what the player fetches and prove those exact bytes decode."""
    try:
        with urllib.request.urlopen(url, timeout=300) as response:
            status = response.status
            body = response.read()
    except (urllib.error.URLError, OSError) as error:
        return {"url": url, "fetched": False, "error": str(error)}
    with tempfile.TemporaryDirectory() as scratch:
        target = Path(scratch) / "player-fetch.audio"
        target.write_bytes(body)
        measured = probe(ffprobe, str(target))
    return {"url": url, "fetched": True, "http_status": status, "bytes": len(body), **measured}


def poll(base_url: str, project_id: str, job_id: str) -> tuple[dict[str, Any], float]:
    """Poll to a terminal state. Prints nothing to stdout, so the variant's JSON block
    stays the only record; a timeout reports elapsed time and last status on stderr."""
    path = f"/api/projects/{project_id}/jobs/{job_id}"
    where = f"GET {path}"
    started = time.monotonic()
    last_status = "unread"
    failures = 0
    while time.monotonic() - started < POLL_CEILING_SECONDS:
        job = request(base_url, path, tolerant=True)
        if job is None:
            failures += 1
            if failures >= POLL_FAILURE_TOLERANCE:
                abort(
                    f"job {job_id} status could not be read {failures} times in a row; "
                    "the generation may still be running in ComfyUI, which was left untouched"
                )
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        failures = 0
        job = expect_object(job, where=where, keys=("id", "status"))
        if job["status"] != last_status:
            last_status = job["status"]
            note(f"  job {job_id}: {last_status} ({round(time.monotonic() - started)} s)")
        if job["status"] in TERMINAL_STATUSES:
            return job, time.monotonic() - started
        time.sleep(POLL_INTERVAL_SECONDS)
    # One last read: a generation that finishes right at the ceiling is not a failure.
    final = request(base_url, path, tolerant=True)
    if final is not None:
        final = expect_object(final, where=where, keys=("id", "status"))
        last_status = final["status"]
        if last_status in TERMINAL_STATUSES:
            note(f"  job {job_id}: {last_status} at the ceiling")
            return final, time.monotonic() - started
    elapsed = time.monotonic() - started
    abort(
        f"job {job_id} was still {last_status!r} after {round(elapsed, 1)} s "
        f"(ceiling {POLL_CEILING_SECONDS} s); no JSON block was printed for this variant. "
        "The generation may still be running in ComfyUI, which was left untouched"
    )
    return {}, elapsed


def run_variant(
    variant: dict[str, Any],
    *,
    base_url: str,
    comfy_url: str,
    comfy_version: str,
    output_root: Path,
    output_root_is_the_app_s: bool,
    ffprobe: str,
) -> None:
    label = variant["variant"]
    note(f"[{label}] creating project {variant['project_name']!r}")
    project = expect_object(
        request(base_url, "/api/projects", method="POST", payload={"name": variant["project_name"]}),
        where="POST /api/projects",
        keys=("id",),
    )
    body = {
        "title": variant["title"],
        "idea": variant["idea"],
        "genre_hint": variant["genre_hint"],
        "duration": REQUESTED_DURATION,
        "seed": variant["seed"],
    }
    if variant["lyrics"] is not None:
        body["lyrics"] = variant["lyrics"]
    note(f"[{label}] submitting {REQUESTED_DURATION} s song -- GPU spend starts here")
    submit_path = f"/api/projects/{project['id']}/generate/songplanner"
    job = expect_object(
        request(base_url, submit_path, method="POST", payload=body),
        where=f"POST {submit_path}",
        keys=("id", "prompt_id", "status"),
    )
    # Printed before polling: nothing in persisted state marks a song as SongPlanner's,
    # so this mapping must survive even if a later step fails.
    note(
        f"[{label}] project {project['id']} job {job['id']} "
        f"prompt {job['prompt_id']} seed {job.get('seed')}"
    )
    job, elapsed = poll(base_url, project["id"], job["id"])
    refreshed = expect_object(
        request(base_url, f"/api/projects/{project['id']}"),
        where=f"GET /api/projects/{project['id']}",
    )
    song = refreshed.get("song") if isinstance(refreshed.get("song"), dict) else {}
    output_path = str(song.get("path") or "")
    resolved, unresolved_reason = resolve_output(output_root, output_path)
    on_disk = bool(resolved and resolved.is_file())
    if on_disk:
        disk_check = "verified-on-disk"
    elif not output_root_is_the_app_s:
        disk_check = (
            f"skipped: the app at {base_url} is not local, so {output_root} is this "
            "machine's ComfyUI output root and not necessarily the app's; the player URL "
            "below carries the measurement instead"
        )
    else:
        disk_check = f"not-found: {unresolved_reason or f'no file at {resolved}'}"

    # The one discriminator available: the route stores the submitted sheet verbatim
    # apart from edge whitespace, and stores "" for the invented variant.
    expected_lyrics = variant["lyrics"] or ""
    stored_lyrics = song.get("lyrics") if isinstance(song.get("lyrics"), str) else ""

    measurement = probe(ffprobe, str(resolved)) if on_disk else {}
    player = probe_view_url(ffprobe, view_url(comfy_url, output_path)) if output_path else {}
    measured_duration = as_float(
        measurement.get("duration_seconds"), as_float(player.get("duration_seconds"))
    )
    record: dict[str, Any] = {
        "variant": label,
        "adapter": (
            "build_songplanner_known_lyrics_payload"
            if variant["lyrics"] is not None
            else "build_songplanner_invented_payload"
        ),
        "project_id": project["id"],
        "project_name": variant["project_name"],
        "job_id": job["id"],
        "prompt_id": job.get("prompt_id", ""),
        "seed": job.get("seed"),
        "status": job["status"],
        "error": job.get("error", ""),
        "elapsed_seconds": round(elapsed, 1),
        "comfyui_version": comfy_version,
        "requested_duration_seconds": REQUESTED_DURATION,
        "song_duration_field": song.get("duration"),
        "measured_duration_seconds": round(measured_duration, 3),
        # Recorded as a fact, not an error: the encoder resolves its own length.
        "duration_delta_seconds": round(measured_duration - REQUESTED_DURATION, 3),
        "duration_tolerance_seconds": round(REQUESTED_DURATION * DURATION_TOLERANCE_FRACTION, 3),
        "lyrics_round_trip": {
            "expected_chars": len(expected_lyrics),
            "stored_chars": len(stored_lyrics),
            "matches": stored_lyrics == expected_lyrics,
        },
        "job_output_files": job.get("output_files", []),
        "song_path": output_path,
        "resolved_output": str(resolved) if resolved else "",
        "output_check": disk_check,
        "output_root_is_the_app_s": output_root_is_the_app_s,
        "ffprobe": measurement,
        "player": player,
    }
    print(json.dumps(record, indent=2), flush=True)

    if job["status"] != "complete":
        abort(f"{label} job ended {job['status']!r}: {job.get('error') or 'no error text'}")
    if not record["lyrics_round_trip"]["matches"]:
        abort(
            f"{label} lyric round trip failed: submitted {len(expected_lyrics)} chars, "
            f"the project stored {len(stored_lyrics)}"
        )
    if output_root_is_the_app_s and not on_disk:
        abort(f"{label} completed but {disk_check}")
    if on_disk and not measurement.get("decodable"):
        abort(f"{label} output is not decodable audio")
    if not player.get("decodable"):
        abort(f"{label} player URL did not return decodable audio")
    if measured_duration <= 0:
        abort(f"{label} produced no measurable duration")
    if abs(record["duration_delta_seconds"]) > record["duration_tolerance_seconds"]:
        abort(
            f"{label} measured {measured_duration} s against a {REQUESTED_DURATION} s request "
            f"(delta {record['duration_delta_seconds']} s, tolerance "
            f"±{record['duration_tolerance_seconds']} s) -- the output looks wrong, not merely "
            "model-resolved"
        )


def main() -> None:
    flags = {item for item in sys.argv[1:] if item.startswith("-")}
    positional = [item for item in sys.argv[1:] if not item.startswith("-")]
    unknown = flags - {"--confirm-gpu"}
    if unknown or "--confirm-gpu" not in flags:
        print(
            "usage: uv run python tests/smoke_songplanner_app.py [base_url] --confirm-gpu\n"
            "\n"
            "This script generates two real songs on the user-managed ComfyUI and costs\n"
            "GPU minutes. It refuses to submit anything without --confirm-gpu.",
            file=sys.stderr,
        )
        if unknown:
            print(f"unrecognized option(s): {' '.join(sorted(unknown))}", file=sys.stderr)
        raise SystemExit(2)
    base_url = (positional[0] if positional else DEFAULT_BASE_URL).rstrip("/")

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        abort("ffprobe is not on PATH; the run cannot be measured, so nothing was submitted")

    # comfy_root is read from this machine's settings, so it only describes the app's
    # output root when the app is running on this machine.
    output_root = (Settings().comfy_root / "output").resolve()
    output_root_is_the_app_s = urllib.parse.urlparse(base_url).hostname in LOCAL_HOSTS
    if not output_root_is_the_app_s:
        note(
            f"the app at {base_url} is not local: {output_root} is this machine's ComfyUI "
            "output root, so the on-disk check will be skipped rather than reported false"
        )
    comfy_url, comfy_version = check_comfy(base_url)

    note("running the SongPlanner pre-flight audit")
    run_preflight(comfy_url)

    for variant in VARIANTS:
        run_variant(
            variant,
            base_url=base_url,
            comfy_url=comfy_url,
            comfy_version=comfy_version,
            output_root=output_root,
            output_root_is_the_app_s=output_root_is_the_app_s,
            ffprobe=ffprobe,
        )
    note("both variants completed")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
