"""Browser QA for switching a shot between its own takes from the shot inspector.

The Director's report, 2026-08-21: *"when looking at that Shot 03 that we have 2 takes for, i can
see the takes in the shot info window but clicking on either does nothing instead of hot swapping
between available shots."*

**Driven in a browser before anything was changed, because there were two candidate causes and only
one of them was real.** `takesStripRows` was already returning the right model -- the non-current
take carried `Use` and `disabled: false` -- so the defect was in the interaction layer, and the two
candidates were (1) *affordance*: only the small chip at the row's right end was live, and (2) a
*stale handler*: this application has a recorded defect where the inspector is rebuilt by a reply
nobody awaited and elements go stale mid-interaction. Measured on a shot with two real takes:

- clicking the **row** left `latest_output` exactly where it was, and
- clicking the **chip** switched it correctly.

So it was the affordance, and the handler was never stale. That distinction is worth a test rather
than a note, because the two causes have opposite fixes and the second one would have implicated
every other control in this panel. Both halves are asserted below: the row activates the take now,
**and** it still does after the inspector has been torn down and rebuilt.

Every assertion reads the *server's* copy of `latest_output` back, never the browser's. A stub DOM
will happily report that a listener was attached to a node that no longer exists, which is exactly
how this survived to be reported by hand.

What is asserted, in order:

1. **The strip's shape.** Two rows, each a real `<button>`; the current one `disabled` and
   unfocusable, the other enabled and hit-testable at its centre.
2. **The row swaps the take.** A click on the row's *text*, as far from the chip as the row allows,
   and the swap read back from the server. The Monitor follows: its video source names the new take.
3. **The keyboard swaps the take**, because the row is a real control now -- focus it and press
   Enter.
4. **It survives a rebuild.** The inspector is torn down and redrawn (select another shot, come
   back, let the readiness reply land) before the click. This is the direct guard for the recorded
   stale-element defect.
5. **The current row is closed, not merely inert.** Disabled, unfocusable, and clicking it changes
   nothing on the server.
6. **The two rows are distinguishable.** Two takes of one shot differ only by a serial buried in a
   filename the row has to truncate, so each row carries the seed it was rendered at and when it
   landed; the test asserts those two lines actually differ.

No GPU is spent and nothing reaches `/prompt`. The two takes are synthesized locally by ffmpeg into
an isolated `MVP_COMFY_ROOT` this run owns -- ComfyUI's half of the contract, which has no route to
arrive by without a GPU -- and every gesture here is a click, a keypress or a `select-take` write.

Run from the repo root -- it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_take_swap.py [--port 8770]

Assumes: nothing listening on the port (it refuses to reuse a bound one), Microsoft Edge and its
WebDriver installed, ffmpeg on PATH, and `music_video_producer` importable from this checkout's
`src/`. ComfyUI does not need to be running.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

from e2e_support import (
    ManagedServer,
    StaleServer,
    console_gate,
    edge_driver,
    get_json,
    post_json,
    put_json,
    report,
    settle,
    visible_and_clickable,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

NAME = "take-swap"

SHOT = "shot_03"
OTHER = "shot_04"

# The Clips library draws each take as a `<video>` pointed straight at ComfyUI's own `/view`
# endpoint rather than through this application's take route, so every card 404s whenever ComfyUI
# is down -- which is the documented state for browser QA. Declared by name rather than filtered,
# so the gate stays a gate; recorded as a finding rather than fixed, because it is a different
# surface from the one under test here.
EXPECTED_CONSOLE = ["127.0.0.1:8188/view", ":8188/view"]

#: What the strip is, as the browser has it. Read as markup *and* as computed state, because
#: "renders a button" and "is a control a Director can use" are different claims.
STRIP = """
const rows = [...document.querySelectorAll('#shot-inspector .take-row')];
return rows.map((row) => {
  const style = getComputedStyle(row);
  const box = row.getBoundingClientRect();
  return {
    tag: row.tagName,
    type: row.getAttribute('type'),
    className: row.className,
    disabled: row.disabled === true,
    output: row.dataset.output || '',
    title: row.getAttribute('title') || '',
    name: (row.querySelector('.take-name') || {}).textContent || '',
    meta: (row.querySelector('.take-meta') || {}).textContent || '',
    chip: (row.querySelector('.take-chip') || {}).textContent || '',
    cursor: style.cursor,
    width: Math.round(box.width),
    height: Math.round(box.height),
  };
});
"""

#: Where the Monitor is pointed. The take route carries the selected output in its query string,
#: so a swap the Monitor followed is visible in the URL the element is actually loading.
MONITOR = """
const video = document.querySelector('#monitor-video');
const player = document.querySelector('#take-player');
return {
  monitorSrc: video ? (video.getAttribute('src') || '') : '',
  showingTake: document.querySelector('#timeline-monitor').classList.contains('showing-take'),
  inspectorPlayerSrc: player ? (player.getAttribute('src') || '') : '',
};
"""


def synthesize_take(target: Path, colour: str) -> None:
    """One real, playable take. Local ffmpeg, no GPU, nothing to `/prompt`.

    Two visibly different colours, so the two takes are genuinely two files rather than one file
    copied -- a swap that appeared to work because both bytes were identical would prove nothing.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c={colour}:size=320x240:duration=2:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(target),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
    except FileNotFoundError as error:
        raise StaleServer(
            "ffmpeg is not on PATH, and this script needs it to synthesize two takes without a GPU."
        ) from error
    except subprocess.CalledProcessError as error:
        raise StaleServer(f"ffmpeg could not synthesize a take:\n{error.stderr}") from error
    if not target.is_file() or target.stat().st_size == 0:
        raise StaleServer(f"ffmpeg reported success and wrote nothing at {target}")


def seed(base_url: str, comfy_root: Path) -> dict:
    """A shot with two real takes, built through shipped routes.

    The two job records are written through the whole-project PUT: a `RenderJob` is ComfyUI's
    record of a render and there is no route that mints one without spending a GPU pass. Their
    seeds and timestamps are deliberately **different**, because that is what the strip now shows
    to tell two takes of one shot apart, and identical fixtures would make that assertion vacuous.
    """
    project = post_json(f"{base_url}/api/projects", {"name": "Take swap browser QA"})
    project_id = project["id"]
    takes = [
        f"music-video-producer/{project_id}/shots/{SHOT}-h3-reference_00001-audio.mp4",
        f"music-video-producer/{project_id}/shots/{SHOT}-h3-reference_00002-audio.mp4",
    ]
    for take, colour in zip(takes, ("orange", "blue")):
        synthesize_take(comfy_root / "output" / take, colour)

    put_json(f"{base_url}/api/projects/{project_id}/shots", {"shots": [
        {"id": SHOT, "start": 0, "duration": 4, "prompt": "The corridor, pushing in.",
         "mode": "text", "status": "complete", "latest_output": takes[1]},
        {"id": OTHER, "start": 4, "duration": 4, "prompt": "The same corridor, wider.",
         "mode": "text", "status": "draft"},
    ]})
    project = get_json(f"{base_url}/api/projects/{project_id}")
    project["jobs"] = [
        {"id": "job_take_1", "kind": "h3", "status": "complete", "prompt_id": "prompt-1",
         "target_id": SHOT, "seed": 3, "output_files": [takes[0]],
         "created_at": "2026-08-20T09:15:00Z", "updated_at": "2026-08-20T09:21:00Z"},
        {"id": "job_take_2", "kind": "h3", "status": "complete", "prompt_id": "prompt-2",
         "target_id": SHOT, "seed": 104, "output_files": [takes[1]],
         "created_at": "2026-08-20T11:02:00Z", "updated_at": "2026-08-20T11:08:00Z"},
    ]
    put_json(f"{base_url}/api/projects/{project_id}", project)
    return {"id": project_id, "takes": takes}


def stored_output(base_url: str, project_id: str, shot_id: str = SHOT) -> str:
    project = get_json(f"{base_url}/api/projects/{project_id}")
    return next(shot["latest_output"] for shot in project["shots"] if shot["id"] == shot_id)


def wait_for_output(base_url: str, project_id: str, wanted: str, timeout: float = 10.0) -> str:
    """The stored `latest_output`, once it is `wanted` -- or whatever it still is at the timeout.

    Polls the *effect* rather than waiting for the panel to go quiet. `settle` returns as soon as
    nothing has mutated for its quiet window, which a click whose `select-take` POST is still in
    flight satisfies trivially: the DOM has not moved *yet*. Reading the server in that gap made
    this script fail once in three runs against a fix that works, which is the worst kind of test.
    Returning rather than raising keeps the caller's own sentence -- the one that names the
    Director's report -- as the failure message.
    """
    deadline = time.monotonic() + timeout
    seen = stored_output(base_url, project_id)
    while seen != wanted and time.monotonic() < deadline:
        time.sleep(0.15)
        seen = stored_output(base_url, project_id)
    return seen


def select_clip(driver, wait, shot_id: str):
    """Click the clip on the timeline, the way a Director selects a shot, and wait for the panel."""
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


def strip_rows(driver) -> list[dict]:
    return driver.execute_script(STRIP)


def actionable_row(driver):
    """The one row a Director can act on: rendered, enabled, and hit-testable at its centre."""
    rows = driver.find_elements(By.CSS_SELECTOR, "#shot-inspector .take-row:not(:disabled)")
    assert len(rows) == 1, f"expected exactly one selectable take row, found {len(rows)}"
    return rows[0]


def main() -> None:
    port = 8770
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    result: dict[str, object] = {}
    # An isolated ComfyUI root this run owns. The user's real installation is never read or
    # written, and the two synthesized takes are the only things that will ever exist under it.
    comfy_root = Path(tempfile.mkdtemp(prefix="mvp-take-swap-comfy-"))
    os.environ["MVP_COMFY_ROOT"] = str(comfy_root)
    with ManagedServer(port, label=NAME) as server:
        result["server_identity"] = server.evidence
        fixture = seed(server.base_url, comfy_root)
        project_id = fixture["id"]
        take_one, take_two = fixture["takes"]

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
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            select_clip(driver, wait, SHOT)

            # --- 1. The strip's shape ---------------------------------------------------------
            rows = strip_rows(driver)
            assert len(rows) == 2, rows
            assert stored_output(server.base_url, project_id) == take_two
            for row in rows:
                assert row["tag"] == "BUTTON", (
                    "a take row is not a button, so it carries no focus, no Enter and no Space: "
                    f"{row}"
                )
                assert row["type"] == "button", (
                    "the row is a submit button by default, which is a form-shaped surprise", row
                )
                assert row["width"] > 150 and row["height"] > 0, row
            first, second = rows
            assert first["output"] == take_one and second["output"] == take_two, rows
            assert first["disabled"] is False and second["disabled"] is True, (
                "the selectable take is not the one the shot is pointing away from", rows
            )
            assert "current" in second["className"] and "current" not in first["className"], rows
            assert first["cursor"] == "pointer", (
                "the selectable row does not even look clickable", first
            )
            assert second["cursor"] != "pointer", (
                "the row the shot already points at invites a click that does nothing", second
            )
            assert first["chip"] == "Use" and second["chip"] == "Current", rows
            visible_and_clickable(driver, actionable_row(driver), "the selectable take row")
            result["strip"] = rows

            # --- 6. The two rows are distinguishable ------------------------------------------
            #
            # Asserted here, while both rows are on screen: two takes of one shot differ only by a
            # serial buried in a filename the row truncates to an ellipsis, so the seed and the
            # landing time are what a Director actually chooses between.
            assert first["meta"] and second["meta"], (
                "the take rows carry nothing but a truncated filename to tell them apart", rows
            )
            assert first["meta"] != second["meta"], (
                "both takes of this shot render the same provenance line, so the strip still gives "
                f"a Director nothing to choose between: {first['meta']!r}"
            )
            assert "seed 3" in first["meta"] and "seed 104" in second["meta"], rows
            # The full path is still reachable, on the row itself rather than on an inner span.
            assert first["title"] == take_one and second["title"] == take_two, rows
            result["provenance"] = [first["meta"], second["meta"]]

            # --- 2. Clicking the ROW swaps the take -------------------------------------------
            #
            # The click lands on the row's *name*, which is the text the Director reported clicking
            # and the part of the row that used to do nothing. Not on the chip: the chip already
            # worked, and a test that clicked it would pass against the defect.
            name = driver.find_element(
                By.CSS_SELECTOR, "#shot-inspector .take-row:not(:disabled) .take-name"
            )
            monitor_before = driver.execute_script(MONITOR)
            name.click()
            after_click = wait_for_output(server.base_url, project_id, take_one)
            settle(driver, "#shot-inspector")
            assert after_click == take_one, (
                "clicking the take row did nothing: the shot is still pointed at "
                f"{after_click!r}. This is the Director's report, unfixed."
            )
            monitor_after = driver.execute_script(MONITOR)
            assert monitor_after["inspectorPlayerSrc"] != monitor_before["inspectorPlayerSrc"], (
                (
                    "the take swapped on the server and the inspector's player is still loading "
                    "the old one"
                ),
                monitor_before,
                monitor_after,
            )
            # The take route carries the selected output in its query string, percent-encoded, so
            # "the Monitor followed" is checkable on the URL the element is really loading rather
            # than inferred from the swap having happened.
            wanted = quote(take_one, safe="")
            assert wanted in monitor_after["inspectorPlayerSrc"], monitor_after
            assert wanted in monitor_after["monitorSrc"], (
                (
                    "the inspector's player followed the swap and the Monitor above the transport "
                    "did not"
                ),
                monitor_after,
            )
            assert monitor_after["showingTake"] is True, monitor_after
            result["row_click"] = {
                "from": take_two, "to": after_click,
                "player": [monitor_before["inspectorPlayerSrc"],
                           monitor_after["inspectorPlayerSrc"]],
                "monitor_showing_take": monitor_after["showingTake"],
            }

            # The rows swapped roles, so the strip is describing the new state and not the old one.
            rows = strip_rows(driver)
            assert [row["chip"] for row in rows] == ["Current", "Use"], rows
            assert [row["disabled"] for row in rows] == [True, False], rows

            # --- 3. The keyboard swaps the take -----------------------------------------------
            #
            # It is a real control now, so this must work without a mouse. Focused with Tab-free
            # precision by calling focus() -- what is under test is that the element *takes* focus
            # and that Enter activates it, not the tab order of the whole panel.
            row = actionable_row(driver)
            driver.execute_script("arguments[0].focus();", row)
            assert driver.execute_script(
                "return document.activeElement === arguments[0];", row
            ), "the take row cannot take keyboard focus"
            row.send_keys(Keys.ENTER)
            after_enter = wait_for_output(server.base_url, project_id, take_two)
            settle(driver, "#shot-inspector")
            assert after_enter == take_two, (
                f"Enter on a focused take row did not swap the take: still {after_enter!r}"
            )
            result["keyboard_swap"] = {"from": take_one, "to": after_enter}

            # --- 5. The current row is closed, not merely inert -------------------------------
            current = driver.find_element(
                By.CSS_SELECTOR, "#shot-inspector .take-row:disabled"
            )
            assert current.get_attribute("data-output") == take_two, current.get_attribute(
                "data-output"
            )
            driver.execute_script("arguments[0].focus();", current)
            assert not driver.execute_script(
                "return document.activeElement === arguments[0];", current
            ), "the current take row takes keyboard focus, so a Director can land on a dead control"
            before_dead_click = stored_output(server.base_url, project_id)
            driver.execute_script("arguments[0].click();", current)
            # The negative case, so this waits *for* the write it expects never to arrive rather
            # than reading once and moving on: `wait_for_output` polls for the full timeout when
            # the value never becomes what it was asked for, which is exactly the shape wanted
            # here. Asking for the *other* take makes "it never changed" the passing outcome.
            settle(driver, "#shot-inspector", quiet_ms=900)
            assert wait_for_output(
                server.base_url, project_id, take_one, timeout=2.0
            ) == before_dead_click, "clicking the disabled current row wrote something"

            # --- 4. It survives a rebuild -----------------------------------------------------
            #
            # The other candidate cause of the report, and the recorded failure mode in this exact
            # panel: the inspector is rebuilt by replies nobody awaited, and a handler bound to the
            # superseded nodes is genuinely dead. The panel is torn right down here -- another shot
            # selected, then this one again, each selection dragging its own readiness reply behind
            # it -- and the row must still work afterwards.
            select_clip(driver, wait, OTHER)
            assert not driver.find_elements(By.CSS_SELECTOR, "#shot-inspector .take-row"), (
                "the other shot has no takes and is drawing a takes strip anyway"
            )
            select_clip(driver, wait, SHOT)
            rebuilt = strip_rows(driver)
            assert len(rebuilt) == 2, rebuilt
            before_rebuild_click = stored_output(server.base_url, project_id)
            driver.find_element(
                By.CSS_SELECTOR, "#shot-inspector .take-row:not(:disabled) .take-name"
            ).click()
            after_rebuild = wait_for_output(server.base_url, project_id, take_one)
            settle(driver, "#shot-inspector")
            assert after_rebuild != before_rebuild_click, (
                "after the inspector was rebuilt the take row stopped working, which is the stale-"
                "element defect this panel has a recorded history of"
            )
            assert after_rebuild == take_one, after_rebuild
            result["swap_after_rebuild"] = {"from": before_rebuild_click, "to": after_rebuild}

            # Nothing was queued by any of it: every write here is the one-field `select-take`.
            project = get_json(f"{server.base_url}/api/projects/{project_id}")
            assert [job["id"] for job in project["jobs"]] == ["job_take_1", "job_take_2"], (
                "this script created a job, and it is supposed to spend no GPU time at all"
            )

            console_gate(driver, NAME, result, expected=EXPECTED_CONSOLE)
            report(NAME, result)
        finally:
            driver.quit()


if __name__ == "__main__":
    main()
