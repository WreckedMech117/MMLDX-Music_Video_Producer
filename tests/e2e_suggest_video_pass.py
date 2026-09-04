"""Browser QA for the Suggest Video pass a Director watches and walks away from (Epic 13, E2).

Story 13.2's browser half, delivering TP-4's indicator and its abandon. Everything that can be
proven without a browser already is: `suggestVideoTicked`, `suggestVideoRunningLabel`,
`suggestVideoMeasured`, `suggestVideoAbandonQuestion`, `suggestVideoNote` and the bound control that
calls each of them are executed against a stub DOM under node by
`tests/test_frontend_contract.py`, and the route's retry, its byte-identical Brief on failure and
its reported elapsed time are driven through the real route by `tests/test_suggest_video.py`.

**What none of that can reach is whether an indicator is on the screen.** A dot that is drawn at
zero height, a `display: inline-flex` that outranks the `hidden` attribute so the indicator never
goes away, a pulse that runs for a Director who asked for no motion, a note that lands under the
fold, a restore button still greyed out at the moment it finally has something to offer -- every one
of those passes an offline gate and is visible only by looking. So this script asserts the paint:
the dot's resolved colour, its measured box, its resolved `animation-name` with and without
`prefers-reduced-motion`, the reading's own text as the browser renders it, and the note's left edge
resolved to a palette token.

**A recording double, never a live model.** `MVP_LLM_BASE_URL` is pointed at a stub HTTP server this
script owns, which answers `/v1/chat/completions` after a delay it is told to take and with a reply
it is told to give -- complete, thin, or prose carrying no tool call. That is what makes the
indicator's whole life observable: a real pass takes minutes and cannot be made to fail on demand.
ComfyUI is pointed at a dead port, nothing is queued, and `/prompt` is never reached.

**The abandon section is the one with a ruling behind it.** The route has no cancellation awareness,
so abandoning stops this browser waiting and nothing else: the pass runs to completion and writes.
This script drives that to the end -- it abandons, lets the double finish, reloads, and asserts the
Brief holds what the pass wrote *and that Restore is armed*, which is the whole reason the honest
sentence is safe to say.

Run from the repo root -- it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_suggest_video_pass.py [--port 8789]

Assumes: nothing listening on the port, Microsoft Edge and its WebDriver installed, and
`music_video_producer` importable from this checkout's `src/`. ComfyUI does not need to be running
and no language-model host is needed -- this script provides its own.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    reachable_widths,
    report,
    visible_and_clickable,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Read from the server rather than retyped. A harness outside `uv run pytest` that transcribes a
# Director-facing sentence is a copy no gate watches -- `e2e_brief_recovery.py` carried one that was
# stale for a slice. These are the two the report is built from and the label the button carries.
from music_video_producer.app import (
    DOCUMENT_LABELS,
    SUGGEST_VIDEO_PARTIAL_NOTICE,
    SUGGEST_VIDEO_WRITTEN_NOTICE,
)

NAME = "suggest-video-pass"

#: The one console entry this script causes on purpose: section 7 drives the route into its real
#: 502 by making the double answer prose twice, and a browser logs a failed request whatever the
#: application does about it. Declared by fragment rather than filtered, so a *second* 502 -- one
#: this script did not ask for -- still fails the gate.
EXPECTED_CONSOLE = ["/brief/suggest - Failed to load resource"]

#: The palette, resolved as the browser renders it. `--amber` is *running* (standing law 7) and the
#: dot and a partial note's edge are both drawn in it; a failure's edge is `--red`. Compared as
#: resolved `rgb()` because that is what `getComputedStyle` answers and a hex would be a second copy
#: of the stylesheet -- these are read off `:root` at run time instead.
PALETTE_TOKENS = ("--amber", "--red", "--acid", "--dim")

SONG_SECONDS = 24.0
LYRICS = (
    "[Verse 1]\nThe headlights find the edge of town\n"
    "[Chorus]\nAnd everything we said falls down\n"
)
CAPTION = "Slow, sodium-lit, close and claustrophobic until the chorus opens out."

BRIEF_TYPED = (
    "A night drive that opens into wilderness. One character, no dialogue, and the car is the "
    "second character. Constraint: everything is practical light."
)

#: The five sections `SuggestedBrief` requires, filled. The double answers with this for a whole
#: pass, and with two of them blanked for the partial one -- so `partial` and `missing` come off the
#: real route rather than being simulated anywhere in this script.
WHOLE_BRIEF = {
    "premise": "A night drive out of a sodium-lit town and into salt flats at first light.",
    "cast": "One driver. No dialogue. The car is the second character.",
    "locations": "A coast road at night, a filling station, salt flats at dawn.",
    "arc": "Claustrophobia through the verses, opening out on the chorus and never closing again.",
    "look": "Practical light only: sodium, headlamps, then flat noon. Never a studio key.",
}
THIN_BRIEF = {**WHOLE_BRIEF, "cast": "", "locations": ""}
#: A third, deliberately different whole brief for the abandoned pass. `write_document` skips a
#: byte-identical write and spends no slot, so a pass answering exactly what is already stored would
#: leave the recovery slot holding an older version -- and section 9's assertion would be about a
#: write that never happened.
ALTERED_BRIEF = {
    **WHOLE_BRIEF,
    "premise": "The same drive, told from the salt flats backwards to the filling station.",
}


def free_port() -> int:
    """A port the OS just handed out, for the double. Never a fixed one: two of these scripts
    running at once on fixed ports is the collision `docs/OPERATIONS.md` already tracks for the app
    itself, and the double has no runbook line to track it in."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class RecordedDirector:
    """A stand-in for the local model on a port this script owns. **Never LM Studio.**

    It answers `POST /v1/chat/completions` and nothing else, after `delay` seconds, with whichever
    of three replies it has been set to: a complete `suggest_video` call, a thin one, or prose
    carrying no tool call at all -- which is the shape `parse_suggested_brief` refuses and the retry
    then rolls again on, so two of them make the route's real 502.

    The delay is what makes this script possible. An indicator that is up and down inside one
    request has no life to observe, and the acceptance criteria are all about its life.
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.delay = 0.0
        self.mode = "whole"
        self.calls: list[float] = []
        self.server: ThreadingHTTPServer | None = None

    def payload(self) -> dict:
        if self.mode == "prose":
            # No tool call: `parse_suggested_brief` refuses it by name, which is a `DirectorError`
            # and therefore a retry. Two of these is what the Director meets as a 502.
            return {"choices": [{"message": {"role": "assistant", "content": "Sure! Here are some ideas..."}}]}
        arguments = {"whole": WHOLE_BRIEF, "thin": THIN_BRIEF, "altered": ALTERED_BRIEF}[self.mode]
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "suggest_video",
                                    "arguments": json.dumps(arguments),
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
            # HTTP/1.0 for `e2e_clips_and_attach.StubComfy`'s reason: one connection per request,
            # so stopping this double is visible to the application's pooled client immediately.
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args: object) -> None:
                return None

            def do_POST(self) -> None:  # BaseHTTPRequestHandler's spelling, not ours
                length = int(self.headers.get("content-length") or 0)
                self.rfile.read(length)
                if not self.path.endswith("/chat/completions"):
                    self.send_error(404)
                    return
                double.calls.append(time.monotonic())
                time.sleep(double.delay)
                body = json.dumps(double.payload()).encode()
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


def synthesize_song(target: Path, seconds: float = SONG_SECONDS) -> None:
    """Silence at a real sample rate. The pass reads the song's *words*, never its audio, so a
    track with nothing in it is the honest fixture: what this script is about is what happens while
    a model thinks, and giving the analysis something to find would only slow the seed down."""
    frames = bytearray(int(seconds * 8000) * 2)
    with wave.open(str(target), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(bytes(frames))


def seed(base_url: str, name: str) -> str:
    """A project the pass can actually run on: a Song with both context fields filled.

    `suggest_video_refusal` needs a Song and both of its context fields, and nothing else -- not
    sections, not a duration (R-15). Both are written through the routes that own them rather than
    into the manifest, so this fixture is a project a Director could have made.
    """
    project = post_json(f"{base_url}/api/projects", {"name": name})
    song = artifact_dir() / f"{NAME}-song.wav"
    synthesize_song(song)
    post_multipart(
        f"{base_url}/api/projects/{project['id']}/songs/upload",
        {"title": "Suggest Video QA song", "duration": str(SONG_SECONDS)},
        ("file", song),
    )
    put_json(
        f"{base_url}/api/projects/{project['id']}/song/context",
        {"lyrics": LYRICS, "caption": CAPTION},
    )
    return project["id"]


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
    """Every token this script compares against, resolved by the browser off `:root`."""
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


def indicator_paint(driver) -> dict:
    """What the running indicator actually *is* on screen, measured rather than asserted.

    A structural check cannot tell a dot from a dot drawn at zero height inside a hidden box, and
    both of this application's recorded "drawn as nothing" defects were exactly that.
    """
    return driver.execute_script("""
      const row = document.querySelector('#suggest-indicator');
      const dot = row.querySelector('.pass-dot');
      const reading = document.querySelector('#suggest-elapsed');
      const dotStyle = getComputedStyle(dot);
      const rowBox = row.getBoundingClientRect();
      const dotBox = dot.getBoundingClientRect();
      return {
        hidden: row.hasAttribute('hidden'),
        rowDisplay: getComputedStyle(row).display,
        rowBox: { width: rowBox.width, height: rowBox.height, top: rowBox.top, left: rowBox.left },
        dotBox: { width: dotBox.width, height: dotBox.height },
        dotColour: dotStyle.backgroundColor,
        dotRadius: dotStyle.borderRadius,
        animation: dotStyle.animationName,
        reading: reading.textContent,
        rowText: row.textContent,
        title: row.getAttribute('title') || '',
        noPreference: window.matchMedia('(prefers-reduced-motion: no-preference)').matches,
      };
    """)


def note_paint(driver) -> dict:
    return driver.execute_script("""
      const note = document.querySelector('#suggest-note');
      const style = getComputedStyle(note);
      const box = note.getBoundingClientRect();
      return {
        hidden: note.hasAttribute('hidden'),
        display: style.display,
        text: note.textContent,
        classes: [...note.classList],
        edge: style.borderLeftColor,
        box: { width: box.width, height: box.height, top: box.top, bottom: box.bottom },
        inView: box.top >= 0 && box.bottom <= window.innerHeight && box.height > 0,
      };
    """)


def paint_evidence(driver, text: str) -> None:
    """Put a sentence the browser's own chrome swallowed into the page, so the screenshot carries
    it. `e2e_shot_numbering.py`'s idiom: a native `confirm` never lands in a viewport capture, and
    the assertion has already been made against the real dialog by the time this runs."""
    driver.execute_script("""
      const banner = document.createElement('pre');
      banner.id = 'e2e-dialog-evidence';
      banner.textContent = arguments[0];
      banner.style.cssText = 'position:fixed;z-index:99;left:12px;bottom:12px;max-width:60ch;'
        + 'white-space:pre-wrap;padding:10px;border:1px solid #ffb454;background:#171919;'
        + 'color:#f0f2ed;font:12px Consolas,monospace';
      document.body.append(banner);
    """, text)


def clear_evidence(driver) -> None:
    driver.execute_script("document.querySelector('#e2e-dialog-evidence')?.remove();")


def wait_for_pass_to_land(driver, wait) -> str:
    """Wait for the pass to be over on screen, and answer the report it left.

    Waiting on a *fragment* of the note cannot serve when consecutive passes report the same
    sentence: the previous pass's note already contains it and the wait returns before the new pass
    has started. `beginSuggestPass` empties the note, so "the indicator is down and the note has
    text again" is the state that only the new pass can produce.
    """
    wait.until(
        lambda browser: browser.find_element(By.ID, "suggest-indicator").get_attribute("hidden")
        and (browser.find_element(By.ID, "suggest-note").get_attribute("textContent") or "").strip()
    )
    return driver.find_element(By.ID, "suggest-note").get_attribute("textContent")


def wait_for_note(driver, wait, fragment: str) -> str:
    wait.until(
        lambda browser: fragment
        in browser.find_element(By.ID, "suggest-note").get_attribute("textContent")
    )
    return driver.find_element(By.ID, "suggest-note").get_attribute("textContent")


def elapsed_seconds(driver) -> int | None:
    """The whole seconds the indicator is claiming, or `None` when it is claiming nothing.

    `None` rather than a raise: this is read inside a wait predicate, and a pass that lands between
    two polls empties the element -- which must end the wait rather than blow it up with an error
    about the harness.
    """
    reading = driver.execute_script(
        "return document.querySelector('#suggest-elapsed').textContent || '';"
    )
    found = re.search(r"(\d+)s", reading)
    return int(found.group(1)) if found else None


def disabled_ids(driver) -> list[str]:
    """Every control in the Treatment workspace the browser is refusing, by id."""
    return sorted(
        element.get_attribute("id")
        for element in driver.find_elements(
            By.CSS_SELECTOR, "#panel-treatment button, #panel-treatment textarea"
        )
        if not element.is_enabled()
    )


def stored(base_url: str, project_id: str) -> dict:
    return get_json(f"{base_url}/api/projects/{project_id}")


def main() -> None:
    port = 8789
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    double = RecordedDirector(free_port())
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
            project_id = seed(server.base_url, "Suggest Video browser QA")

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
                # An indicator painted by markup would claim a pass nothing started -- this
                # application's own "a control that says something happened when it did not", one
                # tense forward. Both stateful elements ship `hidden`, and the check is what the
                # browser *draws*, because `display: inline-flex` on the class would outrank the
                # user agent's `[hidden]` rule and no offline test would see it.
                at_rest = indicator_paint(driver)
                assert at_rest["hidden"] is True, at_rest
                assert at_rest["rowDisplay"] == "none", (
                    f"the indicator is drawn at rest as {at_rest['rowDisplay']}: the stylesheet's "
                    "display outranks the hidden attribute"
                )
                assert note_paint(driver)["display"] == "none", note_paint(driver)
                button = driver.find_element(By.ID, "suggest-video")
                visible_and_clickable(driver, button, "the Suggest video button")
                assert button.is_enabled()
                # The workspace's resting refusals -- the H3 expansion and the three assistant
                # controls all ship disabled for their own reasons -- so section 3 can ask what the
                # *pass* shut rather than what was already shut.
                at_rest_disabled = disabled_ids(driver)
                assert "suggest-video" not in at_rest_disabled
                result["nothing_claimed_before_the_press"] = {
                    "indicator": at_rest["rowDisplay"], "button": button.text,
                }

                # A Brief the Director wrote themselves, so the pass has a version to displace and
                # the abandon sentence's kept arm is the true one.
                brief_box = driver.find_element(By.ID, "creative-brief")
                brief_box.send_keys(BRIEF_TYPED)
                driver.find_element(By.ID, "save-treatment").click()
                wait.until(
                    lambda _: stored(server.base_url, project_id)["creative_brief"] == BRIEF_TYPED
                )

                # --- 2. A pass in flight: an indicator, elapsed time, and no progress figure ---
                double.mode = "whole"
                double.delay = 9.0
                driver.find_element(By.ID, "suggest-video").click()
                wait.until(
                    lambda browser: not browser.find_element(
                        By.ID, "suggest-indicator"
                    ).get_attribute("hidden")
                )
                running = indicator_paint(driver)
                assert running["hidden"] is False and running["rowDisplay"] != "none", running
                assert running["rowBox"]["height"] > 8 and running["rowBox"]["width"] > 80, running
                # The dot is a real, round, amber dot rather than a correctly-sized empty box.
                assert running["dotBox"] == {"width": 9, "height": 9}, running["dotBox"]
                assert running["dotColour"] == amber, (running["dotColour"], amber)
                assert running["dotRadius"] == "50%", running["dotRadius"]
                # Whatever *this* browser asked for, the dot agrees with it. Not asserted as a
                # constant: this machine's Edge answers `reduce` unprompted, which is itself the
                # evidence for the `no-preference` form -- under a `reduce` rule the pulse would be
                # the default here and the still dot the state nobody could reach. Both arms are
                # then driven explicitly in section 6.
                assert running["animation"] == (
                    "pass-pulse" if running["noPreference"] else "none"
                ), running
                result["native_motion_preference"] = {
                    "no_preference": running["noPreference"], "animation": running["animation"],
                }
                # Elapsed time, and nothing that could be read as progress. The number will get
                # large: the pass's own timeout is 300 s and that is the honest reading.
                assert re.fullmatch(r"Suggest Video · running · \d+s", running["reading"]), (
                    running["reading"]
                )
                assert "%" not in running["rowText"], running["rowText"]
                assert not driver.find_elements(By.CSS_SELECTOR, "#suggest-indicator progress")
                assert not driver.find_elements(
                    By.CSS_SELECTOR, "#suggest-indicator [role='progressbar']"
                )
                assert "no progress figure" in running["title"], running["title"]
                # And it climbs, which is the difference between an indicator and a label.
                first = elapsed_seconds(driver)
                assert first is not None, running["reading"]
                wait.until(
                    lambda browser: (elapsed_seconds(browser) or 0) > first
                    or elapsed_seconds(browser) is None
                )
                assert elapsed_seconds(driver) is not None, (
                    "the pass landed before the reading could be seen to climb; raise the "
                    "double's delay"
                )
                climbing = indicator_paint(driver)
                result["indicator_in_flight"] = {
                    "first": running["reading"], "later": climbing["reading"],
                    "dot": running["dotBox"], "colour": running["dotColour"],
                    "animation": running["animation"],
                }
                driver.save_screenshot(str(artifact_dir() / f"{NAME}-running.png"))

                # --- 3. The rest of the workspace is usable while it runs ----------------------
                # Asserted rather than assumed (constraint 4). No modal, nothing disabled but the
                # control that would start a second pass, and a real gesture elsewhere that works.
                assert not driver.find_elements(By.CSS_SELECTOR, "dialog[open]"), (
                    "a pass in flight put a modal over the workspace"
                )
                shut = disabled_ids(driver)
                gained = sorted(set(shut) - set(at_rest_disabled))
                assert gained == ["suggest-video"], (gained, shut, at_rest_disabled)
                assert not set(at_rest_disabled) - set(shut), (shut, at_rest_disabled)
                # A different document opened, typed in, and the tab switched back -- the indicator
                # is in the workspace heading precisely so it survives that.
                driver.find_element(By.CSS_SELECTOR, '.document-tabs [data-doc="style"]').click()
                wait.until(
                    lambda browser: browser.find_element(By.ID, "style-bible").is_displayed()
                )
                driver.find_element(By.ID, "style-bible").send_keys("Sodium and rain.")
                # And another workspace entirely, and back.
                driver.find_element(By.CSS_SELECTOR, '[data-panel="assets"]').click()
                driver.find_element(By.CSS_SELECTOR, '[data-panel="treatment"]').click()
                driver.find_element(By.CSS_SELECTOR, '.document-tabs [data-doc="brief"]').click()
                wait.until(
                    lambda browser: browser.find_element(By.ID, "creative-brief").is_displayed()
                )
                survived = indicator_paint(driver)
                assert survived["hidden"] is False, (
                    "the indicator went away when the Director looked at another workspace"
                )
                result["workspace_usable_while_running"] = {
                    "disabled": shut, "indicator_survived_navigation": True,
                    "reading": survived["reading"],
                }
                driver.save_screenshot(str(artifact_dir() / f"{NAME}-workspace-usable.png"))

                # --- 4. It lands, and the report states the measurement -------------------------
                written = wait_for_note(driver, wait, "Suggest Video wrote")
                landed = indicator_paint(driver)
                note = note_paint(driver)
                # The ticking approximation is gone from the screen *and* from the element that
                # held it, which is what stops the two elapsed numbers being read side by side.
                assert landed["hidden"] is True and landed["rowDisplay"] == "none", landed
                assert landed["reading"] == "", (
                    f"the client's ticking number is still on the page as {landed['reading']!r} "
                    "beside the server's measurement"
                )
                # The sentence is the server's own, and it quotes the measurement.
                assert written == SUGGEST_VIDEO_WRITTEN_NOTICE.format(
                    document=DOCUMENT_LABELS["creative_brief"],
                    elapsed=float(re.search(r"in (\d+\.\d)s", written).group(1)),
                ), written
                measured = float(re.search(r"in (\d+\.\d)s", written).group(1))
                assert double.delay <= measured < double.delay + 20, measured
                assert note["inView"] and note["box"]["height"] > 20, note
                assert note["classes"] == ["pass-note", "written"], note["classes"]
                assert note["edge"] == as_rgb(driver, tokens["--acid"]), note["edge"]
                # The Brief on screen is what the pass wrote, and the version it displaced is
                # restorable -- the button beside it says so rather than staying greyed out.
                wait.until(
                    lambda browser: "## Premise"
                    in browser.find_element(By.ID, "creative-brief").get_attribute("value")
                )
                restore = driver.find_element(By.ID, "restore-brief")
                assert restore.is_enabled(), "Restore is greyed out over a version it is holding"
                after = stored(server.base_url, project_id)
                assert after["creative_brief_previous"] == BRIEF_TYPED, after
                result["complete_pass"] = {
                    "measured": measured, "note": written, "edge": note["edge"],
                    "restore_armed": True, "calls": len(double.calls),
                }
                driver.save_screenshot(str(artifact_dir() / f"{NAME}-report.png"))

                # --- 4b. And it fits, at three widths, with the note on screen -------------------
                # The note is a **fourth row** in a grid whose template was written for three, and
                # a new child in a template written for fewer is exactly how `e2e_brief_recovery.py`
                # found the Brief's help paragraph squeezing the editor. The actions row also gained
                # a button and wraps. So this measures rather than assumes: the note is whole and
                # inside the panel, the editor keeps a usable height, and the two new controls stay
                # reachable. The window is left where it started.
                fits: dict[str, object] = {}
                for width in (1600, 1280, 1024):
                    driver.set_window_size(width, 1100)
                    fits[str(width)] = driver.execute_script("""
                      const panel = document.querySelector('.document-column');
                      const note = document.querySelector('#suggest-note');
                      const editor = document.querySelector('#creative-brief');
                      const actions = document.querySelector('.document-actions');
                      const box = (element) => element.getBoundingClientRect();
                      return {
                        note: box(note).height,
                        noteInsidePanel: box(note).bottom <= box(panel).bottom + 1,
                        noteClipped: note.scrollHeight > note.clientHeight + 1,
                        editor: box(editor).height,
                        actionsInsidePanel: box(actions).bottom <= box(panel).bottom + 1,
                        panelInWindow: box(panel).bottom <= window.innerHeight + 1,
                      };
                    """)
                for width, measured in fits.items():
                    assert measured["note"] > 20, (width, measured)
                    assert not measured["noteClipped"], (width, measured)
                    assert measured["noteInsidePanel"], (width, measured)
                    assert measured["actionsInsidePanel"], (width, measured)
                    assert measured["editor"] > 120, (
                        f"at {width}px the note and the actions row squeezed the Brief editor to "
                        f"{measured['editor']}px"
                    )
                fits["suggest_video"] = reachable_widths(
                    driver, "#suggest-video", [1600, 1280, 1024]
                )
                result["fits_at_three_widths"] = fits
                driver.set_window_size(1600, 1100)

                # --- 5. A partial result is drawn as partial, and names what came back thin ------
                double.mode = "thin"
                double.delay = 1.0
                driver.find_element(By.ID, "suggest-video").click()
                partial = wait_for_note(driver, wait, "Partial:")
                thin = note_paint(driver)
                headings = [
                    heading
                    for heading in ("Cast", "Locations")
                    if heading.lower() in partial.lower()
                ]
                assert headings == ["Cast", "Locations"], (headings, partial)
                assert partial == SUGGEST_VIDEO_PARTIAL_NOTICE.format(
                    document=DOCUMENT_LABELS["creative_brief"],
                    elapsed=float(re.search(r"in (\d+\.\d)s", partial).group(1)),
                    missing=re.search(r"nothing for ([^.]+)\.", partial).group(1),
                ), partial
                assert thin["classes"] == ["pass-note", "partial"], thin["classes"]
                assert thin["edge"] == amber, (thin["edge"], amber)
                assert thin["inView"], thin
                result["partial_pass"] = {"note": partial, "edge": thin["edge"]}
                driver.save_screenshot(str(artifact_dir() / f"{NAME}-partial.png"))

                # --- 6. prefers-reduced-motion gets a still dot, and both arms are driven -------
                # Through the media query itself rather than by reading the stylesheet: what is
                # being asked is what *this browser* does for a Director who asked for less motion,
                # and that is a question only a browser answers. Both arms, because a rule that
                # never animates would pass the reduced half on its own -- and this machine's Edge
                # answers `reduce` without being asked, so the pulsing arm is the one that would
                # otherwise never be executed anywhere.
                motion: dict[str, object] = {}
                # A different brief per arm: `write_document` skips a byte-identical write and
                # spends no slot, so two identical passes in a row would leave the second reporting
                # a slot it did not fill.
                for wanted, expected, answer in (
                    ("reduce", "none", "whole"), ("no-preference", "pass-pulse", "altered"),
                ):
                    driver.execute_cdp_cmd(
                        "Emulation.setEmulatedMedia",
                        {"features": [{"name": "prefers-reduced-motion", "value": wanted}]},
                    )
                    double.delay = 6.0
                    double.mode = answer
                    driver.find_element(By.ID, "suggest-video").click()
                    wait.until(
                        lambda browser: not browser.find_element(
                            By.ID, "suggest-indicator"
                        ).get_attribute("hidden")
                    )
                    drawn = indicator_paint(driver)
                    assert drawn["noPreference"] is (wanted == "no-preference"), (
                        f"the {wanted} emulation did not reach the page", drawn
                    )
                    assert drawn["animation"] == expected, (wanted, drawn["animation"])
                    # It is the same indicator either way: the dot is still there, still amber and
                    # still the same size, so no state is carried by the motion alone.
                    assert drawn["dotBox"] == {"width": 9, "height": 9}, drawn["dotBox"]
                    assert drawn["dotColour"] == amber, drawn["dotColour"]
                    assert re.fullmatch(r"Suggest Video · running · \d+s", drawn["reading"]), drawn
                    motion[wanted] = {
                        "animation": drawn["animation"], "dot": drawn["dotBox"],
                        "colour": drawn["dotColour"], "reading": drawn["reading"],
                    }
                    driver.save_screenshot(
                        str(artifact_dir() / f"{NAME}-motion-{wanted}.png")
                    )
                    assert "Suggest Video wrote" in wait_for_pass_to_land(driver, wait)
                result["motion_preference"] = motion
                driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"features": []})

                # --- 7. A failure reports E1's sentence, never a blank --------------------------
                # Two prose replies: `parse_suggested_brief` refuses each, the route retries once,
                # and the Director meets the 502 that names the exception class and the elapsed
                # time. `httpx.ReadTimeout` stringifies to `""`, which is why this is reported by
                # class at all -- a blank reads as a fault in the application.
                before_failure = stored(server.base_url, project_id)
                double.mode = "prose"
                double.delay = 0.5
                calls_before = len(double.calls)
                driver.find_element(By.ID, "suggest-video").click()
                failed = wait_for_note(driver, wait, "returned nothing usable")
                broken = note_paint(driver)
                assert "DirectorError" in failed, failed
                assert re.search(r"ran for \d+\.\d+s", failed), failed
                assert "Nothing was written" in failed, failed
                assert failed.strip(), "a failure reached the Director as a blank"
                assert broken["classes"] == ["pass-note", "failed"], broken["classes"]
                assert broken["edge"] == as_rgb(driver, tokens["--red"]), broken["edge"]
                assert indicator_paint(driver)["hidden"] is True
                # Byte-identical, which is the other half of what that sentence promises.
                assert stored(server.base_url, project_id) == before_failure, (
                    "a failed pass changed the manifest"
                )
                # And the retry really is one, not zero and not two.
                assert len(double.calls) - calls_before == 2, double.calls
                result["failed_pass"] = {
                    "note": failed, "edge": broken["edge"],
                    "attempts": len(double.calls) - calls_before,
                }
                driver.save_screenshot(str(artifact_dir() / f"{NAME}-failed.png"))

                # --- 8. Abandon stops watching, and says so before it is pressed ----------------
                # The ruling of 2026-09-04. The route has no cancellation awareness, so this stops
                # the browser waiting and nothing else -- and the sentence says exactly that.
                # `whole` again, and it differs from what section 6's last arm left stored --
                # a byte-identical write spends no slot, and section 9 is entirely about the slot.
                double.mode = "whole"
                double.delay = 25.0
                kept_before = stored(server.base_url, project_id)["creative_brief"]
                driver.find_element(By.ID, "suggest-video").click()
                wait.until(
                    lambda browser: not browser.find_element(
                        By.ID, "suggest-indicator"
                    ).get_attribute("hidden")
                )
                driver.find_element(By.ID, "abandon-suggest").click()
                wait.until(EC.alert_is_present())
                question = driver.switch_to.alert.text
                assert "cannot be called back" in question, question
                assert "if it finishes it writes" in question, question
                assert "Restore Creative brief" in question, question
                for lie in ("will be cancelled", "nothing will be written", "has been cancelled"):
                    assert lie not in question.lower(), (lie, question)
                # Cancel first: a confirmation that stops watching either way is not one.
                driver.switch_to.alert.dismiss()
                declined = indicator_paint(driver)
                assert declined["hidden"] is False, "declining the question stopped the pass anyway"
                paint_evidence(driver, question)
                driver.save_screenshot(str(artifact_dir() / f"{NAME}-abandon-question.png"))
                clear_evidence(driver)
                # And now accept.
                driver.find_element(By.ID, "abandon-suggest").click()
                wait.until(EC.alert_is_present())
                driver.switch_to.alert.accept()
                wait.until(
                    lambda browser: browser.find_element(By.ID, "suggest-indicator").get_attribute(
                        "hidden"
                    )
                )
                stopped = note_paint(driver)
                assert "not called back" in stopped["text"], stopped["text"]
                assert "reload" in stopped["text"].lower(), stopped["text"]
                assert "Restore Creative brief" in stopped["text"], stopped["text"]
                # An abandon is not an outcome, so it wears none of the three outcome faces.
                assert stopped["classes"] == ["pass-note"], stopped["classes"]
                assert indicator_paint(driver)["reading"] == ""
                assert driver.find_element(By.ID, "suggest-video").is_enabled()
                result["abandon"] = {"question": question, "note": stopped["text"]}
                driver.save_screenshot(str(artifact_dir() / f"{NAME}-abandoned.png"))

                # --- 9. The pass finishes anyway, and Restore is armed for it -------------------
                # The whole reason the honest sentence is safe to say: E1's recovery slot holds the
                # text this pass displaced, so the outcome is recoverable. Waited for on the
                # server's own manifest -- this browser stopped watching and is not told.
                wait.until(
                    lambda _: stored(server.base_url, project_id)["creative_brief_previous"]
                    == kept_before
                )
                driver.get(server.base_url)
                select_project(wait, project_id)
                open_treatment(driver, wait)
                restore = wait.until(
                    lambda browser: browser.find_element(By.ID, "restore-brief")
                )
                assert restore.is_enabled(), (
                    "Restore is greyed out at the exact moment it has something to offer"
                )
                visible_and_clickable(driver, restore, "the armed Brief restore button")
                on_screen = driver.find_element(By.ID, "creative-brief").get_attribute("value")
                assert WHOLE_BRIEF["premise"] in on_screen, (
                    "the abandoned pass's write is not on screen after a reload"
                )
                # Nothing is claimed about a pass on a fresh load: the note is a session fact.
                assert note_paint(driver)["display"] == "none"
                assert indicator_paint(driver)["hidden"] is True
                result["abandoned_pass_wrote_and_restore_is_armed"] = True
                driver.save_screenshot(str(artifact_dir() / f"{NAME}-restore-armed.png"))

                # And the restore really puts it back, which is what the sentence promised.
                driver.find_element(By.ID, "restore-brief").click()
                wait.until(
                    lambda browser: browser.find_element(By.ID, "creative-brief").get_attribute(
                        "value"
                    )
                    == kept_before
                )
                result["restore_round_trips_after_an_abandon"] = True

                assert not stored(server.base_url, project_id)["jobs"], "a render was queued"
                console_gate(driver, NAME, result, EXPECTED_CONSOLE)
            finally:
                driver.quit()
    finally:
        double.stop()

    report(NAME, result)


if __name__ == "__main__":
    main()
