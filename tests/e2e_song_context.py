"""Browser QA for the song-context editor and the VRAM eject toggle.

Neither had ever been driven in a browser. Both are proven offline by *executing* their deciding
logic against a stub DOM under node -- `songContextCount`, `songContextRestoreAvailable`,
`vramEjectChecked`, `vramEjectNote` and the render that seeds them -- so nothing here re-proves
that logic. What the stub cannot reach is whether the boxes render at all, whether the counters
are on screen beside them, whether the save and restore buttons are where a Director could press
them, whether the browser honours a `disabled` restore, and whether the browser's own confirm
dialog actually appears in front of a destructive save. Those are the assertions below.

The eject toggle is driven for real, which is safe and does not need a GPU: the control writes a
machine preference through its own route, and the setting reaches ComfyUI only through the
pre-submission hook. Nothing here submits anything. The preference file lives under the data root,
and this script's data root is a fresh one it created, so the Director's own stored choice is
never touched -- which it would be if this ran against the default root.

Run from the repo root -- it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_song_context.py [--port 8768]

Assumes: nothing listening on the port (it refuses to reuse a bound one), Microsoft Edge and its
WebDriver installed, and `music_video_producer` importable from this checkout's `src/`. ComfyUI
does not need to be running. No language-model host is needed either: the eject setting is read
and written without one, and the note simply reports that nothing has been submitted yet.
"""

from __future__ import annotations

import contextlib
import json
import math
import struct
import sys
import wave
from pathlib import Path

from e2e_support import (
    ManagedServer,
    StaleServer,
    artifact_dir,
    clipped,
    console_gate,
    edge_driver,
    get_json,
    post_json,
    post_multipart,
    reachable_widths,
    report,
    visible_and_clickable,
    wait_for_toast,
)
from selenium.common.exceptions import (
    NoAlertPresentException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

NAME = "song-context"

LYRICS = "First verse, held one beat too long.\n\nSecond verse, the same corridor."
STYLE = "Amber sodium light, slow, close-miked."


def write_wav(path: Path) -> None:
    rate = 22050
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        frames = bytearray()
        for index in range(rate * 2):
            frames.extend(struct.pack("<h", int(6000 * math.sin(2 * math.pi * 440 * index / rate))))
        target.writeframes(frames)


def seed(base_url: str) -> str:
    """A project with a song and no context at all, built through shipped routes."""
    project = post_json(f"{base_url}/api/projects", {"name": "Song context browser QA"})
    audio = artifact_dir() / "song-context-fixture.wav"
    write_wav(audio)
    post_multipart(
        f"{base_url}/api/projects/{project['id']}/songs/upload",
        {"title": "Corridor (master)", "duration": "2.0"},
        ("file", audio),
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


def stored_song(base_url: str, project_id: str) -> dict:
    return get_json(f"{base_url}/api/projects/{project_id}")["song"]


def set_box(driver, element, text: str) -> None:
    """Put text in a box the way the app hears it: assign, then fire the real `input` event.

    Used only for the oversized paste, where typing eight thousand characters through the
    WebDriver keyboard would take minutes. Every other edit in this script is typed.
    """
    driver.execute_script(
        "arguments[0].value = arguments[1];"
        "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));",
        element,
        text,
    )


def main() -> None:
    port = 8768
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    result: dict[str, object] = {}
    with ManagedServer(port, label=NAME) as server:
        result["server_identity"] = server.evidence
        project_id = seed(server.base_url)

        driver = edge_driver()
        wait = WebDriverWait(driver, 25)
        try:
            driver.get(server.base_url)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="song"]').click()

            # --- A project with no song: the whole block shut, on screen ----------------------
            # The markup ships all five controls disabled and `renderSongContext` is the only thing
            # that opens them. The identity project the server proof created has no song, so this
            # is the shut arm, rendered -- the state a save that could only 404 would come from.
            select_project(wait, str(server.evidence["identity_project"]))
            shut = wait.until(EC.visibility_of_element_located((By.ID, "song-lyrics")))
            assert not shut.is_enabled(), "the lyrics box is open in a project with no song"
            for element_id in ("song-style", "save-song-context", "restore-song-lyrics",
                               "restore-song-style"):
                assert not driver.find_element(By.ID, element_id).is_enabled(), element_id
            result["no_song_shuts_the_block"] = True

            select_project(wait, project_id)

            # --- The editor exists, is reachable, and is open because a song is loaded ---------
            lyrics = wait.until(EC.visibility_of_element_located((By.ID, "song-lyrics")))
            style = driver.find_element(By.ID, "song-style")
            save = driver.find_element(By.ID, "save-song-context")
            restore_lyrics = driver.find_element(By.ID, "restore-song-lyrics")
            restore_style = driver.find_element(By.ID, "restore-song-style")
            for element, what in (
                (lyrics, "the song lyrics box"),
                (style, "the song style box"),
                (save, "the save-song-context button"),
                (restore_lyrics, "the restore-lyrics button"),
                (restore_style, "the restore-style button"),
            ):
                visible_and_clickable(driver, element, what)
            wait.until(lambda browser: browser.find_element(By.ID, "save-song-context").is_enabled())
            assert lyrics.is_enabled() and style.is_enabled(), (
                "the boxes ship disabled and only renderSongContext opens them; they are still shut "
                "with a song loaded"
            )
            assert lyrics.get_attribute("value") == "", lyrics.get_attribute("value")
            assert not restore_lyrics.is_enabled() and not restore_style.is_enabled(), (
                "a restore is offered before any save has displaced anything"
            )
            assert "No previous version" in (restore_lyrics.get_attribute("title") or ""), (
                restore_lyrics.get_attribute("title")
            )

            # --- The counters are on screen beside the boxes and follow real typing -----------
            lyrics_count = driver.find_element(By.ID, "song-lyrics-count")
            style_count = driver.find_element(By.ID, "song-style-count")
            visible_and_clickable(driver, lyrics_count, "the lyrics character counter")
            assert lyrics_count.text.strip() == "0 / 8,000", lyrics_count.text
            assert style_count.text.strip() == "0 / 4,000", style_count.text

            lyrics.send_keys(LYRICS)
            wait.until(
                lambda browser: browser.find_element(By.ID, "song-lyrics-count").text.strip()
                == f"{len(LYRICS):,} / 8,000"
            )
            assert style_count.text.strip() == "0 / 4,000", (
                f"typing into the lyric sheet moved the style counter: {style_count.text}"
            )
            # The import block's own boxes are a different pair of controls in the same workspace,
            # and a crossed selector would send a finished master's lyric sheet into a song being
            # generated. On screen at the same time, and unmoved by what was just typed.
            import_lyrics = driver.find_element(By.ID, "import-lyrics")
            assert import_lyrics.id != lyrics.id, "the two lyric boxes are the same element"
            assert import_lyrics.get_attribute("value") == "", (
                "typing into the song's lyric sheet also filled the import block's"
            )
            assert driver.find_element(By.ID, "import-lyrics-count").text.strip() == "0 / 8,000"
            result["counters_follow_typing"] = lyrics_count.text.strip()

            # --- The save reaches the server and only the two context fields move --------------
            before = stored_song(server.base_url, project_id)
            save.click()
            wait_for_toast(driver, wait, "Song context saved")
            after = stored_song(server.base_url, project_id)
            assert after["lyrics"] == LYRICS, after["lyrics"]
            assert (after["path"], after["duration"], after["source"]) == (
                before["path"], before["duration"], before["source"]
            ), "the save moved the audio, its length or its provenance"
            result["save_reached_the_server"] = True

            # --- One restore opened, the other left shut, in the browser -----------------------
            # The style description was never displaced, so its slot stays empty and its button
            # stays shut. Two buttons in one block sharing a flag is exactly the wiring mistake a
            # rendered check catches and a source read does not.
            wait.until(
                lambda browser: browser.find_element(By.ID, "restore-song-lyrics").is_enabled()
            )
            restore_lyrics = driver.find_element(By.ID, "restore-song-lyrics")
            restore_style = driver.find_element(By.ID, "restore-song-style")
            assert not restore_style.is_enabled(), (
                "saving the lyric sheet opened the style description's restore too; the two "
                "buttons are sharing one flag"
            )
            assert "Swap the lyric sheet back" in (restore_lyrics.get_attribute("title") or ""), (
                restore_lyrics.get_attribute("title")
            )
            visible_and_clickable(driver, restore_lyrics, "the enabled restore-lyrics button")
            restore_style.click()
            assert stored_song(server.base_url, project_id)["caption"] == "", (
                "a disabled restore button still called the route"
            )
            result["restore_is_per_field"] = True

            # The restore is its own inverse: back to the displaced blank, then forward again.
            restore_lyrics.click()
            wait_for_toast(driver, wait, "Lyric sheet")
            wait.until(
                lambda browser: browser.find_element(By.ID, "song-lyrics").get_attribute("value") == ""
            )
            assert stored_song(server.base_url, project_id)["lyrics"] == ""
            assert driver.find_element(By.ID, "song-lyrics-count").text.strip() == "0 / 8,000", (
                "the counter did not follow the restore that emptied the box"
            )
            driver.find_element(By.ID, "restore-song-lyrics").click()
            wait.until(
                lambda browser: browser.find_element(By.ID, "song-lyrics").get_attribute("value")
                == LYRICS
            )
            result["restore_round_trips"] = True

            # --- The destructive save asks, in a dialog the browser actually paints -------------
            # A stubbed `window.confirm` proves the call is made. It cannot prove the browser puts
            # a dialog in front of the Director, or that answering no leaves the text alone.
            lyrics = driver.find_element(By.ID, "song-lyrics")
            lyrics.clear()
            driver.find_element(By.ID, "save-song-context").click()
            try:
                alert = wait.until(EC.alert_is_present())
            except TimeoutException as error:  # pragma: no cover - a real failure, not a skip
                raise AssertionError(
                    "clearing the lyric sheet and saving raised no dialog; the confirmation is "
                    "either not reaching the browser or is being auto-dismissed"
                ) from error
            question = alert.text
            assert "deletes the stored lyric sheet" in question, question
            assert "Restore beside the box swaps it back" in question, question
            alert.dismiss()
            assert stored_song(server.base_url, project_id)["lyrics"] == LYRICS, (
                "answering no to the clearing question deleted the lyric sheet anyway"
            )
            result["clearing_question"] = " ".join(question.split())[:200]

            # --- The over-limit counter says so in words, and the route agrees ------------------
            oversized = "x" * 8_001
            set_box(driver, driver.find_element(By.ID, "song-lyrics"), oversized)
            wait.until(
                lambda browser: "too long to save"
                in browser.find_element(By.ID, "song-lyrics-count").text
            )
            counter = driver.find_element(By.ID, "song-lyrics-count")
            assert "over" in (counter.get_attribute("class") or ""), counter.get_attribute("class")
            over_colour = counter.value_of_css_property("color")
            assert over_colour != driver.find_element(By.ID, "song-style-count").value_of_css_property(
                "color"
            ), "the over-limit counter is not distinguished from a counter within its bound"
            driver.find_element(By.ID, "save-song-context").click()
            refusal = wait_for_toast(driver, wait, "8001")
            assert stored_song(server.base_url, project_id)["lyrics"] == LYRICS, (
                "the route accepted an oversized lyric sheet the counter said it would refuse"
            )
            result["over_limit"] = {
                "counter": counter.text.strip(),
                "route_refusal": " ".join(refusal.split())[:200],
            }

            # Put the box back and save cleanly, so nothing is left unsaved for the tab close --
            # the beforeunload guard would otherwise hold a dialog open at quit.
            set_box(driver, driver.find_element(By.ID, "song-lyrics"), LYRICS)
            style = driver.find_element(By.ID, "song-style")
            style.clear()
            style.send_keys(STYLE)
            driver.find_element(By.ID, "save-song-context").click()
            wait_for_toast(driver, wait, "Song context saved")
            settled = stored_song(server.base_url, project_id)
            assert (settled["lyrics"], settled["caption"]) == (LYRICS, STYLE), settled
            result["style_box_lands_on_caption"] = True

            # --- The VRAM eject toggle, in the topbar -----------------------------------------
            toggle = driver.find_element(By.ID, "vram-eject")
            note = driver.find_element(By.ID, "vram-eject-note")
            wait.until(lambda browser: browser.find_element(By.ID, "vram-eject").is_enabled())
            result["eject_geometry"] = visible_and_clickable(driver, toggle, "the VRAM eject box")
            visible_and_clickable(driver, note, "the VRAM eject note")
            label = driver.find_element(By.ID, "vram-eject-toggle")
            assert "EJECT LLM" in label.text.upper(), label.text
            assert "Release the language model" in (label.get_attribute("title") or "")

            server_state = get_json(f"{server.base_url}/api/vram-eject")
            assert toggle.is_selected() == server_state["enabled"], (
                f"the box shows {toggle.is_selected()} while the server says {server_state}"
            )
            expected_note = (
                "On. Nothing has been submitted yet."
                if server_state["enabled"]
                else "Off. No eject before a render."
            )
            assert note.text.strip() == expected_note, note.text
            assert "no VRAM" not in note.text and "GB" not in note.text, (
                f"the note is reporting a VRAM figure, which is the one thing it must not: {note.text}"
            )
            result["eject_note_clipped"] = clipped(driver, note)

            preferences = server.data_root / "machine-preferences.json"
            assert not preferences.exists(), (
                "a preference file existed before the control was touched; this run is not isolated"
            )

            toggle.click()
            wanted = not server_state["enabled"]
            wait_for_toast(driver, wait, "language model will be")
            wait.until(
                lambda browser: browser.find_element(By.ID, "vram-eject-note").text.strip()
                != expected_note
            )
            flipped = get_json(f"{server.base_url}/api/vram-eject")
            assert flipped["enabled"] is wanted, flipped
            assert flipped["source"] == "director", (
                f"the server did not record the change as the Director's: {flipped}"
            )
            assert driver.find_element(By.ID, "vram-eject").is_selected() is wanted
            assert preferences.is_file(), "the change was applied but never written to disk"
            stored = json.loads(preferences.read_text(encoding="utf-8"))
            assert stored["llm_eject_before_render"] is wanted, stored
            result["eject_flip"] = {"to": wanted, "note": driver.find_element(
                By.ID, "vram-eject-note"
            ).text.strip(), "preference_file": stored}

            # And back, so the toggle is proven in both directions rather than only one.
            driver.find_element(By.ID, "vram-eject").click()
            wait_for_toast(driver, wait, "language model will be")
            wait.until(
                lambda browser: get_json(f"{server.base_url}/api/vram-eject")["enabled"]
                is server_state["enabled"]
            )
            assert driver.find_element(By.ID, "vram-eject").is_selected() is server_state["enabled"]
            result["eject_returned"] = True

            # A project load refreshes this control and must never blank it -- that path is the one
            # place a machine-wide setting could silently disappear from the topbar.
            driver.refresh()
            select_project(wait, project_id)
            wait.until(lambda browser: browser.find_element(By.ID, "vram-eject").is_enabled())
            assert driver.find_element(By.ID, "vram-eject-note").text.strip() == expected_note
            result["eject_survives_reload"] = True

            # --- Where these controls stop being reachable -------------------------------------
            result["reachable_widths"] = {
                "#vram-eject": reachable_widths(driver, "#vram-eject", [1600, 1280, 1024, 820]),
                "#vram-eject-note": reachable_widths(
                    driver, "#vram-eject-note", [1600, 1280, 1024, 820]
                ),
                "#save-song-context": reachable_widths(
                    driver, "#save-song-context", [1600, 1280, 1024, 820]
                ),
            }
            assert result["reachable_widths"]["#vram-eject"]["1280"].startswith("reachable"), (
                result["reachable_widths"]
            )

            driver.save_screenshot(str(artifact_dir() / f"{NAME}.png"))
            # The one SEVERE entry this script means to cause: the oversized save above is driven
            # into the route's own 422 on purpose, and the browser logs every 4xx as SEVERE. It is
            # named here rather than filtered out of the log, and anything else still fails.
            console_gate(driver, NAME, result, expected=["song/context - Failed to load resource"])
        finally:
            # An unanswered dialog would hold `quit` open; dismissing it here is teardown, never
            # a verdict -- every dialog this script cares about is asserted on where it is raised.
            with contextlib.suppress(NoAlertPresentException, WebDriverException):
                driver.switch_to.alert.dismiss()
            driver.quit()
    report(NAME, result)


if __name__ == "__main__":
    try:
        main()
    except StaleServer as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        raise SystemExit(2) from error
