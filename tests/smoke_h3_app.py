"""Manual live smoke of the text-only H3 Director path, driven through the running app.

Not pytest-collected (like ``smoke_songplanner_app.py``). It spends real GPU minutes on
the user-managed ComfyUI, so it refuses to submit anything without ``--confirm-gpu``.
Run from the repo root with the app already serving and ComfyUI already up (never
started or stopped here)::

    uv run python tests/smoke_h3_app.py [base_url] --confirm-gpu

Order of operations is the one ``smoke_songplanner_app.py`` settled on, and it exists so
every abort lands ahead of the GPU spend: check the cost gate before any network call at
all, read ``/api/health`` and abort unless ComfyUI is online, re-run the
``tests/preflight_h3_ultra.py`` audit against that ComfyUI, and only then create a
project, write one 4-second text-mode shot, and submit it. The job is polled to a
terminal state and exactly one JSON block goes to stdout -- the record of which prompt ID
this run produced. Everything else, the audit's own report included, is redirected to
stderr so that block stays the only thing on stdout.

What the audit does and does not prove about *this* payload is worth stating plainly,
because the H3 pre-flight was written for the references-to-video graph while this script
submits the Director graph. Every node class the Director payload uses except
``MiniMaxH3DirectorCS`` is audited, as are three of its four model files -- the ``fl2va``
checkpoint is only loaded by this path, so its absence is still discovered the expensive
way. That leaves the audit a cheap check on the H3 custom nodes being installed and
loadable rather than a full validation of what is submitted here; it is called through
``preflight_h3_ultra.main()`` rather than by reaching into the audit's internals, so
whatever that audit learns to check later applies here without a change.
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# From this script's own directory, which is sys.path[0] when it is run as a script.
# Importing it also puts `src` on the path, but this script needs nothing from there.
import preflight_h3_ultra

DEFAULT_BASE_URL = "http://127.0.0.1:8766"
POLL_INTERVAL_SECONDS = 8
POLL_CEILING_SECONDS = 1200
TERMINAL_STATUSES = {"complete", "error", "cancelled"}


def note(message: str) -> None:
    """Progress for the operator watching a live run; stdout stays the JSON block."""
    print(message, file=sys.stderr, flush=True)


def abort(message: str, *, code: int = 1) -> None:
    print(f"ABORT: {message}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def request(base_url: str, path: str, *, method: str = "GET", payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {} if body is None else {"Content-Type": "application/json"}
    req = urllib.request.Request(f"{base_url}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"{method} {path}: {error.code} {error.read().decode()}") from error


def check_comfy(base_url: str) -> str:
    """The abort gate: the ComfyUI URL, and only if ComfyUI is actually reachable.

    The app is asked rather than this machine's settings, because the audit below has to
    run against the same ComfyUI the render would be submitted to.
    """
    note(f"reading {base_url}/api/health")
    health = request(base_url, "/api/health")
    comfy = health.get("comfy") if isinstance(health.get("comfy"), dict) else {}
    comfy_url = str(comfy.get("url") or "").rstrip("/")
    if not comfy.get("online"):
        abort(
            f"ComfyUI is not reachable at {comfy_url or 'the configured URL'}: "
            f"{comfy.get('error', 'no detail reported')}. It is user-managed -- start it "
            "yourself and re-run; nothing was submitted"
        )
    if not comfy_url:
        abort("/api/health reports ComfyUI online but names no URL to audit against")
    stats = comfy.get("stats") if isinstance(comfy.get("stats"), dict) else {}
    system = stats.get("system") if isinstance(stats.get("system"), dict) else {}
    note(f"ComfyUI {system.get('comfyui_version') or 'version unknown'} online at {comfy_url}")
    return comfy_url


def run_preflight(comfy_url: str) -> None:
    """Reuse the audit rather than duplicating it; its ``main()`` reads ``sys.argv``.

    Its report is printed to stdout, so it is redirected here: a passing audit's ``OK``
    line would otherwise sit in front of the JSON block that is this script's whole
    machine-readable output, and a failing audit's ``FAIL`` lines belong with the other
    aborts on stderr.
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


def main() -> None:
    # The cost gate, ahead of every network call: this script bypasses the browser's own
    # confirmation, so refusing here is the only thing standing between an idle keystroke
    # and real GPU minutes on hardware nobody asked to spend.
    flags = {item for item in sys.argv[1:] if item.startswith("-")}
    positional = [item for item in sys.argv[1:] if not item.startswith("-")]
    unknown = flags - {"--confirm-gpu"}
    if unknown or "--confirm-gpu" not in flags:
        print(
            "usage: uv run python tests/smoke_h3_app.py [base_url] --confirm-gpu\n"
            "\n"
            "This script renders a real 4-second H3 shot on the user-managed ComfyUI and\n"
            "costs GPU minutes. It refuses to submit anything without --confirm-gpu.",
            file=sys.stderr,
        )
        if unknown:
            print(f"unrecognized option(s): {' '.join(sorted(unknown))}", file=sys.stderr)
        raise SystemExit(2)
    base_url = (positional[0] if positional else DEFAULT_BASE_URL).rstrip("/")

    comfy_url = check_comfy(base_url)
    note("running the H3 pre-flight audit")
    run_preflight(comfy_url)

    project = request(base_url, "/api/projects", method="POST", payload={"name": "H3 Text Smoke QA"})
    shot = {
        "start": 0,
        "duration": 4,
        "prompt": (
            "A cinematic medium shot of a solo singer under one amber warehouse light, "
            "subtle breathing and a slow camera push, stable face and background, one continuous take."
        ),
        "mode": "text",
        "asset_ids": [],
        "seed": 260816,
        "status": "ready",
    }
    project = request(
        base_url,
        f"/api/projects/{project['id']}/shots",
        method="PUT",
        payload={"shots": [shot]},
    )
    shot_id = project["shots"][0]["id"]
    note("submitting the H3 shot -- GPU spend starts here")
    job = request(
        base_url,
        f"/api/projects/{project['id']}/shots/{shot_id}/generate/h3",
        method="POST",
        payload={"width": 768, "height": 448, "steps": 4},
    )
    started = time.monotonic()
    while time.monotonic() - started < POLL_CEILING_SECONDS:
        current = request(base_url, f"/api/projects/{project['id']}/jobs/{job['id']}")
        if current["status"] in TERMINAL_STATUSES:
            print(
                json.dumps(
                    {
                        "project_id": project["id"],
                        "shot_id": shot_id,
                        "job_id": job["id"],
                        "prompt_id": current["prompt_id"],
                        "status": current["status"],
                        "outputs": current["output_files"],
                        "error": current["error"],
                        "elapsed_seconds": round(time.monotonic() - started, 1),
                    },
                    indent=2,
                )
            )
            raise SystemExit(0 if current["status"] == "complete" else 1)
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError("H3 smoke job did not reach a terminal state within 20 minutes")


if __name__ == "__main__":
    main()
