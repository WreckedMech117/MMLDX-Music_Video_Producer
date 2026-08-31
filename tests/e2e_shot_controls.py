"""Browser QA for the shot inspector's commitment and expansion controls, and the promote control.

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

Since pass two of the H3 expansion landed it also drives "Expand prompt" and the plan-wide sweep,
which spend language-model calls rather than GPU minutes and are therefore *pressed*. Their
assertions are written for either answer the model can give, because a local model returning a
malformed prompt is the expected case the whole feature exists to catch: what has to hold is that
the creative intent is never overwritten and that a refused answer is never stored.

Since 2026-08-18 it also gates the three defects the first browser run found, none of which the
offline harness can reach: that a live toast does not intercept a click meant for the control that
raised it, that neither inspector is hidden by a media query at any width this script measures, and
that choosing which shot to look at does not write the whole shot list back or rebuild the panel
under the Director's hands. The toast overlap used to be recorded here as an observation.

Since 2026-08-18 it also gates the take player and the approval pair (FR-21): a real, playable
take is synthesized with ffmpeg -- a two-second color-and-tone source, no GPU, nothing to
`/prompt` -- and placed where the server's confined `latest_output` resolution will find it, under
an isolated `MVP_COMFY_ROOT` this script owns. The browser then proves what no offline harness
can: `loadedmetadata` fires with a nonzero duration read from bytes the take route actually
streamed, a `currentTime` seek lands, approving flips the pair and disables render-again with the
approval named in place, and un-approving restores it.

Every fixture is built through shipped routes. Nothing is hand-written into a manifest; the one
file written to disk directly is the synthesized take, which is ComfyUI's half of the contract
and has no route to arrive by without a GPU.

Run from the repo root -- it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_shot_controls.py [--port 8767]

Assumes: nothing listening on the port (it refuses to reuse a bound one), Microsoft Edge and its
WebDriver installed, ffmpeg on PATH, and `music_video_producer` importable from this checkout's
`src/`. ComfyUI does not need to be running: the ComfyUI status dot goes red and nothing here
reads it.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

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
    resource_hits,
    settle,
    toasts_over,
    visible_and_clickable,
    wait_for_readiness,
    wait_for_toast,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
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
    # A completed shot with a real, playable take on disk -- the subject of the FR-21 section.
    # `latest_output` is filled in by `seed`, which knows the project id the path must carry, and
    # the ffmpeg synthesis writes the file where the server's confined resolution will find it.
    {"id": "shot_take", "start": 20, "duration": 4, "prompt": "The corridor, held on the turn.",
     "mode": "text", "status": "complete", "latest_output": ""},
]


def synthesize_take(target: Path) -> None:
    """A real two-second video with a tone, made locally by ffmpeg.

    No GPU and nothing to `/prompt`: this is the file a render would have left behind, produced
    the only way a test may produce one. Color plus a 440 Hz sine, H.264 in an MP4, because the
    assertion downstream is the browser's own `loadedmetadata` and a container Edge cannot open
    would fail the right test for the wrong reason.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=orange:size=320x240:duration=2:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(target),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
    except FileNotFoundError as error:
        raise StaleServer(
            "ffmpeg is not on PATH, and the take-playback section needs it to synthesize a "
            "playable take without a GPU."
        ) from error
    except subprocess.CalledProcessError as error:
        raise StaleServer(f"ffmpeg could not synthesize the take:\n{error.stderr}") from error
    if not target.is_file() or target.stat().st_size == 0:
        raise StaleServer(f"ffmpeg reported success and wrote nothing at {target}")


def seed(base_url: str, comfy_root: Path) -> dict:
    """Build the project through shipped routes only.

    The one exception is the take file itself, which is ComfyUI's half of the contract: it is
    synthesized onto disk under `comfy_root/output` at exactly the path the manifest names, so
    the take route's confined resolution -- ids in, manifest out -- is what the browser exercises.
    """
    project = post_json(f"{base_url}/api/projects", {"name": "Shot controls browser QA"})
    project_id = project["id"]
    latest_output = f"music-video-producer/{project_id}/shots/shot_take-h3_00001.mp4"
    synthesize_take(comfy_root / "output" / latest_output)
    shots = [
        {**shot, "latest_output": latest_output} if shot["id"] == "shot_take" else shot
        for shot in SHOTS
    ]
    project = put_json(f"{base_url}/api/projects/{project_id}/shots", {"shots": shots})

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
    # The track before the clip: a reply landing between finding a clip and clicking it detaches
    # the element, and a retry there would hide the race rather than wait for it.
    settle(driver, "#shots-track")
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
    # An isolated ComfyUI root this run owns, told to the server through the environment the
    # ManagedServer copies. The user's real ComfyUI installation is never read or written, and
    # the synthesized take is the only thing that will ever exist under this one.
    comfy_root = Path(tempfile.mkdtemp(prefix="mvp-shot-controls-comfy-"))
    os.environ["MVP_COMFY_ROOT"] = str(comfy_root)
    with ManagedServer(port, label=NAME) as server:
        result["server_identity"] = server.evidence
        fixture = seed(server.base_url, comfy_root)
        project_id = fixture["id"]
        assets = fixture["assets"]

        driver = edge_driver()
        wait = WebDriverWait(driver, 25)
        try:
            driver.get(server.base_url)
            select_project(driver, wait, project_id)
            # Before anything is read off the timeline: the readiness reply rebuilds the clips and
            # the inspector, and every reference taken ahead of it is stale the moment it lands.
            # This plan's own report, named by its shot count: the app loads the first project in
            # the root before this script selects the one it seeded, and that project's empty
            # report would otherwise be taken as the signal that this one's had landed.
            result["readiness_region"] = " ".join(
                wait_for_readiness(driver, wait, f"of {len(SHOTS)} shots").split()
            )[:160]
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
            # Scoped to the control it belongs to, not the first `.control-reason` in the panel.
            # Since pass two the inspector draws a second refusable control -- "Expand prompt",
            # above this one -- so a bare class selector reads that one's reason and asserts the
            # wrong sentence. The browser run caught exactly that.
            assert control(driver, "#mark-ready + .control-reason") is None, (
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
            # Scoped to the control it belongs to, not the first `.control-reason` in the panel.
            # Since pass two the inspector draws a second refusable control -- "Expand prompt",
            # above this one -- so a bare class selector reads that one's reason and asserts the
            # wrong sentence. The browser run caught exactly that.
            reason = control(driver, "#mark-ready + .control-reason")
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
            # The button's own cost statement. It used to read "no GPU time is spent",
            # which was true while render-again only re-opened the shot; since 2026-08-18
            # it queues a take in one gesture (713d09b) and `RENDER_AGAIN_HELP` says so.
            # This assertion tracked the old sentence and had been failing at baseline
            # ever since — the behaviour changed and its guard did not follow.
            assert "queue one new take" in (again.get_attribute("title") or ""), (
                again.get_attribute("title")
            )
            assert status_chip(driver) == "complete", status_chip(driver)

            again.click()
            # **The click asks first, and this script answers Cancel.** Since 2026-08-19 (`713d09b`)
            # Render again is one gesture -- re-open, move the seed, queue a take -- and it puts that
            # question up before any of it. This harness had never learned to answer it, so every run
            # since died on the unhandled dialog rather than on anything it asserts; that is item 86,
            # and it is the second stale claim found in this file after the `no GPU time is spent`
            # title assertion corrected in `b217585`.
            #
            # Cancel rather than accept, and not for convenience: this script's whole cost contract
            # is that nothing here reaches `/prompt`, and the cancel arm is exactly the behaviour the
            # assertions below were written for -- re-opened, seed untouched, nothing queued. The
            # accept arm is driven, with a seed read back from the manifest, by
            # `tests/e2e_seed_and_asset_tabs.py`, which owns a dead ComfyUI port for the purpose.
            question = WebDriverWait(driver, 10).until(EC.alert_is_present())
            asked = question.text
            # The bundle it would spend, named from the project rather than promised. The dialog said
            # `(turbo, fresh seed)` from 2026-08-19 until 2026-08-31 while the submission had stopped
            # sending a profile at all on 2026-08-23 (`cd5c785`) -- a 4-step bundle promised for a
            # render that takes the project's own, which is 20 steps for a Director who has changed
            # nothing. Asserted here because this is the sentence a Director reads last before
            # spending GPU minutes; `renderAgainQuestion` is executed against every bundle by the
            # frontend contract.
            assert "fresh seed" in asked, asked
            assert "Cancel re-opens the shot without rendering" in asked, asked
            assert "Bundle: Default — 20 steps" in asked, (
                ("the queue question is not naming the bundle this project actually renders on; it "
                 "promised `turbo` for four days after the submission stopped asking for one"),
                asked,
            )
            assert "turbo" not in asked.lower(), asked
            question.dismiss()
            result["render_again_question"] = " ".join(asked.split())
            reopened = wait_for_toast(driver, wait, "is open for another render")
            assert "is not deleted" in reopened, reopened
            # Asserted while that toast is still up. This was an observation until 2026-08-18:
            # `.toast-region` is `position: fixed` in the bottom-right corner at `z-index: 50`,
            # which is where the shot inspector's own buttons are, and the render-again toast is six
            # lines tall -- so a click meant for `#compile-shot` landed on `div.toast.info`, and the
            # confirmation of what the Director had just done blocked the next thing they tried.
            #
            # **This assertion read `not any(covering.values())` until 2026-08-31, and the product
            # stopped meaning it on 2026-08-19.** `9bd9447` gave `.toast` back `pointer-events:
            # auto` and a click handler, deliberately and with its reason in `styles.css`: an error
            # toast has no timer at all, so a dead click-shield would trap stale text over the
            # workspace forever. The region between toasts stays transparent; a toast itself takes
            # one click, and that click only removes it. So "nothing is on top of the control" is no
            # longer true and is no longer the guarantee.
            #
            # What is still owed to the Director is that the control underneath stays *reachable*,
            # and that is what is asserted now: only a toast may be on top of an inspector control,
            # one click clears it, that click does nothing else, and the press then arrives. Nothing
            # caught the change when it landed because this script had been dying on the Render
            # again dialog since 2026-08-19 -- the same day -- which is item 86 exactly.
            standing_over = toasts_over(driver, control(driver, "#compile-shot"))
            covering = {
                selector: covering_element(driver, control(driver, selector))
                for selector in ("#compile-shot", "#shot-prompt", "#shot-seed")
            }
            assert all(
                found is None or found.startswith("div.toast")
                for found in covering.values()
            ), (
                f"something that is not a toast is intercepting clicks meant for the shot "
                f"inspector's own controls: {covering}"
            )
            # One click, and only to dismiss. Aimed at the compile button and answered by the toast
            # standing over it, so what this measures is the gesture a Director actually makes.
            absorbed = bool(covering["#compile-shot"])
            if absorbed:
                # Through the pointer at the button's own centre, not `element.click()`: WebDriver
                # refuses to click an element something else would receive the click for, which is
                # the very situation under test. `ActionChains` presses the pixel, so whatever is on
                # top gets it -- exactly as it does for a Director aiming at the compile button.
                ActionChains(driver).move_to_element(control(driver, "#compile-shot")).click().perform()
                WebDriverWait(driver, 8).until(
                    lambda browser: not browser.find_elements(
                        By.CSS_SELECTOR, "#toast-region .toast"
                    ),
                    "the toast over the compile button did not go away when it was clicked",
                )
                # And it took nothing with it. A click that reached `#compile-shot` as well would
                # raise "Director data ready" a moment later, so the corner is watched staying
                # empty rather than merely being empty once.
                time.sleep(0.6)
                assert not driver.find_elements(By.CSS_SELECTOR, "#toast-region .toast"), (
                    "the click that dismissed the toast also fired the control underneath it, so "
                    "one gesture did two things"
                )
            after_dismiss = {
                selector: covering_element(driver, control(driver, selector))
                for selector in ("#compile-shot", "#shot-prompt", "#shot-seed")
            }
            assert not any(after_dismiss.values()), (
                f"the shot inspector's controls are still covered after the toast was dismissed, "
                f"so this is a shield rather than a toast: {after_dismiss}"
            )
            # And the press itself, not only the hit test. Compile is the one control here that can
            # be fired through a live toast to prove the press arrives: it is a dry run that queues
            # nothing, spends no GPU time and changes no stored state.
            compile_button = control(driver, "#compile-shot")
            visible_and_clickable(driver, compile_button, "the compile button under a live toast")
            compile_button.click()
            compiled = wait_for_toast(driver, wait, "Director data ready")
            result["toast_over_inspector_controls"] = {
                "toasts_standing_over_compile": standing_over,
                "covering": covering,
                "one_click_dismissed_it": absorbed,
                "covering_after_dismiss": after_dismiss,
                "click_through_while_toast_up": " ".join(compiled.split())[:120],
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
            # Scoped to the control it belongs to, not the first `.control-reason` in the panel.
            # Since pass two the inspector draws a second refusable control -- "Expand prompt",
            # above this one -- so a bare class selector reads that one's reason and asserts the
            # wrong sentence. The browser run caught exactly that.
            locked_reason = control(driver, "#render-again + .control-reason")
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

            # --- A take can be watched, scrubbed, approved and un-approved (FR-21) -------------
            # The player's metadata is the browser's own reading of bytes the take route
            # streamed, so a nonzero duration is proof of the whole serving path -- not of any
            # markup. The seek is the scrub bar working end to end against that route.
            select_clip(driver, wait, "shot_take")
            player = control(driver, "#take-player")
            assert player is not None, "a shot with a take on disk draws no player"
            assert player.is_displayed(), "the take player is rendered but not visible"
            media = WebDriverWait(driver, 20).until(
                lambda browser: browser.execute_script(
                    "const video = document.querySelector('#take-player');"
                    "return video && video.readyState >= 1 && video.duration > 0"
                    " ? {duration: video.duration, src: video.currentSrc} : null;"
                )
            )
            assert 1.5 <= media["duration"] <= 2.6, media
            assert f"/api/projects/{project_id}/shots/shot_take/take" in media["src"], media
            seeked = WebDriverWait(driver, 20).until(
                lambda browser: browser.execute_script(
                    "const video = document.querySelector('#take-player');"
                    "if (video.dataset.seekAsked !== '1') {"
                    "  video.dataset.seekAsked = '1'; video.currentTime = 1.2; return null; }"
                    "return video.seekable.length > 0 && video.currentTime >= 1.0"
                    " ? {currentTime: video.currentTime, seekableEnd: video.seekable.end(0)}"
                    " : null;"
                )
            )
            assert seeked["currentTime"] >= 1.0, seeked
            result["take_playback"] = {"duration": media["duration"], **seeked}

            # Approve: the pair flips, the manifest holds the watched take in both fields, and
            # render-again shuts with the approval named where the Director is refused.
            approve_button = control(driver, "#approve-take")
            facts = visible_and_clickable(driver, approve_button, "the approve button on a take")
            assert approve_button.is_enabled(), "a settled take offers a dead approve button"
            assert approve_button.text.strip() == "Approve take", approve_button.text
            clear_toasts(driver)
            approve_button.click()
            approved_toast = wait_for_toast(driver, wait, "latest take is approved")
            assert "cannot be re-rendered" in approved_toast, approved_toast
            wait.until(lambda browser: status_chip(browser) == "approved")
            settle(driver, "#shot-inspector")
            stored_take = next(
                shot for shot in get_json(f"{server.base_url}/api/projects/{project_id}")["shots"]
                if shot["id"] == "shot_take"
            )
            assert stored_take["status"] == "approved", stored_take["status"]
            assert stored_take["approved_output"] == stored_take["latest_output"], stored_take
            assert stored_take["approved_output"].endswith("shot_take-h3_00001.mp4")
            flipped = control(driver, "#approve-take")
            assert flipped.text.strip() == "Un-approve take", flipped.text
            refused = control(driver, "#render-again")
            assert refused is not None, "an approved shot draws no render-again control at all"
            assert not refused.is_enabled(), "an approved shot offers a live render-again button"
            approval_reason = control(driver, "#render-again + .control-reason")
            assert approval_reason is not None and approval_reason.is_displayed(), (
                "the approval refusal is not rendered where the Director is refused"
            )
            assert "approved take" in approval_reason.text, approval_reason.text
            # A disabled attribute the browser actually honours, rather than a grey button that
            # still fires. Nothing offline can tell those apart.
            refused.click()
            assert stored_status(server.base_url, project_id, "shot_take") == "approved", (
                "a disabled render-again button still re-opened an approved shot"
            )
            result["take_approved"] = {
                "toast": " ".join(approved_toast.split())[:160],
                "refusal_in_place": " ".join(approval_reason.text.split())[:160],
                "geometry": facts,
            }

            # Un-approve: the one way back, and it re-enables render-again in the same panel.
            clear_toasts(driver)
            flipped = control(driver, "#approve-take")
            visible_and_clickable(driver, flipped, "the un-approve button on an approved shot")
            flipped.click()
            cleared_toast = wait_for_toast(driver, wait, "approval is cleared")
            assert "Nothing was deleted" in cleared_toast, cleared_toast
            wait.until(lambda browser: status_chip(browser) == "complete")
            settle(driver, "#shot-inspector")
            stored_take = next(
                shot for shot in get_json(f"{server.base_url}/api/projects/{project_id}")["shots"]
                if shot["id"] == "shot_take"
            )
            assert stored_take["status"] == "complete", stored_take["status"]
            assert stored_take["approved_output"] == "", stored_take["approved_output"]
            assert stored_take["latest_output"].endswith("shot_take-h3_00001.mp4"), (
                "un-approving touched the take pointer"
            )
            again_back = control(driver, "#render-again")
            assert again_back is not None and again_back.is_enabled(), (
                "un-approving did not re-enable render-again"
            )
            # The whole lane queued nothing: watching, approving and un-approving are free.
            assert not get_json(f"{server.base_url}/api/projects/{project_id}")["jobs"], (
                "the approval lane queued a render; it must never reach /prompt"
            )
            result["take_approval_round_trip"] = {
                "unapprove_toast": " ".join(cleared_toast.split())[:160],
                "render_again_reenabled": True,
            }

            # --- Choosing which shot to look at is not an edit ---------------------------------
            # Selecting a clip used to write the whole shot list back on pointerup whether or not
            # anything had moved. The reply to that write reloaded readiness, and the reply to
            # *that* rebuilt the inspector -- two renders after the click looked finished, over a
            # panel the Director was already reading. Read out of the browser's own resource
            # timings, because what is being asserted is what the client chose to send.
            shots_path = f"/api/projects/{project_id}/shots"
            readiness_path = f"/api/projects/{project_id}/readiness"
            writes = resource_hits(driver, shots_path)
            reads = resource_hits(driver, readiness_path)
            select_clip(driver, wait, "shot_draft")
            assert resource_hits(driver, shots_path) == writes, (
                "selecting a clip wrote the whole shot list back; the reply to that write reloads "
                "readiness and the reply to that rebuilds the inspector under the Director's hands"
            )
            assert resource_hits(driver, readiness_path) == reads, (
                "selecting a clip reloaded readiness, which redraws the timeline and the panel"
            )
            # The counter is not blind. A real edit still saves, and still refreshes the readiness
            # the saved prompt could have changed -- without this the assertion above would pass
            # just as well against a page that had stopped talking to the server at all.
            audio_box = control(driver, "#shot-song-audio")
            visible_and_clickable(driver, audio_box, "the master-audio reference checkbox")
            audio_box.click()
            wait.until(lambda browser: resource_hits(browser, shots_path) == writes + 1)
            wait.until(lambda browser: resource_hits(browser, readiness_path) == reads + 1)
            settle(driver, "#shot-inspector")
            result["selection_is_not_a_write"] = {"selection": 0, "edit": 1}

            # --- A rebuild under the Director's hands keeps their place ------------------------
            # The panel is rebuilt with `innerHTML` by replies nobody awaited, so everything typed
            # since the last `change` went with it. Driven here through the zoom control, which
            # calls the same `renderTimeline` the readiness reply calls but synchronously, and
            # through a scripted `.click()`, which moves no focus -- the race without the timing.
            typed = "A corridor that holds one beat too long."
            prompt_box = control(driver, "#shot-prompt")
            prompt_box.click()
            prompt_box.send_keys(Keys.CONTROL, "a")
            prompt_box.send_keys(typed)
            driver.execute_script("document.querySelector('#zoom-in').click();")
            kept = driver.execute_script(
                "const box = document.querySelector('#shot-prompt');"
                "return {focused: document.activeElement === box, value: box.value,"
                " caret: box.selectionStart};"
            )
            assert kept["value"] == typed, (
                f"a rebuild discarded what was typed into the prompt box: {kept}"
            )
            assert kept["focused"], "a rebuild took the caret out of the box being typed into"
            assert kept["caret"] == len(typed), kept
            result["edit_survives_a_rebuild"] = kept
            # Committed, so nothing is left unsaved for the tab close -- the beforeunload guard
            # would otherwise hold a dialog open at quit. Blurred through the page rather than by
            # tabbing out of an element handle: the blur fires `change`, `change` rebuilds this
            # panel, and the handle the keystroke was addressed to is detached by the time the
            # keystroke lands. That is the same rebuild the assertions above are about, met from
            # the other side.
            driver.execute_script(
                "const box = document.querySelector('#shot-prompt');"
                "box.blur();"
                "box.dispatchEvent(new Event('change', {bubbles: true}));"
            )
            wait.until(lambda browser: resource_hits(browser, shots_path) >= writes + 2)
            settle(driver, "#shot-inspector")

            # --- The H3 expansion controls ----------------------------------------------------
            # Added 2026-08-18 with pass two. Every state of `expandPromptControl` is already
            # executed offline against a stub DOM, so nothing here re-proves the decision: what a
            # stub structurally cannot see is a control that never renders, a button hidden by CSS,
            # a handler bound to an element that is gone, or a `disabled` attribute the browser does
            # not honour -- and a brand-new control carries all four.
            #
            # **No GPU is spent and nothing reaches `/prompt`.** The expansion routes queue nothing;
            # what they spend is one language-model call, which is the point of driving them here.
            expansion: dict[str, object] = {}
            for shot_id, expected in (
                ("shot_draft", "enabled"),
                ("shot_blank", "creative intent"),
                ("shot_locked", "locked"),
                ("shot_settled", "already rendered"),
            ):
                select_clip(driver, wait, shot_id)
                button = control(driver, "#expand-prompt")
                assert button is not None, f"{shot_id} draws no expand-prompt control at all"
                facts = visible_and_clickable(driver, button, f"the expand-prompt button on {shot_id}")
                if expected == "enabled":
                    assert button.is_enabled(), "a prompted, open shot offers a dead expand button"
                    assert button.text.strip() == "Expand prompt", button.text
                    assert "Nothing is rendered" in (button.get_attribute("title") or ""), (
                        button.get_attribute("title")
                    )
                    expansion[shot_id] = {"enabled": True, "geometry": facts}
                    continue
                assert not button.is_enabled(), f"{shot_id} offers a live expand button"
                reason = control(driver, "#expand-prompt + .control-reason")
                assert reason is not None and reason.is_displayed(), (
                    f"the refusal for {shot_id} is not rendered where the Director is refused"
                )
                assert expected in reason.text.lower(), reason.text
                # A `disabled` attribute the browser actually honours, rather than a grey button
                # that still fires. Nothing offline can tell those apart.
                button.click()
                expansion[shot_id] = {"enabled": False, "reason": " ".join(reason.text.split())[:140]}
            after_disabled = get_json(f"{server.base_url}/api/projects/{project_id}")
            assert all(not shot["h3_prompt"] for shot in after_disabled["shots"]), (
                "a disabled expand button still expanded a shot"
            )

            # The live call. Whichever way the model answers, two things hold: the creative intent
            # is not overwritten, and an answer the format checker refuses is not stored. Both
            # outcomes are asserted rather than one being treated as a flake, because a local model
            # returning a malformed prompt is the *expected* case this feature exists to catch.
            select_clip(driver, wait, "shot_draft")
            intent_before = next(
                shot["prompt"] for shot in after_disabled["shots"] if shot["id"] == "shot_draft"
            )
            control(driver, "#expand-prompt").click()
            expanded = wait_for_toast(driver, WebDriverWait(driver, 300), "H3 prompt")
            settle(driver, "#shot-inspector")
            stored = next(
                shot for shot in get_json(f"{server.base_url}/api/projects/{project_id}")["shots"]
                if shot["id"] == "shot_draft"
            )
            assert stored["prompt"] == intent_before, (
                "the expansion overwrote the creative intent it was written from"
            )
            applied = "written for" in expanded
            if applied:
                assert stored["h3_prompt"], "the toast claims a prompt was written and none was"
                assert control(driver, "#shot-h3-prompt") is not None, (
                    "the expansion was stored and the panel does not show it"
                )
                assert control(driver, "#expand-prompt").text.strip() == "Expand prompt again"
            else:
                assert stored["h3_prompt"] == "", (
                    "a malformed answer reached the manifest, where the next render would submit it"
                )
                report_block = control(driver, "#expansion-report")
                assert report_block is not None and report_block.is_displayed(), (
                    "a refused answer was thrown away instead of being shown"
                )
            expansion["live_single"] = {
                "toast": " ".join(expanded.split())[:200],
                "applied": applied,
                "intent_unchanged": stored["prompt"] == intent_before,
            }

            # The plan-wide sweep, from the Director workspace. Only one shot in this plan is
            # writable and prompted, so this is one further model call; the other four are refused
            # without one and have to be named anyway.
            driver.find_element(By.CSS_SELECTOR, '[data-panel="treatment"]').click()
            sweep = wait.until(EC.visibility_of_element_located((By.ID, "expand-h3-prompts")))
            # Before the hit test, not after it. The single expansion's own toast is still up, it
            # is a long one, and a toast has taken clicks again since 2026-08-19 (`9bd9447`) -- so
            # `visible_and_clickable` was measuring the corner the previous step had just filled and
            # reporting the sweep button as covered. The wait was already here; only its position
            # was wrong, and nothing noticed because this script died three hundred lines earlier.
            clear_toasts(driver)
            visible_and_clickable(driver, sweep, "the plan-wide H3 sweep button")
            assert sweep.is_enabled(), "a plan with shots offers a dead sweep button"
            assert "one call per shot" in (sweep.get_attribute("title") or ""), (
                sweep.get_attribute("title")
            )
            sweep.click()
            swept = wait_for_toast(driver, WebDriverWait(driver, 600), "H3 prompt")
            thread = driver.find_element(By.ID, "chat-thread").text
            after_sweep = get_json(f"{server.base_url}/api/projects/{project_id}")
            # Every shot is named, which is what "reported individually" means: a shot the sweep
            # skipped in silence is indistinguishable from one it forgot.
            for shot in after_sweep["shots"]:
                assert shot["id"] in thread, (
                    f"{shot['id']} was swept and never mentioned in the report: {thread[-600:]}"
                )
            assert "no intent to expand from" in thread
            assert "they are locked" in thread
            expansion["live_sweep"] = {
                "toast": " ".join(swept.split())[:200],
                "written": sum(1 for shot in after_sweep["shots"] if shot["h3_prompt"]),
                "report": " ".join(thread.split())[-900:],
            }
            result["h3_expansion"] = expansion
            assert not after_sweep["jobs"], "expansion queued a render; it must never reach /prompt"
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            wait.until(EC.visibility_of_element_located((By.ID, "shot-inspector")))

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

            # --- These controls stop being reachable nowhere ----------------------------------
            # Until 2026-08-18 they did: `.asset-layout .inspector` was `display: none` below
            # 1180px and `.timeline-layout .shot-inspector` below 860px. Neither panel has a second
            # surface -- the promote control exists only in the asset inspector, and mark-ready,
            # render-again and the shot prompt only in the shot inspector -- so those queries took
            # the actions off the screen rather than rearranging them. Both panels reflow now.
            widths = [1600, 1280, 1024, 820]
            reach = {
                "#create-multiview": reachable_widths(driver, "#create-multiview", widths),
                "#asset-inspector": reachable_widths(driver, "#asset-inspector", widths),
            }
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            wait.until(EC.visibility_of_element_located((By.ID, "shot-inspector")))
            for selector in ("#shot-inspector", "#mark-ready", "#compile-shot"):
                reach[selector] = reachable_widths(driver, selector, widths)
            result["reachable_widths"] = reach
            # The controls, at every width. A button is small enough that "reachable" is the whole
            # question for it.
            for selector in ("#create-multiview", "#mark-ready", "#compile-shot"):
                for width, verdict in reach[selector].items():
                    assert verdict.startswith("reachable"), (selector, width, reach[selector])
            # The panels themselves are asserted only to be *drawn*: stacked into one column a
            # narrow window they are taller than the viewport, which is a scroll rather than a
            # disappearance, and "not displayed" is the verdict that means the media query is back.
            for selector in ("#asset-inspector", "#shot-inspector"):
                for width, verdict in reach[selector].items():
                    assert not verdict.startswith("not displayed"), (
                        f"{selector} is hidden by a media query at {width}px, taking the only "
                        f"controls of their kind with it: {reach[selector]}"
                    )

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
