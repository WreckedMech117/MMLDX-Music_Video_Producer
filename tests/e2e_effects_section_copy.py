"""Browser QA for the Section target on the copy control (Story 9.5's other half).

The offline harness executes every decision this control makes -- which shots the press ticks,
which sentence each absence gets, what the press produces from whatever is ticked now -- against a
stub DOM under node. What a stub DOM structurally cannot see is what this script is for: a button
drawn behind the target list, a sentence painted over the ticks, a checkbox whose `checked`
property and attribute disagree after a rebuild, and the five states looking like an error rather
than an answer. Three defects in Epic 9 were caught only by looking, every one of them with green
automated gates.

**No GPU is spent and nothing reaches `/prompt`.** Every write here is `PUT .../shots/{id}/effects`
or `POST .../effects/copy`, both of which validate a stack and save a manifest. ComfyUI is pointed
at a dead port and never contacted.

**The five states, each of which is reachable and none of which may sit inert or read as an
error**, driven in the order a Director reaches them:

1. **No sections marked at all** -- the common case, because nothing infers them. Driven first,
   before any section is written, so it is the project's real state rather than a simulated one.
2. **The source Shot is in a section with others.** The press resolves the section into ticks the
   Director can see and change; the write is still the explicit id list every copy has sent.
3. **The section holds locked Shots.** They are ticked with the rest, the route refuses them by
   name and applies the others, and both halves are read back off the stored manifests.
4. **The source Shot is in no section**, because sections need not tile the song.
5. **The section holds only the source Shot** -- legitimate, and stated by name.
6. **Sections exist but the song was replaced.** Windows are absolute seconds and nothing moves
   them (`SONG_CHANGE_CONSEQUENCE`), so the marks now describe a track that is gone: the run
   replaces a 60 s song with a 24 s one under the Director's own confirmation and asserts the
   control goes on answering about the windows that are there, unchanged and without complaint.

The membership itself is never computed here. `Project.shot_sections` is the server's answer and
the browser looks shots up in it; this script asserts the ticked set against **the map the server
served**, so a client that started deciding for itself fails rather than agreeing by luck.

Screenshots of all five states, of the ticked set before the copy is confirmed, and of the report
afterwards are written to `test-artifacts/`.

Run from the repo root -- it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_effects_section_copy.py [--port 8783]

Assumes: nothing listening on the port, Microsoft Edge and its WebDriver installed, and
`music_video_producer` importable from this checkout's `src/`. ComfyUI does not need to be running.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import wave
from pathlib import Path

from e2e_support import (
    ManagedServer,
    artifact_dir,
    console_gate,
    edge_driver,
    get_json,
    post_json,
    post_multipart,
    put_json,
    report,
    settle,
    visible_and_clickable,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

NAME = "effects-section-copy"

SONG_SECONDS = 60.0
#: The replacement track, deliberately far shorter than the marks: after it lands, the Chorus box
#: describes seconds the song no longer has. That is state 5 and it is reachable by one gesture.
SHORT_SONG_SECONDS = 24.0

#: The source, and the four shots around it. The windows are chosen so that every state below is
#: reached by selecting a different clip in one project rather than by rebuilding the fixture:
#: three shots inside Verse 1 (one of them locked), one alone inside Chorus, one in the gap after
#: the last mark.
SHOTS = [
    {"id": "shot_01", "start": 0.0, "duration": 6.0, "prompt": "The rooftop, wide, at dusk.",
     "mode": "text_to_video", "status": "draft", "seed": 11},
    {"id": "shot_02", "start": 6.0, "duration": 6.0, "prompt": "The same rooftop, closer.",
     "mode": "text_to_video", "status": "draft", "seed": 12},
    {"id": "shot_03", "start": 12.0, "duration": 6.0, "prompt": "The stairwell, handheld.",
     "mode": "text_to_video", "status": "draft", "seed": 13, "locked": True},
    {"id": "shot_04", "start": 20.0, "duration": 6.0, "prompt": "The bed, glamour angle.",
     "mode": "text_to_video", "status": "draft", "seed": 14},
    {"id": "shot_05", "start": 40.0, "duration": 6.0, "prompt": "The car park, at night.",
     "mode": "text_to_video", "status": "draft", "seed": 15},
]

#: Two marks and a stretch of song in neither of them. `Verse 1` ends where `Chorus` begins, so
#: nothing here depends on the tie clause -- the tie is pinned by the unit tests, and a browser
#: script asserting it would be asserting arithmetic through a screenshot.
SECTIONS = [
    {"id": "section_verse", "label": "Verse 1", "start": 0.0, "duration": 18.0,
     "prompt": "at the standing mic"},
    {"id": "section_chorus", "label": "Chorus", "start": 18.0, "duration": 12.0,
     "prompt": "on the canopy bed"},
]

SOURCE = "shot_01"
#: The one unlocked shot the section press should reach, and the locked one beside it.
IN_SECTION = "shot_02"
LOCKED = "shot_03"
#: Alone in its own section, and outside every section.
ALONE = "shot_04"
OUTSIDE = "shot_05"

STACK = [
    {"effect": "grain", "parameters": {"strength": 18}},
    {"effect": "punch_in", "parameters": {}},
]


def dead_port() -> int:
    """A port nothing is listening on, so the ComfyUI reads fail fast and change nothing."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def synthesize_song(target: Path, seconds: float) -> None:
    with wave.open(str(target), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(b"\x00\x00" * int(seconds * 8000))


def seed_project(base_url: str) -> str:
    """A song, five shots, a graded source -- and **no sections**, which is state 1."""
    project = post_json(f"{base_url}/api/projects", {"name": "Section copy browser QA"})
    project_id = project["id"]
    song = artifact_dir() / f"{NAME}-song.wav"
    synthesize_song(song, SONG_SECONDS)
    post_multipart(
        f"{base_url}/api/projects/{project_id}/songs/upload",
        {"title": "Section copy QA song", "duration": str(SONG_SECONDS)},
        ("file", song),
    )
    put_json(f"{base_url}/api/projects/{project_id}/shots", {"shots": SHOTS})
    put_json(f"{base_url}/api/projects/{project_id}/shots/{SOURCE}/effects", {"effects": STACK})
    return project_id


def stored_stack(base_url: str, project_id: str, shot_id: str) -> list[dict]:
    return get_json(f"{base_url}/api/projects/{project_id}/shots/{shot_id}/effects")["effects"]


def served_map(base_url: str, project_id: str) -> dict[str, str]:
    """`Project.shot_sections` as the browser is given it. The only membership answer here."""
    return get_json(f"{base_url}/api/projects/{project_id}")["shot_sections"]


def manifest(server: ManagedServer, project_id: str) -> dict:
    path = server.data_root / "projects" / project_id / "project.json"
    return json.loads(path.read_text(encoding="utf-8"))


def open_panel(driver, panel: str) -> None:
    driver.find_element(By.CSS_SELECTOR, f'[data-panel="{panel}"]').click()


def select_clip(driver, wait, shot_id: str) -> None:
    settle(driver, "#shots-track")
    clip = wait.until(
        lambda browser: browser.find_element(
            By.CSS_SELECTOR, f'#shots-track .shot-clip[data-shot-id="{shot_id}"]'
        )
    )
    driver.execute_script("arguments[0].scrollIntoView({ block: 'center' });", clip)
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


def open_copy(driver) -> None:
    """Open the copy disclosure if it is not open already."""
    if driver.execute_script(
        "const p = document.getElementById('effect-copy-panel');"
        " return Boolean(p) && !p.hasAttribute('hidden');"
    ):
        return
    button = driver.find_element(By.ID, "effect-copy")
    visible_and_clickable(driver, button, "the copy control")
    button.click()
    settle(driver, "#shot-inspector", quiet_ms=400)


#: Everything about the Section control a Director could see, measured rather than inferred: which
#: of the two elements is drawn, where it sits relative to the list it ticks, whether it is
#: reachable, and -- the half a stub DOM cannot answer -- which boxes are really ticked, read off
#: the live `checked` **property** rather than the attribute the markup shipped.
STATE = """
const box = (node) => {
  if (!node) return null;
  const rect = node.getBoundingClientRect();
  const style = getComputedStyle(node);
  return {
    text: node.textContent, top: rect.top, left: rect.left,
    width: rect.width, height: rect.height,
    display: style.display, visibility: style.visibility, opacity: style.opacity,
    hiddenAttr: node.hasAttribute('hidden'), disabled: Boolean(node.disabled),
    title: node.getAttribute('title') || '',
  };
};
const panel = document.getElementById('effect-copy-panel');
const targets = [...document.querySelectorAll('.effect-copy-target')].map((label) => {
  const input = label.querySelector('input');
  const rect = input.getBoundingClientRect();
  return {
    id: input.id.replace('effect-copy-target-', ''),
    name: label.querySelector('.effect-copy-name').textContent,
    mark: (label.querySelector('.effect-copy-mark') || {}).textContent || '',
    checked: input.checked,
    top: rect.top,
  };
});
return {
  panelHidden: panel ? panel.hasAttribute('hidden') : null,
  panelDisplay: panel ? getComputedStyle(panel).display : null,
  button: box(document.getElementById('effect-copy-section')),
  note: box(document.getElementById('effect-copy-section-note')),
  announcement: box(document.getElementById('effect-copy-note')),
  apply: box(document.getElementById('effect-copy-apply')),
  reason: box(document.getElementById('effect-copy-reason')),
  targets,
  ticked: targets.filter((target) => target.checked).map((target) => target.id),
  report: [...document.querySelectorAll('#effects-copy-report strong, #effects-copy-report p')]
    .map((node) => node.textContent),
};
"""


def state(driver) -> dict:
    return driver.execute_script(STATE)


def targets_reachable(driver, seen: dict) -> list[str]:
    """Every target checkbox, scrolled to and hit-tested at its centre.

    Separate from `STATE` and not folded into it, because `visible_and_clickable` scrolls to
    reach an element and `STATE`'s own measurements are about where things sit relative to each
    other. Measuring both in one pass would have the first target's scroll move the rest.
    """
    for target in seen["targets"]:
        element = driver.find_element(By.ID, "effect-copy-target-" + target["id"])
        visible_and_clickable(driver, element, f"the {target['id']} target")
    return [target["id"] for target in seen["targets"]]


def shoot(driver, element_id: str, label: str) -> None:
    """One element, scrolled into the middle of the inspector, as its own artifact."""
    element = driver.find_element(By.ID, element_id)
    driver.execute_script("arguments[0].scrollIntoView({ block: 'center' });", element)
    settle(driver, "#shot-inspector", quiet_ms=250)
    element.screenshot(str(artifact_dir() / f"{NAME}-{label}.png"))


def main() -> None:
    port = 8783
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    result: dict[str, object] = {}
    comfy_root = Path(tempfile.mkdtemp(prefix="mvp-section-copy-comfy-"))
    unreachable = f"http://127.0.0.1:{dead_port()}"
    os.environ["MVP_COMFY_ROOT"] = str(comfy_root)
    os.environ["MVP_COMFY_URL"] = unreachable
    os.environ["MVP_LLM_EJECT_BEFORE_RENDER"] = "false"
    os.environ["MVP_LLM_BASE_URL"] = ""

    with ManagedServer(port, label=NAME) as server:
        result["server_identity"] = server.evidence
        result["comfy_url"] = unreachable
        project_id = seed_project(server.base_url)
        assert served_map(server.base_url, project_id) == {}, "an unmarked song placed a shot"

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
            select_clip(driver, wait, SOURCE)
            open_effects(driver)

            # === 1. No sections marked at all ================================================
            open_copy(driver)
            unmarked = state(driver)
            assert unmarked["panelHidden"] is False and unmarked["panelDisplay"] != "none", unmarked
            assert unmarked["button"] is None, "a section button was drawn with no sections marked"
            assert unmarked["note"] is not None, "nothing was said about the missing sections"
            assert "No sections are marked on this song" in unmarked["note"]["text"], \
                unmarked["note"]
            # Said, and legible: not a zero-height element, and not painted over the list.
            assert unmarked["note"]["height"] > 0 and unmarked["note"]["opacity"] == "1", \
                unmarked["note"]
            assert unmarked["note"]["top"] < min(target["top"] for target in unmarked["targets"]), \
                "the sentence is below the list it stands in for"
            # And the rest of the control is untouched: the per-shot ticks still work.
            assert len(unmarked["targets"]) == 4, unmarked["targets"]
            assert targets_reachable(driver, unmarked) == [
                IN_SECTION, LOCKED, ALONE, OUTSIDE], unmarked["targets"]
            shoot(driver, "effect-copy-panel", "01-no-sections-marked")
            result["no_sections"] = {"note": unmarked["note"]["text"],
                                     "targets": len(unmarked["targets"])}

            # === The Director marks two sections ============================================
            put_json(f"{server.base_url}/api/projects/{project_id}/sections",
                     {"sections": SECTIONS})
            placed = served_map(server.base_url, project_id)
            # The server's own answer, stated once and used as the yardstick for every tick below.
            assert placed == {
                "shot_01": "section_verse", "shot_02": "section_verse",
                "shot_03": "section_verse", "shot_04": "section_chorus",
            }, placed
            result["served_map"] = placed
            driver.refresh()
            wait.until(EC.presence_of_element_located((By.ID, "project-select")))
            settle(driver, "#shots-track")
            select_clip(driver, wait, SOURCE)
            open_effects(driver)
            open_copy(driver)

            # === 2. The section resolved into ticks the Director can see =====================
            offered = state(driver)
            assert offered["button"] is not None, "no section button with a section to offer"
            assert offered["button"]["text"] == 'Tick the 2 other shots in "Verse 1"', \
                offered["button"]
            assert offered["button"]["disabled"] is False, offered["button"]
            assert "midpoint" in offered["button"]["title"], offered["button"]["title"]
            # Read on the way to the choice: below the announcement that a copy replaces, above
            # the list it ticks, and above the button that writes.
            assert offered["announcement"]["top"] < offered["button"]["top"], offered
            assert offered["button"]["top"] < min(t["top"] for t in offered["targets"]), offered
            assert max(t["top"] for t in offered["targets"]) < offered["apply"]["top"], offered
            assert offered["ticked"] == [], "something was ticked before the Director asked"
            visible_and_clickable(driver, driver.find_element(By.ID, "effect-copy-section"),
                                  "the section target")
            shoot(driver, "effect-copy-panel", "02-section-offered")

            driver.find_element(By.ID, "effect-copy-section").click()
            settle(driver, "#shot-inspector", quiet_ms=500)
            ticked = state(driver)
            # Exactly the shots the *server* places in this shot's section, and nothing else.
            expected = sorted(
                shot for shot, section in placed.items()
                if section == placed[SOURCE] and shot != SOURCE
            )
            assert sorted(ticked["ticked"]) == expected == [IN_SECTION, LOCKED], ticked["ticked"]
            assert ticked["apply"]["text"] == "Copy to 2 shots", ticked["apply"]
            assert ticked["apply"]["disabled"] is False, ticked["apply"]
            # The button now offers the way back, so it is never a press that does nothing.
            assert ticked["button"]["text"] == 'Untick the 2 other shots in "Verse 1"', \
                ticked["button"]
            # === 3. The locked shot is in the set, marked, and left to the route =============
            marks = {target["id"]: target["mark"] for target in ticked["targets"]}
            assert marks[LOCKED] == "LOCKED", marks
            assert marks[IN_SECTION] == "", marks
            # Nothing has been written yet: this is a proposal the Director is looking at.
            assert stored_stack(server.base_url, project_id, IN_SECTION) == [], \
                "the section press wrote a stack"
            shoot(driver, "effect-copy-panel", "03-ticked-before-confirming")
            result["ticked"] = {"ids": ticked["ticked"], "apply": ticked["apply"]["text"],
                                "button": ticked["button"]["text"]}

            # === The copy, confirmed ========================================================
            driver.find_element(By.ID, "effect-copy-apply").click()
            settle(driver, "#shot-inspector", quiet_ms=900)
            reported = state(driver)
            assert reported["report"], "the panel says nothing about the copy"
            assert reported["report"][0] == "PARTLY COPIED", reported["report"]
            assert "Copied 2 effects onto 1 shot" in reported["report"][1], reported["report"]
            # The route's own refusal, whole, naming the Shot as the timeline names one.
            assert reported["report"][2] == (
                f"SHOT 03 ({LOCKED}) is locked, so its effects were not changed. "
                "Unlock the shot to change its look."), reported["report"]
            shoot(driver, "effects-copy-report", "04-report-after-confirming")
            # Read back off the store, both halves: the unlocked target carries the source's
            # stack and the locked one carries nothing at all.
            assert stored_stack(server.base_url, project_id, IN_SECTION) == \
                stored_stack(server.base_url, project_id, SOURCE), "the copy did not land"
            assert stored_stack(server.base_url, project_id, LOCKED) == [], \
                "a locked shot in the section was written"
            assert stored_stack(server.base_url, project_id, ALONE) == [], \
                "a shot outside the section was written"
            assert stored_stack(server.base_url, project_id, OUTSIDE) == [], \
                "a shot in no section was written"
            # And the ticks are cleared, so the panel does not invite the same write again.
            assert state(driver)["ticked"] == [], state(driver)["ticked"]
            result["copy_report"] = reported["report"]

            # === 4. The source Shot is in no section ========================================
            select_clip(driver, wait, OUTSIDE)
            open_effects(driver)
            open_copy(driver)
            outside = state(driver)
            assert OUTSIDE not in placed, placed
            assert outside["button"] is None, "a section was offered for a shot in none"
            assert "outside every marked section" in outside["note"]["text"], outside["note"]
            assert outside["note"]["height"] > 0, outside["note"]
            assert len(outside["targets"]) == 4, outside["targets"]
            assert targets_reachable(driver, outside) == [
                SOURCE, IN_SECTION, LOCKED, ALONE], outside["targets"]
            shoot(driver, "effect-copy-panel", "05-shot-in-no-section")
            result["outside"] = outside["note"]["text"]

            # === 5. The section holds only the source Shot ==================================
            select_clip(driver, wait, ALONE)
            open_effects(driver)
            open_copy(driver)
            alone = state(driver)
            assert alone["button"] is None, "a one-shot section offered a press"
            assert alone["note"]["text"] == (
                '"Chorus" holds this shot and no other, so there is nothing in it to copy onto.'
            ), alone["note"]
            shoot(driver, "effect-copy-panel", "06-section-of-one")
            result["alone"] = alone["note"]["text"]

            # === 6. Sections exist, and the song was replaced ================================
            replacement = artifact_dir() / f"{NAME}-replacement.wav"
            synthesize_song(replacement, SHORT_SONG_SECONDS)
            post_multipart(
                f"{server.base_url}/api/projects/{project_id}/songs/upload",
                {"title": "A different song", "duration": str(SHORT_SONG_SECONDS),
                 "confirm_song_replacement": "true"},
                ("file", replacement),
            )
            # Windows are absolute seconds and nothing moves them, so the marks now describe a
            # track that is gone -- the Chorus box runs past the new song's end entirely.
            after = manifest(server, project_id)
            assert after["song"]["duration"] == SHORT_SONG_SECONDS, after["song"]
            assert [section["start"] for section in after["sections"]] == [0.0, 18.0], after
            assert served_map(server.base_url, project_id) == placed, "a replacement moved a shot"
            driver.refresh()
            wait.until(EC.presence_of_element_located((By.ID, "project-select")))
            settle(driver, "#shots-track")
            select_clip(driver, wait, SOURCE)
            open_effects(driver)
            open_copy(driver)
            replaced = state(driver)
            # Unchanged and uncomplaining: the control answers about the windows that are there.
            assert replaced["button"] is not None, "the control went silent after a replacement"
            assert replaced["button"]["text"] == 'Tick the 2 other shots in "Verse 1"', \
                replaced["button"]
            assert replaced["note"] is None, replaced["note"]
            driver.find_element(By.ID, "effect-copy-section").click()
            settle(driver, "#shot-inspector", quiet_ms=500)
            assert sorted(state(driver)["ticked"]) == [IN_SECTION, LOCKED], state(driver)["ticked"]
            shoot(driver, "effect-copy-panel", "07-song-replaced")
            result["after_song_replacement"] = {
                "button": replaced["button"]["text"],
                "song_seconds": after["song"]["duration"],
                "sections": [(s["label"], s["start"], s["duration"]) for s in after["sections"]],
            }

            # The derived map is served and never stored, which is what keeps it from going stale.
            assert "shot_sections" not in manifest(server, project_id), \
                "the derived section map was written into the manifest"
            result["manifest_keys_without_the_map"] = True

            console_gate(driver, NAME, result)
        finally:
            driver.quit()

    report(NAME, result)


if __name__ == "__main__":
    main()
