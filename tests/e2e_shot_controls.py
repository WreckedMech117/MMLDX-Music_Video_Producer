"""Browser QA for the shot inspector's two commitment controls, and the promote control.

None of these had ever been driven in a browser. All three are proven offline by *executing* their
deciding logic -- `markReadyControl`, `renderAgainControl`, `multiviewPlan` -- against a stub DOM
under node, which is why nothing here re-proves that logic. A stub DOM structurally cannot see a
control that never renders, a selector that matches nothing, a button hidden by CSS, a handler
bound to an element that no longer exists, or a control that renders and is then covered by
something else. Those are the failure modes a brand-new control carries, so this script asserts on
what a Director could see and click, and on what the server holds afterwards.

**No GPU is spent and nothing reaches `/prompt`.** Mark-ready, mark-draft and render-again are
status writes on their own routes; none of them submits anything. The promote control *would*
submit, so it is inspected and never clicked -- its rendered presence and enabled state are the
whole assertion, which is exactly what changed today when `prop` and `setting` joined `character`.

Every fixture is built through shipped routes. Nothing is hand-written into a manifest.

Run from the repo root -- it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_shot_controls.py [--port 8767]

Assumes: nothing listening on the port (it refuses to reuse a bound one), Microsoft Edge and its
WebDriver installed, and `music_video_producer` importable from this checkout's `src/`. ComfyUI
does not need to be running: the ComfyUI status dot goes red and nothing here reads it.
"""

from __future__ import annotations

import base64
import sys

from e2e_support import (
    ManagedServer,
    StaleServer,
    artifact_dir,
    clear_toasts,
    console_gate,
    covering_element,
    edge_driver,
    get_json,
    post_json,
    post_multipart,
    put_json,
    reachable_widths,
    report,
    settle,
    visible_and_clickable,
    wait_for_readiness,
    wait_for_toast,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

NAME = "shot-controls"

# A 1x1 PNG. Real bytes, because the upload route checks the suffix and the inspector puts the
# file behind an <img> the browser actually fetches.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

SHOTS = [
    {"id": "shot_draft", "start": 0, "duration": 4, "prompt": "The corridor, pushing in slowly.",
     "mode": "text", "status": "draft"},
    {"id": "shot_blank", "start": 4, "duration": 4, "prompt": "",
     "mode": "text", "status": "draft"},
    {"id": "shot_settled", "start": 8, "duration": 4, "prompt": "The same corridor, wider.",
     "mode": "text", "status": "complete"},
    {"id": "shot_locked", "start": 12, "duration": 4, "prompt": "A take nobody may touch.",
     "mode": "text", "status": "complete", "locked": True},
    # In flight. Neither control's status list covers it, so the panel must draw neither -- the one
    # state where a second submission does concrete harm.
    {"id": "shot_queued", "start": 16, "duration": 4, "prompt": "Already on the card.",
     "mode": "text", "status": "queued"},
]


def seed(base_url: str) -> dict:
    """Build the project through shipped routes only."""
    project = post_json(f"{base_url}/api/projects", {"name": "Shot controls browser QA"})
    project_id = project["id"]
    project = put_json(f"{base_url}/api/projects/{project_id}/shots", {"shots": SHOTS})

    picture = artifact_dir() / "shot-controls-source.png"
    picture.write_bytes(PNG)
    for kind in ("prop", "character", "style"):
        project = post_multipart(
            f"{base_url}/api/projects/{project_id}/assets/upload",
            {"name": f"Landed {kind}", "kind": kind},
            ("file", picture),
        )
    # One Asset whose source image has not landed. That is the state a Flux generation leaves
    # behind while the job runs, and it is the only way to reach the promote control's disabled
    # arm without spending a GPU pass -- so it is fabricated through the full-project write rather
    # than by rendering. `path` is what `multiviewPlan` reads for readiness.
    project["assets"].append(
        {"name": "Pending setting", "kind": "setting", "path": "", "source": "flux",
         "prompt_id": "pending-1234"}
    )
    project = put_json(f"{base_url}/api/projects/{project_id}", project)
    kinds = {asset["kind"]: asset["id"] for asset in project["assets"]}
    return {"project": project, "id": project_id, "assets": kinds}


def select_project(driver, wait, project_id: str) -> None:
    wait.until(EC.presence_of_element_located((By.ID, "project-select")))
    option = wait.until(
        lambda browser: browser.find_element(
            By.CSS_SELECTOR, f'#project-select option[value="{project_id}"]'
        )
    )
    option.click()
    wait.until(
        lambda browser: browser.find_element(By.ID, "project-select").get_attribute("value")
        == project_id
    )


def select_clip(driver, wait, shot_id: str):
    """Click the clip on the timeline, the way a Director selects a shot, and wait for the panel.

    The clip is bound on `pointerdown`, so a scripted `.dispatchEvent(new Event('click'))` would
    select nothing. A real click is the point.
    """
    clear_toasts(driver)
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
    # The selection writes the shot list back, and the reply to that write reloads readiness,
    # whose reply rebuilds the inspector again. Read nothing until that has finished.
    settle(driver, "#shot-inspector")
    return wait.until(EC.visibility_of_element_located((By.ID, "shot-inspector")))


def control(driver, selector: str):
    found = driver.find_elements(By.CSS_SELECTOR, f"#shot-inspector {selector}")
    return found[0] if found else None


def status_chip(driver) -> str:
    """The status the chip actually paints. Lower-cased: `.shot-status` is uppercased in CSS, so
    the rendered text is "DRAFT" where the manifest says "draft"."""
    return driver.find_element(By.CSS_SELECTOR, "#shot-inspector .shot-status").text.strip().lower()


def stored_status(base_url: str, project_id: str, shot_id: str) -> str:
    project = get_json(f"{base_url}/api/projects/{project_id}")
    return next(shot["status"] for shot in project["shots"] if shot["id"] == shot_id)


def main() -> None:
    port = 8767
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    result: dict[str, object] = {}
    with ManagedServer(port, label=NAME) as server:
        result["server_identity"] = server.evidence
        fixture = seed(server.base_url)
        project_id = fixture["id"]
        assets = fixture["assets"]

        driver = edge_driver()
        wait = WebDriverWait(driver, 25)
        try:
            driver.get(server.base_url)
            select_project(driver, wait, project_id)
            # Before anything is read off the timeline: the readiness reply rebuilds the clips and
            # the inspector, and every reference taken ahead of it is stale the moment it lands.
            result["readiness_region"] = " ".join(wait_for_readiness(driver, wait).split())[:160]
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            wait.until(
                lambda browser: len(browser.find_elements(By.CSS_SELECTOR, "#shots-track .shot-clip"))
                == len(SHOTS)
            )

            # --- A drafted, prompted shot: the control the primary journey was missing ----------
            select_clip(driver, wait, "shot_draft")
            mark = control(driver, "#mark-ready")
            facts = visible_and_clickable(driver, mark, "the mark-ready button on a drafted shot")
            assert mark.is_enabled(), "mark-ready is drawn disabled on a prompted draft"
            assert mark.text.strip() == "Mark ready to queue", mark.text
            assert "no GPU time is spent" in (mark.get_attribute("title") or ""), (
                "the button carries no hover text saying nothing is rendered by it"
            )
            assert control(driver, "#render-again") is None, (
                "both commitment controls are drawn at once; their status lists are supposed to "
                "partition the vocabulary"
            )
            assert control(driver, ".control-reason") is None, (
                "an enabled control is carrying a refusal reason"
            )
            assert status_chip(driver) == "draft", status_chip(driver)
            queue_before = driver.find_element(By.ID, "queue-ready")
            assert queue_before.get_attribute("title") == "Mark a shot ready to queue H3", (
                queue_before.get_attribute("title")
            )
            result["mark_ready_geometry"] = facts

            mark.click()
            toast = wait_for_toast(driver, wait, "committed to the render queue")
            assert "Nothing has been rendered" in toast, toast
            wait.until(lambda browser: status_chip(browser) == "ready")
            assert stored_status(server.base_url, project_id, "shot_draft") == "ready", (
                "the click redrew the panel but the server never stored the commitment"
            )
            assert driver.find_element(By.ID, "queue-ready").get_attribute("title") != (
                "Mark a shot ready to queue H3"
            ), "the commitment never reached the queue button that acts on it"
            result["mark_ready_committed"] = True

            # The same button, now the other direction. Re-found: the click re-rendered the panel,
            # so the reference above points at an element that is no longer in the document.
            settle(driver, "#shot-inspector")
            back = control(driver, "#mark-ready")
            clear_toasts(driver)
            visible_and_clickable(driver, back, "the back-to-draft button on a committed shot")
            assert back.text.strip() == "Back to draft", back.text
            back.click()
            wait_for_toast(driver, wait, "is back to draft")
            wait.until(lambda browser: status_chip(browser) == "draft")
            assert stored_status(server.base_url, project_id, "shot_draft") == "draft"
            result["mark_draft_reversed"] = True

            # --- A drafted shot with no prompt: shown, shut, and saying why --------------------
            select_clip(driver, wait, "shot_blank")
            blocked = control(driver, "#mark-ready")
            visible_and_clickable(driver, blocked, "the mark-ready button on an unprompted shot")
            assert not blocked.is_enabled(), "an unprompted shot offers a live commit button"
            reason = control(driver, ".control-reason")
            assert reason is not None and reason.is_displayed(), (
                "the refusal is not rendered where the Director is being refused"
            )
            assert "no prompt" in reason.text.lower(), reason.text
            assert "shot inspector" in reason.text.lower(), (
                f"the reason does not say what to do about it: {reason.text}"
            )
            # A disabled attribute the browser actually honours, rather than a grey button that
            # still fires. Nothing offline can tell those apart.
            blocked.click()
            assert stored_status(server.base_url, project_id, "shot_blank") == "draft", (
                "a disabled mark-ready button still committed the shot"
            )
            result["unprompted_shot_refused_in_place"] = " ".join(reason.text.split())[:160]

            # --- A settled shot: render again ------------------------------------------------
            select_clip(driver, wait, "shot_settled")
            assert control(driver, "#mark-ready") is None, (
                "a settled shot offers the commit control as well as the re-open one"
            )
            again = control(driver, "#render-again")
            result["render_again_geometry"] = visible_and_clickable(
                driver, again, "the render-again button on a completed shot"
            )
            assert again.is_enabled(), "render-again is drawn disabled on an ordinary completed shot"
            assert again.text.strip() == "Render again", again.text
            assert "no GPU time is spent" in (again.get_attribute("title") or ""), (
                again.get_attribute("title")
            )
            assert status_chip(driver) == "complete", status_chip(driver)

            again.click()
            reopened = wait_for_toast(driver, wait, "is open for another render")
            assert "is not deleted" in reopened, reopened
            # Measured while that toast is still up, and recorded rather than judged. `.toast-region`
            # is `position: fixed` in the bottom-right corner at `z-index: 50`, which is where the
            # shot inspector's own buttons are, and the render-again toast is six lines tall. So the
            # panel that raised it can be partly un-clickable for the 4.2 s it stands, and this is
            # the first run that could see that -- it is invisible to a stub DOM, and it is what
            # made this script's own first pass fail. Whether it is acceptable is the Director's call.
            result["toast_over_inspector_controls"] = {
                selector: covering_element(driver, control(driver, selector))
                for selector in ("#compile-shot", "#shot-prompt", "#shot-seed")
            }
            wait.until(lambda browser: status_chip(browser) == "ready")
            assert stored_status(server.base_url, project_id, "shot_settled") == "ready"
            # The one thing this control must never do without being asked twice.
            after = get_json(f"{server.base_url}/api/projects/{project_id}")
            assert not after["jobs"], f"re-opening a shot queued a render: {after['jobs']}"
            assert after["shots"][2]["latest_output"] == "", "the previous take's pointer moved"
            result["render_again_reopened_without_submitting"] = True

            # --- A locked shot: shown, shut, and saying why ------------------------------------
            select_clip(driver, wait, "shot_locked")
            locked = control(driver, "#render-again")
            visible_and_clickable(driver, locked, "the render-again button on a locked shot")
            assert not locked.is_enabled(), "a locked shot offers a live render-again button"
            locked_reason = control(driver, ".control-reason")
            assert locked_reason is not None and locked_reason.is_displayed(), (
                "the lock refusal is not rendered in the panel"
            )
            assert "locked" in locked_reason.text.lower(), locked_reason.text
            locked.click()
            assert stored_status(server.base_url, project_id, "shot_locked") == "complete", (
                "a disabled render-again button still re-opened a locked shot"
            )
            result["locked_shot_refused_in_place"] = " ".join(locked_reason.text.split())[:160]

            # --- A shot in flight: neither control, and the panel still draws -------------------
            select_clip(driver, wait, "shot_queued")
            assert control(driver, "#mark-ready") is None and control(driver, "#render-again") is None, (
                "a queued shot is offering a commitment control; a second submission is the one "
                "thing this state must not make easy"
            )
            assert status_chip(driver) == "queued", status_chip(driver)
            visible_and_clickable(driver, control(driver, "#compile-shot"), "the compile button")
            result["in_flight_shot_offers_neither_control"] = True

            # --- The promote control, driven but deliberately never fired ----------------------
            driver.find_element(By.CSS_SELECTOR, '[data-panel="assets"]').click()
            wait.until(EC.visibility_of_element_located((By.ID, "asset-grid")))
            wait.until(
                lambda browser: len(browser.find_elements(By.CSS_SELECTOR, ".asset-card")) == 4
            )
            promote: dict[str, object] = {}
            for kind, asset_id in assets.items():
                card = driver.find_element(By.CSS_SELECTOR, f'.asset-card[data-asset-id="{asset_id}"]')
                visible_and_clickable(driver, card, f"the {kind} asset card")
                card.click()
                wait.until(
                    lambda browser, wanted=asset_id: "selected"
                    in browser.find_element(
                        By.CSS_SELECTOR, f'.asset-card[data-asset-id="{wanted}"]'
                    ).get_attribute("class")
                )
                buttons = driver.find_elements(By.CSS_SELECTOR, "#asset-inspector #create-multiview")
                if not buttons:
                    promote[kind] = {"shown": False}
                    continue
                button = buttons[0]
                visible_and_clickable(driver, button, f"the promote button on a {kind} asset")
                promote[kind] = {
                    "shown": True,
                    "enabled": button.is_enabled(),
                    "label": button.text.strip(),
                }
            # Today's change: the control is no longer character-only.
            assert promote["prop"] == {"shown": True, "enabled": True,
                                       "label": "Create Krea multiview sheet"}, promote
            assert promote["character"]["shown"] and promote["character"]["enabled"], promote
            assert promote["style"] == {"shown": False}, (
                f"a style asset offers a promotion the route refuses: {promote}"
            )
            assert promote["setting"]["shown"] and not promote["setting"]["enabled"], (
                f"an asset whose source image has not landed offers a live promotion: {promote}"
            )
            result["promote_control"] = promote
            # Nothing above pressed it. Proven rather than asserted in a comment.
            assert not get_json(f"{server.base_url}/api/projects/{project_id}")["jobs"], (
                "this script queued a job; it must never reach /prompt"
            )
            result["nothing_submitted"] = True

            # --- Where these controls stop being reachable -------------------------------------
            widths = [1600, 1280, 1024, 820]
            reach = {"#create-multiview": reachable_widths(driver, "#create-multiview", widths)}
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            wait.until(EC.visibility_of_element_located((By.ID, "shot-inspector")))
            reach["#shot-inspector"] = reachable_widths(driver, "#shot-inspector", widths)
            result["reachable_widths"] = reach
            assert reach["#create-multiview"]["1600"].startswith("reachable"), reach
            assert reach["#shot-inspector"]["1600"].startswith("reachable"), reach

            driver.save_screenshot(str(artifact_dir() / f"{NAME}.png"))
            console_gate(driver, NAME, result)
        finally:
            driver.quit()
    report(NAME, result)


if __name__ == "__main__":
    try:
        main()
    except StaleServer as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        raise SystemExit(2) from error
