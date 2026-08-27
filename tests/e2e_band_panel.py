"""Browser QA for the band panel and its spectrum strip (Epic 10, slices E3 and E4).

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

And, added by slice E4 as steps 7b and 7c:

7b. **The spectrum strip is drawn, and drawn in both tokens** -- the census below counts the
   painted pixels of a real canvas and separates the `--dim` spectrum from the `--blue` band, so
   "the strip is there" is a number rather than a screenshot somebody has to squint at. A drag on
   its body then writes **once, on release**, and the three numeric boxes read the dragged band
   the whole way through.
7c. **At `band_width`'s minimum the region is under four pixels across**, and every gesture is
   still reachable: both edge handles are painted and both still resize. Take the softness to
   zero as well and its handle has no ground left -- it is withdrawn and the panel names the box
   that still sets it.

The panel's height and its distance below the rail's fold are recorded in both the unbound and
the bound states, because the strip is 36px added to a panel that was already 503.6px in a 626px
rail with 212px of it below the fold.

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
from selenium.webdriver import ActionChains
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
const strip = panel ? panel.querySelector('.effect-band-strip') : null;
const painted = (() => {
  if (!strip || !strip.getContext || !strip.width) return null;
  const data = strip.getContext('2d').getImageData(0, 0, strip.width, strip.height).data;
  let lit = 0;
  let blue = 0;
  let dim = 0;
  for (let index = 0; index < data.length; index += 4) {
    const [r, g, b, a] = [data[index], data[index + 1], data[index + 2], data[index + 3]];
    if (!a) continue;
    lit += 1;
    if (b - r > 30) blue += 1;
    else if (Math.abs(r - g) < 12 && Math.abs(g - b) < 12) dim += 1;
  }
  return { width: strip.width, height: strip.height, lit, blue, dim };
})();
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
  readout: sentence('.effect-band-readout'),
  // The Drive readout, which is **not** in this rail: it is a figure under the Monitor, in the
  // other half of the timeline panel. Measured here because the two are one feature -- the panel
  // points at it and it draws the panel's binding -- and because "the canvas is drawn" is only a
  // fact if the pixels are counted. A canvas that threw halfway through, or one measured at zero
  // width inside a `hidden` figure, is a correctly-sized empty box that every structural
  // assertion in this file would pass over.
  drive: (() => {
    const figure = document.querySelector('#drive-readout');
    const canvas = document.querySelector('#drive-readout-canvas');
    const monitor = document.querySelector('#timeline-monitor');
    const main = document.querySelector('.timeline-main');
    const painted = (() => {
      if (!canvas || !canvas.getContext || !canvas.width) return null;
      const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
      let lit = 0, blue = 0, dim = 0, acid = 0, rest = 0, silenced = 0, ground = 0;
      for (let index = 0; index < data.length; index += 4) {
        const [r, g, b, a] = [data[index], data[index+1], data[index+2], data[index+3]];
        if (!a) continue;
        const row = Math.floor((index / 4) / canvas.width);
        lit += 1;
        if (g - b > 60) acid += 1;
        else if (b - r > 30) blue += 1;
        else if (Math.abs(r - g) < 12 && Math.abs(g - b) < 12) {
          dim += 1;
          // The rest line lives in the bottom two rows and runs the whole width. Counted apart
          // from the dim *above* it, because they are two different claims: the hairline is the
          // datum a silenced run is read against, and a silenced run is a passage the Trigger
          // Floor shut. A census that added them together could not tell a drawing that lost the
          // line from one that lost the silence.
          if (row >= canvas.height - 2) rest += 1;
          else silenced += 1;
          // The ground bar's own row, counted on its own. A silenced *transition* paints a dim
          // wedge here for a handful of columns whatever else is drawn, so a census that only
          // counted dim-above-the-line could not tell the ground bar from that wedge -- and
          // removing the bar entirely left every assertion in this file passing. A long passage
          // the floor shut has no height of its own to draw (a `punch` drive below the floor is
          // exactly zero), so this row is the only evidence that it is marked at all.
          if (row === canvas.height - 4) ground += 1;
        }
      }
      return { width: canvas.width, height: canvas.height, lit, blue, dim, acid, rest, silenced, ground };
    })();
    return {
      hidden: figure ? figure.hasAttribute('hidden') : null,
      label: figure ? figure.getAttribute('aria-label') : null,
      figure: box(figure),
      canvas: box(canvas),
      canvasHidden: canvas ? canvas.getAttribute('aria-hidden') : null,
      caption: box(document.querySelector('#drive-readout-caption')),
      monitor: box(monitor),
      main: box(main),
      rows: main ? getComputedStyle(main).gridTemplateRows : '',
      painted,
    };
  })(),
  crowded: sentence('.effect-band-crowded'),
  undrawn: sentence('.effect-band-undrawn'),
  strip: box(strip),
  stripTitle: strip ? strip.getAttribute('title') : null,
  stripHidden: strip ? strip.getAttribute('aria-hidden') : null,
  // **A census of the painted canvas**, which is the only way a drawing is verifiable at all: a
  // canvas that threw halfway through, or one measured at zero width inside a hidden tab, is a
  // correctly-sized empty box that every structural assertion in this file would pass. `dim`
  // counts the spectrum's own bars and `blue` the band drawn over them, told apart by channel
  // rather than by token string, because what is on screen is blended pixels and not a variable.
  painted,
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


#: The strip's geometry, asked of **the same pure function the page asks** rather than worked out
#: again here. `api.js` is a module this server already serves, so the browser imports it and
#: answers where this canvas's handles are and which ground each of them owns -- at the width the
#: canvas is really painted at. A harness that recomputed the axis for itself would be a second
#: implementation of the thing under test, and would agree with a wrong one.
STRIP_PLAN = """
const [bands, edges, settings, values, done] = arguments;
const node = document.querySelector('.effect-band-strip');
if (!node) { done(null); }
else {
  const rect = node.getBoundingClientRect();
  import('/assets/api.js').then((api) => {
    const plan = api.effectBandStripPlan({
      bands, edges, settings, values, width: rect.width, height: rect.height,
    });
    done({
      width: rect.width, height: rect.height, left: rect.left, top: rect.top,
      count: plan.count, band: plan.band, note: plan.note, hz: plan.hz,
      handles: plan.handles, targets: plan.targets,
    });
  }).catch((error) => done({ error: String(error) }));
}
"""


def strip_geometry(driver, envelope: dict, settings: list[dict], values: dict) -> dict:
    """Where this canvas's handles are, and what ground each of them owns."""
    plan = driver.execute_async_script(
        STRIP_PLAN,
        envelope.get("band_average") or [],
        envelope.get("band_edges") or [],
        settings,
        values,
    )
    assert plan and not plan.get("error"), plan
    return plan


def band_values(state: dict) -> dict:
    """The three band numbers as the panel is showing them, read off the boxes themselves.

    From the inputs rather than from the manifest on purpose: the claim being checked is that the
    canvas and the boxes are one band, and a value read back from storage would prove only that
    storage agrees with itself.
    """
    values = {}
    for item in state["inputs"]:
        name = item["id"].replace("effect-band-0-amount-", "")
        if name in ("band_centre", "band_width", "band_softness"):
            values[name] = float(item["value"])
    return values


def free_ground(plan: dict) -> float:
    """An x on the strip that belongs to the body drag and to no handle.

    Swept rather than assumed: which ground is free depends on where the band is, and an x picked
    by eye would silently become a handle press the first time a default moved.
    """
    for x in range(int(plan["width"]) - 2, 1, -1):
        if not any(target["from"] <= x <= target["to"] for target in plan["targets"]):
            return float(x)
    raise AssertionError(("no pixel of the strip is free of a handle", plan["targets"]))


class StripPointer:
    """A pointer on the strip, holding its own position.

    `move_by_offset` is relative, so a drag that has lost track of where it is releases on a band
    nobody chose -- which would read as a defect in the gesture rather than in this script.
    Offsets are given from the element's in-view centre, which is what Selenium 4 means by them.
    """

    def __init__(self, driver, canvas, plan: dict) -> None:
        self.driver = driver
        self.canvas = canvas
        self.plan = plan
        self.x = 0.0

    def press(self, x: float) -> None:
        self.x = float(x)
        ActionChains(self.driver).move_to_element_with_offset(
            self.canvas, round(x - self.plan["width"] / 2), 0
        ).click_and_hold().perform()

    def drag_to(self, x: float) -> None:
        ActionChains(self.driver).move_by_offset(round(x - self.x), 0).perform()
        self.x = float(x)

    def release(self) -> None:
        ActionChains(self.driver).release().perform()


def shot(driver, state: str) -> None:
    driver.find_element(By.ID, "shot-inspector").screenshot(
        str(artifact_dir() / f"{NAME}-{state}.png"))


def readout_shot(driver, state: str) -> None:
    """The Drive readout on its own — the canvas and its caption. A 34px strip inside a whole-panel
    screenshot is not something a person can judge a drawing from, and this drawing is the slice."""
    driver.find_element(By.ID, "drive-readout").screenshot(
        str(artifact_dir() / f"{NAME}-{state}.png"))


def monitor_shot(driver, state: str) -> None:
    """The Monitor with the readout under it, which is the layout question this slice raises: a
    fourth row in `.timeline-main` takes its height from the picture and the tracks."""
    driver.find_element(By.CSS_SELECTOR, ".timeline-main").screenshot(
        str(artifact_dir() / f"{NAME}-{state}.png"))


def strip_shot(driver, state: str) -> None:
    """The canvas on its own, because 183x36 inside a 280px panel screenshot is not something a
    person can judge a drawing from -- and this drawing is the whole slice."""
    driver.find_element(By.CSS_SELECTOR, ".effect-band-strip").screenshot(
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
            assert fresh["readout"] is None, (
                "an unwritten binding points at a readout that compiles nothing", fresh["readout"])
            assert fresh["drive"]["hidden"] is True, (
                "the readout is drawn for a shot whose binding is not written", fresh["drive"])
            # The strip is drawn on a fresh, unwritten binding too: the Band is a thing a Director
            # looks at *while* deciding, not a picture that appears once it has been decided.
            assert fresh["strip"] and fresh["painted"], (
                "the strip is absent on a fresh panel", fresh["strip"], fresh["painted"])
            result["fresh_panel"] = {
                "panel": fresh["panel"], "inputs": fresh["inputs"], "modes": fresh["modes"],
                "needs": fresh["needs"], "gate": fresh["gate"],
                "strip": fresh["strip"], "painted": fresh["painted"],
                "railScroll": fresh["railScroll"], "panelBelowFold": fresh["panelBelowFold"],
                "railScrollHeight": fresh["railScrollHeight"],
                "railClientHeight": fresh["railClientHeight"],
            }
            shot(driver, "02-fresh-panel")
            strip_shot(driver, "02-strip-canvas-fresh")

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
            # No readout yet: the binding is not written, so nothing is compiled to draw.
            assert chosen["drive"]["hidden"] is True, chosen["drive"]
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


            # --- 5c. The Drive readout, under the Monitor (story 10.3) ----------------------
            #
            # The panel above is where the binding is made; this is where it is *seen*. The census
            # is the point, exactly as it is for the strip: a canvas that threw halfway through
            # drawing, or one measured at zero width inside a figure that was still `hidden` when
            # it was painted, is a correctly-sized empty box and every structural assertion here
            # would pass over it. So the pixels are counted and the three tokens are told apart.
            drawn_readout = look(driver)["drive"]
            assert drawn_readout["hidden"] is False, (
                "the readout is not drawn for a bound shot", drawn_readout)
            assert drawn_readout["canvasHidden"] == "true", (
                "the canvas is in the accessibility tree, where it announces nothing (UX-DR15)",
                drawn_readout["canvasHidden"])
            assert drawn_readout["painted"], "nothing is painted on the readout"
            # Both halves of the picture: the envelope in `--blue`, and the rest line in `--dim`
            # running the whole width beneath it. Either alone is a drawing that has lost what it
            # exists to show — and the line is counted separately from the dim *above* it precisely
            # because the first pass drew the line **before** the envelope's fill and had it
            # painted out along its whole width, which a single `dim > 50` would have passed.
            assert drawn_readout["painted"]["blue"] > 50, drawn_readout["painted"]
            assert drawn_readout["painted"]["rest"] > drawn_readout["canvas"]["width"] * 0.8, (
                "the rest line is missing or painted over", drawn_readout["painted"])
            # Nothing is silenced yet: this binding's floor is 0, so the gate never shuts and no
            # ground is laid. A handful of pixels is the blue stroke and the acid line blending to
            # something grey where they cross; a real silenced run is hundreds of pixels of band.
            assert drawn_readout["painted"]["silenced"] < 20, drawn_readout["painted"]
            assert drawn_readout["painted"]["ground"] < 20, drawn_readout["painted"]
            # It sits immediately beneath the Monitor and spans the same width, so the envelope and
            # the picture read against one axis rather than two.
            assert drawn_readout["figure"]["top"] >= drawn_readout["monitor"]["bottom"] - 1, (
                drawn_readout["figure"], drawn_readout["monitor"])
            assert abs(drawn_readout["figure"]["width"]
                       - drawn_readout["monitor"]["width"]) < 2, drawn_readout
            assert drawn_readout["figure"]["top"] - drawn_readout["monitor"]["bottom"] < 4, (
                "the readout is not immediately beneath the Monitor", drawn_readout)
            # The canvas's non-canvas equivalent, immediately under it and carrying every fact it
            # draws: which binding, and where the drive peaks or that it never fires (UX-DR7).
            caption = drawn_readout["caption"]["text"]
            assert "Exposure" in caption and "Amount" in caption, (
                "the readout does not say which binding it is drawing", caption)
            assert ("peaks" in caption) or ("never rises" in caption), caption
            assert drawn_readout["caption"]["top"] >= drawn_readout["canvas"]["bottom"] - 1, (
                drawn_readout["caption"], drawn_readout["canvas"])
            assert not clipped(driver, driver.find_element(By.ID, "drive-readout-caption")), (
                "the readout's caption is cut off", drawn_readout["caption"])
            result["readout"] = {
                "figure": drawn_readout["figure"], "canvas": drawn_readout["canvas"],
                "caption": drawn_readout["caption"], "painted": drawn_readout["painted"],
                "monitor": drawn_readout["monitor"], "main": drawn_readout["main"],
                "rows": drawn_readout["rows"], "label": drawn_readout["label"],
                "text": caption,
            }
            readout_shot(driver, "04c-readout-canvas")
            monitor_shot(driver, "04c-monitor-and-readout")

            # The `--acid` playhead, drawn through the picture — and **absent** while the playhead
            # is outside this Shot's window, because the readout spans the *selected* Shot and the
            # Monitor follows the clock. A line pinned to an edge would claim the picture is at its
            # start when it is somewhere else entirely.
            assert drawn_readout["painted"]["acid"] > 0, (
                "the playhead is not drawn through the readout", drawn_readout["painted"])
            driver.execute_script(
                "document.querySelector('#master-audio').currentTime = 6.5;"
                "document.querySelector('#master-audio').dispatchEvent(new Event('timeupdate'));")
            settle(driver, "#drive-readout", quiet_ms=350)
            moved = look(driver)["drive"]
            assert moved["hidden"] is False, moved
            assert moved["painted"]["acid"] == 0, (
                "the playhead is drawn on a shot the clock is not inside", moved["painted"])
            assert moved["painted"]["blue"] > 50, (
                "the envelope went with the playhead", moved["painted"])
            result["readout"]["playhead_outside"] = moved["painted"]
            readout_shot(driver, "04d-readout-playhead-outside")
            driver.execute_script(
                "document.querySelector('#master-audio').currentTime = 0;"
                "document.querySelector('#master-audio').dispatchEvent(new Event('timeupdate'));")
            settle(driver, "#drive-readout", quiet_ms=350)

            # **A passage below the Trigger Floor draws `--dim`, distinguishably from one merely
            # low** — the readout's whole reason for existing, and the acceptance criterion that
            # cannot be checked from the markup at all.
            #
            # The floor is **swept rather than guessed**: which value shuts the gate on part of this
            # song and not all of it is a fact about the audio, and a number picked by eye would
            # silently become "everything is dim" the first time the fixture's song changed.
            silencing = None
            for floor in ("0.05", "0.1", "0.2", "0.35", "0.5"):
                type_into(driver, "effect-band-0-amount-floor", floor)
                driver.find_element(By.ID, "shot-tab-effects").click()
                settle(driver, "#shot-inspector", quiet_ms=500)
                wait_for_stack(
                    server.base_url, project_id,
                    lambda entries, want=float(floor): (
                        entries[0]["bindings"][0].get("floor") == want),
                    f"the floor {floor} did not reach the manifest",
                )
                settle(driver, "#drive-readout", quiet_ms=400)
                painted = look(driver)["drive"]["painted"]
                if painted["ground"] > 200 and painted["blue"] > 50:
                    silencing = {"floor": floor, "painted": painted}
                    break
                open_effects(driver)
            assert silencing, (
                "no trigger floor between 0.05 and 0.5 silences part of this song's drive without "
                "silencing all of it, so the dim/blue distinction could not be looked at")
            # Both on screen at once, which is what "distinguishably" means: a silenced passage in
            # `--dim` beside a firing one in `--blue`, and the rest line still under both.
            assert silencing["painted"]["rest"] > drawn_readout["canvas"]["width"] * 0.8, silencing
            result["readout"]["silenced"] = silencing
            readout_shot(driver, "04f-readout-silenced")
            open_effects(driver)
            type_into(driver, "effect-band-0-amount-floor", "0")
            driver.find_element(By.ID, "shot-tab-effects").click()
            settle(driver, "#shot-inspector", quiet_ms=500)
            open_effects(driver)

            # **What the readout costs the Monitor**, which is the number this slice has to be
            # honest about: a fourth row in `.timeline-main` takes its height from the two `fr`
            # rows either side of it. Measured on **this** Shot against the same Shot before the
            # binding existed, so nothing else about the layout has moved. Recorded rather than
            # asserted at a threshold, because the right number is a judgement — and a number
            # nobody wrote down is how it grows.
            #
            # Measured by hiding the figure and measuring again **at the same instant**, then
            # restoring it — the only way to isolate this row from everything else that moves
            # the panel's height. Comparing against another Shot, or against an earlier moment
            # on this one, measures the inspector's own height as well: it answered a 98px
            # difference for a row that is nothing like that tall.
            cost = driver.execute_script("const figure = document.querySelector('#drive-readout');\nconst monitor = document.querySelector('#timeline-monitor');\nconst tracks = document.querySelector('#timeline-scroll');\nconst height = (node) => (node ? node.getBoundingClientRect().height : 0);\nconst before = {monitor: height(monitor), tracks: height(tracks), readout: height(figure)};\nfigure.hidden = true;\nconst after = {monitor: height(monitor), tracks: height(tracks)};\nfigure.hidden = false;\nreturn {before, after};")
            result["readout"]["cost"] = {
                "readout_height": round(cost["before"]["readout"], 1),
                "monitor_with": round(cost["before"]["monitor"], 1),
                "monitor_without": round(cost["after"]["monitor"], 1),
                "tracks_with": round(cost["before"]["tracks"], 1),
                "tracks_without": round(cost["after"]["tracks"], 1),
            }
            assert cost["before"]["monitor"] < cost["after"]["monitor"], (
                "the readout took no height from the Monitor, so it is drawn over something",
                cost)

            # And it is **absent, not empty**, on a Shot that carries no binding: the figure is
            # `hidden` and its grid row is exactly zero high.
            select_clip(driver, wait, LOCKED)
            settle(driver, "#drive-readout", quiet_ms=350)
            unbound = look(driver)["drive"]
            assert unbound["hidden"] is True, (
                "a shot with no binding draws a readout", unbound)
            assert unbound["figure"]["height"] == 0, unbound["figure"]
            assert unbound["canvas"]["height"] == 0, unbound["canvas"]
            result["readout"]["unbound_figure"] = unbound["figure"]
            monitor_shot(driver, "04e-monitor-unbound")
            select_clip(driver, wait, SHOT)
            open_effects(driver)
            settle(driver, "#shot-inspector", quiet_ms=350)

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

            # --- 7b. The spectrum strip: drawn, drawn in both tokens, and dragged ------------
            #
            # The census is the point. A canvas that threw halfway through drawing, or one
            # measured at zero width inside a panel that was hidden when it was painted, is a
            # correctly-sized empty box -- and every structural assertion in this file would pass
            # over it. So the pixels are counted and the two tokens are told apart.
            catalogue = get_json(f"{server.base_url}/api/effects/catalogue")
            settings = catalogue["binding_settings"]
            envelope = targets(server, project_id)["envelope"]
            assert envelope and envelope.get("band_average"), (
                "the measured song serves no spectrum for the strip to draw", envelope)
            reach(driver, "effect-band-0-amount-band_centre")
            drawn = look(driver)
            assert drawn["strip"], "the spectrum strip is not drawn on a measured song"
            assert drawn["stripHidden"] == "true", (
                "the canvas is in the accessibility tree, where it announces nothing (UX-DR15)",
                drawn["stripHidden"])
            assert drawn["strip"]["right"] <= drawn["panel"]["right"] + 1, drawn["strip"]
            assert drawn["strip"]["width"] > 120, ("the strip is too narrow to pick a band on",
                                                   drawn["strip"])
            assert drawn["painted"], "nothing is painted on the strip"
            # Both halves of the picture: the song's own spectrum in `--dim`, and the band over it
            # in `--blue`. Either alone is a drawing that has lost the thing it exists to compare.
            assert drawn["painted"]["dim"] > 100, drawn["painted"]
            assert drawn["painted"]["blue"] > 100, drawn["painted"]
            # And it is the panel's *first* control -- the Band is a thing you look at, and the
            # three boxes under it are its equivalent rather than its substitute.
            assert drawn["strip"]["top"] < min(item["top"] for item in drawn["modes"]), (
                drawn["strip"], drawn["modes"])
            assert drawn["strip"]["top"] < min(item["top"] for item in drawn["inputs"]), (
                drawn["strip"], drawn["inputs"])
            assert drawn["stripTitle"] and "Hz" in drawn["stripTitle"], drawn["stripTitle"]
            # The sentence that used to stand where the canvas now is names one absence, not two.
            assert "missing" not in (drawn["readout"] or {}).get("text", ""), drawn["readout"]
            assert drawn["crowded"] is None or not drawn["crowded"]["text"], drawn["crowded"]
            result["strip"] = {
                "box": drawn["strip"], "painted": drawn["painted"], "title": drawn["stripTitle"],
                "readout": (drawn["readout"] or {}).get("text", ""),
                "panel": drawn["panel"], "panelBelowFold": drawn["panelBelowFold"],
                "railScrollHeight": drawn["railScrollHeight"],
                "railClientHeight": drawn["railClientHeight"], "railScroll": drawn["railScroll"],
            }
            shot(driver, "07b-strip-drawn")
            strip_shot(driver, "07b-strip-canvas")

            # **A canvas measured inside a hidden panel is zero pixels wide**, and switching tabs
            # does not rebuild the inspector -- so a Director who looked at Shot Info while this
            # panel was redrawn behind them, and came back, would find a correctly-sized empty box
            # with no way to fix it. The rebuild is the load-bearing half and it is forced here
            # rather than waited for: re-selecting the clip is what the two-second reload does to
            # this panel, and it happens while the Effects tab is hidden.
            driver.find_element(By.ID, "shot-tab-info").click()
            settle(driver, "#shot-inspector", quiet_ms=350)
            select_clip(driver, wait, SHOT)
            settle(driver, "#shot-inspector", quiet_ms=350)
            open_effects(driver)
            reach(driver, "effect-band-0-amount-band_centre")
            again = look(driver)
            assert again["painted"], "the strip came back from a tab switch unpainted"
            # Within a few pixels rather than exactly: the rail's own scroll position moves the
            # canvas by a fraction of a pixel and the antialiasing follows it. What is being
            # checked is that the picture came back at all, not that it came back bit for bit.
            assert abs(again["painted"]["lit"] - drawn["painted"]["lit"]) < 40, (
                "the strip came back from a tab switch different", again["painted"],
                drawn["painted"])
            assert again["painted"]["blue"] > 100 and again["painted"]["dim"] > 100, (
                again["painted"])
            result["strip"]["after_tab_switch"] = again["painted"]

            # A drag on the strip's body: pressed, moved, and **nothing stored until release**.
            plan = strip_geometry(driver, envelope, settings, band_values(drawn))
            assert plan["count"] == len(envelope["band_average"]), (
                "the strip drew a band count the measurement does not carry", plan)
            held = stack(server.base_url, project_id)[0]["bindings"][0]
            canvas = driver.find_element(By.CSS_SELECTOR, ".effect-band-strip")
            body = free_ground(plan)
            pointer = StripPointer(driver, canvas, plan)
            pointer.press(body)
            pointer.drag_to(max(2.0, body - 30))
            mid = stack(server.base_url, project_id)[0]["bindings"][0]
            assert mid == held, ("the drag wrote before it was released -- a save per pixel", mid)
            # The boxes track the band while it is being dragged, because the canvas and the three
            # numbers are one band rather than two: one owns the value and the other reads it.
            moving = look(driver)
            assert band_values(moving)["band_centre"] != band_values(drawn)["band_centre"], (
                "the numeric boxes did not follow the drag", band_values(moving))
            pointer.release()
            dragged = wait_for_stack(
                server.base_url, project_id,
                lambda entries: entries[0]["bindings"][0] != held,
                "releasing the drag wrote nothing",
            )[0]["bindings"][0]
            assert dragged["drive"] == held["drive"] and dragged["depth"] == held["depth"], (
                "a band drag rewrote a decision it does not own", dragged)
            settle(driver, "#shot-inspector", quiet_ms=350)
            landed = look(driver)
            assert band_values(landed)["band_centre"] == dragged.get("band_centre"), (
                "the panel and the manifest disagree about where the band is",
                band_values(landed), dragged)
            result["dragged_band"] = {
                "before": held, "after": dragged, "shown": band_values(landed),
                "pressed_at": body, "released_at": max(2.0, body - 30),
            }
            shot(driver, "07b-strip-dragged")
            strip_shot(driver, "07b-strip-canvas-dragged")

            # --- 7c. The minimum band width, which is a state and not an edge case ------------
            #
            # `band_width`'s minimum is 0.02, which on this strip is a region under four pixels
            # across holding two edge handles and a softness handle with no interior left to
            # drag. Every gesture has to survive that, and the one that cannot is named.
            for name, value in (("band_width", "0.02"), ("band_softness", "0")):
                type_into(driver, f"effect-band-0-amount-{name}", value)
                driver.find_element(By.ID, "shot-tab-effects").click()
                settle(driver, "#shot-inspector", quiet_ms=500)
                open_effects(driver)
            wait_for_stack(
                server.base_url, project_id,
                lambda entries: entries[0]["bindings"][0].get("band_width") == 0.02,
                "the minimum width did not reach the manifest",
            )
            reach(driver, "effect-band-0-amount-band_centre")
            tightest = look(driver)
            narrow = strip_geometry(driver, envelope, settings, band_values(tightest))
            assert narrow["band"]["right"] - narrow["band"]["left"] < 4, (
                "the minimum width is not the narrow case this step is about", narrow["band"])
            # The softness handle has no ground left, so it is withdrawn -- and named, with the
            # box that still sets it, rather than offered at two pixels (R-16).
            assert [handle["name"] for handle in narrow["handles"]] == ["low", "high"], narrow
            assert tightest["crowded"] and "Softness" in tightest["crowded"]["text"], (
                tightest["crowded"])
            assert tightest["crowded"]["colour"] == tightest["palette"]["muted"], (
                "a geometry note took an accent", tightest["crowded"])
            shot(driver, "07c-minimum-width")
            strip_shot(driver, "07c-strip-canvas-minimum")
            # And the two edges are still reachable: pressed on the low edge, the drag widens the
            # band rather than moving it, at a width where the region itself is three pixels.
            widened_from = stack(server.base_url, project_id)[0]["bindings"][0]
            canvas = driver.find_element(By.CSS_SELECTOR, ".effect-band-strip")
            low = next(handle["x"] for handle in narrow["handles"] if handle["name"] == "low")
            pointer = StripPointer(driver, canvas, narrow)
            pointer.press(low)
            pointer.drag_to(max(2.0, low - 25))
            pointer.release()
            widened = wait_for_stack(
                server.base_url, project_id,
                lambda entries: entries[0]["bindings"][0].get("band_width", 0) > 0.02,
                "the low edge is unreachable at the minimum width",
            )[0]["bindings"][0]
            assert widened["band_width"] > widened_from["band_width"], widened
            result["minimum_width"] = {
                "region_pixels": round(narrow["band"]["right"] - narrow["band"]["left"], 2),
                "handles": [handle["name"] for handle in narrow["handles"]],
                "withdrawn": tightest["crowded"]["text"],
                "widened": {"from": widened_from, "to": widened},
            }
            shot(driver, "07c-widened-from-the-minimum")
            strip_shot(driver, "07c-strip-canvas-widened")

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
