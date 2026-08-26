"""Browser QA for slice D2: the Monitor plays the look, and says so when it stops being true.

**This slice is mostly a looking problem, and this script is most of the gate.** The offline
harness executes every decision -- whether a Shot wants a preview, whether the clip in hand is a
picture of the look as it stands, what the label says -- against a stub DOM under node. What a stub
DOM structurally cannot see is the thing this whole slice is about: whether the picture ever goes
black, freezes, or shows a frame belonging to another Shot, in the milliseconds between one clip
and the next.

So this script does not re-prove any decision. It reconstructs, **frame by frame**, what a Director
would be looking at: a `requestAnimationFrame` sampler reads the composited answer -- which layer is
opaque, what clip is on it, what its `currentTime` is, and the average RGB of the picture itself,
drawn to a canvas -- and every claim below is an assertion over that log. A black flash is a sample
whose RGB is near zero; a frozen frame is a run of samples whose `currentTime` never moves; a frame
from the previous Shot is a sample whose clip belongs to it.

**"Is it actually graded?" is measured, not judged.** The take is a moving colour pattern and the
stack is full monochrome, so the grade is the collapse of saturation to zero -- a number, sampled
off both elements at the same moment.

**No GPU is spent and nothing reaches `/prompt`.** The takes are synthesized with ffmpeg under an
isolated `MVP_COMFY_ROOT`; every preview is the ffmpeg transcode `POST .../preview` runs, which is
what this code is for. ComfyUI is pointed at a dead port and never contacted.

What is driven, in the spec's own order:

1. A Shot with no stack: today's Monitor, and **no preview request at all**, read from the
   browser's own resource timings rather than from the picture.
2. A Shot with a stack: the picture is visibly graded, measured as saturation.
3. A parameter changed while it plays: the transition, sampled every frame. No black, no blank,
   no freeze, and the `STALE` label over moving picture.
4. The new clip lands: the swap, and the label clearing.
5. A slider dragged continuously for several seconds: no blackout, no percentage, no spinner,
   and it settles on the final value.
6. The other Shot selected mid-render: no frame belonging to the Shot just left.
7. A `.cube` a stack names deleted: the reason named, the picture kept.

Screenshots of the graded picture, the stale state with its label over moving picture, and the
failure state are written to `test-artifacts/`.

Run from the repo root -- it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_monitor_preview.py [--port 8779]

Assumes: nothing listening on the port, Microsoft Edge and its WebDriver installed, ffmpeg on
PATH. ComfyUI does not need to be running.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from e2e_support import (
    ManagedServer,
    StaleServer,
    artifact_dir,
    clear_toasts,
    console_gate,
    edge_driver,
    get_json,
    post_json,
    post_multipart,
    put_json,
    report,
    resource_hits,
    settle,
)
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

NAME = "monitor-preview"
FLAT = "shot_flat"
LOOK = "shot_look"

#: The sampler. Installed once, cleared between sections, and read back as a list of frames.
#: `willReadFrequently` because this reads pixels on every animation frame and the default
#: GPU-backed canvas makes each read a stall.
SAMPLER = """
window.__d2 = { frames: [], running: true };
const canvas = document.createElement('canvas');
canvas.width = 32; canvas.height = 32;
const context = canvas.getContext('2d', { willReadFrequently: true });
// The average colour of the frame, and its average *per-pixel* saturation.
//
// Two numbers and not one, because they answer different questions and one of them is a trap:
// the saturation of the average colour of a colour pattern is zero -- the colours cancel -- and
// reading grading off it says a saturated test pattern is already monochrome. So saturation is
// measured per pixel and then averaged. Brightness is the opposite way round: an average is
// exactly what "is this rectangle black" wants.
const sample = (video) => {
  if (!video || !video.videoWidth) return null;
  try {
    context.drawImage(video, 0, 0, 32, 32);
    const data = context.getImageData(4, 4, 24, 24).data;
    let red = 0, green = 0, blue = 0, spread = 0;
    for (let index = 0; index < data.length; index += 4) {
      const r = data[index], g = data[index + 1], b = data[index + 2];
      red += r; green += g; blue += b;
      spread += Math.max(r, g, b) - Math.min(r, g, b);
    }
    const pixels = data.length / 4;
    return {
      rgb: [Math.round(red / pixels), Math.round(green / pixels), Math.round(blue / pixels)],
      saturation: Math.round(spread / pixels),
    };
  } catch (error) { return 'blocked:' + error.name; }
};
const tick = () => {
  const frame = document.querySelector('#timeline-monitor');
  const previewing = frame.classList.contains('previewing');
  const on = document.querySelector('.monitor-preview.on');
  // What the viewer sees: the preview layer is opaque over the take whenever the Monitor is
  // previewing, and the take is the picture otherwise. Reconstructed from the same two facts
  // the stylesheet composites from.
  const seen = previewing && on ? on : document.querySelector('#monitor-video');
  window.__d2.frames.push({
    t: Math.round(performance.now()),
    previewing,
    which: seen ? seen.id : '',
    url: (seen && seen.dataset.url) || '',
    at: seen ? Number(seen.currentTime.toFixed(3)) : null,
    ready: seen ? seen.readyState : null,
    rgb: sample(seen),
    stale: document.querySelector('#monitor-stale').textContent,
    note: document.querySelector('#monitor-note').textContent,
    showing: frame.classList.contains('showing-take'),
  });
  if (window.__d2.running) requestAnimationFrame(tick);
};
requestAnimationFrame(tick);
"""

#: A slow render, simulated in the client. The route answers a small preview in tens of
#: milliseconds, and a state that lasts 115 ms cannot be photographed: two screenshot round trips
#: are longer than the whole of it, so the first attempt at this caught the picture *after* the
#: swap and reported it as the stale state. The delay is applied to the **reply**, after the
#: server has answered, so nothing about the render changes -- what is stretched is exactly the
#: window in which the Director is looking at the previous picture, which is what a preview of a
#: full-size window would do on its own.
SLOW_PREVIEW = """
window.__slowPreview = 0;
const realFetch = window.fetch;
window.fetch = async (path, options) => {
  const answer = await realFetch(path, options);
  if (window.__slowPreview && String(path).endsWith('/preview')) {
    await new Promise((resolve) => setTimeout(resolve, window.__slowPreview));
  }
  return answer;
};
"""

#: Whether the `STALE` label is drawn over the picture rather than over a letterbox bar, measured
#: from the rectangle `object-fit: contain` actually draws the clip into.
LABEL_OVER_PICTURE = """
const layer = document.querySelector('.monitor-preview.on');
const box = layer.getBoundingClientRect();
const scale = Math.min(box.width / layer.videoWidth, box.height / layer.videoHeight);
const drawn = {
  left: box.left + (box.width - layer.videoWidth * scale) / 2,
  top: box.top + (box.height - layer.videoHeight * scale) / 2,
  width: layer.videoWidth * scale,
  height: layer.videoHeight * scale,
};
const label = document.querySelector('#monitor-stale').getBoundingClientRect();
const style = getComputedStyle(document.querySelector('#monitor-stale'));
return {
  over_picture: label.left >= drawn.left - 1 && label.right <= drawn.left + drawn.width + 1
    && label.top >= drawn.top - 1 && label.bottom <= drawn.top + drawn.height + 1,
  label: { left: Math.round(label.left), top: Math.round(label.top),
           width: Math.round(label.width), height: Math.round(label.height) },
  picture: { left: Math.round(drawn.left), top: Math.round(drawn.top),
             width: Math.round(drawn.width), height: Math.round(drawn.height) },
  font: style.font || style.fontFamily,
  colour: style.color,
  plate: style.backgroundColor,
  text: document.querySelector('#monitor-stale').textContent,
};
"""

RESET = "window.__d2.frames.length = 0;"
READ = "return window.__d2.frames;"


def dead_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def synthesize_take(target: Path, seconds: float = 8.0) -> None:
    """A real take: a moving, saturated test pattern, long enough to hold a 4 s window.

    Moving, because a frozen frame and a playing one are indistinguishable in a still colour --
    and "never a frozen frame" is one of the two claims this script exists to check. Saturated,
    because the other claim is that the grade is visible, and full monochrome is the collapse of
    exactly that.

    Wide, and that is the third claim: `object-fit: contain` letterboxes anything whose shape
    differs from the Monitor's, and a corner label photographed against a letterbox bar proves
    nothing about a label over *moving picture*. At roughly the pane's own 4.6:1 the clip fills
    it, and `label_over_picture` below measures that it really did.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=928x200:rate=24:duration={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=180)
    if not target.is_file() or target.stat().st_size == 0:
        raise StaleServer(f"ffmpeg wrote nothing at {target}")


def synthesize_song(target: Path, seconds: float = 12.0) -> None:
    with wave.open(str(target), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(b"\x00\x00" * int(seconds * 8000))


def seed(base_url: str, comfy_root: Path) -> str:
    """Two Shots, both with an approved take; one carrying a stack and one not."""
    project = post_json(f"{base_url}/api/projects", {"name": "Monitor preview browser QA"})
    project_id = project["id"]
    song = artifact_dir() / f"{NAME}-song.wav"
    synthesize_song(song)
    post_multipart(
        f"{base_url}/api/projects/{project_id}/songs/upload",
        {"title": "Preview QA song", "duration": "12.0"},
        ("file", song),
    )
    shots = []
    for index, shot_id in enumerate((FLAT, LOOK)):
        output = f"music-video-producer/{project_id}/shots/{shot_id}-h3_00001.mp4"
        synthesize_take(comfy_root / "output" / output)
        shots.append({
            "id": shot_id, "start": index * 4, "duration": 4,
            "prompt": "The rooftop, wide." if index == 0 else "The same rooftop, graded.",
            "mode": "text_to_video", "status": "complete", "latest_output": output,
        })
    put_json(f"{base_url}/api/projects/{project_id}/shots", {"shots": shots})
    # Approval is its own route and its own decision: the preview renders the *approved* take
    # because the export does, so both Shots are approved before anything is graded.
    for shot_id in (FLAT, LOOK):
        post_json(f"{base_url}/api/projects/{project_id}/shots/{shot_id}/approve")
    # Full monochrome, which is the default the card is added at. Unmistakable against a
    # saturated pattern, so "is it graded" is a measurement rather than an opinion.
    put_json(
        f"{base_url}/api/projects/{project_id}/shots/{LOOK}/effects",
        {"effects": [{"effect": "monochrome", "enabled": True, "parameters": {}}]},
    )
    return project_id


def select_project(driver, wait, project_id: str) -> None:
    wait.until(EC.presence_of_element_located((By.ID, "project-select")))
    option = wait.until(
        lambda browser: browser.find_element(
            By.CSS_SELECTOR, f'#project-select option[value="{project_id}"]'
        )
    )
    option.click()
    wait.until(
        lambda browser: browser.find_element(By.ID, "project-select").get_attribute("value")
        == project_id
    )


def seek_to(driver, seconds: float) -> None:
    """The real pointerdown the timeline handler listens for, at the pixel it maps this second to."""
    driver.execute_script(
        """
        const canvas = document.querySelector('#timeline-canvas');
        const rect = canvas.getBoundingClientRect();
        const label = document.querySelector('#zoom-label').textContent;
        const pps = 16 * (parseFloat(label) / 100);
        const x = rect.left + 90 + arguments[0] * pps;
        canvas.dispatchEvent(new PointerEvent('pointerdown', {
          clientX: x, clientY: rect.top + 10, bubbles: true,
        }));
        """,
        seconds,
    )


def monitor_now(driver) -> dict:
    """The Monitor's whole visible state in one read, for the assertions and the diagnostics."""
    return driver.execute_script(
        """
        const frame = document.querySelector('#timeline-monitor');
        const on = document.querySelector('.monitor-preview.on');
        return {
          previewing: frame.classList.contains('previewing'),
          showing: frame.classList.contains('showing-take'),
          stale: document.querySelector('#monitor-stale').textContent,
          note: document.querySelector('#monitor-note').textContent,
          overlay: document.querySelector('#monitor-overlay').textContent,
          onUrl: on ? (on.dataset.url || '') : '',
          onReady: on ? on.readyState : null,
          layers: ['a', 'b'].map((name) => {
            const layer = document.querySelector('#monitor-preview-' + name);
            return { name, url: layer.dataset.url || '', shot: layer.dataset.shot || '',
                     on: layer.classList.contains('on'), ready: layer.readyState,
                     at: Number(layer.currentTime.toFixed(3)), paused: layer.paused };
          }),
        };
        """
    )


def await_label(driver, wanted: str, seconds: float = 25.0) -> bool:
    """Whether the corner label reached `wanted` inside `seconds`. Observed, never thrown:
    a browser QA script that dies at the first surprise reports nothing about the surprise."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if driver.execute_script(
            "return document.querySelector('#monitor-stale').textContent;"
        ) == wanted:
            return True
        time.sleep(0.05)
    return False


def frames(driver) -> list[dict]:
    return driver.execute_script(READ)


def black(sample) -> bool:
    """Whether this one sample was an effectively black rectangle.

    A sample with no frame decoded at all counts: a video element with no picture in it *is* a
    black rectangle on screen, which is the same defect however it came about.
    """
    if not isinstance(sample, dict):
        return True
    return max(sample["rgb"]) <= 10


def blackout(sampled: list[dict], previewing_only: bool = False) -> list[dict]:
    """Every sample whose picture was effectively black — the failure this slice may never have.

    `previewing_only` narrows it to the frames the Preview Clip was responsible for. The take
    element blanks for a frame or two when the playhead crosses into another Shot's take, and
    that is the Monitor's own long-standing behaviour on a Shot with no effects — outside this
    slice's Ask-First boundary, and not a thing to blame the preview layers for.
    """
    return [
        frame for frame in sampled
        if (frame["previewing"] or not previewing_only) and black(frame["rgb"])
    ]


def advanced(sampled: list[dict]) -> bool:
    """Whether the picture was moving across these samples — the check for a frozen frame."""
    times = [frame["at"] for frame in sampled if frame["at"] is not None]
    return len(set(times)) > 1


def main() -> int:
    port = 8779
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    results: dict[str, dict] = {}
    comfy_root = Path(tempfile.mkdtemp(prefix="mvp-preview-comfy-"))
    os.environ["MVP_COMFY_ROOT"] = str(comfy_root)
    os.environ["MVP_COMFY_URL"] = f"http://127.0.0.1:{dead_port()}"
    with ManagedServer(port, label=NAME) as server:
        project_id = seed(server.base_url, comfy_root)
        driver = edge_driver()
        try:
            wait = WebDriverWait(driver, 30)
            driver.get(server.base_url)
            select_project(driver, wait, project_id)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            wait.until(
                lambda browser: len(
                    browser.find_elements(By.CSS_SELECTOR, "#shots-track .shot-clip")
                ) == 2
            )
            driver.execute_script(SAMPLER)
            driver.execute_script(SLOW_PREVIEW)

            # === 1. The un-graded Shot: today's Monitor, and no request ======================
            seek_to(driver, 1.0)
            settle(driver, "#timeline-monitor", quiet_ms=600)
            time.sleep(1.5)
            flat = driver.execute_script(
                "return {"
                " previewing: document.querySelector('#timeline-monitor')"
                "   .classList.contains('previewing'),"
                " showing: document.querySelector('#timeline-monitor')"
                "   .classList.contains('showing-take'),"
                " stale: document.querySelector('#monitor-stale').textContent,"
                " note: document.querySelector('#monitor-note').textContent,"
                " takeSrc: document.querySelector('#monitor-video').currentSrc,"
                "};"
            )
            asked = resource_hits(driver, "/preview")
            driver.save_screenshot(str(artifact_dir() / f"{NAME}-01-ungraded-shot.png"))
            results["no-stack-asks-for-nothing"] = {
                "ok": asked == 0 and flat["showing"] and not flat["previewing"]
                and flat["stale"] == "" and "/take" in flat["takeSrc"],
                "preview_requests": asked, **flat,
            }

            # === 2. The graded Shot: the picture is graded, and measurably so ================
            seek_to(driver, 5.0)
            wait.until(
                lambda browser: browser.execute_script(
                    "return document.querySelector('#timeline-monitor')"
                    ".classList.contains('previewing');"
                )
            )
            time.sleep(1.2)
            driver.execute_script(RESET)
            time.sleep(1.0)
            graded_frames = frames(driver)
            # The grade, measured on both elements at the same moment. The take is the picture
            # the export would ship ungraded; the preview is the picture it will ship. Full
            # monochrome is the collapse of saturation, so this is a number rather than a look.
            both = driver.execute_script(
                """
                const canvas = document.createElement('canvas');
                canvas.width = 32; canvas.height = 32;
                const context = canvas.getContext('2d', { willReadFrequently: true });
                const read = (video) => {
                  context.drawImage(video, 0, 0, 32, 32);
                  const data = context.getImageData(4, 4, 24, 24).data;
                  let r = 0, g = 0, b = 0, spread = 0;
                  for (let i = 0; i < data.length; i += 4) {
                    r += data[i]; g += data[i + 1]; b += data[i + 2];
                    spread += Math.max(data[i], data[i + 1], data[i + 2])
                            - Math.min(data[i], data[i + 1], data[i + 2]);
                  }
                  const n = data.length / 4;
                  return {
                    rgb: [Math.round(r / n), Math.round(g / n), Math.round(b / n)],
                    saturation: Math.round(spread / n),
                  };
                };
                const on = document.querySelector('.monitor-preview.on');
                return {
                  take: read(document.querySelector('#monitor-video')),
                  preview: read(on),
                  clip: on.dataset.url,
                  size: [on.videoWidth, on.videoHeight],
                };
                """
            )
            driver.save_screenshot(str(artifact_dir() / f"{NAME}-02-graded-picture.png"))
            driver.find_element(By.ID, "timeline-monitor").screenshot(
                str(artifact_dir() / f"{NAME}-02-graded-monitor.png"))
            results["the-picture-is-graded"] = {
                "ok": both["preview"]["saturation"] <= 4 and both["take"]["saturation"] >= 30
                and not blackout(graded_frames) and advanced(graded_frames),
                "take": both["take"], "preview": both["preview"],
                "clip": both["clip"], "clip_size": both["size"],
                "samples": len(graded_frames),
                "kept_moving": advanced(graded_frames),
                "black_frames": len(blackout(graded_frames)),
            }

            # === 3 & 4. A parameter changed while it plays, watched frame by frame ===========
            driver.find_element(By.CSS_SELECTOR, f'[data-shot-id="{LOOK}"]').click()
            settle(driver, "#shot-inspector", quiet_ms=600)
            driver.find_element(By.ID, "shot-tab-effects").click()
            settle(driver, "#shot-inspector", quiet_ms=600)
            wait.until(EC.presence_of_element_located((By.ID, "effect-param-0-amount")))
            before = resource_hits(driver, "/preview")
            driver.execute_script(RESET)
            # A real change to a real parameter, written the way a released slider writes it.
            driver.execute_script(
                "const input = document.getElementById('effect-param-0-amount');"
                "input.value = '0.35';"
                "input.dispatchEvent(new Event('input', { bubbles: true }));"
                "input.dispatchEvent(new Event('change', { bubbles: true }));"
            )
            # The label is a state that lasts as long as a transcode, so it is caught by watching
            # rather than by asking afterwards.
            saw_stale = False
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if driver.execute_script(
                    "return document.querySelector('#monitor-stale').textContent;"
                ) == "STALE":
                    saw_stale = True
                    driver.save_screenshot(str(artifact_dir() / f"{NAME}-03-stale-label.png"))
                    driver.find_element(By.ID, "timeline-monitor").screenshot(
                        str(artifact_dir() / f"{NAME}-03-stale-monitor.png"))
                    break
                time.sleep(0.03)
            cleared = await_label(driver, "")
            time.sleep(0.8)
            transition = frames(driver)
            clips = [frame["url"] for frame in transition if frame["previewing"]]
            stale_run = [frame for frame in transition if frame["stale"]]
            results["the-transition-never-blacks-out"] = {
                "ok": not blackout(transition) and advanced(transition)
                and len({clip for clip in clips if clip}) == 2
                and all(frame["stale"] == "STALE" for frame in stale_run)
                and all(frame["previewing"] for frame in stale_run)
                and resource_hits(driver, "/preview") == before + 1,
                "samples": len(transition),
                "black_frames": len(blackout(transition)),
                "kept_moving": advanced(transition),
                "clips_seen": sorted({clip.rsplit("/", 1)[-1] for clip in clips if clip}),
                "stale_samples": len(stale_run),
                "stale_label_seen": saw_stale,
                "stale_note": next((frame["note"] for frame in stale_run), ""),
                "stale_kept_picture": all(frame["previewing"] for frame in stale_run),
                "requests": resource_hits(driver, "/preview") - before,
                # Never a percentage, never a spinner, in any sample.
                "labels_seen": sorted({frame["stale"] for frame in transition}),
            }
            results["the-swap-clears-the-label"] = {
                "ok": cleared and transition[-1]["stale"] == ""
                and transition[-1]["note"] == "" and transition[-1]["previewing"],
                "label_cleared": cleared,
                "last": transition[-1],
                "state": monitor_now(driver),
            }

            # === 4b. The stale state, held still long enough to be photographed =============
            #
            # Everything above is the real timing, sampled. This is the same state with the reply
            # delayed in the client so a camera can be pointed at it: the render has finished, the
            # Monitor has not been told yet, and what is on screen is the previous look with the
            # label over it. A full-size window would produce this for real.
            driver.execute_script("window.__slowPreview = 5000;")
            driver.execute_script(RESET)
            driver.execute_script(
                "const input = document.getElementById('effect-param-0-amount');"
                "input.value = '0.75';"
                "input.dispatchEvent(new Event('input', { bubbles: true }));"
                "input.dispatchEvent(new Event('change', { bubbles: true }));"
            )
            held_stale = await_label(driver, "STALE", 20)
            time.sleep(0.4)
            label = driver.execute_script(LABEL_OVER_PICTURE)
            driver.save_screenshot(str(artifact_dir() / f"{NAME}-04-stale-held.png"))
            driver.find_element(By.ID, "timeline-monitor").screenshot(
                str(artifact_dir() / f"{NAME}-04-stale-held-monitor.png"))
            during_stale = frames(driver)
            driver.execute_script("window.__slowPreview = 0;")
            landed_after = await_label(driver, "", 25)
            results["the-stale-label-sits-over-moving-picture"] = {
                "ok": held_stale and landed_after and label["over_picture"]
                and "Consolas" in label["font"] and label["text"] == "STALE"
                and advanced(during_stale) and not blackout(during_stale)
                and all(frame["previewing"] for frame in during_stale),
                "label_seen": held_stale,
                "cleared_afterwards": landed_after,
                "kept_moving_while_stale": advanced(during_stale),
                "black_frames": len(blackout(during_stale)),
                "samples": len(during_stale),
                **label,
            }

            # === 5. A slider dragged continuously for several seconds ========================
            driver.execute_script(RESET)
            during = resource_hits(driver, "/preview")
            slider = driver.find_element(By.ID, "effect-param-0-amount")
            chain = ActionChains(driver)
            chain.click_and_hold(slider)
            for step in range(24):
                chain.move_by_offset(-3 if step % 2 == 0 else -2, 0)
                chain.pause(0.16)
            chain.release()
            chain.perform()
            settle(driver, "#shot-inspector", quiet_ms=700)
            dragged = frames(driver)
            drag_cleared = await_label(driver, "")
            time.sleep(0.6)
            after_drag = frames(driver)
            settled_state = monitor_now(driver)
            stored = get_json(f"{server.base_url}/api/projects/{project_id}")
            settled_stack = next(
                shot["effects"] for shot in stored["shots"] if shot["id"] == LOOK)
            shown = driver.execute_script(
                "return document.getElementById('effect-readout-0-amount').textContent;")
            driver.save_screenshot(str(artifact_dir() / f"{NAME}-05-after-drag.png"))
            results["the-drag-never-blacks-out"] = {
                "ok": drag_cleared and not blackout(after_drag) and advanced(dragged)
                and all(frame["stale"] in ("", "STALE") for frame in after_drag)
                and float(settled_stack[0]["parameters"].get("amount", 1.0)) == float(shown),
                "label_cleared": drag_cleared,
                "settled_state": settled_state,
                "samples_during_drag": len(dragged),
                "black_frames": len(blackout(after_drag)),
                "kept_moving": advanced(dragged),
                "labels_seen": sorted({frame["stale"] for frame in after_drag}),
                "requests_from_the_drag": resource_hits(driver, "/preview") - during,
                "stored_amount": settled_stack[0]["parameters"].get("amount"),
                "readout": shown,
            }

            # === 6. The other Shot, selected mid-render ======================================
            driver.execute_script(RESET)
            graded_clip = driver.execute_script(
                "return document.querySelector('.monitor-preview.on').dataset.url;")
            driver.execute_script(
                "const input = document.getElementById('effect-param-0-amount');"
                "input.value = '0.9';"
                "input.dispatchEvent(new Event('input', { bubbles: true }));"
                "input.dispatchEvent(new Event('change', { bubbles: true }));"
            )
            seek_to(driver, 1.0)
            time.sleep(2.5)
            crossed = frames(driver)
            # From the moment the playhead landed on the un-graded Shot, nothing belonging to the
            # graded one may appear. The move is the first sample that stopped previewing.
            after_move = crossed[next(
                (index for index, frame in enumerate(crossed) if not frame["previewing"]),
                len(crossed)):]
            results["no-frame-from-the-shot-just-left"] = {
                "ok": bool(after_move)
                and all(frame["url"] != graded_clip for frame in after_move)
                and all(not frame["previewing"] for frame in after_move)
                and not blackout(after_move, previewing_only=True),
                "graded_clip": graded_clip.rsplit("/", 1)[-1],
                "samples_after_the_move": len(after_move),
                "urls_after_the_move": sorted({frame["url"] for frame in after_move}),
                "black_frames_from_the_preview": len(
                    blackout(after_move, previewing_only=True)),
                # Reported, not asserted. The Monitor's own take element reloads when the
                # playhead crosses into a Shot with a different take, and it has blanked for a
                # frame or two while it does since long before this slice. Changing that is a
                # change to what the Monitor shows for a Shot with **no** effects, which this
                # spec puts behind Ask First.
                "black_frames_from_the_take_reloading": len(blackout(after_move)),
            }

            # === 7. A look whose `.cube` has been deleted ====================================
            #
            # The stack is set up through the route and the page reloaded onto it, so the browser
            # holds the same manifest the server does; the *failing* change is then made in the
            # interface, because "the Director changed something and the render refused" is the
            # state under test, and a manifest edited behind the page's back is not that state.
            clear_toasts(driver)
            catalogue = get_json(f"{server.base_url}/api/effects/catalogue")
            looks = catalogue["looks"]
            failure: dict = {"ok": False, "why": "no looks on this machine to delete"}
            if looks:
                look = looks[0]
                put_json(
                    f"{server.base_url}/api/projects/{project_id}/shots/{LOOK}/effects",
                    {"effects": [
                        {"effect": "monochrome", "enabled": True, "parameters": {"amount": 0.5}},
                        {"effect": "lut_look", "enabled": True,
                         "parameters": {"lut": look["lut_id"]}},
                    ]},
                )
                driver.get(server.base_url)
                select_project(driver, wait, project_id)
                driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
                wait.until(
                    lambda browser: len(
                        browser.find_elements(By.CSS_SELECTOR, "#shots-track .shot-clip")
                    ) == 2
                )
                driver.execute_script(SAMPLER)
                driver.execute_script(SLOW_PREVIEW)
                seek_to(driver, 5.0)
                # Wait for this stack's own clip rather than merely for a preview: the memo is
                # empty after a reload, so what appears is the render this stack asks for.
                wait.until(
                    lambda browser: browser.execute_script(
                        "const on = document.querySelector('.monitor-preview.on');"
                        "return Boolean(on) && document.querySelector('#timeline-monitor')"
                        ".classList.contains('previewing')"
                        " && document.querySelector('#monitor-stale').textContent === '';")
                )
                time.sleep(1.0)
                kept = driver.execute_script(
                    "return document.querySelector('.monitor-preview.on').dataset.url;")
                cube = (server.data_root / "luts" / f"{look['name']}.cube")
                cube.unlink()
                driver.find_element(By.CSS_SELECTOR, f'[data-shot-id="{LOOK}"]').click()
                settle(driver, "#shot-inspector", quiet_ms=600)
                driver.find_element(By.ID, "shot-tab-effects").click()
                settle(driver, "#shot-inspector", quiet_ms=600)
                wait.until(EC.presence_of_element_located((By.ID, "effect-param-0-amount")))
                driver.execute_script(RESET)
                # A change to the stack is what asks again, and the look it names is now gone.
                driver.execute_script(
                    "const input = document.getElementById('effect-param-0-amount');"
                    "input.value = '0.8';"
                    "input.dispatchEvent(new Event('input', { bubbles: true }));"
                    "input.dispatchEvent(new Event('change', { bubbles: true }));"
                )
                toast_text = ""
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    toasts = driver.find_elements(By.CSS_SELECTOR, "#toast-region .toast")
                    if toasts:
                        toast_text = toasts[-1].text
                        break
                    time.sleep(0.1)
                time.sleep(0.5)
                refused = frames(driver)
                driver.save_screenshot(str(artifact_dir() / f"{NAME}-07-failure.png"))
                driver.find_element(By.ID, "timeline-monitor").screenshot(
                    str(artifact_dir() / f"{NAME}-07-failure-monitor.png"))
                still_there = monitor_now(driver)
                stack_now = next(
                    shot["effects"] for shot in
                    get_json(f"{server.base_url}/api/projects/{project_id}")["shots"]
                    if shot["id"] == LOOK)
                failure = {
                    "ok": bool(toast_text) and still_there["onUrl"] == kept
                    and still_there["previewing"] and still_there["stale"] == "STALE"
                    and not blackout(refused) and advanced(refused)
                    and [spec["effect"] for spec in stack_now] == ["monochrome", "lut_look"],
                    "look": look["name"],
                    "deleted": cube.name,
                    "toast": toast_text,
                    "clip_kept": still_there["onUrl"] == kept,
                    "black_frames": len(blackout(refused)),
                    "kept_moving": advanced(refused),
                    "stack_after_the_refusal": stack_now,
                    "state": still_there,
                }
            results["a-deleted-look-names-its-reason-and-keeps-the-picture"] = failure

            driver.execute_script("window.__d2.running = false;")
            results["console"] = {"ok": True}
            # The refused preview is a 502 the browser logs as a network error. It is what
            # section 7 drove on purpose, so it is separated out and reported rather
            # than filtered away.
            console_gate(driver, NAME, results["console"], expected=["/preview"])
        finally:
            driver.quit()

    (artifact_dir() / f"{NAME}-results.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8")
    failures = [name for name, result in results.items() if not result.get("ok")]
    for name, result in results.items():
        report(name, result)
    if failures:
        print(f"FAILED: {failures}")
        return 1
    print(f"{NAME}: all sections passed")
    return 0


if __name__ == "__main__":
    os.chdir(Path(__file__).parent.parent)
    sys.exit(main())
