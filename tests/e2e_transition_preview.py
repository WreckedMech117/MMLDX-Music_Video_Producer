"""Browser QA for story 11.5: the blend a Director can look at before exporting.

**What only a browser answers here.** The route is covered offline — `tests/test_shot_preview.py`
renders a real boundary with real ffmpeg and walks the decoded frames — and the key and the row's
states are covered in `tests/test_frontend_contract.py`. What neither can see is the thing this
epic's siblings kept getting wrong: whether the control is reachable, whether the sentence that
replaces it is drawn, and whether the clip that comes back **decodes and plays** in a real video
element rather than merely being served. Eleven defects across Epics 9 and 10 were caught only by
looking, and two more in the slice before this one — the most recent because a label landed on a
chip that only appeared once the fixture carried a real effect stack.

So this script drives the real gestures and reads the real paint:

1. **A paired boundary with no type chosen** — the row says which absence it is
   (`api.BOUNDARY_PREVIEW_UNTYPED`) and offers no control. An Overlap with nothing set is a hard
   cut (UX-DR8), and a Director sitting in that state is the one who most needs to be told what to
   do next.
2. **A type is chosen through the real select**, and the row gains `Watch blend`. The button is
   hit-tested rather than merely found: the Effects tab is a scrolling rail and the band panel has
   already once overflowed so far that Selenium reported `element not interactable`, which a
   Director meets as a control they cannot reach.
3. **The blend is watched.** The clip is fetched, decoded and playing — `readyState`,
   `videoWidth`/`videoHeight` against what the route said it served, and `currentTime` advancing.
   A `<video>` with a `src` that never decoded looks identical in the DOM to one that did.
4. **A different type is a different clip.** The `xfade` is the seventh input of the boundary
   fingerprint, so choosing another transition has to fetch another file — asserted on the
   element's own `src`, and on the request count from the page's resource timings.
5. **The incoming Shot's `Transition in` row offers the same clip**, addressed to the outgoing
   Shot (AD-30). A Director told "watch this blend" on one Shot and offered nothing on the next
   would be looking at two accounts of one transition.
6. **A one-sided transition is drawn as the absence it is**, and the Monitor asks for the Shot's
   own preview even though that Shot carries **no effect stack at all** — which is the gap this
   story found by looking rather than by reading. Before it, `shotPreviewWanted` refused to ask
   for any preview of a Shot with an empty stack, so a Director setting a fade-out on an ungraded
   Shot watched the Monitor go on showing the untreated take, silently, while the route was
   perfectly capable of rendering the fade.

Six screenshots go to `test-artifacts/`. No GPU is spent and nothing reaches ComfyUI, which is
pointed at a dead port; every clip is a local ffmpeg transcode, so **ffmpeg must be on PATH**.

Run from the repo root — it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_transition_preview.py [--port 8786]

Assumes: nothing listening on the port, Microsoft Edge and its WebDriver installed, and
`music_video_producer` importable from this checkout's `src/`.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))

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
    resource_hits,
    settle,
    visible_and_clickable,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

NAME = "transition-preview"

SONG_SECONDS = 8.0

#: `shot_01` runs 0 → 4 s and `shot_02` starts just over half a second before that, so the pair
#: really overlaps and the plan really composes a `TransitionClip`. The second Shot runs to the
#: song's end, so the plan tiles the whole song and the export would assemble — which is what makes
#: the previewed blend the export's blend rather than a picture of a state the export refuses.
#:
#: **0.51 and not 0.50, deliberately.** A Director drags freehand, so an Overlap is a float and the
#: blend is that float in frames: 0.51 s is twelve frames and a fraction, and the grid keeps the
#: twelve. The row was ruled on 2026-08-30 to state **what renders**, so this fixture is the only
#: shape in which that ruling is visible at all — at a grid-aligned 0.50 the old readout and the
#: new one print the same string and the screenshot proves nothing.
OVERLAP_SECONDS = 0.51
SHOTS = (
    ("shot_01", 0.0, 4.0),
    ("shot_02", 4.0 - OVERLAP_SECONDS, SONG_SECONDS - (4.0 - OVERLAP_SECONDS)),
)

#: The row that names the boundary between them from each side.
OUT_ROW = "transition-out"
IN_ROW = "transition-in"

#: What the `<video>` says about itself. `readyState >= 2` is HAVE_CURRENT_DATA — the element has
#: decoded the frame at its current position — and `videoWidth` is 0 until it has. A `src` that
#: 404s, or a file a browser cannot play, leaves both at zero and leaves the DOM looking correct.
CLIP_STATE = """
const video = document.querySelector(arguments[0]);
if (!video) return null;
return {
  src: video.getAttribute('src') || '',
  fingerprint: video.dataset.fingerprint || '',
  readyState: video.readyState,
  videoWidth: video.videoWidth,
  videoHeight: video.videoHeight,
  currentTime: video.currentTime,
  duration: video.duration,
  paused: video.paused,
  displayed: getComputedStyle(video).display,
  box: video.getBoundingClientRect().toJSON(),
};
"""

ROW_STATE = """
const row = document.querySelector('#' + arguments[0]).closest('.transition-row');
const note = document.querySelector('#' + arguments[0] + '-preview-note');
const watch = document.querySelector('#' + arguments[0] + '-watch');
const length = document.querySelector('#' + arguments[0] + '-length');
const label = row.querySelector('.transition-head label');
const rowBox = row.getBoundingClientRect();
const lengthBox = length ? length.getBoundingClientRect() : null;
return {
  state: row.dataset.state,
  edge: row.dataset.edge,
  value: document.querySelector('#' + arguments[0]).value,
  previewNote: note ? note.textContent : '',
  watchShown: Boolean(watch),
  watchText: watch ? watch.textContent : '',
  watchShot: watch ? (watch.dataset.shot || '') : '',
  length: length ? length.textContent : '',
  // The readout's own box against the row it sits in and the label it sits beside. The rail is
  // 250px and both share one line, so a readout that grew would either wrap or run into the
  // label — and neither is visible to any assertion about its text.
  rowWidth: rowBox.width,
  lengthWidth: lengthBox ? lengthBox.width : null,
  lengthOverflow: lengthBox ? lengthBox.right - rowBox.right : null,
  labelGap: lengthBox && label ? lengthBox.left - label.getBoundingClientRect().right : null,
  // One client rect per line box on an inline element -- exact, where dividing the height by a
  // computed `line-height` is not: that property resolves to the string `normal` here and
  // `parseFloat` answers NaN, which reads back as a null and asserts nothing.
  lengthLines: length ? length.getClientRects().length : null,
};
"""


def dead_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def synthesize_song(target: Path) -> None:
    """Silence at a real sample rate. The timeline needs a Song with a duration and a path."""
    with wave.open(str(target), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(b"\x00\x00" * int(SONG_SECONDS * 8000))


def synthesize_take(target: Path, colour: str) -> None:
    """A flat, saturated take, long enough to cover its window with the grid's slack.

    **Two different colours, which is the fixture containing the thing under test.** A blend
    between two identical pictures is indistinguishable from no blend at all, and the frames in
    the middle of this clip are the whole point of it.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c={colour}:size=320x180:rate=24",
         "-t", "6.0", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target)],
        check=True, capture_output=True, text=True, timeout=180,
    )
    if not target.is_file() or target.stat().st_size == 0:
        raise StaleServer(f"ffmpeg wrote nothing at {target}")


def seed(base_url: str, comfy_root: Path) -> str:
    """Two approved, overlapping Shots and no transition on either — the state a Director is in
    the moment before they choose one."""
    project = post_json(base_url + "/api/projects", {"name": "Transition preview QA"})
    song = artifact_dir() / f"{NAME}-song.wav"
    synthesize_song(song)
    post_multipart(
        f"{base_url}/api/projects/{project['id']}/songs/upload",
        {"title": "Transition preview QA song", "duration": str(SONG_SECONDS)},
        ("file", song),
    )
    shots = []
    for index, (shot_id, start, duration) in enumerate(SHOTS):
        output = f"music-video-producer/{project['id']}/shots/{shot_id}-h3_00001.mp4"
        synthesize_take(comfy_root / "output" / output, "red" if index == 0 else "blue")
        shots.append({
            "id": shot_id, "start": start, "duration": duration,
            "prompt": f"{shot_id}: a corridor, pushing in through the dark.",
            "mode": "text_to_video", "status": "complete", "latest_output": output,
        })
    put_json(f"{base_url}/api/projects/{project['id']}/shots", {"shots": shots})
    for shot_id, _start, _duration in SHOTS:
        post_json(f"{base_url}/api/projects/{project['id']}/shots/{shot_id}/approve")
    return project["id"]


def select_project(driver, wait, project_id: str) -> None:
    wait.until(EC.presence_of_element_located((By.ID, "project-select")))
    wait.until(lambda browser: browser.find_element(
        By.CSS_SELECTOR, f'#project-select option[value="{project_id}"]')).click()
    wait.until(lambda browser: browser.find_element(
        By.ID, "project-select").get_attribute("value") == project_id)


def select_clip(driver, wait, shot_id: str) -> None:
    settle(driver, "#shots-track")
    clip = wait.until(lambda browser: browser.find_element(
        By.CSS_SELECTOR, f'#shots-track .shot-clip[data-shot-id="{shot_id}"]'))
    visible_and_clickable(driver, clip, f"the timeline clip for {shot_id}")
    clip.click()
    wait.until(lambda browser: "selected" in browser.find_element(
        By.CSS_SELECTOR, f'#shots-track .shot-clip[data-shot-id="{shot_id}"]'
    ).get_attribute("class"))
    settle(driver, "#shot-inspector")


def open_effects_tab(driver, wait) -> None:
    tab = wait.until(lambda browser: browser.find_element(By.ID, "shot-tab-effects"))
    visible_and_clickable(driver, tab, "the Effects tab")
    tab.click()
    wait.until(lambda browser: browser.find_element(
        By.ID, "shot-panel-effects").get_attribute("hidden") in (None, "false"))
    settle(driver, "#shot-panel-effects", quiet_ms=400)


def row_state(driver, control: str) -> dict:
    return driver.execute_script(ROW_STATE, control)


def clip_state(driver, control: str) -> dict | None:
    return driver.execute_script(CLIP_STATE, f"#{control}-clip")


def choose(driver, wait, control: str, value: str) -> None:
    """The Director's own gesture: the select, changed, and the panel redrawn from what was
    stored. Never `PUT .../transitions` from this script — the write, the mirror's toast and the
    row's redraw are all part of what is being looked at."""
    select = driver.find_element(By.ID, control)
    visible_and_clickable(driver, select, f"the {control} select")
    driver.execute_script(
        "arguments[0].value = arguments[1];"
        "arguments[0].dispatchEvent(new Event('change', { bubbles: true }));",
        select, value,
    )
    settle(driver, "#shot-panel-effects", quiet_ms=500)
    wait.until(lambda browser: row_state(browser, control)["value"] == value)


def watch(driver, wait, control: str, seconds: float = 90.0) -> dict:
    """Press `Watch blend` and wait until the clip has actually decoded a frame.

    **Decoded, not fetched.** A `<video>` whose `src` 404s, or whose file a browser will not play,
    is indistinguishable in the DOM from one that is playing — same attribute, same element. So
    this waits on `readyState` and `videoWidth`, which are zero until the media pipeline has really
    produced a picture.
    """
    button = wait.until(lambda browser: browser.find_element(By.ID, f"{control}-watch"))
    visible_and_clickable(driver, button, f"the {control} Watch blend control")
    button.click()
    deadline = time.monotonic() + seconds
    seen: dict | None = None
    while time.monotonic() < deadline:
        seen = clip_state(driver, control)
        if seen and seen["readyState"] >= 2 and seen["videoWidth"] > 0:
            return seen
        time.sleep(0.25)
    raise AssertionError(f"no blend ever decoded on {control}: {seen}")


#: The playhead, moved by the real `pointerdown` the timeline handler listens for, at the pixel it
#: maps this second to.
def seek_to(driver, seconds: float) -> None:
    driver.execute_script(
        """
        const canvas = document.querySelector('#timeline-canvas');
        const rect = canvas.getBoundingClientRect();
        const label = document.querySelector('#zoom-label').textContent;
        const pps = 16 * (parseFloat(label) / 100);
        canvas.dispatchEvent(new PointerEvent('pointerdown', {
          clientX: rect.left + 90 + arguments[0] * pps, clientY: rect.top + 10, bubbles: true,
        }));
        """,
        seconds,
    )


#: One pixel out of whichever Monitor layer is on screen, drawn through a canvas.
#:
#: **The middle of the frame, not the corner.** The Monitor letterboxes, so a corner sample reads
#: the black bars whatever the picture is doing -- which would make a fade-to-black assertion pass
#: on a Monitor that never changed at all.
MONITOR_SAMPLE = """
const frame = document.querySelector('#timeline-monitor');
const video = document.querySelector('.monitor-preview.on');
if (!video || !video.videoWidth) {
  return { previewing: frame.classList.contains('previewing'), rgb: [-1, -1, -1] };
}
const canvas = document.createElement('canvas');
canvas.width = video.videoWidth;
canvas.height = video.videoHeight;
const context = canvas.getContext('2d');
context.drawImage(video, 0, 0);
const middle = context.getImageData(
  Math.floor(canvas.width / 2), Math.floor(canvas.height / 2), 1, 1).data;
return {
  previewing: frame.classList.contains('previewing'),
  url: video.dataset.url || '',
  currentTime: video.currentTime,
  rgb: [middle[0], middle[1], middle[2]],
};
"""


def shoot(driver, state: str) -> None:
    driver.save_screenshot(str(artifact_dir() / f"{NAME}-{state}.png"))


def main() -> int:
    port = 8786
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    comfy_root = Path(tempfile.mkdtemp(prefix="mvp-transition-preview-comfy-"))
    os.environ["MVP_COMFY_ROOT"] = str(comfy_root)
    os.environ["MVP_COMFY_URL"] = f"http://127.0.0.1:{dead_port()}"

    result: dict[str, object] = {}
    with ManagedServer(port, label=NAME) as server:
        result["server_identity"] = server.evidence
        project_id = seed(server.base_url, comfy_root)
        driver = edge_driver()
        wait = WebDriverWait(driver, 40)
        try:
            driver.get(server.base_url)
            select_project(driver, wait, project_id)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            wait.until(lambda browser: len(browser.find_elements(
                By.CSS_SELECTOR, "#shots-track .shot-clip")) == 2)

            # === 1. An Overlap with no type: the absence says which absence it is =============
            select_clip(driver, wait, SHOTS[0][0])
            open_effects_tab(driver, wait)
            untyped = row_state(driver, OUT_ROW)
            assert untyped["state"] == "paired", (
                "the fixture's two clips do not overlap, so nothing below is about a blend",
                untyped)
            assert untyped["watchShown"] is False, (
                ("a boundary with no transition offered a control to watch a blend that "
                 "will not happen"), untyped)
            assert "no transition set" in untyped["previewNote"], (
                "the absence sat inert instead of saying which absence it is", untyped)
            # **The ruled number, on screen.** The Overlap is 0.51 s and the blend is twelve
            # frames, so the row states 0.50 — what renders, not what was dragged (2026-08-30).
            assert untyped["length"] == "0.50s · from overlap", untyped
            # And it still fits the 250px rail beside its own label, on one line.
            assert untyped["lengthOverflow"] <= 0, (
                "the length readout has run past the row's right edge", untyped)
            assert untyped["labelGap"] > 4, (
                "the length readout has run into the row's label", untyped)
            assert untyped["lengthLines"] == 1, (
                "the length readout wrapped onto a second line", untyped)
            result["untyped_row"] = untyped
            shoot(driver, "01-overlap-with-no-transition")

            # === 2. A type is chosen and the control appears ==================================
            choose(driver, wait, OUT_ROW, "dissolve")
            offered = row_state(driver, OUT_ROW)
            assert offered["watchShown"] is True, ("no control to watch the blend", offered)
            assert offered["previewNote"] == "", (
                "the row is saying there is no blend and offering to play one", offered)
            assert offered["watchShot"] == SHOTS[0][0], (
                "the control is addressed to a Shot that does not own this boundary (AD-30)",
                offered)
            button = driver.find_element(By.ID, f"{OUT_ROW}-watch")
            result["watch_control"] = visible_and_clickable(
                driver, button, "the Watch blend control")
            result["dissolve_row"] = offered
            shoot(driver, "02-dissolve-offered")

            # === 3. The blend is watched, and it really decoded ==============================
            asked_before = resource_hits(driver, "/boundary-preview")
            dissolve = watch(driver, wait, OUT_ROW)
            served = post_json(
                f"{server.base_url}/api/projects/{project_id}/shots/{SHOTS[0][0]}"
                "/boundary-preview")
            assert dissolve["videoWidth"] == served["width"], (dissolve, served)
            assert dissolve["videoHeight"] == served["height"], (dissolve, served)
            assert dissolve["fingerprint"] == served["fingerprint"], (
                "the element is holding a different clip from the one the route names",
                dissolve, served)
            assert dissolve["displayed"] != "none", (
                "the clip decoded and is not drawn", dissolve)
            assert dissolve["box"]["width"] > 40 and dissolve["box"]["height"] > 20, dissolve
            # **Whole, inside the rail that scrolls.** A clip drawn below the fold of the Effects
            # tab is a picture a Director cannot see, and it looks exactly like a working one from
            # every structural assertion — the band panel's own 225px-below-the-fold measurement
            # is the precedent, and this one was real: full-width, the video's bottom edge landed
            # past the rail until `watchBoundaryBlend` scrolled it into view on `loadeddata`.
            rail = driver.execute_script(
                "return document.querySelector('#shot-inspector')"
                ".getBoundingClientRect().toJSON();")
            below = round(dissolve["box"]["bottom"] - rail["bottom"], 1)
            assert below <= 1, (
                ("the blend is drawn below the fold of the Effects tab, so a Director "
                 "presses Watch blend and sees nothing"),
                {"below_fold_px": below, "clip": dissolve["box"], "rail": rail})
            result["below_fold_px"] = below
            # Playing, not merely loaded. `loop` is set, so the clip runs continuously and a
            # Director watches the blend rather than a still.
            time.sleep(0.8)
            playing = clip_state(driver, OUT_ROW)
            assert playing["paused"] is False or playing["currentTime"] > 0, (
                "the blend loaded and never played", playing)
            assert abs(playing["duration"] - served["window_seconds"]) < 0.2, (
                "the clip on screen is not the window the route said it served", playing, served)
            assert resource_hits(driver, "/boundary-preview") > asked_before
            result["dissolve_clip"] = {**dissolve, "served": served}
            shoot(driver, "03-dissolve-playing")

            # === 4. A different type is a different clip =====================================
            choose(driver, wait, OUT_ROW, "fade_black")
            faded = watch(driver, wait, OUT_ROW)
            assert faded["fingerprint"] != dissolve["fingerprint"], (
                "choosing another transition served the previous blend out of the cache",
                faded, dissolve)
            assert faded["src"] != dissolve["src"], (faded, dissolve)
            result["fade_clip"] = faded
            shoot(driver, "04-fade-through-black-playing")

            # === 5. The incoming Shot's row names the same boundary ==========================
            select_clip(driver, wait, SHOTS[1][0])
            open_effects_tab(driver, wait)
            incoming = row_state(driver, IN_ROW)
            assert incoming["state"] == "paired" and incoming["watchShown"] is True, incoming
            assert incoming["watchShot"] == SHOTS[0][0], (
                "the incoming row is addressed to itself rather than to the outgoing Shot",
                incoming)
            mirrored = watch(driver, wait, IN_ROW)
            assert mirrored["fingerprint"] == faded["fingerprint"], (
                "one seam is showing two different blends depending on which row asked",
                mirrored, faded)
            result["incoming_row"] = {**incoming, "clip": mirrored}
            shoot(driver, "05-same-blend-from-the-incoming-row")

            # === 6. A one-sided transition: the absence, and the Shot's own preview ===========
            # `shot_02` is the last Shot in the song, so its own `Transition out` has no Overlap
            # under it: the treatment lands on its own last frames and then it cuts.
            choose(driver, wait, OUT_ROW, "fade_black")
            one_sided = row_state(driver, OUT_ROW)
            assert one_sided["state"] == "one-sided", one_sided
            assert one_sided["watchShown"] is False, (
                "a boundary with no blend offered to play one", one_sided)
            assert one_sided["previewNote"] == "", (
                ("the row said there is no blend twice — once in its own note and once "
                 "in the preview's"), one_sided)
            # **The gap this story found by looking.** `shot_02` carries no Effect Stack at all,
            # and before story 11.5 `shotPreviewWanted` refused to ask for any preview of such a
            # Shot — so the Monitor showed the untreated take while the export shipped the fade.
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            settle(driver, "#shots-track", quiet_ms=400)
            deadline = time.monotonic() + 60.0
            asked = 0
            while time.monotonic() < deadline:
                asked = resource_hits(driver, f"/shots/{SHOTS[1][0]}/preview")
                if asked:
                    break
                time.sleep(0.25)
            assert asked, (
                "a Shot with a one-sided transition and no effects was never previewed, so the "
                "Monitor is showing the untreated take and saying nothing")
            # **And the treatment is looked at rather than inferred.** The playhead goes to the
            # last half-second of `shot_02`, which is where a `dip_black` has already reached the
            # colour and is holding it to the cut (`effects.ONE_SIDED_FORMS`). The Monitor's own
            # canvas is sampled: a request that was made and a picture that changed are two
            # different claims, and this is the second one.
            seek_to(driver, SONG_SECONDS - 0.1)
            deadline = time.monotonic() + 60.0
            sample = None
            while time.monotonic() < deadline:
                sample = driver.execute_script(MONITOR_SAMPLE)
                if sample and sample["previewing"] and max(sample["rgb"]) < 40:
                    break
                time.sleep(0.25)
            assert sample and sample["previewing"], (
                "no Preview Clip reached the Monitor for the one-sided Shot", sample)
            assert max(sample["rgb"]) < 40, (
                "the Monitor is showing the untreated take at a second the export renders black",
                sample)
            result["one_sided_row"] = {
                **one_sided, "previews_asked": asked, "monitor_sample": sample}
            shoot(driver, "06-one-sided-on-an-ungraded-shot")

            console_gate(driver, NAME, result)
        finally:
            driver.quit()
    result["artifacts"] = sorted(
        path.name for path in artifact_dir().glob(f"{NAME}-*") if path.is_file())
    report(NAME, result)
    print(json.dumps({"ok": True, "screenshots": result["artifacts"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
