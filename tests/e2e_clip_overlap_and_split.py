"""Browser QA for two more of the four recorded interaction defects of 2026-08-21.

The Director's reports:

* a clip that overlaps its later neighbour has that neighbour painted over its **right resize
  handle**, so the handle cannot be grabbed. A consequence of the 2026-08-20 ruling that overlaps
  resolve as layers with the later shot on top;
* **✂ Split** refuses a window it cannot halve and says nothing at all — the same shape as the
  report that started this whole thread, a control that appears to do nothing.

**Only a browser can settle either one.** Whether a 12-pixel handle can be *grabbed* is a question
about paint order, stacking contexts and hit testing, and no reading of `app.js` or of
`styles.css` answers it: `document.elementFromPoint` does. Whether a refusal reaches the screen is
a question about a toast that only exists in a real document. This panel has produced five defect
classes a stub DOM structurally could not see, one of which was a `dblclick` that never fired
because `pointerdown` re-rendered every clip.

The plan below is shaped like the Director's live one (`project_59f14d19ff10`, read and never
touched): eighteen overlapping pairs, from 0.002 s to 5.492 s, plus a deliberate micro-cut. The
big overlaps matter — 5.492 s is 88 px at the default zoom, which buries a 12 px handle whole —
and so do the small ones, because they are what the plan is mostly made of.

What is asserted, in order:

1. **The overlap is real on screen.** For every overlapping pair the later clip's painted box is
   measured to actually intersect the earlier clip's right handle. Without this the hit tests
   below would pass on a plan that has no overlaps and prove nothing.
2. **Every right handle is hit-testable.** `elementFromPoint` at the middle of the handle returns
   that clip's own `.resize-handle.right`, for every overlapped clip — and every left handle too.
3. **The layering ruling is untouched.** At a point inside the overlap that is *not* on a handle,
   the element painted on top is still the later clip's body, exactly as before, and no
   `.shot-clip` has acquired a `z-index`.
4. **The handle is not merely reachable but usable**: a real drag on an overlapped clip's right
   edge moves that clip's window on the manifest and leaves its neighbour's alone.
5. **Split refuses a window under a second, out loud**, naming the window, the half, the floor and
   the number to drag past — and writes nothing at all.
6. **Split still splits.** An ordinary window halves on the manifest, contiguously.
7. **Split with no selection refuses too**, rather than returning in silence.

**No GPU time and no model time is spent.** Nothing here reaches `/prompt`, `MVP_COMFY_URL` points
at a dead port this run chose, and ComfyUI is never contacted, started or stopped.

Run from the repo root -- it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_clip_overlap_and_split.py [--port 8777]

Assumes: nothing listening on the port (it refuses to reuse a bound one), Microsoft Edge and its
WebDriver installed, and `music_video_producer` importable from this checkout's `src/`.
"""

from __future__ import annotations

import itertools
import json
import os
import socket
import sys
import tempfile
import time
import wave
from pathlib import Path

from e2e_support import (
    ManagedServer,
    artifact_dir,
    clear_toasts,
    console_gate,
    edge_driver,
    post_json,
    post_multipart,
    put_json,
    report,
    settle,
    visible_and_clickable,
    wait_for_readiness,
    wait_for_toast,
)
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

NAME = "clip-overlap-and-split"

SONG_SECONDS = 90.0

#: The plan, with the overlaps of the Director's live project in it. Each entry is
#: `(id, start, duration)`; the overlaps are the *measured* ones from `project_59f14d19ff10`
#: -- 0.002, 0.014, 0.042, 0.079, 0.708 and 5.492 seconds -- which is the range that has to work,
#: not a convenient one. `shot_micro` is the deliberate micro-cut the split must refuse: the
#: Director creates those on purpose and the refusal must explain rather than scold.
SHOTS = [
    ("shot_01", 0.0, 5.042),
    ("shot_02", 5.0, 5.333),        # overlaps shot_01 by 0.042
    ("shot_03", 10.331, 3.292),     # overlaps shot_02 by 0.002
    ("shot_04", 13.621, 5.010),     # overlaps shot_03 by 0.002
    ("shot_05", 18.552, 5.811),     # overlaps shot_04 by 0.079
    ("shot_06", 24.349, 1.833),     # overlaps shot_05 by 0.014
    ("shot_07", 25.474, 5.102),     # overlaps shot_06 by 0.708
    ("shot_08", 30.576, 10.375),    # meets shot_07 exactly
    ("shot_09", 35.459, 8.0),       # overlaps shot_08 by 5.492 -- 88 px at the default zoom
    ("shot_micro", 44.0, 0.75),     # the deliberate micro-cut
    ("shot_11", 45.0, 6.0),
]

#: **One shape from the live plan that is deliberately not modelled above, and why.** In
#: `project_59f14d19ff10` the 5.492 s overlap is between a 10.375 s shot and a 5.507 s one that
#: ends 0.015 s *after* it -- so the two clips' right edges are a quarter of a pixel apart at the
#: default zoom and their two 12 px handles sit on top of each other. The later handle wins that
#: hit test, and no z-order can decide it: the edges are, to the eye and to the pointer, the same
#: edge. That is a distinct problem from a handle buried under a neighbour's *body*, which is what
#: the Director reported and what this run proves fixed. Recorded here rather than asserted, so
#: nobody reads this fixture as a claim that coincident edges are separable.
COINCIDENT_EDGES_NOTE = (
    "Two clips whose right edges are 0.015 s apart (SHOT 30/SHOT 31 in the Director's live plan) "
    "have overlapping handles, and the later one takes the hit test. Raising the handles cannot "
    "separate two edges that land on the same pixel; nothing here claims it does."
)

#: Which of the pairs above overlap, as `(earlier, later, seconds)`. Derived rather than written
#: twice: a hand-kept list beside the plan is a list that drifts from it.
def overlapping_pairs() -> list[tuple[str, str, float]]:
    ordered = sorted(SHOTS, key=lambda entry: entry[1])
    pairs = []
    for (earlier, start, duration), (later, next_start, _) in itertools.pairwise(ordered):
        gap = next_start - (start + duration)
        if gap < -1e-9:
            pairs.append((earlier, later, round(-gap, 6)))
    return pairs


#: Every clip's painted box and both handle boxes, plus what the browser says is on top at four
#: probe points. **This is the assertion no source read can make.**
#:
#: `elementFromPoint` is the browser's own hit test: it answers with the element a click at that
#: pixel would be delivered to, after paint order, stacking contexts and `pointer-events` have all
#: been resolved. A handle that is present in the DOM, sized, visible and unreachable answers with
#: something else -- which is exactly the defect.
CLIP_HIT_TESTS = """
const at = (x, y) => {
  const node = document.elementFromPoint(x, y);
  if (!node) return {tag: '', shot: '', handle: ''};
  const clip = node.closest ? node.closest('.shot-clip') : null;
  return {
    tag: node.tagName.toLowerCase(),
    className: typeof node.className === 'string' ? node.className : '',
    shot: clip ? clip.dataset.shotId : '',
    handle: node.classList && node.classList.contains('resize-handle')
      ? (node.classList.contains('right') ? 'right' : 'left') : '',
  };
};
const out = {};
for (const clip of document.querySelectorAll('#shots-track .shot-clip')) {
  const box = clip.getBoundingClientRect();
  const left = clip.querySelector('.resize-handle.left').getBoundingClientRect();
  const right = clip.querySelector('.resize-handle.right').getBoundingClientRect();
  const mid = (r) => ({x: r.left + r.width / 2, y: r.top + r.height / 2});
  out[clip.dataset.shotId] = {
    box: {left: box.left, right: box.right, top: box.top, bottom: box.bottom, width: box.width},
    zIndex: getComputedStyle(clip).zIndex,
    handleZIndex: getComputedStyle(clip.querySelector('.resize-handle.right')).zIndex,
    leftHandle: {box: {left: left.left, right: left.right, width: left.width}, at: at(mid(left).x, mid(left).y)},
    rightHandle: {box: {left: right.left, right: right.right, width: right.width}, at: at(mid(right).x, mid(right).y)},
  };
}
out.__order = [...document.querySelectorAll('#shots-track .shot-clip')].map((c) => c.dataset.shotId);
return out;
"""

#: One pair, brought on screen and then hit-tested. Scrolling first is not cosmetic:
#: `elementFromPoint` is defined over the *viewport*, and at the maximum zoom this plan is 5 760 px
#: wide, so a handle two thousand pixels off to the right answers with whatever happens to be at
#: those coordinates in the inspector. A run that did not scroll would report the shot inspector's
#: buttons as the thing covering a resize handle.
PAIR_HIT_TEST = """
const [earlierId, laterId] = arguments;
const earlier = document.querySelector('#shots-track .shot-clip[data-shot-id="' + earlierId + '"]');
const later = document.querySelector('#shots-track .shot-clip[data-shot-id="' + laterId + '"]');
if (!earlier || !later) return null;
earlier.querySelector('.resize-handle.right')
  .scrollIntoView({block: 'nearest', inline: 'center'});
const handle = earlier.querySelector('.resize-handle.right').getBoundingClientRect();
const neighbour = later.getBoundingClientRect();
const x = handle.left + handle.width / 2;
const y = handle.top + handle.height / 2;
const node = document.elementFromPoint(x, y);
const clip = node && node.closest ? node.closest('.shot-clip') : null;
const order = [...document.querySelectorAll('#shots-track .shot-clip')].map((c) => c.dataset.shotId);
return {
  handle: {left: handle.left, right: handle.right, width: handle.width},
  neighbour: {left: neighbour.left, right: neighbour.right},
  inViewport: handle.left >= 0 && handle.right <= window.innerWidth,
  at: {
    shot: clip ? clip.dataset.shotId : '',
    className: node && typeof node.className === 'string' ? node.className : '',
    handle: node && node.classList && node.classList.contains('resize-handle')
      ? (node.classList.contains('right') ? 'right' : 'left') : '',
  },
  laterIsPaintedAfter: order.indexOf(laterId) > order.indexOf(earlierId),
};
"""

#: What is painted at one arbitrary point, for the layering assertion.
TOPMOST_AT = """
const node = document.elementFromPoint(arguments[0], arguments[1]);
if (!node) return {shot: '', className: ''};
const clip = node.closest ? node.closest('.shot-clip') : null;
return {
  shot: clip ? clip.dataset.shotId : '',
  className: typeof node.className === 'string' ? node.className : '',
  isHandle: Boolean(node.classList && node.classList.contains('resize-handle')),
};
"""


def dead_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def synthesize_song(target: Path, seconds: float = SONG_SECONDS) -> None:
    """Silence at a real sample rate. The timeline needs a Song with a duration and a path."""
    with wave.open(str(target), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(b"\x00\x00" * int(seconds * 8000))


def seed(base_url: str) -> str:
    project = post_json(base_url + "/api/projects", {"name": "Clip overlap browser QA"})
    song = artifact_dir() / "clip-overlap-song.wav"
    synthesize_song(song)
    post_multipart(
        f"{base_url}/api/projects/{project['id']}/songs/upload",
        {"title": "Clip overlap QA song", "duration": str(SONG_SECONDS)},
        ("file", song),
    )
    put_json(f"{base_url}/api/projects/{project['id']}/shots", {"shots": [
        {"id": shot_id, "start": start, "duration": duration, "mode": "text", "status": "draft",
         "prompt": f"{shot_id}: the corridor, pushing in."}
        for shot_id, start, duration in SHOTS
    ]})
    return project["id"]


def manifest(server: ManagedServer, project_id: str) -> dict:
    """The project as it is on disk, read straight out of this run's own data root.

    Retried, because the store renames a temp file over this one and a read landing inside that
    window fails outright on Windows.
    """
    path = server.data_root / "projects" / project_id / "project.json"
    last: Exception | None = None
    for _ in range(40):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            last = error
            time.sleep(0.05)
    raise AssertionError(f"{path} could not be read: {last}")


def windows(server: ManagedServer, project_id: str) -> dict[str, dict[str, float]]:
    return {
        shot["id"]: {"start": round(shot["start"], 6), "duration": round(shot["duration"], 6)}
        for shot in manifest(server, project_id)["shots"]
    }


def select_clip(driver, wait, shot_id: str):
    settle(driver, "#shots-track")
    clip = wait.until(lambda browser: browser.find_element(
        By.CSS_SELECTOR, f'#shots-track .shot-clip[data-shot-id="{shot_id}"]'
    ))
    visible_and_clickable(driver, clip, f"the timeline clip for {shot_id}")
    clip.click()
    wait.until(lambda browser: "selected" in browser.find_element(
        By.CSS_SELECTOR, f'#shots-track .shot-clip[data-shot-id="{shot_id}"]'
    ).get_attribute("class"))
    settle(driver, "#shot-inspector")
    return driver.find_element(
        By.CSS_SELECTOR, f'#shots-track .shot-clip[data-shot-id="{shot_id}"]'
    )


def main() -> None:
    port = 8777
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    result: dict[str, object] = {}
    comfy_root = Path(tempfile.mkdtemp(prefix="mvp-overlap-comfy-"))
    unreachable = f"http://127.0.0.1:{dead_port()}"
    os.environ["MVP_COMFY_ROOT"] = str(comfy_root)
    os.environ["MVP_COMFY_URL"] = unreachable
    os.environ["MVP_LLM_EJECT_BEFORE_RENDER"] = "false"
    os.environ["MVP_LLM_BASE_URL"] = ""

    pairs = overlapping_pairs()
    result["overlaps_in_plan"] = [
        {"earlier": a, "later": b, "seconds": s} for a, b, s in pairs
    ]
    result["coincident_edges_note"] = COINCIDENT_EDGES_NOTE
    assert len(pairs) >= 6, ("the fixture has stopped carrying overlaps", pairs)

    with ManagedServer(port, label=NAME) as server:
        result["server_identity"] = server.evidence
        result["comfy_url"] = unreachable
        project_id = seed(server.base_url)
        # Created before the browser opens, because the project list is built at load: a project
        # minted mid-run is not in `#project-select` until something reloads it, and reloading is
        # not what section 7 is testing.
        empty_id = post_json(server.base_url + "/api/projects", {"name": "Empty plan"})["id"]

        driver = edge_driver()
        wait = WebDriverWait(driver, 25)
        try:
            driver.get(server.base_url)
            wait.until(EC.presence_of_element_located((By.ID, "project-select")))
            wait.until(lambda browser: browser.find_element(
                By.CSS_SELECTOR, f'#project-select option[value="{project_id}"]'
            )).click()
            wait.until(lambda browser: browser.find_element(
                By.ID, "project-select").get_attribute("value") == project_id)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            wait_for_readiness(driver, wait, f"of {len(SHOTS)} shots")
            settle(driver, "#shots-track")

            # === 1-3. The overlap, the handles, and the layering ==============================
            hits = driver.execute_script(CLIP_HIT_TESTS)
            order = hits.pop("__order")
            result["dom_order"] = order

            # The handle outranks the bodies, and the bodies are still unranked -- which is the
            # 2026-08-20 layering ruling. `auto` on the clip means DOM order alone decides which
            # picture is in front, and that is what must not change.
            for shot_id, facts in hits.items():
                assert facts["zIndex"] == "auto", (
                    ("a clip body has acquired a z-index, which changes which picture is painted "
                     "on top and is the ruling this fix was written around"),
                    shot_id, facts["zIndex"],
                )
                assert facts["handleZIndex"] not in ("auto", "0"), (
                    "the resize handle is not raised above the clip bodies", shot_id, facts
                )
            result["z_index"] = {
                "clip": hits[SHOTS[0][0]]["zIndex"],
                "handle": hits[SHOTS[0][0]]["handleZIndex"],
            }

            # Measured at the default zoom **and** at the maximum, because how much of a 12 px
            # handle an overlap actually buries is a function of pixels per second: the Director's
            # smallest overlaps are 0.002 s, which is a seventh of a pixel even at 64 px/s and
            # covers nothing at all. Claiming those as evidence would be dishonest, so each pair is
            # measured and classified, and the run asserts the hit tests where there is genuinely
            # something on top -- and separately that at least one handle is buried *whole*.
            def zoom_to_max() -> None:
                button = driver.find_element(By.ID, "zoom-in")
                for _ in range(12):
                    button.click()
                settle(driver, "#shots-track", quiet_ms=250)

            def measure(label: str) -> list[dict]:
                rows = []
                for earlier, later, seconds in pairs:
                    facts = driver.execute_script(PAIR_HIT_TEST, earlier, later)
                    assert facts, (earlier, later)
                    assert facts["inViewport"], (
                        ("the handle could not be brought on screen, so this hit test would be "
                         "asking about the wrong pixels"),
                        label, earlier, facts,
                    )
                    # The later clip must be the one painted after it, or it is not the thing
                    # covering the handle and the pair proves nothing either way.
                    assert facts["laterIsPaintedAfter"], (
                        ("the fixture's later clip is drawn first, so it is not the one covering "
                         "the handle"),
                        earlier, later,
                    )
                    handle, neighbour = facts["handle"], facts["neighbour"]
                    intersection = min(handle["right"], neighbour["right"]) - max(
                        handle["left"], neighbour["left"]
                    )
                    landed = facts["at"]
                    reached = landed["shot"] == earlier and landed["handle"] == "right"
                    if intersection > 0:
                        assert reached, (
                            (f"at {label}, a click at the middle of {earlier}'s right resize "
                             f"handle lands on {landed['shot'] or 'nothing'} "
                             f"({landed['className']}) instead — the {later} clip overlapping it "
                             f"by {seconds}s is covering the handle"),
                            landed,
                        )
                    rows.append({
                        "earlier": earlier, "later": later, "seconds": seconds,
                        "handle_px_covered": round(max(intersection, 0.0), 2),
                        "hit": reached,
                    })
                return rows

            # Every handle in the plan, both edges, overlapped or not -- at the default zoom,
            # where the whole plan is inside the viewport and `elementFromPoint` can be asked
            # about all of it in one pass.
            for shot_id, facts in hits.items():
                for edge in ("leftHandle", "rightHandle"):
                    hit = facts[edge]["at"]
                    assert hit["shot"] == shot_id and hit["handle"], (
                        f"{shot_id}'s {edge} is not hit-testable at the default zoom", hit
                    )

            at_default = measure("the default zoom")
            zoom_to_max()
            zoomed_facts = driver.execute_script(CLIP_HIT_TESTS)
            zoomed_facts.pop("__order")
            at_max = measure("maximum zoom")
            result["covered_handles"] = {"default_zoom": at_default, "max_zoom": at_max}

            # The run is not vacuous: at least one handle is buried **whole** by its neighbour,
            # and several are partly covered. Without this every hit test above could be passing
            # because nothing was ever on top of anything.
            buried = [row for row in at_max if row["handle_px_covered"] >= 12]
            partly = [row for row in at_max if row["handle_px_covered"] > 0]
            assert buried, (
                ("no overlap in this plan covers a whole resize handle, so nothing here proves "
                 "the handle is reachable *through* a neighbour"),
                at_max,
            )
            assert len(partly) >= 3, ("too few overlaps are visible to prove anything", at_max)
            result["buried_handles"] = buried

            # (3) The layering ruling, measured where it is visible: a point inside the big
            # overlap that is *not* on either handle still belongs to the later clip. Taken at the
            # maximum zoom, where that overlap is 351 px wide and the probe cannot land on a handle
            # by accident.
            hits = zoomed_facts
            big = max(pairs, key=lambda entry: entry[2])
            earlier, later, seconds = big
            handle = hits[earlier]["rightHandle"]["box"]
            box = hits[later]["box"]
            probe_x = (box["left"] + handle["left"]) / 2
            probe_y = (hits[later]["box"]["top"] + hits[later]["box"]["bottom"]) / 2
            painted = driver.execute_script(TOPMOST_AT, probe_x, probe_y)
            assert painted["shot"] == later, (
                ("the later clip is no longer the one painted on top inside an overlap, which is "
                 "the 2026-08-20 ruling this fix must not break"),
                big, painted,
            )
            assert painted["isHandle"] is False, painted
            result["layering_probe"] = {"pair": [earlier, later, seconds], "painted": painted}

            # === 4. The handle is usable, not merely reachable ================================
            #
            # **The clip is not selected first**, deliberately: `bindClip`'s `pointerdown` selects
            # the shot the press landed on, so grabbing the handle *is* the selection. It also has
            # to be that way here -- `shot_08`'s *centre* is inside the 5.492 s overlap and is
            # therefore covered by `shot_09`, exactly as the layering ruling says it should be, so
            # a hit test on the whole clip would fail for a reason that is not a defect.
            before = windows(server, project_id)
            clip = driver.find_element(
                By.CSS_SELECTOR, f'#shots-track .shot-clip[data-shot-id="{earlier}"]'
            )
            grip = clip.find_element(By.CSS_SELECTOR, ".resize-handle.right")
            visible_and_clickable(
                driver, grip, f"{earlier}'s right resize handle under the {later} overlap"
            )
            ActionChains(driver).click_and_hold(grip).move_by_offset(-40, 0).release().perform()
            deadline = time.monotonic() + 12
            after = before
            while time.monotonic() < deadline and after[earlier] == before[earlier]:
                time.sleep(0.15)
                after = windows(server, project_id)
            assert after[earlier]["duration"] < before[earlier]["duration"], (
                (f"dragging {earlier}'s right handle left changed nothing on the manifest, so the "
                 "handle is reachable and still not usable"),
                before[earlier], after[earlier],
            )
            assert after[later] == before[later], (
                "the neighbour's window moved with a freehand right-edge drag", before, after
            )
            result["resize_drag"] = {
                "shot": earlier, "before": before[earlier], "after": after[earlier]
            }
            clear_toasts(driver)

            # === 5. Split refuses a micro-cut, out loud ======================================
            select_clip(driver, wait, "shot_micro")
            before_split = windows(server, project_id)
            split = driver.find_element(By.ID, "split-shot")
            visible_and_clickable(driver, split, "the split control")
            split.click()
            said = wait_for_toast(driver, wait, "0.75s")
            for number in ("0.75s", "0.375s", "0.5s", "past 1s"):
                assert number in said, (
                    f"the split's refusal does not name {number}", said
                )
            assert "shot_micro" in said, said
            settle(driver, "#shots-track", quiet_ms=500)
            after_split = windows(server, project_id)
            assert after_split == before_split, (
                "the refused split wrote to the plan anyway", before_split, after_split
            )
            result["split_refusal"] = {"toast": said, "shots": len(after_split)}
            clear_toasts(driver)

            # === 6. Split still splits ======================================================
            select_clip(driver, wait, "shot_11")
            was = windows(server, project_id)["shot_11"]
            driver.find_element(By.ID, "split-shot").click()
            deadline = time.monotonic() + 12
            plan = windows(server, project_id)
            while time.monotonic() < deadline and len(plan) == len(after_split):
                time.sleep(0.15)
                plan = windows(server, project_id)
            assert len(plan) == len(after_split) + 1, (
                "the split added no shot", len(after_split), len(plan)
            )
            halved = plan["shot_11"]
            assert abs(halved["duration"] - was["duration"] / 2) < 1e-9, (was, halved)
            second = next(
                window for shot_id, window in plan.items()
                if shot_id not in after_split
            )
            assert abs(second["start"] - (halved["start"] + halved["duration"])) < 1e-9, (
                "the two halves of the split do not meet", halved, second
            )
            assert abs(second["duration"] - halved["duration"]) < 1e-9, (halved, second)
            result["split_applied"] = {"was": was, "first": halved, "second": second}
            clear_toasts(driver)

            # === 7. Split with nothing selected refuses too ==================================
            #
            # Reached through a project with no shots in it rather than by clicking empty canvas,
            # which this workspace does not treat as a deselect, and `state` is not reachable from
            # the page. The refusal is what is under test, not how the selection was emptied.
            driver.find_element(
                By.CSS_SELECTOR, f'#project-select option[value="{empty_id}"]'
            ).click()
            wait.until(lambda browser: browser.find_element(
                By.ID, "project-select").get_attribute("value") == empty_id)
            settle(driver, "#shots-track", quiet_ms=400)
            clear_toasts(driver)
            driver.find_element(By.ID, "split-shot").click()
            empty_said = wait_for_toast(driver, wait, "No shot is selected")
            assert "nothing to split" in empty_said, empty_said
            result["split_no_selection"] = empty_said

            console_gate(driver, NAME, result, expected=[unreachable.removeprefix("http://")])
            report(NAME, result)
        except TimeoutException as error:  # pragma: no cover - a real failure, not a skip
            raise AssertionError(f"the browser stopped waiting: {error}") from error
        finally:
            driver.quit()


if __name__ == "__main__":
    main()
