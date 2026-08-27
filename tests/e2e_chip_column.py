"""Browser QA for the clip chip column (DESIGN 3, amended 2026-08-25).

Purely visual change, so this script is the point of the work rather than a formality. What it
proves, in order:

1. The lone `ƒ` chip lands on *exactly* the pixels it landed on before the column existed --
   measured by reconstituting the old rule in the live page and comparing rectangles, not by
   trusting two numbers that happen to match.
2. Two chips and four chips stack up the right edge inside the clip's own 82px, with nothing
   clipped by `.shot-clip { overflow: hidden }`. The extra chips are injected into the DOM here
   and removed again -- an experiment, not shipped code.
3. A narrow clip (min-width 40px at minimum zoom) still reads.
4. Row geometry is unmoved: clip height and track height are what they were.

**No GPU is spent and nothing reaches `/prompt`.** Every write here is `PUT .../shots` or
`PUT .../shots/{id}/effects`, both of which only save a manifest. ComfyUI is pointed at a dead
port and never contacted.

Run from the repo root -- it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_chip_column.py [--port 8781]

Assumes: nothing listening on the port, Microsoft Edge and its WebDriver installed, and
`music_video_producer` importable from this checkout's `src/`. ComfyUI does not need to be
running.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
from pathlib import Path

from e2e_support import (
    ManagedServer,
    artifact_dir,
    console_gate,
    edge_driver,
    post_json,
    put_json,
    report,
    settle,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

NAME = "chip-column"
WIDE = "shot_01"      # 6s -- a comfortable clip
NARROW = "shot_02"    # 2s -- 32px at base zoom, floored to the 40px min-width at minimum zoom
MIDDLE = "shot_03"    # 4s
PLAIN = "shot_04"     # 5s, no effects: the control


def dead_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def seed(base_url: str) -> str:
    project = post_json(f"{base_url}/api/projects", {"name": "Chip column browser QA"})
    project_id = project["id"]
    put_json(f"{base_url}/api/projects/{project_id}/shots", {"shots": [
        {"id": WIDE, "start": 0, "duration": 6, "seed": 11, "mode": "text_to_video",
         "status": "draft",
         "prompt": "The rooftop at dusk, wide, the city behind her, neon coming up slowly."},
        {"id": NARROW, "start": 6, "duration": 2, "seed": 12, "mode": "text_to_video",
         "status": "draft",
         "prompt": "Cut to her hands on the rail, very close, held for a beat and no longer."},
        {"id": MIDDLE, "start": 8, "duration": 4, "seed": 13, "mode": "text_to_video",
         "status": "draft", "prompt": "The stairwell, handheld, following her down two flights."},
        {"id": PLAIN, "start": 12, "duration": 5, "seed": 14, "mode": "text_to_video",
         "status": "draft", "prompt": "Street level, static, the door opening into the light."},
    ]})
    for shot, stack in (
        (WIDE, [{"effect": "grain", "enabled": True, "parameters": {}},
                {"effect": "punch_in", "enabled": False, "parameters": {}}]),
        (NARROW, [{"effect": "grain", "enabled": True, "parameters": {}}]),
        (MIDDLE, [{"effect": "vignette", "enabled": True, "parameters": {}}]),
    ):
        put_json(f"{base_url}/api/projects/{project_id}/shots/{shot}/effects", {"effects": stack})
    return project_id


#: Everything about one clip's chips a Director could see, measured off the live page. Offsets are
#: taken from the clip's own border box, so "where the chip sits" is a number that survives the
#: clip moving, and `escaped` is the assertion `overflow: hidden` would otherwise hide from us.
MEASURE = """
const id = arguments[0];
const clip = document.querySelector('#shots-track .shot-clip[data-shot-id="' + id + '"]');
if (!clip) return null;
const cr = clip.getBoundingClientRect();
const column = clip.querySelector('.clip-chips');
const prompt = clip.querySelector('.clip-prompt');
const style = getComputedStyle(prompt);
let textRight = null;
if (prompt.firstChild) {
  const range = document.createRange();
  range.selectNodeContents(prompt);
  const rects = [...range.getClientRects()];
  textRight = rects.length ? Math.max(...rects.map((r) => r.right)) : null;
}
const inside = (r, box) => r.top >= box.top - 0.5 && r.bottom <= box.bottom + 0.5
  && r.left >= box.left - 0.5 && r.right <= box.right + 0.5;
const pr = prompt.getBoundingClientRect();
// Painted glyphs only: `.clip-prompt` clamps to two lines under its own `overflow: hidden` and
// the clip has another, so a laid-out rectangle is not the same thing as ink on a screen.
const painted = [];
if (prompt.firstChild && prompt.firstChild.nodeType === 3) {
  const node = prompt.firstChild;
  for (let i = 0; i < node.length; i += 1) {
    const one = document.createRange();
    one.setStart(node, i);
    one.setEnd(node, i + 1);
    const rect = one.getBoundingClientRect();
    if (!rect.width && !rect.height) continue;
    if (inside(rect, pr) && inside(rect, cr)) painted.push({char: node.data[i], rect: rect});
  }
}
const inkHits = [];
const chips = [...clip.querySelectorAll('.clip-fx')].map((chip) => {
  const r = chip.getBoundingClientRect();
  const cs = getComputedStyle(chip);
  painted.forEach((g) => {
    const x = Math.min(g.rect.right, r.right) - Math.max(g.rect.left, r.left);
    const y = Math.min(g.rect.bottom, r.bottom) - Math.max(g.rect.top, r.top);
    if (x > 0 && y > 0) inkHits.push({char: g.char, x: x, y: y});
  });
  return {
    text: chip.textContent,
    width: r.width, height: r.height,
    fromRight: cr.right - r.right, fromBottom: cr.bottom - r.bottom, fromTop: r.top - cr.top,
    background: cs.backgroundColor, colour: cs.color, font: cs.fontFamily,
    radius: cs.borderRadius,
    escaped: r.top < cr.top || r.bottom > cr.bottom || r.left < cr.left || r.right > cr.right,
    hitTaken: (() => {
      const hit = document.elementFromPoint((r.left + r.right) / 2, (r.top + r.bottom) / 2);
      return hit ? hit.className : null;
    })(),
  };
});
return {
  clip: {width: cr.width, height: cr.height, classes: clip.className,
         chipsAttr: clip.dataset.chips || null,
         inlineStyle: clip.getAttribute('style')},
  inkHits: inkHits,
  paintedChars: painted.length,
  paintedText: painted.map((g) => g.char).join(''),
  track: (() => {
    const t = clip.closest('.track');
    const r = t.getBoundingClientRect();
    return {height: r.height, minHeight: getComputedStyle(t).minHeight};
  })(),
  column: column ? (() => {
    const r = column.getBoundingClientRect();
    const cs = getComputedStyle(column);
    return {width: r.width, height: r.height, fromRight: cr.right - r.right,
            fromBottom: cr.bottom - r.bottom, fromTop: r.top - cr.top,
            direction: cs.flexDirection, gap: cs.gap, position: cs.position};
  })() : null,
  chips,
  prompt: {
    paddingRight: style.paddingRight, paddingLeft: style.paddingLeft,
    contentWidth: prompt.clientWidth
      - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight),
    text: prompt.textContent,
    textRight,
    gapToColumn: (column && textRight !== null)
      ? column.getBoundingClientRect().left - textRight : null,
  },
};
"""

#: The rule as it shipped before this change, put back over the top of the new one. `display:
#: contents` takes the column out of layout entirely, so the chip positions against `.shot-clip`
#: exactly as it did when it carried these three declarations itself.
OLD_RULE = """
const style = document.createElement('style');
style.id = 'old-chip-rule';
style.textContent = '.clip-chips { display: contents; }'
  + '.clip-fx { position: absolute; right: 14px; bottom: 4px; }';
document.head.append(style);
"""

#: Chips 2..N, injected. Cloned from the real one so every declaration under test is the shipped
#: one; only the glyph differs, in the reading order DESIGN 3 fixes (`✓ ƒ ⚑`) plus a fourth so the
#: full height budget is exercised. Marked `data-experiment` so removal cannot miss one.
INJECT = """
const id = arguments[0];
const glyphs = arguments[1];
const clip = document.querySelector('#shots-track .shot-clip[data-shot-id="' + id + '"]');
const column = clip.querySelector('.clip-chips');
const model = column.querySelector('.clip-fx');
column.querySelectorAll('[data-experiment]').forEach((node) => node.remove());
glyphs.forEach((glyph) => {
  const chip = model.cloneNode(true);
  chip.textContent = glyph;
  chip.dataset.experiment = '1';
  if (glyph === '\\u2713') column.prepend(chip);
  else column.append(chip);
});
// The count the renderer would have written for this many chips. The stylesheet reads it, so an
// experiment that added chips without it would be testing a column the CSS thinks is one chip
// tall -- which is the arrangement under test, not a substitute for it.
const total = column.querySelectorAll('.clip-fx').length;
clip.dataset.chips = String(total);
return total;
"""

REMOVE = """
document.querySelectorAll('.clip-fx[data-experiment]').forEach((node) => node.remove());
document.querySelectorAll('.shot-clip[data-chips]').forEach((clip) => {
  clip.dataset.chips = String(clip.querySelectorAll('.clip-fx').length);
});
return document.querySelectorAll('.clip-fx').length;
"""


def shot_element(driver, shot_id: str):
    return driver.find_element(
        By.CSS_SELECTOR, f'#shots-track .shot-clip[data-shot-id="{shot_id}"]')


def shoot(driver, shot_id: str, label: str) -> str:
    """One clip, cropped, plus the track it sits in -- a 40px clip alone tells you nothing."""
    path = artifact_dir() / f"{NAME}-{label}.png"
    shot_element(driver, shot_id).screenshot(str(path))
    wide = artifact_dir() / f"{NAME}-{label}-track.png"
    driver.find_element(By.CSS_SELECTOR, "#shots-track").screenshot(str(wide))
    return str(path)


def close(a: float, b: float, tolerance: float = 0.51) -> bool:
    return abs(a - b) <= tolerance


def main() -> None:
    port = 8781
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    result: dict[str, object] = {}
    comfy_root = Path(tempfile.mkdtemp(prefix="mvp-chip-comfy-"))
    unreachable = f"http://127.0.0.1:{dead_port()}"
    os.environ["MVP_COMFY_ROOT"] = str(comfy_root)
    os.environ["MVP_COMFY_URL"] = unreachable
    os.environ["MVP_LLM_EJECT_BEFORE_RENDER"] = "false"
    os.environ["MVP_LLM_BASE_URL"] = ""

    with ManagedServer(port, label=NAME) as server:
        result["server_identity"] = server.evidence
        result["data_root"] = str(server.data_root)
        project_id = seed(server.base_url)
        driver = edge_driver()
        wait = WebDriverWait(driver, 25)
        try:
            driver.get(server.base_url)
            wait.until(EC.presence_of_element_located((By.ID, "project-select")))
            wait.until(lambda b: b.find_element(
                By.CSS_SELECTOR, f'#project-select option[value="{project_id}"]')).click()
            wait.until(lambda b: b.find_element(By.ID, "project-select").get_attribute("value")
                       == project_id)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            settle(driver, "#shots-track")

            # === 1. One chip, where one chip has always been ================================
            one = driver.execute_script(MEASURE, WIDE)
            assert one and len(one["chips"]) == 1, one
            result["one_chip"] = one
            # The chip that ships costs the prompt nothing: the column stops below the last line
            # the prompt paints, so the prompt keeps its plain 9px inset and no glyph is under it.
            assert one["clip"]["chipsAttr"] == "1", one["clip"]
            assert one["prompt"]["paddingRight"] == one["prompt"]["paddingLeft"], one["prompt"]
            assert one["inkHits"] == [], one["inkHits"]
            shoot(driver, WIDE, "01-one-chip")

            # The same page, with the pre-change rule laid back over it. Identical rectangles is
            # the only claim worth making about the case that currently ships.
            driver.execute_script(OLD_RULE)
            before = driver.execute_script(MEASURE, WIDE)
            driver.execute_script("document.getElementById('old-chip-rule').remove();")
            after = driver.execute_script(MEASURE, WIDE)
            result["old_rule"] = before["chips"][0]
            result["new_rule"] = after["chips"][0]
            for key in ("width", "height", "fromRight", "fromBottom", "fromTop"):
                assert close(before["chips"][0][key], after["chips"][0][key]), \
                    f"the column moved the lone chip: {key} {before['chips'][0][key]} -> " \
                    f"{after['chips'][0][key]}"
            for key in ("background", "colour", "font", "radius"):
                assert before["chips"][0][key] == after["chips"][0][key], key
            result["identical_to_old_rule"] = True

            # The control: a clip with no effects draws no column and takes no inset.
            plain = driver.execute_script(MEASURE, PLAIN)
            assert plain["column"] is None and not plain["chips"], plain
            assert plain["clip"]["chipsAttr"] is None, plain["clip"]
            assert plain["prompt"]["paddingRight"] == plain["prompt"]["paddingLeft"], plain
            result["no_effects_clip"] = plain

            # === 2. Two chips, then four ====================================================
            for label, glyphs in (("02-two-chips", ["\u2691"]),
                                  ("03-four-chips", ["\u2713", "\u2691", "\u25cf"])):
                count = driver.execute_script(INJECT, WIDE, glyphs)
                measured = driver.execute_script(MEASURE, WIDE)
                assert len(measured["chips"]) == count == len(glyphs) + 1, (count, measured)
                shoot(driver, WIDE, label)
                for chip in measured["chips"]:
                    assert not chip["escaped"], (label, chip)
                    assert chip["hitTaken"] == "clip-fx", (label, chip)
                assert close(measured["clip"]["height"], one["clip"]["height"]), measured["clip"]
                assert close(measured["track"]["height"], one["track"]["height"]), measured
                # From the second chip the column reaches the text, so the inset is charged and
                # the text is clear of it -- no ink under a chip, and a real gap beside it.
                assert measured["clip"]["chipsAttr"] == str(count), measured["clip"]
                assert measured["prompt"]["paddingRight"] == "33px", measured["prompt"]
                assert measured["prompt"]["gapToColumn"] > 0, measured["prompt"]
                assert measured["inkHits"] == [], measured["inkHits"]
                result[label.split("-", 1)[1].replace("-", "_")] = measured

            assert driver.execute_script(REMOVE) == 3, "the experiment was not fully removed"
            back = driver.execute_script(MEASURE, WIDE)
            assert len(back["chips"]) == 1, back
            for key in ("fromRight", "fromBottom", "fromTop"):
                assert close(back["chips"][0][key], one["chips"][0][key]), key
            result["experiment_reverted"] = back["chips"][0]

            # === 3. The narrow clip, at the minimum zoom the timeline offers =================
            narrow_base = driver.execute_script(MEASURE, NARROW)
            result["narrow_at_base_zoom"] = narrow_base
            shoot(driver, NARROW, "04-narrow-base-zoom")
            for _ in range(12):
                driver.find_element(By.ID, "zoom-out").click()
            settle(driver, "#shots-track")
            narrow = driver.execute_script(MEASURE, NARROW)
            result["narrow_at_min_zoom"] = narrow
            result["zoom_label"] = driver.find_element(By.ID, "zoom-label").text
            shoot(driver, NARROW, "05-narrow-min-zoom")
            assert len(narrow["chips"]) == 1, narrow
            assert not narrow["chips"][0]["escaped"], narrow["chips"][0]
            assert close(narrow["chips"][0]["fromRight"], one["chips"][0]["fromRight"]), narrow
            wide_at_min = driver.execute_script(MEASURE, WIDE)
            result["wide_at_min_zoom"] = wide_at_min
            shoot(driver, WIDE, "06-wide-min-zoom")
            for _ in range(12):
                driver.find_element(By.ID, "zoom-in").click()
            settle(driver, "#shots-track")

            # === 4. Nothing about the row moved =============================================
            final = driver.execute_script(MEASURE, WIDE)
            result["geometry"] = {
                "clip_height": final["clip"]["height"],
                "track_height": final["track"]["height"],
                "track_min_height": final["track"]["minHeight"],
                "inline_style": final["clip"]["inlineStyle"],
                "plain_clip_height": plain["clip"]["height"],
            }
            assert close(final["clip"]["height"], plain["clip"]["height"]), result["geometry"]
            assert close(final["clip"]["height"], 82), result["geometry"]
            # `.shots-track` carries its own 110px floor over `.track`'s 62px; both are static
            # numbers in the stylesheet, and the point is that neither moved for a chip.
            assert final["track"]["minHeight"] == "110px", result["geometry"]
            assert close(final["track"]["height"], plain["track"]["height"]), result["geometry"]
            console_gate(driver, NAME, result)
        finally:
            driver.quit()
    report(NAME, result)
    print(json.dumps({"screenshots": sorted(
        str(p) for p in artifact_dir().glob(f"{NAME}-*.png"))}, indent=2))


if __name__ == "__main__":
    main()
