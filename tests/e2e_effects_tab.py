"""Browser QA for the shot inspector's Effects tab (slice C2).

The offline harness executes every decision this surface makes -- what a card says, what a write
would contain, whether a stack differs from the stored one -- against a stub DOM under node. What
a stub DOM structurally cannot see is what this script is for: a panel that is `hidden` and
painted anyway, a card at 45% whose controls have become unclickable, a slider whose thumb and
fill disagree, a tab strip that loses the button under the pointer when it redraws, and a
disclosure that opens behind something else. Epic 8's two worst defects -- a waveform buried under
full-height marks, and a dropdown with no background because a token was undefined -- both passed
every automated gate and were caught only by looking.

**No GPU is spent and nothing reaches `/prompt`.** Every write here is `PUT .../shots/{id}/effects`,
which validates a stack and saves a manifest. ComfyUI is pointed at a dead port and never
contacted.

What is driven, in order:

1. **Two tabs, `Shot Info` active, holding what it held.** The prompt box, the seed row and the
   compile button are all inside the *info* panel, and the Effects panel is `hidden` and taking
   no room -- the `[hidden]` failure mode this repo has already had once, where a stylesheet
   painted a pane the attribute said was gone.
2. **The strip is a real tablist and it works from the keyboard.** Arrow keys move it, focus
   follows, and `aria-selected` follows both.
3. **The empty tab offers the picker and nothing pretending to be a stack.**
4. **The picker opens as a grouped list under Consolas family headers**, with no images in it,
   and every option is hit-testable at its centre.
5. **Adding writes, and the card is what the server sent back.**
6. **A disabled card is retained at 45% with its controls still readable and still clickable** --
   the assertion the mockup's own note asks for, and one an opacity rule can invert silently.
7. **A slider writes on release and not on input**, proved by counting the requests the browser
   really issued between the two events, with the readout and the fill following the thumb.
8. **The route's own refusal is shown whole** -- driven for real by grading with a look on a
   machine whose looks folder is empty, which is the refusal C1 wrote for exactly this moment.
9. **The active tab survives the rebuild, and a different Shot returns to `Shot Info`.**
10. **A locked Shot draws every writing control disabled and states the lock.**

A screenshot of the tab carrying two cards, one of them disabled, is written to `test-artifacts/`.

Run from the repo root -- it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_effects_tab.py [--port 8778]

Assumes: nothing listening on the port, Microsoft Edge and its WebDriver installed, and
`music_video_producer` importable from this checkout's `src/`. ComfyUI does not need to be running.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import time
from pathlib import Path

from e2e_support import (
    ManagedServer,
    artifact_dir,
    console_gate,
    edge_driver,
    get_json,
    post_json,
    put_json,
    report,
    resource_hits,
    settle,
    visible_and_clickable,
    wait_for_toast,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

NAME = "effects-tab"
SHOT = "shot_01"
OTHER = "shot_02"


def dead_port() -> int:
    """A port nothing is listening on, so the ComfyUI reads fail fast and change nothing."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def seed_project(base_url: str) -> str:
    """Two shots, no effects on either. Everything else this tab needs comes from the catalogue."""
    project = post_json(f"{base_url}/api/projects", {"name": "Effects tab browser QA"})
    project_id = project["id"]
    put_json(f"{base_url}/api/projects/{project_id}/shots", {"shots": [
        {"id": SHOT, "start": 0, "duration": 4, "prompt": "The rooftop, wide, at dusk.",
         "mode": "text_to_video", "status": "draft", "seed": 11},
        {"id": OTHER, "start": 4, "duration": 4, "prompt": "The same rooftop, close.",
         "mode": "text_to_video", "status": "draft", "seed": 12},
    ]})
    return project_id


def stored_stack(base_url: str, project_id: str, shot_id: str = SHOT) -> list[dict]:
    return get_json(f"{base_url}/api/projects/{project_id}/shots/{shot_id}/effects")["effects"]


def wait_for_stack(base_url: str, project_id: str, predicate, what: str,
                   timeout: float = 12.0) -> list[dict]:
    """The stored stack, once the write this gesture started has actually landed.

    Polled rather than read once: `settle` watches the panel stop moving, and the panel stops
    moving the moment the reply is applied -- which is a different instant from the manifest being
    on disk. Reading once made this script fail on its own timing rather than on the behaviour.
    """
    deadline = time.time() + timeout
    stack: list[dict] = []
    while time.time() < deadline:
        stack = stored_stack(base_url, project_id)
        if predicate(stack):
            return stack
        time.sleep(0.2)
    raise AssertionError(f"{what}; the stored stack is {stack}")


def open_panel(driver, panel: str) -> None:
    driver.find_element(By.CSS_SELECTOR, f'[data-panel="{panel}"]').click()


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


#: Everything about the two panels a Director could see, measured rather than inferred. The
#: `hidden`-and-painted-anyway case is asserted explicitly: this repository has already shipped a
#: pane carrying `hidden` that a `display: block` rule kept on screen.
PANELS = """
const box = (node) => {
  if (!node) return null;
  const rect = node.getBoundingClientRect();
  const style = getComputedStyle(node);
  return {
    width: rect.width, height: rect.height, top: rect.top, left: rect.left,
    display: style.display, visibility: style.visibility, opacity: style.opacity,
    hiddenAttr: node.hasAttribute('hidden'),
  };
};
const tab = (id) => {
  const node = document.getElementById(id);
  if (!node) return null;
  return {
    text: node.textContent,
    selected: node.getAttribute('aria-selected'),
    controls: node.getAttribute('aria-controls'),
    tabindex: node.getAttribute('tabindex'),
    font: getComputedStyle(node).fontFamily,
    transform: getComputedStyle(node).textTransform,
    underline: getComputedStyle(node).borderBottomColor,
    colour: getComputedStyle(node).color,
  };
};
return {
  strip: box(document.querySelector('.shot-tabs')),
  stripRole: document.querySelector('.shot-tabs')?.getAttribute('role') || null,
  info: box(document.getElementById('shot-panel-info')),
  effects: box(document.getElementById('shot-panel-effects')),
  infoTab: tab('shot-tab-info'),
  effectsTab: tab('shot-tab-effects'),
  focused: document.activeElement ? document.activeElement.id : null,
  cards: [...document.querySelectorAll('.effect-card')].map((card) => ({
    name: card.querySelector('.effect-name')?.textContent || '',
    family: card.querySelector('.effect-family')?.textContent || '',
    familyTransform: getComputedStyle(card.querySelector('.effect-family')).textTransform,
    opacity: getComputedStyle(card).opacity,
    background: getComputedStyle(card).backgroundColor,
    rows: card.querySelectorAll('.effect-row').length,
    bind: card.querySelector('.effect-bind')
      ? getComputedStyle(card.querySelector('.effect-bind')).color : null,
  })),
  refusal: document.getElementById('effects-refusal')?.textContent || null,
  lock: document.getElementById('effects-locked')?.textContent || null,
  problem: document.getElementById('effects-problem')?.textContent || null,
  pickerHidden: document.getElementById('effect-picker')?.hasAttribute('hidden') ?? null,
  pickerDisplay: document.getElementById('effect-picker')
    ? getComputedStyle(document.getElementById('effect-picker')).display : null,
  pickerImages: document.querySelectorAll('#effect-picker img').length,
  groups: [...document.querySelectorAll('#effect-picker .effect-group')].map((group) => ({
    family: group.querySelector('.effect-family')?.textContent || '',
    font: getComputedStyle(group.querySelector('.effect-family')).fontFamily,
    options: [...group.querySelectorAll('.effect-option')].map((item) => item.textContent),
  })),
  addDisabled: document.getElementById('effect-add')?.disabled ?? null,
};
"""

#: One slider's thumb, its fill and its readout, in page pixels -- so "the fill agrees with the
#: thumb" is a measurement rather than a claim about two numbers that were never drawn together.
SLIDER = """
const input = document.getElementById(arguments[0]);
const fill = document.getElementById(arguments[1]);
const readout = document.getElementById(arguments[2]);
const track = input.parentElement.querySelector('.effect-track');
const inputStyle = getComputedStyle(input);
return {
  value: input.value, min: input.min, max: input.max, step: input.step,
  disabled: input.disabled,
  inputHeight: input.getBoundingClientRect().height,
  inputBorder: inputStyle.borderTopWidth,
  inputPadding: inputStyle.paddingTop,
  inputBackground: inputStyle.backgroundColor,
  sliderHeight: input.parentElement.getBoundingClientRect().height,
  trackHeight: track.getBoundingClientRect().height,
  fillWidth: fill.getBoundingClientRect().width,
  trackWidth: track.getBoundingClientRect().width,
  fillColour: getComputedStyle(fill).backgroundColor,
  trackColour: getComputedStyle(track).backgroundColor,
  readout: readout.textContent,
  readoutFont: getComputedStyle(readout).fontFamily,
};
"""


def panels(driver) -> dict:
    return driver.execute_script(PANELS)


def main() -> None:
    port = 8778
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    result: dict[str, object] = {}
    comfy_root = Path(tempfile.mkdtemp(prefix="mvp-effects-comfy-"))
    unreachable = f"http://127.0.0.1:{dead_port()}"
    os.environ["MVP_COMFY_ROOT"] = str(comfy_root)
    os.environ["MVP_COMFY_URL"] = unreachable
    os.environ["MVP_LLM_EJECT_BEFORE_RENDER"] = "false"
    os.environ["MVP_LLM_BASE_URL"] = ""

    with ManagedServer(port, label=NAME) as server:
        result["server_identity"] = server.evidence
        result["comfy_url"] = unreachable
        project_id = seed_project(server.base_url)
        catalogue = get_json(f"{server.base_url}/api/effects/catalogue")
        result["catalogue"] = {
            "families": catalogue["families"],
            "effects": len(catalogue["effects"]),
            "looks": len(catalogue["looks"]),
        }
        # A `lut_look` card is added at the catalogue's defaults, and a LUT parameter *has* no
        # default -- there is no look that means "leave it alone" -- so the card the picker adds
        # names no look and the route refuses it by name. That is the real refusal step 8 drives,
        # and it holds whether or not this machine has looks on disk.
        assert any(item["effect"] == "lut_look" for item in catalogue["effects"]), catalogue

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

            # === 1. Two tabs, Shot Info active, holding what it held =========================
            seen = panels(driver)
            assert seen["stripRole"] == "tablist", seen
            assert seen["infoTab"]["selected"] == "true", seen["infoTab"]
            assert seen["effectsTab"]["selected"] == "false", seen["effectsTab"]
            assert seen["infoTab"]["controls"] == "shot-panel-info", seen["infoTab"]
            assert seen["effectsTab"]["controls"] == "shot-panel-effects", seen["effectsTab"]
            assert seen["infoTab"]["tabindex"] == "0", seen["infoTab"]
            assert seen["effectsTab"]["tabindex"] == "-1", seen["effectsTab"]
            assert "Consolas" in seen["infoTab"]["font"], seen["infoTab"]
            assert seen["infoTab"]["transform"] == "uppercase", seen["infoTab"]
            assert seen["info"]["height"] > 0 and seen["info"]["display"] != "none", seen["info"]
            # `hidden` *and* not painted. A `display` rule on the panel would beat the user
            # agent's `[hidden]`, which is how this repository once shipped a pane that carried
            # the attribute and took the room anyway.
            assert seen["effects"]["hiddenAttr"] is True, seen["effects"]
            assert seen["effects"]["display"] == "none", seen["effects"]
            assert seen["effects"]["height"] == 0, seen["effects"]
            for control in ("shot-prompt", "shot-seed", "shot-locked", "compile-shot"):
                owner = driver.execute_script(
                    "return document.getElementById(arguments[0])"
                    "?.closest('.shot-tab-panel')?.id || null;", control)
                assert owner == "shot-panel-info", (control, owner)
            result["opens_on"] = {"info": seen["infoTab"], "effects": seen["effectsTab"]}

            # === 2. The strip works from the keyboard ========================================
            driver.find_element(By.ID, "shot-tab-info").send_keys(Keys.ARROW_RIGHT)
            settle(driver, "#shot-inspector", quiet_ms=300)
            moved = panels(driver)
            assert moved["focused"] == "shot-tab-effects", moved["focused"]
            assert moved["effectsTab"]["selected"] == "true", moved["effectsTab"]
            assert moved["effects"]["hiddenAttr"] is False, moved["effects"]
            assert moved["info"]["display"] == "none", moved["info"]
            driver.find_element(By.ID, "shot-tab-effects").send_keys(Keys.ARROW_LEFT)
            settle(driver, "#shot-inspector", quiet_ms=300)
            back = panels(driver)
            assert back["focused"] == "shot-tab-info", back["focused"]
            assert back["infoTab"]["selected"] == "true", back["infoTab"]
            result["keyboard"] = {"right": moved["effectsTab"], "left": back["infoTab"]}

            # === 3. The empty tab: the picker, and nothing pretending to be a stack ==========
            open_effects(driver)
            empty = panels(driver)
            assert empty["cards"] == [], empty["cards"]
            assert empty["pickerHidden"] is True and empty["pickerDisplay"] == "none", empty
            assert empty["addDisabled"] is False, empty
            visible_and_clickable(driver, driver.find_element(By.ID, "effect-add"), "+ Effect")
            result["empty_tab"] = {"cards": 0, "picker_hidden": empty["pickerHidden"]}

            # === 4. The picker is a grouped list, no thumbnails ==============================
            driver.find_element(By.ID, "effect-add").click()
            settle(driver, "#shot-inspector", quiet_ms=300)
            opened = panels(driver)
            assert opened["pickerHidden"] is False and opened["pickerDisplay"] != "none", opened
            assert [group["family"] for group in opened["groups"]] == catalogue["families"], opened
            assert opened["pickerImages"] == 0, "the picker is drawing thumbnails"
            assert all("Consolas" in group["font"] for group in opened["groups"]), opened["groups"]
            offered = [name for group in opened["groups"] for name in group["options"]]
            assert sorted(offered) == sorted(item["label"] for item in catalogue["effects"]), offered
            for option in ("effect-option-grain", "effect-option-punch_in"):
                visible_and_clickable(driver, driver.find_element(By.ID, option), option)
            result["picker"] = opened["groups"]

            # === 5. Adding writes, and the card is what the server sent back =================
            driver.find_element(By.ID, "effect-option-grain").click()
            settle(driver, "#shot-inspector", quiet_ms=500)
            wait_for_stack(
                server.base_url, project_id,
                lambda stack: stack == [{"effect": "grain", "enabled": True, "parameters": {}}],
                "adding Grain from the picker did not write the stack")
            driver.find_element(By.ID, "effect-add").click()
            settle(driver, "#shot-inspector", quiet_ms=300)
            driver.find_element(By.ID, "effect-option-punch_in").click()
            settle(driver, "#shot-inspector", quiet_ms=500)
            wait_for_stack(
                server.base_url, project_id,
                lambda stack: [spec["effect"] for spec in stack] == ["grain", "punch_in"],
                "adding a second effect did not write the stack")
            two = panels(driver)
            assert [card["name"] for card in two["cards"]] == ["Grain", "Punch In"], two["cards"]
            assert [card["family"] for card in two["cards"]] == ["texture", "geometry"], two["cards"]
            assert all(card["familyTransform"] == "uppercase" for card in two["cards"]), two["cards"]
            # The card has a real surface of its own rather than the panel's, which is what makes
            # a stack read as a list of cards instead of a run of rows.
            assert two["cards"][0]["background"] != "rgba(0, 0, 0, 0)", two["cards"][0]
            assert two["effectsTab"]["text"].strip().endswith("2"), two["effectsTab"]["text"]
            result["added"] = two["cards"]

            # === 6. A disabled card is retained, at 45%, still readable and still clickable ==
            toggle = driver.find_element(By.ID, "effect-toggle-1")
            visible_and_clickable(driver, toggle, "the second card's enable toggle")
            toggle.click()
            settle(driver, "#shot-inspector", quiet_ms=500)
            wait_for_stack(
                server.base_url, project_id,
                lambda stack: len(stack) == 2 and stack[1]["enabled"] is False,
                "disabling the second card did not write `enabled: false`")
            settle(driver, "#shot-inspector", quiet_ms=400)
            off = panels(driver)
            assert [card["name"] for card in off["cards"]] == ["Grain", "Punch In"], off["cards"]
            assert abs(float(off["cards"][1]["opacity"]) - 0.45) < 0.01, off["cards"][1]
            assert float(off["cards"][0]["opacity"]) == 1.0, off["cards"][0]
            # Retained means operable: the zoom slider on the disabled card is still hit-testable
            # and still enabled, which is what "keeps its controls readable" has to mean.
            zoom = driver.find_element(By.ID, "effect-param-1-zoom")
            visible_and_clickable(driver, zoom, "the disabled card's Zoom slider")
            assert zoom.is_enabled(), "a disabled effect's controls were disabled with it"
            shot = driver.find_element(By.ID, "shot-inspector")
            shot.screenshot(str(artifact_dir() / f"{NAME}-two-cards-one-disabled.png"))
            driver.save_screenshot(str(artifact_dir() / f"{NAME}-workspace.png"))
            result["disabled_card"] = off["cards"][1]

            # === 7. The slider writes on release, never on input =============================
            before = resource_hits(driver, "/effects")
            driver.execute_script(
                "const input = document.getElementById('effect-param-0-strength');"
                "input.value = '30';"
                "input.dispatchEvent(new Event('input', { bubbles: true }));")
            settle(driver, "#shot-inspector", quiet_ms=300)
            held = driver.execute_script(
                SLIDER, "effect-param-0-strength", "effect-fill-0-strength",
                "effect-readout-0-strength")
            assert resource_hits(driver, "/effects") == before, \
                "the slider wrote to the route while it was still being dragged"
            assert stored_stack(server.base_url, project_id)[0]["parameters"] == {}, \
                "the drag reached the manifest before it was released"
            assert held["readout"] == "30.00", held
            assert "Consolas" in held["readoutFont"], held
            # The fill agrees with the thumb: 30 of 0..60 is half the track, measured in pixels.
            assert abs(held["fillWidth"] / held["trackWidth"] - 0.5) < 0.02, held
            assert held["fillColour"] != held["trackColour"], held
            # No inherited chrome. The generic `input` rule in this stylesheet gives every input a
            # 1px --line border, a 5px radius and 9px of padding; inherited by a range input it
            # drew a 20px bordered pill around a 3px track -- on screen, through every automated
            # gate, until somebody looked at it.
            assert held["inputBorder"] == "0px", held
            assert held["inputPadding"] == "0px", held
            assert held["inputBackground"] == "rgba(0, 0, 0, 0)", held
            assert held["trackHeight"] <= 4, held
            assert held["inputHeight"] == held["sliderHeight"] <= 14, held
            driver.execute_script(
                "document.getElementById('effect-param-0-strength')"
                ".dispatchEvent(new Event('change', { bubbles: true }));")
            settle(driver, "#shot-inspector", quiet_ms=500)
            wait_for_stack(
                server.base_url, project_id,
                lambda stack: stack[0]["parameters"] == {"strength": 30},
                "releasing the slider did not write the number the readout was showing")
            released = resource_hits(driver, "/effects")
            assert released == before + 1, (before, released)
            # ...and released where it was picked up, it sends nothing at all.
            driver.execute_script(
                "const input = document.getElementById('effect-param-0-strength');"
                "input.dispatchEvent(new Event('change', { bubbles: true }));")
            settle(driver, "#shot-inspector", quiet_ms=400)
            assert resource_hits(driver, "/effects") == released, \
                "a slider released where it was picked up still wrote the stack"
            result["slider"] = held

            # === 8. The route's own refusal, shown whole =====================================
            driver.find_element(By.ID, "effect-add").click()
            settle(driver, "#shot-inspector", quiet_ms=300)
            driver.find_element(By.ID, "effect-option-lut_look").click()
            refusal = wait_for_toast(driver, wait, "needs a look chosen")
            settle(driver, "#shot-inspector", quiet_ms=500)
            refused = panels(driver)
            assert refused["refusal"], "the panel says nothing about the refused write"
            assert refusal in refused["refusal"], (refusal, refused["refusal"])
            assert [card["name"] for card in refused["cards"]] == ["Grain", "Punch In"], \
                refused["cards"]
            assert len(stored_stack(server.base_url, project_id)) == 2, \
                stored_stack(server.base_url, project_id)
            driver.find_element(By.ID, "shot-inspector").screenshot(
                str(artifact_dir() / f"{NAME}-refusal.png"))
            result["refusal"] = refusal

            # === 9. The tab survives a rebuild; another Shot returns to Shot Info ============
            select_clip(driver, wait, SHOT)
            same = panels(driver)
            assert same["effectsTab"]["selected"] == "true", \
                "re-selecting the same Shot threw the Director back to Shot Info"
            select_clip(driver, wait, OTHER)
            other = panels(driver)
            assert other["infoTab"]["selected"] == "true", other["infoTab"]
            assert other["effects"]["hiddenAttr"] is True, other["effects"]
            result["tab_persistence"] = {
                "same_shot": same["effectsTab"]["selected"],
                "other_shot": other["infoTab"]["selected"],
            }

            # === 10. A locked Shot: every writing control disabled, and the lock stated ======
            select_clip(driver, wait, SHOT)
            driver.find_element(By.ID, "shot-locked").click()
            settle(driver, "#shot-inspector", quiet_ms=600)
            open_effects(driver)
            locked = panels(driver)
            assert locked["lock"], "the panel does not state the lock"
            assert "locked" in locked["lock"], locked["lock"]
            assert locked["addDisabled"] is True, locked
            for control in ("effect-toggle-0", "effect-remove-0", "effect-param-0-strength"):
                element = driver.find_element(By.ID, control)
                assert not element.is_enabled(), f"{control} is live on a locked shot"
            driver.find_element(By.ID, "shot-inspector").screenshot(
                str(artifact_dir() / f"{NAME}-locked.png"))
            result["locked"] = locked["lock"]

            # The clip says it carries a look, in the corner and in its accessible name.
            chip = driver.execute_script(
                "const clip = document.querySelector"
                "('#shots-track .shot-clip[data-shot-id=\"" + SHOT + "\"] .clip-fx');"
                "if (!clip) return null;"
                "const rect = clip.getBoundingClientRect();"
                "return { text: clip.textContent, label: clip.getAttribute('aria-label'),"
                " width: rect.width, colour: getComputedStyle(clip).color,"
                " font: getComputedStyle(clip).fontFamily };")
            assert chip, "the clip carrying two effects draws no chip"
            assert chip["text"] == "ƒ", chip
            assert "2 effects" in chip["label"], chip
            assert chip["width"] > 0 and "Consolas" in chip["font"], chip
            result["clip_chip"] = chip

            # Unlock, so nothing is left in a state the next reader has to undo.
            driver.find_element(By.ID, "shot-tab-info").click()
            settle(driver, "#shot-inspector", quiet_ms=300)
            driver.find_element(By.ID, "shot-locked").click()
            settle(driver, "#shot-inspector", quiet_ms=500)

            # Removing takes the card off and writes the shortened stack.
            open_effects(driver)
            driver.find_element(By.ID, "effect-remove-1").click()
            settle(driver, "#shot-inspector", quiet_ms=600)
            wait_for_stack(
                server.base_url, project_id,
                lambda stack: [spec["effect"] for spec in stack] == ["grain"],
                "removing the second card did not write the shortened stack")
            assert [card["name"] for card in panels(driver)["cards"]] == ["Grain"]

            result["final_stack"] = stored_stack(server.base_url, project_id)
            # The one refusal this script drives on purpose: a grade naming no look, on a machine
            # whose looks folder is empty. Declared by name rather than filtered away.
            console_gate(driver, NAME, result, expected=["422"])
        finally:
            driver.quit()

    report(NAME, result)


if __name__ == "__main__":
    main()
