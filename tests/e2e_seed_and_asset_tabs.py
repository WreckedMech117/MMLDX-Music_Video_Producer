"""Browser QA for the seed randomizer and the Assets panel's subtabs.

Two Director requests, 2026-08-20, both of them interaction changes in the same file:

    "in the Shots info panel i can see the 'Seed' section, we should shorten that box a bit and add
    a randomize toggle (1-99999) which would RNG a number and hold it unless regenerate gets hit
    later with randomize still checked."

    "in Assets the generated clips are eating up all the room and hiding the sorted asset sections
    and should be their own subtab along with CHaracters/Settings/Props/Style."

Driven in a real browser because both claims are about what is on the screen and what a gesture
does to the *stored* project, and this repository has a recorded incident where substring
assertions over `app.js` let three UI guarantees invert with a green suite -- plus one found on
2026-08-21 where a `dblclick` listener never fired because `pointerdown` re-rendered every clip.
The offline harness covers the deciding functions; what it structurally cannot see is a pane that
is `hidden` and painted anyway, a tab strip that loses the button under the pointer when it
redraws, or a seed that moved twice.

Every seed assertion reads the seed back **from the server**, never from the input on screen: the
input is what the panel drew, and the manifest is what a render will use.

What is asserted, in order:

**The seed control**

1. **The box is shorter than it was, and the toggle is beside it.** Measured: the seed input is
   materially narrower than the panel and than the intent box above it, both controls sit on one
   line, and each is hit-testable at its centre.
2. **The label names the re-roll moment.** "Randomize on Render again" is on the screen, not only
   in a tooltip, so the moment does not have to be guessed.
3. **Ticking rolls once, inside 1-99999, and stores it.**
4. **It holds, and it is armed on one shot.** Selecting another shot and coming back -- which
   tears the inspector down and rebuilds it, dragging a readiness reply behind it -- does not roll
   again, and the *other* shot's box is off. Then a Render again on that unarmed shot takes the
   server's `RESUBMIT_SEED_STRIDE` rather than a fresh roll. This inverts the assertion that was
   here on 2026-08-20, on the Director's ruling of the 21st: *"Clicking randomize integer on a
   shot toggles it for all shots, it should be per shot."*
5. **A hand-typed seed clears the toggle**, and the typed number stands.
6. **Render again re-rolls, and by exactly one rule.** Ticked, the queued retake's stored seed is a
   fresh number in 1-99999 and specifically *not* `previous + RESUBMIT_SEED_STRIDE`; unticked, it
   is exactly `previous + RESUBMIT_SEED_STRIDE`. The two sources of seed movement cannot both fire.
7. **Mark ready does not re-roll**, with the toggle still ticked. It queues nothing, so a seed that
   moved there would break a fixed-seed comparison for no render.

**The Assets subtabs**

8. **Seven tabs, all reachable**, at three window widths across both of `styles.css`'s breakpoints.
9. **One pane at a time.** On every asset tab the clips library paints zero height and the asset
   grid reaches the bottom of the library panel -- which is the Director's report, measured. On the
   Clips tab it is the other way round.
10. **The tabs show and hide the right assets**, and an empty tab says so in its own words.
11. **The clips library is unchanged by the move**: rows, hover-to-play, and "Open shot" still
    lands on the producing shot in the timeline. With ComfyUI at a dead port, each shot's current
    take still plays -- this application serves it from ComfyUI's output directory on disk -- and
    the one superseded take says why it cannot be shown instead of drawing a 404'd `<video>`.
12. **A tab change loses no selection.** The shot selected on the timeline and the asset selected
    in the grid both survive it, so "Attach to selected shot" stays live on every tab.

**No GPU is spent and nothing reaches a real ComfyUI.** `MVP_COMFY_URL` is pointed at a dead port
this run owns, so the one submission this script drives (step 6's Render again) cannot reach the
user's installation; the seed lands on the manifest before that call is made, which is the thing
under test. `MVP_COMFY_ROOT` is an empty temp tree. The LLM eject is switched off. Nothing here
starts, stops or interrupts ComfyUI.

Run from the repo root -- it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_seed_and_asset_tabs.py [--port 8774]

Assumes: nothing listening on the port (it refuses to reuse a bound one), Microsoft Edge and its
WebDriver installed, and `music_video_producer` importable from this checkout's `src/`. ComfyUI does
not need to be running, and is never contacted.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import get_args

from e2e_support import (
    ManagedServer,
    artifact_dir,
    clipped,
    console_gate,
    edge_driver,
    get_json,
    post_json,
    put_json,
    reachable_widths,
    report,
    settle,
    visible_and_clickable,
)
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from music_video_producer.app import JOB_NEVER_SUBMITTED, RESUBMIT_SEED_STRIDE
from music_video_producer.models import AssetKind

NAME = "seed-and-asset-tabs"

SHOT = "shot_01"
OTHER = "shot_02"
START_SEED = 12

#: The tab strip, exactly as the Director should read it. `prop` is deliberately absent from the
#: seeded assets so one tab is genuinely empty and its message can be asserted.
EXPECTED_TABS = ["All", "Characters", "Settings", "Props", "Style", "Media", "Clips"]

#: One asset per kind except `prop`. The three kinds the Director did not name a tab for --
#: `image`, `audio`, `video` -- are here on purpose: an asset under no tab is invisible and
#: undeletable from this screen, and this is what proves they are reachable.
SEEDED_ASSETS = [
    ("asset_lucy", "character", "Lucy the singer"),
    ("asset_corridor", "setting", "Service corridor"),
    ("asset_grain", "style", "Grain plate"),
    ("asset_still", "image", "Uploaded still"),
    ("asset_tone", "audio", "Room tone"),
    ("asset_plate", "video", "Stock plate"),
]

#: The console entries this run causes on purpose. The H3 submission in step 6 fails because
#: `MVP_COMFY_URL` is a dead port this run chose, deliberately: what is under test is the seed that
#: was written before it. Declared by fragment rather than filtered away, so the gate stays a gate
#: and anything unlisted still fails the run.
#:
#: `/take?v=` is the second: the fixture's shot points at a take filename that has no file behind
#: it, because a real one would have to come off a GPU. The Monitor loads it and gets a 404, and
#: since 2026-08-21 so does the Clips tab's card for that take -- the current take of a shot is
#: served by this application from ComfyUI's output directory on disk, and this fixture puts
#: nothing there. That is a property of the fixture, not of anything under test here.
#:
#: **`/view` is deliberately gone from this list.** Before 2026-08-21 every clip card was a
#: `<video>` pointed at ComfyUI's own `/view`, so all three 404'd on every run of this script --
#: which is the Director's report, sitting in the expected-errors list of the harness that was
#: meant to catch it. A card that cannot be shown now says so instead of asking.
EXPECTED_CONSOLE = ["generate/h3", "/take?v="]

#: The seed row, as the browser has it: both controls, their geometry, and what the toggle says.
SEED_ROW = """
const box = document.querySelector('#shot-seed');
const toggle = document.querySelector('#shot-seed-randomize');
const label = toggle ? toggle.closest('label') : null;
const intent = document.querySelector('#shot-prompt');
const panel = document.querySelector('#shot-inspector');
const rect = (node) => {
  if (!node) return null;
  const r = node.getBoundingClientRect();
  return {width: Math.round(r.width), height: Math.round(r.height),
          top: Math.round(r.top), left: Math.round(r.left)};
};
return {
  box: rect(box),
  toggle: rect(toggle),
  label: rect(label),
  intent: rect(intent),
  panel: rect(panel),
  value: box ? box.value : null,
  checked: toggle ? toggle.checked === true : null,
  labelText: label ? label.textContent.trim() : '',
  labelTitle: label ? (label.getAttribute('title') || '') : '',
};
"""

#: Which pane of the library the panel is actually painting, and how much room each one takes.
#: The Director's report is a *geometry* claim -- the clips were "eating up all the room" -- so it
#: is measured rather than inferred from an attribute.
LIBRARY_PANES = """
const grid = document.querySelector('#asset-grid');
const clips = document.querySelector('#clips-library');
const panel = document.querySelector('.library-panel');
const toolbar = document.querySelector('.library-toolbar');
const box = (node) => {
  const r = node.getBoundingClientRect();
  return {width: Math.round(r.width), height: Math.round(r.height),
          top: Math.round(r.top), bottom: Math.round(r.bottom)};
};
return {
  grid: box(grid),
  clips: box(clips),
  panel: box(panel),
  toolbar: box(toolbar),
  gridHiddenAttr: grid.hasAttribute('hidden'),
  clipsHiddenAttr: clips.hasAttribute('hidden'),
  gridDisplay: getComputedStyle(grid).display,
  clipsDisplay: getComputedStyle(clips).display,
  cards: [...grid.querySelectorAll('.asset-card strong')].map((n) => n.textContent),
  emptyTitle: (grid.querySelector('.library-empty strong') || {}).textContent || '',
  emptyHint: (grid.querySelector('.library-empty span') || {}).textContent || '',
  clipCards: clips.querySelectorAll('.clip-card').length,
  clipVideos: clips.querySelectorAll('.clip-card video').length,
  // Since 2026-08-21 a card whose take cannot be shown says so instead of drawing a broken
  // `<video>`, so the two counts together are what "every take is accounted for" means.
  clipUnplayable: clips.querySelectorAll('.clip-unplayable').length,
  clipJumps: clips.querySelectorAll('.clip-jump').length,
  clipsHeading: (clips.querySelector('.clips-heading') || {}).textContent || '',
  clipsEmpty: (clips.querySelector('.library-empty strong') || {}).textContent || '',
};
"""

TAB_STRIP = """
return [...document.querySelectorAll('#asset-filters button[data-filter]')].map((b) => ({
  id: b.dataset.filter,
  label: b.textContent.trim(),
  active: b.classList.contains('active'),
  selected: b.getAttribute('aria-selected'),
  title: b.getAttribute('title') || '',
  role: b.getAttribute('role'),
  width: Math.round(b.getBoundingClientRect().width),
}));
"""


def dead_port() -> int:
    """A port nothing is listening on, so a submission cannot reach the user's ComfyUI.

    Bound and released, which leaves it free at the moment it is handed over. That is a race in
    principle and irrelevant in practice: what matters is that the address is *not* 8188.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def seed_project(base_url: str) -> str:
    """One shot with a settled take, one without, six assets, three completed H3 jobs.

    The assets and jobs go in through the whole-project PUT: there is no route that mints a
    `RenderJob` without spending a GPU pass, and the uploads would need real files on disk for a
    fixture whose only job is to be counted and filtered.
    """
    project = post_json(f"{base_url}/api/projects", {"name": "Seed and asset tabs browser QA"})
    project_id = project["id"]
    takes = [
        f"music-video-producer/{project_id}/shots/{SHOT}-h3_00001-audio.mp4",
        f"music-video-producer/{project_id}/shots/{SHOT}-h3_00002-audio.mp4",
        f"music-video-producer/{project_id}/shots/{OTHER}-h3_00001-audio.mp4",
    ]
    put_json(f"{base_url}/api/projects/{project_id}/shots", {"shots": [
        {"id": SHOT, "start": 0, "duration": 4, "prompt": "The corridor, pushing in on Lucy.",
         "mode": "text", "status": "complete", "seed": START_SEED, "latest_output": takes[1]},
        # Settled, with its own take, since 2026-08-21: the per-shot randomizer ruling needs a
        # *second* shot that Render again is actually offered for, so that "armed on A, striding
        # on B" can be driven rather than argued.
        {"id": OTHER, "start": 4, "duration": 4, "prompt": "The same corridor, wider.",
         "mode": "text", "status": "complete", "seed": 3, "latest_output": takes[2]},
    ]})
    project = get_json(f"{base_url}/api/projects/{project_id}")
    project["assets"] = [
        {"id": asset_id, "name": name, "kind": kind, "source": "upload",
         "path": "", "prompt": "", "prompt_id": ""}
        for asset_id, kind, name in SEEDED_ASSETS
    ]
    project["jobs"] = [
        {"id": f"job_{index}", "kind": "h3", "status": "complete", "prompt_id": f"p-{index}",
         "target_id": SHOT if index < 3 else OTHER, "seed": index,
         "output_files": [take],
         "created_at": "2026-08-20T09:15:00Z", "updated_at": "2026-08-20T09:21:00Z"}
        for index, take in enumerate(takes, start=1)
    ]
    put_json(f"{base_url}/api/projects/{project_id}", project)
    return project_id


def stored_seed(base_url: str, project_id: str, shot_id: str = SHOT) -> int:
    project = get_json(f"{base_url}/api/projects/{project_id}")
    return int(next(shot["seed"] for shot in project["shots"] if shot["id"] == shot_id))


def wait_for_seed_change(
    base_url: str, project_id: str, was: int, timeout: float = 12.0, shot_id: str = SHOT
) -> int:
    """The stored seed once it stops being `was` -- or whatever it still is at the timeout.

    Polls the *effect* rather than waiting for the panel to go quiet: `settle` returns as soon as
    nothing has mutated for its window, which a click whose PUT is still in flight satisfies
    trivially. Returning rather than raising keeps the caller's own sentence as the failure message.

    `shot_id` is not decoration. It defaulted to `SHOT` and was read that way by every caller until
    the per-shot randomizer arrived on 2026-08-21; a caller that had just requeued the *other*
    shot then compared the wrong shot's number and reported a stride as a random roll.
    """
    deadline = time.monotonic() + timeout
    seen = stored_seed(base_url, project_id, shot_id)
    while seen == was and time.monotonic() < deadline:
        time.sleep(0.15)
        seen = stored_seed(base_url, project_id, shot_id)
    return seen


def wait_for_seed_value(base_url: str, project_id: str, wanted: int, timeout: float = 12.0) -> int:
    """The stored seed once it *is* `wanted` -- or whatever it still is at the timeout.

    Used where the box passes through an intermediate value on its way to the one being asserted,
    so "it changed" is not a strong enough wait.
    """
    deadline = time.monotonic() + timeout
    seen = stored_seed(base_url, project_id)
    while seen != wanted and time.monotonic() < deadline:
        time.sleep(0.15)
        seen = stored_seed(base_url, project_id)
    return seen


def seed_held(base_url: str, project_id: str, expected: int, seconds: float = 2.5) -> int:
    """Assert a negative honestly: watch the stored seed for a while and return what it became."""
    deadline = time.monotonic() + seconds
    seen = expected
    while time.monotonic() < deadline:
        seen = stored_seed(base_url, project_id)
        if seen != expected:
            return seen
        time.sleep(0.2)
    return seen


def wait_for_refused_submissions(
    base_url: str, project_id: str, wanted: int, timeout: float = 25.0
) -> list[dict]:
    """The settled records of the submissions this run's dead ComfyUI turned away.

    **A Render again is not over when the seed lands.** The gesture re-opens the shot, writes the
    plan, and *then* submits -- and `MVP_COMFY_URL` is a dead port this run chose, so that POST
    spends a couple of seconds failing before `generate_h3` settles the record it had already
    written as never submitted. `wait_for_seed_change` returns one request earlier than any of
    that, so a script that used it as the end of the gesture went on typing into an inspector
    whose tab was mid-gesture.

    That matters because the record is written *before* the graph is submitted (the Director's
    2026-08-21 ruling), so the manifest's revision has already moved by the time the submission
    fails -- and a shot edit made in that window is refused with `PROJECT_CHANGED_REFUSAL` and
    lost. Section 5 is about a typed number, not about that race, so the gesture is waited out
    here by its own evidence rather than by a sleep.

    Returns the settled records so the caller can say what they settled *as*: a submission that
    never left the machine must leave a closed record and not a phantom queued one, which is
    what stops the poll asking about it forever and the next render being refused as in-flight.
    """
    deadline = time.monotonic() + timeout
    settled: list[dict] = []
    while time.monotonic() < deadline:
        settled = [
            job for job in get_json(f"{base_url}/api/projects/{project_id}")["jobs"]
            if job.get("error") == JOB_NEVER_SUBMITTED
        ]
        if len(settled) >= wanted:
            return settled
        time.sleep(0.15)
    return settled


def select_clip(driver, wait, shot_id: str) -> None:
    settle(driver, "#shots-track")
    clip = wait.until(
        lambda browser: browser.find_element(
            By.CSS_SELECTOR, f'#shots-track .shot-clip[data-shot-id="{shot_id}"]'
        )
    )
    visible_and_clickable(driver, clip, f"the timeline clip for {shot_id}")
    clip.click()
    wait.until(
        lambda browser: "selected"
        in browser.find_element(
            By.CSS_SELECTOR, f'#shots-track .shot-clip[data-shot-id="{shot_id}"]'
        ).get_attribute("class")
    )
    # The selection's reply reloads readiness, and *that* reply rebuilds the inspector. Read
    # nothing until the panel has stopped moving.
    settle(driver, "#shot-inspector")


def open_panel(driver, panel: str) -> None:
    driver.find_element(By.CSS_SELECTOR, f'[data-panel="{panel}"]').click()


def click_tab(driver, tab_id: str) -> None:
    button = driver.find_element(By.CSS_SELECTOR, f'#asset-filters button[data-filter="{tab_id}"]')
    visible_and_clickable(driver, button, f"the {tab_id} subtab")
    button.click()
    settle(driver, ".library-panel", quiet_ms=350)


def panes(driver) -> dict:
    return driver.execute_script(LIBRARY_PANES)


def assert_asset_pane(facts: dict, tab: str) -> None:
    """On an asset tab the clips must take no room at all, and the grid must take what is left.

    This is the Director's report, measured. Before the subtabs the clips library was a third
    implicit row of a `grid-template-rows: auto 1fr` panel, so its content pushed the asset grid
    up: with thirty-three takes in the real project the sorted sections were off the screen.
    """
    assert facts["clipsHiddenAttr"] is True, (tab, facts)
    assert facts["clipsDisplay"] == "none", (
        (f"the clips library carries `hidden` on the {tab} tab and the stylesheet is painting it "
        "anyway -- `display: block` beats the user agent's `[hidden]` rule"),
        facts,
    )
    assert facts["clips"]["height"] == 0, (
        (f"the clips library is still taking {facts['clips']['height']}px of the library panel on "
        f"the {tab} tab, which is the Director's report unfixed"),
        facts,
    )
    assert facts["gridHiddenAttr"] is False and facts["gridDisplay"] == "grid", (tab, facts)
    # The grid owns everything the toolbar left. Two pixels of slack for sub-pixel rounding.
    assert facts["grid"]["bottom"] >= facts["panel"]["bottom"] - 2, (
        (f"the asset grid stops {facts['panel']['bottom'] - facts['grid']['bottom']}px short of the "
        f"bottom of the library panel on the {tab} tab, so something else is taking that room"),
        facts,
    )
    assert facts["grid"]["height"] > 0, (tab, facts)


def main() -> None:
    port = 8774
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    result: dict[str, object] = {}
    comfy_root = Path(tempfile.mkdtemp(prefix="mvp-seed-tabs-comfy-"))
    unreachable = f"http://127.0.0.1:{dead_port()}"
    # The user's ComfyUI is never contacted: the one submission this script drives is pointed at a
    # dead port. Nothing here starts, stops or interrupts anything the user manages.
    os.environ["MVP_COMFY_ROOT"] = str(comfy_root)
    os.environ["MVP_COMFY_URL"] = unreachable
    os.environ["MVP_LLM_EJECT_BEFORE_RENDER"] = "false"
    os.environ["MVP_LLM_BASE_URL"] = ""

    with ManagedServer(port, label=NAME) as server:
        result["server_identity"] = server.evidence
        result["comfy_url"] = unreachable
        project_id = seed_project(server.base_url)

        driver = edge_driver()
        wait = WebDriverWait(driver, 25)
        try:
            driver.get(server.base_url)
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
            open_panel(driver, "timeline")
            select_clip(driver, wait, SHOT)

            # === 1. The box is shorter, and the toggle is beside it ===========================
            row = driver.execute_script(SEED_ROW)
            assert row["box"] and row["toggle"], (
                "the seed row is not on screen at all", row
            )
            assert int(row["value"]) == START_SEED, row
            assert row["checked"] is False, "the randomizer ships armed"
            # Shortened: the box no longer spans the panel, and is far narrower than the intent
            # box it used to match.
            assert row["box"]["width"] <= 150, (
                (f"the seed box is {row['box']['width']}px wide; the Director asked for it to be "
                "shortened"),
                row,
            )
            assert row["box"]["width"] < row["panel"]["width"] * 0.6, row
            assert row["box"]["width"] < row["intent"]["width"] * 0.7, (
                "the seed box is still as wide as the creative-intent box above it", row
            )
            # ...and the room it gave up carries the toggle, on the same line.
            assert row["label"]["left"] > row["box"]["left"] + row["box"]["width"] - 4, row
            assert abs(row["label"]["top"] - row["box"]["top"]) < row["box"]["height"] + 20, (
                "the randomize toggle is not on the seed's line", row
            )
            seed_input = driver.find_element(By.ID, "shot-seed")
            toggle = driver.find_element(By.ID, "shot-seed-randomize")
            visible_and_clickable(driver, seed_input, "the seed box")
            visible_and_clickable(driver, toggle, "the randomize toggle")
            result["seed_row"] = row

            # === 2. The label names the re-roll moment ========================================
            assert "Randomize on Render again" in row["labelText"], row["labelText"]
            assert "1" in row["labelTitle"] and "99999" in row["labelTitle"], row["labelTitle"]
            assert "Render again" in row["labelTitle"], row["labelTitle"]

            # Shortened means *sized to the number*, not "narrower than the panel happens to be".
            # The inspector is a fixed 280px column until 860px and full width below it, so a box
            # that still stretches to its container is a box whose width changes here -- and a
            # threshold alone would not catch a two-way split of the same row.
            driver.set_window_size(820, 950)
            narrow = driver.execute_script(SEED_ROW)
            driver.set_window_size(1600, 1000)
            settle(driver, "#shot-inspector", quiet_ms=300)
            assert narrow["panel"]["width"] > row["panel"]["width"] * 1.5, (
                ("this assertion needs the inspector to change width between the two layouts and "
                "it did not, so it proves nothing"),
                row["panel"], narrow["panel"],
            )
            assert narrow["box"]["width"] == row["box"]["width"], (
                (
                    "the seed box grew with the panel, so it is still sized to its container "
                    "rather than to the five digits the randomizer can produce"
                ),
                row["box"], narrow["box"],
            )
            result["seed_box_fixed_width"] = {
                "wide_panel": row["panel"]["width"], "wide_box": row["box"]["width"],
                "narrow_panel": narrow["panel"]["width"], "narrow_box": narrow["box"]["width"],
            }

            # === 3. Ticking rolls once, in bounds, and stores it ==============================
            toggle.click()
            rolled = wait_for_seed_change(server.base_url, project_id, START_SEED)
            settle(driver, "#shot-inspector")
            assert rolled != START_SEED, (
                "ticking the randomizer wrote nothing: the shot's stored seed is still "
                f"{rolled}. The Director asked it to RNG a number, not to promise one."
            )
            assert 1 <= rolled <= 99999, f"the rolled seed {rolled} is outside 1-99999"
            after_tick = driver.execute_script(SEED_ROW)
            assert after_tick["checked"] is True, after_tick
            assert int(after_tick["value"]) == rolled, (
                "the panel is showing a different seed from the one it stored", after_tick, rolled
            )
            result["roll_on_tick"] = {"from": START_SEED, "to": rolled}

            # === 4. It holds, and it is armed on ONE shot ====================================
            #
            # The panel is torn right down here -- another shot selected, then this one again,
            # each selection dragging its own readiness reply behind it -- because "hold it" is
            # the Director's word and a redraw that re-rolled would be the obvious wrong reading.
            #
            # **Per shot since 2026-08-21**, on the Director's ruling: *"Clicking randomize integer
            # on a shot toggles it for all shots, it should be per shot."* It shipped session-wide
            # the day before, and this assertion is the inversion of the one that was here then.
            # Only a browser can see it: one module-level boolean and one module-level `Set` read
            # identically in the source, and the difference shows up in a second shot's inspector.
            select_clip(driver, wait, OTHER)
            other_row = driver.execute_script(SEED_ROW)
            assert other_row["checked"] is False, (
                ("ticking the randomizer on one shot armed another one, which is the Director's "
                 "report: \"Clicking randomize integer on a shot toggles it for all shots, it "
                 "should be per shot.\""),
                other_row,
            )
            assert int(other_row["value"]) == 3, (
                ("selecting another shot rolled a seed for it; the toggle re-rolls on Render again "
                "and nowhere else"),
                other_row,
            )
            select_clip(driver, wait, SHOT)
            back_row = driver.execute_script(SEED_ROW)
            assert back_row["checked"] is True, (
                ("the toggle did not survive coming back to the shot it was ticked on, so it is "
                 "not per shot -- it is per render of the inspector"),
                back_row,
            )
            held = seed_held(server.base_url, project_id, rolled)
            assert held == rolled, (
                f"the seed moved from {rolled} to {held} without a render being asked for"
            )
            assert int(back_row["value"]) == rolled
            result["held_across_rebuild"] = held
            result["per_shot_toggle"] = {"armed_shot": True, "other_shot": other_row["checked"]}

            # === 4b. A retake on the *unarmed* shot strides ==================================
            #
            # The other half of the ruling, and the one a checkbox's `checked` attribute cannot
            # prove: with the randomizer armed on SHOT, a Render again on OTHER must take the
            # server's own `RESUBMIT_SEED_STRIDE` and not a fresh roll. `nextRenderSeed` is still
            # the single branch that decides; what changed is which shot's flag it is handed.
            select_clip(driver, wait, OTHER)
            other_before = stored_seed(server.base_url, project_id, OTHER)
            driver.find_element(By.ID, "render-again").click()
            WebDriverWait(driver, 10).until(EC.alert_is_present()).accept()
            other_after = wait_for_seed_change(
                server.base_url, project_id, other_before, shot_id=OTHER
            )
            assert other_after == other_before + RESUBMIT_SEED_STRIDE, (
                ("a retake on a shot whose randomizer is *not* ticked was randomised anyway, so "
                 f"the toggle is still session-wide: {other_before} -> {other_after}"),
            )
            # ...and the armed shot's own number did not move for someone else's render.
            assert stored_seed(server.base_url, project_id, SHOT) == rolled, (
                "requeueing one shot moved another shot's seed"
            )
            result["unarmed_shot_strides"] = {
                "from": other_before, "to": other_after, "stride": RESUBMIT_SEED_STRIDE
            }
            # The gesture is not over yet, and section 5 must not start inside it. The
            # submission is still failing against this run's dead port; when it does, the record
            # it wrote before submitting is settled and the browser re-reads the project whose
            # revision that write moved. Waited out by the record itself -- see
            # `wait_for_refused_submissions`, which is where the whole of it is written down.
            refused = wait_for_refused_submissions(server.base_url, project_id, 1)
            assert len(refused) == 1 and refused[0]["target_id"] == OTHER, (
                ("a Render again that never reached ComfyUI left no settled record: the job it "
                 "wrote before submitting is still open, which keeps the poll asking about a "
                 "graph nobody queued and refuses the next render as in-flight"),
                refused,
            )
            assert refused[0]["status"] == "error", refused
            result["refused_submission_settles"] = {
                "target": refused[0]["target_id"], "status": refused[0]["status"],
                "error": refused[0]["error"],
            }
            select_clip(driver, wait, SHOT)
            settle(driver, "#shot-inspector")

            # === 5. A hand-typed seed clears the toggle =======================================
            seed_input = driver.find_element(By.ID, "shot-seed")
            # Select-and-retype rather than `clear()`: WebDriver's clear fires `change` with an
            # empty box, which saves a 0 and rebuilds the panel -- taking this element reference
            # with it. Typing raises no `change` until the blur below, which is the one gesture
            # under test here.
            seed_input.send_keys(Keys.CONTROL, "a")
            seed_input.send_keys("4242")
            driver.find_element(By.ID, "shot-prompt").click()
            typed = wait_for_seed_value(server.base_url, project_id, 4242)
            settle(driver, "#shot-inspector")
            assert typed == 4242, (
                f"a hand-typed seed did not reach the manifest: it is {typed}"
            )
            after_typing = driver.execute_script(SEED_ROW)
            assert after_typing["checked"] is False, (
                ("typing a seed by hand left the randomizer armed, so the next Render again would "
                "throw the Director's own number away"),
                after_typing,
            )
            result["hand_typed"] = {"seed": typed, "toggle_cleared": True}
            # The seed row as a Director sees it, kept because this is the half of this script that
            # a number cannot show: the shortened box, the toggle beside it on the same line, and
            # the panel around them. Every defect this repository has caught by looking was caught
            # in a picture nobody had to ask for.
            driver.save_screenshot(str(artifact_dir() / f"{NAME}-seed-row.png"))

            # === 6. Render again re-rolls -- and by exactly one rule ==========================
            #
            # Unticked first, so the rule that shipped is proven unchanged before the new one is
            # added to it. The submission that follows the seed write cannot reach ComfyUI (the
            # URL is a dead port this run chose), and the seed lands on the manifest *before* that
            # call is made -- which is the ordering the one-gesture flow depends on.
            fixed_before = stored_seed(server.base_url, project_id)
            driver.find_element(By.ID, "render-again").click()
            question = WebDriverWait(driver, 10).until(EC.alert_is_present())
            asked = question.text
            assert "fresh seed" in asked, asked
            question.accept()
            fixed_after = wait_for_seed_change(server.base_url, project_id, fixed_before)
            assert fixed_after == fixed_before + RESUBMIT_SEED_STRIDE, (
                "with the randomizer off, Render again must still stride the seed by the server's "
                f"own RESUBMIT_SEED_STRIDE: {fixed_before} -> {fixed_after}"
            )
            result["render_again_fixed"] = {"from": fixed_before, "to": fixed_after,
                                            "stride": RESUBMIT_SEED_STRIDE, "question": asked}
            wait_for_refused_submissions(server.base_url, project_id, 2)

            # Now with it ticked. The shot was re-opened by the click above, so it is put back to
            # a settled state through the same routes a Director would use -- the render never
            # landed, because it never left the machine.
            put_json(f"{server.base_url}/api/projects/{project_id}/shots", {"shots": [
                {"id": SHOT, "start": 0, "duration": 4,
                 "prompt": "The corridor, pushing in on Lucy.", "mode": "text",
                 "status": "complete", "seed": fixed_after,
                 "latest_output": f"music-video-producer/{project_id}/shots/{SHOT}-h3_00002-audio.mp4"},
                # Put back the way the fixture seeded it -- settled, with its own take -- so the
                # clips library still holds the three takes step 11 counts.
                {"id": OTHER, "start": 4, "duration": 4, "prompt": "The same corridor, wider.",
                 "mode": "text", "status": "complete", "seed": 3,
                 "latest_output": f"music-video-producer/{project_id}/shots/{OTHER}-h3_00001-audio.mp4"},
            ]})
            driver.refresh()
            wait.until(EC.presence_of_element_located((By.ID, "project-select")))
            wait.until(
                lambda browser: browser.find_element(By.ID, "project-select").get_attribute("value")
                == project_id
            )
            open_panel(driver, "timeline")
            select_clip(driver, wait, SHOT)
            # The toggle is session state, so a reload leaves it off -- which is the documented
            # decision and is asserted here rather than worked around.
            reloaded = driver.execute_script(SEED_ROW)
            assert reloaded["checked"] is False, (
                ("the randomize toggle survived a reload; it is session state and is not supposed "
                "to be persisted anywhere"),
                reloaded,
            )
            result["not_persisted_across_reload"] = True

            driver.find_element(By.ID, "shot-seed-randomize").click()
            tick_two = wait_for_seed_change(server.base_url, project_id, fixed_after)
            settle(driver, "#shot-inspector")
            assert tick_two != fixed_after and 1 <= tick_two <= 99999, tick_two
            driver.find_element(By.ID, "render-again").click()
            question = WebDriverWait(driver, 10).until(EC.alert_is_present())
            question.accept()
            random_after = wait_for_seed_change(server.base_url, project_id, tick_two)
            assert random_after != tick_two, (
                "with the randomizer ticked, Render again left the seed exactly where it was: "
                f"{random_after}. That resubmits at the same seed and prompt, which is served "
                "from ComfyUI's execution cache without sampling, or reproduces the identical "
                "take while the model stays resident (measured 2026-08-23)."
            )
            assert 1 <= random_after <= 99999, (
                f"the re-rolled seed {random_after} is outside the Director's 1-99999"
            )
            assert random_after != tick_two + RESUBMIT_SEED_STRIDE, (
                "the randomizer and the resubmit stride both fired on one click, so the seed is "
                f"drifting under a value the Director asked to own: {tick_two} -> {random_after}"
            )
            result["render_again_random"] = {"from": tick_two, "to": random_after}
            # Same wait as the two arms before it, and here it is also what keeps section 7's
            # click off a button the re-read is about to replace.
            wait_for_refused_submissions(server.base_url, project_id, 3)

            # === 7. Mark ready does not re-roll ==============================================
            #
            # The toggle is still ticked and the shot is open after the re-render, so this is the
            # other control in the panel that could plausibly have been read as "regenerate".
            settle(driver, "#shot-inspector")
            mark = driver.find_elements(By.ID, "mark-ready")
            if mark:
                visible_and_clickable(driver, mark[0], "the mark-ready control")
                mark[0].click()
                still = seed_held(server.base_url, project_id, random_after)
                assert still == random_after, (
                    "Mark ready re-rolled the seed. It queues nothing, so a seed that moved there "
                    f"breaks a fixed-seed comparison for no render: {random_after} -> {still}"
                )
                result["mark_ready_holds"] = still
            else:
                result["mark_ready_holds"] = "not offered in this state"

            # === 8-12. The Assets panel's subtabs ============================================
            open_panel(driver, "assets")
            settle(driver, ".library-panel", quiet_ms=350)

            strip = driver.execute_script(TAB_STRIP)
            assert [tab["id"] for tab in strip] == [
                "all", "character", "setting", "prop", "style", "media", "clips"
            ], strip
            labels = [tab["label"] for tab in strip]
            # The clips tab carries its count; the others carry their bare label.
            assert labels[:6] == EXPECTED_TABS[:6], labels
            assert labels[6].startswith("Clips"), labels
            assert labels[6].split()[-1] == "3", (
                "the Clips tab is not naming how many takes are behind it", labels
            )
            for tab in strip:
                assert tab["role"] == "tab", tab
                button = driver.find_element(
                    By.CSS_SELECTOR, f'#asset-filters button[data-filter="{tab["id"]}"]'
                )
                visible_and_clickable(driver, button, f"the {tab['label']} subtab")
            result["tab_strip"] = strip

            # The strip survives its own click. A press calls `renderAssets`, which redraws the
            # strip -- and a redraw that rewrites `innerHTML` destroys the very button under the
            # pointer, taking its keyboard focus with it. That is the shape of the defect found in
            # this codebase on 2026-08-21 (a `dblclick` that never fired because `pointerdown`
            # re-rendered every clip), so it is asserted rather than assumed: focus the tab, press
            # it with the keyboard, and it must still be the focused element afterwards.
            clips_tab = driver.find_element(
                By.CSS_SELECTOR, '#asset-filters button[data-filter="clips"]'
            )
            driver.execute_script("arguments[0].focus();", clips_tab)
            clips_tab.send_keys(Keys.ENTER)
            settle(driver, ".library-panel", quiet_ms=350)
            focus = driver.execute_script(
                "const a = document.activeElement;"
                "return {tag: a ? a.tagName : '', tab: a ? (a.dataset.filter || '') : '',"
                " attached: a ? document.contains(a) : false};"
            )
            assert focus["tab"] == "clips" and focus["attached"] is True, (
                (
                    "pressing a subtab with the keyboard threw away the button that was pressed: "
                    "the strip is being rebuilt with innerHTML on every render"
                ),
                focus,
            )
            assert panes(driver)["clipsHiddenAttr"] is False, (
                "the keyboard press did not switch the tab"
            )
            result["tab_keyboard_focus"] = focus

            # --- 9. One pane at a time, at four widths ----------------------------------------
            #
            # 1600 is the design width; 1100 and 950 are both inside the 1180px breakpoint that
            # folds the panel to two columns, and 950 is where the library column gets narrow
            # enough that seven tabs and a 180px search box no longer share a line; 820 crosses the
            # 860px breakpoint that folds everything to one column. The show/hide is asserted at
            # each, because a pane that is `hidden` and painted anyway is a stylesheet failure and
            # stylesheets are what change at a breakpoint.
            by_width: dict[str, dict] = {}
            for width in (1600, 1100, 950, 820):
                driver.set_window_size(width, 950)
                click_tab(driver, "all")
                # Every tab stays whole and reachable. The strip grew from five buttons to seven,
                # and a strip that is allowed to shrink instead of wrap squashes them until their
                # labels are cut off -- reachable by a hit test and unreadable to a Director.
                for tab in driver.find_elements(By.CSS_SELECTOR, "#asset-filters button"):
                    assert not clipped(driver, tab), (
                        f"the {tab.get_attribute('data-filter')} tab's label is cut off at "
                        f"{width}px: the strip is being squashed rather than wrapped"
                    )
                    visible_and_clickable(
                        driver, tab, f"the {tab.get_attribute('data-filter')} tab at {width}px"
                    )
                on_assets = panes(driver)
                assert_asset_pane(on_assets, f"all @ {width}px")
                click_tab(driver, "clips")
                on_clips = panes(driver)
                assert on_clips["gridHiddenAttr"] is True and on_clips["gridDisplay"] == "none", (
                    f"the asset grid is still painted under the Clips tab at {width}px", on_clips
                )
                assert on_clips["grid"]["height"] == 0, (width, on_clips)
                assert on_clips["clipsHiddenAttr"] is False, (width, on_clips)
                assert on_clips["clipCards"] == 3, (width, on_clips)
                assert on_clips["clips"]["height"] > 0, (width, on_clips)
                by_width[str(width)] = {
                    "viewport": driver.execute_script("return window.innerWidth;"),
                    "assets_tab": {"grid_h": on_assets["grid"]["height"],
                                   "clips_h": on_assets["clips"]["height"],
                                   "panel_h": on_assets["panel"]["height"],
                                   "cards": len(on_assets["cards"])},
                    "clips_tab": {"grid_h": on_clips["grid"]["height"],
                                  "clips_h": on_clips["clips"]["height"],
                                  "clip_cards": on_clips["clipCards"]},
                }
            result["panes_by_width"] = by_width
            result["tab_reachability"] = reachable_widths(
                driver, '#asset-filters button[data-filter="clips"]', [1600, 1100, 950, 820]
            )

            driver.set_window_size(1600, 1000)

            # --- 10. The tabs show and hide the right assets ---------------------------------
            per_tab: dict[str, list[str]] = {}
            for tab_id in ("all", "character", "setting", "prop", "style", "media"):
                click_tab(driver, tab_id)
                facts = panes(driver)
                assert_asset_pane(facts, tab_id)
                per_tab[tab_id] = facts["cards"]
                if tab_id == "prop":
                    # The one genuinely empty tab. It says so in its own words rather than
                    # rendering an empty box that reads as a panel that failed to load.
                    assert facts["cards"] == [], facts
                    assert facts["emptyTitle"] == "No props yet", facts
                    assert facts["emptyHint"], facts
                    result["empty_tab"] = {"title": facts["emptyTitle"], "hint": facts["emptyHint"]}
            assert sorted(per_tab["all"]) == sorted(name for _, _, name in SEEDED_ASSETS), per_tab
            # Nothing in the library is invisible: every asset the All tab draws is drawn by one
            # of the kind tabs too. Six of `models.AssetKind`'s seven members are seeded here --
            # `prop` is the deliberately empty one -- and this is the browser's own proof that the
            # three the Director did not name a tab for did not get dropped.
            named = {name for tab, names in per_tab.items() if tab != "all" for name in names}
            assert named == set(per_tab["all"]), (
                "these assets appear under no subtab, so they are invisible and undeletable from "
                f"this screen: {sorted(set(per_tab['all']) - named)}"
            )
            result["kinds_seeded"] = sorted({kind for _, kind, _ in SEEDED_ASSETS})
            result["kinds_in_model"] = sorted(get_args(AssetKind))
            assert per_tab["character"] == ["Lucy the singer"], per_tab
            assert per_tab["setting"] == ["Service corridor"], per_tab
            assert per_tab["style"] == ["Grain plate"], per_tab
            # The three kinds the Director did not name a tab for, together and reachable.
            assert sorted(per_tab["media"]) == ["Room tone", "Stock plate", "Uploaded still"], per_tab
            result["assets_per_tab"] = per_tab

            # --- 12. A tab change loses no selection -----------------------------------------
            #
            # The Director's earlier note is that "Attach to selected shot" is hard to use because
            # the timeline is not visible from here. It must not get *worse*: the shot selected on
            # the timeline and the asset selected in the grid both have to survive a tab change,
            # or the button goes dead for a reason nothing on screen explains.
            click_tab(driver, "character")
            card = driver.find_element(By.CSS_SELECTOR, '.asset-card[data-asset-id="asset_lucy"]')
            visible_and_clickable(driver, card, "the character card")
            card.click()
            settle(driver, "#asset-inspector", quiet_ms=350)
            attach = driver.find_element(By.ID, "attach-asset")
            assert attach.get_attribute("disabled") is None, (
                "Attach to selected shot is shut even though a shot is selected on the timeline"
            )
            click_tab(driver, "clips")
            settle(driver, "#asset-inspector", quiet_ms=350)
            on_clips_selection = {
                "heading": driver.find_element(By.CSS_SELECTOR, "#asset-inspector h2").text,
                "attach_disabled": driver.find_element(By.ID, "attach-asset").get_attribute(
                    "disabled"
                ) is not None,
            }
            assert on_clips_selection["heading"] == "Lucy the singer", on_clips_selection
            assert on_clips_selection["attach_disabled"] is False, (
                ("switching to the Clips tab shut Attach to selected shot, so the tab change lost "
                "the sense of which shot is selected"),
                on_clips_selection,
            )
            click_tab(driver, "character")
            back = driver.find_element(
                By.CSS_SELECTOR, '.asset-card[data-asset-id="asset_lucy"]'
            ).get_attribute("class")
            assert "selected" in back, (
                "coming back to an asset tab lost the selected asset", back
            )
            result["selection_survives_tab_change"] = on_clips_selection

            # --- 11. The clips library is unchanged by the move ------------------------------
            click_tab(driver, "clips")
            # The other half of the Director's ask, in a picture: seven tabs, the clips on their
            # own one, and the sorted sections no longer pushed off the screen by them. The
            # geometry is measured below; this is what a reader can check the measurement against.
            driver.save_screenshot(str(artifact_dir() / f"{NAME}-clips-tab.png"))
            facts = panes(driver)
            # Three takes, three cards, three ways back to the shot -- and with ComfyUI at a dead
            # port, two of them can still be shown: each shot's *current* take, which this
            # application serves from ComfyUI's output directory on disk through
            # `/shots/{id}/take`. The third is a superseded take, addressable only by its path,
            # and only ComfyUI's `/view` serves a path. It says so rather than 404ing (2026-08-21).
            assert facts["clipCards"] == 3 and facts["clipJumps"] == 3, facts
            assert facts["clipVideos"] == 2, (
                ("the Clips tab is drawing a video element for a take it cannot fetch; ComfyUI is "
                 "at a dead port for this whole run"),
                facts,
            )
            assert facts["clipUnplayable"] == 1, facts
            assert facts["clipVideos"] + facts["clipUnplayable"] == facts["clipCards"], (
                "a take is neither shown nor accounted for", facts
            )
            assert "Generated clips" in facts["clipsHeading"], facts
            first_video = driver.find_element(By.CSS_SELECTOR, "#clips-library .clip-card video")
            visible_and_clickable(driver, first_video, "the first clip card")
            # Hover-to-play is still bound. The source 404s (this fixture has no file on disk, and
            # a real one would have to come off a GPU), so what is asserted is that the gesture
            # reaches the element's own play path rather than that a frame appeared -- `play()` on
            # a failed source rejects, and the handler swallows that, which is exactly the
            # behaviour that moved here unchanged.
            played = driver.execute_script(
                "const v = arguments[0];"
                "let plays = 0, pauses = 0;"
                "const play = v.play.bind(v), pause = v.pause.bind(v);"
                "v.play = () => { plays += 1; return play(); };"
                "v.pause = () => { pauses += 1; return pause(); };"
                "v.dispatchEvent(new MouseEvent('mouseenter'));"
                "v.dispatchEvent(new MouseEvent('mouseleave'));"
                "return {plays, pauses, currentTime: v.currentTime};",
                first_video,
            )
            assert played["plays"] == 1 and played["pauses"] == 1, (
                ("hover-to-play did not survive the move to its own subtab: the card's mouseenter "
                "no longer reaches the element's play path"),
                played,
            )
            result["clip_hover"] = played
            # "Open shot" still lands on the producing shot in the timeline.
            driver.find_element(By.CSS_SELECTOR, "#clips-library .clip-jump").click()
            wait.until(
                lambda browser: "active"
                in browser.find_element(By.ID, "panel-timeline").get_attribute("class")
            )
            settle(driver, "#shots-track")
            jumped = driver.execute_script(
                "const c = document.querySelector('#shots-track .shot-clip.selected');"
                "return {shot: c ? c.dataset.shotId : '',"
                " panel: document.querySelector('.panel.active')"
                "  ? document.querySelector('.panel.active').id : ''};"
            )
            assert jumped["shot"] in (SHOT, OTHER), jumped
            result["open_shot"] = jumped

            driver.save_screenshot(str(artifact_dir() / f"{NAME}.png"))
            console_gate(
                driver, NAME, result,
                expected=[*EXPECTED_CONSOLE, unreachable.removeprefix("http://")],
            )
            report(NAME, result)
        except TimeoutException as error:  # pragma: no cover - a real failure, not a skip
            raise AssertionError(f"the browser stopped waiting: {error}") from error
        finally:
            driver.quit()


if __name__ == "__main__":
    main()
