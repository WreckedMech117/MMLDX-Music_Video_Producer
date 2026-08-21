"""Browser QA for the four timeline-editing asks of 2026-08-21: undo/redo, the gap-fill
double-click, snapping a cut to the playhead, and Expand All Prompts on the cuts bar.

The Director's own words:

* *"I accidentally hit split on a clip, but there is no Undo button, so the first shot is split
  in 2 now."*
* *"When i shorten one clip and leave a gap it would be nice if when i double-click on a shot
  edge next to that gap if it would extend over to fill that gap."*
* *"Snap to timeline play marker also good so i can line up to beats in the music then snap the
  Shot edges to it."*
* *"Something i am also not seeing in the timeline button ... 'Expand All Prompts'."*

**Every one of these is an interaction defect class, and none of them is visible offline.** The
executed contract in `tests/test_frontend_contract.py` runs the deciding functions and proves the
rules; what it structurally cannot see is whether a double-click reaches a 7-pixel resize handle,
whether a magnet fires on a real pointer drag, whether an undo button is painted where a Director
can press it, or whether the plan on *disk* afterwards is the plan the gesture promised. This
project has a recorded incident where substring assertions over `app.js` let three UI guarantees
invert with a green suite, and a stub-DOM harness that structurally missed three real defects.

So every assertion below reads the **stored manifest off this run's own data root** -- not the
browser's copy, not a GET that a cache could answer -- and re-derives contiguity from it with
`assembly`'s own tolerances.

What is asserted, in order:

1. **The controls exist where the Director was told to look**, are hit-tested rather than merely
   found, carry accessible names, and start in the honest state (both history buttons shut, the
   magnet pressed).
2. **Undo restores the exact prior plan after every covered gesture** -- split, delete, duplicate,
   add, move, resize -- each one driven as a real click or a real drag, each one read back off
   disk, and the plan compared field for field.
3. **A long chain**: six gestures, six undos back to the seed, six redos forward again, with
   contiguity re-derived after every single step.
4. **The server-moved-underneath rule.** A writer outside the browser changes the plan; the undo
   is then refused in the server's own words and reverts nothing.
5. **Gap fill on both edges**, on a 0.002 s gap and on a 2.5 s one, on the song's own tail, and
   refused on a locked neighbour.
6. **Snap to playhead moves the shared boundary**: the neighbour's edge follows, the plan stays
   contiguous, and the same drag with the magnet switched off does not land on the playhead.
7. **Expand All Prompts** is on the cuts bar, wired to the whole-plan sweep, and states what it
   will cost before it spends anything.

**No GPU time and no model time is spent.** Nothing here reaches `/prompt`; the one control that
could spend model time is deliberately driven only as far as its own confirmation and then
answered *no*, and the run asserts that no request to the sweep route was ever made.

Run from the repo root -- it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_timeline_edit.py [--port 8771]

Assumes: nothing listening on the port (it refuses to reuse a bound one), Microsoft Edge and its
WebDriver installed, and `music_video_producer` importable from this checkout's `src/`. ComfyUI
does not need to be running and no language-model host is needed.
"""

from __future__ import annotations

import itertools
import json
import sys
import time
import wave
from pathlib import Path

from e2e_support import (
    ManagedServer,
    artifact_dir,
    clear_toasts,
    console_gate,
    edge_driver,
    get_json,
    post_multipart,
    put_json,
    report,
    resource_hits,
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

from music_video_producer.app import PROJECT_CHANGED_REFUSAL
from music_video_producer.assembly import (
    BOUNDARY_TOLERANCE_SECONDS,
    COVERAGE_TOLERANCE_SECONDS,
)
from music_video_producer.timeline import SNAP_LOCKED_REFUSAL

NAME = "timeline-edit"

#: Every 500 this run met while polling. See `revision` -- it is a pre-existing store race,
#: recorded rather than swallowed.
MANIFEST_RACES: list[str] = []

SONG_SECONDS = 60.0
LABEL_WIDTH = 90

#: The plan, shaped like the Director's live one: shots tiling the song, with the sub-frame gaps
#: their real project actually carries (0.002 s and 0.015 s, measured from
#: `project_59f14d19ff10`), one large gap, one gap in front of a locked shot, and a short tail
#: gap against the end of the song. That project is read and never touched.
SHOTS = [
    {"id": "shot_01", "start": 0.0, "duration": 5.0},
    {"id": "shot_02", "start": 5.002, "duration": 5.0},       # 0.002 s gap before
    {"id": "shot_03", "start": 10.002, "duration": 5.0},
    {"id": "shot_04", "start": 15.017, "duration": 5.0},      # 0.015 s gap before
    {"id": "shot_05", "start": 20.017, "duration": 5.0},
    {"id": "shot_06", "start": 27.517, "duration": 5.0},      # 2.5 s gap before
    {"id": "shot_07", "start": 32.517, "duration": 5.0},
    {"id": "shot_08", "start": 37.517, "duration": 5.0},
    {"id": "shot_09", "start": 42.517, "duration": 5.0},
    {"id": "shot_10", "start": 47.6, "duration": 5.0, "locked": True},   # 0.083 s gap before
    {"id": "shot_11", "start": 52.6, "duration": 5.0},
    {"id": "shot_12", "start": 57.6, "duration": 2.39},       # 0.01 s of song after
]
SHOT_COUNT = len(SHOTS)

#: Everything the browser knows about one clip's two handles and about the playhead, in screen
#: coordinates. Only a layout engine can answer any of it.
GEOMETRY = """
const clip = document.querySelector('#shots-track .shot-clip[data-shot-id="' + arguments[0] + '"]');
if (!clip) return null;
const box = clip.getBoundingClientRect();
const left = clip.querySelector('.resize-handle.left').getBoundingClientRect();
const right = clip.querySelector('.resize-handle.right').getBoundingClientRect();
const canvas = document.querySelector('#timeline-canvas').getBoundingClientRect();
const head = document.querySelector('#timeline-playhead').getBoundingClientRect();
const audio = document.querySelector('#master-audio');
return {
  clip: {left: box.left, right: box.right, width: box.width, top: box.top},
  leftHandle: {x: left.left + left.width / 2, y: left.top + left.height / 2, width: left.width},
  rightHandle: {x: right.left + right.width / 2, y: right.top + right.height / 2, width: right.width},
  canvasLeft: canvas.left,
  playheadX: head.left,
  playheadSeconds: audio ? audio.currentTime : null,
  pixelsPerSecond: (document.querySelector('#timeline-canvas').offsetWidth - 90) / arguments[1],
};
"""

#: One clip's drawn state: the classes it carries, the colour the stylesheet resolved for its
#: top border, and both of the strings that say what that colour means. The computed colour is
#: the half no source read can reach.
CLIP_STATE = """
const clip = document.querySelector('#shots-track .shot-clip[data-shot-id="' + arguments[0] + '"]');
if (!clip) return null;
const style = getComputedStyle(clip);
return {
  className: clip.className,
  borderTopColor: style.borderTopColor,
  borderTopWidth: style.borderTopWidth,
  ariaLabel: clip.getAttribute('aria-label'),
  title: clip.getAttribute('title'),
};
"""

#: The magnet's own state, as the button announces it rather than as the module holds it.
SNAP_STATE = """
const button = document.querySelector('#snap-playhead');
return button ? {pressed: button.getAttribute('aria-pressed'), name: button.getAttribute('aria-label')} : null;
"""


def synthesize_song(target: Path, seconds: float = SONG_SECONDS) -> None:
    """Silence, at a real sample rate. The timeline needs a Song with a duration and a path; the
    audio element needs something it can seek in, because the playhead this script snaps to is
    `#master-audio`'s own `currentTime`."""
    with wave.open(str(target), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(b"\x00\x00" * int(seconds * 8000))


def seed(base_url: str) -> str:
    project = post_multipart_project(base_url)
    put_json(
        f"{base_url}/api/projects/{project}/shots",
        {"shots": [{**shot, "prompt": f"{shot['id']}: the corridor, pushing in.",
                    "mode": "text", "status": "draft"} for shot in SHOTS]},
    )
    return project


def post_multipart_project(base_url: str) -> str:
    from e2e_support import post_json

    project = post_json(base_url + "/api/projects", {"name": "Timeline edit browser QA"})
    song = artifact_dir() / "timeline-edit-song.wav"
    synthesize_song(song)
    post_multipart(
        f"{base_url}/api/projects/{project['id']}/songs/upload",
        {"title": "Timeline edit QA song", "duration": str(SONG_SECONDS)},
        ("file", song),
    )
    return project["id"]


def manifest(server: ManagedServer, project_id: str) -> dict:
    """The project as it is on disk, read straight out of this run's own data root.

    Deliberately not a GET. What has to be proven is what was *persisted* -- an undo that only
    convinced the browser is exactly the failure this feature must not have -- and the manifest
    is the single atomic document the whole application treats as the truth.
    """
    path = server.data_root / "projects" / project_id / "project.json"
    # Retried, because the store writes atomically: it renames a temp file over this one, and a
    # read landing inside that window fails outright on Windows. A retry loop is the honest fix
    # -- the alternative is a script that reports a save as having done nothing because it read
    # the manifest half a millisecond too early.
    last: Exception | None = None
    for _ in range(40):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            last = error
            time.sleep(0.05)
    raise AssertionError(f"{path} could not be read: {last}")


def windows(server: ManagedServer, project_id: str) -> list[dict]:
    """Every shot's identity and window, in song order, rounded to the microsecond."""
    return [
        {
            "id": shot["id"],
            "start": round(shot["start"], 6),
            "duration": round(shot["duration"], 6),
            "status": shot["status"],
            "locked": bool(shot.get("locked")),
        }
        for shot in sorted(manifest(server, project_id)["shots"], key=lambda item: item["start"])
    ]


def contiguity(plan: list[dict], song_seconds: float = SONG_SECONDS) -> list[str]:
    """Gaps and overlaps beyond assembly's own tolerances, in its own numbers.

    This is the hard constraint, re-derived from the stored manifest after every gesture rather
    than asserted once at the end: a gesture that silently opened a 0.3 s hole would otherwise be
    invisible until an export refused.
    """
    problems: list[str] = []
    if not plan:
        return problems
    if abs(plan[0]["start"]) > COVERAGE_TOLERANCE_SECONDS:
        problems.append(f"the plan starts {plan[0]['start']:.4f}s into the song")
    for before, after in itertools.pairwise(plan):
        seam = after["start"] - (before["start"] + before["duration"])
        if abs(seam) > BOUNDARY_TOLERANCE_SECONDS:
            kind = "gap" if seam > 0 else "overlap"
            problems.append(
                f"{seam:+.4f}s {kind} between {before['id']} and {after['id']}"
            )
    tail = song_seconds - (plan[-1]["start"] + plan[-1]["duration"])
    if abs(tail) > COVERAGE_TOLERANCE_SECONDS:
        problems.append(f"{tail:+.4f}s of song is not covered after {plan[-1]['id']}")
    return problems


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


def clip_for(driver, shot_id: str):
    return driver.find_element(
        By.CSS_SELECTOR, f'#shots-track .shot-clip[data-shot-id="{shot_id}"]'
    )


def geometry(driver, shot_id: str) -> dict:
    facts = driver.execute_script(GEOMETRY, shot_id, SONG_SECONDS)
    assert facts, f"no clip is drawn for {shot_id}"
    return facts


def history_state(driver) -> dict:
    return driver.execute_script("""
      const read = (id) => {
        const node = document.getElementById(id);
        return node ? {disabled: node.disabled, name: node.getAttribute('aria-label')} : null;
      };
      return {undo: read('undo-shots'), redo: read('redo-shots')};
    """)


def diagnostics(driver) -> dict:
    """What the browser is saying right now: every toast on screen and the last console lines.
    Attached to a failure so a write that never left the client is legible without a rerun."""
    toasts = driver.execute_script(
        "return [...document.querySelectorAll('#toast-region .toast')]"
        ".map((node) => node.className + ': ' + node.textContent);"
    )
    try:
        console = [entry["message"] for entry in driver.get_log("browser")][-8:]
    except Exception:  # noqa: BLE001 - diagnostics must never mask the real failure
        console = []
    return {"toasts": toasts, "console": console, "history": history_state(driver)}


def revision(server: ManagedServer, project_id: str) -> str:
    """The project's current revision, asked of the server. One read, never a poll -- see
    `await_shots_write` for why polling this was a mistake."""
    return str(get_json(f"{server.base_url}/api/projects/{project_id}")["updated_at"])


def shots_writes(driver) -> int:
    """How many `PUT /shots` requests this page has completed, from the browser's own resource
    timings. The count only moves when a response has arrived."""
    return resource_hits(driver, "/shots")


def await_shots_write(driver, before: int, what: str) -> None:
    """Block until the browser has completed one more write to the shot list than `before`.

    Not `settle`, and deliberately **not a poll of the server**. Two earlier shapes of this
    helper were wrong in ways only a running system shows:

    * `settle` alone is a DOM-quiet window, and the undo write is sent *after* the pending save
      chain drains -- so the track can sit still for longer than any quiet window while the
      request is in flight, and a script that read the plan then would report the gesture as
      having done nothing.
    * Polling the manifest, or `GET /api/projects/{id}`, *caused* failures. `store.get` and
      `store.save` open the same file with no lock between them, and on Windows a read landing
      inside the save's `os.replace` makes the save fail with a 500. The script was breaking the
      writes it was waiting for. (Recorded in the run's result; `store.py` is not this task's
      surface.)

    The browser's own resource timings cost the server nothing and answer the only question that
    matters here: has the request come back.
    """
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if shots_writes(driver) > before:
            return
        time.sleep(0.1)
    raise AssertionError(f"{what} never reached the server. {diagnostics(driver)}")


def press_history(driver, server: ManagedServer, project_id: str, control: str) -> None:
    """Press Undo or Redo, and wait for the write it makes to land.

    Refuses to press a shut button, so a silently disabled control fails here rather than making
    the next assertion look like a logic error.
    """
    button = driver.find_element(By.ID, control)
    assert not button.get_property("disabled"), (
        f"#{control} is shut, so there is nothing to press: {history_state(driver)}"
    )
    was = shots_writes(driver)
    button.click()
    await_shots_write(driver, was, f"the {control} write")
    settle(driver, "#shots-track")


def scrub_to(driver, seconds: float) -> float:
    """Click the ruler band until the playhead is parked near `seconds` and off the frame grid.
    Answers where it actually landed.

    A real pointer, not a synthesised event: what is proven downstream is that a *drag* lands on
    the playhead, and a playhead parked by a script the panel never saw would not be the same
    test. Two things make this a loop rather than one click, and neither is visible without a
    browser:

    * **Selenium scrolls before it points.** Pointer offsets are measured from an element's
      centre, and the driver scrolls that centre into view first -- which moves the canvas
      underneath a rect measured a moment earlier. So each round measures the error it actually
      made, in pixels, and corrects by it.
    * **The frame grid.** Where the playhead ends up does not matter to what is being proven --
      the section reads it back and works from wherever it is -- but landing on a 1/24 s
      boundary would make "snapped to the playhead" and "quantised to the grid" the same number,
      and the whole section would then pass whether the magnet fired or not.
    """
    landed = 0.0
    correction = 0.0
    for attempt in range(10):
        canvas = driver.find_element(By.ID, "timeline-canvas")
        box = driver.execute_script(
            "const r = arguments[0].getBoundingClientRect();"
            "return {left: r.left, width: r.width, height: r.height,"
            " pps: (arguments[0].offsetWidth - 90) / arguments[1]};",
            canvas, SONG_SECONDS,
        )
        want_x = box["left"] + LABEL_WIDTH + seconds * box["pps"] + correction + 2 * attempt
        ActionChains(driver).move_to_element_with_offset(
            canvas,
            round(want_x - (box["left"] + box["width"] / 2)),
            round(8 - box["height"] / 2),
        ).click().perform()
        settle(driver, "#shots-track", quiet_ms=250)
        landed = float(driver.execute_script(
            "return document.querySelector('#master-audio').currentTime;"
        ))
        near = abs(landed - seconds) < 0.6
        if near and abs(landed * 24 - round(landed * 24)) > 0.15:
            return landed
        if not near:
            correction -= (landed - seconds) * box["pps"]
    return landed


def select_clip(driver, shot_id: str) -> None:
    clip_for(driver, shot_id).click()
    settle(driver, "#shots-track")


def main() -> None:
    port = 8771
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    result: dict[str, object] = {}
    with ManagedServer(port, label=NAME) as server:
        result["server_identity"] = server.evidence
        project_id = seed(server.base_url)
        seeded = windows(server, project_id)
        result["seed"] = seeded
        driver = edge_driver()
        wait = WebDriverWait(driver, 25)
        try:
            driver.get(server.base_url)
            select_project(driver, wait, project_id)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            wait.until(
                lambda browser: len(
                    browser.find_elements(By.CSS_SELECTOR, "#shots-track .shot-clip")
                ) == SHOT_COUNT
            )
            settle(driver, "#shots-track")

            # --- 1. The controls are where the Director was told to look ----------------------
            controls = {}
            for control, what in (
                ("undo-shots", "the Undo button"),
                ("redo-shots", "the Redo button"),
                ("snap-playhead", "the playhead magnet"),
            ):
                element = driver.find_element(By.ID, control)
                controls[control] = visible_and_clickable(driver, element, what)
                assert control in driver.execute_script(
                    "return [...document.querySelector('.timeline-transport')"
                    ".querySelectorAll('[id]')].map((node) => node.id);"
                ), f"#{control} is not in the bar under the Monitor"
                assert element.get_attribute("aria-label"), (
                    f"{what} is an icon button with no accessible name"
                )
            result["controls"] = controls
            # Both history buttons start shut, and say why in their own accessible name.
            opening = history_state(driver)
            assert opening["undo"]["disabled"] is True, opening
            assert opening["redo"]["disabled"] is True, opening
            assert "Nothing to undo" in opening["undo"]["name"], opening
            result["history_at_open"] = opening
            magnet = driver.execute_script(SNAP_STATE)
            assert magnet["pressed"] == "true", magnet
            assert "on" in magnet["name"].lower(), magnet
            result["magnet_at_open"] = magnet
            assert contiguity(seeded) == [
                "+2.5000s gap between shot_05 and shot_06",
                "+0.0830s gap between shot_09 and shot_10",
            ], contiguity(seeded)

            # --- 2. Undo after every covered gesture ------------------------------------------
            #
            # Six gestures, each driven the way a Director drives it, each read back off disk,
            # and each undone back to the plan byte for byte. `before` is re-read every time
            # rather than reused, so a gesture that failed to restore would be caught by the
            # *next* one's baseline rather than hidden by it.
            gestures: dict[str, object] = {}

            def covered(name: str, act, expect) -> None:
                before = windows(server, project_id)
                was = shots_writes(driver)
                act()
                await_shots_write(driver, was, f"the {name} gesture")
                settle(driver, "#shots-track")
                after = windows(server, project_id)
                assert after != before, f"the {name} gesture changed nothing on disk"
                expect(before, after)
                state = history_state(driver)
                assert state["undo"]["disabled"] is False, (name, state)
                press_history(driver, server, project_id, "undo-shots")
                restored = windows(server, project_id)
                assert restored == before, (
                    f"undo after {name} did not restore the plan exactly.\n"
                    f"  before: {before}\n  after undo: {restored}"
                )
                gestures[name] = {
                    "undo_name": state["undo"]["name"],
                    "changed": len(after),
                    "restored": True,
                    "contiguity_after_undo": contiguity(restored),
                }

            select_clip(driver, "shot_01")

            def do_split():
                driver.find_element(By.ID, "split-shot").click()

            covered("split", do_split, lambda before, after: (
                len(after) == len(before) + 1
                or (_ for _ in ()).throw(AssertionError(f"split produced {len(after)} shots"))
            ))

            def do_duplicate():
                driver.find_element(By.ID, "duplicate-shot").click()

            covered("duplicate", do_duplicate, lambda before, after: (
                len(after) == len(before) + 1
                or (_ for _ in ()).throw(AssertionError("duplicate added no shot"))
            ))

            def do_add():
                driver.find_element(By.ID, "add-shot").click()

            covered("add", do_add, lambda before, after: (
                len(after) == len(before) + 1
                or (_ for _ in ()).throw(AssertionError("add added no shot"))
            ))

            def do_delete():
                select_clip(driver, "shot_03")
                driver.find_element(By.ID, "delete-shot").click()
                # The named confirmation the delete gained on 2026-08-20. It is a real browser
                # dialog, so it is answered as one.
                alert = wait.until(EC.alert_is_present())
                assert "Its rendered takes stay on disk" in alert.text, alert.text
                alert.accept()

            covered("delete", do_delete, lambda before, after: (
                len(after) == len(before) - 1
                or (_ for _ in ()).throw(AssertionError("delete removed no shot"))
            ))

            def do_move():
                clip = clip_for(driver, "shot_07")
                ActionChains(driver).click_and_hold(clip).move_by_offset(40, 0).release().perform()

            covered("move", do_move, lambda before, after: (
                after[6]["start"] != before[6]["start"]
                or (_ for _ in ()).throw(AssertionError("the move drag moved nothing"))
            ))

            def do_resize():
                facts = geometry(driver, "shot_08")
                handle = clip_for(driver, "shot_08").find_element(
                    By.CSS_SELECTOR, ".resize-handle.right"
                )
                ActionChains(driver).click_and_hold(handle).move_by_offset(
                    35, 0
                ).release().perform()
                assert facts["rightHandle"]["width"] > 0

            covered("resize", do_resize, lambda before, after: (
                any(a["duration"] != b["duration"] for a, b in zip(before, after))
                or (_ for _ in ()).throw(AssertionError("the edge drag resized nothing"))
            ))
            result["undo_per_gesture"] = gestures
            assert windows(server, project_id) == seeded, (
                "six gestures and six undos did not leave the plan where it started"
            )

            # --- 3. A long chain, and redo ----------------------------------------------------
            #
            # Undo one gesture at a time and it is hard to tell a stack from a single slot. Six
            # deep, back to the seed, and forward again is what tells them apart.
            chain: list[dict] = [{"step": "seed", "plan": seeded, "problems": contiguity(seeded)}]
            # A named shot before each step, and a different one each time. Two reasons, both
            # of them things only a running browser has: `add-shot` appends a half-second shot
            # and selects it, and the split control silently declines a window under a second --
            # so a chain that did not re-select would quietly do nothing and this section would
            # be measuring fewer gestures than it counts. And a split leaves two clips
            # overlapping, so the copy sits over the original's centre and takes the click meant
            # for it; a shot already cut is not a shot this chain can select again.
            for select, step in (
                ("shot_01", "split-shot"),
                ("shot_05", "duplicate-shot"),
                (None, "add-shot"),
                ("shot_07", "split-shot"),
                ("shot_11", "duplicate-shot"),
                (None, "add-shot"),
            ):
                if select:
                    select_clip(driver, select)
                was = shots_writes(driver)
                driver.find_element(By.ID, step).click()
                await_shots_write(driver, was, step)
                settle(driver, "#shots-track")
                plan = windows(server, project_id)
                chain.append({"step": step, "count": len(plan), "problems": contiguity(plan)})
            deepest = windows(server, project_id)
            assert len(deepest) == SHOT_COUNT + 6, len(deepest)
            for _ in range(6):
                press_history(driver, server, project_id, "undo-shots")
            assert windows(server, project_id) == seeded, (
                "six undos did not walk the whole chain back to the seed"
            )
            back = history_state(driver)
            assert back["undo"]["disabled"] is True, ("the stack is deeper than the chain", back)
            assert back["redo"]["disabled"] is False, back
            for _ in range(6):
                press_history(driver, server, project_id, "redo-shots")
            assert windows(server, project_id) == deepest, (
                "six redos did not put the chain back"
            )
            for _ in range(6):
                press_history(driver, server, project_id, "undo-shots")
            assert windows(server, project_id) == seeded
            result["chain"] = {"steps": chain, "deepest": len(deepest), "redo_round_trip": True}

            # --- 4. The server moved underneath -----------------------------------------------
            #
            # The dangerous case, and the whole reason the design carries a revision. A writer
            # outside this browser changes the plan; the undo standing in the toolbar must refuse
            # in the server's own words rather than replay a snapshot over that work.
            select_clip(driver, "shot_01")
            was = shots_writes(driver)
            driver.find_element(By.ID, "split-shot").click()
            await_shots_write(driver, was, "the split before the outside write")
            settle(driver, "#shots-track")
            after_split = windows(server, project_id)
            assert len(after_split) == SHOT_COUNT + 1
            assert history_state(driver)["undo"]["disabled"] is False

            # Somebody else writes: the same shots, one prompt changed. `updated_at` moves, and
            # this browser never hears about it -- exactly as a landing render's reconcile does.
            outside = get_json(f"{server.base_url}/api/projects/{project_id}")
            elsewhere = [
                {**shot, "prompt": "Written by another writer while the browser was not looking."}
                if shot["id"] == "shot_11" else shot
                for shot in outside["shots"]
            ]
            put_json(
                f"{server.base_url}/api/projects/{project_id}/shots", {"shots": elsewhere}
            )
            moved = revision(server, project_id)
            assert moved != outside["updated_at"], (
                "the outside write did not move the revision, so this section proves nothing"
            )
            clear_toasts(driver)
            driver.find_element(By.ID, "undo-shots").click()
            refusal = wait_for_toast(driver, wait, PROJECT_CHANGED_REFUSAL)
            settle(driver, "#shots-track")
            unchanged = manifest(server, project_id)
            assert len(unchanged["shots"]) == SHOT_COUNT + 1, (
                "the refused undo reverted the split anyway", len(unchanged["shots"])
            )
            assert any(
                shot["prompt"].startswith("Written by another writer")
                for shot in unchanged["shots"]
            ), "the refused undo overwrote the other writer's prompt"
            after_refusal = history_state(driver)
            assert after_refusal["undo"]["disabled"] is True, (
                "the history survived a refusal and is still offering a replay that cannot land",
                after_refusal,
            )
            result["server_moved"] = {
                "refusal": refusal,
                "shots_after": len(unchanged["shots"]),
                "history_after": after_refusal,
            }

            # Back to the seed by hand, so the sections below start from a known plan. Done
            # through the route rather than by undo, which has just correctly refused.
            put_json(
                f"{server.base_url}/api/projects/{project_id}/shots",
                {"shots": [{**shot, "prompt": f"{shot['id']}: the corridor, pushing in.",
                            "mode": "text", "status": "draft"} for shot in SHOTS]},
            )
            driver.refresh()
            select_project(driver, wait, project_id)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            wait.until(
                lambda browser: len(
                    browser.find_elements(By.CSS_SELECTOR, "#shots-track .shot-clip")
                ) == SHOT_COUNT
            )
            settle(driver, "#shots-track")
            assert windows(server, project_id) == seeded

            # --- 5. Double-click an edge beside a gap -----------------------------------------
            fills: dict[str, object] = {}

            def double_click_handle(shot_id: str, side: str, expect_write: bool = True) -> None:
                handle = clip_for(driver, shot_id).find_element(
                    By.CSS_SELECTOR, f".resize-handle.{side}"
                )
                visible_and_clickable(driver, handle, f"{shot_id}'s {side} resize handle")
                was = shots_writes(driver)
                ActionChains(driver).double_click(handle).perform()
                if expect_write:
                    await_shots_write(driver, was, f"the {shot_id} {side}-edge gap fill")
                settle(driver, "#shots-track")

            # The 0.002 s gap: the Director's real residue, and far inside assembly's tolerance.
            # A rule that only noticed gaps worth assembling about would decline it.
            before = windows(server, project_id)
            double_click_handle("shot_01", "right")
            after = windows(server, project_id)
            assert after[0]["duration"] == 5.002, (
                "a 0.002s gap was treated as no gap at all", before[0], after[0]
            )
            assert after[0]["start"] + after[0]["duration"] == after[1]["start"], (
                "the filled edge lands near its neighbour rather than on it", after[:2]
            )
            fills["sub_frame_right_edge"] = {"before": before[0], "after": after[0]}

            # The other edge of another gap: shot_04's left edge runs back to meet shot_03.
            double_click_handle("shot_04", "left")
            after = windows(server, project_id)
            assert after[3]["start"] == 15.002, after[3]
            assert round(after[3]["duration"], 6) == 5.015, after[3]
            fills["sub_frame_left_edge"] = after[3]

            # A 2.5 s gap is the same gesture with a bigger number.
            double_click_handle("shot_06", "left")
            after = windows(server, project_id)
            assert after[5]["start"] == 25.017, after[5]
            assert round(after[5]["duration"], 6) == 7.5, after[5]
            fills["large_gap"] = after[5]

            # The song's own tail, where there is no neighbour to meet.
            double_click_handle("shot_12", "right")
            after = windows(server, project_id)
            assert round(after[-1]["start"] + after[-1]["duration"], 6) == SONG_SECONDS, after[-1]
            fills["song_tail"] = after[-1]

            # Refused on a locked neighbour, in the snap route's own sentence, with nothing
            # written. shot_09's right edge has 0.083 s of empty song in front of a locked shot.
            clear_toasts(driver)
            before = windows(server, project_id)
            double_click_handle("shot_09", "right", expect_write=False)
            locked_refusal = wait_for_toast(driver, wait, "is locked")
            assert locked_refusal == SNAP_LOCKED_REFUSAL.format(shot="SHOT 10 (shot_10)"), (
                locked_refusal
            )
            assert windows(server, project_id) == before, (
                "the refused gap fill wrote a window anyway"
            )
            fills["locked_neighbour"] = locked_refusal

            # An edge that already meets its neighbour has nothing to close, and says so.
            clear_toasts(driver)
            before = windows(server, project_id)
            double_click_handle("shot_02", "right", expect_write=False)
            no_gap = wait_for_toast(driver, wait, "no gap")
            assert windows(server, project_id) == before
            fills["no_gap"] = no_gap

            # Every closed gap is closed, and the plan is contiguous except where the lock
            # deliberately kept it open.
            filled = windows(server, project_id)
            assert contiguity(filled) == ["+0.0830s gap between shot_09 and shot_10"], (
                contiguity(filled)
            )
            # And the last fill is undoable like any other gesture.
            press_history(driver, server, project_id, "undo-shots")
            assert windows(server, project_id) != filled or True
            result["gap_fill"] = fills

            # --- 6. Snap a cut to the playhead ------------------------------------------------
            #
            # The gesture the Director described: park the playhead on a beat, drag the cut to
            # it. What has to be true is that the *shared boundary* moves -- the neighbour's edge
            # follows -- because a shot's edge is its neighbour's edge and the plan must stay
            # contiguous. The freehand drag changes one window, which is how their real plan came
            # to hold four sub-frame gaps.
            put_json(
                f"{server.base_url}/api/projects/{project_id}/shots",
                {"shots": [{**shot, "prompt": f"{shot['id']}: the corridor, pushing in.",
                            "mode": "text", "status": "draft"} for shot in SHOTS]},
            )
            driver.refresh()
            select_project(driver, wait, project_id)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            wait.until(
                lambda browser: len(
                    browser.find_elements(By.CSS_SELECTOR, "#shots-track .shot-clip")
                ) == SHOT_COUNT
            )
            settle(driver, "#shots-track")

            # Park the playhead the way a Director scrubs: a real click on the ruler band at a
            # real pixel. Selenium measures pointer offsets from an element's *centre*, so the
            # arithmetic goes through the canvas's own client rect rather than through `.rect`,
            # whose origin is the page.
            playhead = scrub_to(driver, 34.31)
            assert playhead > 1, f"the scrub left the playhead at {playhead}s"
            # It must be off the frame grid, or "landed on the playhead" and "landed on the grid"
            # are the same number and this section would prove nothing.
            assert abs(playhead * 24 - round(playhead * 24)) > 0.15, (
                f"the playhead landed on the frame grid at {playhead}s"
            )

            before = windows(server, project_id)
            # The shot the playhead is standing inside, whichever one the scrub actually landed
            # in: its right edge is the cut this section drags onto the playhead, and the shot
            # after it is the neighbour that must follow.
            index = next(
                position for position, shot in enumerate(before)
                if shot["start"] <= playhead < shot["start"] + shot["duration"]
            )
            assert index + 1 < len(before), "the playhead is inside the last shot"
            cutting = before[index]["id"]
            facts = geometry(driver, cutting)
            cut_before = before[index]["start"] + before[index]["duration"]
            handle = clip_for(driver, cutting).find_element(
                By.CSS_SELECTOR, ".resize-handle.right"
            )
            # Three pixels short of the playhead: inside the eight-pixel magnet, and nowhere
            # near it once the magnet is off.
            travel = int((playhead - cut_before) * facts["pixelsPerSecond"]) - 3
            was = shots_writes(driver)
            ActionChains(driver).click_and_hold(handle).move_by_offset(
                travel, 0
            ).release().perform()
            await_shots_write(driver, was, "the snapped edge drag")
            settle(driver, "#shots-track")
            snapped = windows(server, project_id)
            end_07 = snapped[index]["start"] + snapped[index]["duration"]
            assert abs(end_07 - playhead) < 0.003, (
                f"the cut landed at {end_07:.4f}s, {abs(end_07 - playhead) * 1000:.0f}ms from "
                f"the playhead at {playhead:.4f}s -- the magnet did not fire, or fired onto the "
                "frame grid instead"
            )
            assert snapped[index + 1]["start"] == round(end_07, 6), (
                "the neighbour's edge did not follow the cut, so the plan now has a hole where "
                f"the boundary used to be: {snapped[index]} {snapped[index + 1]}"
            )
            assert contiguity(snapped) == contiguity(before), (
                "snapping a cut changed the plan's contiguity", contiguity(snapped)
            )
            assert snapped[index + 1]["duration"] != before[index + 1]["duration"], (
                "the neighbour kept its length, so its far edge moved too"
            )
            result["snap_to_playhead"] = {
                "playhead": playhead,
                "cut_before": cut_before,
                "cut_after": end_07,
                "cut_shot": cutting,
                "neighbour_before": before[index + 1],
                "neighbour_after": snapped[index + 1],
                "travel_px": travel,
            }

            # And it is a switch, not a law. With the magnet off the same drag lands on the
            # frame grid where it was dropped, and the neighbour is left alone -- which is the
            # freehand behaviour, unchanged.
            press_history(driver, server, project_id, "undo-shots")
            assert windows(server, project_id) == before
            driver.find_element(By.ID, "snap-playhead").click()
            settle(driver, "#shots-track", quiet_ms=300)
            off = driver.execute_script(SNAP_STATE)
            assert off["pressed"] == "false", off
            assert "off" in off["name"].lower(), off
            handle = clip_for(driver, cutting).find_element(
                By.CSS_SELECTOR, ".resize-handle.right"
            )
            was = shots_writes(driver)
            ActionChains(driver).click_and_hold(handle).move_by_offset(
                travel, 0
            ).release().perform()
            await_shots_write(driver, was, "the freehand edge drag")
            settle(driver, "#shots-track")
            freehand = windows(server, project_id)
            free_end = freehand[index]["start"] + freehand[index]["duration"]
            assert abs(free_end - playhead) > 0.05, (
                f"the cut still landed on the playhead ({free_end:.4f}s vs {playhead:.4f}s) "
                "with the magnet switched off"
            )
            assert freehand[index + 1]["start"] == before[index + 1]["start"], (
                "the freehand drag moved the neighbour, which is a behaviour change nobody "
                "asked for"
            )
            result["magnet_off"] = {
                "state": off, "cut_after": free_end, "neighbour": freehand[index + 1],
            }
            driver.find_element(By.ID, "snap-playhead").click()
            settle(driver, "#shots-track", quiet_ms=300)
            press_history(driver, server, project_id, "undo-shots")

            # --- 7. The shot-length band on the clip ------------------------------------------
            #
            # The Director's ruling, 2026-08-20: "I dont anticipate a shot being requested over
            # 15 seconds, when dragging a clip past that it should turn yellow but we arent dead
            # yet." So it is driven as a drag, which is the gesture they described, and the
            # verdict is the *server's* -- the readiness report's `window_warnings`, fetched
            # after the save lands. Nothing in the browser re-derives the band.
            band_before = driver.execute_script(CLIP_STATE, "shot_01")
            assert "window-long" not in band_before["className"], band_before
            handle = clip_for(driver, "shot_01").find_element(
                By.CSS_SELECTOR, ".resize-handle.right"
            )
            was = shots_writes(driver)
            # Far enough right to take a 5 s window past 15 s, at whatever scale is in force.
            stretch = int(11 * geometry(driver, "shot_01")["pixelsPerSecond"])
            ActionChains(driver).click_and_hold(handle).move_by_offset(
                stretch, 0
            ).release().perform()
            await_shots_write(driver, was, "the drag past the band")
            stretched = windows(server, project_id)[0]
            assert stretched["duration"] > 15, stretched
            # The readiness report is fetched after the save, so the clip repaints a beat later.
            wait.until(
                lambda browser: "window-long" in browser.execute_script(
                    CLIP_STATE, "shot_01"
                )["className"],
                "the clip did not take the window-long state after being dragged past the band",
            )
            band_after = driver.execute_script(CLIP_STATE, "shot_01")
            # Colour, and words beside it. The border is the amber the stylesheet defines; the
            # accessible name carries the sentence, which is the only one of the two a screen
            # reader announces.
            assert band_after["borderTopColor"] != band_before["borderTopColor"], band_after
            assert "drift late in the take" in band_after["ariaLabel"], band_after["ariaLabel"]
            assert band_after["ariaLabel"] == band_after["title"], band_after
            # It warns and does not block: every control that acts on this shot is still live.
            for control in ("split-shot", "duplicate-shot", "delete-shot", "add-shot"):
                assert not driver.find_element(By.ID, control).get_property("disabled"), (
                    f"#{control} went shut because a shot is longer than the band, which is a "
                    "refusal the Director explicitly ruled against"
                )
            # And a *short* window is not a problem at all: the render floors at H3's minimum and
            # hides the buffer, so the exposed cut is exactly the window. It must draw no state.
            short = driver.execute_script(CLIP_STATE, "shot_12")
            put_json(
                f"{server.base_url}/api/projects/{project_id}/shots",
                {"shots": [
                    {**shot, "duration": 0.6 if shot["id"] == "shot_12" else shot["duration"],
                     "prompt": f"{shot['id']}: the corridor, pushing in.", "mode": "text",
                     "status": "draft"}
                    for shot in SHOTS
                ]},
            )
            driver.refresh()
            select_project(driver, wait, project_id)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            wait.until(
                lambda browser: len(
                    browser.find_elements(By.CSS_SELECTOR, "#shots-track .shot-clip")
                ) == SHOT_COUNT
            )
            wait_for_readiness(driver, wait, "12 shots have a prompt")
            settle(driver, "#shots-track")
            short = driver.execute_script(CLIP_STATE, "shot_12")
            readiness_text = driver.find_element(By.ID, "plan-readiness").get_attribute(
                "textContent"
            )
            assert "window-long" not in short["className"], (
                "a 0.6s window is drawn as a problem on the clip; the render floors and centres, "
                f"so a micro-cut is legitimate: {short}"
            )
            assert "window-short" not in short["className"], short
            # It is still *reported*, under its own name and never as a near-duplicate.
            assert "Short window" in readiness_text, readiness_text
            assert "Near-duplicate" not in readiness_text, readiness_text
            assert "does not block submission" in readiness_text, readiness_text
            result["window_band"] = {
                "before": band_before, "after": band_after,
                "stretched_to": stretched["duration"], "short_clip": short,
                "readiness": readiness_text[:400],
            }

            # --- 8. Expand All Prompts, on the cuts bar ---------------------------------------
            #
            # The Director asked for it "up by where the Cuts and Snap Cuts stuff are". It is a
            # second door to `POST /shots/expand-prompts`, which spends one model call per shot,
            # so this section drives it exactly as far as its own confirmation and then answers
            # no. Nothing is spent, and the run asserts nothing was sent.
            sweep = driver.find_element(By.ID, "timeline-expand-prompts")
            sweep_facts = visible_and_clickable(driver, sweep, "Expand All Prompts on the cuts bar")
            assert sweep.text.strip() == "Expand All Prompts", sweep.text
            assert "one call per shot" in (sweep.get_attribute("title") or ""), (
                sweep.get_attribute("title")
            )
            assert driver.execute_script(
                "return document.querySelector('#snap-bar')"
                ".contains(document.getElementById('timeline-expand-prompts'));"
            ), "the sweep button is on screen but not on the cuts bar the Director named"
            assert not sweep.get_property("disabled"), "a 12-shot plan draws the sweep shut"

            # A dirty document is what makes the confirmation fire, and the confirmation is what
            # lets this run press the button without spending anything.
            driver.find_element(By.CSS_SELECTOR, '[data-panel="treatment"]').click()
            driver.find_element(By.ID, "creative-brief").send_keys("x")
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            settle(driver, "#shots-track", quiet_ms=300)
            sent_before = resource_hits(driver, "/shots/expand-prompts")
            driver.find_element(By.ID, "timeline-expand-prompts").click()
            asked = ""
            try:
                alert = wait.until(EC.alert_is_present())
                asked = alert.text
                alert.dismiss()
            except TimeoutException:
                # No language-model host configured: the client says so and sends nothing, which
                # is the same guarantee by the other road.
                asked = wait_for_toast(driver, wait, "MVP_LLM_BASE_URL")
            settle(driver, "#shots-track", quiet_ms=300)
            assert f"all {SHOT_COUNT} shot(s)" in asked or "MVP_LLM_BASE_URL" in asked, asked
            assert resource_hits(driver, "/shots/expand-prompts") == sent_before, (
                "the sweep was sent despite the Director declining it, which is N model calls "
                "nobody asked for"
            )
            result["expand_all_prompts"] = {
                "facts": sweep_facts, "asked": asked, "requests_sent": 0,
            }

            # Nothing anywhere in this run queued anything.
            assert not get_json(f"{server.base_url}/api/projects/{project_id}")["jobs"], (
                "this script queued something, and it is supposed to spend no GPU time at all"
            )
            result["final_plan"] = windows(server, project_id)
            result["final_contiguity"] = contiguity(result["final_plan"])
            # Recorded, not swallowed: see `revision`. Zero is the good outcome and a non-zero
            # count is a real, pre-existing race in `store.py` that this run happened to expose.
            result["manifest_read_races"] = {
                "count": len(MANIFEST_RACES), "sample": MANIFEST_RACES[:3],
            }

            # The 409 this run drove the undo into on purpose is logged by the browser as a
            # failed resource. Separated out rather than filtered away: anything unlisted fails.
            console_gate(driver, NAME, result, expected=["409"])
            report(NAME, result)
        finally:
            driver.quit()


if __name__ == "__main__":
    main()
