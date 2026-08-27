"""Browser QA for the band panel (Epic 10, slice E3).

The offline harness executes every decision this surface makes -- which state a glyph is in, what
the panel says in each absence, whether a binding is complete enough to write, what the request
body contains. What a stub DOM structurally cannot see is what this script is for, and Epic 9's
record says exactly what that is worth: sliders drawn as 20px bordered pills, checkboxes drawn as
grey pills that squeezed the text out of their rows, a refusal note 130px wide and ten lines tall,
and a `STALE` label that never went away. **Every one of those shipped with a green suite, clean
ruff and passing contract tests, and every one was caught only by looking.**

So this script drives the real control in a real browser and measures the painted result: the
glyph's resolved colour in each state, the panel's own box against the 260px rail it lives in, the
number inputs against the 38px pill the generic `input` rule would otherwise give them, and every
sentence against the box painting it.

**No GPU is spent and nothing reaches ComfyUI.** The only writes are `PUT .../effects`,
`PUT .../effects/{index}/bindings` and `POST /song/analyze`, all of which touch a manifest and a
sidecar. ComfyUI is pointed at a dead port and never contacted.

What is driven, in order:

1. **The glyph is live on every row and dim on none of them by accident** -- present on a drivable
   parameter, on an undrivable one and on a look, in the tab order, never `disabled`, never hidden.
2. **A fresh panel opens beneath its own row**, with neither drive pressed, an empty depth box,
   both missing decisions said in full, and the sustain gate's own two timings drawn but inert
   with the reason under them -- and nothing written.
3. **The panel fits the rail.** Every control inside the inspector's own width, no horizontal
   overflow, no label clipped, and the number inputs shorter than the 38px the generic rule gives.
4. **The drive presses, and the panel says only what is left.** Still nothing written.
5. **The depth completes it and the write happens** -- one request, at the bindings route -- and
   the glyph turns `--blue`, resolved from the stylesheet rather than read off a class name.
5b. **`sustain` brings `hold` and `sustain` alive**, at the bounds `BINDING_SETTINGS` serves; a
   tuned hold reaches the manifest; and switching back to `punch` keeps it, greys it, and still
   shows it -- which is `BINDING_SETTINGS`' own promise and the only place it can be observed.
6. **An undrivable parameter refuses in the catalogue's own sentence**, with no band controls under
   it and no `[Analyze song]`, because it is a fact about ffmpeg and not about the song.
7. **A song whose analysis has gone leaves the binding lit and unresolvable**, says which absence
   it is, and offers the measurement -- and pressing it brings the binding back live.
8. **A locked Shot draws the panel readable with every writing control disabled.**
9. **Remove binding takes it off and leaves the parameter's own number where it was.**

Screenshots of every one of those states are written to `test-artifacts/`.

Run from the repo root -- it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_band_panel.py [--port 8779]

Assumes: nothing listening on the port, Microsoft Edge and its WebDriver installed, and
`music_video_producer` importable from this checkout's `src/`. ComfyUI does not need to be running.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_song_analysis import sidecar, targets, write_manifest
from e2e_support import (
    ManagedServer,
    artifact_dir,
    clipped,
    console_gate,
    edge_driver,
    get_json,
    put_json,
    report,
    settle,
    visible_and_clickable,
)
from e2e_timeline_edit import manifest, post_multipart_project, select_project
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

NAME = "band-panel"
SHOT = "shot_01"
LOCKED = "shot_02"

#: The bound parameter. Exposure is a Grade card, which is inside Epic 10's measured drivable
#: subset; Grain is in the same stack precisely so a *refusal* is on screen beside a working panel.
BIND = "effect-bind-0-amount"
GRAIN_BIND = "effect-bind-1-strength"


#: Everything about the panel a Director could see, measured rather than inferred: the resolved
#: colours, the painted boxes, and whether anything overflows the rail it lives in.
PANEL_STATE = """
const probe = document.createElement('span');
document.body.appendChild(probe);
const resolve = (token) => {
  probe.style.color = 'var(' + token + ')';
  return getComputedStyle(probe).color;
};
const palette = {
  blue: resolve('--blue'), dim: resolve('--dim'), muted: resolve('--muted'),
  red: resolve('--red'), redEdge: resolve('--red-edge'), acid: resolve('--acid'),
  lineStrong: resolve('--line-strong'),
};
probe.remove();
const rail = document.querySelector('#shot-inspector');
const box = (node) => {
  if (!node) return null;
  const rect = node.getBoundingClientRect();
  const style = getComputedStyle(node);
  return {
    width: Math.round(rect.width * 10) / 10, height: Math.round(rect.height * 10) / 10,
    left: Math.round(rect.left * 10) / 10, right: Math.round(rect.right * 10) / 10,
    top: Math.round(rect.top * 10) / 10, bottom: Math.round(rect.bottom * 10) / 10,
    colour: style.color, background: style.backgroundColor,
    borderLeft: style.borderLeftColor, borderLeftWidth: style.borderLeftWidth,
    fontSize: style.fontSize, display: style.display,
    text: (node.textContent || '').replace(/\\s+/g, ' ').trim(),
  };
};
const panel = document.querySelector('.effect-band');
const glyphs = [...document.querySelectorAll('.effect-bind')].map((node) => ({
  id: node.id,
  state: node.dataset.state,
  tag: node.tagName.toLowerCase(),
  disabled: node.disabled,
  hidden: node.getAttribute('aria-hidden'),
  expanded: node.getAttribute('aria-expanded'),
  label: node.getAttribute('aria-label'),
  colour: getComputedStyle(node).color,
  ...box(node),
}));
const inputs = panel ? [...panel.querySelectorAll('input')].map((node) => ({
  id: node.id, type: node.type, value: node.value, min: node.min, max: node.max,
  step: node.step, disabled: node.disabled, title: node.getAttribute('title'),
  ...box(node),
})) : [];
const modes = panel ? [...panel.querySelectorAll('.effect-band-mode')].map((node) => ({
  id: node.id, text: node.textContent.trim(),
  pressed: node.getAttribute('aria-pressed'), disabled: node.disabled,
  ...box(node),
})) : [];
const labels = panel ? [...panel.querySelectorAll('.effect-band-label')].map((node) => ({
  text: node.textContent.trim(),
  // The *painted* opacity of the row the label sits in, so "this pair reads as inert" is a
  // measurement rather than a screenshot somebody has to notice.
  rowOpacity: getComputedStyle(node.closest('.effect-band-row')).opacity,
  // A `label` is `display: grid` with a 12px bottom margin everywhere else in this application,
  // which here would stack the caption above its own input and space five rows an inch apart.
  display: getComputedStyle(node).display,
  marginBottom: getComputedStyle(node).marginBottom,
  clipped: node.scrollWidth > node.clientWidth + 1,
})) : [];
const sentence = (selector) => box(panel ? panel.querySelector(selector) : null);
return {
  palette,
  rail: box(rail),
  panel: box(panel),
  panelCount: document.querySelectorAll('.effect-band').length,
  glyphs, inputs, modes, labels,
  note: sentence('.effect-band-note'),
  gate: sentence('.effect-band-gate'),
  needs: sentence('.effect-band-needs'),
  strip: sentence('.effect-band-strip'),
  analyze: box(panel ? panel.querySelector('.effect-band-analyze') : null),
  remove: box(panel ? panel.querySelector('.effect-band-remove') : null),
  panelState: panel ? panel.dataset.state : null,
  // The lock is *not* a paragraph inside the panel -- it is the tab's own notice above the stack,
  // and each disabled control carries it as its reason. Both are read, because "the sentence is
  // said once" and "the sentence is said at all" are different claims.
  tabLock: box(document.querySelector('#effects-locked')),
  panelReasons: panel ? panel.querySelectorAll('.control-reason').length : 0,
  titles: panel
    ? [...panel.querySelectorAll('input, .effect-band-mode, .effect-band-remove')]
        .map((node) => ({ id: node.id, title: node.getAttribute('title') }))
    : [],
  // The rail must not scroll sideways: a panel wider than the inspector is the failure a stub DOM
  // cannot see at all, and it is how a 260px column ends up hiding half of every sentence.
  railScroll: rail ? rail.scrollWidth - rail.clientWidth : null,
  // How much of the panel sits below the rail's own fold. Seven boxes in a 250px column is real
  // density pressure and this is the number that says whether it has gone too far.
  panelBelowFold: panel && rail
    ? Math.max(0, Math.round(panel.getBoundingClientRect().bottom
        - rail.getBoundingClientRect().bottom))
    : null,
  railScrollHeight: rail ? rail.scrollHeight : null,
  railClientHeight: rail ? rail.clientHeight : null,
  // Every painted line inside the panel, so a note squeezed into a ten-line column is visible as
  // a number rather than as a screenshot somebody has to notice.
  noteLines: panel && panel.querySelector('.effect-band-note')
    ? panel.querySelector('.effect-band-note').getClientRects().length : 0,
  refusedRows: document.querySelectorAll('.effect-row-refused').length,
};
"""


def dead_port() -> int:
    """A port nothing is listening on, so the ComfyUI reads fail fast and change nothing."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def seed(base_url: str) -> str:
    """A measured song, two shots, and a stack carrying one drivable card and one refused one."""
    project_id = post_multipart_project(
        base_url, name="Band panel browser QA", clicks_per_minute=100.0
    )
    put_json(f"{base_url}/api/projects/{project_id}/shots", {"shots": [
        {"id": SHOT, "start": 0, "duration": 4, "prompt": "The rooftop, wide, at dusk.",
         "mode": "text_to_video", "status": "draft", "seed": 11},
        {"id": LOCKED, "start": 4, "duration": 4, "prompt": "The stairwell, handheld.",
         "mode": "text_to_video", "status": "draft", "seed": 12, "locked": True},
    ]})
    # Exposure first, so it is card 0 in storage *and* first in the grade run; Grain is the
    # texture card whose every parameter the catalogue refuses (R-25).
    put_json(f"{base_url}/api/projects/{project_id}/shots/{SHOT}/effects", {"effects": [
        {"effect": "exposure", "enabled": True, "parameters": {"amount": 0.2}},
        {"effect": "grain", "enabled": True, "parameters": {"strength": 8.0}},
    ]})
    return project_id


def stack(base_url: str, project_id: str, shot_id: str = SHOT) -> list[dict]:
    return get_json(f"{base_url}/api/projects/{project_id}/shots/{shot_id}/effects")["effects"]


def wait_for_stack(base_url: str, project_id: str, predicate, what: str,
                   shot_id: str = SHOT, timeout: float = 12.0) -> list[dict]:
    """The stored stack, once the write this gesture started has actually landed on disk.

    Polled rather than read once: `settle` watches the panel stop moving, and the panel stops
    moving the moment the reply is applied -- a different instant from the manifest being written.
    """
    deadline = time.time() + timeout
    held: list[dict] = []
    while time.time() < deadline:
        held = stack(base_url, project_id, shot_id)
        if predicate(held):
            return held
        time.sleep(0.2)
    raise AssertionError(f"{what}; the stored stack is {held}")


def select_clip(driver, wait, shot_id: str) -> None:
    settle(driver, "#shots-track")
    clip = wait.until(
        lambda browser: browser.find_element(
            By.CSS_SELECTOR, f'#shots-track .shot-clip[data-shot-id="{shot_id}"]'
        )
    )
    clip.click()
    wait.until(
        lambda browser: "selected" in browser.find_element(
            By.CSS_SELECTOR, f'#shots-track .shot-clip[data-shot-id="{shot_id}"]'
        ).get_attribute("class")
    )
    settle(driver, "#shot-inspector")


def open_effects(driver) -> None:
    tab = driver.find_element(By.ID, "shot-tab-effects")
    visible_and_clickable(driver, tab, "the Effects tab")
    tab.click()
    settle(driver, "#shot-inspector", quiet_ms=350)


def look(driver) -> dict:
    return driver.execute_script(PANEL_STATE)


def reach(driver, control: str):
    """Scroll one band control into the rail's view and hand it back.

    The panel is seven boxes and two sentences tall now, so its foot sits below the inspector's
    fold at this viewport -- which is exactly what a Director meets, and what made the first pass
    of this step fail with `element not interactable` rather than with an assertion. Scrolling is
    what a Director does; the *distance* is recorded in the result so the density is a number
    somebody can argue with rather than a screenshot somebody has to notice.
    """
    element = driver.find_element(By.ID, control)
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center', behavior: 'instant'});", element)
    visible_and_clickable(driver, element, f"the {control} control")
    return element


def type_into(driver, control: str, text: str) -> None:
    """Select the whole box and type over it, the way a Director replaces a number.

    **Not `clear()` then `send_keys`.** On a box that already holds a value, `clear()` fires its
    own `change` -- which on a complete binding is a real write, a reply, and a rebuilt panel, so
    the element the next keystroke was aimed at is detached by the time it lands. The browser
    reports that as `element not interactable`, which reads like a layout fault and is not one.
    Selecting and overtyping is one gesture and one write, which is also what actually happens.
    """
    element = reach(driver, control)
    element.send_keys(Keys.CONTROL, "a")
    element.send_keys(text)


def shot(driver, state: str) -> None:
    driver.find_element(By.ID, "shot-inspector").screenshot(
        str(artifact_dir() / f"{NAME}-{state}.png"))


def glyph(state: dict, control: str) -> dict:
    found = [item for item in state["glyphs"] if item["id"] == control]
    assert found, (control, [item["id"] for item in state["glyphs"]])
    return found[0]


def main() -> None:
    port = 8779
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    os.environ["MVP_COMFY_URL"] = f"http://127.0.0.1:{dead_port()}"

    result: dict[str, object] = {}
    with ManagedServer(port, label=NAME) as server:
        result["server_identity"] = server.evidence
        project_id = seed(server.base_url)
        measured = targets(server, project_id)
        assert measured["analysed"] is True, ("the click track measured nothing", measured)
        assert measured["reason"] == "", measured
        result["seeded"] = {"analysed": measured["analysed"], "beats": len(measured["beats"])}

        driver = edge_driver()
        wait = WebDriverWait(driver, 30)
        try:
            driver.get(server.base_url)
            select_project(driver, wait, project_id)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            select_clip(driver, wait, SHOT)
            open_effects(driver)

            # --- 1. The glyph is live on every row, and never hidden --------------------------
            closed = look(driver)
            assert closed["panelCount"] == 0, closed
            assert len(closed["glyphs"]) == 3, [item["id"] for item in closed["glyphs"]]
            for item in closed["glyphs"]:
                assert item["tag"] == "button", item
                assert item["disabled"] is False, item
                assert item["hidden"] is None, ("the glyph is still hidden from the tree", item)
                assert item["label"], ("the glyph has no accessible name", item)
                assert item["expanded"] == "false", item
                assert item["colour"] == closed["palette"]["dim"], item
                visible_and_clickable(
                    driver, driver.find_element(By.ID, item["id"]), f"the {item['id']} glyph")
            assert {item["state"] for item in closed["glyphs"]} == {"free", "undrivable"}, closed
            result["glyphs_before_binding"] = [
                {k: item[k] for k in ("id", "state", "colour", "width", "height")}
                for item in closed["glyphs"]
            ]
            shot(driver, "01-glyphs-closed")

            # --- 2. A fresh panel: no drive, no depth, both said, nothing written -------------
            driver.find_element(By.ID, BIND).click()
            settle(driver, "#shot-inspector", quiet_ms=350)
            fresh = look(driver)
            assert fresh["panelCount"] == 1, fresh
            assert glyph(fresh, BIND)["expanded"] == "true", fresh["glyphs"]
            assert [item["pressed"] for item in fresh["modes"]] == ["false", "false"], fresh["modes"]
            depth = next(item for item in fresh["inputs"] if item["id"].endswith("-depth"))
            assert depth["value"] == "", ("a depth was invented for the Director", depth)
            assert "Choose punch or sustain" in fresh["needs"]["text"], fresh["needs"]
            assert "Set a depth" in fresh["needs"]["text"], fresh["needs"]
            assert "untouched" in fresh["needs"]["text"], fresh["needs"]
            assert stack(server.base_url, project_id)[0].get("bindings") in (None, []), (
                "opening a panel wrote a binding")

            # --- 3. It fits the rail, and nothing inherits the generic control chrome ---------
            assert fresh["railScroll"] == 0, ("the inspector scrolls sideways", fresh)
            assert fresh["panel"]["right"] <= fresh["rail"]["right"] + 1, fresh
            # A panel that *is* the reactive surface takes the seventh accent, and only that.
            assert fresh["panelState"] == "free", fresh["panelState"]
            assert fresh["panel"]["borderLeft"] == fresh["palette"]["blue"], (
                "the panel's left edge is not --blue", fresh["panel"])
            assert fresh["panel"]["borderLeftWidth"] == "2px", fresh["panel"]
            for item in fresh["inputs"]:
                assert item["right"] <= fresh["panel"]["right"] + 1, item
                # The generic `input` rule gives every input in this application a 38px height and
                # 9px of padding. Five of those in a 260px rail is a panel taller than its card --
                # the same inheritance that drew a slider as a 20px bordered pill in Epic 9.
                assert item["height"] <= 28, ("a band input inherited the 38px form control", item)
                assert item["width"] > 40, ("a band input is too narrow to read", item)
            # **The sustain gate's own two timings, drawn and inert.** Story 10.1 says `sustain`
            # engages after a hold time and survives dips for a sustain time; a Director who could
            # not reach either had a drive with two numbers they could not choose. They are drawn
            # in every state rather than appearing when the drive is picked -- a control that
            # materialises under a Director moves the row shape they are working in.
            timings = {item["id"].rsplit("-", 1)[-1]: item
                       for item in fresh["inputs"] if item["id"].endswith(("-hold", "-sustain"))}
            assert set(timings) == {"hold", "sustain"}, [item["id"] for item in fresh["inputs"]]
            for name, item in timings.items():
                assert item["disabled"] is True, (name, item)
                assert item["title"] and "sustain drive" in item["title"], (name, item)
            assert fresh["gate"] and "read only by the sustain drive" in fresh["gate"]["text"], (
                fresh["gate"])
            # Nothing has been set on a fresh binding, so there is nothing to promise to keep --
            # and that clause was four of the five lines the first looking pass measured as prose.
            assert "switching back does not lose it" not in fresh["gate"]["text"], fresh["gate"]
            # The pair reads as inert *as a row*, not merely as two dimmer numbers: dimming the box
            # alone left the labels at the working rows' weight and the pair read as live.
            rows = {item["text"]: item["rowOpacity"] for item in fresh["labels"]}
            assert float(rows["Hold"]) < 0.8 and float(rows["Sustain"]) < 0.8, rows
            assert float(rows["Floor"]) == 1.0 and float(rows["Centre"]) == 1.0, rows
            assert fresh["gate"]["colour"] == fresh["palette"]["muted"], fresh["gate"]
            # Drawn last, under the pair they are about, so the sentence is read where the dead
            # boxes are rather than at the top of a panel the Director has scrolled past.
            assert fresh["gate"]["top"] > timings["sustain"]["top"], (fresh["gate"], timings)

            for item in fresh["labels"]:
                assert item["display"] == "block", (
                    "a band label inherited the generic `label { display: grid }`", item)
                assert item["marginBottom"] == "0px", item
                assert item["clipped"] is False, ("a band label is cut off", item)
            for item in fresh["modes"]:
                visible_and_clickable(
                    driver, driver.find_element(By.ID, item["id"]), f"the {item['text']} drive")
            # The sentences are readable at this width rather than a column of single words.
            assert fresh["needs"]["width"] > 150, ("the needs block is a narrow column", fresh)
            assert fresh["strip"]["text"], "the panel does not say the strip is still to come"
            result["fresh_panel"] = {
                "panel": fresh["panel"], "inputs": fresh["inputs"], "modes": fresh["modes"],
                "needs": fresh["needs"], "gate": fresh["gate"],
                "railScroll": fresh["railScroll"], "panelBelowFold": fresh["panelBelowFold"],
                "railScrollHeight": fresh["railScrollHeight"],
                "railClientHeight": fresh["railClientHeight"],
            }
            shot(driver, "02-fresh-panel")

            # --- 4. The drive alone still writes nothing -------------------------------------
            punch = next(item for item in fresh["modes"] if item["text"] == "punch")
            driver.find_element(By.ID, punch["id"]).click()
            settle(driver, "#shot-inspector", quiet_ms=350)
            chosen = look(driver)
            assert [item["pressed"] for item in chosen["modes"]] == ["true", "false"], chosen["modes"]
            assert "Choose punch or sustain" not in chosen["needs"]["text"], chosen["needs"]
            assert "Set a depth" in chosen["needs"]["text"], chosen["needs"]
            assert chosen["modes"][0]["borderLeft"] == chosen["palette"]["blue"], chosen["modes"][0]
            assert stack(server.base_url, project_id)[0].get("bindings") in (None, []), (
                "the drive press alone wrote a binding at a depth nobody chose")
            result["after_the_drive"] = {"modes": chosen["modes"], "needs": chosen["needs"]}
            shot(driver, "03-drive-chosen")

            # --- 5. The depth completes it, and the glyph turns --blue -----------------------
            type_into(driver, "effect-band-0-amount-depth", "0.5")
            driver.find_element(By.ID, "shot-tab-effects").click()
            settle(driver, "#shot-inspector", quiet_ms=500)
            held = wait_for_stack(
                server.base_url, project_id,
                lambda entries: bool(entries[0].get("bindings")),
                "the depth did not write a binding",
            )
            assert held[0]["bindings"] == [
                {"parameter": "amount", "drive": "punch", "depth": 0.5}
            ], held[0]["bindings"]
            open_effects(driver)
            bound = look(driver)
            assert glyph(bound, BIND)["state"] == "bound", bound["glyphs"]
            assert glyph(bound, BIND)["colour"] == bound["palette"]["blue"], (
                "a bound parameter's glyph is not the seventh accent", glyph(bound, BIND))
            assert glyph(bound, GRAIN_BIND)["colour"] == bound["palette"]["dim"], (
                "an unbound glyph took the reactive colour", glyph(bound, GRAIN_BIND))
            result["written_binding"] = {
                "stored": held[0]["bindings"],
                "glyph": {k: glyph(bound, BIND)[k] for k in ("state", "colour", "label")},
            }
            shot(driver, "04-bound")

            # The panel is still open and now shows what was stored, with a way to take it off.
            assert bound["panelCount"] == 1, bound
            assert bound["remove"] and "Remove binding" in bound["remove"]["text"], bound["remove"]
            assert bound["needs"]["height"] == 0 or not bound["needs"]["text"], bound["needs"]

            # --- 5b. `sustain` brings its own two timings alive, and `punch` keeps them ------
            #
            # Story 10.1: `sustain` engages after a hold time and survives dips for a sustain
            # time. Both are drawn in every state and inert under the drive that does not read
            # them; choosing `sustain` is what makes them reachable, and going back to `punch`
            # must not lose what was set -- which is `BINDING_SETTINGS`' own promise.
            reach(driver, "effect-band-0-amount-drive-sustain").click()
            settle(driver, "#shot-inspector", quiet_ms=500)
            wait_for_stack(
                server.base_url, project_id,
                lambda entries: entries[0]["bindings"][0]["drive"] == "sustain",
                "choosing sustain did not write the drive",
            )
            gated = look(driver)
            timings = {item["id"].rsplit("-", 1)[-1]: item
                       for item in gated["inputs"] if item["id"].endswith(("-hold", "-sustain"))}
            assert set(timings) == {"hold", "sustain"}, gated["inputs"]
            for name, item in timings.items():
                assert item["disabled"] is False, (name, item)
                visible_and_clickable(
                    driver, driver.find_element(By.ID, item["id"]), f"the {name} timing")
            # And they read as live: the row comes back to full weight with the box.
            alive = {item["text"]: item["rowOpacity"] for item in gated["labels"]}
            assert float(alive["Hold"]) == 1.0 and float(alive["Sustain"]) == 1.0, alive
            # The sentence about them is gone, because a panel narrating a working control is
            # noise -- and its absence is what makes it a statement rather than decoration.
            assert gated["gate"] is None, ("the inert sentence outlived the state", gated["gate"])
            # At the served defaults until a Director says otherwise: 0.8s and 1.5s.
            assert float(timings["hold"]["value"]) == 0.8, timings["hold"]
            assert float(timings["sustain"]["value"]) == 1.5, timings["sustain"]
            assert (float(timings["hold"]["min"]), float(timings["hold"]["max"])) == (0.0, 10.0)
            assert (float(timings["sustain"]["min"]),
                    float(timings["sustain"]["max"])) == (0.0, 20.0)
            result["sustain_timings"] = timings
            shot(driver, "04b-sustain-timings")

            type_into(driver, "effect-band-0-amount-hold", "2.5")
            driver.find_element(By.ID, "shot-tab-effects").click()
            settle(driver, "#shot-inspector", quiet_ms=500)
            tuned = wait_for_stack(
                server.base_url, project_id,
                lambda entries: entries[0]["bindings"][0].get("hold") == 2.5,
                "the hold time did not reach the manifest",
            )
            assert tuned[0]["bindings"] == [{
                "parameter": "amount", "drive": "sustain", "depth": 0.5, "hold": 2.5,
            }], tuned[0]["bindings"]
            # `sustain` at its default is still omitted, so the sparse write survives the gate.
            assert "sustain" not in tuned[0]["bindings"][0], tuned[0]["bindings"]

            open_effects(driver)
            reach(driver, "effect-band-0-amount-drive-punch").click()
            settle(driver, "#shot-inspector", quiet_ms=500)
            kept = wait_for_stack(
                server.base_url, project_id,
                lambda entries: entries[0]["bindings"][0]["drive"] == "punch",
                "switching back to punch did not write",
            )
            assert kept[0]["bindings"][0].get("hold") == 2.5, (
                "switching drive lost a timing the Director set", kept[0]["bindings"])
            back = look(driver)
            inert = {item["id"].rsplit("-", 1)[-1]: item
                     for item in back["inputs"] if item["id"].endswith(("-hold", "-sustain"))}
            assert all(item["disabled"] for item in inert.values()), inert
            assert float(inert["hold"]["value"]) == 2.5, (
                "the kept timing is not shown, so a Director cannot see it was kept", inert)
            # The reassurance is *earned* here and only here: something was set under `sustain`,
            # so the sentence about keeping it is about something. It is absent on a fresh panel.
            assert back["gate"], back
            assert "read only by the sustain drive" in back["gate"]["text"], back["gate"]
            assert "switching back does not lose it" in back["gate"]["text"], back["gate"]
            result["timing_kept_through_punch"] = {
                "stored": kept[0]["bindings"], "shown": inert["hold"]["value"],
            }
            shot(driver, "04c-punch-keeps-them")

            # Back to where step 6 expects it: bound on punch, at the depth it was written with.

            # --- 6. An undrivable parameter refuses in the catalogue's own sentence ----------
            driver.find_element(By.ID, GRAIN_BIND).click()
            settle(driver, "#shot-inspector", quiet_ms=350)
            refused = look(driver)
            assert refused["panelCount"] == 1, ("two band panels are open at once", refused)
            assert glyph(refused, GRAIN_BIND)["state"] == "undrivable", refused["glyphs"]
            assert refused["note"]["text"] == (
                "Strength cannot be driven by the music: ffmpeg's noise filter takes no runtime "
                "commands."
            ), refused["note"]
            assert refused["modes"] == [] and refused["inputs"] == [], refused
            assert refused["analyze"] is None, (
                "an ffmpeg fact was offered a song measurement as its remedy", refused)
            # Not an error: the refusal takes the inert weight, never --red or the red edge.
            assert refused["note"]["colour"] == refused["palette"]["muted"], refused["note"]
            # **And not the seventh accent either.** `--blue` means *reactive*, and a panel saying
            # this parameter can never be reactive must not wear it -- the exact overloading
            # DESIGN 1 closed the palette against. Caught by looking on the first pass.
            assert refused["panelState"] == "undrivable", refused["panelState"]
            assert refused["panel"]["borderLeft"] != refused["palette"]["blue"], refused["panel"]
            assert refused["panel"]["borderLeft"] == refused["palette"]["lineStrong"], (
                refused["panel"], refused["palette"])
            # And the sentence is readable rather than a 130px column ten lines tall -- the Epic 9
            # defect this measurement exists because of.
            assert refused["note"]["width"] > 150, refused["note"]
            assert refused["noteLines"] <= 4, ("the refusal is a narrow column", refused)
            result["undrivable"] = {"note": refused["note"], "lines": refused["noteLines"]}
            shot(driver, "05-undrivable")

            # And the first panel closed when this one opened.
            assert glyph(refused, BIND)["expanded"] == "false", refused["glyphs"]

            # --- 7. The analysis goes away: retained, unresolvable, and the way back ---------
            stored = manifest(server, project_id)
            sidecar(server, project_id, stored["song"]["analysis"]["path"]).unlink()
            stored["song"]["analysis"] = {}
            write_manifest(server, project_id, stored)
            absent = targets(server, project_id)
            assert absent["analysed"] is False and absent["reason"], absent
            driver.refresh()
            select_project(driver, wait, project_id)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            select_clip(driver, wait, SHOT)
            open_effects(driver)
            driver.find_element(By.ID, BIND).click()
            settle(driver, "#shot-inspector", quiet_ms=350)
            stale = look(driver)
            assert glyph(stale, BIND)["state"] == "unresolvable", stale["glyphs"]
            assert glyph(stale, BIND)["colour"] == stale["palette"]["blue"], (
                "an unresolvable binding was quietly disowned", glyph(stale, BIND))
            assert absent["reason"] in stale["note"]["text"], (stale["note"], absent["reason"])
            assert "nothing is dropped or zeroed" in stale["note"]["text"], stale["note"]
            assert stale["analyze"] and "Analyze song" in stale["analyze"]["text"], stale["analyze"]
            assert stale["refusedRows"] == 0, ("a stale envelope is drawn as a fault", stale)
            result["unresolvable"] = {
                "served_reason": absent["reason"], "note": stale["note"],
                "glyph": {k: glyph(stale, BIND)[k] for k in ("state", "colour")},
            }
            shot(driver, "06-unresolvable")

            analyze = driver.find_element(By.CSS_SELECTOR, ".effect-band-analyze")
            visible_and_clickable(driver, analyze, "the band panel's Analyze song")
            assert clipped(driver, analyze) is False, "the Analyze song label is cut off"
            analyze.click()
            wait.until(
                lambda browser: browser.execute_script(
                    "const node = document.querySelector('.effect-bind[data-state=\\'bound\\']');"
                    "return Boolean(node);"
                ),
                "re-analysing did not bring the binding back live",
            )
            settle(driver, "#shot-inspector", quiet_ms=500)
            live = look(driver)
            assert glyph(live, BIND)["state"] == "bound", live["glyphs"]
            # Its stored values came back intact -- nothing was dropped or zeroed.
            revived = stack(server.base_url, project_id)[0]["bindings"]
            # Including the hold time set under `sustain` and kept across the switch to `punch`
            # -- an envelope that went away and came back must not be the thing that loses it.
            assert revived == [
                {"parameter": "amount", "drive": "punch", "depth": 0.5, "hold": 2.5}
            ], revived
            result["after_re_analysis"] = {"bindings": revived, "state": glyph(live, BIND)["state"]}
            shot(driver, "07-live-again")

            # --- 8. A locked Shot: readable, and every writing control disabled --------------
            #
            # Bound while unlocked and then locked, because the lock is exactly what the routes
            # refuse -- both of them, in the same sentence -- which is the half of FX-7 that is
            # already a guard and is not what this step is about.
            def relock(locked: bool) -> None:
                held = get_json(f"{server.base_url}/api/projects/{project_id}")
                for entry in held["shots"]:
                    if entry["id"] == LOCKED:
                        entry["locked"] = locked
                put_json(f"{server.base_url}/api/projects/{project_id}/shots",
                         {"shots": held["shots"]})

            relock(False)
            put_json(f"{server.base_url}/api/projects/{project_id}/shots/{LOCKED}/effects",
                     {"effects": [{"effect": "exposure", "enabled": True,
                                   "parameters": {"amount": 0.3}}]})
            put_json(
                f"{server.base_url}/api/projects/{project_id}/shots/{LOCKED}/effects/0/bindings",
                {"effect": "exposure", "bindings": [
                    {"parameter": "amount", "drive": "sustain", "depth": -0.4}]},
            )
            relock(True)
            driver.refresh()
            select_project(driver, wait, project_id)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            select_clip(driver, wait, LOCKED)
            open_effects(driver)
            driver.find_element(By.ID, BIND).click()
            settle(driver, "#shot-inspector", quiet_ms=350)
            locked = look(driver)
            assert locked["panelCount"] == 1, locked
            assert glyph(locked, BIND)["disabled"] is False, (
                "a locked Shot's binding cannot be read", glyph(locked, BIND))
            assert all(item["disabled"] for item in locked["inputs"]), locked["inputs"]
            assert all(item["disabled"] for item in locked["modes"]), locked["modes"]
            # Said once for the tab, and carried on each dead control as its reason -- not printed
            # a second time inside the panel, which is what browser QA caught on the first pass.
            assert locked["tabLock"] and "locked" in locked["tabLock"]["text"], locked["tabLock"]
            assert locked["panelReasons"] == 0, (
                "the lock sentence is printed twice on the same screen", locked)
            assert locked["titles"], locked
            for item in locked["titles"]:
                assert item["title"] and "locked" in item["title"], item
            # The stored values are still readable, which is the point of drawing it at all.
            values = {item["id"].rsplit("-", 1)[-1]: item["value"] for item in locked["inputs"]}
            assert values["depth"] == "-0.4", values
            assert [item["pressed"] for item in locked["modes"]] == ["false", "true"], locked["modes"]
            result["locked"] = {"inputs": locked["inputs"], "modes": locked["modes"],
                                "tab_notice": locked["tabLock"], "titles": locked["titles"]}
            shot(driver, "08-locked")

            # --- 9. Remove binding, and the parameter stays where it was ---------------------
            select_clip(driver, wait, SHOT)
            open_effects(driver)
            driver.find_element(By.ID, BIND).click()
            settle(driver, "#shot-inspector", quiet_ms=350)
            remove = driver.find_element(By.CSS_SELECTOR, ".effect-band-remove")
            visible_and_clickable(driver, remove, "Remove binding")
            remove.click()
            gone = wait_for_stack(
                server.base_url, project_id,
                lambda entries: not entries[0].get("bindings"),
                "Remove binding left something behind",
            )
            assert gone[0]["parameters"] == {"amount": 0.2}, (
                "removing the binding moved the parameter's own value", gone[0])
            assert "bindings" not in gone[0] or gone[0]["bindings"] == [], gone[0]
            settle(driver, "#shot-inspector", quiet_ms=350)
            after = look(driver)
            assert glyph(after, BIND)["state"] == "free", after["glyphs"]
            assert glyph(after, BIND)["colour"] == after["palette"]["dim"], glyph(after, BIND)
            assert after["remove"] is None, "the removal control outlived the binding"
            result["removed"] = {"stack": gone[0], "glyph": glyph(after, BIND)["state"]}
            shot(driver, "09-removed")

            driver.save_screenshot(str(artifact_dir() / f"{NAME}-workspace.png"))
            console_gate(driver, NAME, result)
        finally:
            driver.quit()

    result["artifacts"] = sorted(
        path.name for path in artifact_dir().glob(f"{NAME}-*") if path.is_file()
    )
    report(NAME, result)
    print(json.dumps({"ok": True, "screenshots": result["artifacts"]}, indent=2))


if __name__ == "__main__":
    main()
