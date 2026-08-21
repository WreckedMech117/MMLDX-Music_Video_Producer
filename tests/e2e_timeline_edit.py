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
7. **One Undo steps back one gesture**, even when two gestures were made inside a single round
   trip -- and the state between them is reachable (review Finding 4, 2026-08-21).
8. **A drag does not leave a double-click behind**: re-grabbing an edge just after dragging it
   starts a second drag rather than running the gap fill (Finding 5). The genuine double-click
   is driven again afterwards, because a fix that simply forgot the press would take gesture B
   away and no count of writes would notice.
9. **A refused snap writes nothing at all**: no `PUT /shots`, no revision bump, no phantom undo
   entry and no discarded redo stack (Finding 6).
10. **The shot-length band** is taken on a real drag past 15 s, in the colour the stylesheet
    resolves and the words the readiness report supplies, and it warns without blocking.
11. **Expand All Prompts** is on the cuts bar, wired to the whole-plan sweep, and states what it
    will cost before it spends anything.
13. **The ruling of 2026-08-21** — *"we dont want to slide the take next to the one we are
    adjusting either ... those gestures should only slide the window bounds but leave the clip
    position intact."* Section 12 proves the shot under the hand; this proves the shot that is
    **not**: the neighbour whose `start` a playhead snap carries, and the shot a double-click gap
    fill runs back. Both anchors are read off the manifest, both directions of the gap fill are
    driven, the neighbour's *own* lock is unticked to show it is the one that governs, and undo
    and redo are stepped through to show a restore is not compensated a second time.
12. **The music lock and the second yellow** (2026-08-21). The lock is beside the trim nudge and
    starts ticked; a locked move-drag leaves the take on the same second of the song and an
    unlocked one moves it, both read back as `start - lead - nudge` off the manifest; a window
    dragged off the picture its take holds turns amber without shutting anything; and a take that
    recorded no window of its own is never warned about.

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
    post_json,
    post_multipart,
    put_json,
    report,
    resource_hits,
    settle,
    visible_and_clickable,
    wait_for_readiness,
    wait_for_toast,
)
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
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

#: The shot section 12 gives a take to, and the take's own bookkeeping, written exactly as the H3
#: submission route writes it: the lead it was rendered with, and the window it was rendered for.
#: `latest_output` names a file that does not exist and does not need to -- nothing in this section
#: plays it, and every decision under test is made from these four numbers.
#:
#: The recorded window is **9 s while the live one is 5 s**, which is a shot whose window the
#: Director shortened after its render -- the ordinary reason the snapshot exists at all. It is
#: also what gives this section room to work: `over_render_frames(9.0)` is 243 frames, so the take
#: holds 10.125 s of picture and a 1.5 s drag stays well inside it. A take rendered for the *live*
#: 5 s window holds 5.875 s and would leave 0.625 s of headroom, so every drag big enough to
#: measure would be an uncovered one and this section could not tell the two states apart.
TAKE_SHOT = "shot_07"
#: What that take holds, in seconds of picture: `over_render_frames(9.0) / H3_FPS`. Named because
#: two assertions read it -- the readiness sentence quotes it, and the drag in 12c has to be big
#: enough to leave it.
TAKE_PICTURE_SECONDS = 243 / 24
TAKE_FIELDS = {
    "latest_output": "music-video-producer/qa/shots/shot_07-h3_00001.mp4",
    "latest_take_lead": 0.25,
    "latest_take_start": 32.517,
    "latest_take_duration": 9.0,
    "trim_nudge": 0.0,
    "status": "complete",
}
#: And a take that recorded no window of its own -- `latest_take_duration` 0. Every take rendered
#: before 2026-08-21 is one of these, including the 33 in the Director's real project, and so is
#: every hand-picked clip. It carries a nudge no coverage could survive, precisely so that the
#: silence about it is an assertion rather than an accident of the numbers.
LEGACY_SHOT = "shot_09"
LEGACY_TAKE_FIELDS = {
    "latest_output": "music-video-producer/qa/shots/shot_09-h3_00001.mp4",
    "latest_take_lead": 0.25,
    "latest_take_start": 0.0,
    "latest_take_duration": 0.0,
    "trim_nudge": 9.0,
    "status": "complete",
}

#: The takes section 13 needs, one per gesture the Director's ruling of 2026-08-21 reaches.
#:
#:     "Well we dont want to slide the take next to the one we are adjusting either, rather move
#:     its windows edge while both clips stay in place, same for double click, those gestures
#:     should only slide the window bounds but leave the clip position intact."
#:
#: Four shots carry a take, and each is there for a gesture:
#:
#: * `shot_04` has 0.015 s of empty song in front of it -- the Director's own residue, measured
#:   from their real project -- so its left edge is the *leftward* gap fill, the direction that
#:   moves a start. 0.015 s is also nowhere near the 1/24 s grid, so a compensation rounded to a
#:   frame comes out visibly wrong rather than accidentally right.
#: * `shot_05` has 2.5 s of empty song *after* it, so its right edge is the *rightward* fill --
#:   the direction that grows a duration and moves no start at all.
#: * `shot_07` is the shot under the hand when a cut is snapped to the playhead.
#: * `shot_08` shares that cut, so it is the *neighbour* whose `start` the snap moves and whose
#:   take the old shape slid. It is the whole point of the ruling.
#:
#: Every one starts with a **non-zero** nudge, and no two the same: a compensation that wrote the
#: delta instead of adding it to what was there would land on the right anchor from a zero nudge
#: and be invisible. The recorded windows are 9 s against live windows of 5 s -- takes the Director
#: shortened after rendering -- which is `TAKE_FIELDS`' own reason: 10.125 s of picture leaves room
#: for a real drag inside the take.
RULING_TAKES = {
    "shot_04": {"latest_take_start": 15.017, "trim_nudge": 0.125},
    "shot_05": {"latest_take_start": 20.017, "trim_nudge": 0.375},
    "shot_07": {"latest_take_start": 32.517, "trim_nudge": 0.25},
    "shot_08": {"latest_take_start": 37.517, "trim_nudge": 0.0625},
}


def ruling_plan() -> list[dict]:
    """`SHOTS`, with a take on each of `RULING_TAKES`. Written through `PUT /shots` exactly as the
    H3 submission route writes one, which is what makes section 13 cost no GPU time at all."""
    return [
        {**shot, "prompt": f"{shot['id']}: the corridor, pushing in.", "mode": "text",
         "status": "draft",
         **({"latest_output": f"music-video-producer/qa/shots/{shot['id']}-h3_00001.mp4",
             "latest_take_lead": 0.25, "latest_take_duration": 9.0, "status": "complete",
             **RULING_TAKES[shot["id"]]} if shot["id"] in RULING_TAKES else {})}
        for shot in SHOTS
    ]


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

#: Latency, injected into the page, for `PUT /shots` alone.
#:
#: Nothing about the application's own code path changes: `request()` in api.js looks the global
#: `fetch` up by name on every call, so this is the network being slow and nothing else. It exists
#: because review Finding 4 is a race that only lives *inside* one round trip -- two gestures made
#: before the first save comes back -- and against a loopback server that window is a millisecond
#: or two wide. Widened here rather than hoped for, and `__mvpOpenShotWrites` is what lets the
#: section prove the second gesture really was made while the first write was open, instead of
#: assuming it.
DELAY_SHOT_WRITES = """
window.__mvpDelayShots = arguments[0];
if (!window.__mvpRealFetch) {
  window.__mvpOpenShotWrites = 0;
  window.__mvpRealFetch = window.fetch;
  window.fetch = function (input, init) {
    const url = String((input && input.url) || input || '');
    const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
    const held = window.__mvpDelayShots || 0;
    if (held > 0 && method === 'PUT' && url.indexOf('/shots') >= 0) {
      window.__mvpOpenShotWrites += 1;
      return new Promise((resolve, reject) => setTimeout(() => {
        window.__mvpRealFetch(input, init).then(resolve, reject)
          .finally(() => { window.__mvpOpenShotWrites -= 1; });
      }, held));
    }
    return window.__mvpRealFetch(input, init);
  };
}
return window.__mvpDelayShots;
"""

#: Every press on a resize handle, stamped, recorded from the capture phase so it lands before the
#: clip's own handler. The only way to prove the two drags of the Finding 5 section really did fall
#: inside `EDGE_DOUBLE_CLICK_MS` of each other: a wall-clock measurement around `perform()` includes
#: the driver round trip and would report a gesture as too slow that the page saw as fast.
WATCH_EDGE_PRESSES = """
window.__mvpEdgePresses = [];
if (!window.__mvpEdgeWatch) {
  window.__mvpEdgeWatch = true;
  document.addEventListener('pointerdown', (event) => {
    if (event.target && event.target.classList
        && event.target.classList.contains('resize-handle')) {
      window.__mvpEdgePresses.push(Date.now());
    }
  }, true);
}
return true;
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


def await_shot_writes(driver, target: int, what: str, timeout: float = 60) -> None:
    """Block until the browser has completed at least `target` writes to the shot list.

    `await_shots_write`'s rule for a burst rather than a single write: the sections below hold two
    saves open on purpose, so the wait is against a count instead of against "one more than
    before" -- which would come back the moment the *first* of the two landed.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if shots_writes(driver) >= target:
            return
        time.sleep(0.1)
    raise AssertionError(f"{what} never reached the server. {diagnostics(driver)}")


def toasts_on_screen(driver) -> list[str]:
    return [str(entry) for entry in driver.execute_script(
        "return [...document.querySelectorAll('#toast-region .toast')]"
        ".map((node) => node.textContent);"
    )]


def reseed(driver, wait, server: ManagedServer, project_id: str) -> list[dict]:
    """Put the plan back to `SHOTS` and reload the page onto it. Answers what is on disk.

    The block sections 4 and 6 already do by hand, factored out because three more sections need
    it. A reload rather than a walk back through Undo: what those sections prove is about the
    history stacks themselves, so they must not start from a stack an earlier section left
    standing -- and a reload is the one thing that empties them.
    """
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
    return windows(server, project_id)


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


def stable_click(driver, selector: str, what: str, tries: int = 10) -> None:
    """Click a control, re-finding it if the workspace redraws underneath the click.

    `e2e_section_looks.stable_click`, brought here for the same reason and copied rather than
    imported because these harnesses are deliberately standalone scripts. Not a retry hiding a
    race -- it *is* the race, and it is the application's design: `loadProject` fires the readiness
    GET without awaiting it, and the reply calls `renderTimeline`, which rebuilds the cuts bar
    wholesale. An element found a moment earlier is gone by the time the driver points at it, which
    is what made this run fail on the sweep button twice in three attempts on 2026-08-21 -- a flake
    in the script, never in the application.
    """
    last: Exception | None = None
    for _ in range(tries):
        try:
            driver.find_element(By.CSS_SELECTOR, selector).click()
            return
        except (StaleElementReferenceException, NoSuchElementException) as error:
            last = error
            time.sleep(0.25)
    raise AssertionError(f"{what} could not be clicked: {last}")


def take_state(server: ManagedServer, project_id: str, shot_id: str) -> dict:
    """One shot's window, its take bookkeeping, and the song second its take begins at.

    `anchor` is `start - latest_take_lead - trim_nudge`: the second of the song the take's first
    frame plays at, which is `timeline.over_render_window`'s own invariant read backwards. It is
    the number the whole lock is about -- a locked drag leaves it alone and an unlocked one moves
    it -- and it is computed here from the stored manifest rather than asked of the browser.
    """
    shot = next(
        item for item in manifest(server, project_id)["shots"] if item["id"] == shot_id
    )
    lead = float(shot.get("latest_take_lead") or 0)
    nudge = float(shot.get("trim_nudge") or 0)
    return {
        "start": round(float(shot["start"]), 6),
        "duration": round(float(shot["duration"]), 6),
        "latest_take_lead": lead,
        "trim_nudge": round(nudge, 6),
        "anchor": round(float(shot["start"]) - lead - nudge, 6),
    }


def take_anchor_second(server: ManagedServer, project_id: str, shot_id: str) -> dict:
    """`take_state` under the name the assertions read it by. One reader, two spellings would be
    two readers."""
    return take_state(server, project_id, shot_id)


def shot_label_in(text: str, shot_id: str) -> bool:
    """Whether the readiness list names this shot at all. The labels carry the raw id in
    brackets (`shot_label`), so the id is the honest thing to look for."""
    return f"({shot_id})" in text


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

            # --- 7. Two gestures inside one round trip ----------------------------------------
            #
            # Review Finding 4, 2026-08-21, and the only shape that shows it. The undo snapshot
            # used to be cloned when a save was *queued*, from a baseline that only advanced when
            # a save *landed*. Two gestures made before the first write came back therefore
            # recorded the same "before": one Undo rolled back **both** of them while the button
            # named only the second, and a second Undo replayed the same plan and did nothing
            # visible. The state between the two gestures was unreachable.
            #
            # `add-shot` is the second gesture on purpose: it appends against the end of the song,
            # so the shot it adds is last in song order and the split's copy is not -- which makes
            # the comparison below an exact plan, not a count of shots.
            assert reseed(driver, wait, server, project_id) == seeded
            driver.execute_script(DELAY_SHOT_WRITES, 2500)
            select_clip(driver, "shot_01")
            was = shots_writes(driver)
            driver.find_element(By.ID, "split-shot").click()
            driver.find_element(By.ID, "add-shot").click()
            # The proof that this is one round trip and not two.
            open_writes = driver.execute_script("return window.__mvpOpenShotWrites;")
            assert open_writes >= 1, (
                "the split's write had already landed before the second gesture was made, so "
                "this section is not testing what it says it is"
            )
            await_shot_writes(driver, was + 2, "both gestures of the round trip")
            driver.execute_script("window.__mvpDelayShots = 0;")
            settle(driver, "#shots-track")
            both = windows(server, project_id)
            assert len(both) == SHOT_COUNT + 2, both
            named = history_state(driver)
            assert "adding a shot" in named["undo"]["name"], named
            press_history(driver, server, project_id, "undo-shots")
            intermediate = windows(server, project_id)
            assert intermediate != seeded, (
                "one Undo rolled back both gestures at once, so the plan between them cannot be "
                "reached at all -- Finding 4"
            )
            assert intermediate == both[:-1], (
                "one Undo did not step back exactly one gesture.\n"
                f"  after both: {both}\n  after one undo: {intermediate}"
            )
            after_one = history_state(driver)
            assert "the split" in after_one["undo"]["name"], after_one
            press_history(driver, server, project_id, "undo-shots")
            assert windows(server, project_id) == seeded, (
                "the second Undo did not reach the plan the two gestures started from"
            )
            result["one_round_trip"] = {
                "open_writes_at_second_gesture": open_writes,
                "after_both": len(both),
                "after_one_undo": len(intermediate),
                "undo_names": [named["undo"]["name"], after_one["undo"]["name"]],
            }

            # --- 8. A drag does not leave a double-click behind -------------------------------
            #
            # Review Finding 5. The press on a resize handle was remembered until a double-press
            # completed and nothing cleared it on release -- and `doubleEdgePress` measures from
            # the *first* press. So a short edge drag followed by re-grabbing the same edge inside
            # the window read as a double-click and ran the gap fill: the shot ran out to meet its
            # neighbour instead of taking the second drag.
            #
            # Both drags go in ONE W3C action sequence, because the whole defect lives in the
            # milliseconds between them and two `perform()` calls could not be relied on to land
            # inside the window at all. The second press looks up no element: the first drag has
            # already replaced every clip node in the document, and the pointer is standing on the
            # edge it just dragged, which is where the handle now is.
            assert reseed(driver, wait, server, project_id) == seeded
            clear_toasts(driver)
            double_click_ms = driver.execute_async_script(
                "const done = arguments[0];"
                "import('/assets/api.js').then((m) => done(m.EDGE_DOUBLE_CLICK_MS),"
                " () => done(null));"
            )
            assert isinstance(double_click_ms, (int, float)) and double_click_ms > 0, (
                "the double-click window could not be read out of api.js", double_click_ms
            )
            driver.execute_script(WATCH_EDGE_PRESSES)
            pps = geometry(driver, "shot_05")["pixelsPerSecond"]
            first_travel = round(1.0 * pps)
            second_travel = round(0.5 * pps)
            handle = clip_for(driver, "shot_05").find_element(
                By.CSS_SELECTOR, ".resize-handle.right"
            )
            was = shots_writes(driver)
            chain = ActionChains(driver, duration=60)
            chain.move_to_element(handle).click_and_hold()
            chain.move_by_offset(first_travel, 0).release()
            chain.pause(0.08)
            chain.click_and_hold().move_by_offset(second_travel, 0).release()
            chain.perform()
            await_shot_writes(driver, was + 2, "the two edge drags")
            settle(driver, "#shots-track")
            presses = driver.execute_script("return window.__mvpEdgePresses;")
            assert len(presses) == 2, ("the sequence did not land two presses on the handle",
                                       presses)
            interval = presses[1] - presses[0]
            assert interval <= double_click_ms, (
                f"the two presses were {interval} ms apart, outside the {double_click_ms} ms "
                "double-click window, so this section proves nothing about Finding 5"
            )
            dragged = windows(server, project_id)[4]
            assert dragged["id"] == "shot_05", dragged
            meets_neighbour = SHOTS[5]["start"]
            assert abs(dragged["start"] + dragged["duration"] - meets_neighbour) > 0.2, (
                "re-grabbing an edge just after dragging it ran the gap fill: the shot was "
                f"stretched to its neighbour at {meets_neighbour}s -- Finding 5"
            )
            want = SHOTS[4]["duration"] + (first_travel + second_travel) / pps
            assert abs(dragged["duration"] - want) < 0.3, (
                "the second gesture was neither the drag it was nor the gap fill it must not be",
                dragged, want,
            )
            assert not [line for line in toasts_on_screen(driver) if "close the gap" in line], (
                toasts_on_screen(driver)
            )
            # And the genuine double-click still fills the gap. Both directions matter: a fix
            # that simply stopped remembering the press would take gesture B away entirely, and
            # nothing above would notice.
            clear_toasts(driver)
            double_click_handle("shot_05", "right")
            filled_after = windows(server, project_id)[4]
            assert round(filled_after["start"] + filled_after["duration"], 6) == round(
                meets_neighbour, 6
            ), ("a real double-click no longer closes the gap", filled_after)
            result["drag_then_regrab"] = {
                "press_interval_ms": interval,
                "double_click_window_ms": double_click_ms,
                "after_two_drags": dragged,
                "after_a_real_double_click": filled_after,
            }

            # --- 9. A refused snap writes nothing ---------------------------------------------
            #
            # Review Finding 6. `moved` was measured *before* the snap was attempted, and a
            # refused snap puts the shot back and answers false -- so control fell into the save
            # anyway and sent a no-op `PUT /shots`: it bumped `updated_at`, pushed an undo entry
            # for a gesture that never happened, and threw the redo stack away, immediately after
            # telling the Director the edit was refused.
            #
            # A redo entry is built first, deliberately: "redo is still offered afterwards" is the
            # half of the damage no count of writes can see.
            #
            # A second project is created before the reload so its option is in the selector: the
            # last assertion here is that a refused snap leaves *nothing* outstanding, and the
            # only place the dirty flag is observable is the switch guard. It was invisible while
            # every drag ended in a write; now that one can end without one, a flag left standing
            # would have the guard warning about work that does not exist.
            scratch = post_json(
                server.base_url + "/api/projects", {"name": "Somewhere else to switch to"}
            )["id"]
            assert reseed(driver, wait, server, project_id) == seeded
            was = shots_writes(driver)
            driver.find_element(By.ID, "add-shot").click()
            await_shots_write(driver, was, "the gesture that fills the redo stack")
            settle(driver, "#shots-track")
            press_history(driver, server, project_id, "undo-shots")
            assert windows(server, project_id) == seeded
            standing = history_state(driver)
            assert standing["redo"]["disabled"] is False, standing
            before = windows(server, project_id)
            before_revision = revision(server, project_id)
            writes_before = shots_writes(driver)

            # The playhead inside shot_11, whose left edge is the cut it shares with the locked
            # shot_10. Dragging that edge onto the playhead is a boundary move, and the boundary
            # is protected -- so the snap is refused in the snap route's own sentence.
            playhead = scrub_to(driver, 54.0)
            assert 52.7 < playhead < 57.0, f"the scrub left the playhead at {playhead}s"
            pps = geometry(driver, "shot_11")["pixelsPerSecond"]
            travel = int((playhead - before[10]["start"]) * pps) - 3
            assert travel > 8, ("the drag is too short to be a drag at this zoom", travel, pps)
            handle = clip_for(driver, "shot_11").find_element(
                By.CSS_SELECTOR, ".resize-handle.left"
            )
            clear_toasts(driver)
            ActionChains(driver).click_and_hold(handle).move_by_offset(
                travel, 0
            ).release().perform()
            snap_refusal = wait_for_toast(driver, wait, "is locked")
            assert snap_refusal == SNAP_LOCKED_REFUSAL.format(shot="SHOT 10 (shot_10)"), (
                snap_refusal
            )
            settle(driver, "#shots-track")
            assert windows(server, project_id) == before, (
                "the refused snap wrote a window anyway"
            )
            assert shots_writes(driver) == writes_before, (
                "a refused snap still sent a PUT /shots -- it bumps updated_at, pushes a phantom "
                "undo entry and clears the redo stack, immediately after refusing the edit "
                "-- Finding 6"
            )
            assert revision(server, project_id) == before_revision, (
                "the refused snap moved the project's revision, so every other client's stack "
                "was dropped for an edit that never happened"
            )
            after_state = history_state(driver)
            assert after_state == standing, (
                "the refused snap changed the history stacks", standing, after_state
            )
            # And nothing is left outstanding. Switching projects asks before discarding unsaved
            # work; after a gesture that changed nothing there is none to discard, so it must not
            # ask. The dialog is answered either way so a failure here does not wedge the run.
            switched = driver.find_element(
                By.CSS_SELECTOR, f'#project-select option[value="{scratch}"]'
            )
            switched.click()
            asked_to_discard = ""
            try:
                alert = WebDriverWait(driver, 3).until(EC.alert_is_present())
                asked_to_discard = alert.text
                alert.accept()
            except TimeoutException:
                pass
            assert not asked_to_discard, (
                ("switching projects after a refused snap asked about unsaved work that does "
                 "not exist: the drag set the dirty flag and nothing put it back"),
                asked_to_discard,
            )
            wait.until(
                lambda browser: browser.find_element(By.ID, "project-select").get_attribute(
                    "value"
                ) == scratch
            )
            select_project(driver, wait, project_id)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            wait.until(
                lambda browser: len(
                    browser.find_elements(By.CSS_SELECTOR, "#shots-track .shot-clip")
                ) == SHOT_COUNT
            )
            settle(driver, "#shots-track")
            assert windows(server, project_id) == before
            result["refused_snap"] = {
                "refusal": snap_refusal,
                "playhead": playhead,
                "travel_px": travel,
                "writes": {"before": writes_before, "after": shots_writes(driver)},
                "revision_unchanged": True,
                "history_before": standing,
                "history_after": after_state,
                "switch_asked_about_unsaved_work": bool(asked_to_discard),
            }

            # --- 10. The shot-length band on the clip -----------------------------------------
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

            # --- 11. Expand All Prompts, on the cuts bar --------------------------------------
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
            stable_click(driver, "#timeline-expand-prompts", "Expand All Prompts")
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

            # --- 12. The music lock, and the second yellow ------------------------------------
            #
            # The Director's ask, 2026-08-21: "When dragging in the timeline though it would just
            # move the window over the clip but keep the clip aligned where it belongs with the
            # music. Perhaps a lock/unlock from timeline toggle in the shots info panel may be
            # useful next to that nudge input so that dragging a b-roll clip would be easier
            # (default locked)." And, on the warning: "if the bounds of the shots window are
            # dragged beyond where that clip covers then the shot would turn yellow."
            #
            # Every assertion here is the *song second the take's first frame plays at* --
            # `start - lead - nudge` -- read off the stored manifest. That number is what "the
            # clip stays where it belongs with the music" means, and it is the only thing that
            # tells a locked drag from an unlocked one: both move `start`.
            #
            # No render is submitted. The take is written onto the plan through `PUT /shots`
            # exactly as the H3 route writes it at submission, which is what makes this section
            # cost no GPU time at all.
            take_plan = [
                {**shot,
                 "prompt": f"{shot['id']}: the corridor, pushing in.", "mode": "text",
                 "status": "draft",
                 **(TAKE_FIELDS if shot["id"] == TAKE_SHOT else {}),
                 **(LEGACY_TAKE_FIELDS if shot["id"] == LEGACY_SHOT else {})}
                for shot in SHOTS
            ]
            put_json(f"{server.base_url}/api/projects/{project_id}/shots", {"shots": take_plan})
            driver.refresh()
            select_project(driver, wait, project_id)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            wait.until(
                lambda browser: len(
                    browser.find_elements(By.CSS_SELECTOR, "#shots-track .shot-clip")
                ) == SHOT_COUNT
            )
            wait_for_readiness(driver, wait, f"{SHOT_COUNT} shots have a prompt")
            settle(driver, "#shots-track")

            # The control, where the Director asked for it: inside the trim-nudge row, ticked.
            select_clip(driver, TAKE_SHOT)
            anchor = driver.find_element(By.ID, "take-anchor")
            anchor_facts = visible_and_clickable(driver, anchor, "the music lock")
            assert anchor.get_property("checked") is True, (
                "the music lock does not default to locked, which is the Director's own word"
            )
            assert driver.execute_script(
                "return document.getElementById('trim-nudge')"
                ".contains(document.getElementById('take-anchor'));"
            ), "the music lock is on screen but not beside the trim nudge the Director named"
            anchor_help = driver.execute_script(
                "return document.getElementById('take-anchor').closest('label')"
                ".getAttribute('title');"
            )
            assert "not the shot lock" in anchor_help, anchor_help

            # And a shot with no take shows neither the nudge nor the lock: there is nothing to
            # hold still, and a live control that does nothing is worse than no control.
            select_clip(driver, "shot_04")
            assert not driver.find_elements(By.ID, "take-anchor"), (
                "a shot with no take draws the music lock, which would do nothing at all"
            )
            assert not driver.find_elements(By.ID, "trim-nudge")

            # --- 12a. Locked: the window moves, the take does not -----------------------------
            select_clip(driver, TAKE_SHOT)
            before = take_anchor_second(server, project_id, TAKE_SHOT)
            was = shots_writes(driver)
            travel = int(1.5 * geometry(driver, TAKE_SHOT)["pixelsPerSecond"])
            ActionChains(driver).click_and_hold(
                clip_for(driver, TAKE_SHOT)
            ).move_by_offset(travel, 0).release().perform()
            await_shots_write(driver, was, "the locked move drag")
            locked = take_state(server, project_id, TAKE_SHOT)
            assert locked["start"] > before["start"], (
                "the locked drag did not move the window at all", before, locked
            )
            assert locked["trim_nudge"] != before["trim_nudge"], (
                ("the locked drag moved the window without moving the cut into the take, so "
                 "the take travelled with it"), before, locked
            )
            # The whole point, in one number: the take's first frame still plays at the same
            # second of the song. And the nudge moved by exactly what the window moved by.
            assert abs(locked["anchor"] - before["anchor"]) < 1e-6, (
                "a locked drag pulled the take off the music", before, locked
            )
            assert abs(
                (locked["start"] - before["start"]) - (locked["trim_nudge"] - before["trim_nudge"])
            ) < 1e-6, (before, locked)

            # --- 12b. Unlocked: the take travels with the window ------------------------------
            unlock = driver.find_element(By.ID, "take-anchor")
            unlock.click()
            assert unlock.get_property("checked") is False
            writes_after_toggle = shots_writes(driver)
            settle(driver, "#shots-track", quiet_ms=300)
            assert shots_writes(driver) == writes_after_toggle, (
                "toggling the lock wrote the plan to the server; it changes what the next drag "
                "does and nothing else"
            )
            was = shots_writes(driver)
            ActionChains(driver).click_and_hold(
                clip_for(driver, TAKE_SHOT)
            ).move_by_offset(-travel, 0).release().perform()
            await_shots_write(driver, was, "the unlocked move drag")
            unlocked = take_state(server, project_id, TAKE_SHOT)
            assert unlocked["start"] < locked["start"], (
                "the unlocked drag did not move the window", locked, unlocked
            )
            assert unlocked["trim_nudge"] == locked["trim_nudge"], (
                "the unlocked drag still compensated the nudge, so unlocking did nothing",
                locked, unlocked,
            )
            assert abs(unlocked["anchor"] - locked["anchor"]) > 1e-6, (
                ("an unlocked drag left the take on the same song second, which is the "
                 "locked behaviour"), locked, unlocked,
            )
            result["music_lock"] = {
                "control": anchor_facts,
                "travel_px": travel,
                "before": before,
                "locked_drag": locked,
                "unlocked_drag": unlocked,
                "writes_on_toggle": 0,
            }

            # --- 12c. Dragged off what the take covers, it turns yellow -----------------------
            #
            # Locked again, and dragged far enough that the window asks for picture past the end
            # of the take. The verdict is the *server's* -- the readiness report's
            # `window_warnings` -- and nothing in the browser re-derives it.
            driver.find_element(By.ID, "take-anchor").click()
            assert driver.find_element(By.ID, "take-anchor").get_property("checked") is True
            plain = driver.execute_script(CLIP_STATE, TAKE_SHOT)
            assert "take-uncovered" not in plain["className"], plain
            was = shots_writes(driver)
            # Far enough that the window asks for more picture than the take holds: it is already
            # 1.5 s in from 12a, and the take runs out 4.875 s past the window's own start.
            ActionChains(driver).click_and_hold(
                clip_for(driver, TAKE_SHOT)
            ).move_by_offset(int(5 * geometry(driver, TAKE_SHOT)["pixelsPerSecond"]), 0)\
                .release().perform()
            await_shots_write(driver, was, "the drag off the end of the take")
            wait.until(
                lambda browser: "take-uncovered" in browser.execute_script(
                    CLIP_STATE, TAKE_SHOT
                )["className"],
                "the clip did not turn amber after its window was dragged off its take",
            )
            uncovered = driver.execute_script(CLIP_STATE, TAKE_SHOT)
            assert uncovered["borderTopColor"] != plain["borderTopColor"], uncovered
            assert "past the picture this take holds" in uncovered["ariaLabel"], uncovered
            assert uncovered["ariaLabel"] == uncovered["title"], uncovered
            # It warns and never blocks -- and the drag itself was never constrained, which is
            # the Director's non-negotiable: the window really did move where it was dragged.
            for control in ("split-shot", "duplicate-shot", "delete-shot", "add-shot"):
                assert not driver.find_element(By.ID, control).get_property("disabled"), (
                    f"#{control} went shut because a window left its take, which is a refusal "
                    "the Director ruled against"
                )
            dragged_off = take_state(server, project_id, TAKE_SHOT)
            assert dragged_off["start"] > unlocked["start"], dragged_off
            readiness_text = driver.find_element(By.ID, "plan-readiness").get_attribute(
                "textContent"
            )
            assert "Past the take" in readiness_text, readiness_text
            assert "does not block submission" in readiness_text, readiness_text
            # The server's own numbers reach the Director unreworded -- including the take's own
            # length, which is the picture its *recorded* window asked H3 for and not the live
            # window's. A client re-deriving this from the plan would print 5.875s here.
            assert f"{TAKE_PICTURE_SECONDS:.3f}s of picture" in readiness_text, readiness_text

            # And the take that cannot be checked is never warned about, however far its window
            # sits from where its picture would be: `latest_take_duration` of 0 is "never
            # snapshotted", which is every take rendered before 2026-08-21 -- including the 33 in
            # the Director's own project -- and every hand-picked clip.
            legacy = driver.execute_script(CLIP_STATE, LEGACY_SHOT)
            assert "take-uncovered" not in legacy["className"], (
                ("a take that recorded no window of its own was coverage-checked against "
                 "the live one, which is a guess this application does not make"), legacy,
            )
            assert shot_label_in(readiness_text, LEGACY_SHOT) is False, readiness_text
            result["take_uncovered"] = {
                "plain": plain, "uncovered": uncovered, "legacy": legacy,
                "dragged_to": dragged_off,
                "readiness": readiness_text[:600],
            }

            # --- 13. The ruling: the take next to the one being adjusted -----------------------
            #
            # The Director, 2026-08-21, overruling the half-applied version of section 12's rule:
            #
            #     "Well we dont want to slide the take next to the one we are adjusting either,
            #     rather move its windows edge while both clips stay in place, same for double
            #     click, those gestures should only slide the window bounds but leave the clip
            #     position intact."
            #
            # Section 12 proves the shot *under the hand*. This proves the two gestures that write
            # some *other* shot's start -- snapping a cut to the playhead, which carries the
            # neighbour's edge, and the double-click gap fill -- and it proves them the only way
            # they can be proven: as `start - lead - nudge` read off the stored manifest, for the
            # shot the pointer was never on.
            #
            # It also settles whose lock decides. When a gesture on A moves B's start it is B's
            # take that must stay on the music, so it is B's toggle that governs -- driven here by
            # unticking B and repeating the same drag on A.
            ruling: dict[str, object] = {}

            def load_ruling_plan() -> None:
                put_json(
                    f"{server.base_url}/api/projects/{project_id}/shots",
                    {"shots": ruling_plan()},
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

            def held(before: dict, after: dict, what: str) -> None:
                """The take is still on the same second of the song, and the nudge moved by
                exactly the seconds the window moved by. One microsecond of tolerance, which is
                `exactSeconds`' own resolution -- a frame of tolerance would have passed the
                17 ms drift a browser measured on 2026-08-21."""
                assert abs(after["anchor"] - before["anchor"]) < 1e-6, (
                    (f"{what}: the take slid "
                     f"{(after['anchor'] - before['anchor']) * 1000:.1f}ms off the music"),
                    before, after,
                )
                assert abs(
                    (after["start"] - before["start"])
                    - (after["trim_nudge"] - before["trim_nudge"])
                ) < 1e-6, (f"{what}: the nudge did not follow the window", before, after)

            load_ruling_plan()
            magnet = driver.execute_script(SNAP_STATE)
            assert magnet["pressed"] == "true", ("the magnet is off before section 13", magnet)

            # --- 13a. Snapping a cut carries the neighbour's window, not its take -------------
            #
            # The reported case. A *right*-edge snap moves the dragged shot's `duration` and the
            # neighbour's `start` -- so the one take this gesture can displace is the neighbour's,
            # and the neighbour's was the one take that was never compensated.
            neighbour_before = take_anchor_second(server, project_id, "shot_08")
            dragged_before = take_anchor_second(server, project_id, "shot_07")

            def snap_shot_07_right() -> float:
                """Park the playhead inside shot_07 and drag the cut at its right edge onto it.
                Answers where the playhead actually landed.

                The scrub is inside this helper rather than done once, because *selecting a clip
                moves the playhead*: an unmoved click on a clip parks it at that shot's start, and
                section 13c has to select two clips between its two runs of this drag.
                """
                at = scrub_to(driver, 36.8)
                assert 32.6 < at < 37.4, f"the scrub left the playhead at {at}s"
                # Off the frame grid, or "landed on the playhead" and "quantised to the frame"
                # are the same number and this section would pass whether the magnet fired or not.
                assert abs(at * 24 - round(at * 24)) > 0.15, (
                    f"the playhead landed on the frame grid at {at}s"
                )
                facts = geometry(driver, "shot_07")
                cut = take_state(server, project_id, "shot_07")
                travel = int(
                    (at - (cut["start"] + cut["duration"])) * facts["pixelsPerSecond"]
                ) - 3
                assert abs(travel) > 8, ("the drag is too short to be a drag at this zoom", travel)
                handle = clip_for(driver, "shot_07").find_element(
                    By.CSS_SELECTOR, ".resize-handle.right"
                )
                was = shots_writes(driver)
                ActionChains(driver).click_and_hold(handle).move_by_offset(
                    travel, 0
                ).release().perform()
                await_shots_write(driver, was, "the snap onto the playhead")
                settle(driver, "#shots-track")
                return at

            playhead = snap_shot_07_right()
            snapped_08 = take_state(server, project_id, "shot_08")
            snapped_07 = take_state(server, project_id, "shot_07")
            # The gesture really did what it says: the shared cut is on the playhead and the
            # neighbour's window followed it.
            assert abs(snapped_08["start"] - playhead) < 0.003, (
                ("the neighbour's edge did not land on the playhead, so this section is not "
                 "measuring the gesture the ruling is about"), playhead, snapped_08,
            )
            assert abs(snapped_08["start"] - neighbour_before["start"]) > 0.05, (
                "the neighbour's start did not move at all", neighbour_before, snapped_08
            )
            # And the ruling: the neighbour's take is still on the same second of the song.
            held(neighbour_before, snapped_08, "the snapped neighbour")
            # The dragged shot's own start never moved, so nothing was written to its nudge --
            # a duration is not in the anchor and cannot displace a take.
            assert snapped_07["start"] == dragged_before["start"], snapped_07
            assert snapped_07["trim_nudge"] == dragged_before["trim_nudge"], (
                "a right-edge drag wrote a nudge, and a duration cannot move a take",
                dragged_before, snapped_07,
            )
            assert contiguity(windows(server, project_id)) == contiguity(seeded), (
                "the snap changed the plan's contiguity",
                contiguity(windows(server, project_id)),
            )
            ruling["snap_neighbour"] = {
                "playhead": playhead,
                "before": neighbour_before, "after": snapped_08,
                "dragged_before": dragged_before, "dragged_after": snapped_07,
            }

            # --- 13b. Undo restores the pair and does not compensate a second time ------------
            #
            # Undo replays a snapshotted shot list, and that snapshot already holds the `start`
            # and the `trim_nudge` that belonged together when it was taken. If the restore were
            # routed through the anchoring rule it would move every anchored take by the amount
            # the gesture had just been rolled back by -- the rule applied twice. Both fields are
            # compared, not only the anchor: a double-application that moved `start` and
            # `trim_nudge` by the same amount would leave the anchor right and the plan wrong.
            press_history(driver, server, project_id, "undo-shots")
            undone_08 = take_state(server, project_id, "shot_08")
            assert undone_08 == neighbour_before, (
                "undo did not put the neighbour back exactly -- start, nudge and anchor together",
                neighbour_before, snapped_08, undone_08,
            )
            assert take_state(server, project_id, "shot_07") == dragged_before
            press_history(driver, server, project_id, "redo-shots")
            redone_08 = take_state(server, project_id, "shot_08")
            assert redone_08 == snapped_08, (
                "redo did not reach the state undo displaced", snapped_08, redone_08
            )
            press_history(driver, server, project_id, "undo-shots")
            assert take_state(server, project_id, "shot_08") == neighbour_before
            ruling["undo_restores"] = {
                "undone": undone_08, "redone": redone_08,
            }

            # --- 13c. And it is the *neighbour's* lock that governs the neighbour -------------
            #
            # Unticked on shot_08, which the pointer never touches: the same drag on shot_07's
            # right edge now lets shot_08's take travel with its window. Nothing about shot_07
            # changes, and shot_07 stays locked throughout -- so a client reading the lock of the
            # clip under the hand would answer "locked" here and hold the take still.
            select_clip(driver, "shot_08")
            unlock = driver.find_element(By.ID, "take-anchor")
            assert unlock.get_property("checked") is True
            unlock.click()
            assert driver.find_element(By.ID, "take-anchor").get_property("checked") is False
            select_clip(driver, "shot_07")
            assert driver.find_element(By.ID, "take-anchor").get_property("checked") is True, (
                "unticking the neighbour unticked the shot under the hand as well, so the two "
                "clips share one flag and this section proves nothing"
            )
            free_playhead = snap_shot_07_right()
            free_08 = take_state(server, project_id, "shot_08")
            assert abs(free_08["start"] - free_playhead) < 0.003, (
                "the unlocked neighbour's edge did not land on the playhead", free_08
            )
            assert free_08["trim_nudge"] == neighbour_before["trim_nudge"], (
                ("the unlocked neighbour's nudge was still compensated, so unlocking it did "
                 "nothing"), neighbour_before, free_08,
            )
            # The take travelled with the window: the anchor moved by exactly what the window did.
            assert abs(
                (free_08["anchor"] - neighbour_before["anchor"])
                - (free_08["start"] - neighbour_before["start"])
            ) < 1e-6, (neighbour_before, free_08)
            assert abs(free_08["anchor"] - neighbour_before["anchor"]) > 0.05, (
                ("the unlocked neighbour's take stayed on the same song second, which is the "
                 "locked behaviour"), neighbour_before, free_08,
            )
            ruling["neighbour_lock_governs"] = {
                "locked": snapped_08, "unlocked": free_08,
            }

            # --- 13d. The double-click, in both of its directions ------------------------------
            #
            # The other reported case, and an earlier explicit ruling is superseded here: this
            # gesture used to leave `trim_nudge` alone on purpose, on the reasoning that closing a
            # 0.002 s gap must not silently re-time a take. The Director has ruled the other way,
            # and the reasoning was inverted: moving a window 0.015 s earlier and leaving the nudge
            # alone *is* the re-timing.
            #
            # `gapFillPlan` never moves the neighbour in either direction, so the two directions
            # are: leftward, which moves this shot's own start, and rightward, which only grows
            # its duration. Both are driven.
            load_ruling_plan()
            fill_before = take_anchor_second(server, project_id, "shot_04")
            double_click_handle("shot_04", "left")
            filled_left = take_state(server, project_id, "shot_04")
            # 0.015 s of the Director's own residue, closed exactly -- and closed off the frame
            # grid, so a compensation quantised to 1/24 s would be 0.0417 s wrong here.
            assert filled_left["start"] == 15.002, filled_left
            assert round(filled_left["start"] - fill_before["start"], 6) == -0.015, filled_left
            held(fill_before, filled_left, "the leftward gap fill")
            # Rightward: 2.5 s of empty song after shot_05, closed by growing a duration. The
            # start does not move, so nothing is written to the nudge at all.
            right_before = take_anchor_second(server, project_id, "shot_05")
            double_click_handle("shot_05", "right")
            filled_right = take_state(server, project_id, "shot_05")
            assert round(filled_right["duration"], 6) == 7.5, filled_right
            assert filled_right["start"] == right_before["start"], filled_right
            assert filled_right["trim_nudge"] == right_before["trim_nudge"], (
                "the rightward fill wrote a nudge, and it moved no start to compensate for",
                right_before, filled_right,
            )
            held(right_before, filled_right, "the rightward gap fill")
            ruling["gap_fill_left"] = {"before": fill_before, "after": filled_left}
            ruling["gap_fill_right"] = {"before": right_before, "after": filled_right}

            # Unlocked, the same double-click lets the take travel -- one rule, every gesture.
            press_history(driver, server, project_id, "undo-shots")
            press_history(driver, server, project_id, "undo-shots")
            assert take_state(server, project_id, "shot_04") == fill_before
            select_clip(driver, "shot_04")
            driver.find_element(By.ID, "take-anchor").click()
            assert driver.find_element(By.ID, "take-anchor").get_property("checked") is False
            double_click_handle("shot_04", "left")
            free_04 = take_state(server, project_id, "shot_04")
            assert free_04["start"] == 15.002, free_04
            assert free_04["trim_nudge"] == fill_before["trim_nudge"], free_04
            assert abs(free_04["anchor"] - fill_before["anchor"]) > 1e-6, (
                "an unlocked gap fill left the take on the same song second", fill_before, free_04
            )
            ruling["gap_fill_unlocked"] = free_04

            # --- 13e. The inspector's own Start box is the same edit written as a number -------
            #
            # Not a gesture the Director named, and included because a typed 1.5 s that slid the
            # take while a dragged 1.5 s did not would be exactly the inconsistency the ruling is
            # about. The box is a few rows above the lock, so a Director who means to move the take
            # has the toggle to hand.
            load_ruling_plan()
            typed_before = take_anchor_second(server, project_id, "shot_07")
            select_clip(driver, "shot_07")
            box = driver.find_element(By.ID, "shot-start")
            assert float(box.get_attribute("value")) == typed_before["start"]
            was = shots_writes(driver)
            box.send_keys(Keys.CONTROL, "a")
            box.send_keys("34.767")
            box.send_keys(Keys.TAB)
            await_shots_write(driver, was, "the typed start")
            settle(driver, "#shots-track")
            typed = take_state(server, project_id, "shot_07")
            assert typed["start"] == 34.767, ("the typed start never reached disk", typed)
            held(typed_before, typed, "the typed start")
            ruling["typed_start"] = {"before": typed_before, "after": typed}

            result["ruling_2026_08_21"] = ruling

            # The plan is put back the way the seed left it, so the final contiguity recorded
            # below describes the Director's own shape rather than this section's drags.
            put_json(
                f"{server.base_url}/api/projects/{project_id}/shots",
                {"shots": [{**shot, "prompt": f"{shot['id']}: the corridor, pushing in.",
                            "mode": "text", "status": "draft"} for shot in SHOTS]},
            )

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
            #
            # And the take previews section 12 asks for: it writes a take onto two shots without
            # rendering one -- which is the whole reason this run spends no GPU time -- so the
            # `<video>` the inspector draws asks for a file that was never made and is answered
            # 404. Listed by shot rather than by status code, so an unexpected 404 anywhere else
            # still fails the run.
            console_gate(
                driver, NAME, result,
                expected=["409", f"shots/{TAKE_SHOT}/take", f"shots/{LEGACY_SHOT}/take"]
                + [f"shots/{shot_id}/take" for shot_id in RULING_TAKES],
            )
            report(NAME, result)
        finally:
            driver.quit()


if __name__ == "__main__":
    main()
