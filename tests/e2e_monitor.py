"""Browser QA for the Monitor, the trim nudge, the audio acceptance, and the line mutes.

The offline harness executes `monitorState`, `effectiveOffset`, `takeAudioControl` and
`trimNudgeControl` under node, so nothing here re-proves the decisions. What a stub DOM
structurally cannot see is the *wiring*: whether the video element actually switches and
seeks when the playhead moves, whether the overlay really covers the picture over a gap,
whether the acceptance checkbox truly un-mutes the element, and whether the line mutes
reach the master audio and the frame. Those are exactly the failure modes a viewer has.

**No GPU is spent and nothing reaches `/prompt`.** The one take is synthesized with ffmpeg
(color + tone) under an isolated `MVP_COMFY_ROOT`; the song is a locally generated WAV.

Run from the repo root -- it starts and proves its own server::

    uv run --with selenium python tests/e2e_monitor.py [--port 8768]

Assumes: nothing listening on the port, Microsoft Edge + WebDriver, ffmpeg on PATH.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from e2e_support import (
    ManagedServer,
    StaleServer,
    artifact_dir,
    console_gate,
    edge_driver,
    post_json,
    post_multipart,
    put_json,
    report,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

NAME = "monitor"


def synthesize_take(target: Path, seconds: float = 6.5) -> None:
    """A real take: color + 440 Hz tone, long enough to hold shot A's 6 s window."""
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=orange:size=320x240:duration={seconds}:rate=24",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(target),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
    if not target.is_file() or target.stat().st_size == 0:
        raise StaleServer(f"ffmpeg wrote nothing at {target}")


def synthesize_song(target: Path, seconds: float = 12.0) -> None:
    with wave.open(str(target), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(b"\x00\x00" * int(seconds * 8000))


def seed(base_url: str, comfy_root: Path) -> str:
    project = post_json(f"{base_url}/api/projects", {"name": "Monitor browser QA"})
    project_id = project["id"]
    song = artifact_dir() / "monitor-song.wav"
    synthesize_song(song)
    post_multipart(
        f"{base_url}/api/projects/{project_id}/songs/upload",
        {"title": "Monitor QA song", "duration": "12.0"},
        ("file", song),
    )
    latest_output = f"music-video-producer/{project_id}/shots/shot_take-h3_00001.mp4"
    synthesize_take(comfy_root / "output" / latest_output)
    shots = [
        {"id": "shot_take", "start": 0, "duration": 6, "prompt": "The take on screen.",
         "mode": "text", "status": "complete", "latest_output": latest_output},
        {"id": "shot_bare", "start": 6, "duration": 6, "prompt": "Never rendered.",
         "mode": "text", "status": "draft"},
    ]
    put_json(f"{base_url}/api/projects/{project_id}/shots", {"shots": shots})
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
    """Dispatch the real pointerdown the timeline handler listens for, at the pixel the
    workspace itself maps this second to (the 90 px gutter plus pixelsPerSecond)."""
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


def monitor_state(driver) -> dict:
    return driver.execute_script(
        """
        const frame = document.querySelector('#timeline-monitor');
        const video = document.querySelector('#monitor-video');
        const audio = document.querySelector('#master-audio');
        return {
          showingTake: frame.classList.contains('showing-take'),
          overlay: document.querySelector('#monitor-overlay').textContent,
          src: video.currentSrc || video.src || '',
          muted: video.muted,
          currentTime: video.currentTime,
          audioMuted: audio.muted,
        };
        """
    )


def main() -> int:
    port = 8768
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    results: dict[str, dict] = {}
    comfy_root = Path(tempfile.mkdtemp(prefix="mvp-monitor-comfy-"))
    os.environ["MVP_COMFY_ROOT"] = str(comfy_root)
    with ManagedServer(port, label=NAME) as server:
        project_id = seed(server.base_url, comfy_root)
        driver = edge_driver()
        try:
            wait = WebDriverWait(driver, 25)
            driver.get(server.base_url)
            select_project(driver, wait, project_id)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            wait.until(
                lambda browser: len(
                    browser.find_elements(By.CSS_SELECTOR, "#shots-track .shot-clip")
                )
                == 2
            )

            # 1. Playhead 0 sits inside the shot with a take: the Monitor shows it, muted
            # (nothing accepted), with the take route as its source.
            wait.until(lambda b: monitor_state(b)["showingTake"])
            state = monitor_state(driver)
            results["take-on-load"] = {
                "ok": state["showingTake"] and state["muted"] and "/take" in state["src"],
                **state,
            }

            # 2. Seek into the unrendered shot: honest placeholder, never a stale frame.
            seek_to(driver, 8.0)
            wait.until(lambda b: not monitor_state(b)["showingTake"])
            state = monitor_state(driver)
            results["placeholder-over-bare-shot"] = {
                "ok": "no rendered take" in state["overlay"],
                **state,
            }

            # 3. Seek back inside the take: the element lands within the drift threshold.
            seek_to(driver, 2.0)
            wait.until(
                lambda b: monitor_state(b)["showingTake"]
                and abs(monitor_state(b)["currentTime"] - 2.0) < 0.25
            )
            results["seek-lands-in-take"] = {"ok": True, **monitor_state(driver)}

            # 4. Accept the take's audio in the inspector: the same field assembly mixes
            # by un-mutes the preview.
            driver.find_element(By.CSS_SELECTOR, '[data-shot-id="shot_take"]').click()
            wait.until(EC.presence_of_element_located((By.ID, "mix-take-audio")))
            driver.find_element(By.ID, "mix-take-audio").click()
            wait.until(lambda b: monitor_state(b)["muted"] is False)
            results["acceptance-unmutes"] = {"ok": True, **monitor_state(driver)}

            # 5. The trim nudge steps by frames and the readout follows. Reads go through
            # execute_script because the silent shot save rebuilds the inspector between a
            # find and a property read — the same staleness the shot-controls run recorded.
            def nudge_text(b):
                return b.execute_script(
                    "return document.querySelector('#nudge-value')?.textContent || '';"
                )

            before = nudge_text(driver)
            driver.execute_script("document.querySelector('#nudge-forward').click();")
            wait.until(lambda b: nudge_text(b) not in ("", before))
            after = nudge_text(driver)
            results["nudge-steps"] = {
                "ok": "0.042" in after, "before": before, "after": after,
            }

            # 6. The line mutes: video blanks the frame with a named overlay; song
            # silences the master element. Session-only, nothing persisted.
            driver.execute_script("document.querySelector('#mute-video').click();")
            wait.until(lambda b: "Video line muted" in monitor_state(b)["overlay"])
            driver.execute_script("document.querySelector('#mute-video').click();")
            driver.execute_script("document.querySelector('#mute-song').click();")
            wait.until(lambda b: monitor_state(b)["audioMuted"])
            state = monitor_state(driver)
            results["line-mutes"] = {"ok": state["showingTake"], **state}

            results["console"] = {"ok": True}
            console_gate(driver, NAME, results["console"])
        finally:
            driver.quit()

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
