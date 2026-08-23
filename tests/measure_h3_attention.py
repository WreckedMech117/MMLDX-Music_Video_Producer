"""Measure what an H3 attention backend costs, and what it does to the mouth.

Not pytest-collected (like ``smoke_h3_reference_app.py``, whose gate and abort order it
copies). It spends **real GPU minutes per profile** on the user-managed ComfyUI, so it
refuses to submit anything without ``--confirm-gpu``. Run from the repo root with ComfyUI
already up (never started or stopped here)::

    # screen a backend cheaply first, then promote the survivors
    uv run python tests/measure_h3_attention.py --project project_xxxx --shot shot_yyyy \\
        --frames 107 --profiles default,comfy-kitchen --confirm-gpu
    # the bundles that decide the window ceiling
    uv run python tests/measure_h3_attention.py --project project_xxxx --shot shot_yyyy \\
        --sampling turbo-references2v,turbo --profiles default --confirm-gpu

**First run 2026-08-21/22.** Findings are in
`_bmad-output/planning-artifacts/h3-attention-backend-experiment.md` §6.

What it measures and why
------------------------

`src/music_video_producer/app.py`'s `POPULATE_MAX_WINDOW_SECONDS` records a render-cost
cliff reconstructed from output-file mtimes over the serial overnight batch of 2026-08-19/20:
about 2.58 s/frame at 107 frames, drifting to 2.89 at 158, then **8.02 at 226** and 8.47 at
277. This harness was built for the second of its three caveats — "the acceleration was never
a controlled variable" — and the first thing it established is that **that caveat was wrong**:
ComfyUI is launched `--use-sage-attention`, the adapters' `sage_attention: "disabled"` writes
no override at all, and so every timed render inherited SageAttention. The acceleration was
never off.

So the arms are backends *against each other*, not against an unaccelerated baseline. Each
renders the same frame count from one shot, one seed, one prompt, one set of references, one
window and one geometry — the payloads are diffed against each other before anything is
submitted, and the run aborts unless the only difference within a sampling bundle is the
attention node.

**Screen before you commit.** `--frames 107` costs about four minutes an arm and answers
"does this backend load, engage and sample at all"; only survivors are worth `--frames 226`,
the cliff point. That order was learned by spending 97 minutes discovering that plain PyTorch
attention does not fit 226 frames on a 32 GB card — the finding is in §6.9, and five minutes
at 107 would have suggested it.

**An arm can fail, and a failure is a result.** `should_cut` ends an arm that has entered the
characterised memory-bound signature — past 1.5x a completed arm with no `loaded completely`
line, zero websocket progress frames across a three-minute listen, and under 200 W at high
reported utilisation — and records it as `did-not-complete` with the evidence and **no
timing**, because an arm that sampled no steps has no cost to put in a cost column.

Baseline is re-measured rather than reused. ComfyUI moved from 0.33.1 to 0.33.3 underneath
the old numbers and the card's driver may have moved too, so the ``default`` profile is an
arm of this experiment like any other. Nothing in the old table is carried forward.

**It is resumable, and it has to be.** Every arm writes its record the moment it lands, a
later invocation reuses what is already on disk, and `--adopt PROMPT_ID` takes over a render
that a killed run left executing in ComfyUI. A multi-hour experiment can be run one arm per
invocation, which is what makes it survivable.

The thing the numbers are not allowed to decide alone
-----------------------------------------------------

On 2026-08-18 the LTX enhancer sharpened a take beautifully and **moved the mouth**: sampled
at frames 20, 44 and 70, the source was mid-vowel with teeth visible while the enhanced clip
had the mouth closed, consistently rather than as a one-off. Nothing about that was
predictable from the graph (`docs/DEVELOPMENT-LOG.md`, 2026-08-18). Attention is a deeper
change than a sigma schedule, and two of the candidates here — Comfy Kitchen's int8 attention
and SageAttention's int8/fp8 kernels — are *quantised* attention. Quantised attention that is
30% faster and half a phoneme late is not a win for a project whose premise is H3's
audio-driven lip-sync.

So this harness follows that investigation's method rather than inventing one:

* every output is **preserved**, whole, one file per profile per repeat, and nothing is
  written back to any Shot or project;
* frames are sampled at **fixed indices across profiles**, so the same instant of the same
  performance is compared;
* the indices include the enhancer's own **20, 44 and 70**, which makes the two
  investigations comparable, plus 113, 180 and 225 because a 226-frame render's second half
  is where drift would accumulate and the enhancer's clip had no second half;
* each index becomes **one contact sheet with every profile side by side**, the shape of
  `test-artifacts/2026-08-18-render-comparisons/mouth_compare.jpg`, because lip position is
  judged across arms at one instant and by eye. There is no metric here and this harness
  invents none. It produces the evidence; the Director rules.

What it does not do
-------------------

It does not decide. It prints a table and writes `report.json`, and the entire question of
which backend this project should ship is a Director ruling on numbers plus mouths, not a
threshold in this file.

It does not touch the application. The payloads are built by the shipped
`build_h3_reference_payload` and submitted straight to ComfyUI, so no route, no manifest and
no Shot is involved — which is also why `MVP_SAGE_ATTENTION` is refused rather than honoured:
the app's submission-time choke point rewrites every `PathchSageAttentionKJ.sage_attention`
and is not in this path, so a Director who has it set would read profile names in the report
that do not describe what the app would send.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from preflight import repo_src_on_path

repo_src_on_path()

# Imported after `repo_src_on_path()` on purpose: run as a script, `src` is not importable
# until that call puts it on the path.
import preflight_h3_ultra

from music_video_producer.comfy import ComfyWebSocket, execution_span_ms
from music_video_producer.timeline import (
    H3_FPS,
    margin_frames,
    over_render_frames,
    over_render_lead,
    over_render_window,
)
from music_video_producer.workflows import (
    H3_ATTENTION_PROFILES,
    H3_DEFAULT_ATTENTION,
    H3_DEFAULT_PROFILE,
    H3_REFERENCE_PROFILES,
    build_h3_reference_payload,
)

#: The frame count every arm renders. Not a round number and not a preference: it is the
#: first point in the recorded cost table past the cliff, where per-frame cost went from
#: 2.89 s to 8.02 s, and it is on H3's own 17k+5 grid (17 x 13 + 5).
MEASURED_FRAMES = 226

#: A short render on the default profile, before the arms, purely to make the checkpoint
#: resident. Without it the first arm pays the model load and looks slower for a reason that
#: has nothing to do with attention. 107 frames is the cheapest point in the recorded table
#: (a median 4.6 min), and its numbers are reported as `warmup` and excluded from comparison.
WARMUP_FRAMES = 107

#: The frames pulled from every output, the same indices in every arm. 20/44/70 are the LTX
#: enhancer investigation's own; 113/180/225 cover the back half a 226-frame render has and
#: that investigation's clip did not. 225 is the last frame of 226, so a backend that drops
#: or duplicates a tail frame shows up here rather than in nobody's report.
SAMPLE_FRAMES = (20, 44, 70, 113, 180, 225)

DEFAULT_COMFY_URL = "http://127.0.0.1:8188"

#: The line ComfyUI prints when a requested attention backend is not registered and it
#: quietly uses PyTorch instead (`comfy_extras/nodes_model_advanced.py:378`).
#: `ModelAttentionBackend.VALIDATE_INPUTS` returns `True` unconditionally, so a payload
#: naming an unavailable backend **validates, renders, and produces a perfectly good file**.
#: An inert arm looks exactly like an arm that made no difference, which is the one result
#: this experiment cannot afford to publish by accident. If this line appears inside a
#: render's own log window, that arm is reported inconclusive and not as "no difference".
FALLBACK_LINE = "is unavailable; using PyTorch attention"

#: The line KJNodes prints on every sage patch (`model_optimization_nodes.py:29`). Positive
#: evidence, and the only positive evidence any of these nodes emits: it names the mode that
#: was actually selected, so a sage arm can be confirmed rather than merely not-refuted.
SAGE_MODE_LINE = "Using sage attention mode:"

#: The line ComfyUI prints once a checkpoint is resident. Its *absence* long after submission
#: is the first of the four did-not-complete signals: an arm that never reaches it never
#: sampled, so whatever it spent was spent on memory rather than on frames.
LOADED_LINE = "loaded completely"

#: tqdm's summary line, which ComfyUI's sampler prints when it finishes: `20/20 [03:01<00:00,
#: 9.08s/it]`. The elapsed field is **sampling time alone**, and sampling is the only part of a
#: render an attention backend touches.
#:
#: This exists because of a measurement error worth naming. The 107-frame screen ran with the
#: checkpoint cold for its first arm and resident for the rest, so that arm paid a 62 s load
#: (CLIP 25.9 GB + UNET 20.0 GB) the others did not — and a table built on total execution
#: reported it as the *backend* being 24% slower. The per-step rates were 9.08, 9.18 and 9.22
#: s/it: indistinguishable. Total execution measures whatever the machine happened to be doing;
#: this measures the thing under test, and it does not depend on the operator sequencing runs
#: correctly.
SAMPLING_LINE = re.compile(r"(\d+)/(\d+) \[(\d+):(\d\d)<[^\]]*?([\d.]+)s/it\]")

#: How far past a completed arm's execution time another arm may go before the
#: did-not-complete check is even considered. Deliberately loose — an arm is allowed to be
#: slower than the baseline, and being slower is not this failure.
DID_NOT_COMPLETE_BASELINE_MULTIPLE = 1.5

#: Power draw below which "100% utilisation" means memory traffic rather than arithmetic.
#: An RTX 5090 pulls 400-575 W under compute; the stalled arm measured 159-232 W.
DID_NOT_COMPLETE_POWER_WATTS = 200

#: How long the watchdog listens for a sampling step before concluding there are none. Longer
#: than any single step the 226-frame baseline took (~88 s), with room to spare.
PROGRESS_LISTEN_SECONDS = 180

#: The cheap screen. A backend that cannot load, cannot engage, or thrashes will show it here
#: for a few minutes instead of for half an hour, and only survivors are promoted to the
#: cliff point. Learned the expensive way: 97 minutes went into discovering that plain
#: PyTorch attention does not fit 226 frames, which 5 minutes at 107 would have suggested.
SCREEN_FRAMES = 107

#: The per-frame cost the 2026-08-19/20 batch recorded at 226 frames, used only to price a run
#: before it starts. **It is a `default`-profile, 20-step number** — the batch route sends no
#: profile and `BatchRequest.profile` defaults there — so it overprices every turbo arm, which
#: is the safe direction for a cost warning.
RECORDED_SECONDS_PER_FRAME = 8.02

#: How far either way the audio comparison looks for the best alignment. H3 *generates* its
#: audio conditioned on the reference rather than copying it, so a small offset is expected;
#: what a lip-sync investigation wants to know is whether one arm's offset is *different* from
#: another's. A second is far wider than any plausible sync error and narrow enough that the
#: best lag cannot land on an unrelated part of the phrase.
AUDIO_LAG_LIMIT_SECONDS = 1.0

#: The rate both sides of that comparison are resampled to. Speech energy that matters for
#: sync lives well under 8 kHz, and a common rate is required at all — the take and the master
#: song are not the same format.
AUDIO_COMPARE_RATE = 16000

#: Where the outputs, sampled frames and contact sheets are kept. `test-artifacts/` is
#: gitignored — these are local evidence, as the 2026-08-18 comparisons were.
ARTIFACT_ROOT = REPO_ROOT / "test-artifacts"

USAGE = (
    "usage: uv run python tests/measure_h3_attention.py --project PROJECT_ID "
    "[--shot SHOT_ID] [--profiles ATTENTION,...] [--sampling BUNDLE,...] [--repeats N] "
    "[--no-warmup] [--comfy-url URL] --confirm-gpu\n"
    "  backends at fixed steps: --sampling default --profiles default,pytorch,comfy-kitchen\n"
    "  bundles at a fixed backend: --sampling turbo-references2v,turbo --profiles default"
)


def today() -> str:
    """Today, in the local zone, for naming an evidence directory.

    Explicitly zoned because the alternative is a naive clock, and an evidence
    directory that disagrees with the dev-log entry citing it is a small lie.
    """
    return datetime.now(UTC).astimezone().date().isoformat()


def log_tail(log_path: Path, offset: int) -> tuple[str, int]:
    """Whatever ComfyUI wrote since `offset`, and the new offset.

    Bytes rather than lines because the log is rotated by size and read while it is being
    written; a decode that cannot be made is worth less than a run, so it is replaced rather
    than raised.
    """
    if not log_path.is_file():
        return "", offset
    with log_path.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read()
        return chunk.decode("utf-8", errors="replace"), handle.tell()


def log_window_between(log_path: Path, started_ms: int, finished_ms: int) -> str:
    """The log lines stamped inside one render's own execution span.

    The live path tracks byte offsets, which is exact because this harness submits serially.
    This is the *recovery* path: when a killed run leaves a render executing, the offsets died
    with the process and the only thing left to attribute lines by is their timestamps.
    ComfyUI stamps every line ``[YYYY-MM-DD HH:MM:SS.mmm]`` in local time, and `/history`
    carries the span in epoch milliseconds, so the two can be lined up.

    Generous at both ends by a second, because the model-load lines that precede the first
    sampling step — and the attention warning among them — can be stamped just before
    `execution_start`. An extra second of a serial log cannot capture another render.
    """
    if not log_path.is_file():
        return ""
    begin = datetime.fromtimestamp(started_ms / 1000).astimezone() - timedelta(seconds=1)
    end = datetime.fromtimestamp(finished_ms / 1000).astimezone() + timedelta(seconds=1)
    kept: list[str] = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.(\d{3})\]", line)
        if not match:
            # A continuation line belongs to whichever stamped line preceded it.
            if kept:
                kept.append(line)
            continue
        stamped = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").astimezone()
        stamped = stamped.replace(microsecond=int(match.group(2)) * 1000)
        if begin <= stamped <= end:
            kept.append(line)
        elif stamped > end:
            break
    return "\n".join(kept)


def sampling_seconds(log_window: str) -> tuple[float | None, float | None]:
    """``(seconds, seconds_per_step)`` spent sampling, read from ComfyUI's own tqdm summary.

    The last complete summary in the window, because a render prints one per sampler and the
    graph has one. ``None`` when the window holds no summary — an arm that never sampled has
    no sampling time, which is different from having one of zero.
    """
    best: tuple[float, float] | None = None
    for match in SAMPLING_LINE.finditer(log_window):
        done, total, minutes, seconds, per_step = match.groups()
        if done != total:
            continue
        best = (int(minutes) * 60 + int(seconds), float(per_step))
    return best if best else (None, None)


def engagement(profile_name: str, log_window: str) -> tuple[str, list[str]]:
    """Did the backend this arm names actually run? Verdict plus the lines it rests on.

    This is the check that stops the experiment publishing a false null. Three verdicts:

    * ``fell-back`` — ComfyUI logged the fallback inside this render's own window. The arm
      measured PyTorch attention wearing another name; it is inconclusive, never "no
      difference".
    * ``confirmed`` — a sage arm whose selected mode ComfyUI named in the log, and the name
      matches the one the profile asked for.
    * ``registered`` — a `ModelAttentionBackend` arm. There is no positive log line for one,
      so the evidence is a chain rather than a sentence, and the chain is sound because a
      single boolean gates both ends of it: `COMFY_KITCHEN_INT8_ATTENTION_IS_AVAILABLE` is
      evaluated once at import and decides *both* whether `"comfy kitchen attention"` appears
      in `INPUT_TYPES` and whether `register_attention_function("comfy_kitchen_int8", …)`
      runs (`comfy/ldm/modules/attention.py:54, 886-888`). So the option appearing in the
      *running server's* `/object_info` — which the pre-flight confirms against that same
      server — means the function is registered in that same process, which means
      `get_attention_function` cannot return `None`, which means the fallback branch cannot
      be taken. The absence of the fallback line is then a second, independent confirmation
      rather than the whole argument.

    A `registered` verdict is still weaker than a `confirmed` one, and the report says so.
    The experiment's own discriminator is the `pytorch` arm: if `comfy-kitchen` and `pytorch`
    time identically while the sage arms differ from both, that similarity has to be
    explained before it is believed.
    """
    lines = [line.strip() for line in log_window.splitlines() if line.strip()]
    fallback = [line for line in lines if FALLBACK_LINE in line]
    if fallback:
        return "fell-back", fallback
    profile = H3_ATTENTION_PROFILES[profile_name]
    if profile.class_type == "PathchSageAttentionKJ":
        wanted = dict(profile.inputs)["sage_attention"]
        if wanted == "disabled":
            # The default profile writes nothing at all, so there is nothing to confirm and
            # nothing that could have fallen back. What it inherits is ComfyUI's launch flag,
            # which the report records from `/system_stats` argv rather than guessing.
            return "inherited", []
        selected = [line for line in lines if SAGE_MODE_LINE in line]
        if not selected:
            return "unconfirmed", []
        if not any(line.rstrip().endswith(wanted) for line in selected):
            return "wrong-mode", selected
        return "confirmed", selected
    return "registered", []


def abort(message: str) -> None:
    """Stop, on stderr, with a non-zero code and without having submitted anything."""
    print(f"ABORT: {message}", file=sys.stderr)
    raise SystemExit(1)


def get_json(url: str, timeout: float = 60.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(url: str, body: dict[str, Any], timeout: float = 300.0) -> Any:
    call = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(call, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def duration_for_frames(frames: int) -> float:
    """The shot duration whose over-rendered frame count is exactly `frames`.

    The builder takes seconds and derives frames — that is the project's rule (seconds
    against the master song, frames only at the workflow boundary), and this harness does not
    get to break it just because its subject is a frame count. So the duration is *solved*
    rather than guessed, by walking the grid, and the answer is asserted rather than trusted:
    if `over_render_frames` ever stops producing this count for any duration, the run has no
    such point to measure and says so instead of measuring something else.
    """
    # `margin_frames`, not `over_render_frames`, and **the largest** match rather than the
    # first.
    #
    # This is the fixture bug that voided a whole screen. `over_render_frames` floors every
    # short window at `H3_MIN_RENDER_FRAMES` (107), so *every* duration below ~3.271 s answers
    # 107 — and walking up from one frame returned **0.0417 s**. The 107-frame arms were
    # therefore rendered from a 42-millisecond window and conditioned on 42 ms of song, which
    # is no song at all. `margin_frames` is the un-floored arithmetic, so requiring it to
    # agree picks a duration that genuinely *asks* for this many frames; taking the top of
    # that band puts the fixture as far from the floor boundary as the band allows.
    step = 1 / 24
    natural = [
        round(index * step, 6)
        for index in range(1, 24 * 20)
        if margin_frames(index * step) == frames and over_render_frames(index * step) == frames
    ]
    if natural:
        return natural[-1]
    abort(
        f"No shot duration naturally produces {frames} frames — `margin_frames` never answers "
        f"it, so the only windows that reach it are floored ones and a fixture built from "
        f"them would condition the render on almost no song. The frame grid or the "
        f"over-render margin moved."
    )
    raise AssertionError("unreachable")


def measured_duration() -> float:
    """The duration for the cliff point, kept as its own name because it is the default."""
    return duration_for_frames(MEASURED_FRAMES)


def warmup_duration() -> float:
    return duration_for_frames(WARMUP_FRAMES)


def sample_indices(frames: int) -> tuple[int, ...]:
    """The frame indices sampled from a take of `frames` frames.

    Fixed across arms at one frame count, which is the whole point — the same instant of the
    same performance, compared between backends. Indices past the end of a shorter screening
    take are dropped rather than clamped: two arms sharing a clamped index would look aligned
    while both were showing their last frame, which is agreement about nothing.
    """
    return tuple(index for index in SAMPLE_FRAMES if index < frames)


def should_cut(
    *,
    elapsed: float,
    baseline: float | None,
    loaded_completely: bool,
    progress_frames: int,
    watts: float | None,
) -> bool:
    """Whether an arm has entered the known memory-bound failure and must be cut.

    All four conditions, never any subset, because each alone has an innocent reading: a slow
    arm is allowed to be slow, a quiet log is allowed between phases, a short listen can fall
    inside one long sampling step, and low power can be a gap between allocations. Together
    they are the signature measured on 2026-08-22, when plain PyTorch attention spent 97
    minutes on a 226-frame sequence without reaching `loaded completely` or emitting a single
    sampling step, pinned at 100% utilisation drawing 159 W on a card that pulls 400-575 W
    under compute, with host RAM free falling from 30.5 to 14.3 GiB.

    ``baseline`` is a *completed* arm's execution seconds, so the first arm of a run — which
    has nothing to be slow relative to — is never cut. That is deliberate: this rule exists to
    stop re-confirming a known pattern, not to police an experiment nobody has a reference for
    yet.

    Anything that does not match all four — an error, a crash, an arm that is merely slow but
    genuinely sampling — is not this pattern and is left alone for a person to look at.
    """
    if baseline is None or elapsed < baseline * DID_NOT_COMPLETE_BASELINE_MULTIPLE:
        return False
    if loaded_completely or progress_frames > 0:
        return False
    return watts is not None and watts < DID_NOT_COMPLETE_POWER_WATTS


def gpu_power_watts() -> float | None:
    """The card's power draw, or None when nvidia-smi cannot be asked.

    None never cuts an arm: an unreadable meter is not evidence of thrashing.
    """
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        result = subprocess.run(
            [smi, "--query-gpu=power.draw", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=30, check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    first = result.stdout.strip().splitlines()[:1]
    try:
        return float(first[0]) if first else None
    except ValueError:
        return None


#: How long to let ComfyUI act on a `/free` before reading the result. The endpoint sets
#: queue flags and returns; `PromptQueue.set_flag` calls `not_empty.notify()`, which wakes the
#: idle worker so it reaches `get_flags()` and runs `unload_all_models()` plus a gc — but none
#: of that has happened when the 200 arrives. Verified by reading state either side rather
#: than trusted: `free_memory` below records the delta, and a zero delta is reported, not
#: hidden.
FREE_SETTLE_SECONDS = 8.0


def free_memory(comfy_url: str) -> dict[str, Any]:
    """Ask ComfyUI to unload models and drop its caches, and measure whether it did.

    `POST /free` is core ComfyUI (`server.py:1192`), not a custom node: it sets the
    `unload_models` and `free_memory` queue flags, which `main.py:398-410` consumes on the
    worker's next tick — calling `comfy.model_management.unload_all_models()`, resetting the
    executor's cache and forcing a gc.

    **It is asynchronous and this function does not assume it worked.** State is read before
    the call and again after a settle, and the deltas are returned for the record. On this
    machine a long session grew ComfyUI to 22.78 GiB resident against 61.6 GiB of physical
    RAM, and the arms rendered late in that session cost up to twice what the same frame
    counts cost early — so whether this endpoint holds the line is itself a question worth
    measuring, and a production answer for long batches if it does.
    """
    before = gpu_state(comfy_url)
    outcome: dict[str, Any] = {"before": before}
    try:
        # **A raw request, not `post_json`.** `/free` answers 200 with a zero-length body, so
        # parsing the reply as JSON raises — and the first version of this reported `called:
        # False` for a call that had in fact succeeded. That is the same trap as assuming it
        # worked, entered from the other side: the endpoint's *contract* has to be read, not
        # its convenience wrapper's.
        call = urllib.request.Request(
            f"{comfy_url}/free",
            data=json.dumps({"unload_models": True, "free_memory": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(call, timeout=60) as response:
            outcome["called"] = response.status == 200
            outcome["status"] = response.status
    except (urllib.error.URLError, OSError, ValueError) as error:
        outcome["called"] = False
        outcome["error"] = str(error)
        return outcome
    time.sleep(FREE_SETTLE_SECONDS)
    after = gpu_state(comfy_url)
    outcome["after"] = after
    outcome["freed"] = {
        key: round(after[key] - before[key], 2)
        for key in ("comfy_vram_free_gib", "host_ram_free_gib", "vram_used_mib")
        if isinstance(before.get(key), (int, float)) and isinstance(after.get(key), (int, float))
    }
    return outcome


def apply_preview_override(payload: dict[str, dict[str, Any]], preview_frames: int | None) -> int:
    """Patch `ModelPreviewOverrideKJ.preview_frames` for a measurement. Returns nodes touched.

    **`None` must patch nothing**, or every submitted payload silently stops matching the
    builder that produced it and every digest in the suite becomes a statement about something
    else. Extracted from the submission path purely so that guarantee is testable: a mutation
    that made it patch unconditionally, and one that made it patch the *attention* node
    instead, both survived the suite while the logic lived inline.
    """
    if preview_frames is None:
        return 0
    touched = 0
    for node in payload.values():
        if node.get("class_type") == "ModelPreviewOverrideKJ":
            node["inputs"]["preview_frames"] = preview_frames
            touched += 1
    return touched


def cost_fields(
    *, sampling_span: float | None, execution: float | None, wall: float, frames: int
) -> dict[str, Any]:
    """Per-frame cost and the basis it was computed on.

    Sampling time when it is known, because that is the only part a backend or a bundle
    touches and the only part independent of whether the checkpoint happened to be resident.
    The whole render is the fallback, and it is *labelled* as such — mixing the two silently
    is how a 62 s cold load once read as a 24% backend difference.
    """
    if sampling_span:
        return {
            "seconds_per_frame": round(sampling_span / frames, 3),
            "seconds_per_frame_basis": "sampling",
        }
    return {
        "seconds_per_frame": round((execution or wall) / frames, 3),
        "seconds_per_frame_basis": "whole-render",
    }


def gpu_state(comfy_url: str) -> dict[str, Any]:
    """Whatever the card and the server will cheaply say about conditions right now.

    Recorded per arm at submission time, and **not interpreted**. This run has twice been
    misled by a measurement whose conditions differed from its neighbours' — a cold checkpoint
    read as a slower backend, and a fixture that conditioned on 42 ms of song — and in both
    cases the conditions were knowable at the time and simply were not written down. VRAM
    already in use, temperature and power draw cost one subprocess call and one HTTP request;
    the next anomaly then has context instead of a hypothesis.

    Everything here is best-effort. A missing reading is `None`, never a guess, and nothing
    decides anything on these values.
    """
    state: dict[str, Any] = {}
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            result = subprocess.run(
                [
                    smi,
                    (
                        "--query-gpu=memory.used,memory.total,temperature.gpu,power.draw,"
                        "utilization.gpu,clocks.current.sm,clocks.current.memory,"
                        "clocks_throttle_reasons.active"
                    ),
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True, text=True, timeout=30, check=True,
            )
            fields = [item.strip() for item in result.stdout.strip().splitlines()[0].split(",")]
            # `throttle_hex` is the one most likely to differ silently between sessions and
            # never be recorded: 0x0 is unthrottled, 0x4 is SW Power Cap. Two sessions of this
            # experiment ran in different regimes — one at ~284 W and unthrottled, another
            # pinned at the ~500 W cap — and nothing captured it at the time.
            names = (
                "vram_used_mib", "vram_total_mib", "temp_c", "power_w", "util_pct",
                "sm_mhz", "mem_mhz", "throttle_hex",
            )
            for name, raw in zip(names, fields, strict=False):
                if name == "throttle_hex":
                    # Kept as the string nvidia-smi prints; it is a bit field, not a quantity.
                    state[name] = raw or None
                    continue
                try:
                    state[name] = float(raw)
                except ValueError:
                    state[name] = None
        except (subprocess.SubprocessError, OSError, IndexError):
            pass
    try:
        stats = get_json(f"{comfy_url}/system_stats", timeout=20)
        devices = stats.get("devices") or []
        if devices:
            state["comfy_vram_free_gib"] = round(devices[0].get("vram_free", 0) / 2**30, 2)
        state["host_ram_free_gib"] = round(
            (stats.get("system") or {}).get("ram_free", 0) / 2**30, 2
        )
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        pass
    return state


def progress_frames_seen(comfy_url: str, seconds: float) -> int:
    """How many per-step progress frames ComfyUI emits over `seconds`.

    The one signal that separates "sampling slowly" from "not sampling". A render that is
    working emits a frame per step; the 226-frame baseline averaged ~88 s per step, so a
    three-minute listen crosses at least one boundary on any arm that is actually running.
    A socket that cannot be opened answers 1 rather than 0 — an unobservable render is not
    evidence of a stalled one, and this number can only ever cut an arm when it is zero.
    """

    async def listen() -> int:
        socket = await ComfyWebSocket.connect(
            comfy_url, client_id=f"mvp-watchdog-{uuid.uuid4().hex[:8]}", timeout=10
        )
        seen = 0
        deadline = time.monotonic() + seconds
        try:
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(socket.receive(), timeout=10)
                except TimeoutError:
                    continue
                if isinstance(raw, (bytes, bytearray)):
                    # Binary frames are preview images, which only a sampling render sends.
                    seen += 1
                    continue
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if message.get("type") in {"progress", "progress_state"}:
                    seen += 1
        finally:
            with contextlib.suppress(Exception):
                await socket.close()
        return seen

    try:
        return asyncio.run(listen())
    except (TimeoutError, OSError, RuntimeError, ValueError):
        # An unobservable render is not evidence of a stalled one, so a socket that will not
        # open answers "one frame seen" and the watchdog declines to cut on it.
        return 1


def load_shot(project_id: str, shot_id: str | None) -> tuple[dict, dict, Path]:
    """The project manifest, the shot to measure, and the project's media root.

    Read as plain JSON rather than through the store, deliberately. This is a manual harness
    whose only relationship to a project is *reading one shot's inputs*; it writes nothing
    back, and going through the model layer would couple a measurement to a schema that is
    under active edit for unrelated reasons.
    """
    root = REPO_ROOT / "data" / "projects" / project_id
    manifest_path = root / "project.json"
    if not manifest_path.is_file():
        abort(f"No project manifest at {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    shots = manifest.get("shots") or []

    def is_singing(shot: dict) -> bool:
        """A shot this measurement can say anything about lip-sync from.

        The master song has to be conditioning the render — that is what makes a mouth move
        at all — and there has to be a picture, because a backend that shifts a phoneme is
        judged on a face. A shot with neither is a perfectly good render and a useless arm.
        """
        pictures = [
            citation
            for citation in (shot.get("citations") or [])
            if citation.get("asset_id")
        ]
        return bool(shot.get("use_song_audio")) and bool(pictures)

    if shot_id is None:
        candidates = [shot["id"] for shot in shots if is_singing(shot)]
        abort(
            "Name the shot to measure with --shot. This harness will not pick one: the "
            "measurement is about lip-sync, and which face is being sung with is the "
            "Director's call.\n"
            + (
                "  singing shots in this project: " + ", ".join(candidates)
                if candidates
                else "  no shot in this project uses the song audio with a picture cited"
            )
        )
    shot = next((item for item in shots if item.get("id") == shot_id), None)
    if shot is None:
        abort(f"No shot {shot_id!r} in {project_id}")
    if not is_singing(shot):
        abort(
            f"Shot {shot_id} does not sing to the master song with a picture cited, so a "
            f"lip-sync comparison over it would be comparing nothing. Pick a singing shot."
        )
    return manifest, shot, root


def build_references(manifest: dict, shot: dict, root: Path, window: tuple[float, float]) -> list[dict]:
    """The shot's own references, resolved to files, plus the master song windowed.

    The window is the shot's start and the *measured* duration, which is longer than the
    shot's own — 226 frames is the point being measured, not this shot's length. Said out
    loud because the render this produces is deliberately not the render the application
    would produce for this shot.
    """
    assets = {asset["id"]: asset for asset in (manifest.get("assets") or [])}
    references: list[dict] = []
    for citation in shot.get("citations") or []:
        asset = assets.get(citation.get("asset_id"))
        if asset is None:
            abort(f"Shot cites asset {citation.get('asset_id')!r}, which the manifest lacks")
        path = (root / asset["path"]).resolve()
        if not path.is_file():
            abort(f"Reference {asset['name']!r} is not on disk at {path}")
        references.append(
            {
                "kind": "picture",
                "file": str(path).replace("\\", "/"),
                "label": (shot.get("reference_labels") or {}).get(asset["id"], asset["name"]),
            }
        )
    if not references:
        abort("The shot cites no usable picture reference")
    song = manifest.get("song") or {}
    song_path = song.get("path")
    if not song_path:
        abort("The project has no master song, so there is nothing to sing to")
    resolved = (root / song_path).resolve()
    if not resolved.is_file():
        abort(f"The master song is not on disk at {resolved}")
    references.append(
        {
            "kind": "audio",
            "file": str(resolved).replace("\\", "/"),
            "label": "master song",
            "trim": {"start": window[0], "end": window[1]},
        }
    )
    return references


def only_the_attention_node_differs(payloads: dict[str, dict]) -> None:
    """Abort unless the arms are identical apart from `mvp:attention` and the shift wiring.

    The whole experiment is one controlled variable, and this is the only place that claim
    can be checked before the GPU is spent. Two attention profiles legitimately differ in
    three ways and no more: the attention node itself, `mvp:shift`'s `model` input (the two
    node classes sit on opposite sides of it), and `mvp:preview`'s `model` input (which reads
    whichever of the two is last). Anything else — a seed, a frame count, a reference, a step
    count — means the arms are not comparable and the run must not start.
    """
    allowed = {"mvp:attention", "mvp:shift", "mvp:preview"}
    reference_name = H3_DEFAULT_ATTENTION
    reference = payloads[reference_name]
    for name, payload in payloads.items():
        if name == reference_name:
            continue
        if set(payload) - allowed != set(reference) - allowed:
            abort(f"Profile {name!r} emits a different set of nodes; the arms are not comparable")
        for node_id, node in payload.items():
            if node_id in allowed:
                continue
            if node != reference[node_id]:
                abort(
                    f"Profile {name!r} differs from {reference_name!r} at node {node_id}, "
                    f"which is not the attention node. The arms are not comparable and "
                    f"nothing has been submitted."
                )
        for node_id in ("mvp:shift", "mvp:preview"):
            mine = dict(payload[node_id]["inputs"])
            theirs = dict(reference[node_id]["inputs"])
            mine.pop("model", None)
            theirs.pop("model", None)
            if mine != theirs or payload[node_id]["class_type"] != reference[node_id]["class_type"]:
                abort(f"Profile {name!r} changes {node_id} beyond its model wiring")


def wait_for(
    comfy_url: str,
    prompt_id: str,
    poll: float = 5.0,
    watchdog: Callable[[float], list[str] | None] | None = None,
    power_samples: list[float] | None = None,
) -> dict | None:
    """Block until this prompt leaves the queue, then return its history entry.

    `/history` alone reports an executing render as absent, which is indistinguishable from
    one that never arrived, so `/queue` is what says "still ours" — the same distinction the
    application's job refresh has to make.

    ``watchdog`` is asked, on each poll, whether this arm has entered the known memory-bound
    failure; when it says so this interrupts the prompt and answers ``None``. Interrupting one
    prompt is not stopping ComfyUI — it is what the application's own cancel does — and the
    alternative is hours of a card doing memory traffic to re-confirm a pattern already
    characterised.
    """
    power_samples = [] if power_samples is None else power_samples
    started = time.monotonic()
    while True:
        # **Sampled mid-render, because the idle snapshots either side cannot see this.**
        # Arms of this experiment have landed in two distinct regimes — some drawing ~500 W
        # power-capped, others ~200-280 W — and the fast ones are the high-power ones. Nothing
        # recorded before this could tell them apart after the fact, because state_before and
        # state_after are both taken while the card is idle at ~40 W.
        watts = gpu_power_watts()
        if watts is not None:
            power_samples.append(watts)
        entry = get_json(f"{comfy_url}/history/{urllib.parse.quote(prompt_id)}").get(prompt_id)
        if entry and entry.get("outputs"):
            return entry
        queue = get_json(f"{comfy_url}/queue")
        pending = [
            item
            for bucket in ("queue_running", "queue_pending")
            for item in queue.get(bucket, [])
            if len(item) > 1 and item[1] == prompt_id
        ]
        if not pending:
            if entry:
                return entry
            abort(f"Prompt {prompt_id} left the queue without a history entry")
        if watchdog is not None:
            evidence = watchdog(time.monotonic() - started)
            if evidence:
                print("  !! cutting this arm — the memory-bound signature, all four signals:")
                for line in evidence:
                    print(f"     - {line}")
                post_json(f"{comfy_url}/interrupt", {}, timeout=60)
                return None
        time.sleep(poll)


def output_video(comfy_output_root: Path, entry: dict) -> Path:
    """The one video this render produced, on disk."""
    candidates: list[Path] = []
    for node_outputs in (entry.get("outputs") or {}).values():
        for key in ("gifs", "video", "images"):
            for item in node_outputs.get(key, []) or []:
                name = item.get("filename")
                if not name or not name.lower().endswith((".mp4", ".webm", ".mkv")):
                    continue
                candidates.append(
                    comfy_output_root
                    / (item.get("subfolder") or "")
                    / name
                )
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        abort(f"The render produced no video on disk; history named {candidates}")
    # More than one is chosen between rather than refused. `VHS_VideoCombine` can emit a
    # silent companion beside the muxed file, and by the time this runs the GPU minutes are
    # already spent — throwing the render away over an ambiguity we can resolve would be the
    # expensive kind of strictness. The muxed file is the one with the audio H3 generated,
    # which is the only one a lip-sync comparison can use.
    with_audio = [path for path in existing if "-audio" in path.name]
    return (with_audio or existing)[0]


def sample_frames(
    ffmpeg: str, video: Path, out_dir: Path, label: str, indices: tuple[int, ...] = SAMPLE_FRAMES
) -> dict[int, Path]:
    """One PNG per index in `SAMPLE_FRAMES`, selected by frame number rather than by time.

    `select=eq(n,N)` counts decoded frames, so the same N is the same frame in every arm.
    Seeking by timestamp would not be: a container whose timebase or first PTS moved would
    hand back a neighbour, and a neighbouring frame is exactly the size of the lip-sync
    difference being looked for.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    picked: dict[int, Path] = {}
    for index in indices:
        target = out_dir / f"{label}-f{index:04d}.png"
        subprocess.run(
            [
                ffmpeg, "-y", "-v", "error", "-i", str(video),
                "-vf", f"select=eq(n\\,{index})", "-vsync", "0", "-frames:v", "1",
                str(target),
            ],
            check=True,
        )
        if target.is_file():
            picked[index] = target
    return picked


def contact_sheet(ffmpeg: str, frames: list[tuple[str, Path]], target: Path) -> None:
    """Every arm's copy of one frame, side by side in one image, labelled.

    The shape the enhancer investigation's `mouth_compare.jpg` had, and for its reason: a
    mouth is judged against another mouth at the same instant, in one glance, by a person.
    """
    if len(frames) < 2:
        return
    command = [ffmpeg, "-y", "-v", "error"]
    for _, path in frames:
        command += ["-i", str(path)]
    labels = "".join(
        f"[{index}:v]drawtext=text='{name}':x=10:y=10:fontsize=28:fontcolor=white:"
        f"box=1:boxcolor=black@0.6[v{index}];"
        for index, (name, _) in enumerate(frames)
    )
    chain = "".join(f"[v{index}]" for index in range(len(frames)))
    command += [
        "-filter_complex",
        f"{labels}{chain}hstack=inputs={len(frames)}[out]",
        "-map", "[out]", str(target),
    ]
    # `check=False` on purpose: the fallback below is the handling, and a raise here would
    # throw away every sheet because one ffmpeg build lacks freetype.
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        # A missing `drawtext` (an ffmpeg built without freetype) must not lose the sheet;
        # the frames are still side by side and the file order still names the arms.
        command = [ffmpeg, "-y", "-v", "error"]
        for _, path in frames:
            command += ["-i", str(path)]
        command += [
            "-filter_complex", f"hstack=inputs={len(frames)}", str(target)
        ]
        subprocess.run(command, check=True)


def resolve_run_dir(requested: str | None) -> Path:
    """Which evidence directory this invocation belongs to, refusing to guess wrongly.

    The directory is date-stamped, and **an experiment that takes hours crosses midnight**.
    The first version of this took `today()` unconditionally, so a resumed run made a second,
    empty directory, found no records in it, and set about adopting an in-flight render as the
    first arm of a fresh experiment — which would have filed a `pytorch` render under the
    `default` label. That is the mislabelled-arm failure this whole harness exists to prevent,
    arriving through the one door nobody was watching.

    So: an explicit `--run-dir` always wins. Otherwise today's directory is used when it is
    the only candidate — a genuinely new run, or a resumed one that has not crossed midnight.
    But when today's holds no records and some *other* run directory does, this refuses and
    names it, because at that point the harness has two defensible answers and picking one
    silently is how the wrong arm gets a name.
    """
    if requested:
        candidate = Path(requested)
        return candidate if candidate.is_absolute() else ARTIFACT_ROOT / candidate
    todays = ARTIFACT_ROOT / f"{today()}-h3-attention"
    if (todays / "records").is_dir() and any((todays / "records").glob("*.json")):
        return todays
    others = sorted(
        path
        for path in ARTIFACT_ROOT.glob("*-h3-attention")
        if path != todays and any((path / "records").glob("*.json"))
    )
    if others:
        abort(
            f"Today's evidence directory ({todays.name}) holds no arm records, but "
            f"{', '.join(path.name for path in others)} does. A run that crosses midnight "
            f"must be told which experiment it is resuming — starting a second one here "
            f"would file the next arm under a fresh experiment's first label. Pass "
            f"--run-dir {others[-1].name} to continue it, or --run-dir {todays.name} to "
            f"start a new one deliberately."
        )
    return todays


def bundle_facts(sampling: str) -> dict[str, Any]:
    """The sampling bundle's own numbers, recorded beside every arm's cost.

    A named function rather than three inline lookups because these are the fields the
    ceiling question turns on: an arm that reported the *default* bundle's 20 steps while
    rendering `turbo`'s 4 would make a five-fold speed difference look like an attention
    result. Read from `H3_REFERENCE_PROFILES` so a bundle retuned there cannot leave a stale
    number in a report.
    """
    profile = H3_REFERENCE_PROFILES[sampling]
    return {
        "steps": profile.steps,
        "lora": profile.lora,
        "lora_strength": profile.lora_strength,
        "scheduler": profile.scheduler,
        "sampler": profile.sampler,
    }


def decode_audio(ffmpeg: str, source: Path, target: Path, window: tuple[float, float] | None = None) -> Path:
    """One mono 16 kHz PCM file, so two recordings from different formats can be compared."""
    command = [ffmpeg, "-y", "-v", "error"]
    if window is not None:
        command += ["-ss", f"{window[0]:.6f}", "-to", f"{window[1]:.6f}"]
    command += [
        "-i", str(source), "-vn", "-ac", "1", "-ar", str(AUDIO_COMPARE_RATE),
        "-c:a", "pcm_s16le", str(target),
    ]
    subprocess.run(command, check=True)
    return target


def audio_comparison(take: Path, reference: Path) -> dict[str, Any]:
    """How this take's generated audio sits against the master-song window it was given.

    **This is not a fidelity score and must not be read as one.** H3 generates its audio
    conditioned on the reference rather than copying it, so a correlation well below 1 is the
    expected, correct behaviour — the Director's note that a test render's audio was "a bit of
    a mutation of what I assume was the input audio" describes the model working as designed.
    What this measures is therefore *comparative*: whether one arm's audio sits further from
    the source, or later against it, than another's, at the same seed and the same window.

    Two numbers, and the second is the one a lip-sync investigation wants:

    * ``correlation`` — the peak of the normalised cross-correlation. Across arms, a markedly
      lower value means that arm's audio departed further from the phrase it was given.
    * ``lag_ms`` — where that peak sits. A *systematic* offset between arms is a sync
      difference stated in milliseconds instead of eyeballed off a contact sheet, and it is
      the one part of the lip-sync question that does not need a person. The sheets still do;
      a mouth can be wrong while the audio is right.

    **The lag aliases on periodic material, and that is a property of correlation rather than
    a bug.** A sustained note repeats every period, so its correlation peaks at every multiple
    of that period and the largest one is not necessarily the true offset — a 220 Hz tone
    delayed by 100 ms reports 50 ms, eleven periods out. Sung phrases are broadband where the
    consonants are, which is where sync is judged anyway, so the number is usable on real
    material; on a held vowel it is not. Read a *difference between arms* as a signal and an
    absolute value as a hint.

    Deliberately no verdict, no threshold. The frames and the Director settle it.
    """
    import wave

    import numpy as np

    def samples(path: Path) -> Any:
        with wave.open(str(path), "rb") as handle:
            raw = handle.readframes(handle.getnframes())
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
        # Centred and unit-normalised so the correlation is a shape comparison rather than a
        # loudness one — two takes at different gains are not two different performances.
        data -= data.mean() if data.size else 0.0
        norm = np.linalg.norm(data)
        # A silent or empty side has no shape to compare, and normalising it would leave
        # zeros that correlate at exactly 0.0 with everything. That number is indistinguishable
        # from "this arm's audio bears no resemblance to the song", which is the most alarming
        # thing this report can say — so a dead track is reported as *absent*, never scored.
        return data / norm if norm else None

    def envelope(data: Any) -> Any:
        """The amplitude envelope, which is what "in time with the song" actually lives in.

        **Raw waveform correlation is the wrong tool here and the first run proved it.** All
        four screened arms came back at |r| < 0.04 with the lag pinned near the search
        boundary — not because the takes were unrelated to the song, but because H3
        *regenerates* its audio. A regenerated phrase can carry the same words at the same
        moments while sharing no phase and no timbre with the original, and waveform
        correlation sees only phase. The envelope survives that: it tracks where energy rises
        and falls, which is where syllables are, which is what a lip-sync question is about.

        Rectify, then average over a 20 ms window — long enough to discard pitch structure,
        short enough to keep syllable onsets, which arrive no faster than about 10 a second.
        """
        window_size = max(1, int(AUDIO_COMPARE_RATE * 0.02))
        smoothed = np.convolve(np.abs(data), np.ones(window_size) / window_size, mode="same")
        smoothed -= smoothed.mean()
        norm = np.linalg.norm(smoothed)
        return smoothed / norm if norm else None

    left, right = samples(take), samples(reference)
    if left is None or right is None:
        which = "the take" if left is None else "the master-song window"
        return {
            "correlation": None,
            "lag_ms": None,
            "note": f"{which} decoded to silence, so there is nothing to compare",
        }
    width = min(left.size, right.size)
    left, right = left[:width], right[:width]
    limit = int(AUDIO_LAG_LIMIT_SECONDS * AUDIO_COMPARE_RATE)

    def peak(first: Any, second: Any) -> tuple[float, float] | None:
        if first is None or second is None:
            return None
        size = 1 << (2 * width - 1).bit_length()
        spectrum = np.fft.rfft(first, size) * np.conjugate(np.fft.rfft(second, size))
        correlation = np.fft.irfft(spectrum, size)
        # Lags either side of zero live at the two ends of the wrapped correlation.
        window = np.concatenate((correlation[-limit:], correlation[: limit + 1]))
        best = int(np.argmax(window))
        return round(float(window[best]), 4), round((best - limit) * 1000 / AUDIO_COMPARE_RATE, 1)

    envelopes = peak(envelope(left), envelope(right))
    waveform = peak(left, right)
    result: dict[str, Any] = {
        # The envelope figures are the ones to read. The waveform pair is kept because it is
        # what the first run reported and dropping it silently would make the old numbers
        # unexplainable.
        "correlation": envelopes[0] if envelopes else None,
        "lag_ms": envelopes[1] if envelopes else None,
        "basis": "amplitude envelope, 20 ms window",
        "waveform_correlation": waveform[0] if waveform else None,
        "waveform_lag_ms": waveform[1] if waveform else None,
    }
    # A peak sitting on the edge of the search window is not a measured offset — it is the
    # search running out of room, and reporting it as a delay would invent one.
    if result["lag_ms"] is not None and abs(result["lag_ms"]) >= AUDIO_LAG_LIMIT_SECONDS * 1000 * 0.9:
        result["note"] = (
            f"peak is at the edge of the +/-{AUDIO_LAG_LIMIT_SECONDS * 1000:.0f} ms search window, so the lag is not a "
            "measured offset"
        )
    return result


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(usage=USAGE, add_help=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--shot", default=None)
    parser.add_argument("--profiles", default=",".join(H3_ATTENTION_PROFILES))
    # The *sampling* bundle, which is a different question from the attention backend and is
    # here because the render-cost table turned out to answer neither. Every number in it was
    # taken on `default` — 20 steps, no LoRA — because the batch route's `BatchRequest.profile`
    # defaults there and the frontend sends no profile with a batch. Single-shot "Render
    # Again" hardcodes `turbo` (4 steps). So the ceiling those numbers justify was derived
    # from the slowest bundle the application ships, and the arms that decide it are these.
    parser.add_argument("--sampling", default=H3_DEFAULT_PROFILE)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--no-warmup", action="store_true")
    parser.add_argument("--comfy-url", default=DEFAULT_COMFY_URL)
    # Recovery, not convenience. A killed harness leaves ComfyUI happily rendering the prompt
    # it had already submitted: the GPU minutes are being spent whether or not anything is
    # left to collect them. `--adopt` takes that prompt id, waits for it, and records it as
    # the first arm that has no record yet — which is the arm the dead process was on, because
    # arms are rendered in order and each writes its record the moment it lands.
    parser.add_argument("--adopt", default=None)
    # Call `POST /free` before every arm, so each starts from the same machine state instead
    # of inheriting whatever the previous one left. Off by default: it changes what is being
    # measured, and an experiment that quietly cleaned up between arms would be describing a
    # machine nobody runs.
    parser.add_argument("--free-between-arms", action="store_true")
    # Override `ModelPreviewOverrideKJ.preview_frames` at submission, for measuring what
    # preview generation costs. **Default None patches nothing**, so the submitted payload is
    # byte-identical to what the builders emit and every digest is untouched. The builders are
    # deliberately not changed: the node and its `preview_frames: 12` come from the Director's
    # audited export (`h3-ultra-references-user-export.json` node 2376), so altering them is a
    # deviation from evidence and a Director decision, not a measurement's business.
    #
    # Worth measuring because ComfyUI generates previews whether or not anyone is listening:
    # `server.py`'s `send_image` resizes and encodes *before* `send_bytes` consults
    # `self.sockets`, and the previewer is built from `args.preview_method` rather than from
    # client count. So a batch pays for twelve decoded, resized, WebP-encoded frames per
    # sampling step for an audience of nobody.
    parser.add_argument("--preview-frames", default=None)
    # Which evidence directory this invocation belongs to. Defaults to today, and `resolve_run_dir`
    # refuses rather than guesses when today is not where the run lives.
    parser.add_argument("--run-dir", default=None)
    # The frame count every arm in this invocation renders. 226 is the cliff point and the
    # default; `--frames 107` is the cheap screen that answers "does this backend load,
    # engage and sample at all" for a few minutes instead of half an hour.
    parser.add_argument("--frames", default=str(MEASURED_FRAMES))
    parser.add_argument("--seed", type=int, default=20260821)
    # The gate. Deliberately not a `--dry-run` inverted into a default-on submission: the
    # safe state has to be the one a mistyped command lands in.
    parser.add_argument("--confirm-gpu", action="store_true")
    return parser.parse_args(argv)


def parse_and_gate(argv: list[str]) -> argparse.Namespace:
    """Read the arguments and refuse the run unless the GPU is explicitly authorised.

    Separated from `main` so the gate can be tested without a ComfyUI, an ffmpeg or a
    project: everything here happens before the first network call, and a test that had to
    stand one up to reach the refusal would be proving the refusal comes too late.

    The parsed arm list is attached to the namespace so `main` cannot re-split the strings
    differently from the lists this validated.

    An **arm is a pair** — one sampling bundle and one attention backend — because the two are
    orthogonal questions and the experiment needs both shapes. `--sampling default --profiles
    a,b,c` compares backends at fixed steps; `--sampling turbo,turbo-references2v --profiles x`
    compares bundles at a fixed backend. The cross product is what gets rendered, so asking for
    both lists at once is possible and expensive, and the cost line says how expensive.
    """
    args = parse_arguments(argv)
    attentions = [name.strip() for name in args.profiles.split(",") if name.strip()]
    samplings = [name.strip() for name in args.sampling.split(",") if name.strip()]

    # --- Every abort ahead of the GPU spend, in the order the smokes settled on. ---

    unknown = [name for name in attentions if name not in H3_ATTENTION_PROFILES]
    if unknown:
        abort(
            f"Unknown attention profile(s) {unknown}; the profiles are "
            f"{', '.join(sorted(H3_ATTENTION_PROFILES))}"
        )
    unknown = [name for name in samplings if name not in H3_REFERENCE_PROFILES]
    if unknown:
        abort(
            f"Unknown sampling profile(s) {unknown}; the profiles are "
            f"{', '.join(sorted(H3_REFERENCE_PROFILES))}"
        )
    for label, names in (("attention", attentions), ("sampling", samplings)):
        if len(set(names)) != len(names):
            abort(
                f"An {label} profile is named twice in {names}; an arm compared with itself "
                f"measures noise"
            )
    # A *list*, because a band sweep is one experiment across frame counts and running it as
    # seven invocations would be seven chances to vary something else by accident.
    try:
        frame_counts = [int(i.strip()) for i in str(args.frames).split(",") if i.strip()]
    except ValueError:
        abort(f"--frames takes whole numbers, not {args.frames!r}")
        raise AssertionError("unreachable")
    if not frame_counts:
        abort("--frames needs at least one count")
    if len(set(frame_counts)) != len(frame_counts):
        abort(f"A frame count is named twice in {frame_counts}")
    # Solved rather than trusted: a count off H3's 17k+5 grid has no duration that produces
    # it, and the arms would silently render a neighbouring length instead.
    for count in frame_counts:
        duration_for_frames(count)
    # `None` is the "patch nothing" axis value and is the only one by default, so a run that
    # does not ask for a preview comparison emits exactly what the builders emit.
    if args.preview_frames is None:
        previews: list[int | None] = [None]
    else:
        try:
            previews = [int(i.strip()) for i in str(args.preview_frames).split(",") if i.strip()]
        except ValueError:
            abort(f"--preview-frames takes whole numbers, not {args.preview_frames!r}")
            raise AssertionError("unreachable")
        if not previews:
            abort("--preview-frames needs at least one value")
        if len(set(previews)) != len(previews):
            abort(f"A preview value is named twice in {previews}")
        if any(value < 1 for value in previews):
            abort(f"preview_frames must be at least 1, not {previews}")
    arms = [
        (count, sampling, attention, preview)
        for count in frame_counts
        for sampling in samplings
        for attention in attentions
        for preview in previews
    ]
    # Two *renders*, not two distinct arms. One arm repeated is a legitimate experiment and
    # an important one: with frame count held constant, any rise across repeats is render
    # ORDER and nothing else. Both sweeps run so far confound order with size — they render
    # ascending frame counts in sequence — so neither can separate "bigger render" from
    # "later render", and only a constant-frame repeat can.
    if len(arms) * args.repeats < 2:
        abort(
            "An A/B needs at least two renders: either two arms, or one arm with --repeats"
        )
    if args.repeats < 1:
        abort("--repeats must be at least 1")

    renders = len(arms) * args.repeats + (0 if args.no_warmup else 1)
    # Costed per arm rather than at one flat rate, because step count is exactly what these
    # arms vary: quoting 30 minutes for a 4-step bundle would misprice the run by 5x in the
    # direction that matters — a Director declining a measurement that is actually cheap.
    minutes = round(
        sum(RECORDED_SECONDS_PER_FRAME * count * args.repeats for count, _, _, _ in arms) / 60
    ) + (0 if args.no_warmup else 6)
    if not args.confirm_gpu:
        print(USAGE, file=sys.stderr)
        print(
            f"\nThis would submit {renders} H3 renders at {frame_counts} frames "
            f"(roughly {minutes} minutes at the last recorded 20-step cost; the turbo bundles "
            f"sample fewer steps and come in well under it) to the user-managed ComfyUI at "
            f"{args.comfy_url}.\n"
            f"Arms: {', '.join(f'{s}+{a}@{c}f' + (f'/p{p}' if p else '') for c, s, a, p in arms)}\n"
            f"It refuses to submit anything without --confirm-gpu.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    args.arms = arms
    args.preview_list = previews
    args.attention_list = attentions
    args.sampling_list = samplings
    args.frame_counts = frame_counts
    args.render_count = renders
    return args


def main() -> None:
    args = parse_and_gate(sys.argv[1:])
    arms = args.arms
    attentions = args.attention_list
    renders = args.render_count

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        abort("ffmpeg is not on PATH; the lip-sync frames could not be sampled, so nothing "
              "was submitted. Speed that costs lip-sync is not a win, and a run that cannot "
              "check it is not worth the GPU minutes.")

    from music_video_producer.config import Settings

    settings = Settings()
    if settings.sage_attention:
        abort(
            f"MVP_SAGE_ATTENTION is set to {settings.sage_attention!r}. This harness submits "
            f"straight to ComfyUI, so the application's submission-time choke point is not in "
            f"the path and the report's profile names would not describe what the app sends. "
            f"Unset it for the measurement."
        )
    comfy_output_root = Path(settings.comfy_root) / "output"
    if not comfy_output_root.is_dir():
        abort(f"ComfyUI's output root is not a directory: {comfy_output_root}")
    # The log is not a nicety here. It is where the silent fallback announces itself, and a
    # run that cannot read it cannot tell an inert arm from an arm that made no difference.
    comfy_log = Path(settings.comfy_root) / "user" / "comfyui.log"
    if not comfy_log.is_file():
        abort(
            f"ComfyUI's log is not at {comfy_log}. It is the only place a silently "
            f"substituted attention backend is visible — `ModelAttentionBackend` validates "
            f"any string and falls back to PyTorch with nothing but a log line — so without "
            f"it every arm's result would be unfalsifiable. Nothing was submitted."
        )

    frame_counts = args.frame_counts

    manifest, shot, project_root = load_shot(args.project, args.shot)
    start = float(shot.get("start") or 0.0)
    song_duration = float((manifest.get("song") or {}).get("duration") or 0.0)

    def fixture_for(count: int) -> tuple[float, tuple[float, float], float, list[dict]]:
        """Duration, take window, lead and references for one frame count.

        **The take's own seconds of the song, through the same two functions the route uses.**
        A take does not begin at `shot.start`: it begins `lead` seconds earlier, and take
        second `t` is song second `start - lead + t` (`timeline.over_render_window`). The
        first version of this harness sent the bare exposed slice `(start, start + duration)`
        — shorter than the take and not lead-shifted — so it conditioned H3 on a window the
        application would never send, and then compared the result against a *third* window
        again. Every audio number it produced was measuring that mismatch.

        Computed **per frame count**, because a sweep across lengths must vary the length and
        nothing else: each count gets its own natural duration, its own lead, and conditioning
        audio proportional to the take. Sharing one window across counts would reintroduce the
        confound the corrected fixture was built to remove.
        """
        span = duration_for_frames(count)
        if over_render_frames(span) != count:
            abort(f"The solved duration does not produce {count} frames")
        picture = over_render_frames(span) / H3_FPS
        lead = over_render_lead(
            start=start, duration=span,
            picture_seconds=picture, song_duration=song_duration,
        )
        take_window = over_render_window(
            start=start, lead=lead, picture_seconds=picture, song_duration=song_duration,
        )
        return span, take_window, lead, build_references(
            manifest, shot, project_root, take_window
        )

    fixtures = {count: fixture_for(count) for count in frame_counts}
    prompt = shot.get("h3_prompt") or shot.get("prompt") or ""
    if not prompt.strip():
        abort("The shot has no prompt; an empty prompt is not a controlled variable")

    try:
        stats = get_json(f"{args.comfy_url}/system_stats", timeout=30)
    except (urllib.error.URLError, OSError) as error:
        abort(f"ComfyUI is not answering at {args.comfy_url} ({error}); it is user-managed "
              f"and is not started here")
        raise AssertionError("unreachable")
    launch_argv = (stats.get("system") or {}).get("argv") or []

    # The audit, against the same ComfyUI about to be submitted to. Its `main()` reads argv.
    saved_argv = sys.argv
    sys.argv = [str(Path(preflight_h3_ultra.__file__)), args.comfy_url]
    try:
        preflight_h3_ultra.main()
    except SystemExit as exit_code:
        if exit_code.code:
            abort("The H3 pre-flight audit failed against this ComfyUI; nothing was submitted")
    finally:
        sys.argv = saved_argv

    run_dir = resolve_run_dir(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Evidence directory: {run_dir}")
    # One JSON per arm, written the moment that arm lands. Three things follow from it and
    # each was learned the expensive way: a run killed mid-experiment keeps every arm it had
    # already paid for; the next invocation skips those arms instead of re-rendering them; and
    # a multi-hour experiment can be run one arm per invocation, which is what makes it
    # survivable at all. The first attempt at this run was killed after two arms and would
    # have had to start over.
    records_dir = run_dir / "records"
    records_dir.mkdir(parents=True, exist_ok=True)
    existing = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(records_dir.glob("*.json"))
    }
    if existing:
        print(f"Reusing {len(existing)} arm(s) already on disk: {', '.join(sorted(existing))}")

    def payload_for(
        name: str, frames_duration: float, prefix: str, sampling: str = H3_DEFAULT_PROFILE,
        refs: list[dict] | None = None,
    ) -> dict:
        return build_h3_reference_payload(
            prompt=prompt,
            references=refs if refs is not None else fixtures[frame_counts[0]][3],
            duration=frames_duration,
            seed=args.seed,
            prefix=prefix,
            profile=sampling,
            attention=name,
        )

    # Compared under **one shared prefix**, so the filename each arm writes to is not one of
    # the things being held constant. Every arm must write to a different file or they would
    # overwrite each other, and that difference is real but is not part of the graph — so it
    # is taken out of the comparison rather than exempted from it, and the guard below stays
    # strict enough to catch a seed or a frame count moving in `mvp:save`'s company.
    #
    # Run **per sampling bundle**, because two bundles legitimately differ in a LoRA node and
    # a step count and the guard would rightly refuse them. Within one bundle, attention is
    # the only thing allowed to move.
    for count in frame_counts:
        span, _, _, refs = fixtures[count]
        for sampling in args.sampling_list:
            group = [a for c, s_, a, pv in arms if c == count and s_ == sampling and pv is None]
            if H3_DEFAULT_ATTENTION in group and len(group) > 1:
                only_the_attention_node_differs(
                    {
                        name: payload_for(
                            name, span, "mvp/attention-compare", sampling, refs
                        )
                        for name in group
                    }
                )
    if H3_DEFAULT_ATTENTION not in attentions:
        print(
            "The default attention profile is not among the arms, so within a sampling bundle "
            "there is no baseline to diff the others against.",
            file=sys.stderr,
        )

    # The option list, re-read here as well as in the audit, and recorded beside the numbers.
    # `ModelAttentionBackend` publishes `"comfy kitchen attention"` only when the running
    # server has the backend, so this *is* the availability check — but it has to end up in
    # the report, because a reader six months from now cannot re-query a server.
    published = {
        class_type: (
            get_json(f"{args.comfy_url}/object_info/{class_type}")
            .get(class_type, {})
            .get("input", {})
        )
        for class_type in {profile.class_type for profile in H3_ATTENTION_PROFILES.values()}
    }
    backends = (published.get("ModelAttentionBackend", {}).get("required", {}).get("attention") or [[]])[0]
    if "comfy-kitchen" in attentions and "comfy kitchen attention" not in backends:
        abort(
            f"This build does not offer 'comfy kitchen attention' — it publishes {backends}. "
            f"The node would accept the string anyway and silently render on PyTorch. "
            f"Unavailable is a result; a mislabelled render is not. Nothing was submitted."
        )

    print(f"Renders: {renders}. Frames: {frame_counts}. Seed: {args.seed}.")
    print(f"ComfyUI {stats.get('system', {}).get('comfyui_version')} launched as {launch_argv}")
    print("The default profile inherits that launch flag rather than selecting a backend.")
    print(f"ModelAttentionBackend.attention publishes {backends}")

    results: list[dict[str, Any]] = []
    log_offset = comfy_log.stat().st_size
    # Which render this was, in submission order, counting only arms this invocation actually
    # rendered. **Position is a variable in this experiment** — two band sweeps ran ascending
    # frame counts and so could never separate "bigger render" from "later render". Recording
    # it lets a shuffled order answer that: if cost tracks frames regardless of position, the
    # curve is real; if it tracks position under a shuffled order, the order effect is.
    executed = 0

    def run_one(
        frames: int, sampling: str, name: str, repeat: int, payload: dict, label: str,
        adopt: str | None = None, preview: int | None = None,
    ) -> dict[str, Any]:
        nonlocal log_offset, executed
        executed += 1
        # The log window opens immediately before the submission and closes after the render
        # settles, so every line in it belongs to this render and no other. Serial by
        # construction — this harness submits one prompt at a time and waits — which is the
        # only reason a window can be attributed at all.
        _, log_offset = log_tail(comfy_log, log_offset)
        # Before the state snapshot, so what is recorded is the state this arm actually
        # started from rather than the state it inherited.
        # Patched here rather than in the builder, so the *builder* still emits the export's
        # own value and only this measurement's submission differs. `None` patches nothing.
        apply_preview_override(payload, preview)
        freed = free_memory(args.comfy_url) if (args.free_between_arms and not adopt) else None
        state_before = gpu_state(args.comfy_url)
        started = time.monotonic()
        if adopt:
            prompt_id = adopt
            print(f"  adopting in-flight prompt {prompt_id} as {label}")
        else:
            response = post_json(f"{args.comfy_url}/prompt", {"prompt": payload})
            prompt_id = response.get("prompt_id")
            if not prompt_id:
                abort(f"ComfyUI accepted no prompt id for {label}: {response}")
        cut_evidence: list[str] = []

        def completed_baseline() -> float | None:
            """The slowest arm that actually finished, or None while none has.

            The *slowest* rather than the fastest, so an arm is only cut when it is well past
            everything that has genuinely completed — the most conservative reference this run
            can offer. While no arm has finished there is no baseline and nothing is ever cut:
            this rule exists to stop re-confirming a characterised pattern, not to police an
            experiment that has no reference yet.
            """
            spans = [
                item["execution_seconds"]
                for item in results
                if item.get("execution_seconds")
            ]
            return max(spans) if spans else None

        def watchdog(elapsed: float) -> list[str] | None:
            """The four did-not-complete signals, checked cheapest first.

            Nothing is asked of the GPU or the socket until the arm is already well past a
            completed arm's time *and* has never reported its checkpoint resident — so an arm
            that is merely slow costs one string search per poll and nothing else.
            """
            baseline = completed_baseline()
            if baseline is None or elapsed < baseline * DID_NOT_COMPLETE_BASELINE_MULTIPLE:
                return None
            since, _ = log_tail(comfy_log, log_offset)
            if LOADED_LINE in since:
                return None
            watts = gpu_power_watts()
            seen = progress_frames_seen(args.comfy_url, PROGRESS_LISTEN_SECONDS)
            if not should_cut(
                elapsed=elapsed, baseline=baseline, loaded_completely=False,
                progress_frames=seen, watts=watts,
            ):
                return None
            cut_evidence[:] = [
                (
                    f"{elapsed / 60:.1f} min elapsed against a {baseline / 60:.1f} min "
                    f"completed arm ({elapsed / baseline:.1f}x)"
                ),
                f"ComfyUI never printed {LOADED_LINE!r} for this prompt",
                f"{seen} progress frames in a {PROGRESS_LISTEN_SECONDS}s websocket listen",
                f"{watts} W at high reported utilisation (compute on this card is 400-575 W)",
            ]
            return cut_evidence

        power_samples: list[float] = []
        entry = wait_for(
            args.comfy_url, prompt_id,
            watchdog=None if adopt else watchdog, power_samples=power_samples,
        )
        wall = time.monotonic() - started
        if entry is None:
            record = {
                "attention": name, "sampling": sampling, **bundle_facts(sampling),
                "repeat": repeat, "label": label, "prompt_id": prompt_id,
                "status": "did-not-complete", "frames": frames,
                # No timing at all, deliberately: the arm sampled no steps, so a duration
                # here would sort into a cost column as though it meant the same thing.
                "wall_seconds": None, "execution_seconds": None, "seconds_per_frame": None,
                "timing_source": "none — cut before the first sampling step",
                "engagement": "did-not-complete",
                "engagement_evidence": cut_evidence[:],
                "output": None, "adopted": False,
            }
            (records_dir / f"{label}.json").write_text(
                json.dumps(record, indent=2), encoding="utf-8"
            )
            print(f"  {label}: did not complete — recorded with its evidence")
            return record
        began, ended = execution_span_ms(entry.get("status") or {})
        execution = (ended - began) / 1000 if began and ended else None
        if adopt:
            # The byte offsets died with the process that owned them, so the log window is
            # recovered from ComfyUI's own execution span instead. The wall clock is *not*
            # recoverable — this one started before this process did — so it is reported as
            # the execution span rather than as a stopwatch reading nobody took.
            window_text = (
                log_window_between(comfy_log, began, ended) if began and ended else ""
            )
            wall = execution if execution else wall
        else:
            window_text, log_offset = log_tail(comfy_log, log_offset)
        verdict, evidence = engagement(name, window_text)
        sampled_span, per_step = sampling_seconds(window_text)
        video = output_video(comfy_output_root, entry)
        kept = run_dir / f"{label}{video.suffix}"
        shutil.copy2(video, kept)
        (run_dir / f"{label}-comfy.log").write_text(window_text, encoding="utf-8")
        record = {
            "attention": name,
            "sampling": sampling,
            # The bundle this arm actually sampled, recorded beside its cost because that is
            # the variable the ceiling turns on and the one nobody was tracking.
            **bundle_facts(sampling),
            "repeat": repeat,
            "execution_index": executed,
            "label": label,
            "prompt_id": prompt_id,
            "wall_seconds": round(wall, 2),
            "execution_seconds": round(execution, 2) if execution else None,
            # **The primary measure.** Sampling is the only part of a render an attention
            # backend touches, and it is the only part that does not depend on whether the
            # checkpoint happened to be resident. See `SAMPLING_LINE` for the run that taught
            # this: a 62 s cold load on one arm read as a 24% backend difference.
            "sampling_seconds": sampled_span,
            "seconds_per_step": per_step,
            # Everything that was not sampling: model load, VAE decode, muxing. Exposed
            # rather than hidden, because a large value here is what a load-inflated arm
            # looks like and the table should show it instead of burying it in a total.
            "non_sampling_seconds": (
                round(execution - sampled_span, 2)
                if execution and sampled_span else None
            ),
            "frames": frames,
            **cost_fields(
                sampling_span=sampled_span, execution=execution, wall=wall, frames=frames
            ),
            "timing_source": "comfy" if execution else "wall-clock-upper-bound",
            "engagement": verdict,
            "engagement_evidence": evidence,
            "preview_frames": preview,
            # Conditions at submission, recorded and not interpreted. See `gpu_state`.
            "state_before": state_before,
            "state_after": gpu_state(args.comfy_url),
            # The regime this arm actually ran in. A median near 500 W and one near 250 W are
            # different machines as far as a timing is concerned.
            "power_w_samples": len(power_samples),
            "power_w_median": (
                round(statistics.median(power_samples), 1) if power_samples else None
            ),
            "power_w_max": round(max(power_samples), 1) if power_samples else None,
            "free_before_arm": freed,
            "output": str(kept),
        }
        record["adopted"] = bool(adopt)
        (records_dir / f"{label}.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8"
        )
        print(
            f"  {label}: {record['seconds_per_frame']}s/frame "
            f"({record['seconds_per_frame_basis']}), sampling {sampled_span}s at "
            f"{per_step}s/it, execution {record['execution_seconds']}s, "
            f"non-sampling {record['non_sampling_seconds']}s  [{verdict}]"
        )
        if verdict == "fell-back":
            print(
                f"    !! this arm did NOT run {name}: ComfyUI substituted PyTorch attention "
                f"and said so. Its number is inconclusive, not a null result.",
                file=sys.stderr,
            )
        return record

    # The warmup exists to make the checkpoint resident.
    #
    # **This condition used to include `not existing`, and that was a bug.** Records on disk
    # say arms have been rendered at some point; they say nothing about whether the checkpoint
    # is resident in ComfyUI *now*. On 2026-08-22 a resumed run skipped the warmup on that
    # reasoning after the previous render had been cancelled mid-load, so its first arm paid a
    # 62 s cold load and the table reported that as the attention backend being 24% slower.
    #
    # It is left out of the condition now, and the deeper fix is that cost is measured as
    # *sampling* time, so residency cannot move the number even when the warmup is skipped.
    # Two independent defences, because this one already got through once.
    if not args.no_warmup and not args.adopt:
        print("Warmup (excluded from comparison; it exists so the first arm does not pay "
              "the model load):")
        # **The warmup uses the first arm's own bundle**, not the default one. Its whole job
        # is to leave resident exactly what the measured arms will use — and a bundle carries
        # a LoRA, so warming on `default` would leave the first real arm still paying for a
        # LoRA load. "Warm" has to mean warm for the thing being measured.
        _, warm_sampling, warm_attention, _ = arms[0]
        warm = payload_for(
            warm_attention, warmup_duration(), "mvp/attention-warmup", warm_sampling,
        )
        started = time.monotonic()
        response = post_json(f"{args.comfy_url}/prompt", {"prompt": warm})
        wait_for(args.comfy_url, response["prompt_id"])
        print(f"  warmup: {round(time.monotonic() - started, 2)}s at {WARMUP_FRAMES} frames")

    # Round-robin rather than one profile at a time: thermal drift and any background load
    # over a multi-hour run would otherwise land entirely on whichever arm ran last.
    adopt = args.adopt
    for repeat in range(1, args.repeats + 1):
        print(f"Repeat {repeat} of {args.repeats}:")
        for count, sampling, name, preview in arms:
            # The frame count is part of the identity, not just of the numbers. Without it a
            # 107-frame screen and a 226-frame promotion of the same arm share a label, and
            # the resume logic would "reuse" the screen as though it answered the cliff
            # question. Labels are how arms are told apart; anything that changes what an arm
            # measured belongs in one.
            # The preview value joins the label only when it is being varied, so every
            # label written before this axis existed keeps its exact name.
            tag = f"-p{preview}" if preview is not None else ""
            label = f"{sampling}+{name}-f{count}{tag}-r{repeat}"
            if label in existing:
                results.append(existing[label])
                print(f"  {label}: reused from disk")
                continue
            span, _, _, refs = fixtures[count]
            results.append(
                run_one(
                    count, sampling, name, repeat,
                    payload_for(name, span, f"mvp/attention-{label}", sampling, refs),
                    label,
                    adopt=adopt, preview=preview,
                )
            )
            # An adoption applies to exactly one arm: the one the killed run was on.
            adopt = None

    def write_report(sheets: list[str]) -> None:
        """The report, written twice: once the moment the last render lands, once with sheets.

        The renders are the expensive part and everything after them is ffmpeg on files that
        are already saved. A frame extraction that raises after three hours of GPU must not
        take the timings with it, so the numbers are on disk before anything can.
        """
        report = {
            "taken": today(),
            "comfyui_version": (stats.get("system") or {}).get("comfyui_version"),
            "comfyui_argv": launch_argv,
            "gpu": [device.get("name") for device in stats.get("devices") or []],
            "project": args.project,
            "shot": shot.get("id"),
            "seed": args.seed,
            # This invocation's frame count, and every count present in the directory —
            # a run directory accumulates arms across invocations and one number would
            # misdescribe the others. Each arm carries its own `frames` in `runs`.
            "frames_this_invocation": frame_counts,
            "frame_counts_present": sorted(
                {record["frames"] for record in results if record.get("frames")}
            ),
            # One window per frame count, because each length has its own lead and its own
            # take. A single top-level window would describe at most one of the arms.
            "windows": {
                str(count): {
                    "start": fixtures[count][1][0], "end": fixtures[count][1][1],
                    "take_lead_seconds": round(fixtures[count][2], 4),
                    "shot_duration": fixtures[count][0],
                }
                for count in frame_counts
            },
            "repeats": args.repeats,
            "warmup": not args.no_warmup,
            "sample_frames": {
                str(count): list(sample_indices(count)) for count in frame_counts
            },
            "published_attention_options": backends,
            "runs": results,
            "contact_sheets": sheets,
        }
        (run_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    write_report([])

    # --- Lip-sync evidence, after every render and before any verdict. ---
    #
    # **Grouped by frame count, and that grouping is load-bearing.** A run directory
    # accumulates arms across invocations, and arms rendered at different lengths cover
    # different windows of the song — frame 44 of a 107-frame take (a 4.5 s window) and frame
    # 44 of a 226-frame take (8.25 s) are different instants of the performance. Putting them
    # side by side would present two different moments as the same one, which is a *worse*
    # failure than no sheet at all: it manufactures an apparent lip-sync difference out of
    # arithmetic. Only arms of equal length are ever compared.
    frames_dir = run_dir / "frames"
    sampled: dict[str, dict[int, Path]] = {}
    finished = [record for record in results if record.get("output")]
    for record in finished:
        sampled[record["label"]] = sample_frames(
            ffmpeg, Path(record["output"]), frames_dir, record["label"],
            indices=sample_indices(record["frames"]),
        )
    sheets: list[str] = []
    for length in sorted({record["frames"] for record in finished}):
        peers = [record for record in finished if record["frames"] == length]
        for index in sample_indices(length):
            row = [
                (record["label"], sampled[record["label"]][index])
                for record in peers
                if index in sampled.get(record["label"], {})
            ]
            target = run_dir / f"mouth_compare-f{length}-i{index:04d}.png"
            contact_sheet(ffmpeg, row, target)
            if target.is_file():
                sheets.append(str(target))

    # --- Audio evidence, the third verdict and never folded into the other two. ---
    #
    # The Director heard a test render's audio as "a bit of a mutation of what I assume was
    # the input audio". H3 generates its audio rather than copying it, so some departure is
    # the model working — but *how much*, and whether it differs across bundles and backends,
    # is a fact this run can produce cheaply and nobody has. Fewer sampling steps is a quality
    # trade as much as a speed one, and the audio track is where that trade would land first
    # on a project whose premise is lip-sync.
    audio_dir = run_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    try:
        master = Path(
            next(
                item["file"]
                for item in fixtures[frame_counts[0]][3]
                if item["kind"] == "audio"
            )
        )
        # One reference window **per frame count**, for the same reason the contact sheets are
        # grouped: a 107-frame arm was conditioned on 4.5 s of the song and a 226-frame arm on
        # 8.25 s of it. Comparing either take against the other's window would measure the
        # length difference and report it as an audio difference.
        sources: dict[int, Path] = {}
        for length in sorted({record["frames"] for record in finished}):
            # The take's own window, through `fixture_for` — the same call the render used,
            # never a second copy of the arithmetic. Reusing it is the whole point: the last
            # time this window was computed twice the two answers disagreed, and every audio
            # number measured the disagreement rather than the audio. A length reached from a
            # reused record rather than this invocation's list is computed on demand, by that
            # same function.
            span_window = (
                fixtures[length][1] if length in fixtures else fixture_for(length)[1]
            )
            sources[length] = decode_audio(
                ffmpeg, master, audio_dir / f"master-window-f{length}.wav", span_window,
            )
        for record in finished:
            length = record["frames"]
            take_audio = decode_audio(
                ffmpeg, Path(record["output"]), audio_dir / f"{record['label']}.wav"
            )
            record["audio"] = audio_comparison(take_audio, sources[length])
            print(f"  {record['label']} audio: {record['audio']}")
    except (subprocess.CalledProcessError, StopIteration, OSError) as error:
        # Never fatal. The renders and the frames are the expensive evidence and they are
        # already on disk; a failed audio decode must reduce what is known, not destroy it.
        for record in finished:
            record.setdefault("audio", {"error": str(error)})
        print(f"Audio comparison unavailable: {error}", file=sys.stderr)

    # Records are written when an arm lands, which is before its audio is measured — so they
    # are re-written here or the per-arm evidence would disagree with report.json about the
    # same run. The record is the durable artefact; it must be the complete one.
    for record in results:
        target = records_dir / f"{record['label']}.json"
        if target.exists():
            target.write_text(json.dumps(record, indent=2), encoding="utf-8")

    write_report(sheets)

    print(
        "\narm                              steps   s/frame  execution s   "
        "audio r   lag ms  engagement"
    )
    for record in results:
        audio = record.get("audio") or {}
        # A cut arm prints as a dash rather than a number. It has no cost — it sampled no
        # steps — and putting any figure in a s/frame column would invite it to be compared
        # with arms that measured one.
        cost = record.get("seconds_per_frame")
        span = record.get("sampling_seconds")
        print(
            f"  {record['label']:<31}{record['steps']:>6}"
            f"{('-' if cost is None else cost)!s:>10}"
            f"{('-' if span is None else span)!s:>12}"
            f"{record.get('seconds_per_step') or '-'!s:>7}"
            f"{record.get('non_sampling_seconds') or '-'!s:>10}"
            f"{audio.get('correlation', '-')!s:>9}{audio.get('lag_ms', '-')!s:>9}"
            f"  {record['engagement']}"
        )
    cut = [r["label"] for r in results if r.get("engagement") == "did-not-complete"]
    if cut:
        print(
            f"\nDID NOT COMPLETE: {cut}. These arms never sampled a step — the memory-bound "
            f"signature, evidence in each arm's record. A backend that cannot fit this frame "
            f"count is a result about the frame count, not a slower timing."
        )
    inert = [record["label"] for record in results if record["engagement"] == "fell-back"]
    if inert:
        print(
            f"\nINCONCLUSIVE arms (ComfyUI substituted PyTorch attention): {inert}. "
            f"Their timings measure the fallback, not the backend they name."
        )
    if any(
        record.get("seconds_per_frame")
        and record.get("seconds_per_frame_basis") == "whole-render"
        for record in results
    ):
        print(
            "\nSome arms report s/frame over the WHOLE render, because no sampling summary "
            "was found in their log window. Those numbers include model load and are NOT "
            "comparable with the sampling-based ones."
        )
    print(
        "\ns/frame is SAMPLING time per frame. Model load sits in the non-samp column and is "
        "deliberately outside the cost: a cold checkpoint costs ~62 s on this machine, and on "
        "2026-08-22 it was read as a 24% backend difference that did not exist."
    )
    print(f"\nEvidence in {run_dir}")
    print(
        "Three verdicts, never averaged: speed, audio, lip-sync. Open every "
        "mouth_compare-*.png before drawing one — the LTX enhancer was sharper and later, "
        "and nothing in a timing table said so. Fewer steps is a picture trade as well as a "
        "speed one, and which trade is worth making is the Director's call, not this file's."
    )


if __name__ == "__main__":
    main()
