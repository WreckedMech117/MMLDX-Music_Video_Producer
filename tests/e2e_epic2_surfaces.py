"""Browser QA for the Epic 2 surfaces, none of which had ever been rendered.

Every guarantee those stories ship was pinned offline: notice blocks, the readiness
flag on a clip, the plan-readiness region, the document lock and restore controls.
Offline was not enough three separate times -- a substring assertion let the clip flag
render inverted, let the batch filter invert, and would have let the whole thread render
as literal HTML text. This drives the real DOM and asserts what a Director would see.

The project state is seeded by writing the manifest directly rather than through the API,
for two reasons: notices can only be produced by a live language model, and `PUT
/api/projects/{id}` now deliberately refuses client-supplied messages. Writing the file is
the only way to fixture a reply without a model, and it is exactly what the app reads.

Run the app on an isolated data root first, then::

    MVP_APP_PORT=8766 MVP_DATA_ROOT=<empty dir> uv run python run.py
    uv run --with selenium python tests/e2e_epic2_surfaces.py http://127.0.0.1:8766 <data root>
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8766"
DATA_ROOT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("data")
ARTIFACT_DIR = Path("test-artifacts")
ARTIFACT_DIR.mkdir(exist_ok=True)

REFUSED_RAW = '[{"style":"moody","palette":["amber","teal"]}]'
PROSE = "I tightened the second verse and left the pacing alone."


def create_project(name: str) -> dict:
    body = json.dumps({"name": name}).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE_URL}/api/projects", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def seed(project_id: str) -> None:
    """Write the state a Director turn would have produced, straight into the manifest."""
    path = DATA_ROOT / "projects" / project_id / "project.json"
    project = json.loads(path.read_text(encoding="utf-8"))
    project["treatment"] = "A corridor, and someone walking it slower than the beat."
    project["style_bible"] = "Amber sodium light. Handheld. Never a clean cut."
    project["style_bible_previous"] = "An older style bible, kept for recovery."
    project["style_bible_locked"] = True
    project["shots"] = [
        {"id": "shot_written", "start": 0, "duration": 5, "prompt": "The corridor, pushing in slowly.",
         "mode": "text", "status": "draft"},
        {"id": "shot_blank", "start": 5, "duration": 5, "prompt": "",
         "mode": "text", "status": "draft"},
    ]
    project["messages"] = [
        {"id": "msg_user", "role": "user", "content": "Tighten the second verse.",
         "created_at": "2026-08-17T12:00:00Z", "notices": []},
        {"id": "msg_reply", "role": "assistant",
         "content": PROSE + "\n\n---\nStyle bible was NOT replaced: the model returned JSON "
                            "instead of prose.\n\nTreatment was replaced by this reply.",
         "created_at": "2026-08-17T12:00:01Z",
         "notices": [
             {"kind": "refusal", "text": "Style bible was NOT replaced: the model returned JSON "
                                         "instead of prose.", "raw": REFUSED_RAW},
             {"kind": "change", "text": "Treatment was replaced by this reply.", "raw": ""},
         ]},
    ]
    path.write_text(json.dumps(project, indent=2), encoding="utf-8")


def main() -> None:
    project = create_project("Epic 2 Surfaces Browser QA")
    seed(project["id"])

    options = webdriver.EdgeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1440,900")
    options.set_capability("ms:loggingPrefs", {"browser": "ALL"})
    driver = webdriver.Edge(options=options)
    wait = WebDriverWait(driver, 20)
    result: dict[str, object] = {}
    try:
        driver.get(BASE_URL)
        wait.until(lambda b: project["name"] in b.find_element(By.ID, "project-select").text)

        # --- Treatment workspace: the notice blocks -------------------------------------
        driver.find_element(By.CSS_SELECTOR, '[data-panel="treatment"]').click()
        wait.until(EC.visibility_of_element_located((By.ID, "chat-thread")))
        notices = driver.find_elements(By.CSS_SELECTOR, "#chat-thread .message-notice")
        assert len(notices) == 2, f"expected two notice blocks, saw {len(notices)}"

        # They must be real elements, not text: if the thread ever rendered escaped HTML the
        # selector above would find nothing while the characters were still on screen.
        reply = driver.find_element(By.CSS_SELECTOR, "#chat-thread .message.assistant")
        assert "<div" not in reply.text, "the thread rendered markup as literal text"

        kinds = [n.get_attribute("class") for n in notices]
        assert any("notice-refusal" in k for k in kinds), kinds
        assert any("notice-change" in k for k in kinds), kinds
        labels = [n.find_element(By.CSS_SELECTOR, ".notice-label").text for n in notices]
        assert "SAFETY NOTICE" in [x.upper() for x in labels], labels
        assert any("CHANGE" in x.upper() for x in labels), labels

        refusal = next(n for n in notices if "notice-refusal" in n.get_attribute("class"))
        change = next(n for n in notices if "notice-change" in n.get_attribute("class"))
        edges = {
            "refusal": refusal.value_of_css_property("border-left-color"),
            "change": change.value_of_css_property("border-left-color"),
        }
        assert edges["refusal"] != edges["change"], f"kinds share one edge colour: {edges}"
        assert refusal.value_of_css_property("border-left-style") == "solid"
        assert notices[0].get_attribute("role") == "note"

        # The prose must survive intact beside the notices.
        assert PROSE in reply.text, reply.text

        # Raw output: present, collapsed, and readable once opened.
        disclosure = refusal.find_element(By.CSS_SELECTOR, "details.notice-raw")
        assert disclosure.get_attribute("open") is None, "disclosure starts open"
        assert REFUSED_RAW not in refusal.text, "raw output is visible before opening"
        driver.execute_script("arguments[0].open = true;", disclosure)
        assert REFUSED_RAW in disclosure.text, disclosure.text
        result["notice_kinds_distinct"] = True
        result["raw_output_inspectable"] = True

        # --- Song workspace: lock and restore ------------------------------------------
        driver.find_element(By.CSS_SELECTOR, '[data-panel="song"]').click()
        driver.find_element(By.CSS_SELECTOR, '[data-panel="treatment"]').click()
        lock = driver.find_element(By.ID, "lock-style")
        restore = driver.find_element(By.ID, "restore-style")
        assert lock.is_selected(), "style bible lock did not reflect the stored project"
        assert restore.is_enabled(), "restore was disabled despite a kept version"
        assert not driver.find_element(By.ID, "restore-treatment").is_enabled(), (
            "restore offered for a document with nothing kept"
        )
        result["lock_and_restore_reflect_state"] = True

        # --- Timeline: the unprompted clip and the readiness region ---------------------
        driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
        wait.until(EC.presence_of_element_located((By.ID, "shots-track")))
        # The readiness fetch re-renders the timeline when it lands, so every reference taken
        # before it is stale. Read the clips in one shot, after the flag has appeared.
        wait.until(lambda b: b.find_elements(By.CSS_SELECTOR, ".shot-clip.no-prompt"))
        clips = driver.find_elements(By.CSS_SELECTOR, ".shot-clip")
        flagged = [c for c in clips if "no-prompt" in (c.get_attribute("class") or "")]
        written = [c for c in clips if "no-prompt" not in (c.get_attribute("class") or "")]
        assert len(flagged) == 1 and len(written) == 1, (
            f"expected one flagged and one written clip, saw {len(flagged)}/{len(written)}"
        )
        assert "NO PROMPT" in flagged[0].text.upper(), flagged[0].text
        assert "NO PROMPT" not in written[0].text.upper(), (
            "the flag is inverted -- a written clip claims it has no prompt"
        )
        assert flagged[0].value_of_css_property("border-style") == "dashed", (
            "blocked state carried by colour alone"
        )
        result["unprompted_clip_flagged"] = True

        # The readiness region lives in the Queue panel, so it has to be visible to be read --
        # Selenium reports empty text for a hidden element, which is indistinguishable from a
        # region that was never filled.
        driver.find_element(By.CSS_SELECTOR, '[data-panel="queue"]').click()
        readiness = wait.until(EC.visibility_of_element_located((By.ID, "plan-readiness")))
        wait.until(lambda b: b.find_element(By.ID, "plan-readiness").text.strip())
        text = readiness.text
        assert "SHOT 02" in text.upper() or "shot_blank" in text, text
        assert "no prompt" in text.lower(), text
        result["readiness_region_names_the_blocker"] = " ".join(text.split())[:180]

        # --- Consent is per turn: ticked, then cleared by a project change ---------------
        driver.find_element(By.CSS_SELECTOR, '[data-panel="treatment"]').click()
        consent = wait.until(EC.visibility_of_element_located((By.ID, "apply-documents")))
        assert not consent.is_selected(), "document consent was pre-ticked"
        consent.click()
        assert consent.is_selected()
        select = driver.find_element(By.ID, "project-select")
        others = [o for o in select.find_elements(By.TAG_NAME, "option")
                  if o.get_attribute("value") and o.get_attribute("value") != project["id"]]
        if others:
            others[0].click()
            wait.until(lambda b: b.find_element(By.ID, "project-select").get_attribute("value")
                       != project["id"])
            assert not driver.find_element(By.ID, "apply-documents").is_selected(), (
                "consent survived a project change -- it is not per turn"
            )
            result["consent_cleared_on_project_change"] = True
        else:
            result["consent_cleared_on_project_change"] = "skipped: only one project"

        # --- Song workspace: the preset drives the form ----------------------------------
        driver.find_element(By.CSS_SELECTOR, '[data-panel="song"]').click()
        preset = wait.until(EC.visibility_of_element_located((By.NAME, "preset")))
        lyrics = driver.find_element(By.NAME, "lyrics")
        duration = driver.find_element(By.NAME, "duration")
        states = {}
        for value in ("balanced", "songplanner-invented", "songplanner-known"):
            driver.execute_script(
                "arguments[0].value = arguments[1];"
                "arguments[0].dispatchEvent(new Event('change'));", preset, value,
            )
            states[value] = {
                "lyrics_visible": lyrics.is_displayed(),
                "duration_min": duration.get_attribute("min"),
                "duration_max": duration.get_attribute("max"),
            }
        assert states["songplanner-invented"]["lyrics_visible"] is False, states
        assert states["songplanner-known"]["lyrics_visible"] is True, states
        assert states["songplanner-invented"]["duration_min"] == "30", states
        assert states["balanced"]["duration_min"] == "4", states
        result["preset_drives_the_form"] = states

        driver.save_screenshot(str(ARTIFACT_DIR / "epic2-surfaces.png"))
        severe = [e for e in driver.get_log("browser") if e.get("level") == "SEVERE"]
        result["severe_console_errors"] = severe
        (ARTIFACT_DIR / "epic2-surfaces-console.json").write_text(
            json.dumps(driver.get_log("browser"), indent=2), encoding="utf-8"
        )
        assert not severe, severe
    finally:
        driver.quit()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
