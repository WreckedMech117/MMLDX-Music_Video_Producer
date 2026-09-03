"""Browser QA for the Brief's lock, its recovery slot and its restore button (Epic 12, Slice A).

The deciding logic is proven offline and none of it is re-proved here: `documentRestoreAvailable`,
`documentRestoreTitle`, `documentLockNotice`, `documentSlotDisplacement` and the save payload are
all executed against a stub DOM under node by `tests/test_frontend_contract.py`, and the capture,
the byte-equal skip, the lock and the `replace_project` adoption are driven through the real routes
by `tests/test_api.py`.

What none of that can reach is whether a Director can *see and use* any of it. Eight of the last
epics' defects passed every automated gate and were caught only by looking, and the shape of this
change is exactly the shape that produces those: a new pair of controls placed in a group scoped to
a tab, a paragraph inserted into a CSS grid whose row template was written for two children, and a
default tab whose controls are painted by markup rather than by the handler that paints the other
two. So the assertions below are: the help paragraph is on screen and unclipped in the Brief's own
panel, the Brief's lock and restore are visible on load *without* a tab click, the other documents'
controls are not, the restore button honours its disabled state, the round trip works through real
clicks, and a byte-equal re-save leaves the kept version alone where a Director would look for it.

Run from the repo root -- it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_brief_recovery.py [--port 8787]

Assumes: nothing listening on the port, Microsoft Edge and its WebDriver installed, and
`music_video_producer` importable from this checkout's `src/`. ComfyUI does not need to be running
and no language-model host is needed: nothing here reaches the Director, by design -- the whole
point of the restore route is that recovery does not depend on the model that caused the problem.
"""

from __future__ import annotations

import sys

from e2e_support import (
    ManagedServer,
    artifact_dir,
    clear_toasts,
    clipped,
    console_gate,
    covering_element,
    edge_driver,
    get_json,
    post_json,
    report,
    visible_and_clickable,
    wait_for_toast,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

NAME = "brief-recovery"

PASTED = (
    "A night drive that opens into wilderness. One character, no dialogue, and the car is the "
    "second character. Constraint: everything is practical light."
)
REVISED = (
    "A daylight desert crossing. Two characters who never speak to each other. Constraint: "
    "the wardrobe never changes, and neither does the car."
)


def stored(base_url: str, project_id: str) -> dict:
    return get_json(f"{base_url}/api/projects/{project_id}")


def select_project(wait, project_id: str) -> None:
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


def save_document(driver, wait) -> None:
    clear_toasts(driver)
    driver.find_element(By.ID, "save-treatment").click()
    wait_for_toast(driver, wait, "Project saved")


def main() -> None:
    port = 8787
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    result: dict[str, object] = {}
    with ManagedServer(port, label=NAME) as server:
        result["server_identity"] = server.evidence
        project = post_json(f"{server.base_url}/api/projects", {"name": "Brief recovery browser QA"})
        project_id = project["id"]

        driver = edge_driver()
        wait = WebDriverWait(driver, 25)
        try:
            driver.get(server.base_url)
            select_project(wait, project_id)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="treatment"]').click()

            # --- The Brief's tab is the one open on load, and its controls are painted with it --
            # The tab handler is the only thing that scopes a controls group, and it runs on a
            # *click*. The Brief is the default tab, so if its group did not ship carrying `active`
            # the Director would arrive at a Brief with no lock and no restore and would have to
            # visit another document and come back to make them appear.
            brief_box = wait.until(EC.visibility_of_element_located((By.ID, "creative-brief")))
            lock = driver.find_element(By.ID, "lock-brief")
            restore = driver.find_element(By.ID, "restore-brief")
            for element, what in (
                (brief_box, "the Brief editor"),
                (restore, "the Brief's restore button"),
            ):
                visible_and_clickable(driver, element, what)
            assert lock.is_displayed() or driver.find_element(
                By.CSS_SELECTOR, '[data-doc-controls="brief"] .lock-toggle'
            ).is_displayed(), "the Brief's lock checkbox is not on screen on the tab that opens"
            # And the other two documents' controls are not, or a lock left visible over the Brief
            # protects a document the Director is not looking at.
            for other in ("treatment", "style"):
                group = driver.find_element(By.CSS_SELECTOR, f'[data-doc-controls="{other}"]')
                assert not group.is_displayed(), (
                    f"the {other} controls are on screen while the Brief tab is open"
                )
            result["brief_controls_painted_on_load"] = True

            # --- TP-2: the Brief says what belongs in it, where the Brief is written -------------
            # The contract test proves the paragraph is the same string as `BRIEF_HELP`. It cannot
            # prove a Director can read it: the panel is a CSS grid whose `active` rule was written
            # for two rows, and a third child in a two-row template is how a paragraph ends up
            # squeezed to nothing or pushed under the fold with every automated gate green.
            help_paragraph = driver.find_element(
                By.CSS_SELECTOR, '[data-doc-panel="brief"] .field-help'
            )
            assert help_paragraph.is_displayed(), "the Brief's help paragraph is not displayed"
            assert not clipped(driver, help_paragraph), (
                "the Brief's help paragraph is clipped by its container"
            )
            box = help_paragraph.size
            assert box["height"] > 20 and box["width"] > 200, box
            assert brief_box.size["height"] > 100, (
                f"the help paragraph squeezed the Brief editor to {brief_box.size['height']}px"
            )
            text = " ".join(help_paragraph.text.split())
            for phrase in ("first stage of planning", "Treatment", "Style bible", "generated from it"):
                assert phrase in text, (phrase, text)
            result["brief_help"] = {"text": text[:400], "box": box, "editor": brief_box.size}
            # The other two panels carry no such paragraph, and this is the browser's answer rather
            # than the markup's: a stylesheet that showed every panel's children at once would put
            # the Brief's sentence over the Treatment.
            for other in ("treatment", "style"):
                assert not driver.find_elements(
                    By.CSS_SELECTOR, f'[data-doc-panel="{other}"] .field-help'
                ), other

            driver.save_screenshot(str(artifact_dir() / f"{NAME}-brief-tab.png"))

            # --- Nothing kept yet, and the button says so rather than offering a 409 --------------
            assert not restore.is_enabled(), (
                "a restore is offered on a Brief that has never been saved over"
            )
            assert "No previous version" in (restore.get_attribute("title") or ""), (
                restore.get_attribute("title")
            )
            # And the sentence names the writer that would keep one. For the Brief that is a save;
            # the Treatment's button says "a Director reply", and getting this backwards tells a
            # Director to wait for a reply that will never touch the Brief.
            assert "a save changes its text" in (restore.get_attribute("title") or ""), (
                restore.get_attribute("title")
            )

            # --- The first save fills a blank, so there is still nothing to restore ---------------
            brief_box.send_keys(PASTED)
            save_document(driver, wait)
            assert stored(server.base_url, project_id)["creative_brief"] == PASTED
            assert stored(server.base_url, project_id)["creative_brief_previous"] == ""
            wait.until(lambda browser: not browser.find_element(By.ID, "restore-brief").is_enabled())
            result["first_draft_keeps_nothing"] = True

            # --- A save that changes it keeps the version it displaced ---------------------------
            brief_box = driver.find_element(By.ID, "creative-brief")
            brief_box.clear()
            brief_box.send_keys(REVISED)
            save_document(driver, wait)
            after = stored(server.base_url, project_id)
            assert after["creative_brief"] == REVISED, after["creative_brief"]
            assert after["creative_brief_previous"] == PASTED, after["creative_brief_previous"]
            wait.until(lambda browser: browser.find_element(By.ID, "restore-brief").is_enabled())
            restore = driver.find_element(By.ID, "restore-brief")
            assert "save that changed it" in (restore.get_attribute("title") or ""), (
                restore.get_attribute("title")
            )
            visible_and_clickable(driver, restore, "the enabled Brief restore button")
            assert covering_element(driver, restore) is None, (
                "a toast is intercepting clicks meant for the Brief's restore button"
            )
            result["save_keeps_the_displaced_version"] = True

            # --- A byte-equal re-save spends nothing, which is the whole feature ------------------
            # The likeliest accidental path there is: open the Brief, click Save, touch nothing.
            # If that captured, the recoverable version would be replaced by a copy of what is
            # already on screen and the button beside it would become a no-op that looks armed.
            save_document(driver, wait)
            unchanged = stored(server.base_url, project_id)
            assert unchanged["creative_brief"] == REVISED
            assert unchanged["creative_brief_previous"] == PASTED, (
                "a save that changed nothing spent the Brief's single recovery slot"
            )
            assert driver.find_element(By.ID, "restore-brief").is_enabled()
            result["byte_equal_resave_keeps_the_slot"] = True

            # --- The restore is a real click, and it swaps ----------------------------------------
            clear_toasts(driver)
            driver.find_element(By.ID, "restore-brief").click()
            wait_for_toast(driver, wait, "Creative brief was restored")
            wait.until(
                lambda browser: browser.find_element(By.ID, "creative-brief").get_attribute("value")
                == PASTED
            )
            swapped = stored(server.base_url, project_id)
            assert swapped["creative_brief"] == PASTED
            assert swapped["creative_brief_previous"] == REVISED, "the restore was not a swap"
            # It is recorded in the thread, which is the audit trail of what happened to the
            # documents -- and it is on screen, not only in the manifest.
            thread = driver.find_element(By.ID, "chat-thread").text
            assert "Creative brief was restored" in thread, thread[-400:]
            assert "save that changed it" in thread, thread[-400:]
            result["restore_round_trips"] = True

            # --- The lock says what it protects against, and does not stop the human --------------
            clear_toasts(driver)
            driver.find_element(By.ID, "lock-brief").click()
            notice = wait_for_toast(driver, wait, "Creative brief is locked")
            assert "planning pass" in notice, notice
            assert "Director reply" not in notice, notice
            assert stored(server.base_url, project_id)["creative_brief_locked"] is True
            result["lock_notice"] = " ".join(notice.split())[:220]

            locked_box = driver.find_element(By.ID, "creative-brief")
            locked_box.clear()
            locked_box.send_keys("Edited by the Director who locked it.")
            save_document(driver, wait)
            locked = stored(server.base_url, project_id)
            assert locked["creative_brief"] == "Edited by the Director who locked it."
            assert locked["creative_brief_locked"] is True, "the edit unlocked the document"
            assert locked["creative_brief_previous"] == PASTED, (
                "a locked Brief stopped keeping the version its own save displaced"
            )
            assert driver.find_element(By.ID, "lock-brief").is_selected(), (
                "the lock checkbox lost its state across the save that carried it"
            )
            result["locked_brief_still_takes_the_directors_own_edit"] = True

            # --- Switching tabs moves the controls with the document ------------------------------
            driver.find_element(By.CSS_SELECTOR, '.document-tabs [data-doc="treatment"]').click()
            wait.until(
                lambda browser: browser.find_element(
                    By.CSS_SELECTOR, '[data-doc-controls="treatment"]'
                ).is_displayed()
            )
            assert not driver.find_element(
                By.CSS_SELECTOR, '[data-doc-controls="brief"]'
            ).is_displayed(), "the Brief's lock is still on screen over the Treatment"
            treatment_restore = driver.find_element(By.ID, "restore-treatment")
            assert "a Director reply replaces it" in (treatment_restore.get_attribute("title") or ""), (
                treatment_restore.get_attribute("title")
            )
            driver.save_screenshot(str(artifact_dir() / f"{NAME}-treatment-tab.png"))
            driver.find_element(By.CSS_SELECTOR, '.document-tabs [data-doc="brief"]').click()
            wait.until(
                lambda browser: browser.find_element(
                    By.CSS_SELECTOR, '[data-doc-controls="brief"]'
                ).is_displayed()
            )
            result["controls_follow_the_open_tab"] = True

            driver.save_screenshot(str(artifact_dir() / f"{NAME}-locked-brief.png"))
            console_gate(driver, NAME, result)
        finally:
            driver.quit()

    report(NAME, result)


if __name__ == "__main__":
    main()
