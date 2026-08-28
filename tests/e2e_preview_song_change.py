"""Browser QA for the one question a Preview Clip's name answers: *what determines this picture?*

The defect this gate exists against, found by the Epic 10 audit on 2026-08-27 and fixed the same
day. The server passed `song_fingerprint` into `preview_fingerprint` **unconditionally** while the
client's `previewInputKey` carried no song at all. Two answers to one question -- the fifth time
this repository has paid for that shape, after `shotLabel`, the H3 partition, the preview
fingerprint's own fourth slot and the section membership rule -- and the two failures it produced
pull in opposite directions:

* **The server renamed clips nobody re-asked for.** A bound Shot's picture *is* a function of the
  measurement, so replacing the song made the route name a different clip -- and the client, whose
  key had not moved, never sent the request. The Monitor went on playing the clip driven by a song
  the project no longer had, unflagged and labelled current, at the same moment `POST /assemble`
  refused that same Shot by name. No request was made, so there was nothing to refuse and nothing
  to say.
* **And it renamed clips that had not changed.** The gate was on the envelope *read*, never on the
  slot, so analysing a song orphaned the cached preview of **every** Shot carrying any effect at
  all -- bound or not -- for a reason that cannot reach an unbound Shot's picture, since
  `build_effect_stages` ignores the envelope entirely for a stack with no binding.

Both sides now gate on one rule: the song is part of this picture's identity exactly when the
picture asks the song a question. `app.stack_is_driven` and `api.stackIsDriven` are the two
spellings of it and `test_the_client_and_the_server_answer_driven_identically` compares their
answers over one table.

**Why this needs a browser at all.** Every part of the rule is covered offline -- the route's
fingerprint in `tests/test_shot_preview.py`, the key and the predicate in
`tests/test_frontend_contract.py`. What a stub DOM structurally cannot see is the thing that was
actually wrong: a *request that is never sent*. The old client was perfectly self-consistent, and
so was the old server; the defect lived in the space between them, and it showed up as a picture
on a screen that nobody had asked a question about. So this script drives the real gesture -- a
Director importing a different track over an open project -- and measures what the page then does
with its own resource timings.

**It never reloads the page**, and that is load-bearing. A refresh empties `monitorPreviews` and
would repair the symptom without touching the cause, so the song is replaced through the Song
workspace's own file input while the page stays up, exactly as a Director does it.

**A replaced song is measured on import**, which is what makes the ordinary case the *silent*
one. `upload_song` analyses the track it just wrote and only logs a failure, so a Director who
swaps a master gets a new, current measurement -- and the binding resolves against it immediately.
Nothing refuses. Before the fix the Monitor simply went on showing the clip driven by the previous
track, labelled current, with no toast and nothing on screen to hint at it, while the export
shipped the new drive: the preview and the export disagreeing about the picture, permanently and
without a symptom, which is the one thing Story 9.2 exists to prevent. The refusal path is the
*other* case -- a measurement that is absent rather than replaced -- and both are driven here.

What is driven, in order:

1. **A bound Shot's preview is on screen** -- the clip rendered against the current measurement,
   requested once, `showing-take` with no stale flag.
2. **A graded but unbound Shot's preview is on screen** too, and its request is counted, because
   the half of the rule about *not* invalidating needs a Shot whose picture the song cannot reach.
3. **The song is replaced through the real control**, same duration so no Shot window moves and
   the song is the only thing that changed. The import measures it, so the binding stays live.
4. **The bound Shot is re-asked for** -- one new request -- and comes back a **different clip**,
   because it is now driven by a different track. This is the silent half: nothing refuses, nothing
   is said, and before the fix nothing was asked either.
5. **The unbound Shot is not re-asked for**, and the clip on screen is the same file it was: a
   cached preview is not thrown away for a reason that cannot change its picture.
6. **The measurement is then taken away entirely** -- the record emptied and the sidecar deleted,
   which is the state every pre-Epic-8 project is in -- and the bound Shot is re-asked once more.
   That one *is* refused, in the export's own words, in a toast, with the Monitor flagging the
   picture on screen rather than passing it off as the look as it stands.
7. **Re-analysing brings it back live**, which is Story 10.4's promise observed at the Monitor
   rather than in the manifest -- and it costs no render at all, because the key returns to the
   value the clip in hand was made under.

No GPU is spent and nothing reaches ComfyUI, which is pointed at a dead port. Every preview is a
local ffmpeg transcode, so **ffmpeg must be on PATH**.

Run from the repo root -- it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_preview_song_change.py [--port 8784]

Assumes: nothing listening on the port, Microsoft Edge and its WebDriver installed, and
`music_video_producer` importable from this checkout's `src/`.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import socket
import struct
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
    get_json,
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

NAME = "preview-song-change"

#: The bound Shot and the graded-but-unbound one. Both carry a stack and an approved take, which
#: is what makes them comparable: the only difference between them is the binding, so the only
#: thing that can explain a difference in what the page asks for is the binding.
BOUND = "shot_bound"
PLAIN = "shot_plain"

#: Both songs are this long, deliberately. A replacement of a different length moves nothing about
#: a Shot's window -- windows are absolute seconds -- but it *does* move what the plan runs past,
#: and a harness whose two states differ in two ways proves nothing about either. Same length, same
#: windows, different bytes: the song is the only thing that changed.
SONG_SECONDS = 12.0


def dead_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def synthesize_song(target: Path, beats_per_minute: float) -> None:
    """A click track at a named tempo: a decaying burst per beat, silence between.

    Two songs that differ in tempo differ in every band of the measurement, which is what makes
    "the drive is compiled from a different track" a real difference rather than a nominal one.
    Written with `wave` and `struct` because a browser QA script is stdlib-only.
    """
    rate = 8000
    total = int(SONG_SECONDS * rate)
    samples = [0] * total
    period = 60.0 / beats_per_minute
    burst = [
        int(24000 * math.sin(2 * math.pi * 1000 * n / rate) * math.exp(-n / rate * 120))
        for n in range(int(0.02 * rate))
    ]
    beat = 0
    while True:
        start = int(beat * period * rate)
        if start + len(burst) >= total:
            break
        for offset, value in enumerate(burst):
            samples[start + offset] = max(-32767, min(32767, samples[start + offset] + value))
        beat += 1
    with wave.open(str(target), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(struct.pack(f"<{total}h", *samples))


def synthesize_take(target: Path, seconds: float = 6.0) -> None:
    """A real, moving, saturated take -- long enough to cover a 4 s window with the grid's slack."""
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=24:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target)],
        check=True, capture_output=True, text=True, timeout=180,
    )
    if not target.is_file() or target.stat().st_size == 0:
        raise StaleServer(f"ffmpeg wrote nothing at {target}")


def seed(base_url: str, comfy_root: Path) -> tuple[str, str]:
    """A measured project with two graded Shots, one of them bound.

    Both Shots carry a stack whose card composes at a non-identity value, because a preview is
    only asked for at all where there is a stack -- and both are approved, because the preview
    renders the *approved* take for the reason the export does.
    """
    project_id = post_json(f"{base_url}/api/projects", {"name": "Preview song change QA"})["id"]
    # A second, empty project. Nothing is driven in it: it exists so `reload_project` below has
    # somewhere to go, because selecting the project already selected fires no `change` and the
    # page would go on believing whatever it last read.
    elsewhere = post_json(f"{base_url}/api/projects", {"name": "Somewhere else"})["id"]
    song = artifact_dir() / f"{NAME}-song-a.wav"
    synthesize_song(song, 100.0)
    post_multipart(
        f"{base_url}/api/projects/{project_id}/songs/upload",
        {"title": "The first track", "duration": str(SONG_SECONDS)},
        ("file", song),
    )
    shots = []
    for index, shot_id in enumerate((BOUND, PLAIN)):
        output = f"music-video-producer/{project_id}/shots/{shot_id}-h3_00001.mp4"
        synthesize_take(comfy_root / "output" / output)
        shots.append({
            "id": shot_id, "start": index * 4.0, "duration": 4.0,
            "prompt": "The rooftop, wide." if index == 0 else "The stairwell, handheld.",
            "mode": "text_to_video", "status": "complete", "latest_output": output,
        })
    put_json(f"{base_url}/api/projects/{project_id}/shots", {"shots": shots})
    for shot_id in (BOUND, PLAIN):
        post_json(f"{base_url}/api/projects/{project_id}/shots/{shot_id}/approve")
    # Exposure at a non-identity 0.2 on both, so the two chains are the *same* chain and the only
    # difference between the Shots is the binding -- which is what the request counts are about.
    for shot_id in (BOUND, PLAIN):
        put_json(
            f"{base_url}/api/projects/{project_id}/shots/{shot_id}/effects",
            {"effects": [{"effect": "exposure", "enabled": True, "parameters": {"amount": 0.2}}]},
        )
    measured = post_json(f"{base_url}/api/projects/{project_id}/song/analyze")
    if not measured.get("song", {}).get("analysis", {}).get("song_fingerprint"):
        raise StaleServer("the seeded song did not measure, so nothing here would mean anything")
    # The binding, written the only way one can be: through its own route.
    put_json(
        f"{base_url}/api/projects/{project_id}/shots/{BOUND}/effects/0/bindings",
        {"effect": "exposure", "bindings": [{
            "parameter": "amount", "drive": "punch", "depth": 0.6,
            "band_centre": 0.1, "band_width": 0.3, "band_softness": 0.35, "floor": 0.0,
        }]},
    )
    return project_id, elsewhere


def select_project(driver, wait, project_id: str) -> None:
    wait.until(EC.presence_of_element_located((By.ID, "project-select")))
    wait.until(
        lambda browser: browser.find_element(
            By.CSS_SELECTOR, f'#project-select option[value="{project_id}"]'
        )
    ).click()
    wait.until(
        lambda browser: browser.find_element(By.ID, "project-select").get_attribute("value")
        == project_id
    )


def reload_project(driver, wait, project_id: str, elsewhere: str) -> None:
    """Make the page read this project again, without reloading the page.

    **Never `driver.refresh()`**, which is the whole point of this script: a refresh empties the
    Monitor's held clips, so every assertion below it would pass over the defect rather than
    through it. Selecting the project that is already selected fires no `change` event, so the
    round trip goes via a second, empty project -- which is a thing a Director really does, and
    which leaves `monitorPreviews` exactly as it was.
    """
    select_project(driver, wait, elsewhere)
    settle(driver, "#shots-track", quiet_ms=300)
    select_project(driver, wait, project_id)
    settle(driver, "#shots-track", quiet_ms=400)


def seek_to(driver, seconds: float) -> None:
    """The real pointerdown the timeline handler listens for, at the pixel it maps this second to."""
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


MONITOR = """
const frame = document.querySelector('#timeline-monitor');
const on = document.querySelector('.monitor-preview.on');
return {
  previewing: frame.classList.contains('previewing'),
  showing: frame.classList.contains('showing-take'),
  stale: document.querySelector('#monitor-stale').textContent,
  note: document.querySelector('#monitor-note').textContent,
  onUrl: on ? (on.dataset.url || '') : '',
  onShot: on ? (on.dataset.shot || '') : '',
};
"""


def monitor(driver) -> dict:
    return driver.execute_script(MONITOR)


def previews_asked(driver, shot_id: str) -> int:
    return resource_hits(driver, f"/shots/{shot_id}/preview")


def await_preview(driver, shot_id: str, seconds: float = 60.0) -> dict:
    """The Monitor once a Preview Clip for this Shot is decoded and on screen."""
    deadline = time.monotonic() + seconds
    seen: dict = {}
    while time.monotonic() < deadline:
        seen = monitor(driver)
        if seen["previewing"] and seen["onUrl"] and seen["onShot"] == shot_id:
            return seen
        time.sleep(0.2)
    raise AssertionError(f"no Preview Clip reached the Monitor for {shot_id}: {seen}")


def await_count(driver, shot_id: str, wanted: int, seconds: float = 60.0) -> int:
    deadline = time.monotonic() + seconds
    count = previews_asked(driver, shot_id)
    while time.monotonic() < deadline and count < wanted:
        time.sleep(0.2)
        count = previews_asked(driver, shot_id)
    return count


def dismiss_toasts(driver, timeout: float = 10.0) -> list[str]:
    """Click every toast away, and report what was dismissed.

    **`clear_toasts` cannot do this and its docstring used to say otherwise.** `app.js`'s `toast()`
    gives an `error` toast no timer -- *"errors stay until dismissed"* -- so waiting for a refusal
    to expire waits for ever, and a script that then asserts "no refusal is on screen" is asserting
    about a page still wearing the last one. Every toast carries `title="Click to dismiss"` and a
    click handler, so clicking is the shipped way, and it is what a Director does.
    """
    dismissed: list[str] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        items = driver.find_elements(By.CSS_SELECTOR, "#toast-region .toast")
        if not items:
            return dismissed
        for item in items:
            # A toast that went stale between the query and the click is the outcome this loop
            # wants, so it is suppressed rather than retried.
            with contextlib.suppress(Exception):
                dismissed.append(item.text)
                driver.execute_script("arguments[0].click();", item)
        time.sleep(0.2)
    raise AssertionError(f"toasts would not dismiss: {dismissed}")


def shoot(driver, state: str) -> None:
    driver.save_screenshot(str(artifact_dir() / f"{NAME}-{state}.png"))


def replace_the_song(driver, wait, path: Path, base_url: str, project_id: str,
                     title_text: str = "The second track") -> str:
    """The Director's own gesture: the Song workspace's file input, the button, the confirmation.

    Deliberately not `POST /songs/upload` from this script. The whole defect lives in what the
    *page* does after the song changes, and a replacement the page never saw happen would be a
    different scenario -- as would a `driver.refresh()`, which empties the Monitor's held clips and
    repairs the symptom without touching the cause.
    """
    driver.find_element(By.CSS_SELECTOR, '[data-panel="song"]').click()
    settle(driver, "#song-panel" if driver.find_elements(By.ID, "song-panel") else "body",
           quiet_ms=300)
    driver.find_element(By.ID, "song-file").send_keys(str(path))
    title = driver.find_element(By.ID, "import-title")
    driver.execute_script("arguments[0].value = '';", title)
    title.send_keys(title_text)
    button = driver.find_element(By.ID, "import-song")
    visible_and_clickable(driver, button, "the Import song button")
    button.click()
    question = WebDriverWait(driver, 15).until(EC.alert_is_present())
    asked = question.text
    question.accept()
    # The import is a multipart POST the page makes for itself, and the decode that supplies its
    # duration is async, so "the dialog was accepted" is not "the song was replaced". Waited on the
    # manifest rather than on a spinner: the manifest is what every later assertion reads.
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if get_json(f"{base_url}/api/projects/{project_id}")["song"]["title"] == title_text:
            return asked
        time.sleep(0.3)
    raise AssertionError(f"the song was never replaced; the dialog said {asked!r}")


def main() -> int:
    port = 8784
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    comfy_root = Path(tempfile.mkdtemp(prefix="mvp-song-change-comfy-"))
    os.environ["MVP_COMFY_ROOT"] = str(comfy_root)
    os.environ["MVP_COMFY_URL"] = f"http://127.0.0.1:{dead_port()}"

    result: dict[str, object] = {}
    with ManagedServer(port, label=NAME) as server:
        result["server_identity"] = server.evidence
        project_id, elsewhere = seed(server.base_url, comfy_root)
        driver = edge_driver()
        wait = WebDriverWait(driver, 30)
        try:
            driver.get(server.base_url)
            select_project(driver, wait, project_id)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            wait.until(
                lambda browser: len(
                    browser.find_elements(By.CSS_SELECTOR, "#shots-track .shot-clip")
                ) == 2
            )

            # === 1. The bound Shot's driven preview is on screen =============================
            seek_to(driver, 1.0)
            bound_before = await_preview(driver, BOUND)
            bound_asks = await_count(driver, BOUND, 1)
            assert bound_asks == 1, ("a bound Shot's preview was asked for more than once",
                                     bound_asks)
            assert bound_before["stale"] == "", (
                "the driven clip was flagged stale against the song it was rendered from",
                bound_before)
            first_fingerprint = get_json(
                f"{server.base_url}/api/projects/{project_id}"
            )["song"]["analysis"]["song_fingerprint"]
            result["bound_before"] = {**bound_before, "asks": bound_asks}
            shoot(driver, "01-bound-driven")

            # === 2. The graded-but-unbound Shot's preview, for the other half of the rule =====
            seek_to(driver, 5.0)
            plain_before = await_preview(driver, PLAIN)
            plain_asks = await_count(driver, PLAIN, 1)
            assert plain_asks == 1, plain_asks
            assert plain_before["onUrl"] != bound_before["onUrl"], (
                "both Shots are showing the same clip, so this measures nothing")
            result["plain_before"] = {**plain_before, "asks": plain_asks}
            shoot(driver, "02-unbound-graded")

            # === 3. The song is replaced, in the page, by the Director's own gesture ==========
            replacement = artifact_dir() / f"{NAME}-song-b.wav"
            synthesize_song(replacement, 137.0)
            asked = replace_the_song(driver, wait, replacement, server.base_url, project_id)
            assert "song" in asked.lower(), ("the replacement was not confirmed by name", asked)
            stored = get_json(f"{server.base_url}/api/projects/{project_id}")
            assert stored["song"]["title"] == "The second track", stored["song"]
            # The windows did not move, so the song is the only thing that changed. Asserted rather
            # than assumed: a replacement that moved a window would move the key on its own and
            # every count below would be meaningless.
            assert [(shot["start"], shot["duration"]) for shot in stored["shots"]] == [
                (0.0, 4.0), (4.0, 4.0)], stored["shots"]
            # **The import measured the replacement**, which is what makes this the silent case
            # rather than the loud one. Asserted rather than assumed: if a future import stopped
            # analysing, section 4 below would be testing the refusal path while claiming not to.
            targets = get_json(f"{server.base_url}/api/projects/{project_id}/timeline/snap-targets")
            assert targets["analysed"] is True, (
                ("the replacement left no measurement, so this is the refusal path and not the "
                 "silent one this section is about"),
                targets,
            )
            measured = get_json(f"{server.base_url}/api/projects/{project_id}")["song"]["analysis"]
            assert measured["song_fingerprint"] != first_fingerprint, measured
            result["replaced"] = {
                "question": asked, "bpm": measured["bpm"], "analysed": targets["analysed"],
            }

            # === 4. The bound Shot is re-asked for, and comes back a different picture =========
            #
            # The silent half, and the reason this gate is worth its runtime. Nothing refuses here
            # and nothing is said: the binding resolves perfectly well against the new measurement,
            # and the only thing that goes wrong when the key is missing an element is that the
            # question is never asked. So the assertion is about a *request*, counted out of the
            # browser's own resource timings, and about the file that came back.
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            settle(driver, "#shots-track", quiet_ms=400)
            seek_to(driver, 1.0)
            after_asks = await_count(driver, BOUND, bound_asks + 1)
            assert after_asks == bound_asks + 1, (
                ("the Monitor never re-asked for a bound Shot's preview after the song was "
                 "replaced, so it is still showing a picture driven by the previous track -- "
                 "silently, with the export shipping the new one. This is the defect this gate "
                 "exists against and it has no symptom of its own"),
                {"before": bound_asks, "after": after_asks},
            )
            bound_after = await_preview(driver, BOUND)
            assert bound_after["onUrl"] != bound_before["onUrl"], (
                "the bound Shot came back with the clip driven by the previous song",
                bound_after, bound_before)
            assert bound_after["stale"] == "", (
                "the new drive is on screen and is being called out of date", bound_after)
            result["bound_after"] = {**bound_after, "asks": after_asks}
            shoot(driver, "03-bound-redriven")

            # === 5. The unbound Shot's cached clip is untouched ===============================
            seek_to(driver, 5.0)
            settle(driver, "#timeline-monitor", quiet_ms=600)
            time.sleep(1.5)
            plain_after = monitor(driver)
            plain_after_asks = previews_asked(driver, PLAIN)
            assert plain_after_asks == plain_asks, (
                ("an unbound Shot's preview was re-asked for because the song changed, which "
                 "throws away a cached clip for a reason that cannot reach its picture"),
                {"before": plain_asks, "after": plain_after_asks},
            )
            assert plain_after["onUrl"] == plain_before["onUrl"], (
                "the unbound Shot's clip was replaced", plain_after, plain_before)
            assert plain_after["stale"] == "", (
                "an unbound Shot's clip was flagged stale by a song change", plain_after)
            result["plain_after"] = {**plain_after, "asks": plain_after_asks}
            shoot(driver, "04-unbound-untouched")

            # === 6. The measurement taken away entirely: the loud half =======================
            #
            # Not a replacement -- an *absence*, which is the state every project written before
            # Epic 8 is in and the state a failed import analysis leaves behind (`upload_song`
            # logs that failure and does not fail the import, so nothing on screen says so). The
            # record is emptied and the sidecar deleted, the way `e2e_song_analysis` reaches the
            # same state, because that is the real shape of it rather than a simulated one.
            manifest = server.data_root / "projects" / project_id / "project.json"
            stored = json.loads(manifest.read_text(encoding="utf-8"))
            sidecar = server.data_root / "projects" / project_id / stored["song"]["analysis"]["path"]
            stored["song"]["analysis"] = {}
            manifest.write_text(json.dumps(stored, indent=2), encoding="utf-8")
            if sidecar.is_file():
                sidecar.unlink()
            reload_project(driver, wait, project_id, elsewhere)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            settle(driver, "#shots-track", quiet_ms=400)
            seek_to(driver, 1.0)
            refused_asks = await_count(driver, BOUND, after_asks + 1)
            assert refused_asks > after_asks, (
                "the Monitor did not re-ask for the bound Shot once its measurement had gone",
                refused_asks)
            refusal = WebDriverWait(driver, 25).until(
                lambda browser: next(
                    (item.text for item in browser.find_elements(
                        By.CSS_SELECTOR, "#toast-region .toast")
                     if "Parameter Binding" in item.text), None)
            )
            assert "no current song analysis to drive it" in refusal, refusal
            refused_state = monitor(driver)
            assert refused_state["stale"], (
                "the Monitor is passing off a clip it can no longer make as the look as it stands",
                refused_state)
            result["refused"] = {**refused_state, "asks": refused_asks, "refusal": refusal}
            shoot(driver, "05-bound-refused")
            # An error toast has no timer, so this is a click and not a wait.
            result["dismissed_refusal"] = dismiss_toasts(driver)

            # === 7. Re-analysing brings the bound Shot back, live ============================
            #
            # Driven through the Snap-to row's own `[Analyze song]` rather than through the route,
            # and that is not fussiness. This script emptied the measurement by writing the
            # manifest under a live page, so the page is holding a copy that says there is none;
            # re-measuring behind its back and then making it re-read leaves a window in which the
            # page can save its stale copy over the fresh analysis -- which is exactly what
            # happened when this section was written the short way. The affordance Epic 8 shipped
            # for this is on screen, it updates `state.project` from its own reply, and it is what
            # a Director presses.
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            settle(driver, "#shots-track", quiet_ms=400)
            if not driver.find_element(By.ID, "snap-targets").get_attribute("open"):
                driver.find_element(By.ID, "snap-targets-summary").click()
            settle(driver, "#snap-target-kinds", quiet_ms=250)
            analyze = WebDriverWait(driver, 20).until(
                lambda browser: browser.find_element(By.ID, "snap-action-beat"))
            visible_and_clickable(driver, analyze, "the Snap-to row's Analyze song")
            analyze.click()
            deadline = time.monotonic() + 90.0
            while time.monotonic() < deadline:
                if get_json(
                    f"{server.base_url}/api/projects/{project_id}/timeline/snap-targets"
                )["analysed"]:
                    break
                time.sleep(0.3)
            else:
                raise AssertionError("the Snap-to row's Analyze song never re-measured the song")
            settle(driver, "#snap-target-kinds", quiet_ms=400)
            seek_to(driver, 1.0)
            revived = await_preview(driver, BOUND)
            revived_asks = previews_asked(driver, BOUND)
            # **And it costs nothing**, which is the gate's own rule paying off rather than a
            # detail. Re-analysing the same track reproduces the same measurement, so the key
            # returns to the value section 4's clip was rendered under -- and the Monitor is still
            # holding that clip. Nothing is asked for and the right picture is already on screen.
            # A key that carried the song *ungated*, or one that carried a re-analysis timestamp,
            # would re-render here for a picture that did not change.
            assert revived_asks == refused_asks, (
                "re-analysing re-rendered a Preview Clip the Monitor was already holding",
                {"before": refused_asks, "after": revived_asks})
            assert revived["onUrl"] == bound_after["onUrl"], (
                "the binding did not come back live against its own measurement", revived,
                bound_after)
            assert revived["onUrl"] != bound_before["onUrl"], (
                "the bound Shot is showing the clip driven by the *first* song", revived)
            assert revived["stale"] == "", revived
            # **Dismissed here rather than waited out, and after the page has settled.** Two
            # things had to be got right for this window to mean anything. An `error` toast never
            # expires (`app.js`'s `toast()`: *errors stay until dismissed*), so section 6's refusal
            # is still on screen however long this waits -- it has to be clicked. And the drain
            # goes *after* the clip is on screen and the count is read, because
            # `revived_asks == refused_asks` above already proves no further request can be made:
            # nothing new can refuse, so what follows observes a settled page rather than racing
            # one.
            dismiss_toasts(driver)
            # Watched rather than sampled once: a refusal would arrive with the reply to a
            # request, which is not instantaneous, so a single read the moment after a seek could
            # miss one and call the absence a pass.
            settled_until = time.monotonic() + 4.0
            lingering: list[str] = []
            while time.monotonic() < settled_until:
                lingering = [
                    item.text for item in driver.find_elements(
                        By.CSS_SELECTOR, "#toast-region .toast")
                    if "Parameter Binding" in item.text
                ]
                if lingering:
                    break
                time.sleep(0.2)
            assert not lingering, (
                "the binding is live again and something still refused it", lingering)
            result["revived"] = {**revived, "asks": revived_asks}
            shoot(driver, "06-bound-live-again")

            # The refusal in section 6 is this script's own doing and is declared rather than
            # filtered away: a gate that quietly drops what it does not want to see is not a gate.
            console_gate(driver, NAME, result, expected=[f"/shots/{BOUND}/preview"])
        finally:
            driver.quit()
    result["artifacts"] = sorted(
        path.name for path in artifact_dir().glob(f"{NAME}-*") if path.is_file()
    )
    report(NAME, result)
    print(json.dumps({"ok": True, "screenshots": result["artifacts"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
