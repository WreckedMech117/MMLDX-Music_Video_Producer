"""Browser QA for the clearing question on a document save (Epic 12, Slice A2 / R-18).

The deciding logic is proven offline and none of it is re-proved here: `documentClearing`,
`documentCapturedOnSave`, `documentClearingQuestion` and both consequence sentences are executed
against a stub DOM under node by `tests/test_frontend_contract.py`, which also drives the Save
button and the lock checkbox through `saveProject` itself.

What none of that can reach is whether the browser puts a dialog in front of the Director at all.
`window.confirm` is stubbed in every offline harness by construction, so "the call is made" is the
only thing those can say; whether Edge paints it, whether dismissing it really abandons the save,
and whether the sentence the Director reads is the one the constant holds are three separate
questions and all three are browser questions. Slice A's own restore-button defect was invisible to
every offline gate and was caught by looking, and it was the fourteenth of its kind across Epics
9-11.

So the assertions below are: an emptied Treatment raises a real dialog whose sentence does *not*
offer a Restore; an emptied Brief raises one that does, and the offer is then proved by clicking
that button; declining leaves the stored text and the on-screen text alone; a save that changes
text to other text and a save over an already-empty document raise nothing; and two documents
cleared in one save raise one dialog that names both, on the right side of the partition each.

Run from the repo root -- it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_document_clearing.py [--port 8788]

Assumes: nothing listening on the port, Microsoft Edge and its WebDriver installed, and
`music_video_producer` importable from this checkout's `src/`. ComfyUI does not need to be running
and no language-model host is needed: nothing here reaches the Director, by design.
"""

from __future__ import annotations

import sys

from e2e_support import (
    ManagedServer,
    artifact_dir,
    clear_toasts,
    console_gate,
    edge_driver,
    get_json,
    post_json,
    put_json,
    report,
    visible_and_clickable,
    wait_for_toast,
)
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

NAME = "document-clearing"

BRIEF = (
    "A night drive that opens into wilderness. One character, no dialogue. Constraint: "
    "everything is practical light."
)
TREATMENT = (
    "Three movements. The car leaves the city under sodium light, crosses the salt flats at "
    "dawn, and stops at the ridge without arriving anywhere."
)
STYLE = "Anamorphic, 40mm, practical sources only. Wardrobe never changes. No handheld."

BOXES = {"creative_brief": "creative-brief", "treatment": "treatment-text", "style_bible": "style-bible"}
TABS = {"creative_brief": "brief", "treatment": "treatment", "style_bible": "style"}


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


def open_tab(driver, wait, document: str) -> None:
    driver.find_element(By.CSS_SELECTOR, f'.document-tabs [data-doc="{TABS[document]}"]').click()
    wait.until(
        lambda browser: browser.find_element(
            By.CSS_SELECTOR, f'[data-doc-controls="{TABS[document]}"]'
        ).is_displayed()
    )


def empty(driver, wait, document: str) -> None:
    """Empty a document's editor the way a Director does: open its tab and clear the box."""
    open_tab(driver, wait, document)
    box = driver.find_element(By.ID, BOXES[document])
    visible_and_clickable(driver, box, f"the {document} editor")
    box.clear()
    wait.until(lambda browser: browser.find_element(By.ID, BOXES[document]).get_attribute("value") == "")


def click_save(driver) -> None:
    clear_toasts(driver)
    driver.find_element(By.ID, "save-treatment").click()


def dialog(driver, wait, expected: bool) -> str:
    """The text of the dialog the Save click raised, or "" when it raised none.

    `expected` is what the script claims, so both directions are a named failure rather than a
    `TimeoutException` in one and a silent pass in the other.
    """
    # A short wait for the *absence* case, and it is not a nicety: the save that follows raises a
    # toast which removes itself after 4.2 s, so watching a full 25 s for a dialog that is never
    # coming means the confirmation of the save has already gone by the time anything looks.
    watch = wait if expected else WebDriverWait(driver, 2)
    try:
        text = watch.until(EC.alert_is_present()).text
    except TimeoutException as error:  # pragma: no cover - a real failure, not a skip
        if expected:
            raise AssertionError(
                "clearing a document and saving raised no dialog; the confirmation is either "
                "not reaching the browser or is being auto-dismissed"
            ) from error
        return ""
    if not expected:
        raise AssertionError(f"a save that loses nothing asked the Director anyway: {text!r}")
    return text


def main() -> None:
    port = 8788
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    result: dict[str, object] = {}
    with ManagedServer(port, label=NAME) as server:
        result["server_identity"] = server.evidence
        project = post_json(
            f"{server.base_url}/api/projects", {"name": "Document clearing browser QA"}
        )
        project_id = project["id"]
        # Seeded through the real route, so every document starts with stored text and an empty
        # recovery slot -- which is the state R-18 measured and the state the question is about.
        put_json(
            f"{server.base_url}/api/projects/{project_id}/documents",
            {"creative_brief": BRIEF, "treatment": TREATMENT, "style_bible": STYLE},
        )
        seeded = stored(server.base_url, project_id)
        assert seeded["creative_brief_previous"] == ""
        assert seeded["treatment_previous"] == ""
        assert seeded["style_bible_previous"] == ""

        driver = edge_driver()
        wait = WebDriverWait(driver, 25)
        try:
            driver.get(server.base_url)
            select_project(wait, project_id)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="treatment"]').click()
            wait.until(EC.visibility_of_element_located((By.ID, "creative-brief")))

            # --- An emptied Treatment asks, and the sentence does not offer a Restore -----------
            # The Treatment's slot holds whatever a Director reply last displaced. This save does
            # not touch it, so the text being deleted has no way back -- and a question that named
            # the Restore button here would send the Director to a control that answers 409.
            empty(driver, wait, "treatment")
            click_save(driver)
            question = dialog(driver, wait, expected=True)
            assert "Treatment" in question, question
            assert "nothing is kept by this save" in question, question
            assert "no way back" in question, question
            assert "Restore" not in question, question
            assert "Creative brief" not in question, question
            assert "Style bible" not in question, question
            driver.switch_to.alert.dismiss()
            result["treatment_question"] = " ".join(question.split())

            # Declining abandons the save: the server still holds the text, and the box on screen
            # still holds the Director's own gesture rather than being reverted under them.
            after_decline = stored(server.base_url, project_id)
            assert after_decline["treatment"] == TREATMENT, (
                "declining the clearing question deleted the Treatment anyway"
            )
            assert after_decline["treatment_previous"] == "", (
                "a declined save touched the recovery slot R-18 left to the model"
            )
            assert driver.find_element(By.ID, "treatment-text").get_attribute("value") == "", (
                "declining re-seeded the editor and discarded what the Director had typed"
            )
            driver.save_screenshot(str(artifact_dir() / f"{NAME}-treatment-declined.png"))
            result["decline_keeps_everything"] = True

            # --- Accepting deletes it, and the slot is still the model's ------------------------
            click_save(driver)
            dialog(driver, wait, expected=True)
            driver.switch_to.alert.accept()
            wait_for_toast(driver, wait, "Project saved")
            emptied = stored(server.base_url, project_id)
            assert emptied["treatment"] == ""
            assert emptied["treatment_previous"] == "", (
                "the save captured into the Treatment's slot, which is what R-18 declined"
            )
            open_tab(driver, wait, "treatment")
            assert not driver.find_element(By.ID, "restore-treatment").is_enabled(), (
                "the emptied Treatment offers a restore the server would refuse with 409"
            )
            result["treatment_save_keeps_nothing"] = True

            # --- A document that is already empty loses nothing, and must not ask ---------------
            click_save(driver)
            assert dialog(driver, wait, expected=False) == ""
            wait_for_toast(driver, wait, "Project saved")
            result["already_empty_does_not_ask"] = True

            # --- Typing is not deleting -----------------------------------------------------
            open_tab(driver, wait, "treatment")
            driver.find_element(By.ID, "treatment-text").send_keys("A different treatment.")
            click_save(driver)
            assert dialog(driver, wait, expected=False) == ""
            wait_for_toast(driver, wait, "Project saved")
            assert stored(server.base_url, project_id)["treatment"] == "A different treatment."
            result["retyping_does_not_ask"] = True

            # --- An emptied Brief asks, and its sentence is the other one ----------------------
            empty(driver, wait, "creative_brief")
            click_save(driver)
            brief_question = dialog(driver, wait, expected=True)
            assert "Creative brief" in brief_question, brief_question
            assert "Restore beside the box swaps it back" in brief_question, brief_question
            assert "the next save spends it" in brief_question, brief_question
            assert "no way back" not in brief_question, brief_question
            driver.switch_to.alert.accept()
            wait_for_toast(driver, wait, "Project saved")
            result["brief_question"] = " ".join(brief_question.split())

            # And the promise the sentence made is kept: the button it named is armed, and the
            # click brings the text back. A consequence that overstates the damage is as corrosive
            # as one that understates it, so this is the half that has to be shown, not asserted.
            after_brief = stored(server.base_url, project_id)
            assert after_brief["creative_brief"] == ""
            assert after_brief["creative_brief_previous"] == BRIEF
            open_tab(driver, wait, "creative_brief")
            restore = wait.until(lambda browser: browser.find_element(By.ID, "restore-brief"))
            wait.until(lambda browser: browser.find_element(By.ID, "restore-brief").is_enabled())
            visible_and_clickable(driver, restore, "the Brief's restore button after a clearing save")
            driver.save_screenshot(str(artifact_dir() / f"{NAME}-brief-restore-armed.png"))
            clear_toasts(driver)
            restore.click()
            wait_for_toast(driver, wait, "Creative brief was restored")
            wait.until(
                lambda browser: browser.find_element(By.ID, "creative-brief").get_attribute("value")
                == BRIEF
            )
            assert stored(server.base_url, project_id)["creative_brief"] == BRIEF
            result["brief_question_told_the_truth"] = True

            # --- Two documents cleared in one save: one dialog, and each on its own side --------
            empty(driver, wait, "creative_brief")
            empty(driver, wait, "style_bible")
            click_save(driver)
            together = dialog(driver, wait, expected=True)
            assert "Creative brief" in together and "Style bible" in together, together
            # The lead names both, and there are exactly two consequence sentences -- the Brief's
            # and the Style bible's, not one sentence pretending they are the same document.
            assert "Creative brief and Style bible" in together, together
            assert "Restore beside the box swaps it back" in together, together
            assert "no way back" in together, together
            assert "Treatment" not in together, together
            driver.switch_to.alert.dismiss()
            assert stored(server.base_url, project_id)["style_bible"] == STYLE
            result["one_question_names_both"] = " ".join(together.split())
            driver.save_screenshot(str(artifact_dir() / f"{NAME}-two-documents.png"))

            console_gate(driver, NAME, result)
        finally:
            driver.quit()

    report(NAME, result)


if __name__ == "__main__":
    main()
