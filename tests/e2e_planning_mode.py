"""Browser QA for Planning Mode, the mode the browser holds (Epic 14, Slice D1).

Story 14.2, delivering TP-6 and binding AD-35. Everything that can be proven without a browser
already is: `planningBar`, `planningEnterNotice`, `planningConsentControl` and `composerRoute` are
executed under node by `tests/test_frontend_contract.py`, the enter/exit controls and the composer's
submit handler are *driven* there against a stub DOM, and the safety property itself -- a request
without consent refused after one that carried it, on the same project in the same process -- is
asserted through the real route by `tests/test_planning.py`.

**What none of that can reach is whether the suspended control looks suspended.** A checkbox that is
`disabled` and otherwise unchanged still looks exactly like the thing that decides: the Director
ticks it, nothing happens, and the next write gets blamed on a mode that was doing precisely what it
said. `.lock-toggle.superseded` is a stylesheet rule, and a stylesheet rule that does not apply --
a specificity loss, a rule the class never reaches, a `text-decoration` that inherits away -- is
invisible to every offline gate in this repository. Fourteen defects across four epics passed every
automated gate and were caught only by looking; this is that shape of thing.

So this script asserts the **paint**: the bar's measured box and its position above the document
tabs, its dot resolved to `--amber` at a real 9x9, the resolved `text-decoration-line` on the
superseded label, the superseded micro-label's own measured box, and that the bar is genuinely gone
-- `display: none`, zero height -- with the mode off. It also drives the two things only a real
browser answers about state: that a **reload** leaves the mode off, and that the thread carries a
planning reply's notices in the same bubbles every other reply uses (AD-43).

**A recording double, never a live model.** `MVP_LLM_BASE_URL` is pointed at a stub HTTP server this
script owns, which answers `/v1/chat/completions` with a `write_brief` tool call it is told to make.
ComfyUI is pointed at a dead port, nothing is queued, and `/prompt` is never reached.

Run from the repo root -- it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_planning_mode.py [--port 8791]

Assumes: nothing listening on the port, Microsoft Edge and its WebDriver installed, and
`music_video_producer` importable from this checkout's `src/`. ComfyUI does not need to be running
and no language-model host is needed -- this script provides its own.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    visible_and_clickable,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Read from the server rather than retyped, for `e2e_suggest_video_pass.py`'s reason: a harness
# outside `uv run pytest` that transcribes a Director-facing sentence is a copy no gate watches.
from music_video_producer.app import (
    DOCUMENT_LABELS,
    DOCUMENT_LOCK_NOTICE,
    PLANNING_WITHOUT_CONSENT_NOTICE,
)
from music_video_producer.director import WRITE_BRIEF_TOOL

NAME = "planning-mode"

#: This script causes no failed request on purpose. Declared empty rather than omitted, so a
#: console entry that does appear fails the gate by name.
EXPECTED_CONSOLE: list[str] = []

#: The palette, resolved as the browser renders it. `--amber` is *caution* in this palette and
#: planning mode is a write happening without per-turn consent, which is honestly one (DESIGN §1).
PALETTE_TOKENS = ("--amber", "--dim", "--muted")

BRIEF_TYPED = (
    "A night drive that opens into wilderness. One driver, no dialogue, and the car is the second "
    "character. Constraint: everything is practical light."
)

#: What the double writes when a turn is allowed to write. Deliberately long and different, because
#: `document_rejection` refuses a much shorter candidate before consent is ever consulted -- a
#: refusal this script would then misread as the consent gate working.
BRIEF_WRITTEN = (
    "A night drive that opens into wilderness, told from the passenger seat: she is not the "
    "driver, her brother is, and they have not spoken since the funeral. One car, two people, no "
    "dialogue, and everything is practical light -- sodium, headlamps, and then nothing at all."
)

BRIEF_SECOND = (
    "A night drive that opens into wilderness, told from the passenger seat and ending before "
    "dawn: she is not the driver, her brother is, and the road runs out before either of them "
    "says anything. One car, two people, and everything is practical light throughout."
)

PROSE = "Here is the revision you asked for."


def free_port() -> int:
    """A port the OS just handed out, for the double. Never a fixed one: the double has no runbook
    line to track a collision in, unlike the application's own port."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class RecordedPlanner:
    """A stand-in for the local model on a port this script owns. **Never LM Studio.**

    It answers `POST /v1/chat/completions` and nothing else, and it answers **the shape the caller
    asked for**: a planning turn offers `tools`, and a Director reply carries a
    `response_format: json_schema` for `DirectorResult`. Branching on the request rather than on a
    mode this script sets is what makes section 6 honest -- the browser decides which route it
    used, and the double answers whatever arrived, so a client that sent the wrong one produces a
    502 rather than a plausible-looking success.

    Every planning turn it answers is one the route *would* have written from, so what the
    refusals below prove is that a guard refused -- not that the model proposed nothing.
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.brief = BRIEF_WRITTEN
        self.calls = 0
        self.planning_calls = 0
        self.chat_calls = 0
        self.server: ThreadingHTTPServer | None = None

    def payload(self, request: dict) -> dict:
        if "tools" not in request:
            # A Director reply, answered as its schema requires and echoing both documents it may
            # replace -- an echo is not a replacement, so this turn proposes nothing and the
            # Brief, which no reply can carry at all, is untouched either way.
            self.chat_calls += 1
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {"message": PROSE, "treatment": "", "style_bible": ""}
                            ),
                        }
                    }
                ]
            }
        self.planning_calls += 1
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": PROSE,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": WRITE_BRIEF_TOOL,
                                    "arguments": json.dumps({"creative_brief": self.brief}),
                                },
                            }
                        ],
                    }
                }
            ]
        }

    def start(self) -> None:
        double = self

        class Handler(BaseHTTPRequestHandler):
            # HTTP/1.0 for `e2e_suggest_video_pass.RecordedDirector`'s reason: one connection per
            # request, so stopping this double is visible to the pooled client immediately.
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args: object) -> None:
                return None

            def do_POST(self) -> None:  # BaseHTTPRequestHandler's spelling, not ours
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length)
                if not self.path.endswith("/chat/completions"):
                    self.send_error(404)
                    return
                double.calls += 1
                body = json.dumps(double.payload(json.loads(raw or b"{}"))).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


def seed(base_url: str, name: str) -> str:
    """A project at the stage Planning Mode is for: a Brief that exists and is being revised."""
    project = post_json(f"{base_url}/api/projects", {"name": name})
    put_json(
        f"{base_url}/api/projects/{project['id']}/documents",
        {"creative_brief": BRIEF_TYPED, "treatment": "", "style_bible": ""},
    )
    return project["id"]


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


def open_treatment(driver, wait) -> None:
    driver.find_element(By.CSS_SELECTOR, '[data-panel="treatment"]').click()
    wait.until(EC.visibility_of_element_located((By.ID, "creative-brief")))


def palette(driver) -> dict[str, str]:
    return driver.execute_script(
        "const style = getComputedStyle(document.documentElement);"
        "return Object.fromEntries(arguments[0].map((name) => ["
        "  name, style.getPropertyValue(name).trim()]));",
        list(PALETTE_TOKENS),
    )


def as_rgb(driver, colour: str) -> str:
    """One CSS colour as the browser resolves it, so a hex and an `rgb()` can be compared."""
    return driver.execute_script(
        "const probe = document.createElement('span');"
        "probe.style.color = arguments[0];"
        "document.body.append(probe);"
        "const resolved = getComputedStyle(probe).color;"
        "probe.remove();"
        "return resolved;",
        colour,
    )


def bar_paint(driver) -> dict:
    """What the Planning bar actually *is* on screen, measured rather than asserted.

    A structural check cannot tell a bar from a bar drawn at zero height inside a collapsed grid
    row, and both of this application's recorded "drawn as nothing" defects were exactly that. The
    tabs' own top is read alongside, because DESIGN §4.1 puts this strip *above* the documents and
    a bar that laid out under them would pass every assertion about its own box.
    """
    return driver.execute_script("""
      const bar = document.querySelector('#planning-bar');
      const dot = bar.querySelector('.planning-dot');
      const tabs = document.querySelector('.document-tabs');
      const style = getComputedStyle(bar);
      const dotStyle = getComputedStyle(dot);
      const box = bar.getBoundingClientRect();
      const dotBox = dot.getBoundingClientRect();
      const exit = bar.querySelector('#exit-planning');
      return {
        hidden: bar.hasAttribute('hidden'),
        display: style.display,
        background: style.backgroundColor,
        box: { width: box.width, height: box.height, top: box.top, left: box.left },
        tabsTop: tabs.getBoundingClientRect().top,
        columnTop: document.querySelector('.document-column').getBoundingClientRect().top,
        // The Brief's own height, which is what a mis-placed grid row actually costs: the editor
        // loses the `1fr` to the actions row and collapses to two lines, and every structural
        // assertion about the template goes on passing.
        editorHeight: document.querySelector('#creative-brief').getBoundingClientRect().height,
        columnHeight: document.querySelector('.document-column').getBoundingClientRect().height,
        dotBox: { width: dotBox.width, height: dotBox.height },
        dotColour: dotStyle.backgroundColor,
        dotRadius: dotStyle.borderRadius,
        name: document.querySelector('#planning-mode-name').textContent,
        sentence: document.querySelector('#planning-sentence').textContent,
        exit: exit.textContent,
        role: bar.getAttribute('role'),
        text: bar.textContent,
      };
    """)


def consent_paint(driver) -> dict:
    """The per-turn control and the label around it, as the browser draws them.

    `text-decoration-line` is the assertion that only a browser can make: the class is applied by
    `syncPlanningMode` and the strike is a stylesheet rule, and a rule that loses on specificity or
    never reaches the element leaves a control that is disabled and looks entirely ordinary.
    """
    return driver.execute_script("""
      const box = document.querySelector('#apply-documents');
      const label = document.querySelector('#apply-documents-toggle');
      const name = document.querySelector('#apply-documents-name');
      const note = document.querySelector('#apply-documents-superseded');
      const labelStyle = getComputedStyle(label);
      const nameStyle = getComputedStyle(name);
      const noteStyle = getComputedStyle(note);
      const noteBox = note.getBoundingClientRect();
      return {
        disabled: box.disabled,
        checked: box.checked,
        superseded: label.classList.contains('superseded'),
        decoration: nameStyle.textDecorationLine,
        colour: labelStyle.color,
        // The note must not be struck as well: the row's own decoration would propagate into it,
        // and a label naming what took over, crossed out, says the opposite of what it means.
        noteDecoration: noteStyle.textDecorationLine,
        nameSize: nameStyle.fontSize,
        title: label.getAttribute('title'),
        note: note.textContent,
        noteDisplay: noteStyle.display,
        noteColour: noteStyle.color,
        noteBox: { width: noteBox.width, height: noteBox.height },
      };
    """)


def thread_text(driver) -> str:
    return driver.find_element(By.ID, "chat-thread").text


def send_turn(driver, wait, message: str) -> None:
    """Type into the real composer and press its real submit button."""
    field = driver.find_element(By.CSS_SELECTOR, "#chat-form textarea")
    field.clear()
    field.send_keys(message)
    before = thread_text(driver)
    driver.find_element(By.CSS_SELECTOR, "#chat-form button[type=submit]").click()
    wait.until(lambda browser: thread_text(browser) != before)


def main() -> None:
    port = 8791
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    double = RecordedPlanner(free_port())
    double.start()
    # The application talks to the double above and to no model anywhere else, and ComfyUI is
    # pointed at a port nothing is listening on: nothing here can submit a render even by accident.
    os.environ["MVP_LLM_BASE_URL"] = f"{double.url}/v1"
    os.environ["MVP_LLM_MODEL"] = "recorded-double"
    os.environ["MVP_LLM_EJECT_BEFORE_RENDER"] = "false"
    os.environ["MVP_COMFY_URL"] = f"http://127.0.0.1:{free_port()}"

    result: dict[str, object] = {"double": double.url}
    try:
        with ManagedServer(port, label=NAME) as server:
            result["server_identity"] = server.evidence
            project_id = seed(server.base_url, "Planning Mode browser QA")
            # The second project is seeded **before** the browser starts, because the project
            # picker is filled at boot and on reload: one created mid-session has no option to
            # click, and the switch in section 8 would time out for a reason that has nothing to
            # do with what it is about.
            other_id = seed(server.base_url, "Planning Mode browser QA (second)")

            driver = edge_driver()
            wait = WebDriverWait(driver, 60)
            try:
                driver.get(server.base_url)
                select_project(wait, project_id)
                open_treatment(driver, wait)
                tokens = palette(driver)
                amber = as_rgb(driver, tokens["--amber"])
                result["palette"] = {"tokens": tokens, "amber_resolved": amber}

                # --- 1. Nothing is claimed before anything is pressed -------------------------
                # A bar painted at rest would claim a standing consent to rewrite the Brief. The
                # check is what the browser *draws*: `display: flex` on the class outranks the user
                # agent's `[hidden]` rule, and no offline test can see that.
                at_rest = bar_paint(driver)
                assert at_rest["hidden"] is True, at_rest
                assert at_rest["display"] == "none", (
                    f"the Planning bar is drawn at rest as {at_rest['display']}: the stylesheet's "
                    "display outranks the hidden attribute"
                )
                assert at_rest["box"]["height"] == 0, at_rest["box"]
                # A hidden bar leaves the editor the row it had: `display: none` takes the bar out
                # of the grid, and under auto-placement everything below it slides up a track.
                assert at_rest["editorHeight"] > at_rest["columnHeight"] * 0.5, (
                    "the Brief editor collapsed with the Planning bar hidden: the grid row it was "
                    f"given went somewhere else ({at_rest['editorHeight']} of "
                    f"{at_rest['columnHeight']})"
                )
                resting_consent = consent_paint(driver)
                assert resting_consent["disabled"] is False, resting_consent
                assert resting_consent["superseded"] is False, resting_consent
                assert resting_consent["decoration"] == "none", resting_consent
                assert resting_consent["noteDisplay"] == "none", resting_consent
                enter = driver.find_element(By.ID, "enter-planning")
                visible_and_clickable(driver, enter, "the Enter planning button")
                assert enter.is_enabled()
                result["nothing_claimed_before_the_press"] = {
                    "bar": at_rest["display"], "button": enter.text,
                }
                driver.save_screenshot(str(artifact_dir() / f"{NAME}-at-rest.png"))

                # --- 2. The mode is entered explicitly, and the bar states the trade ----------
                driver.find_element(By.ID, "enter-planning").click()
                wait.until(
                    lambda browser: not browser.find_element(
                        By.ID, "planning-bar"
                    ).get_attribute("hidden")
                )
                on = bar_paint(driver)
                assert on["hidden"] is False and on["display"] != "none", on
                # A real strip with real height, not a collapsed grid row.
                assert on["box"]["height"] > 16 and on["box"]["width"] > 200, on["box"]
                # Above the documents, which is where DESIGN §4.1 puts it and why.
                assert on["box"]["top"] < on["tabsTop"], on
                assert abs(on["box"]["top"] - on["columnTop"]) < 2, on
                # A real, round, amber dot rather than a correctly-sized empty box.
                assert on["dotBox"] == {"width": 9, "height": 9}, on["dotBox"]
                assert on["dotColour"] == amber, (on["dotColour"], amber)
                assert on["dotRadius"] == "50%", on["dotRadius"]
                # And the bar takes its own height from the column rather than from the editor's
                # row: the Brief is still the tall thing on screen.
                assert on["editorHeight"] > on["columnHeight"] * 0.5, on
                # A sentence, not just a dot: state is never colour-alone.
                assert len(on["sentence"]) > 80, on["sentence"]
                assert "Apply document changes" in on["sentence"], on["sentence"]
                assert on["role"] == "status", on["role"]
                assert on["exit"], "the bar offers no way out"
                exit_button = driver.find_element(By.ID, "exit-planning")
                visible_and_clickable(driver, exit_button, "the Exit planning button")
                result["bar_states_the_trade"] = {
                    "sentence": on["sentence"], "exit": on["exit"], "box": on["box"],
                }

                # --- 3. The suspended control is disabled AND visibly superseded --------------
                suspended = consent_paint(driver)
                assert suspended["disabled"] is True, suspended
                assert suspended["superseded"] is True, suspended
                assert "line-through" in suspended["decoration"], (
                    "the suspended consent control is disabled and otherwise looks entirely "
                    f"operable: text-decoration-line is {suspended['decoration']}"
                )
                assert suspended["colour"] == as_rgb(driver, tokens["--dim"]), suspended
                # The micro-label naming what took over is really on screen, with a real box.
                assert suspended["note"], suspended
                assert suspended["noteDisplay"] != "none", suspended
                assert suspended["noteBox"]["width"] > 40, suspended["noteBox"]
                assert suspended["noteColour"] == amber, (suspended["noteColour"], amber)
                assert "line-through" not in suspended["noteDecoration"], suspended
                # And the control's own name is unchanged in size by having become an element.
                assert suspended["nameSize"] == resting_consent["nameSize"], (
                    suspended["nameSize"], resting_consent["nameSize"]
                )
                # A disabled checkbox the browser honours: clicking it changes nothing.
                driver.find_element(By.ID, "apply-documents").click()
                assert consent_paint(driver)["checked"] is False, "a suspended control took consent"
                result["consent_control_is_superseded"] = suspended
                driver.save_screenshot(str(artifact_dir() / f"{NAME}-planning-on.png"))

                # --- 4. A turn sent while planning writes, and its notices land in the thread --
                double.brief = BRIEF_WRITTEN
                send_turn(driver, wait, "she is a passenger, not the driver")
                wait.until(
                    lambda _: stored(server.base_url, project_id)["creative_brief"]
                    == BRIEF_WRITTEN
                )
                thread = thread_text(driver)
                assert PROSE in thread, thread
                # AD-43: what the turn did is a `MessageNotice` on an ordinary message, drawn in
                # the same thread as every other reply, and there is no second surface for it.
                assert DOCUMENT_LABELS["creative_brief"] in thread, thread
                notices = driver.find_elements(By.CSS_SELECTOR, "#chat-thread .message-notice")
                assert notices, "a planning reply's notices did not reach the thread"
                assert driver.find_element(
                    By.ID, "creative-brief"
                ).get_attribute("value") == BRIEF_WRITTEN
                # The bar is still up: consent is per request, and the mode is not spent by a turn.
                assert bar_paint(driver)["hidden"] is False
                assert consent_paint(driver)["disabled"] is True
                result["planning_turn_wrote_and_reported_itself"] = {
                    "notices": len(notices), "planning_calls": double.planning_calls,
                }
                driver.save_screenshot(str(artifact_dir() / f"{NAME}-turn-landed.png"))

                # --- 5. Leaving restores the per-turn tick, unticked -------------------------
                driver.find_element(By.ID, "exit-planning").click()
                wait.until(
                    lambda browser: browser.find_element(By.ID, "planning-bar").get_attribute(
                        "hidden"
                    )
                    is not None
                )
                left_bar = bar_paint(driver)
                assert left_bar["display"] == "none" and left_bar["box"]["height"] == 0, left_bar
                restored = consent_paint(driver)
                assert restored["disabled"] is False and restored["checked"] is False, restored
                assert restored["decoration"] == "none", restored
                assert restored["noteDisplay"] == "none", restored
                # And the tick works again, which is what "restored" has to mean.
                driver.find_element(By.ID, "apply-documents").click()
                assert consent_paint(driver)["checked"] is True
                driver.find_element(By.ID, "apply-documents").click()
                assert consent_paint(driver)["checked"] is False
                result["leaving_restores_the_tick_unticked"] = restored
                driver.save_screenshot(str(artifact_dir() / f"{NAME}-planning-off.png"))

                # --- 6. A turn with the mode off is an ordinary chat turn --------------------
                # The Brief is what a Director reply can never replace, so a turn sent here must
                # leave it exactly as the planning turn left it -- and that is the observable
                # difference between "the composer sends a planning turn" and "it does not".
                before_chat = stored(server.base_url, project_id)["creative_brief"]
                send_turn(driver, wait, "and what about the ending")
                after_chat = stored(server.base_url, project_id)
                assert after_chat["creative_brief"] == before_chat, (
                    "a turn sent with Planning Mode off still wrote the Brief"
                )
                # Which route the browser used, counted by the double rather than inferred: a
                # composer that went on sending planning turns with the mode off would leave the
                # Brief unchanged too, because the turn would be refused for want of consent.
                assert double.chat_calls == 1, double.chat_calls
                assert double.planning_calls == 1, double.planning_calls
                result["mode_off_sends_an_ordinary_turn"] = {
                    "planning_calls": double.planning_calls, "chat_calls": double.chat_calls,
                }

                # --- 7. A locked Brief: the bar says so, and the server is still the authority -
                put_json(
                    f"{server.base_url}/api/projects/{project_id}/documents",
                    {
                        "creative_brief": after_chat["creative_brief"],
                        "treatment": after_chat["treatment"],
                        "style_bible": after_chat["style_bible"],
                        "creative_brief_locked": True,
                    },
                )
                driver.refresh()
                select_project(wait, project_id)
                open_treatment(driver, wait)
                # A reload leaves the mode off. It is session state and nothing restores it.
                reloaded = bar_paint(driver)
                assert reloaded["hidden"] is True, "the mode survived a reload"
                assert consent_paint(driver)["disabled"] is False
                result["a_reload_leaves_the_mode_off"] = True

                driver.find_element(By.ID, "enter-planning").click()
                wait.until(
                    lambda browser: not browser.find_element(
                        By.ID, "planning-bar"
                    ).get_attribute("hidden")
                )
                locked_bar = bar_paint(driver)
                assert "locked" in locked_bar["sentence"], locked_bar["sentence"]
                assert "cannot write it" in locked_bar["sentence"], locked_bar["sentence"]
                assert "still runs" in locked_bar["sentence"], locked_bar["sentence"]
                result["a_locked_brief_is_stated_by_the_bar"] = locked_bar["sentence"]
                driver.save_screenshot(str(artifact_dir() / f"{NAME}-locked-brief.png"))

                # The bar is a statement and never a gate: the request still goes, and the
                # server's own refusal is what the Director reads.
                locked_brief = stored(server.base_url, project_id)["creative_brief"]
                double.brief = BRIEF_SECOND
                send_turn(driver, wait, "tighten it anyway")
                refusal = DOCUMENT_LOCK_NOTICE.format(
                    document=DOCUMENT_LABELS["creative_brief"]
                )
                thread = thread_text(driver)
                assert refusal in " ".join(thread.split()), thread
                assert stored(server.base_url, project_id)["creative_brief"] == locked_brief
                result["the_server_still_refuses_a_locked_brief"] = True
                driver.save_screenshot(str(artifact_dir() / f"{NAME}-locked-refusal.png"))

                # --- 8. A project change leaves the mode, and the bar goes with it ------------
                select_project(wait, other_id)
                open_treatment(driver, wait)
                switched = bar_paint(driver)
                assert switched["hidden"] is True, "the mode followed a project change"
                assert consent_paint(driver)["disabled"] is False
                assert consent_paint(driver)["checked"] is False
                result["a_project_change_leaves_the_mode"] = True
                driver.save_screenshot(str(artifact_dir() / f"{NAME}-project-change.png"))

                assert not stored(server.base_url, project_id)["jobs"], "a render was queued"
                console_gate(driver, NAME, result, EXPECTED_CONSOLE)
            finally:
                driver.quit()
    finally:
        double.stop()

    # Never asserted from the client alone: the refusal the Director reads is the server's, and
    # this is the sentence it is built from.
    result["consent_refusal_wording"] = PLANNING_WITHOUT_CONSENT_NOTICE.format(
        document=DOCUMENT_LABELS["creative_brief"]
    )
    report(NAME, result)


if __name__ == "__main__":
    main()
