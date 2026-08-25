"""Browser QA for A1: analysing an existing song from the "Snap to" selector.

**Why this script exists at all.** Epic 8's retrospective found that `POST /song/analyze` had no
caller anywhere in the interface and that all five real projects had a song and no analysis — so
the epic's beat markers and beat snapping did nothing on any project the Director already had. It
also found twelve computed absence reasons and two served flags, `measured` and `analysed`, that
reached the Director nowhere: an un-analysed song and a machine with no ffmpeg looked identical on
screen. Both halves are fixed in the selector's own rows.

**And why it exists as a browser script rather than only offline.** The executed contract in
`tests/test_frontend_contract.py` runs `snapSelectorPlan` over every state and drives `app.js`
against a stub DOM, which is why the decisions are well covered. What a stub DOM structurally
cannot see is whether the reason paragraph is painted, whether the button is reachable at its own
centre inside a `<details>` hanging over the timeline, whether a real keyboard press on a checkbox
keeps its focus, and whether the marks actually appear on the waveform afterwards. Epic 8 shipped
two defects that passed every automated gate and were caught only by looking; this story's own
spec says so in as many words — *a control that has not been operated is not verified*.

What is asserted, in order:

1. **The un-analysed state is real, not simulated.** The song is imported (which measures it), and
   then the analysis is taken out of the manifest and the sidecar deleted — which is the state
   every project that predates Epic 8 is actually in.
2. **The rows say what is missing.** The Beats row names the thing and the reason and offers the
   action; the Phrase gaps row names where transcription happens and offers no button that would
   not help; the Playhead row is untouched. Nothing is red, amber, or an error state.
3. **A tick still moves while a reason is displayed**, driven as a real keyboard press, and
   keyboard focus stays on the checkbox that was pressed.
4. **Operating the action measures the song**, and the selector, the beat marker band and the
   served snap targets all reflect it **with no page reload** — proved by a sentinel written into
   `window` before the press and read back after it.
5. **A forced re-measurement of a byte-identical file is not a no-op.** The sidecar is deleted
   with the manifest's record left intact, so the song fingerprint does not move — which is
   exactly what would let both client-side loaders short-circuit and show the cached absence.
6. **A refusal shows the server's own sentence and changes nothing**, driven into the real 404 by
   moving the song's media file out from under it.

It queues nothing, renders nothing, and never touches ComfyUI.

    uv run --with selenium python tests/e2e_song_analysis.py    # default port 8772
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from e2e_support import (
    ManagedServer,
    console_gate,
    edge_driver,
    get_json,
    report,
    settle,
    visible_and_clickable,
)

# The song synthesizer, the project seeder and the manifest reader are shared with the timeline
# script rather than copied. The retrospective already counts three test-audio generators as
# duplication; a fourth -- and a second hand-rolled retry loop around the same atomic rename --
# would be this change adding to a finding it is meant to be closing.
from e2e_timeline_edit import manifest, post_multipart_project, select_project
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

NAME = "song-analysis"

#: Every row of the open panel, as painted: the tick, the words, the reason, the action, and the
#: colour the stylesheet actually resolved for the reason — because "absence is not an error" is a
#: claim about pixels, and a token that had been swapped for `--red` would read identically in the
#: source.
PANEL_STATE = """
const list = document.querySelector('#snap-target-kinds');
if (!list) return null;
const probe = document.createElement('span');
document.body.appendChild(probe);
const resolve = (token) => {
  probe.style.color = 'var(' + token + ')';
  return getComputedStyle(probe).color;
};
const palette = {
  red: resolve('--red'), amber: resolve('--amber'),
  muted: resolve('--muted'), dim: resolve('--dim'),
};
probe.remove();
const rows = [...list.querySelectorAll('.snap-kind-row')].map((row) => {
  const box = row.querySelector('input[data-kind]');
  const reason = row.querySelector('.control-reason');
  const button = row.querySelector('button[data-snap-action]');
  return {
    kind: box ? box.dataset.kind : null,
    checked: box ? box.checked : null,
    text: row.textContent.replace(/\\s+/g, ' ').trim(),
    reason: reason ? reason.textContent.trim() : '',
    reason_colour: reason ? getComputedStyle(reason).color : '',
    described_by: box ? box.getAttribute('aria-describedby') : null,
    action: button ? {
      id: button.id,
      action: button.dataset.snapAction,
      label: button.textContent.trim(),
      // The DOM's own `disabled`, which must stay false -- a browser blurs a focused element
      // the moment it is set, so the press that starts the measurement would take the keyboard
      // Director's place away. The state is announced by `aria-disabled` instead.
      disabled: button.disabled,
      aria_disabled: button.getAttribute('aria-disabled'),
      // What the browser will actually deliver a press to. A handler reading `event.target`
      // stops firing the moment the button has any child at all.
      child_tags: [...button.children].map((node) => node.tagName.toLowerCase()),
    } : null,
  };
});
return {
  palette,
  rows,
  open: document.querySelector('#snap-targets').open,
  summary: document.querySelector('#snap-targets-summary').getAttribute('aria-label'),
  marks: document.querySelectorAll('#beat-band > *').length,
  // A `<span role="group">` permits phrasing content only, so a `<div>` or a `<p>` written into
  // it is invalid markup that browsers repair differently. Read as the browser parsed it, which
  // is the only place that can be checked.
  flow_content: list.querySelectorAll('div, p').length,
  live: list.getAttribute('aria-live'),
};
"""


def manifest_path(server: ManagedServer, project_id: str) -> Path:
    return server.data_root / "projects" / project_id / "project.json"


def write_manifest(server: ManagedServer, project_id: str, payload: dict) -> None:
    manifest_path(server, project_id).write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def sidecar(server: ManagedServer, project_id: str, relative: str) -> Path:
    return server.data_root / "projects" / project_id / relative


def targets(server: ManagedServer, project_id: str) -> dict:
    return get_json(f"{server.base_url}/api/projects/{project_id}/timeline/snap-targets")


def open_panel(driver) -> None:
    if not driver.find_element(By.ID, "snap-targets").get_attribute("open"):
        driver.find_element(By.ID, "snap-targets-summary").click()
    settle(driver, "#snap-target-kinds", quiet_ms=250)


def panel(driver) -> dict:
    return driver.execute_script(PANEL_STATE)


def row_of(state: dict, kind: str) -> dict:
    found = [row for row in state["rows"] if row["kind"] == kind]
    assert found, (f"the {kind} row is not in the panel at all", state["rows"])
    return found[0]


#: The Beats row's action, by the id `snapActionControl` builds. Named for the row it belongs to
#: rather than for the thing it does, so it cannot be confused with the Song page's `#analyze-song`
#: -- a different act (transcription) with a near-identical id and a different cost.
ACTION_CONTROL = "snap-action-beat"


def await_no_action(driver, wait, what: str) -> None:
    wait.until(
        lambda browser: not browser.find_elements(
            By.CSS_SELECTOR, "#snap-target-kinds button[data-snap-action]"
        ),
        f"{what}: the Beats row still offers the analysis",
    )


def main() -> None:
    port = 8772
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    result: dict[str, object] = {}
    with ManagedServer(port, label=NAME) as server:
        result["server_identity"] = server.evidence

        # --- 1. A real un-analysed project, not a simulated one ------------------------------
        #
        # The upload route measures the song inline, so the project starts *analysed* — which is
        # the one state this feature has nothing to say about. Taking the record out of the
        # manifest and deleting the sidecar puts it in the state every project that predates
        # Epic 8 is genuinely in: a song on disk, no measurement anywhere.
        project_id = post_multipart_project(
            server.base_url, name="Song analysis browser QA", clicks_per_minute=100.0
        )
        imported = targets(server, project_id)
        assert imported["analysed"] is True and imported["beats"], (
            "the click track measured no beats on import, so nothing below would prove anything",
            imported,
        )
        # **Both halves off one read.** The marks the band draws ride with the seconds the drag
        # lands on, so a browser cannot hold a current one and a stale other -- which is what it
        # did until this route grew, and it did it silently.
        assert imported["envelope"] and imported["envelope"]["beats"], (
            "the timeline read carried no measurement, so the beat band has nothing to draw from",
            imported,
        )
        stored = manifest(server, project_id)
        recorded = stored["song"]["analysis"]
        assert recorded and recorded.get("song_fingerprint"), recorded
        envelope_file = sidecar(server, project_id, recorded["path"])
        assert envelope_file.exists(), envelope_file
        result["on_import"] = {
            "analysed": imported["analysed"],
            "beats": len(imported["beats"]),
            "marks_in_the_same_read": len(imported["envelope"]["beats"]),
            "fingerprint": recorded["song_fingerprint"],
            "sidecar_bytes": envelope_file.stat().st_size,
        }

        envelope_file.unlink()
        # An **all-default** record, not a null one. `Song.analysis` is a `SongAnalysis` with every
        # field defaulted, which is precisely how every `project.json` written before Epic 8 loads:
        # the record is present and says nothing. A `null` here is not a state this application can
        # ever be in and would only prove the manifest can be corrupted by hand.
        stored["song"]["analysis"] = {}
        write_manifest(server, project_id, stored)
        unmeasured = targets(server, project_id)
        assert unmeasured["analysed"] is False and unmeasured["beats"] == [], unmeasured
        # Absent together, in one body: there is no reply in which one half is current and the
        # other is not, because there is one computation behind both.
        assert unmeasured["envelope"] is None, unmeasured
        assert unmeasured["measured"] is False and unmeasured["gaps"] == [], (
            "the click track has a transcription, so the gap row would not be absent", unmeasured,
        )
        result["after_stripping_the_analysis"] = unmeasured

        driver = edge_driver()
        wait = WebDriverWait(driver, 30)
        try:
            driver.get(server.base_url)
            select_project(driver, wait, project_id)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            settle(driver, "#snap-target-kinds")

            # --- 2. The rows say what is missing, and offer only the fix this application has --
            open_panel(driver)
            before = panel(driver)
            result["panel_when_unmeasured"] = before
            assert before["marks"] == 0, (
                "the beat band drew marks with no measurement at all", before,
            )
            # The panel is a phrasing-only container, and the browser is the only thing that can
            # be asked whether what went into it parsed as the markup this code wrote.
            assert before["flow_content"] == 0, (
                "flow content was written into a `<span role=\'group\'>`", before,
            )
            assert before["live"] == "polite", (
                "a row whose words change while the panel is open announces nothing", before,
            )
            beat_row = row_of(before, "beat")
            gap_row = row_of(before, "gap")
            playhead_row = row_of(before, "playhead")

            assert "has not been analysed" in beat_row["reason"], beat_row
            assert beat_row["action"], "the Beats row names what is missing and offers no fix"
            assert beat_row["action"]["label"] == "Analyze song", beat_row
            assert beat_row["action"]["id"] == ACTION_CONTROL, beat_row
            # Never the DOM's own `disabled`, which would blur the button on its own press.
            assert beat_row["action"]["disabled"] is False, beat_row
            assert beat_row["action"]["aria_disabled"] == "false", beat_row
            assert beat_row["described_by"] == "snap-reason-beat", (
                "the reason is painted beside the tick but not announced with it", beat_row,
            )

            assert "has not been transcribed" in gap_row["reason"], gap_row
            assert "Song page" in gap_row["reason"], (
                "the Phrase gaps row does not say where transcription happens", gap_row,
            )
            assert gap_row["action"] is None, (
                "the Phrase gaps row offers an analysis that would not produce a phrase gap",
                gap_row,
            )

            assert playhead_row["reason"] == "" and playhead_row["action"] is None, playhead_row

            # Absence is a plain fact. The reason resolves to an inert token, never to the two
            # accents this palette reserves for something being wrong.
            for row in (beat_row, gap_row):
                assert row["reason_colour"] not in (
                    before["palette"]["red"], before["palette"]["amber"],
                ), ("a missing measurement is painted as an error state", row, before["palette"])
                assert row["reason_colour"] in (
                    before["palette"]["muted"], before["palette"]["dim"],
                ), (row, before["palette"])

            # **The collapsed control does not promise what it cannot deliver.** Every kind is
            # ticked by default, and the summary is the one line a Director reads without opening
            # anything -- so on a song nobody has measured it must not go on naming beats and gaps
            # as though a drag would land on them. The ticked kind is marked, not dropped: it is
            # still the Director's own selection, and it is still ticked in the panel.
            assert before["summary"].startswith("Snap to:"), before["summary"]
            for kind in ("gaps", "beats"):
                assert f"{kind} (none)" in before["summary"], (kind, before["summary"])
            assert "playhead (none)" not in before["summary"], before["summary"]
            result["summary_when_unmeasured"] = before["summary"]

            button = driver.find_element(By.ID, ACTION_CONTROL)
            result["action_hit_test"] = visible_and_clickable(
                driver, button, "the Analyze song action on the Beats row"
            )

            # --- 3. A tick still moves, by keyboard, while a reason is displayed --------------
            #
            # The rows are rewritten when the *shape* of the markup changes, and a tick does not
            # change it — which is the guard Story 8.3 put in and this change had to widen without
            # breaking. Driven as a real key press on a focused checkbox, because what has to
            # survive is focus, and a synthetic click would not test it.
            box = driver.find_element(By.ID, "snap-kind-beat")
            driver.execute_script("arguments[0].focus();", box)
            box.send_keys(Keys.SPACE)
            settle(driver, "#snap-target-kinds", quiet_ms=250)
            ticked = driver.execute_script("""
              return {
                focused: document.activeElement ? document.activeElement.id : null,
                checked: document.querySelector('#snap-kind-beat').checked,
                open: document.querySelector('#snap-targets').open,
                reason: !!document.querySelector('#snap-reason-beat'),
                summary: document.querySelector('#snap-targets-summary').getAttribute('aria-label'),
              };
            """)
            result["tick_while_a_reason_is_shown"] = ticked
            assert ticked["checked"] is False, ("the key press did not move the tick", ticked)
            assert ticked["focused"] == "snap-kind-beat", (
                "the rows were rebuilt under the Director's finger and focus was lost", ticked,
            )
            assert ticked["open"] is True, ("the panel closed on a tick", ticked)
            assert ticked["reason"] is True, ("the row lost its reason on a tick", ticked)
            assert "beats" not in ticked["summary"], ticked
            # Put it back, so the measurement below lands on a kind the drag will actually use.
            box.send_keys(Keys.SPACE)
            settle(driver, "#snap-target-kinds", quiet_ms=250)
            assert driver.find_element(By.ID, "snap-kind-beat").is_selected()

            # --- 4. Operating it measures the song, with no reload ---------------------------
            #
            # The sentinel is the whole proof of "no page reload": a `window` property does not
            # survive a navigation, so reading it back after the press is what tells a repaint
            # apart from a reload that happens to look the same.
            driver.execute_script("window.__a1Sentinel = 'held before the press';")
            pressed = driver.find_element(By.ID, ACTION_CONTROL)
            # Focused first, so the press is the keyboard Director's press: on success the row
            # loses its reason and its button, the rows are rebuilt, and the element under their
            # finger stops existing. Where focus lands after that is the whole question.
            driver.execute_script("arguments[0].focus();", pressed)
            pressed.click()
            await_no_action(driver, wait, "after the analysis landed")
            settle(driver, "#snap-target-kinds")
            landed_focus = driver.execute_script(
                "return document.activeElement ? document.activeElement.id : null;"
            )
            result["focus_after_the_action"] = landed_focus
            assert landed_focus == "snap-kind-beat", (
                ("the button the Director activated was removed with no focus restoration, so "
                 "the next Tab starts again at the top of the document"),
                landed_focus,
            )
            measured_panel = panel(driver)
            result["panel_after_analysing"] = measured_panel
            assert driver.execute_script("return window.__a1Sentinel;") == "held before the press", (
                "the page reloaded, so nothing above proves the selector repainted itself"
            )
            assert row_of(measured_panel, "beat")["reason"] == "", measured_panel
            assert row_of(measured_panel, "beat")["action"] is None, measured_panel
            # The gap row is untouched by an analysis that produces no phrase gaps.
            assert "has not been transcribed" in row_of(measured_panel, "gap")["reason"]
            assert measured_panel["marks"] > 0, (
                "the beat marker band did not draw the measurement that was just taken",
                measured_panel,
            )
            assert "beats (none)" not in measured_panel["summary"], measured_panel["summary"]
            # The half that still cannot pull is still marked, so the line is not simply cleared.
            assert "gaps (none)" in measured_panel["summary"], measured_panel["summary"]

            served = targets(server, project_id)
            assert served["analysed"] is True and served["beats"], served
            # The marks on the band above and the beats the drag will land on came from this one
            # read, so the panel's evidence and the waveform's cannot describe different states.
            assert served["envelope"]["beats"], served
            remeasured = manifest(server, project_id)["song"]["analysis"]
            assert remeasured and remeasured["song_fingerprint"] == recorded["song_fingerprint"]
            assert sidecar(server, project_id, remeasured["path"]).exists()
            result["after_operating_the_action"] = {
                "analysed": served["analysed"],
                "beats": len(served["beats"]),
                "marks_on_the_band": measured_panel["marks"],
                "fingerprint": remeasured["song_fingerprint"],
            }

            # --- 5. A forced re-measurement of a byte-identical file is not a no-op ----------
            #
            # **The trap this story was warned about.** `song_fingerprint` is derived from the
            # song's bytes, so re-measuring the same file answers the same fingerprint — and
            # `snapTargetsIdentity` is built on it, so the one loader returns early when its key
            # has not moved. Deleting the sidecar while leaving the manifest's record alone is
            # exactly that state: the browser is told the song has never changed, and must still
            # show the new measurement rather than the cached one.
            sidecar(server, project_id, remeasured["path"]).unlink()
            driver.refresh()
            select_project(driver, wait, project_id)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            settle(driver, "#snap-target-kinds")
            open_panel(driver)
            wait.until(
                lambda browser: browser.find_elements(By.ID, ACTION_CONTROL),
                "the Beats row does not offer the analysis for a deleted sidecar",
            )
            lost = panel(driver)
            assert "has not been analysed" in row_of(lost, "beat")["reason"], lost
            assert lost["marks"] == 0, lost
            driver.execute_script("window.__a1Sentinel = 'held before the forced press';")
            driver.find_element(By.ID, ACTION_CONTROL).click()
            await_no_action(driver, wait, "after the forced re-measurement")
            settle(driver, "#snap-target-kinds")
            forced = panel(driver)
            assert driver.execute_script("return window.__a1Sentinel;") == (
                "held before the forced press"
            ), "the forced re-measurement reloaded the page"
            assert forced["marks"] > 0, (
                "a forced re-measurement of an unchanged file displayed the cached absence",
                forced,
            )
            again = manifest(server, project_id)["song"]["analysis"]
            assert again["song_fingerprint"] == recorded["song_fingerprint"], (
                "the file moved, so this section no longer exercises the trap", again,
            )
            result["forced_re_measurement"] = {
                "fingerprint_unchanged": True,
                "marks_on_the_band": forced["marks"],
                "reason_cleared": row_of(forced, "beat")["reason"] == "",
            }

            # --- 6. A refusal says what the server said, and changes nothing -----------------
            #
            # Driven into `POST /song/analyze`'s real 404 by moving the media file out from under
            # a manifest that still names it — the "song media is gone" state the route answers
            # for, rather than a fault injected into the client.
            audio = server.data_root / "projects" / project_id / manifest(
                server, project_id
            )["song"]["path"]
            moved = audio.with_suffix(".moved")
            sidecar(server, project_id, again["path"]).unlink()
            audio.rename(moved)
            before_refusal = manifest(server, project_id)
            driver.refresh()
            select_project(driver, wait, project_id)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            settle(driver, "#snap-target-kinds")
            open_panel(driver)
            refused_before = panel(driver)
            driver.find_element(By.ID, ACTION_CONTROL).click()
            toast = wait.until(
                lambda browser: next(
                    (item.text for item in browser.find_elements(By.CSS_SELECTOR, ".toast")
                     if item.text.strip()),
                    None,
                ),
                "no toast appeared for the refused analysis",
            )
            settle(driver, "#snap-target-kinds")
            refused_after = panel(driver)
            result["refusal"] = {
                "toast": toast,
                "rows_unchanged": [row["text"] for row in refused_after["rows"]]
                == [row["text"] for row in refused_before["rows"]],
            }
            # The server's own sentence, not a status code and not a paraphrase of one.
            assert "Song media was not found" in toast, toast
            assert manifest(server, project_id) == before_refusal, (
                "a refused analysis changed the manifest"
            )
            assert result["refusal"]["rows_unchanged"], (refused_before, refused_after)
            assert row_of(refused_after, "beat")["action"], (
                "the refusal took the action away, so a Director who fixes the cause cannot retry"
            )
            moved.rename(audio)

            # The two 404s this run drove on purpose: the missing media file, which the master
            # audio element and the waveform both ask for, and the refused analysis itself, which
            # the browser logs as a failed resource. Declared by fragment rather than filtered
            # away, so anything else severe still fails the run.
            console_gate(driver, NAME, result, expected=["media/songs", "song/analyze"])
            report(NAME, result)
        finally:
            driver.quit()


if __name__ == "__main__":
    main()
