"""Browser QA for the delete confirmation's number (fix of 2026-08-26).

The defect: `Delete SHOT 05? Its rendered takes stay on disk, but the shot leaves the plan.`
raised over a clip the timeline draws as `SHOT 02`, because `shotLabel` counted manifest
positions while `renderTimeline` counted song positions. A destructive action naming the wrong
clip is the whole finding; `api.songOrderRanks` is the one ranking both now read.

**A contract test cannot see this defect unless its fixture can express it.** The one that
covered `shotLabel` had no `start` field at all, so manifest order and song order coincided and
it passed under both the broken and the fixed rule. So this script does not fixture the
divergence by hand either -- it builds it with a shipped control. `#split-shot` pushes the
second half onto the **end** of the manifest while its `start` sits mid-song, which is the
mid-timeline insert that makes the two orderings differ, and it is the gesture a Director makes
by accident ("I hit split on the wrong clip").

What is proved, in one project:

1. Four shots at 0/5/10/15. Splitting the first makes a fifth Shot that is **last in the
   manifest and second in the song** -- read back off the stored manifest, so the divergence is
   a measured fact before anything is asserted about it.
2. The clip on screen reads `SHOT 02`, read off the live DOM rather than assumed.
3. Clicking that clip and pressing the bin raises the **browser's own dialog**, and its sentence
   names `SHOT 02 (<that shot's id>)` -- the clip that was clicked. Under the old rule it would
   have read SHOT 05, and the assertion says so by name.
4. Answering *no* deletes nothing; answering *yes* deletes exactly that shot, both read back off
   the stored manifest.
5. The **server** agrees. The fourth seeded shot has a blank prompt, so `/readiness` names it;
   with the divergence rebuilt it is manifest position 4 and song position 5, and the report
   says SHOT 05 -- which is the half of this fix that reaches the model's own report too.

The confirmation is driven as a real dialog, the way `e2e_timeline_edit.py` and
`e2e_song_context.py` drive theirs: `EC.alert_is_present()`, `alert.text`, then `dismiss()` and
`accept()`. A stub would prove the call was made and nothing about whether a Director is ever
shown it. The sentence Selenium reads back is then painted into the page for the screenshot,
because a native `window.confirm` is browser chrome and never appears in a viewport capture --
the alternative artifact is a frame with no dialog in it at all. The painted text is the string
the browser really carried, unedited.

**No GPU time and no model time.** ComfyUI is pointed at a dead port and never contacted.

Run from the repo root -- it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_shot_numbering.py [--port 8780]

Assumes: nothing listening on the port, Microsoft Edge and its WebDriver installed, and
`music_video_producer` importable from this checkout's `src/`. ComfyUI does not need to be
running.
"""

from __future__ import annotations

import math
import os
import socket
import struct
import sys
import tempfile
import time
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
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

NAME = "shot-numbering"

#: The four seeded shots, contiguous, in both orders at once -- which is the state this script
#: then breaks on purpose. The last carries a blank prompt so the readiness report has to name it.
SEEDED = [
    {"id": "shot_aaaa00000001", "start": 0.0, "duration": 5.0, "prompt": "A corridor, amber"},
    {"id": "shot_bbbb00000002", "start": 5.0, "duration": 5.0, "prompt": "A rooftop, cold"},
    {"id": "shot_cccc00000003", "start": 10.0, "duration": 5.0, "prompt": "A stairwell, dim"},
    {"id": "shot_dddd00000004", "start": 15.0, "duration": 5.0, "prompt": "   "},
]


def dead_port() -> int:
    """A port nothing is listening on, so the ComfyUI reads fail fast and change nothing."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def write_wav(path: Path, seconds: float = 25.0) -> None:
    rate = 22050
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        frames = bytearray()
        for index in range(int(rate * seconds)):
            frames.extend(struct.pack("<h", int(6000 * math.sin(2 * math.pi * 220 * index / rate))))
        target.writeframes(frames)


def seed(base_url: str) -> tuple[str, list[dict]]:
    """A song and four contiguous shots, built through shipped routes."""
    project = post_json(f"{base_url}/api/projects", {"name": "Shot numbering browser QA"})
    audio = artifact_dir() / f"{NAME}-fixture.wav"
    write_wav(audio)
    post_multipart(
        f"{base_url}/api/projects/{project['id']}/songs/upload",
        {"title": "Corridor (master)", "duration": "25.0"},
        ("file", audio),
    )
    saved = put_json(f"{base_url}/api/projects/{project['id']}/shots", {"shots": SEEDED})
    return project["id"], saved["shots"]


def stored_shots(base_url: str, project_id: str) -> list[dict]:
    return get_json(f"{base_url}/api/projects/{project_id}")["shots"]


def stored_ids(base_url: str, project_id: str, count: int, what: str,
               timeout: float = 15.0) -> list[str]:
    """The stored manifest once it holds `count` shots, or a failure that says it never did.

    `saveShotsSilently` is fire-and-forget: the timeline redraws off local state the instant a
    gesture lands, so the browser showing five clips is not the server holding five shots. Read
    once and this is a race that fails perhaps one run in three -- which is worse than a gate
    that fails always, because it teaches people to re-run it.
    """
    deadline = time.monotonic() + timeout
    ids: list[str] = []
    while time.monotonic() < deadline:
        ids = [shot["id"] for shot in stored_shots(base_url, project_id)]
        if len(ids) == count:
            return ids
        time.sleep(0.2)
    raise AssertionError(f"{what}: the manifest holds {len(ids)} shots, not {count}: {ids}")


def clip_numbers(driver) -> dict[str, str]:
    """Every clip's `data-shot-id` and the number printed on it, off the live DOM."""
    return driver.execute_script(
        """
        const out = {};
        document.querySelectorAll('#shots-track .shot-clip').forEach((clip) => {
          out[clip.dataset.shotId] =
            clip.querySelector('.clip-id').textContent.split('\\u00b7')[0].trim();
        });
        return out;
        """
    )


def raise_delete(driver, wait):
    """Press the bin and hand back the browser's own dialog, unanswered."""
    driver.find_element(By.ID, "delete-shot").click()
    return wait.until(
        EC.alert_is_present(),
        "pressing delete raised no dialog; the confirmation is either not reaching the browser "
        "or is being auto-dismissed",
    )


def paint_dialog(driver, message: str) -> None:
    """Draw the sentence Selenium read out of the real dialog over the page.

    A native confirm is browser chrome and `save_screenshot` captures the viewport only, so the
    number in the dialog and the number on the clip cannot otherwise be seen in one frame. The
    text below is the string `alert.text` returned, unmodified -- this paints evidence already
    taken, it does not stand in for taking it.
    """
    driver.execute_script(
        """
        const old = document.getElementById('__mvp_qa_dialog');
        if (old) old.remove();
        const box = document.createElement('div');
        box.id = '__mvp_qa_dialog';
        box.style.cssText = 'position:fixed;left:50%;top:96px;transform:translateX(-50%);' +
          'z-index:99999;background:#fbfbfd;color:#111;border:1px solid #8a8a8a;' +
          'border-radius:8px;box-shadow:0 8px 32px rgba(0,0,0,.55);padding:14px 18px;' +
          'font:14px/1.45 system-ui,sans-serif;max-width:760px;';
        box.innerHTML = '<div style="font-weight:600;margin-bottom:6px"></div>' +
          '<div id="__mvp_qa_text"></div>' +
          '<div style="margin-top:12px;text-align:right">' +
          '<button style="margin-right:8px">Cancel</button><button>OK</button></div>';
        document.body.appendChild(box);
        box.firstChild.textContent = location.host + ' says';
        document.getElementById('__mvp_qa_text').textContent = arguments[0];
        """,
        message,
    )


def main() -> None:
    port = 8780
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    result: dict[str, object] = {}
    os.environ["MVP_COMFY_ROOT"] = str(Path(tempfile.mkdtemp(prefix="mvp-shotnum-comfy-")))
    os.environ["MVP_COMFY_URL"] = f"http://127.0.0.1:{dead_port()}"
    os.environ["MVP_LLM_EJECT_BEFORE_RENDER"] = "false"
    os.environ["MVP_LLM_BASE_URL"] = ""

    with ManagedServer(port, label=NAME) as server:
        result["server_identity"] = server.evidence
        result["data_root"] = str(server.data_root)
        project_id, seeded = seed(server.base_url)
        result["seeded"] = [(shot["id"], shot["start"]) for shot in seeded]

        driver = edge_driver()
        wait = WebDriverWait(driver, 25)
        try:
            driver.get(server.base_url)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
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
            wait.until(lambda browser: len(clip_numbers(browser)) == 4, "the four clips never drew")

            # --- 1. Before: manifest order is song order, so nothing can diverge -------------
            #
            # Asserted rather than assumed, because this is exactly the state the old contract
            # fixture never left -- and in it the broken rule and the fixed one agree.
            before = clip_numbers(driver)
            assert before == {
                "shot_aaaa00000001": "SHOT 01",
                "shot_bbbb00000002": "SHOT 02",
                "shot_cccc00000003": "SHOT 03",
                "shot_dddd00000004": "SHOT 04",
            }, before
            result["clips_before_split"] = before

            # --- 2. Split the first clip: the new half lands mid-song, last in the manifest --
            driver.find_element(
                By.CSS_SELECTOR, '#shots-track .shot-clip[data-shot-id="shot_aaaa00000001"]'
            ).click()
            driver.find_element(By.ID, "split-shot").click()
            wait.until(lambda browser: len(clip_numbers(browser)) == 5, "the split never landed")

            manifest = stored_ids(
                server.base_url, project_id, 5, "the split never reached the server")
            inserted = manifest[-1]
            assert inserted not in before, manifest
            result["manifest_after_split"] = manifest
            result["inserted_shot"] = inserted
            result["inserted_manifest_position"] = len(manifest)

            after = clip_numbers(driver)
            result["clips_after_split"] = after
            # The clip on screen. Manifest position 5, song position 2 -- the divergence, and
            # the thing the old fixture could not hold.
            assert after[inserted] == "SHOT 02", after
            assert result["inserted_manifest_position"] == 5, result
            # And every other clip renumbered by the song, not by the manifest.
            assert after == {
                "shot_aaaa00000001": "SHOT 01",
                inserted: "SHOT 02",
                "shot_bbbb00000002": "SHOT 03",
                "shot_cccc00000003": "SHOT 04",
                "shot_dddd00000004": "SHOT 05",
            }, after

            # --- 3. The delete confirmation names the clip that was clicked -----------------
            driver.find_element(
                By.CSS_SELECTOR, f'#shots-track .shot-clip[data-shot-id="{inserted}"]'
            ).click()
            wait.until(
                lambda browser: "selected"
                in browser.find_element(
                    By.CSS_SELECTOR, f'.shot-clip[data-shot-id="{inserted}"]'
                ).get_attribute("class"),
                "the inserted clip never became the selection",
            )
            alert = raise_delete(driver, wait)
            message = alert.text
            result["confirm_message"] = message
            expected = (
                f"Delete SHOT 02 ({inserted})? "
                "Its rendered takes stay on disk, but the shot leaves the plan."
            )
            assert message == expected, (message, expected)
            # What the old rule would have said about this same clip, spelled out so a
            # regression reads as the sentence a Director would have been shown.
            result["would_have_said_under_manifest_numbering"] = (
                f"Delete SHOT 0{len(manifest)} ({inserted})? "
                "Its rendered takes stay on disk, but the shot leaves the plan."
            )
            assert "SHOT 05" not in message, message

            # --- 4. Answering no deletes nothing --------------------------------------------
            alert.dismiss()
            assert stored_ids(
                server.base_url, project_id, 5,
                "answering no to the delete question changed the manifest") == manifest, (
                "answering no to the delete question removed a shot anyway"
            )
            assert clip_numbers(driver) == after, "answering no renumbered the timeline"

            # --- 5. The screenshot: the sentence and the clips in one frame ------------------
            paint_dialog(driver, message)
            shot_path = artifact_dir() / f"{NAME}-delete-confirm-names-the-clip.png"
            driver.save_screenshot(str(shot_path))
            result["screenshot"] = str(shot_path)
            driver.execute_script("document.getElementById('__mvp_qa_dialog').remove();")

            # --- 6. Answering yes deletes exactly that shot ----------------------------------
            second = raise_delete(driver, wait)
            assert second.text == message, (second.text, message)
            second.accept()
            wait.until(lambda browser: len(clip_numbers(browser)) == 4, "the delete never landed")
            left = stored_ids(
                server.base_url, project_id, 4, "the delete never reached the server")
            assert inserted not in left, left
            assert left == [m for m in manifest if m != inserted], left
            result["manifest_after_delete"] = left

            console_gate(driver, NAME, result)
        finally:
            driver.quit()

        # --- 7. The server names the same Shot the same way -----------------------------
        # Rebuilt so the divergence is live again for the server's turn: the delete above put
        # the manifest back in song order, and in song order nothing can diverge.
        put_json(
            f"{server.base_url}/api/projects/{project_id}/shots",
            {
                "shots": [
                    {"id": "shot_aaaa00000001", "start": 0.0, "duration": 2.5,
                     "prompt": "A corridor, amber"},
                    {"id": "shot_bbbb00000002", "start": 5.0, "duration": 5.0,
                     "prompt": "A rooftop, cold"},
                    {"id": "shot_cccc00000003", "start": 10.0, "duration": 5.0,
                     "prompt": "A stairwell, dim"},
                    {"id": "shot_dddd00000004", "start": 15.0, "duration": 5.0, "prompt": "   "},
                    {"id": "shot_eeee00000005", "start": 2.5, "duration": 2.5,
                     "prompt": "A corridor, amber"},
                ]
            },
        )
        readiness = get_json(f"{server.base_url}/api/projects/{project_id}/readiness")
        labels = [label for note in readiness["blocking"] for label in note["labels"]]
        result["readiness_labels"] = labels
        # `shot_dddd00000004` is manifest position 4 and song position 5. The clip says SHOT 05,
        # and so does the server -- which is the half of this fix that reaches the model too.
        assert labels == ["SHOT 05 (shot_dddd00000004)"], readiness["blocking"]

    result["ok"] = True
    report(NAME, result)


if __name__ == "__main__":
    main()
