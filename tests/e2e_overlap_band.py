"""Browser QA for Epic 11's drawn half: the Overlap band, and the transition pair.

**This is the slice where this project has been wrong most often.** Across Epics 9 and 10 eleven
defects passed every automated gate and were caught only by looking: sliders drawn as 20px bordered
pills, checkboxes squeezing text out of their rows, a `STALE` label that never left, a measured band
that drew as **nothing** because a 1px bar was swallowed by a baseline, and a Monitor that collapsed
to its 120px floor on nearly every Shot. So what this script asserts is what only a browser can
see -- painted pixels, paint order, hit testing and text that really fits -- and everything a stub
DOM can answer is left to `tests/test_frontend_contract.py`, where it is cheaper.

What is asserted, in order:

1. **The band draws.** A pixel census of the overlap region: the typed band really paints `--blue`
   at 22 %, the untyped one really paints the `--line-strong` hatch, and neither is an empty box of
   the right size. `color-mix` is declared behind an 8-digit-hex fallback precisely so a browser
   that cannot resolve it still paints something, and this is where which one landed is read back.
2. **The band is above the clips and readable through.** The clip's own text under the band is
   still painted -- `DESIGN.md` asked for the band *behind* the clips and that is unbuildable
   (`.shot-clip` is opaque with `overflow: hidden`), so R-40 put it above at 22 % alpha and this is
   the measurement that says both properties survived.
3. **The band did not make the standing hazard worse.** Every right resize handle under a band is
   still hit-testable at the middle of the handle, and a press inside the band reaches the clip
   beneath it. `tests/e2e_clip_overlap_and_split.py` is the gate for the hazard itself; this one is
   the gate for the overlay not taking it back.
4. **The label fits or is not drawn.** Every label the catalogue can produce is measured at the
   width it really renders at, and `TRANSITION_BAND_LABEL_CHAR_PX` and
   `TRANSITION_BAND_LABEL_PAD_PX` are asserted to bound them -- so the threshold that decides
   whether a band letters is a measurement rather than a guess, and a stylesheet change that widens
   the label fails here rather than clipping a word in silence.
5. **The rows are rows.** Both transition rows, in every state, with their edge colour read off the
   computed style, their sentence measured for overflow out of its own box, and the select really
   operable. Two of Epic 9's eleven were controls whose text was squeezed out of the row it was in.
6. **The gesture works end to end.** Setting `Transition out` writes the pair, the mirror's toast
   names both Shots, the band turns from a hatch into a fill and its label becomes the type.
7. **A pair-only type refuses out loud**, with the route's own sentence, and writes nothing.
8. **Dragging the Overlap away** removes the band, keeps the stored types, and announces it.

**No GPU time and no model time is spent.** Nothing here reaches `/prompt`, `MVP_COMFY_URL` points
at a dead port this run chose, and ComfyUI is never contacted, started or stopped.

Run from the repo root -- it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_overlap_band.py [--port 8785]

Assumes: nothing listening on the port (it refuses to reuse a bound one), Microsoft Edge and its
WebDriver installed, and `music_video_producer` importable from this checkout's `src/`.
"""

from __future__ import annotations

import json
import os
import re
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
    get_json,
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

NAME = "overlap-band"

#: The band's per-character label budget, read out of `api.js` rather than restated. Section 4
#: measures every label the catalogue can produce in a real browser and asserts this number bounds
#: them, so the threshold that decides whether a band letters is a measurement.
API_JS = (Path(__file__).resolve().parents[1]
          / "src" / "music_video_producer" / "web" / "assets" / "api.js")

SONG_SECONDS = 60.0

#: Four shots and three boundaries, each boundary chosen for one state this script has to see.
#:
#: * `shot_01`/`shot_02` overlap by **1.5 s** -- wide enough at a working zoom for a label, and the
#:   pair every gesture below is performed on;
#: * `shot_02`/`shot_03` overlap by **0.05 s** -- above `BOUNDARY_TOLERANCE_SECONDS` (1/48 s) and
#:   therefore a real blend, at a width that rounds to under a pixel at the default zoom. This is
#:   the band that must not draw as nothing;
#: * `shot_03`/`shot_04` **meet exactly** -- no band at all, which is what proves the ones above
#:   are not being drawn on every boundary;
#: * `shot_04`/`shot_05` overlap by **15 s**, with `shot_05` sitting *wholly inside* `shot_04` --
#:   the geometry `assembly._paired_transitions` refuses (story 11.f7). On the assembly grid that
#:   boundary is 120 frames of `shot_04`, a 360-frame blend and then **-240** frames of `shot_05`:
#:   a window that runs backwards, which is `-frames:v -1` and which ffmpeg ignores at rc 0. Until
#:   this slice the timeline drew it as a live blue blend with a `paired` row, which is the whole
#:   defect. It is reached here the way a Director reaches it -- one clip dragged over another.
SHOTS = [
    ("shot_01", 0.0, 12.0),
    ("shot_02", 10.5, 14.55),   # overlaps shot_01 by 1.5
    ("shot_03", 25.0, 15.0),    # overlaps shot_02 by 0.05
    ("shot_04", 40.0, 20.0),    # meets shot_03 exactly
    ("shot_05", 45.0, 5.0),     # sits wholly inside shot_04: the export refuses this boundary
]

#: Every painted pixel in one element's box, counted by colour family. A canvas that threw halfway
#: through drawing, or an element measured at zero width inside a hidden box, is a correctly-sized
#: empty box that every structural assertion passes over -- the lesson `e2e_band_panel.py` records.
#: An element is not a canvas, so the census is taken by reading the *computed* paint rather than
#: by sampling: `background-image` for the hatch, `background-color` for the fill, and the border
#: colours, each resolved by the browser to the value it really used.
PAINT_CENSUS = """
const band = document.querySelector(arguments[0]);
if (!band) return null;
const style = getComputedStyle(band);
const box = band.getBoundingClientRect();
const label = band.querySelector('.overlap-label');
return {
  background: style.backgroundColor,
  image: style.backgroundImage,
  borderTop: style.borderTopColor,
  borderBottom: style.borderBottomColor,
  borderTopWidth: style.borderTopWidth,
  borderBottomWidth: style.borderBottomWidth,
  // The third band state is a dashed box and a cross-hatch rather than a seventh accent, so the
  // style and the number of gradients are what tell it from the other two without hue.
  borderTopStyle: style.borderTopStyle,
  borderLeftStyle: style.borderLeftStyle,
  borderLeftWidth: style.borderLeftWidth,
  className: band.className,
  pointerEvents: style.pointerEvents,
  zIndex: getComputedStyle(band.parentElement).zIndex,
  box: { left: box.left, top: box.top, width: box.width, height: box.height,
         right: box.right, bottom: box.bottom },
  label: label ? {
    text: label.textContent,
    width: label.getBoundingClientRect().width,
    font: getComputedStyle(label).font,
    letterSpacing: getComputedStyle(label).letterSpacing,
    colour: getComputedStyle(label).color,
  } : null,
  title: band.getAttribute('title'),
  ariaLabel: band.getAttribute('aria-label'),
};
"""

#: What is painted at a point, and whether it is a resize handle. The overlay must never be the
#: answer -- `pointer-events: none` is what makes a press inside the band reach the clip beneath.
TOPMOST_AT = """
const found = document.elementFromPoint(arguments[0], arguments[1]);
if (!found) return { tag: null, className: '', shot: null, isHandle: false, handleEdge: '',
                     insideBand: false, offScreen: true };
const clip = found.closest ? found.closest('.shot-clip') : null;
const band = found.closest ? found.closest('.overlap-band') : null;
return {
  tag: found.tagName,
  className: String(found.className || ''),
  shot: clip ? clip.dataset.shotId : null,
  isHandle: Boolean(found.classList && found.classList.contains('resize-handle')),
  handleEdge: found.classList && found.classList.contains('right') ? 'right'
    : found.classList && found.classList.contains('left') ? 'left' : '',
  insideBand: Boolean(band),
};
"""

#: One clip's right handle, and the band drawn over that boundary, as boxes.
HANDLE_AND_BAND = """
const clip = document.querySelector('#shots-track .shot-clip[data-shot-id="' + arguments[0] + '"]');
const band = document.querySelector('.overlap-band[data-before="' + arguments[0] + '"]');
if (!clip || !band) return null;
const handle = clip.querySelector('.resize-handle.right').getBoundingClientRect();
const region = band.getBoundingClientRect();
return {
  handle: { left: handle.left, right: handle.right, top: handle.top, bottom: handle.bottom },
  band: { left: region.left, right: region.right, top: region.top, bottom: region.bottom },
  // Whether the handle's own midpoint is somewhere `elementFromPoint` can be asked about at all.
  // A probe outside the viewport answers `null`, which would read as "the band buried it".
  inViewport: handle.left >= 0 && handle.top >= 0
    && handle.right <= window.innerWidth && handle.bottom <= window.innerHeight,
  clipZIndex: getComputedStyle(clip).zIndex,
  handleZIndex: getComputedStyle(clip.querySelector('.resize-handle.right')).zIndex,
  bandZIndex: getComputedStyle(band.parentElement).zIndex,
};
"""

#: Every label the catalogue can produce, rendered in the band's own font and measured. Written
#: into a real element inside the track rather than measured on a canvas, so the number is the
#: number the stylesheet produces -- letter-spacing, padding and font stack included.
MEASURE_LABELS = """
const host = document.querySelector('.overlap-bands') || document.querySelector('#shots-track');
const probe = document.createElement('div');
probe.className = 'overlap-band typed';
probe.style.left = '0px';
probe.style.width = '900px';
probe.dataset.experiment = 'label-widths';
const span = document.createElement('span');
span.className = 'overlap-label';
probe.appendChild(span);
host.appendChild(probe);
const measured = {};
for (const label of arguments[0]) {
  span.textContent = label;
  measured[label] = span.getBoundingClientRect().width;
}
probe.remove();
return measured;
"""

#: Where a band's label really lands against the **painted glyph rectangles** of the clip text
#: under it -- not against element boxes, which lie: a `-webkit-line-clamp` box has rectangles for
#: lines it does not paint, and counting those invents collisions (the measurement DESIGN 3's chip
#: amendment records). `Range.getClientRects()` over the text node is what the browser actually
#: painted.
#:
#: This exists because the first pass **centred** the label, as DESIGN 3 says, and the screenshot
#: showed `CUT` drawn straight through the word "dark." in the clip beneath -- both illegible, and
#: invisible to every other gate in this repository.
LABEL_AGAINST_THE_CLIP_TEXT = """
const label = document.querySelector('.overlap-band[data-before="' + arguments[0] + '"] .overlap-label');
if (!label) return null;
const box = label.getBoundingClientRect();
const painted = [];
for (const clip of document.querySelectorAll('#shots-track .shot-clip')) {
  for (const part of clip.querySelectorAll('.clip-id, .clip-prompt, .clip-state, .clip-fx')) {
    for (const node of part.childNodes) {
      if (node.nodeType !== 3 || !node.textContent.trim()) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      for (const rect of range.getClientRects()) {
        if (rect.width < 1 || rect.height < 1) continue;
        painted.push({ shot: clip.dataset.shotId, part: part.className, text: node.textContent.trim().slice(0, 24),
                       left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom });
      }
    }
  }
}
const hits = painted.filter((rect) => rect.left < box.right && rect.right > box.left
  && rect.top < box.bottom && rect.bottom > box.top);
return { label: { text: label.textContent, left: box.left, right: box.right, top: box.top,
                  bottom: box.bottom },
         collisions: hits, painted: painted.length };
"""

#: The head of one transition row -- its label and its length readout, side by side -- measured
#: against the row that holds them.
#:
#: **Two of Epic 9's eleven were exactly this**: a checkbox that pushed its own label past the edge
#: of its row, and a slider drawn as a bordered pill, both green under every automated gate. A flex
#: row of two texts with `justify-content: space-between` is the same shape, and `min-width: auto`
#: on a flex item means neither will shrink below its content -- so at a narrow window the two run
#: into each other or out of the box, and nothing but a measurement at that width will say so.
ROW_HEAD_FIT = """
const select = document.querySelector('#' + arguments[0]);
if (!select) return null;
const row = select.closest('.transition-row');
const head = row.querySelector('.transition-head');
const name = head.querySelector('label');
const length = head.querySelector('.transition-length');
const rowBox = row.getBoundingClientRect();
const nameBox = name.getBoundingClientRect();
return {
  rowWidth: rowBox.width,
  overflowRight: head.getBoundingClientRect().right - rowBox.right,
  nameClipped: name.scrollWidth - name.clientWidth,
  // Where the two texts meet. Negative means the readout has run into the label.
  gap: length ? length.getBoundingClientRect().left - nameBox.right : null,
  selectOverflow: select.getBoundingClientRect().right - rowBox.right,
  noteOverflow: row.querySelector('.control-reason')
    ? row.querySelector('.control-reason').getBoundingClientRect().right - rowBox.right : null,
};
"""

#: Whether a block of text is squeezed out of the box it was put in -- the Epic 9 defect where a
#: checkbox pushed its own label past the edge of its row, which every structural gate passed.
OVERFLOWS = """
const element = document.querySelector(arguments[0]);
if (!element) return null;
const box = element.getBoundingClientRect();
const parent = element.parentElement.getBoundingClientRect();
return {
  text: element.textContent,
  clippedRight: box.right - parent.right,
  clippedBottom: box.bottom - parent.bottom,
  scrollOverflow: element.scrollWidth - element.clientWidth,
  height: box.height,
  width: box.width,
  visible: box.width > 0 && box.height > 0,
};
"""

#: One transition row's edge colour and state, read off the computed style rather than off the
#: class -- `--blue` on a row with nothing to blend across is the palette's last accent saying
#: something untrue, and only the resolved colour says which token really landed.
ROW_FACTS = """
const select = document.querySelector('#' + arguments[0]);
if (!select) return null;
const row = select.closest('.transition-row');
const style = getComputedStyle(row);
const note = row.querySelector('.control-reason');
const length = row.querySelector('.transition-length');
const box = row.getBoundingClientRect();
return {
  edge: row.dataset.edge,
  state: row.dataset.state,
  borderLeftColor: style.borderLeftColor,
  borderLeftWidth: style.borderLeftWidth,
  // A refused row and an unoverlapped one are both inert and both take `--dim`, so the edge alone
  // cannot tell them apart; the style is the second signal that agrees with the note.
  borderLeftStyle: style.borderLeftStyle,
  preview: Boolean(row.querySelector('.transition-preview, [id$="-preview"]')),
  note: note ? note.textContent : '',
  noteHeight: note ? note.getBoundingClientRect().height : 0,
  noteOverflow: note ? note.getBoundingClientRect().right - box.right : null,
  length: length ? length.textContent : '',
  lengthColour: length ? getComputedStyle(length).color : '',
  disabled: select.disabled,
  value: select.value,
  options: [...select.options].map((option) => option.value),
  optionLabels: [...select.options].map((option) => option.textContent),
  selectWidth: select.getBoundingClientRect().width,
  rowWidth: box.width,
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
    project = post_json(base_url + "/api/projects", {"name": "Overlap band browser QA"})
    song = artifact_dir() / "overlap-band-song.wav"
    synthesize_song(song)
    post_multipart(
        f"{base_url}/api/projects/{project['id']}/songs/upload",
        {"title": "Overlap band QA song", "duration": str(SONG_SECONDS)},
        ("file", song),
    )
    put_json(f"{base_url}/api/projects/{project['id']}/shots", {"shots": [
        {"id": shot_id, "start": start, "duration": duration, "mode": "text", "status": "draft",
         "prompt": f"{shot_id}: a corridor, pushing in through the dark."}
        for shot_id, start, duration in SHOTS
    ]})
    # **A `f` chip on the earlier clip of the wide pair, and it is not decoration.**
    # `.clip-chips` is anchored `bottom: 4px` at the clip's right edge -- which is inside a narrow
    # band's whole width, and directly under where the band's label is drawn. Section 6 measures
    # the label against every painted glyph rectangle on the track, and a fixture with no chip on
    # it would make that half of the check vacuous. A fixture must contain the thing under test.
    put_json(
        f"{base_url}/api/projects/{project['id']}/shots/{SHOTS[0][0]}/effects",
        {"effects": [{"effect": "grain", "enabled": True, "parameters": {}}]},
    )
    return project["id"]


def manifest(server: ManagedServer, project_id: str) -> dict:
    """The project as it is on disk, read out of this run's own data root, with a retry: the store
    renames a temp file over this one and a read landing inside that window fails on Windows."""
    path = server.data_root / "projects" / project_id / "project.json"
    last: Exception | None = None
    for _ in range(40):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            last = error
            time.sleep(0.05)
    raise AssertionError(f"{path} could not be read: {last}")


def transitions(server: ManagedServer, project_id: str) -> dict[str, dict[str, str]]:
    return {
        shot["id"]: {
            "out": (shot.get("transition_out") or {}).get("type", ""),
            "in": (shot.get("transition_in") or {}).get("type", ""),
        }
        for shot in manifest(server, project_id)["shots"]
    }


def windows(server: ManagedServer, project_id: str) -> dict[str, dict[str, float]]:
    return {
        shot["id"]: {"start": round(shot["start"], 6), "duration": round(shot["duration"], 6)}
        for shot in manifest(server, project_id)["shots"]
    }


def channels(colour: str) -> tuple[int, int, int, float]:
    """A computed colour as `(r, g, b, alpha)` in 0-255 and 0-1, so tokens are told apart by
    channel rather than by the string the browser happened to serialise.

    **Two forms, and both are real here.** A plain declaration computes to
    `rgba(91, 155, 213, 0.22)`; a `color-mix(in srgb, …)` computes in Edge to
    `color(srgb 0.356863 0.607843 0.835294 / 0.22)` — the same colour, in 0-1 floats, under a
    different function name. The stylesheet ships both (the hex is the fallback under the mix), so
    a reader that understood only one would either fail on a working page or pass on a band with
    no fill at all, which is the exact defect this script exists to catch.
    """
    inside = colour[colour.index("(") + 1:colour.index(")")]
    scale = 255.0
    if colour.startswith("color("):
        inside = inside.split(None, 1)[1]  # drop the colour space, which is asserted below
        assert colour.startswith("color(srgb"), colour
        scale = 1.0
    parts = [part.strip() for part in inside.replace("/", " ").replace(",", " ").split()]
    numbers = [float(part.rstrip("%")) for part in parts]
    while len(numbers) < 4:
        numbers.append(1.0)
    return (
        round(numbers[0] * 255 / scale),
        round(numbers[1] * 255 / scale),
        round(numbers[2] * 255 / scale),
        round(numbers[3], 3),
    )


def select_clip(driver, wait, shot_id: str):
    settle(driver, "#shots-track")
    clip = wait.until(lambda browser: browser.find_element(
        By.CSS_SELECTOR, f'#shots-track .shot-clip[data-shot-id="{shot_id}"]'))
    visible_and_clickable(driver, clip, f"the timeline clip for {shot_id}")
    clip.click()
    wait.until(lambda browser: "selected" in browser.find_element(
        By.CSS_SELECTOR, f'#shots-track .shot-clip[data-shot-id="{shot_id}"]'
    ).get_attribute("class"))
    settle(driver, "#shot-inspector")
    return driver.find_element(
        By.CSS_SELECTOR, f'#shots-track .shot-clip[data-shot-id="{shot_id}"]')


def open_effects_tab(driver, wait) -> None:
    tab = wait.until(lambda browser: browser.find_element(By.ID, "shot-tab-effects"))
    visible_and_clickable(driver, tab, "the Effects tab")
    tab.click()
    wait.until(lambda browser: browser.find_element(
        By.ID, "shot-panel-effects").get_attribute("hidden") in (None, "false"))
    settle(driver, "#shot-panel-effects", quiet_ms=400)


def zoom_to(driver, presses: int) -> None:
    button = driver.find_element(By.ID, "zoom-in" if presses > 0 else "zoom-out")
    for _ in range(abs(presses)):
        button.click()
    settle(driver, "#shots-track", quiet_ms=250)


def shot(driver, index: int):
    return driver.find_element(
        By.CSS_SELECTOR, f'#shots-track .shot-clip[data-shot-id="{SHOTS[index][0]}"]')


def main() -> None:
    port = 8785
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    result: dict[str, object] = {}
    comfy_root = Path(tempfile.mkdtemp(prefix="mvp-overlap-band-comfy-"))
    unreachable = f"http://127.0.0.1:{dead_port()}"
    os.environ["MVP_COMFY_ROOT"] = str(comfy_root)
    os.environ["MVP_COMFY_URL"] = unreachable
    os.environ["MVP_LLM_EJECT_BEFORE_RENDER"] = "false"
    os.environ["MVP_LLM_BASE_URL"] = ""

    with ManagedServer(port, label=NAME) as server:
        result["server_identity"] = server.evidence
        result["comfy_url"] = unreachable
        project_id = seed(server.base_url)

        # The catalogue as the server really serves it, so every label this script measures and
        # every option it expects to find in the two selects is the server's own answer rather
        # than a list written here that could go stale the day a thirteenth entry lands.
        catalogue = get_json(
            f"{server.base_url}/api/projects/{project_id}/shots/{SHOTS[0][0]}/transitions"
        )["catalogue"]
        result["catalogue_entries"] = len(catalogue)
        assert len(catalogue) >= 12, catalogue
        pair_only = next(entry for entry in catalogue if entry["pair_only"])
        blend = next(entry for entry in catalogue if not entry["pair_only"])

        driver = edge_driver()
        wait = WebDriverWait(driver, 25)
        try:
            driver.get(server.base_url)
            wait.until(EC.presence_of_element_located((By.ID, "project-select")))
            wait.until(lambda browser: browser.find_element(
                By.CSS_SELECTOR, f'#project-select option[value="{project_id}"]')).click()
            wait.until(lambda browser: browser.find_element(
                By.ID, "project-select").get_attribute("value") == project_id)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            wait_for_readiness(driver, wait, f"of {len(SHOTS)} shots")
            settle(driver, "#shots-track")

            # === 1. The band draws, and it draws only where there is an Overlap ===============
            drawn = driver.execute_script(
                "return [...document.querySelectorAll('.overlap-band')]"
                ".map((band) => band.dataset.before + '|' + band.dataset.after);")
            assert drawn == ["shot_01|shot_02", "shot_02|shot_03", "shot_04|shot_05"], (
                ("the bands on the track are not the overlaps in the plan -- either a boundary "
                 "that merely meets is being drawn, or a real overlap is missing"),
                drawn,
            )
            result["bands_drawn"] = drawn

            wide = driver.execute_script(PAINT_CENSUS, '.overlap-band[data-before="shot_01"]')
            narrow = driver.execute_script(PAINT_CENSUS, '.overlap-band[data-before="shot_02"]')
            assert wide and narrow

            # Untyped, both of them: a `--line-strong` hatch and no `--blue` anywhere.
            for band, where in ((wide, "shot_01"), (narrow, "shot_02")):
                assert "gradient" in band["image"], (
                    "an untyped overlap is not drawing its hatch", where, band)
                assert "rgba(0, 0, 0, 0)" in band["background"] or band["background"] in (
                    "transparent", "rgba(0, 0, 0, 0)"), (where, band["background"])
                red, green, blue, _ = channels(band["borderTop"])
                assert (red, green, blue) != (0x5b, 0x9b, 0xd5), (
                    ("an untyped overlap has borrowed the transition's `--blue`, which it must "
                     "not: an overlap with no type set is a hard cut (UX-DR8)"),
                    where, band["borderTop"],
                )

            # **The measured band that draws as nothing.** 0.05 s is a real blend and 0.83 px at
            # the default zoom; the floor is what stops it rounding away.
            assert narrow["box"]["width"] >= 2, (
                ("a real overlap the assembler will blend across is drawn narrower than 2px, "
                 "which is the Epic 9 defect where a measured band drew as nothing"),
                narrow["box"],
            )
            assert wide["box"]["width"] > narrow["box"]["width"] * 4, (wide["box"], narrow["box"])
            # A band with no height is the same defect said differently, and both edges are real.
            assert wide["box"]["height"] >= 80, wide["box"]
            for edge in ("borderTopWidth", "borderBottomWidth"):
                assert wide[edge] == "1px", (edge, wide[edge])
            result["untyped_paint"] = {"wide": wide, "narrow": narrow}

            # === 2. Above the clips, and readable through =====================================
            #
            # The band spans exactly the clips it is about: `top` and `height` come from the
            # stylesheet, so a band that had drifted off the clip run would be measured here.
            clip_box = driver.execute_script(
                "const box = document.querySelector('#shots-track .shot-clip"
                "[data-shot-id=\"shot_01\"]').getBoundingClientRect();"
                "return { top: box.top, bottom: box.bottom, height: box.height };")
            assert abs(wide["box"]["top"] - clip_box["top"]) < 1.5, (wide["box"], clip_box)
            assert abs(wide["box"]["bottom"] - clip_box["bottom"]) < 1.5, (wide["box"], clip_box)
            result["band_against_clip"] = {"band": wide["box"], "clip": clip_box}

            layering = driver.execute_script(HANDLE_AND_BAND, "shot_01")
            assert layering["clipZIndex"] == "auto", (
                ("a clip body has acquired a z-index, which makes it a stacking context and traps "
                 "the resize handles' z-index inside their own clip -- the 2026-08-21 defect"),
                layering,
            )
            assert int(layering["bandZIndex"]) < int(layering["handleZIndex"]), (
                ("the band's layer is at or above the resize handles, so an overlay drawn for "
                 "story 11.2 has buried the handle the 2026-08-21 fix uncovered"),
                layering,
            )
            result["z_index"] = {
                "clip": layering["clipZIndex"], "band": layering["bandZIndex"],
                "handle": layering["handleZIndex"],
            }
            assert wide["pointerEvents"] == "none", wide["pointerEvents"]

            # Everything under the band is still readable. The clip's own prompt line is inside
            # the overlap region for `shot_02`, and it must still be painted -- which is the half
            # of R-40 that `DESIGN.md`'s "draw it behind" was protecting.
            under = driver.execute_script(
                "const clip = document.querySelector('#shots-track .shot-clip"
                "[data-shot-id=\"shot_02\"]');"
                "const text = clip.querySelector('.clip-id');"
                "const box = text.getBoundingClientRect();"
                "const band = document.querySelector('.overlap-band[data-before=\"shot_01\"]')"
                ".getBoundingClientRect();"
                "return { text: text.textContent, width: box.width, height: box.height,"
                " overlapsBand: box.left < band.right && box.right > band.left,"
                " opacity: getComputedStyle(text).opacity };")
            assert under["width"] > 0 and under["height"] > 0, under
            assert under["opacity"] == "1", under
            result["readable_under_the_band"] = under

            # === 3. Every handle under a band is still reachable, and the band is not a target ==
            #
            # At the **default** zoom, deliberately: this plan is 60 s at 16.6 px/s, so the whole
            # of it is inside a 1600px viewport and `elementFromPoint` can be asked about every
            # handle in one pass. Zooming in puts the later boundaries off screen, where a probe
            # answers `null` — which would read as "the band buried the handle" and be a lie.
            # Both bands really do lie over their handle at this zoom, which is asserted below.
            reach: list[dict] = []
            for shot_id in ("shot_01", "shot_02"):
                facts = driver.execute_script(HANDLE_AND_BAND, shot_id)
                assert facts, shot_id
                assert facts["inViewport"], (
                    ("the handle is not on screen, so this hit test would be asking about the "
                     "wrong pixels"),
                    shot_id, facts,
                )
                handle, band = facts["handle"], facts["band"]
                covered = min(handle["right"], band["right"]) - max(handle["left"], band["left"])
                middle_x = (handle["left"] + handle["right"]) / 2
                middle_y = (handle["top"] + handle["bottom"]) / 2
                landed = driver.execute_script(TOPMOST_AT, middle_x, middle_y)
                assert landed["shot"] == shot_id and landed["handleEdge"] == "right", (
                    (f"a click at the middle of {shot_id}'s right resize handle lands on "
                     f"{landed['shot'] or 'nothing'} ({landed['className']}) -- the Overlap band "
                     "drawn over that boundary is covering the handle"),
                    landed, facts,
                )
                reach.append({"shot": shot_id, "band_px_over_handle": round(covered, 2),
                              "landed": landed})
            # Not vacuous: at least one band really does lie over its handle.
            assert any(row["band_px_over_handle"] > 0 for row in reach), reach
            result["handles_under_bands"] = reach

            # A press inside the band, **away from either handle**, reaches the clip body beneath
            # it. Measured zoomed in, and that is not incidental: at the default zoom this band is
            # 25px and every point in it is inside one clip edge or the other, so a probe at its
            # centre lands on a resize handle and proves nothing about `pointer-events: none`.
            # Widened, the middle of the band is ordinary clip body and the probe is real.
            #
            # It is also the 2026-08-20 layering ruling, measured through the new overlay: inside
            # an overlap the picture on top is the **later** clip, and an overlay drawn above both
            # must not become the answer instead.
            zoom_to(driver, 6)
            facts = driver.execute_script(HANDLE_AND_BAND, "shot_01")
            band = facts["band"]
            assert band["right"] - band["left"] > 40, (
                "the band is too narrow for a probe that is not on a handle", band)
            inside = driver.execute_script(
                TOPMOST_AT, (band["left"] + band["right"]) / 2, (band["top"] + band["bottom"]) / 2)
            assert inside["insideBand"] is False and inside["isHandle"] is False, (
                ("a press in the middle of the Overlap band did not reach the clip body beneath "
                 "it, so the band is a drag target -- `pointer-events: none` is not doing its job "
                 "(R-40)"),
                inside,
            )
            assert inside["shot"] == "shot_02", (
                ("the later clip is no longer the picture on top inside an overlap, which is the "
                 "2026-08-20 layering ruling this overlay must not change"),
                inside,
            )
            result["press_inside_the_band"] = {"band_width": round(band["right"] - band["left"], 2),
                                               "landed": inside}
            zoom_to(driver, -6)

            # === 4. Every label the catalogue can produce, measured =============================
            widths = driver.execute_script(
                MEASURE_LABELS, [entry["label"].upper() for entry in catalogue] + ["CUT"])
            source = API_JS.read_text(encoding="utf-8")
            budget = re.search(r"TRANSITION_BAND_LABEL_CHAR_PX = ([\d.]+)", source)
            padding = re.search(r"TRANSITION_BAND_LABEL_PAD_PX = ([\d.]+)", source)
            assert budget and padding, (
                "api.js no longer declares the band's label budget")
            per_character = float(budget.group(1))
            pad = float(padding.group(1))

            # The threshold `overlapBands` letters a band at is `len * per_character + pad`. It has
            # to be **at least** what the label really renders at, or a band exactly at the
            # threshold draws a clipped fragment of a word. Measured against every label the
            # served catalogue can produce, so a thirteenth entry with a longer name fails here.
            over = {
                label: round(width - (len(label) * per_character + pad), 2)
                for label, width in widths.items()
                if width > len(label) * per_character + pad
            }
            assert not over, (
                ("a label renders wider than the threshold that decides whether to draw it, so a "
                 f"band at that threshold would clip the word: {over}"),
                widths, per_character, pad,
            )
            # And not absurdly generous either -- a budget far above the real advance would
            # withhold labels from bands that could hold them. Twice the measured cost is the line.
            slack = max(
                (len(label) * per_character + pad) / width for label, width in widths.items())
            assert slack < 2.0, ("the label budget is more than twice the measured width", widths)
            result["label_widths"] = {label: round(width, 2) for label, width in widths.items()}
            result["label_budget"] = {
                "per_character": per_character,
                "padding": pad,
                "measured_advance": round(
                    (widths["FADE THROUGH BLACK"] - pad) / len("FADE THROUGH BLACK"), 3),
                "slack_at_worst": round(slack, 3),
            }

            # === 5. The rows, in every state ==================================================
            select_clip(driver, wait, "shot_01")
            open_effects_tab(driver, wait)
            driver.save_screenshot(str(artifact_dir() / f"{NAME}-01-rows-paired-and-opening.png"))

            rows = {
                "in": driver.execute_script(ROW_FACTS, "transition-in"),
                "out": driver.execute_script(ROW_FACTS, "transition-out"),
            }
            assert rows["out"]["state"] == "paired", rows["out"]
            assert rows["out"]["edge"] == "blue", rows["out"]
            assert channels(rows["out"]["borderLeftColor"])[:3] == (0x5b, 0x9b, 0xd5), (
                "the paired row's left edge is not `--blue`", rows["out"])
            assert rows["out"]["length"] == "1.50s · from overlap", rows["out"]
            assert rows["out"]["disabled"] is False
            # The first Shot's `Transition in`: nothing precedes it, so **it opens the video**
            # (R-45, story 11.f8). This row read `headless` and said nothing renders from it.
            assert rows["in"]["state"] == "opening", rows["in"]
            assert channels(rows["in"]["borderLeftColor"])[:3] != (0x5b, 0x9b, 0xd5), rows["in"]
            assert rows["in"]["note"] == (
                "Nothing plays before shot 01 — this treats its opening frames as the video "
                "begins."), rows["in"]
            assert rows["in"]["disabled"] is False, (
                "a live editorial choice has been drawn disabled", rows["in"])
            # Every entry the catalogue holds is offered on both rows, pair-only included (FX-19).
            for side in ("in", "out"):
                assert rows[side]["options"] == [""] + [
                    entry["transition_id"] for entry in catalogue], rows[side]
            result["rows_on_the_overlapping_pair"] = rows

            # **The sentence really fits.** Two of Epic 9's eleven were controls whose own text was
            # squeezed out of the row it was in, and every automated gate passed both.
            for control in ("transition-in-note",):
                fit = driver.execute_script(OVERFLOWS, "#" + control)
                assert fit and fit["visible"], (control, fit)
                assert fit["clippedRight"] <= 1, (
                    "the row's sentence runs past the edge of its own row", control, fit)
                assert fit["scrollOverflow"] <= 1, (control, fit)
                result[f"fit_{control}"] = fit

            # **And the row survives a narrow window.** The head is a flex row of two texts, which
            # is the shape two of Epic 9's eleven defects had, so it is measured at the same five
            # widths `e2e_timeline_scroll.py` sweeps the transport bar at rather than at the one
            # width this script happens to open.
            narrow_rows = {}
            for width in (1600, 1280, 1024, 900, 820):
                driver.set_window_size(width, 1000)
                settle(driver, "#shot-panel-effects", quiet_ms=250)
                fit = driver.execute_script(ROW_HEAD_FIT, "transition-out")
                assert fit, width
                assert fit["overflowRight"] <= 1, (
                    f"the transition row's head runs past the edge of its own row at {width}px",
                    fit)
                assert fit["selectOverflow"] <= 1, (
                    f"the transition select runs past its row at {width}px", fit)
                assert fit["noteOverflow"] is None or fit["noteOverflow"] <= 1, (
                    f"the row's sentence runs past its row at {width}px", fit)
                assert fit["nameClipped"] <= 1, (
                    f"the row's label is clipped by its own box at {width}px", fit)
                if fit["gap"] is not None:
                    assert fit["gap"] >= 0, (
                        f"the length readout has run into the row's label at {width}px", fit)
                narrow_rows[str(width)] = {key: round(value, 2) if isinstance(value, (int, float))
                                           else value for key, value in fit.items()}
            driver.save_screenshot(str(artifact_dir() / f"{NAME}-07-rows-at-820px.png"))
            driver.set_window_size(1600, 1100)
            settle(driver, "#shot-panel-effects", quiet_ms=300)
            result["rows_across_widths"] = narrow_rows

            # The one-sided row, on the boundary where two clips merely meet.
            select_clip(driver, wait, "shot_03")
            open_effects_tab(driver, wait)
            one_sided = driver.execute_script(ROW_FACTS, "transition-out")
            assert one_sided["state"] == "one-sided", one_sided
            assert one_sided["edge"] == "dim", one_sided
            assert channels(one_sided["borderLeftColor"])[:3] != (0x5b, 0x9b, 0xd5), one_sided
            assert one_sided["note"] == (
                "No overlap — this treats shot 03's last frames, then cuts."), one_sided
            assert one_sided["disabled"] is False
            driver.save_screenshot(str(artifact_dir() / f"{NAME}-02-row-one-sided.png"))
            result["one_sided_row"] = one_sided

            fit = driver.execute_script(OVERFLOWS, "#transition-out-note")
            assert fit["clippedRight"] <= 1 and fit["scrollOverflow"] <= 1, fit
            result["fit_one_sided_note"] = fit

            # === 6. The gesture, end to end ===================================================
            select_clip(driver, wait, "shot_01")
            open_effects_tab(driver, wait)
            clear_toasts(driver)
            chooser = driver.find_element(By.ID, "transition-out")
            visible_and_clickable(driver, chooser, "the Transition out row's select")
            driver.execute_script(
                "arguments[0].value = arguments[1];"
                "arguments[0].dispatchEvent(new Event('change', { bubbles: true }));",
                chooser, blend["transition_id"])
            said = wait_for_toast(driver, wait, "to match")
            assert "Shot 02's transition in set to" in said, said
            assert "Shot 01's transition out" in said, said
            assert blend["label"] in said, said
            result["mirror_toast"] = said

            deadline = time.monotonic() + 12
            stored = transitions(server, project_id)
            while time.monotonic() < deadline and not stored["shot_01"]["out"]:
                time.sleep(0.15)
                stored = transitions(server, project_id)
            assert stored["shot_01"]["out"] == blend["transition_id"], stored
            assert stored["shot_02"]["in"] == blend["transition_id"], (
                "the route's mirror did not reach the incoming Shot", stored)
            result["stored_after_set"] = stored

            settle(driver, "#shots-track", quiet_ms=500)
            typed = driver.execute_script(PAINT_CENSUS, '.overlap-band[data-before="shot_01"]')
            red, green, blue, alpha = channels(typed["background"])
            assert (red, green, blue) == (0x5b, 0x9b, 0xd5), (
                ("the typed band is not painting `--blue` -- if the fill resolved to nothing, the "
                 "`color-mix` declaration did not land and its hex fallback did not either"),
                typed["background"],
            )
            assert 0.18 <= alpha <= 0.26, ("the typed band's fill is not at 22%", typed)
            assert channels(typed["borderTop"])[:3] == (0x5b, 0x9b, 0xd5), typed
            assert typed["title"] and blend["label"].upper() in typed["title"], typed
            assert typed["ariaLabel"] == typed["title"], typed
            result["typed_paint"] = typed

            zoom_to(driver, 6)
            settle(driver, "#shots-track", quiet_ms=300)
            lettered = driver.execute_script(
                PAINT_CENSUS, '.overlap-band[data-before="shot_01"]')
            assert lettered["label"], (
                "a 1.5s overlap at the maximum zoom is still not drawing its type as text",
                lettered)
            assert lettered["label"]["text"] == blend["label"].upper(), lettered["label"]
            assert lettered["label"]["width"] <= lettered["box"]["width"], (
                "the band's label is wider than the band it is inside", lettered)
            # **The label reads against what is under it.** The band spans a boundary, so the
            # region beneath it is the later clip's own left edge -- where `.clip-id` and
            # `.clip-prompt` are drawn. The first pass centred the label, as DESIGN 3 says, and
            # `CUT` landed straight through the word "dark.": both illegible, and invisible to
            # every gate but a screenshot. It is drawn along the bottom of the band now, where the
            # clip paints nothing, and this is the measurement that keeps it there.
            against = driver.execute_script(LABEL_AGAINST_THE_CLIP_TEXT, "shot_01")
            assert against and against["painted"] > 4, (
                "no clip text was measured, so this check would pass over anything", against)
            # And the chip is really there, or the half of this check that is about the chip
            # column would be passing over nothing.
            assert driver.find_elements(
                By.CSS_SELECTOR, '#shots-track .shot-clip[data-shot-id="shot_01"] .clip-fx'), (
                "the fixture no longer carries an effects chip, so the label is not being "
                "measured against one")
            assert not against["collisions"], (
                ("the Overlap band's label is drawn over text the clip beneath it is painting, so "
                 "both are unreadable"),
                against,
            )
            result["label_against_clip_text"] = against
            driver.save_screenshot(str(artifact_dir() / f"{NAME}-03-band-typed-lettered.png"))
            result["typed_label"] = lettered["label"]

            # And at the default zoom the same band is 25px, which is **below** the width its
            # own label needs once the chip column's 33px is taken out of it -- so no label is
            # drawn at all rather than one squeezed into the column. Asserted rather than skipped:
            # a check that quietly passes when there is nothing to look at is not a check.
            zoom_to(driver, -6)
            settle(driver, "#shots-track", quiet_ms=300)
            narrow = driver.execute_script(PAINT_CENSUS, '.overlap-band[data-before="shot_01"]')
            assert narrow["label"] is None, (
                ("a band narrower than its own label plus the chip column's inset drew a label "
                 "anyway, which is a word squeezed into a chip"),
                narrow,
            )
            assert narrow["ariaLabel"] and blend["label"].upper() in narrow["ariaLabel"], (
                "and it stopped saying what it is, which is the state carried by the fill alone",
                narrow,
            )
            result["narrow_band_says_it_without_lettering"] = {
                "width": round(narrow["box"]["width"], 2), "aria": narrow["ariaLabel"]}

            # === 7. A pair-only type refuses out loud, and writes nothing ======================
            select_clip(driver, wait, "shot_03")
            open_effects_tab(driver, wait)
            clear_toasts(driver)
            before_refusal = transitions(server, project_id)
            chooser = driver.find_element(By.ID, "transition-out")
            driver.execute_script(
                "arguments[0].value = arguments[1];"
                "arguments[0].dispatchEvent(new Event('change', { bubbles: true }));",
                chooser, pair_only["transition_id"])
            refusal = wait_for_toast(driver, wait, pair_only["label"])
            assert "only exists where two shots overlap" in refusal, refusal
            assert "Drag the two clips across each other" in refusal, refusal
            settle(driver, "#shot-inspector", quiet_ms=500)
            kept = driver.execute_script(
                "const box = document.querySelector('#effects-refusal');"
                "return box ? { text: box.textContent, height: box.getBoundingClientRect().height }"
                " : null;")
            assert kept and refusal.split(" Drag")[0] in kept["text"], (
                "the route's refusal did not survive the toast into the panel", kept)
            assert transitions(server, project_id) == before_refusal, (
                "a refused transition was stored anyway", before_refusal)
            driver.save_screenshot(str(artifact_dir() / f"{NAME}-04-pair-only-refusal.png"))
            result["pair_only_refusal"] = {"toast": refusal, "kept_in_panel": kept}
            clear_toasts(driver)

            # === 8. The Overlap dragged away ==================================================
            select_clip(driver, wait, "shot_01")
            clear_toasts(driver)
            before_drag = windows(server, project_id)
            clip = shot(driver, 0)
            grip = clip.find_element(By.CSS_SELECTOR, ".resize-handle.right")
            visible_and_clickable(driver, grip, "shot_01's right resize handle under the band")
            ActionChains(driver).click_and_hold(grip).move_by_offset(-40, 0).release().perform()
            announced = wait_for_toast(driver, wait, "no longer overlap")
            assert "Shot 01 and Shot 02 no longer overlap" in announced, announced
            assert "treats its own last frames" in announced, announced
            result["overlap_removed_toast"] = announced

            deadline = time.monotonic() + 12
            after_drag = windows(server, project_id)
            while time.monotonic() < deadline and after_drag == before_drag:
                time.sleep(0.15)
                after_drag = windows(server, project_id)
            assert after_drag["shot_01"]["duration"] < before_drag["shot_01"]["duration"], (
                before_drag, after_drag)
            settle(driver, "#shots-track", quiet_ms=600)
            gone = driver.execute_script(
                "return [...document.querySelectorAll('.overlap-band')]"
                ".map((band) => band.dataset.before + '|' + band.dataset.after);")
            assert "shot_01|shot_02" not in gone, (
                "the band is still drawn over a boundary whose Overlap has been dragged away",
                gone)
            # The stored types are retained (FX-16): removing the Overlap converts them, it does
            # not clear them.
            retained = transitions(server, project_id)
            assert retained["shot_01"]["out"] == blend["transition_id"], retained
            assert retained["shot_02"]["in"] == blend["transition_id"], retained
            result["after_the_drag"] = {"bands": gone, "stored": retained,
                                        "windows": after_drag}
            driver.save_screenshot(str(artifact_dir() / f"{NAME}-05-overlap-dragged-away.png"))

            # And the row that was paired now says what will actually happen instead.
            select_clip(driver, wait, "shot_01")
            open_effects_tab(driver, wait)
            converted = driver.execute_script(ROW_FACTS, "transition-out")
            assert converted["state"] == "one-sided", converted
            assert converted["edge"] == "dim", converted
            assert converted["note"] == (
                "No overlap — this treats shot 01's last frames, then cuts."), converted
            assert converted["value"] == blend["transition_id"], (
                "the row forgot the stored type when the Overlap went away", converted)
            assert converted["length"], (
                "a one-sided transition states no length at all, so its bound is invisible",
                converted)
            result["converted_row"] = converted
            driver.save_screenshot(str(artifact_dir() / f"{NAME}-06-row-converted.png"))

            # === 9. A geometry the export refuses says so, and does not read as a blend =======
            #
            # `shot_05` sits wholly inside `shot_04`, so the incoming Shot's own stretch after the
            # blend runs **backwards** by 240 frames and `assembly._paired_transitions` refuses the
            # boundary. Before this slice the band drew the ordinary `--blue` fill and the row read
            # `paired`: a Director set a Dissolve, saw a blend, and exported a hard cut.
            select_clip(driver, wait, "shot_04")
            open_effects_tab(driver, wait)
            clear_toasts(driver)
            # Untyped first, so the change is a change: nobody has asked this boundary to blend.
            before_typing = driver.execute_script(
                PAINT_CENSUS, '.overlap-band[data-before="shot_04"]')
            assert before_typing["className"] == "overlap-band untyped", (
                ("an overlap with no type stored is being drawn as refused, which would make "
                 "the state below prove nothing"), before_typing)

            chooser = driver.find_element(By.ID, "transition-out")
            visible_and_clickable(driver, chooser, "shot_04's Transition out select")
            driver.execute_script(
                "arguments[0].value = arguments[1];"
                "arguments[0].dispatchEvent(new Event('change', { bubbles: true }));",
                chooser, blend["transition_id"])
            wait_for_toast(driver, wait, "to match")
            deadline = time.monotonic() + 12
            stored = transitions(server, project_id)
            while time.monotonic() < deadline and not stored["shot_04"]["out"]:
                time.sleep(0.15)
                stored = transitions(server, project_id)
            assert stored["shot_04"]["out"] == blend["transition_id"], stored
            settle(driver, "#shots-track", quiet_ms=600)

            refused = driver.execute_script(
                PAINT_CENSUS, '.overlap-band[data-before="shot_04"]')
            assert refused, "the band over the refused boundary is not on the track at all"
            assert refused["className"] == "overlap-band refused", refused

            # **It does not read as a live blend.** No fill at all, and no `--blue` on any edge --
            # the two things the typed band is, measured rather than inferred from the class.
            assert refused["background"] in ("transparent", "rgba(0, 0, 0, 0)"), (
                ("a boundary the export refuses is painting the transition's fill, so the "
                 "timeline is still promising a blend"),
                refused["background"],
            )
            for edge in ("borderTop", "borderBottom"):
                assert channels(refused[edge])[:3] != (0x5b, 0x9b, 0xd5), (
                    "a refused boundary has taken `--blue`, which means a transition will run",
                    edge, refused[edge])

            # **And it is told from the untyped hatch without hue** (UX-DR15, standing law 7): a
            # cross-hatch rather than a one-way one, inside a dashed box on all four sides. Both
            # are measured against the untyped band drawn on the same track at the same moment,
            # so this cannot pass on two bands that merely both exist.
            untyped_now = driver.execute_script(
                PAINT_CENSUS, '.overlap-band[data-before="shot_02"]')
            assert refused["image"].count("repeating-linear-gradient") == 2, (
                "the refused band is not drawing its cross-hatch", refused["image"])
            assert untyped_now["image"].count("repeating-linear-gradient") == 1, (
                "the untyped band has changed, so the two are no longer distinguishable",
                untyped_now["image"])
            assert refused["image"] != untyped_now["image"], (refused["image"],
                                                              untyped_now["image"])
            # A **closed** box against the untyped band's open one, and solid rather than dashed.
            # The clip's own border is already drawn through this hatch as a dotted `--amber` or
            # `--acid` line one pixel inside the band's edge -- R-40's "readable through" working
            # as designed -- and a dashed band edge put a second broken line immediately above it,
            # which read as one noisy multicoloured stripe. Found by looking at
            # `overlap-band-08-band-refused.png`, and invisible to every computed-style assertion
            # because the band's own colour is `--line-strong` either way; what is asserted here is
            # the part that *is* executable, which is that the edge is neutral, continuous and on
            # all four sides.
            assert refused["borderTopStyle"] == "solid", refused
            assert refused["borderLeftWidth"] == "1px", (
                "the refused band has no side edges, so its box is not closed", refused)
            assert channels(refused["borderTop"])[:3] == channels(
                untyped_now["borderTop"])[:3], (
                ("the refused band's edge is not the neutral the untyped band's is, so it has "
                 "spent an accent on a state that is not an error"), refused, untyped_now)
            assert untyped_now["borderLeftWidth"] == "0px", (
                ("the untyped band has grown side edges, so the closed box no longer tells the "
                 "two apart"), untyped_now)

            # The band is a real region and it letters: 15 s at the default zoom is 249 px, which
            # is five times what `NO BLEND` needs. The label says the **outcome**, because
            # `DISSOLVE` over a boundary that hard-cuts is the sentence this slice exists to stop.
            assert refused["box"]["width"] > 200, refused["box"]
            assert refused["label"] and refused["label"]["text"] == "NO BLEND", refused["label"]
            assert refused["label"]["width"] <= refused["box"]["width"], refused
            # And it says which type was set, and that it will not run, at every width.
            assert refused["title"] == refused["ariaLabel"], refused
            assert blend["label"].upper() in refused["title"], refused["title"]
            assert "will not blend it" in refused["title"], refused["title"]
            assert "15.00s overlap between shot 04 and shot 05" in refused["title"], refused
            result["refused_band"] = refused
            driver.save_screenshot(str(artifact_dir() / f"{NAME}-08-band-refused.png"))

            # The row says why, with the numbers, and offers nothing to watch.
            select_clip(driver, wait, "shot_04")
            open_effects_tab(driver, wait)
            refused_row = driver.execute_script(ROW_FACTS, "transition-out")
            assert refused_row["state"] == "refused", refused_row
            assert refused_row["edge"] == "dim", refused_row
            assert channels(refused_row["borderLeftColor"])[:3] != (0x5b, 0x9b, 0xd5), refused_row
            assert refused_row["borderLeftStyle"] == "dashed", (
                ("the refused row is drawn exactly like an unoverlapped one, so the edge says "
                 "nothing the note does not"), refused_row)
            assert refused_row["note"] == (
                "Will not blend — on the assembly grid this boundary is 120 frames of shot 04, a "
                "360-frame blend, then -240 frames of shot 05, so the export cuts here."
            ), refused_row["note"]
            assert refused_row["length"] == "", (
                "a boundary that will not blend is stating a blend length", refused_row)
            assert refused_row["value"] == blend["transition_id"], (
                "the row forgot the stored type", refused_row)
            assert refused_row["preview"] is False, (
                "the row offers to play a blend the export will not compose", refused_row)
            result["refused_row"] = refused_row

            # The sentence really fits its own box at this width and at the narrowest one the rest
            # of this script sweeps -- it is the longest note either row can carry.
            for width in (1600, 820):
                driver.set_window_size(width, 1000)
                settle(driver, "#shot-panel-effects", quiet_ms=250)
                fit = driver.execute_script(OVERFLOWS, "#transition-out-note")
                assert fit and fit["visible"], (width, fit)
                assert fit["clippedRight"] <= 1 and fit["scrollOverflow"] <= 1, (width, fit)
                result[f"fit_refused_note_{width}"] = fit
            driver.set_window_size(1600, 1100)
            settle(driver, "#shot-panel-effects", quiet_ms=300)
            driver.save_screenshot(str(artifact_dir() / f"{NAME}-09-row-refused.png"))

            driver.save_screenshot(str(artifact_dir() / f"{NAME}-workspace.png"))
            # One deliberate 422, declared by name: section 7 drives the route into refusing a
            # pair-only type on a boundary with no Overlap, which is the whole point of that
            # section. Separated out rather than filtered away, so anything else still fails.
            console_gate(driver, NAME, result, expected=[
                unreachable.removeprefix("http://"),
                ("/shots/shot_03/transitions - Failed to load resource: the server "
                 "responded with a status of 422"),
            ])
            report(NAME, result)
        except TimeoutException as error:  # pragma: no cover - a real failure, not a skip
            raise AssertionError(f"the browser stopped waiting: {error}") from error
        finally:
            driver.quit()


if __name__ == "__main__":
    main()
