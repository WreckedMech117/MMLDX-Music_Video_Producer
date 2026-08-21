"""Browser QA for "Fill looks from Treatment", and for review Finding 3 (2026-08-21).

The finding: the section inspector's button asked the route for a report and then bailed with
`if (!report.filled) return toast(report.message, "error")` **before** it ever asked the overwrite
question. A structure where every section already carries a look short-circuits server-side to
`0 filled` with `SECTION_LOOKS_ALL_WRITTEN` — *"Every section already has a look. Nothing was
changed — send overwrite=true to replace what is there."* — so for that project the button could
do nothing but error, while the sentence it showed described a consent the screen never offered.
The Director's live project is in exactly that state: all seven sections written.

The same early return threw away the per-section skip reasons whenever the model filled nothing,
which the route's own docstring calls half the feature: *"the treatment does not describe this
section"* is the sentence that sends a Director back to the treatment, and swallowed, the box is
just mysteriously still blank.

**Both halves are interaction defects and neither is visible offline.** `tests/test_frontend_
contract.py` executes the deciding functions, and this project has a recorded incident where
substring assertions over `app.js` let three UI guarantees invert with a green suite. So every
assertion here drives a real browser and reads either the **stored manifest off this run's own
data root** or the text actually painted into the inspector.

What is asserted, in order:

1. **The control is reachable** in the section inspector — hit-tested, not merely found.
2. **All seven written: the overwrite question is asked**, in its own words, on a report that
   filled nothing. Declining it writes nothing and asks no model, and the report's per-section
   reasons and existing text stay painted in the panel rather than vanishing with the dialog.
3. **Accepting it re-reports with the consent and shows the diff before anything lands**: every
   section's proposed look beside the words it would replace. Only then does the confirm write,
   and the manifest afterwards carries the new looks — with the one section the pass had nothing
   to say about left exactly as the Director wrote it.
4. **Some written and some empty: one report answers both halves.** The preview comes first
   because something *would* be filled, the consent is the second question, and the confirm
   applies that same plan — one model call for the whole gesture.
5. **Nothing filled and nothing written: the reasons render anyway.** No consent question is
   asked (there is nothing to overwrite), nothing is written, and every section's own skip
   sentence is on screen.

**No GPU time and no model time is spent.** The language-model host is a stub HTTP server started
by this script — `MVP_LLM_BASE_URL` is pointed at it before the app starts, so the machine's real
LM Studio is never addressed and no model is loaded, prompted or unloaded. The stub also *counts*
its calls, which is how the run asserts the confirm asks no model at all.

Run from the repo root -- it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_section_looks.py [--port 8773]

Assumes: nothing listening on the port (it refuses to reuse a bound one), Microsoft Edge and its
WebDriver installed, and `music_video_producer` importable from this checkout's `src/`. ComfyUI
does not need to be running and no language-model host is needed.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

from e2e_support import (
    ManagedServer,
    artifact_dir,
    clear_toasts,
    console_gate,
    edge_driver,
    post_json,
    post_multipart,
    put_json,
    report,
    resource_hits,
    settle,
    visible_and_clickable,
    wait_for_toast,
)
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from music_video_producer.app import (
    SECTION_LOOK_SKIP_UNDESCRIBED,
    SECTION_LOOK_SKIP_WRITTEN,
    SECTION_LOOKS_ALL_WRITTEN,
)

NAME = "section-looks"

SONG_SECONDS = 140.0

#: The Director's own structure, in shape: seven boxes, labels that repeat by design, and a look
#: written into every one of them. `project_59f14d19ff10` is read for its shape and never touched.
SECTIONS = [
    {"id": "section_intro", "label": "Intro", "start": 0.0, "duration": 11.0},
    {"id": "section_verse", "label": "Verse", "start": 11.0, "duration": 24.0},
    {"id": "section_chorus", "label": "Chorus", "start": 35.0, "duration": 22.0},
    {"id": "section_verse2", "label": "Verse 2", "start": 57.0, "duration": 24.0},
    {"id": "section_chorus2", "label": "Chorus 2", "start": 81.0, "duration": 22.0},
    {"id": "section_bridge", "label": "Bridge", "start": 103.0, "duration": 21.0},
    {"id": "section_outro", "label": "Outro", "start": 124.0, "duration": 16.0},
]

#: What the Director wrote into each box by hand. These are the words the overwrite consent is
#: about, so the run asserts they are on screen before it is asked and gone only after it is given.
WRITTEN = {
    section["id"]: f"{section['label']}: the words I wrote myself, in the corridor."
    for section in SECTIONS
}

#: The sections the stub answers with an empty look, which is the route's honest empty: the model
#: was required to emit the key and said "the treatment does not say". Mutated in place by the
#: last scene, which needs every section answered that way -- a list rather than a rebound name so
#: the handler running on the stub's own thread reads the change.
UNDESCRIBED = "section_bridge"
SILENT_SECTIONS = [UNDESCRIBED]

TREATMENT = (
    "INTRO — the corridor, low and slow. VERSE — the chrome microphone, handheld. "
    "CHORUS — the canopy bed, wide. OUTRO — the door, closing on the light."
)
STYLE_BIBLE = (
    "Anamorphic 40mm, sodium practicals, wet asphalt, crimson and brass, wardrobe of "
    "black satin. Everything is lit from one side."
)


class StubLanguageModel(BaseHTTPRequestHandler):
    """One OpenAI-compatible `POST /v1/chat/completions`, answering the section-look schema.

    A stub, not a model: it reads the section list out of the user message that
    `timeline.section_looks_input` built and answers one look per section, which is exactly the
    contract `director.section_looks` validates. Nothing is generated and nothing is loaded, so
    this run costs no VRAM and cannot disturb a render on the card.

    `calls` is the point of it being a real server rather than a monkeypatch: the guarantee that a
    *confirm* asks no model is a property of what left the machine, and this counts what did.
    """

    calls: ClassVar[list[dict]] = []
    lock = threading.Lock()

    def do_POST(self) -> None:  # BaseHTTPRequestHandler's own spelling
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        asked = json.loads(body["messages"][-1]["content"])
        with StubLanguageModel.lock:
            StubLanguageModel.calls.append(asked)
        looks = [
            {
                "section_id": section["section_id"],
                "label": section["label"],
                # The honest empty for the silent sections, a real look for every other.
                "prompt": (
                    ""
                    if section["section_id"] in SILENT_SECTIONS
                    else f"{section['label']}: read out of the treatment, sodium light, wet street."
                ),
            }
            for section in asked["sections"]
        ]
        self._answer(
            {"choices": [{"message": {"content": json.dumps({"looks": looks})}}]}
        )

    def do_GET(self) -> None:  # BaseHTTPRequestHandler's own spelling
        """`/models`, so the app's health probe gets an answer instead of a 501 in the log."""
        self._answer({"data": [{"id": "browser-qa-stub"}]})

    def _answer(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        """Silent. Its output would interleave with the run's own report."""


#: Every request this page made to the fill-looks route, with the body it sent. Read out of the
#: page rather than a server log because what has to be proven is what the *client* chose to send
#: -- specifically that `overwrite: true` reaches the wire only after the Director says so.
RECORD_FILL_LOOKS = """
window.__mvpFillLooks = [];
if (!window.__mvpRealFetch) {
  window.__mvpRealFetch = window.fetch;
  window.fetch = function (input, init) {
    const url = String((input && input.url) || input || '');
    if (url.indexOf('/sections/fill-looks') >= 0) {
      window.__mvpFillLooks.push(String((init && init.body) || ''));
    }
    return window.__mvpRealFetch(input, init);
  };
}
return true;
"""

#: Every line of the section-look report as it is painted in the inspector, with the kind the
#: stylesheet draws it in. A source read of `renderShotInspector` would pass just as happily if
#: nothing ever called it.
REPORT_LINES = """
return [...document.querySelectorAll('#shot-inspector .section-looks-report div')]
  .map((node) => node.className + '|' + node.textContent);
"""


def synthesize_song(target: Path, seconds: float = SONG_SECONDS) -> None:
    with wave.open(str(target), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(b"\x00\x00" * int(seconds * 8000))


def seed(base_url: str, *, written: bool) -> str:
    project = post_json(base_url + "/api/projects", {"name": "Section looks browser QA"})
    song = artifact_dir() / "section-looks-song.wav"
    synthesize_song(song)
    post_multipart(
        f"{base_url}/api/projects/{project['id']}/songs/upload",
        {"title": "Section looks QA song", "duration": str(SONG_SECONDS)},
        ("file", song),
    )
    put_json(
        f"{base_url}/api/projects/{project['id']}/documents",
        {"creative_brief": "", "treatment": TREATMENT, "style_bible": STYLE_BIBLE},
    )
    write_sections(base_url, project["id"], written=written)
    return project["id"]


def write_sections(base_url: str, project_id: str, *, written: bool) -> None:
    put_json(
        f"{base_url}/api/projects/{project_id}/sections",
        {"sections": [
            {**section, "prompt": WRITTEN[section["id"]] if written else ""}
            for section in SECTIONS
        ]},
    )


def manifest(server: ManagedServer, project_id: str) -> dict:
    path = server.data_root / "projects" / project_id / "project.json"
    return json.loads(path.read_text(encoding="utf-8"))


def looks(server: ManagedServer, project_id: str) -> dict[str, str]:
    """Every section's shared prompt, as it is on disk. The only place a write can be proven."""
    return {
        section["id"]: section["prompt"]
        for section in manifest(server, project_id)["sections"]
    }


def select_project(driver, wait, project_id: str) -> None:
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


def stable_click(driver, selector: str, what: str, tries: int = 10) -> None:
    """Click a control, re-finding it if the workspace redraws underneath the click.

    Not a retry hiding a race -- it is the race, and it is the application's design. `loadProject`
    fires the readiness GET without awaiting it and the reply calls `renderTimeline`, which
    rebuilds the SECTIONS track and the inspector wholesale; an element found a moment earlier is
    gone by the time the driver points at it. `settle` narrows the window and this closes it.
    """
    last: Exception | None = None
    for _ in range(tries):
        try:
            driver.find_element(By.CSS_SELECTOR, selector).click()
            return
        except (StaleElementReferenceException, NoSuchElementException) as error:
            last = error
            time.sleep(0.25)
    raise AssertionError(f"{what} could not be clicked: {last}")


def open_section(driver, wait, section_id: str) -> None:
    """Select a section box and wait for its inspector. A real click on the pill, which is the
    only gesture that opens this panel -- the Director's own design."""
    selector = f'#section-track .section-pill[data-section-id="{section_id}"]'
    wait.until(lambda browser: browser.find_elements(By.CSS_SELECTOR, selector))
    settle(driver, "#section-track")
    stable_click(driver, selector, f"the {section_id} section box")
    wait.until(lambda browser: browser.find_elements(By.ID, "section-fill-looks"))
    settle(driver, "#shot-inspector")


def fill_looks_requests(driver) -> list[dict]:
    return [json.loads(body or "{}") for body in
            driver.execute_script("return window.__mvpFillLooks || [];")]


def report_lines(driver) -> list[str]:
    return [str(line) for line in driver.execute_script(REPORT_LINES)]


def press_fill_looks(driver) -> None:
    button = driver.find_element(By.ID, "section-fill-looks")
    assert not button.get_property("disabled"), "the fill-looks button is shut"
    stable_click(driver, "#section-fill-looks", "Fill looks from Treatment")


def section_line(lines: list[str], section: dict) -> str:
    """The report line for one section, matched on its start *and* label.

    Labels repeat by design -- "Verse"/"Verse 2", "Chorus"/"Chorus 2" -- so a match on the label
    alone would read Verse 2's line as Verse's and pass whatever the panel drew.
    """
    prefix = f"{section['start']:.1f}s {section['label']}"
    found = [row for row in lines if prefix in row]
    assert len(found) == 1, (f"no single line for {prefix}", lines)
    return found[0]


def answer_dialog(wait, accept: bool, what: str = "a dialog") -> str:
    """Answer the next browser dialog and say what it asked.

    A missing dialog is reported as "it was never asked" rather than as a bare Selenium timeout:
    the defect this run exists for is a question the screen could not ask, so that is the sentence
    a failure has to print.
    """
    try:
        alert = wait.until(EC.alert_is_present())
    except TimeoutException:
        raise AssertionError(f"{what} was never asked") from None
    text = alert.text
    if accept:
        alert.accept()
    else:
        alert.dismiss()
    return text


def no_dialog(driver, seconds: float = 3.0) -> bool:
    """True when no dialog appears within `seconds`. Asserting a negative, so it waits."""
    try:
        WebDriverWait(driver, seconds).until(EC.alert_is_present())
    except TimeoutException:
        return True
    return False


def main() -> None:
    port = 8773
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    stub = ThreadingHTTPServer(("127.0.0.1", 0), StubLanguageModel)
    thread = threading.Thread(target=stub.serve_forever, daemon=True)
    thread.start()
    stub_url = f"http://127.0.0.1:{stub.server_address[1]}/v1"
    # Set before the app starts, and it wins: pydantic-settings ranks environment variables above
    # `.env`, where this machine's real LM Studio is configured. Nothing in this run can reach a
    # model, which matters today -- a live render batch owns the card.
    os.environ["MVP_LLM_BASE_URL"] = stub_url
    os.environ["MVP_LLM_MODEL"] = "browser-qa-stub"

    result: dict[str, object] = {"stub_url": stub_url}
    try:
        with ManagedServer(port, label=NAME) as server:
            result["server_identity"] = server.evidence
            project_id = seed(server.base_url, written=True)
            result["seeded_looks"] = looks(server, project_id)
            assert all(result["seeded_looks"].values()), (
                "the seed did not write every section, so the all-written path is not reachable"
            )
            driver = edge_driver()
            wait = WebDriverWait(driver, 25)
            try:
                driver.get(server.base_url)
                select_project(driver, wait, project_id)
                driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
                driver.execute_script(RECORD_FILL_LOOKS)
                open_section(driver, wait, "section_outro")

                # --- 1. The control is reachable -----------------------------------------
                button = driver.find_element(By.ID, "section-fill-looks")
                result["control"] = visible_and_clickable(
                    driver, button, "Fill looks from Treatment"
                )
                assert button.text.strip() == "Fill looks from Treatment", button.text
                assert "never replaced without you saying so" in (
                    button.get_attribute("title") or ""
                ), button.get_attribute("title")

                # --- 2. Every section written: the consent is asked, and declined ---------
                #
                # The path the Director is on right now. Before Finding 3 was fixed this
                # produced one red toast and stopped: the report says `0 filled`, and the
                # handler treated that as an error.
                clear_toasts(driver)
                before = looks(server, project_id)
                press_fill_looks(driver)
                asked = answer_dialog(
                    wait, accept=False,
                    what="the overwrite consent, on a structure where every section is written",
                )
                settle(driver, "#shot-inspector")
                assert asked == "Also replace the section looks you wrote yourself?", (
                    "the overwrite consent was not the question asked", asked
                )
                declined_requests = fill_looks_requests(driver)
                assert len(declined_requests) == 1, declined_requests
                assert declined_requests[0]["overwrite"] is False, declined_requests[0]
                assert declined_requests[0]["confirm_apply"] is False, declined_requests[0]
                assert not StubLanguageModel.calls, (
                    "a report over a fully written structure asked the model, which is a "
                    "300 s call to arrive at an answer already known"
                )
                assert looks(server, project_id) == before, (
                    "declining the overwrite consent wrote something anyway"
                )
                # The reasons, painted where the button is rather than lost with the dialog.
                declined_lines = report_lines(driver)
                assert len(declined_lines) == len(SECTIONS), declined_lines
                for section in SECTIONS:
                    line = section_line(declined_lines, section)
                    assert SECTION_LOOK_SKIP_WRITTEN in line, line
                    assert WRITTEN[section["id"]] in line, (
                        ("the look the consent would replace is not on screen beside "
                         "the question about replacing it"), line,
                    )
                message = wait_for_toast(driver, wait, "Every section already has a look")
                assert message == SECTION_LOOKS_ALL_WRITTEN, message
                result["consent_declined"] = {
                    "question": asked,
                    "requests": declined_requests,
                    "model_calls": len(StubLanguageModel.calls),
                    "lines": declined_lines,
                    "message": message,
                }

                # --- 3. Accepting it shows the diff, then writes --------------------------
                clear_toasts(driver)
                press_fill_looks(driver)
                consent = answer_dialog(wait, accept=True, what="the overwrite consent")
                assert consent == asked, consent
                # The second question is the report itself: what would land, beside what it
                # would replace. This is the one that must never become a single blind click.
                preview = answer_dialog(
                    wait, accept=True, what="the preview of what would be written"
                )
                for section in SECTIONS:
                    assert section["label"] in preview, (section["label"], preview)
                assert "replaces what you wrote" in preview, preview
                for existing in WRITTEN.values():
                    assert existing in preview, (
                        "the preview did not show the words it would replace", existing
                    )
                assert SECTION_LOOK_SKIP_UNDESCRIBED in preview, (
                    "the section the pass had nothing to say about was not named", preview
                )
                assert preview.rstrip().endswith("Write these looks?"), preview
                written_toast = wait_for_toast(driver, wait, "section look(s) written")
                settle(driver, "#shot-inspector")
                after = looks(server, project_id)
                assert after[UNDESCRIBED] == WRITTEN[UNDESCRIBED], (
                    "a section the pass had no look for was overwritten anyway", after
                )
                for section in SECTIONS:
                    if section["id"] == UNDESCRIBED:
                        continue
                    assert after[section["id"]] != WRITTEN[section["id"]], (
                        "the consent was given and nothing was replaced", section["id"], after
                    )
                    assert "read out of the treatment" in after[section["id"]], after
                applied_requests = fill_looks_requests(driver)
                # Four in all: the declined scene's report, this scene's report, the re-report
                # the consent bought, and the confirm that wrote.
                assert len(applied_requests) == 4, applied_requests
                assert applied_requests[1] == {
                    "confirm_apply": False, "overwrite": False, "plan": None
                }, applied_requests[1]
                assert applied_requests[2] == {
                    "confirm_apply": False, "overwrite": True, "plan": None
                }, ("the consent did not reach the wire as a fresh report", applied_requests[2])
                assert applied_requests[3]["confirm_apply"] is True, applied_requests[3]
                assert applied_requests[3]["overwrite"] is True, applied_requests[3]
                assert applied_requests[3]["plan"], (
                    ("the confirm carried no plan, so the looks that landed are not "
                     "provably the looks that were read"), applied_requests[3],
                )
                # The confirm asked no model: one call for the whole scene, the re-report.
                assert len(StubLanguageModel.calls) == 1, (
                    "the confirming call read the treatment again",
                    len(StubLanguageModel.calls),
                )
                applied_lines = report_lines(driver)
                assert any(SECTION_LOOK_SKIP_UNDESCRIBED in row for row in applied_lines), (
                    applied_lines
                )
                result["consent_given"] = {
                    "preview": preview,
                    "toast": written_toast,
                    "requests": applied_requests,
                    "model_calls": len(StubLanguageModel.calls),
                    "looks_after": after,
                    "lines": applied_lines,
                }

                # --- 3b. Some written, some empty: one report answers both halves ---------
                #
                # The mixed structure, and the *other* place the consent is asked. Here the
                # report fills something, so it is previewed first and the consent is the second
                # question -- and the route deliberately carries the proposal for a written
                # section on its row, so saying yes applies that same plan rather than buying a
                # second reading of the treatment. One model call for the whole scene.
                mixed = {
                    section["id"]: WRITTEN[section["id"]] if index < 3 else ""
                    for index, section in enumerate(SECTIONS)
                }
                put_json(
                    f"{server.base_url}/api/projects/{project_id}/sections",
                    {"sections": [
                        {**section, "prompt": mixed[section["id"]]} for section in SECTIONS
                    ]},
                )
                driver.refresh()
                select_project(driver, wait, project_id)
                driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
                driver.execute_script(RECORD_FILL_LOOKS)
                open_section(driver, wait, "section_outro")
                clear_toasts(driver)
                StubLanguageModel.calls.clear()
                press_fill_looks(driver)
                mixed_preview = answer_dialog(
                    wait, accept=True, what="the preview of what would be written"
                )
                assert SECTION_LOOK_SKIP_WRITTEN in mixed_preview, mixed_preview
                mixed_consent = answer_dialog(
                    wait, accept=True, what="the overwrite consent, on a mixed structure"
                )
                assert mixed_consent == asked, mixed_consent
                wait_for_toast(driver, wait, "section look(s) written")
                settle(driver, "#shot-inspector")
                mixed_after = looks(server, project_id)
                mixed_requests = fill_looks_requests(driver)
                assert len(mixed_requests) == 2, (
                    "a mixed structure bought a second reading of the treatment", mixed_requests
                )
                assert len(StubLanguageModel.calls) == 1, len(StubLanguageModel.calls)
                for index, section in enumerate(SECTIONS):
                    if section["id"] == UNDESCRIBED:
                        assert mixed_after[section["id"]] == mixed[section["id"]], mixed_after
                        continue
                    assert "read out of the treatment" in mixed_after[section["id"]], (
                        ("a section was left alone on a structure where the consent was "
                         "given"), index, section["id"], mixed_after,
                    )
                result["mixed_structure"] = {
                    "before": mixed,
                    "preview": mixed_preview,
                    "consent": mixed_consent,
                    "requests": mixed_requests,
                    "model_calls": len(StubLanguageModel.calls),
                    "looks_after": mixed_after,
                }

                # --- 4. Nothing filled and nothing written: the reasons still render ------
                #
                # The other half of Finding 3. Every section empty and the pass with nothing to
                # say about any of them: `0 filled` again, but this time there is genuinely
                # nothing to consent to -- so no question is asked, nothing is written, and the
                # per-section sentences are the entire value of the run.
                SILENT_SECTIONS[:] = [section["id"] for section in SECTIONS]
                write_sections(server.base_url, project_id, written=False)
                driver.refresh()
                select_project(driver, wait, project_id)
                driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
                driver.execute_script(RECORD_FILL_LOOKS)
                open_section(driver, wait, "section_outro")
                clear_toasts(driver)
                empty_before = looks(server, project_id)
                assert not any(empty_before.values()), empty_before
                press_fill_looks(driver)
                assert no_dialog(driver), (
                    "a consent was asked over a structure with nothing written in it"
                )
                undescribed_toast = wait_for_toast(driver, wait, "left alone")
                settle(driver, "#shot-inspector")
                empty_lines = report_lines(driver)
                assert len(empty_lines) == len(SECTIONS), empty_lines
                for section in SECTIONS:
                    line = section_line(empty_lines, section)
                    assert SECTION_LOOK_SKIP_UNDESCRIBED in line, (
                        ("the section's own skip reason was swallowed, which is the "
                         "half of this feature that sends a Director back to the treatment"),
                        line,
                    )
                assert looks(server, project_id) == empty_before, (
                    "a report that filled nothing wrote something"
                )
                assert resource_hits(driver, "/sections/fill-looks") == 1, (
                    "a second request went out after a report that had nothing to apply"
                )
                result["nothing_to_fill"] = {
                    "toast": undescribed_toast,
                    "lines": empty_lines,
                    "looks_after": looks(server, project_id),
                }

                # The dismissal, so the report is not a thing the Director cannot put away.
                driver.find_element(By.ID, "section-looks-dismiss").click()
                settle(driver, "#shot-inspector")
                assert not report_lines(driver), report_lines(driver)

                result["model_calls_total"] = len(StubLanguageModel.calls)
                console_gate(driver, NAME, result)
                report(NAME, result)
            finally:
                driver.quit()
    finally:
        stub.shutdown()
        stub.server_close()


if __name__ == "__main__":
    main()
