"""Browser QA for the Brief's attribution overlay (Epic 14, Slice C2 -- TP-8, AD-32).

Slice C1 built the record and nothing drew it; this slice is the paint, so **the harness is not
optional**. Everything that can be proven without a browser already is: `briefAttributionRanges`,
`briefAttributionSegments`, `briefAttributionMarkAt`, `briefAttributionLabel`,
`briefAttributionSummary` and `briefMarkRule` are executed under node by
`tests/test_frontend_contract.py`, which also drives the four wiring points -- the render, the
scroll, the resize and the label the project switch has to clear -- through the bound controls
against a stub DOM. The reconciliation and every writer of `brief_attribution` are driven through
the real routes by `tests/test_brief_attribution.py`.

**What none of that can reach is whether the mirror is over its own text.** A mirror overlay is
one long argument about metrics, and every way it goes wrong is invisible to an offline gate:

- `white-space` and `overflow` are declared nowhere for the Brief's textarea. A mirror that takes
  the block default collapses every run of spaces and every newline and wraps differently *from
  its first character*.
- `overflow: auto` means the textarea's scrollbar **appears with the content**, taking its gutter
  out of the textarea's wrapping width and not the mirror's -- so a mirror that matches at rest
  drifts the moment the Brief passes one screen, and looks perfect in a screenshot of the top of
  the box. Both are declared now, and section 2 measures the result at two window sizes.
- A trailing newline gets a line box inside a textarea and does not inside a block.

So this script asserts the paint and the metrics: the two elements' resolved styles compared pair
by pair, `mirror.scrollHeight === textarea.scrollHeight` before **and after** a viewport resize
that really re-wraps (the numbers are recorded, and the test fails if they did not move), the
mark's own rectangle against the character it starts at, one rule per mark rather than one per
visual line, and the wash still over its text at the bottom of a scroll.

**The injection constraint is asserted in the browser rather than trusted.** A Brief containing
`<script>` and `</textarea>` must render as those characters and must not create an element: the
mirror is built from text nodes, and this counts the elements the page ended up with.

Run from the repo root -- it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_brief_attribution.py [--port 8790]

Assumes: nothing listening on the port, Microsoft Edge and its WebDriver installed, and
`music_video_producer` importable from this checkout's `src/`. ComfyUI does not need to be running
and no language-model host is needed: the attributed Brief is a **seeded manifest**, which is what
makes a mark with a real `message_id` and a mark with the empty one both reachable in one run.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from e2e_support import (
    ManagedServer,
    artifact_dir,
    console_gate,
    edge_driver,
    get_json,
    post_json,
    report,
    visible_and_clickable,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

NAME = "brief-attribution"

REPO = Path(__file__).resolve().parents[1]
API_JS = REPO / "src" / "music_video_producer" / "web" / "assets" / "api.js"


def asset_constant(name: str) -> str:
    """One `export const NAME = "...";` read out of `api.js` rather than retyped here.

    A harness outside `uv run pytest` that transcribes a Director-facing sentence is a copy no
    gate watches -- `e2e_brief_recovery.py` carried one that was stale for a slice. These are
    JavaScript constants, so they are read from the asset the browser is served.
    """
    found = re.search(
        rf'^export const {name} =\s*"((?:[^"\\]|\\.)*)";', API_JS.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert found, f"api.js no longer exports {name} as a single-line string constant"
    return json.loads(f'"{found.group(1)}"')


# --- The seeded Brief ---------------------------------------------------------------------------
# Built by concatenation with the offsets taken from the pieces, never counted by hand: a mark at
# a miscounted offset would wash the wrong characters and every assertion below would still pass.

#: The Director's own words, **in front of the mark on the same line**. This is the case the whole
#: rule design turns on: a machine write's marks are whole-line aligned, but a mark that survives
#: a save need not be, and `reconcile_attribution` was executed to confirm it. The wash has to
#: start at the `#`, and the rule cannot be a `border-left` on the marked span.
LEAD = "Maybe: "

#: One contribution, with a blank line inside it -- the rule runs across it rather than breaking.
MARKED_ONE = (
    "## Cast\n"
    "One driver who never speaks, and a hitchhiker who does all of the talking for both of them "
    "across four hundred miles of coast road. The car is the second character and it is never "
    "named, never explained, and never once shown from outside at rest.\n"
    "\n"
    "## Look\n"
    "Practical light only: sodium vapour, headlamps, then a flat white noon that never softens "
    "for anybody. Never a studio key, never a gel, and never a haze machine in a single frame "
    "of it.\n"
)

#: The Director's own paragraph between the two marks, carrying the markup this must not parse.
BETWEEN = (
    "\nMy own note, which nothing marked: <script>alert('brief')</script> and a stray "
    "</textarea><img src=x onerror=1> that must arrive as characters.\n\n"
)

#: A second contribution whose `message_id` is `""` -- a machine write with no turn to point at,
#: which is what Suggest Video writes. It must not read as a blank or as a missing turn.
MARKED_TWO = (
    "## Arc\n"
    "Claustrophobia through the verses, opening out on the chorus and never closing again, so "
    "that the last minute is the widest thing in the film and nobody says a word during it.\n"
)

#: Enough after it to push the Brief well past one screen, which is where the scrollbar gutter
#: hazard lives and where a mirror that matches at rest starts to drift.
TAIL = "".join(
    f"\nA closing constraint the Director typed, number {index}, long enough to wrap across the "
    f"width of the box more than once and to keep wrapping after the window is made narrower.\n"
    for index in range(1, 7)
)

BRIEF = LEAD + MARKED_ONE + BETWEEN + MARKED_TWO + TAIL
MARK_ONE = {"start": len(LEAD), "end": len(LEAD) + len(MARKED_ONE), "message_id": "msg_2"}
MARK_TWO = {
    "start": len(LEAD) + len(MARKED_ONE) + len(BETWEEN),
    "end": len(LEAD) + len(MARKED_ONE) + len(BETWEEN) + len(MARKED_TWO),
    "message_id": "",
}
assert BRIEF[MARK_ONE["start"]:MARK_ONE["end"]] == MARKED_ONE
assert BRIEF[MARK_TWO["start"]:MARK_TWO["end"]] == MARKED_TWO

THREAD = [
    {"id": "msg_1", "role": "user", "content": "Open on a night drive and take it somewhere."},
    {"id": "msg_2", "role": "assistant", "content": "Here is a cast and a look for it."},
]

#: The pairs that decide whether the mirror wraps where the box wraps. Compared as the browser
#: resolves them, so `font: inherit` and a cascade three rules deep are read as the one value they
#: actually produce rather than as three declarations someone believes agree.
SHARED_METRICS = (
    "fontFamily", "fontSize", "lineHeight", "fontWeight", "fontStyle", "letterSpacing",
    "wordSpacing", "textIndent", "textTransform", "whiteSpace", "overflowWrap", "wordBreak",
    "tabSize", "direction", "textAlign", "paddingTop", "paddingRight", "paddingBottom",
    "paddingLeft", "borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth",
    "boxSizing", "overflowX", "overflowY",
)

GEOMETRY = """
  const box = document.querySelector('#creative-brief');
  const mirror = document.querySelector('#brief-mirror');
  const body = document.querySelector('#brief-mirror-text');
  const rulesLayer = document.querySelector('#brief-mirror-rules');
  const marks = [...body.querySelectorAll('mark')];
  const rules = [...rulesLayer.querySelectorAll('.brief-mark-rule')];
  const boxRect = box.getBoundingClientRect();
  const mirrorRect = mirror.getBoundingClientRect();
"""


def manifest_path(server: ManagedServer, project_id: str) -> Path:
    return server.data_root / "projects" / project_id / "project.json"


def seed_attribution(server: ManagedServer, project_id: str) -> dict:
    """Write the Brief and its ranges straight onto the manifest.

    The server is the sole writer of `brief_attribution` (AD-45) and no route accepts one from a
    body, which is the point of the field -- so a browser harness that needs a mark with a real
    `message_id` *and* a mark with the empty one seeds the manifest rather than pretending to be
    a writer. This is the "seeded manifest is enough" the spec's verification allows.
    """
    path = manifest_path(server, project_id)
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["creative_brief"] = BRIEF
    stored["brief_attribution"] = [MARK_ONE, MARK_TWO]
    stored["messages"] = THREAD
    path.write_text(json.dumps(stored, indent=2), encoding="utf-8")
    return stored


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


def open_brief(driver, wait) -> None:
    driver.find_element(By.CSS_SELECTOR, '[data-panel="treatment"]').click()
    wait.until(EC.visibility_of_element_located((By.ID, "creative-brief")))


def resolved_metrics(driver) -> dict:
    """Every metric that decides where a line breaks, for both elements, as the browser has them."""
    return driver.execute_script("""
      const wanted = arguments[0];
      const read = (node) => {
        const style = getComputedStyle(node);
        return Object.fromEntries(wanted.map((name) => [name, String(style[name])]));
      };
      return {
        box: read(document.querySelector('#creative-brief')),
        mirror: read(document.querySelector('#brief-mirror')),
        boxBackground: getComputedStyle(document.querySelector('#creative-brief')).backgroundColor,
        mirrorBackground: getComputedStyle(document.querySelector('#brief-mirror')).backgroundColor,
        mirrorColour: getComputedStyle(document.querySelector('#brief-mirror')).color,
        markBackground: getComputedStyle(document.querySelector('#brief-mirror-text mark'))
          .backgroundColor,
        ruleBackground: getComputedStyle(document.querySelector('.brief-mark-rule'))
          .backgroundColor,
        ruleWidth: getComputedStyle(document.querySelector('.brief-mark-rule')).width,
        readoutFont: getComputedStyle(document.querySelector('#brief-attribution')).fontFamily,
        readoutSize: getComputedStyle(document.querySelector('#brief-attribution')).fontSize,
        readoutTransform: getComputedStyle(document.querySelector('#brief-attribution'))
          .textTransform,
      };
    """, list(SHARED_METRICS))


def parity(driver) -> dict:
    """The measurement the spec asks for, taken rather than claimed."""
    return driver.execute_script(GEOMETRY + """
      return {
        boxScrollHeight: box.scrollHeight,
        mirrorScrollHeight: mirror.scrollHeight,
        boxClientWidth: box.clientWidth,
        mirrorClientWidth: mirror.clientWidth,
        boxClientHeight: box.clientHeight,
        mirrorClientHeight: mirror.clientHeight,
        boxWidth: boxRect.width,
        mirrorWidth: mirrorRect.width,
        marks: marks.length,
        rules: rules.length,
      };
    """)


def paint(driver) -> dict:
    """Where the wash and the rules actually are, in the mirror's own scrolled coordinates."""
    return driver.execute_script(GEOMETRY + """
      const layerTop = rulesLayer.getBoundingClientRect().top;
      const layerLeft = rulesLayer.getBoundingClientRect().left;
      const rectsOf = (node) => [...node.getClientRects()].map((rect) => ({
        top: rect.top - layerTop, left: rect.left - layerLeft,
        width: rect.width, height: rect.height,
      }));
      return {
        scrollTop: { box: box.scrollTop, mirror: mirror.scrollTop },
        layerTop, mirrorTop: mirrorRect.top,
        marks: marks.map((mark) => ({ text: mark.textContent, rects: rectsOf(mark) })),
        rules: rules.map((rule) => ({
          top: Number.parseFloat(rule.style.top),
          height: Number.parseFloat(rule.style.height),
          left: rule.getBoundingClientRect().left - layerLeft,
          width: rule.getBoundingClientRect().width,
          named: rule.classList.contains('named'),
        })),
        // The mirror's first text node, which is the Director's own lead-in, so "the wash starts
        // at the character and not at the line" can be measured against where that lead-in ends.
        leadWidth: (() => {
          const range = document.createRange();
          range.setStart(body.firstChild, 0);
          range.setEnd(body.firstChild, body.firstChild.data.length);
          const rect = range.getBoundingClientRect();
          return { left: rect.left - layerLeft, right: rect.right - layerLeft };
        })(),
      };
    """)


def readout(driver) -> dict:
    return driver.execute_script("""
      const node = document.querySelector('#brief-attribution');
      const rect = node.getBoundingClientRect();
      return {
        text: node.textContent,
        role: node.getAttribute('role'),
        live: node.getAttribute('aria-live'),
        hidden: node.getAttribute('aria-hidden'),
        box: { width: rect.width, height: rect.height },
        mirrorHidden: document.querySelector('#brief-mirror').getAttribute('aria-hidden'),
      };
    """)


def put_caret(driver, offset: int) -> None:
    """Move the caret with a real gesture, so a bound handler is what runs.

    `setSelectionRange` alone fires nothing -- it would prove the label function works and say
    nothing about whether anything calls it, which is the survivor this repository keeps
    recording. So the caret is placed one character short and then walked onto its target with an
    arrow key, which is a real `keyup` on the real control.
    """
    box = driver.find_element(By.ID, "creative-brief")
    box.click()
    driver.execute_script(
        "arguments[0].focus(); arguments[0].setSelectionRange(arguments[1], arguments[1]);",
        box, max(offset - 1, 0),
    )
    box.send_keys(Keys.ARROW_RIGHT)


def main() -> None:
    port = 8790
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    result: dict[str, object] = {}
    with ManagedServer(port, label=NAME) as server:
        result["server_identity"] = server.evidence
        project = post_json(
            f"{server.base_url}/api/projects", {"name": "Brief attribution browser QA"}
        )
        project_id = project["id"]
        seed_attribution(server, project_id)
        served = get_json(f"{server.base_url}/api/projects/{project_id}")
        assert served["brief_attribution"] == [MARK_ONE, MARK_TWO], served["brief_attribution"]
        result["ranges_reach_the_browser"] = served["brief_attribution"]

        driver = edge_driver(width=1600, height=1100)
        wait = WebDriverWait(driver, 25)
        try:
            driver.get(server.base_url)
            select_project(wait, project_id)
            open_brief(driver, wait)
            box = driver.find_element(By.ID, "creative-brief")
            visible_and_clickable(driver, box, "the Brief editor")
            wait.until(
                lambda browser: browser.execute_script(
                    "return document.querySelectorAll('#brief-mirror-text mark').length;"
                ) == 2
            )

            # --- 1. The mirror is the textarea's twin, metric for metric -----------------------
            # Compared pair by pair as the browser resolves them, because two of the values that
            # matter most are declared nowhere for the textarea and are the user agent's.
            metrics = resolved_metrics(driver)
            differing = {
                name: (metrics["box"][name], metrics["mirror"][name])
                for name in SHARED_METRICS
                if metrics["box"][name] != metrics["mirror"][name]
            }
            # The border colour is deliberately different -- the textarea's is visible and the
            # mirror's is transparent -- but its *width* is in the compared set, because a border
            # of a different width moves the content box and re-wraps every line.
            assert not differing, ("the mirror and the box disagree about how text lays out",
                                  differing)
            assert metrics["box"]["whiteSpace"] == "pre-wrap", metrics["box"]["whiteSpace"]
            assert metrics["box"]["overflowY"] == "scroll", metrics["box"]["overflowY"]
            # The box's own background is given up, or the mirror is behind an opaque wall.
            assert metrics["boxBackground"] == "rgba(0, 0, 0, 0)", metrics["boxBackground"]
            assert metrics["mirrorBackground"] != "rgba(0, 0, 0, 0)", metrics["mirrorBackground"]
            # And the mirror's own text is invisible: the text on screen is the textarea's, and
            # two copies of it a fraction of a pixel apart is what a Director would see instead.
            assert metrics["mirrorColour"] == "rgba(0, 0, 0, 0)", metrics["mirrorColour"]
            result["shared_metrics"] = metrics["box"]

            # No accent (DESIGN 3): the wash is `--surface-1` and the rule is `--line-strong`,
            # resolved by the browser off `:root` rather than compared against a hex typed here.
            palette = driver.execute_script(
                "const style = getComputedStyle(document.documentElement);"
                "const probe = document.createElement('span');"
                "document.body.append(probe);"
                "const resolve = (token) => { probe.style.color ="
                "  style.getPropertyValue(token).trim(); return getComputedStyle(probe).color; };"
                "const answer = { surface1: resolve('--surface-1'),"
                "  lineStrong: resolve('--line-strong'), muted: resolve('--muted'),"
                "  acid: resolve('--acid'), amber: resolve('--amber'), cyan: resolve('--cyan') };"
                "probe.remove(); return answer;"
            )
            washed = metrics["markBackground"].replace("rgba", "rgb").replace(", 1)", ")")
            assert washed == palette["surface1"], (washed, palette)
            assert metrics["ruleBackground"] == palette["lineStrong"], (metrics, palette)
            assert metrics["ruleWidth"] == "2px", metrics["ruleWidth"]
            for accent in ("acid", "amber", "cyan"):
                assert metrics["markBackground"] != palette[accent], accent
                assert metrics["ruleBackground"] != palette[accent], accent
            # The Consolas micro-label, the established form.
            assert "Consolas" in metrics["readoutFont"], metrics["readoutFont"]
            assert metrics["readoutSize"] == "9px", metrics["readoutSize"]
            assert metrics["readoutTransform"] == "uppercase", metrics["readoutTransform"]
            result["neutral_treatment"] = {
                "wash": metrics["markBackground"], "rule": metrics["ruleBackground"],
                "surface_1": palette["surface1"], "line_strong": palette["lineStrong"],
            }

            # --- 2. Metric parity, measured at two window sizes -------------------------------
            # "Wrapping shows up in height": a mirror that wraps differently from the box has a
            # different scrollHeight, and the Brief above is long enough to wrap many times.
            wide = parity(driver)
            assert wide["marks"] == 2 and wide["rules"] == 2, wide
            assert wide["boxScrollHeight"] > wide["boxClientHeight"], (
                "the fixture Brief does not overflow its box, so nothing here measures a scroll",
                wide,
            )
            assert wide["mirrorScrollHeight"] == wide["boxScrollHeight"], (
                "the mirror and the box wrap differently at 1600px", wide)
            assert wide["mirrorClientWidth"] == wide["boxClientWidth"], wide

            driver.set_window_size(1040, 760)
            wait.until(
                lambda browser: parity(browser)["boxClientWidth"] != wide["boxClientWidth"]
            )
            narrow = parity(driver)
            assert narrow["mirrorScrollHeight"] == narrow["boxScrollHeight"], (
                "the mirror and the box wrap differently after the window was resized", narrow)
            assert narrow["mirrorClientWidth"] == narrow["boxClientWidth"], narrow
            # And the resize really re-wrapped, or the assertion above is about nothing.
            assert narrow["boxScrollHeight"] != wide["boxScrollHeight"], (
                ("the viewport resize did not change the wrapping, so the second parity check "
                 "is vacuous -- pick window sizes that really re-wrap"), wide, narrow)
            result["scroll_height_parity"] = {"wide": wide, "narrow": narrow}
            driver.save_screenshot(str(artifact_dir() / f"{NAME}-narrow.png"))

            driver.set_window_size(1600, 1100)
            wait.until(
                lambda browser: parity(browser)["boxClientWidth"] == wide["boxClientWidth"]
            )

            # --- 3. The wash starts at the character, and one rule runs the whole block -------
            drawn = paint(driver)
            first = drawn["marks"][0]
            assert first["text"].startswith("## Cast"), first["text"][:40]
            # The mark's first rectangle begins where the Director's `Maybe: ` ends, on the same
            # line -- which is the whole reason the rule is its own measured layer.
            assert abs(first["rects"][0]["left"] - drawn["leadWidth"]["right"]) < 1.5, (
                "the wash did not start at the marked character", first["rects"][0], drawn)
            assert first["rects"][0]["left"] > drawn["leadWidth"]["left"] + 10, (
                "the wash started at the line rather than at the character", drawn)
            # One rule for the whole contribution, not one per visual line: the mark wraps over
            # many lines and contains a blank one, and the bar spans all of it.
            assert len(first["rects"]) >= 4, first["rects"]
            rule = drawn["rules"][0]
            span_top = min(rect["top"] for rect in first["rects"])
            span_bottom = max(rect["top"] + rect["height"] for rect in first["rects"])
            assert abs(rule["top"] - span_top) < 1.5, (rule, span_top)
            assert abs(rule["height"] - (span_bottom - span_top)) < 1.5, (rule, span_bottom)
            assert rule["width"] == 2, rule
            # Down the left edge of the block, not at the mark's own mid-line x.
            assert rule["left"] < first["rects"][0]["left"] - 10, (rule, first["rects"][0])
            result["one_rule_down_the_whole_block"] = {
                "visual_lines": len(first["rects"]), "rule": rule,
                "span": {"top": span_top, "bottom": span_bottom},
            }

            # --- 4. Scrolled to the bottom, the marks are still over their own text ------------
            before = paint(driver)
            driver.execute_script(
                "const box = document.querySelector('#creative-brief');"
                "box.scrollTop = box.scrollHeight;"
                "box.dispatchEvent(new Event('scroll'));"
            )
            after = paint(driver)
            assert after["scrollTop"]["box"] > 0, after["scrollTop"]
            assert after["scrollTop"]["mirror"] == after["scrollTop"]["box"], (
                "the mirror did not follow the box's scroll", after["scrollTop"])
            # The rules layer *is* the mirror's padding box and scrolls with its content, so a
            # mark's position measured against it is invariant: the same characters are under the
            # same wash at the bottom of the Brief as at the top.
            for index, (was, now) in enumerate(zip(before["marks"], after["marks"], strict=True)):
                assert was["text"] == now["text"], index
                assert [round(rect["top"], 2) for rect in was["rects"]] == [
                    round(rect["top"], 2) for rect in now["rects"]], (index, was, now)
            assert before["rules"] == after["rules"], (before["rules"], after["rules"])
            # And on screen it really moved, or nothing was scrolled at all.
            assert after["layerTop"] < before["layerTop"] - 10, (before, after)
            result["marks_hold_through_a_scroll"] = {
                "scrolled_by": before["layerTop"] - after["layerTop"],
                "scroll_top": after["scrollTop"],
            }
            driver.save_screenshot(str(artifact_dir() / f"{NAME}-scrolled.png"))
            driver.execute_script(
                "const box = document.querySelector('#creative-brief');"
                "box.scrollTop = 0; box.dispatchEvent(new Event('scroll'));"
            )

            # --- 5. A Brief full of markup makes characters, never an element ------------------
            injection = driver.execute_script("""
              const mirror = document.querySelector('#brief-mirror');
              return {
                elements: mirror.querySelectorAll('*').length,
                marks: mirror.querySelectorAll('mark').length,
                scripts: mirror.querySelectorAll('script').length,
                images: mirror.querySelectorAll('img').length,
                rules: mirror.querySelectorAll('.brief-mark-rule').length,
                textareas: document.querySelectorAll('textarea').length,
                pageScripts: document.querySelectorAll('script').length,
                mirrorText: document.querySelector('#brief-mirror-text').textContent,
                boxValue: document.querySelector('#creative-brief').value,
              };
            """)
            assert injection["scripts"] == 0 and injection["images"] == 0, injection
            assert injection["marks"] == 2, injection
            # Counted rather than sampled: the mirror's two layer wrappers, one `<mark>` per
            # contribution and one bar per mark are **every** element it may contain, so an
            # element the Brief's own text created would show up here whatever it was spelled as.
            assert injection["elements"] == 2 + injection["marks"] + injection["rules"], injection
            assert "<script>alert('brief')</script>" in injection["mirrorText"], (
                "the Brief's own angle brackets did not arrive as characters")
            assert "</textarea><img src=x onerror=1>" in injection["mirrorText"]
            assert injection["boxValue"] == BRIEF, "the textarea's own text was altered"
            result["injection_is_characters"] = {
                "elements_in_the_mirror": injection["elements"],
                "textareas_on_the_page": injection["textareas"],
                "scripts_on_the_page": injection["pageScripts"],
            }

            # --- 6. The caret names the turn, and `""` says what it is ------------------------
            resting = readout(driver)
            assert resting["role"] == "status", resting
            assert resting["live"] == "polite", resting
            assert resting["hidden"] is None, (
                ("the attribution's only non-visual carrier is itself hidden from assistive "
                 "technology, which is AD-32's sentence delivered backwards"), resting)
            assert resting["mirrorHidden"] == "true", resting
            assert resting["box"]["height"] > 0 and resting["box"]["width"] > 100, resting
            assert "written by the assistant" in resting["text"].lower(), resting["text"]
            assert "2" in resting["text"], resting["text"]

            put_caret(driver, MARK_ONE["start"] + 30)
            named = readout(driver)
            assert named["text"] == "Assistant · turn 2 of this thread", named["text"]
            assert driver.execute_script(
                "return [...document.querySelectorAll('.brief-mark-rule')]"
                ".map((rule) => rule.classList.contains('named'));"
            ) == [True, False], "the caret did not brighten its own mark's rule"

            put_caret(driver, MARK_TWO["start"] + 20)
            unturned = readout(driver)
            assert unturned["text"] == asset_constant("BRIEF_ATTRIBUTION_NO_TURN"), unturned
            assert unturned["text"] != named["text"]
            assert unturned["text"].strip() and "{" not in unturned["text"], unturned

            put_caret(driver, len(BRIEF) - 5)
            outside = readout(driver)
            assert outside["text"] == resting["text"], (outside["text"], resting["text"])
            result["caret_label"] = {
                "resting": resting["text"], "turn": named["text"], "no_turn": unturned["text"],
            }

            put_caret(driver, MARK_ONE["start"] + 30)
            driver.save_screenshot(str(artifact_dir() / f"{NAME}-marked.png"))

            # --- 7. Typing keeps the mirror under the caret, and changes nothing native --------
            box = driver.find_element(By.ID, "creative-brief")
            driver.execute_script(
                "arguments[0].focus(); arguments[0].setSelectionRange("
                "arguments[0].value.length, arguments[0].value.length);", box,
            )
            box.send_keys("A sentence the Director types at the very end.")
            typed = driver.execute_script("""
              return {
                boxValue: document.querySelector('#creative-brief').value,
                mirrorText: document.querySelector('#brief-mirror-text').textContent,
                marks: [...document.querySelectorAll('#brief-mirror-text mark')]
                  .map((mark) => mark.textContent),
                parity: document.querySelector('#brief-mirror').scrollHeight
                  === document.querySelector('#creative-brief').scrollHeight,
                spellcheck: document.querySelector('#creative-brief').spellcheck,
                readOnly: document.querySelector('#creative-brief').readOnly,
                tag: document.querySelector('#creative-brief').tagName,
              };
            """)
            assert typed["boxValue"].endswith("A sentence the Director types at the very end.")
            assert typed["mirrorText"].startswith(typed["boxValue"][:200]), (
                "the mirror is a stale copy of the Brief within one keystroke")
            assert typed["marks"] == [MARKED_ONE, MARKED_TWO], typed["marks"]
            assert typed["parity"], "the mirror and the box disagree after a keystroke"
            # It is still a textarea, still editable, and spellcheck is still the browser's.
            assert typed["tag"] == "TEXTAREA" and typed["readOnly"] is False, typed
            assert typed["spellcheck"] is True, typed
            assert not driver.find_elements(By.CSS_SELECTOR, "[contenteditable]"), (
                "a contenteditable appeared in this application, which AD-32 forbids outright")
            result["typing_keeps_the_mirror"] = {
                "marks": typed["marks"], "parity": typed["parity"],
            }

            # --- 8. Neither of the other two documents grows a mirror -------------------------
            driver.find_element(By.CSS_SELECTOR, '.document-tabs [data-doc="treatment"]').click()
            wait.until(
                lambda browser: browser.find_element(
                    By.CSS_SELECTOR, '[data-doc-panel="treatment"]'
                ).is_displayed()
            )
            for other in ("treatment", "style"):
                assert not driver.find_elements(
                    By.CSS_SELECTOR, f'[data-doc-panel="{other}"] .brief-mirror'
                ), other
                assert not driver.find_elements(
                    By.CSS_SELECTOR, f'[data-doc-panel="{other}"] .brief-attribution'
                ), other
            driver.save_screenshot(str(artifact_dir() / f"{NAME}-treatment-tab.png"))

            # --- 9. Coming back re-measures, which is the tab switch a display:none box needs --
            driver.find_element(By.CSS_SELECTOR, '.document-tabs [data-doc="brief"]').click()
            wait.until(
                lambda browser: browser.find_element(By.ID, "creative-brief").is_displayed()
            )
            wait.until(lambda browser: paint(browser)["rules"][0]["height"] > 0)
            returned = paint(driver)
            assert len(returned["rules"]) == 2, returned["rules"]
            for index, rule in enumerate(returned["rules"]):
                assert rule["height"] > 0, (index, rule)
            assert parity(driver)["mirrorScrollHeight"] == parity(driver)["boxScrollHeight"]
            result["rules_survive_a_tab_round_trip"] = returned["rules"]

            driver.save_screenshot(str(artifact_dir() / f"{NAME}-returned.png"))
            console_gate(driver, NAME, result)
        finally:
            driver.quit()

    report(NAME, result)


if __name__ == "__main__":
    main()
