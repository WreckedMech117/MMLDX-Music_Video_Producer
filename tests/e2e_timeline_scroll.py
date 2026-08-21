"""Browser QA for the Timeline panel's viewport: horizontal scrolling, the zoom slider, and the
coordinate maths that direct manipulation depends on once a scroll offset exists.

The Director's report, 2026-08-20: *"I cant scroll left or right and i see what i think is a zoom
slider that isnt functional."* Both halves were true and neither was what it looked like.

**Neither is visible to the offline harness, and that is the whole reason this script exists.**
A stub DOM has no layout: it cannot see that a scroll container's horizontal scrollbar was laid
out sixty-one pixels below the bottom of the window, behind two `overflow: hidden` boxes with no
way to reach it, and it cannot see that a control the Director took for a zoom slider was the
master-volume slider in the transport row. `scrollWidth`, `clientWidth`, `getBoundingClientRect`
and `window.innerHeight` are the entire evidence here, and every one of them is a number only a
real engine produces. This project has a recorded incident where substring assertions over
`app.js` let three UI guarantees invert with a green suite; a scroll that does not scroll and a
slider that does not zoom would pass any of them.

What is asserted, in order:

1. **Reachable.** The scroll box overflows horizontally at a realistic plan (30 shots over a
   154.6 s song), and its horizontal scrollbar band -- and the Assembly bar below it -- land
   inside the window rather than past its bottom edge. Measured at three window heights.
2. **It scrolls, by wheel and by scrollbar.** A real wheel gesture over the tracks moves the
   viewport along the song.
3. **The four tracks move together.** SECTIONS, MASTER, SHOTS and REFERENCES share one canvas and
   one offset, so a section box and the clip beneath it hold their alignment to the pixel through
   a scroll and through a zoom. This is the failure the design has to rule out: four independently
   scrolling tracks would drift, and a section marking the chorus would sit over the wrong shots.
4. **The zoom slider zooms**, the buttons still zoom, the two agree, and the anchor holds -- the
   playhead when it is on screen, the centre of the visible band when it is not. Never zero.
5. **Direct manipulation still edits the right shot at a non-zero scroll offset.** A drag and a
   resize, performed with the timeline scrolled ~900 px along, and the *stored* window read back
   from the server afterwards. This is the regression guard for the pixel<->second conversions: a
   half-converted timeline silently edits the wrong shot, which is worse than one that cannot
   scroll at all.
6. **Every control in the bar under the Monitor is reachable at four widths.** The Director's
   follow-up moved the edit tools and the zoom out of the panel heading and down into
   `.timeline-transport` (2026-08-21), which puts eleven controls and two sliders in one row. This
   application has a recorded defect where three controls vanished behind media queries, so each of
   them is hit-tested at 1600/1280/1024/900 px -- painted, inside the viewport, and a click at its
   centre landing on it and not on something over it. The bar wraps; nothing in it is ever hidden.
   Both sliders are checked for a *visible* label, and every icon-only button for an `aria-label`,
   because a tooltip is not an accessible name and a glyph is not a label.

No GPU is spent and nothing reaches `/prompt`: the fixture is shots and sections written through
shipped routes, and every gesture here is a scroll, a zoom or a window edit.

Run from the repo root -- it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_timeline_scroll.py [--port 8769]

Assumes: nothing listening on the port (it refuses to reuse a bound one), Microsoft Edge and its
WebDriver installed, and `music_video_producer` importable from this checkout's `src/`. ComfyUI
does not need to be running and no language-model host is needed.
"""

from __future__ import annotations

import base64
import sys

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
    settle,
    visible_and_clickable,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.actions.wheel_input import ScrollOrigin
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

NAME = "timeline-scroll"

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

# The Director's own working canvas, in shape: 30 shots across a 154.6 s song in 7 sections. Read
# from `project_59f14d19ff10` and rebuilt here through routes -- that project is never touched.
SONG_SECONDS = 154.644898
SHOT_COUNT = 30
SECTION_COUNT = 7
SHOT_SECONDS = SONG_SECONDS / SHOT_COUNT

SHOTS = [
    {
        "id": f"shot_{index:02d}",
        "start": round(index * SHOT_SECONDS, 3),
        "duration": round(SHOT_SECONDS, 3),
        "prompt": f"Shot {index + 1}: the corridor, pushing in.",
        "mode": "text",
        "status": "draft",
    }
    for index in range(SHOT_COUNT)
]
SECTIONS = [
    {
        "label": f"Part {index + 1}",
        "start": round(index * (SONG_SECONDS / SECTION_COUNT), 3),
        "duration": round(SONG_SECONDS / SECTION_COUNT, 3),
        "prompt": "",
    }
    for index in range(SECTION_COUNT)
]

# One number this whole panel's arithmetic offsets by: the width of every track's label gutter.
# `api.js` exports it as `TIMELINE_LABEL_WIDTH` and `.track`'s grid template is where it is real.
LABEL_WIDTH = 90

#: Everything about the viewport that only a layout engine can answer.
GEOMETRY = """
const scroll = document.querySelector('#timeline-scroll');
const canvas = document.querySelector('#timeline-canvas');
const assembly = document.querySelector('#assembly-bar');
const box = scroll.getBoundingClientRect();
// The horizontal scrollbar occupies the band between the box's content bottom and its border
// bottom. A scrollbar the window does not contain is a scrollbar no Director can grab.
const barHeight = box.height - scroll.clientHeight;
return {
  innerHeight: window.innerHeight,
  scrollLeft: scroll.scrollLeft,
  scrollTop: scroll.scrollTop,
  clientWidth: scroll.clientWidth,
  scrollWidth: scroll.scrollWidth,
  clientHeight: scroll.clientHeight,
  scrollHeight: scroll.scrollHeight,
  overflowsHorizontally: scroll.scrollWidth > scroll.clientWidth + 1,
  scrollbarHeight: barHeight,
  scrollbarVisible: barHeight > 0 && box.bottom <= window.innerHeight,
  boxBottom: box.bottom,
  assemblyBottom: assembly ? assembly.getBoundingClientRect().bottom : null,
  canvasWidth: canvas.offsetWidth,
  canvasLeft: canvas.getBoundingClientRect().left,
  zoomLabel: document.querySelector('#zoom-label').textContent,
  zoomSlider: (() => {
    const slider = document.querySelector('#zoom-slider');
    return slider ? {value: slider.value, min: slider.min, max: slider.max} : null;
  })(),
};
"""

#: Where each of the four tracks' content boxes actually is on screen, plus one section box and
#: the clip beneath it. Alignment is a property of these numbers and of nothing else.
ALIGNMENT = """
const tracks = {};
for (const track of document.querySelectorAll('#timeline-canvas .track')) {
  const label = track.querySelector('.track-label').textContent.trim();
  tracks[label] = track.querySelector('.track-content').getBoundingClientRect().left;
}
const pill = document.querySelector('#section-track .section-pill:last-of-type');
const clip = document.querySelector('#shots-track .shot-clip:last-of-type');
const ref = document.querySelector('#refs-track .ref-pill');
return {
  tracks,
  ruler: document.querySelector('#timeline-ruler').getBoundingClientRect().left,
  playhead: document.querySelector('#timeline-playhead').getBoundingClientRect().left,
  lastSection: pill ? pill.getBoundingClientRect().left : null,
  lastClip: clip ? clip.getBoundingClientRect().left : null,
  firstRef: ref ? ref.getBoundingClientRect().left : null,
};
"""

#: The seconds a given screen x lands on, read the way every handler in `app.js` reads it: off the
#: canvas's own rect, which already carries the scroll offset. If a conversion stopped accounting
#: for scroll, this and the clip's own position would part company.
SECONDS_AT = f"""
const canvas = document.querySelector('#timeline-canvas');
return (arguments[0] - canvas.getBoundingClientRect().left - {LABEL_WIDTH}) / arguments[1];
"""


#: Every control the bar under the Monitor carries, and what to call it in a failure.
BAR_CONTROLS = {
    "timeline-start": "the jump-to-start button",
    "timeline-play": "the play button",
    "timeline-time": "the timecode readout",
    "add-shot": "the add-shot button",
    "split-shot": "the split button",
    "duplicate-shot": "the duplicate button",
    "delete-shot": "the delete button",
    "zoom-out": "the zoom-out button",
    "zoom-slider": "the zoom slider",
    "zoom-in": "the zoom-in button",
    "zoom-label": "the zoom percentage",
    "mute-song": "the song mute",
    "mute-video": "the video mute",
    "master-volume": "the master volume slider",
    "timeline-duration": "the master duration readout",
}

#: The ids the bar actually contains right now, so "it is on screen somewhere" cannot be mistaken
#: for "it is in the bar the Director was told to look in".
BAR_CONTAINMENT = """
const bar = document.querySelector('.timeline-transport');
return [...bar.querySelectorAll('[id]')].map((node) => node.id);
"""

BAR_GEOMETRY = """
const bar = document.querySelector('.timeline-transport');
const box = bar.getBoundingClientRect();
return {
  innerWidth: window.innerWidth, innerHeight: window.innerHeight,
  width: box.width, height: box.height, bottom: box.bottom,
  clientWidth: bar.clientWidth, scrollWidth: bar.scrollWidth,
  // Whether it is still one row. Read from its own height rather than from item tops: the items
  // are different heights and are centred, so their tops differ inside a single row.
  oneRow: box.height <= 48,
};
"""

#: What each control announces, and what it paints. `labelText` is the *rendered* text of the
#: `<label for>` that names a slider -- markup alone would not prove it is on screen.
CONTROL_NAMES = """
const out = {};
for (const id of arguments[0]) {
  const node = document.getElementById(id);
  if (!node) { out[id] = null; continue; }
  const label = document.querySelector('label[for="' + id + '"]');
  const style = getComputedStyle(node);
  out[id] = {
    ariaLabel: node.getAttribute('aria-label') || '',
    title: node.getAttribute('title') || '',
    text: (node.textContent || '').trim(),
    className: node.className,
    color: style.color,
    labelText: label ? (label.textContent || '').trim() : '',
    labelVisible: label
      ? label.getBoundingClientRect().width > 0 && getComputedStyle(label).visibility !== 'hidden'
      : false,
  };
}
return out;
"""


def seed(base_url: str) -> str:
    """The plan, built through shipped routes only."""
    project = post_json(base_url + "/api/projects", {"name": "Timeline scroll browser QA"})
    project_id = project["id"]
    put_json(f"{base_url}/api/projects/{project_id}/shots", {"shots": SHOTS})
    put_json(f"{base_url}/api/projects/{project_id}/sections", {"sections": SECTIONS})

    picture = artifact_dir() / "timeline-scroll-source.png"
    picture.write_bytes(PNG)
    project = post_multipart(
        f"{base_url}/api/projects/{project_id}/assets/upload",
        {"name": "Corridor character", "kind": "character"},
        ("file", picture),
    )
    asset_id = project["assets"][0]["id"]
    # One cited shot, so the REFERENCES track draws a pill and its alignment is measurable rather
    # than vacuous. The last shot, because that is the one only a scroll can reach.
    shots = [
        {**shot, "citations": [{"asset_id": asset_id, "role": "reference", "order": 0}]}
        if shot["id"] == SHOTS[-1]["id"] else shot
        for shot in SHOTS
    ]
    put_json(f"{base_url}/api/projects/{project_id}/shots", {"shots": shots})
    return project_id


def stored_shot(base_url: str, project_id: str, shot_id: str) -> dict:
    project = get_json(f"{base_url}/api/projects/{project_id}")
    return next(shot for shot in project["shots"] if shot["id"] == shot_id)


def pixels_per_second(driver) -> float:
    """The scale in force, read from the canvas rather than from `state`.

    `(canvas width - the label gutter) / duration` is the same arithmetic `renderTimeline` used to
    size it, so this is a reading of what was drawn and not a second copy of the intent.
    """
    width = driver.execute_script(
        "return document.querySelector('#timeline-canvas').offsetWidth;"
    )
    return (width - LABEL_WIDTH) / SONG_SECONDS


def scroll_to(driver, offset: int) -> int:
    driver.execute_script(
        "document.querySelector('#timeline-scroll').scrollLeft = arguments[0];", offset
    )
    return driver.execute_script("return document.querySelector('#timeline-scroll').scrollLeft;")


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


def main() -> None:
    port = 8769
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
            select_project(driver, wait, project_id)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            wait.until(
                lambda browser: len(
                    browser.find_elements(By.CSS_SELECTOR, "#shots-track .shot-clip")
                ) == SHOT_COUNT
            )
            settle(driver, "#shots-track")

            # --- 1. The scrollbar is inside the window, at every height -----------------------
            #
            # The defect verbatim: at 1600x1100 the scroll box's bottom edge -- and with it the
            # only horizontal scrollbar in the panel -- was laid out 61 px past `innerHeight`,
            # behind `.timeline-panel { overflow: hidden }` and `.workspace { overflow: hidden }`.
            # Nothing could scroll down to it, so the timeline could not be scrolled across.
            reach: dict[str, object] = {}
            for height in (1100, 950, 820):
                driver.set_window_size(1600, height)
                settle(driver, "#shots-track", quiet_ms=300)
                facts = driver.execute_script(GEOMETRY)
                reach[str(height)] = facts
                assert facts["overflowsHorizontally"], (
                    f"a {SHOT_COUNT}-shot plan over {SONG_SECONDS:.0f}s does not overflow the "
                    f"viewport at all, so this script proves nothing: {facts}"
                )
                assert facts["scrollbarHeight"] > 0, (
                    f"the scroll box has no horizontal scrollbar at height {height}: {facts}"
                )
                assert facts["boxBottom"] <= facts["innerHeight"], (
                    f"the timeline's horizontal scrollbar is laid out "
                    f"{facts['boxBottom'] - facts['innerHeight']:.0f}px below the bottom of a "
                    f"{height}px window, where nothing can reach it: {facts}"
                )
                assert facts["assemblyBottom"] <= facts["innerHeight"], (
                    f"the Assembly bar is off the bottom of a {height}px window: {facts}"
                )
            result["reachable_at_heights"] = reach

            driver.set_window_size(1600, 1100)
            settle(driver, "#shots-track", quiet_ms=300)
            scroll_to(driver, 0)

            # --- 2. A real wheel gesture scrolls along the song --------------------------------
            before = driver.execute_script(GEOMETRY)
            tracks = driver.find_element(By.ID, "shots-track")
            ActionChains(driver).scroll_from_origin(
                ScrollOrigin.from_element(tracks, 0, 0), 0, 400
            ).perform()
            after_wheel = driver.execute_script(GEOMETRY)
            assert after_wheel["scrollLeft"] > before["scrollLeft"], (
                "a wheel over the tracks moved nothing along the song: "
                f"{before['scrollLeft']} -> {after_wheel['scrollLeft']}"
            )
            result["wheel_scroll"] = {
                "from": before["scrollLeft"], "to": after_wheel["scrollLeft"],
            }

            # --- 3. All four tracks move together, and stay aligned ----------------------------
            scroll_to(driver, 0)
            aligned_at_zero = driver.execute_script(ALIGNMENT)
            offset = scroll_to(driver, 900)
            aligned_at_900 = driver.execute_script(ALIGNMENT)

            for name, place in aligned_at_900["tracks"].items():
                assert abs((aligned_at_zero["tracks"][name] - place) - offset) < 1.5, (
                    f"the {name} track did not move with the others: it shifted by "
                    f"{aligned_at_zero['tracks'][name] - place:.1f}px where the viewport moved "
                    f"{offset}px. The four tracks share one time axis and must share one offset."
                )
            assert set(aligned_at_900["tracks"]) == {"SECTIONS", "MASTER", "SHOTS", "REFERENCES"}, (
                aligned_at_900["tracks"]
            )
            # Alignment as the Director reads it: the gap between a section box and the clip under
            # it is a property of the song, and a scroll may not change it by a pixel.
            for view in (aligned_at_zero, aligned_at_900):
                assert view["lastSection"] is not None and view["lastClip"] is not None
                assert view["firstRef"] is not None, "the REFERENCES track drew no pill to align"
            gap_at_zero = aligned_at_zero["lastClip"] - aligned_at_zero["lastSection"]
            gap_at_900 = aligned_at_900["lastClip"] - aligned_at_900["lastSection"]
            assert abs(gap_at_zero - gap_at_900) < 1.5, (
                f"the SECTIONS box and the SHOTS clip beneath it drifted apart across a scroll: "
                f"{gap_at_zero:.1f}px -> {gap_at_900:.1f}px"
            )
            result["alignment"] = {
                "offset": offset, "at_zero": aligned_at_zero, "at_900": aligned_at_900,
            }

            # --- 4. The zoom slider ------------------------------------------------------------
            #
            # The control the Director went looking for and did not find. What was there is
            # `#master-volume`, in the transport row, which is a volume control -- recorded here
            # so the mistake is legible rather than only fixed.
            volume = driver.find_element(By.ID, "master-volume")
            # Pinned on the *accessible* name, not the tooltip: a `title` appears only on hover, and
            # this control's tooltip is now a longer sentence about what it does and about its being
            # session-only. `tests/test_frontend_contract.py` pins that sentence exactly; what
            # matters here is that the running page announces the control by name.
            assert volume.get_attribute("aria-label") == "Master song volume", (
                volume.get_attribute("aria-label")
            )
            slider = driver.find_element(By.ID, "zoom-slider")
            visible_and_clickable(driver, slider, "the timeline zoom slider")
            assert slider.get_attribute("type") == "range", slider.get_attribute("type")

            scroll_to(driver, 0)
            wide_before = pixels_per_second(driver)
            label_before = driver.find_element(By.ID, "zoom-label").text
            # Driven the way a Director drives it: a real click three quarters along the track.
            width = slider.size["width"]
            ActionChains(driver).move_to_element_with_offset(
                slider, int(width * 0.25), 0
            ).click().perform()
            settle(driver, "#shots-track", quiet_ms=300)
            wide_after = pixels_per_second(driver)
            label_after = driver.find_element(By.ID, "zoom-label").text
            assert wide_after > wide_before * 1.05, (
                "dragging the zoom slider up changed the rendered scale by nothing: "
                f"{wide_before:.2f} -> {wide_after:.2f} px/s"
            )
            assert label_after != label_before, (label_before, label_after)
            result["slider_zoom"] = {
                "px_per_second": [wide_before, wide_after],
                "label": [label_before, label_after],
            }

            # The buttons still work, and the slider follows them rather than going stale.
            slider_before = driver.find_element(By.ID, "zoom-slider").get_attribute("value")
            driver.find_element(By.ID, "zoom-out").click()
            settle(driver, "#shots-track", quiet_ms=300)
            button_after = pixels_per_second(driver)
            slider_after = driver.find_element(By.ID, "zoom-slider").get_attribute("value")
            assert button_after < wide_after * 0.95, (wide_after, button_after)
            assert int(slider_after) < int(slider_before), (
                "the zoom slider's thumb did not follow the zoom-out button: "
                f"{slider_before} -> {slider_after}"
            )
            result["button_zoom"] = {
                "px_per_second": [wide_after, button_after],
                "slider": [slider_before, slider_after],
            }

            # --- 4b. The anchor: never zero ----------------------------------------------------
            #
            # The playhead is at 0 and the viewport is 900px along, so the playhead is off screen
            # and the centre of what is visible is the invariant.
            scroll_to(driver, 900)
            scale_before = pixels_per_second(driver)
            centre_x = driver.execute_script(
                "const b = document.querySelector('#timeline-scroll').getBoundingClientRect();"
                "return b.left + b.width / 2;"
            )
            centre_seconds_before = driver.execute_script(SECONDS_AT, centre_x, scale_before)
            driver.find_element(By.ID, "zoom-in").click()
            settle(driver, "#shots-track", quiet_ms=300)
            offset_after_zoom = driver.execute_script(
                "return document.querySelector('#timeline-scroll').scrollLeft;"
            )
            centre_seconds_after = driver.execute_script(
                SECONDS_AT, centre_x, pixels_per_second(driver)
            )
            assert offset_after_zoom > 0, (
                "zooming a 30-shot plan threw the viewport back to the head of the song"
            )
            assert abs(centre_seconds_after - centre_seconds_before) < 0.35, (
                "the second in the middle of the viewport moved across a zoom: "
                f"{centre_seconds_before:.2f}s -> {centre_seconds_after:.2f}s"
            )
            result["zoom_anchor_centre"] = {
                "seconds": [centre_seconds_before, centre_seconds_after],
                "scroll_left": offset_after_zoom,
            }

            # And with the playhead on screen it is the playhead that holds still. Park it at a
            # second the viewport can see, then zoom.
            scroll_to(driver, 0)
            # Moved by the workspace's own keyboard transport -- Shift+Right is one second -- so
            # the playhead arrives where a Director would put it rather than being written into
            # `state` from outside. The slider still holds focus from the zoom section above and
            # would eat the arrows, so focus is dropped first.
            driver.execute_script("document.activeElement && document.activeElement.blur();")
            ActionChains(driver).key_down(Keys.SHIFT).send_keys(
                Keys.ARROW_RIGHT * 6
            ).key_up(Keys.SHIFT).perform()
            settle(driver, "#shots-track", quiet_ms=300)
            assert driver.find_element(By.ID, "timeline-time").text != "00:00:00", (
                "the keyboard transport did not move the playhead, so the anchor assertion below "
                "would be measuring a playhead parked at zero"
            )
            playhead_x_before = driver.execute_script(
                "return document.querySelector('#timeline-playhead').getBoundingClientRect().left;"
            )
            driver.find_element(By.ID, "zoom-in").click()
            settle(driver, "#shots-track", quiet_ms=300)
            playhead_x_after = driver.execute_script(
                "return document.querySelector('#timeline-playhead').getBoundingClientRect().left;"
            )
            assert abs(playhead_x_after - playhead_x_before) < 2.5, (
                "the playhead was on screen and moved across a zoom: "
                f"{playhead_x_before:.1f}px -> {playhead_x_after:.1f}px"
            )
            result["zoom_anchor_playhead"] = {
                "screen_x": [playhead_x_before, playhead_x_after],
            }

            # --- 5. Drag and resize at a NON-ZERO scroll offset --------------------------------
            #
            # The hard constraint. A new scroll container is exactly what breaks mouse coordinate
            # maths, and a half-converted timeline silently edits the wrong shot -- which is worse
            # than one that cannot scroll. Every assertion below reads the *stored* window back
            # from the server, not the browser's copy.
            # Back to roughly the default scale first, so several clips fit the viewport at once
            # and the two gestures below can be run on shots that are not neighbours.
            for _ in range(8):
                if pixels_per_second(driver) <= 16.5:
                    break
                driver.find_element(By.ID, "zoom-out").click()
                settle(driver, "#shots-track", quiet_ms=250)
            offset = scroll_to(driver, 900)
            assert offset >= 800, offset
            settle(driver, "#shots-track", quiet_ms=300)
            scale = pixels_per_second(driver)

            on_screen = driver.execute_script("""
              const box = document.querySelector('#timeline-scroll').getBoundingClientRect();
              const found = [];
              for (const clip of document.querySelectorAll('#shots-track .shot-clip')) {
                const rect = clip.getBoundingClientRect();
                if (rect.left > box.left + 100 && rect.right < box.right - 100 && rect.width > 60) {
                  found.push(clip.dataset.shotId);
                }
              }
              return found;
            """)
            assert len(on_screen) >= 3, (
                f"fewer than three clips are comfortably inside the viewport at a {offset}px "
                f"scroll offset, so the two gestures below would be run on neighbours: {on_screen}"
            )
            # Two shots, deliberately not neighbours. Shots tile the song contiguously, so dragging
            # one right overlaps the next -- legal (overlaps assemble as layers) but it puts the
            # later clip on top of the earlier one's right resize handle, which would make the
            # second gesture here fail for a reason that has nothing to do with scrolling.
            visible = on_screen[0]
            resizing = on_screen[2]
            result["dragged_shot"] = visible
            result["resized_shot"] = resizing

            before_drag = stored_shot(server.base_url, project_id, visible)
            clip = driver.find_element(
                By.CSS_SELECTOR, f'#shots-track .shot-clip[data-shot-id="{visible}"]'
            )
            visible_and_clickable(driver, clip, f"the clip for {visible} at a 900px scroll offset")
            # Read back rather than assumed: the hit test above centres its target, which is
            # itself a scroll. What matters is that the offset is substantial and non-zero when
            # the gesture happens -- a conversion that ignored it would be out by this many pixels.
            drag_offset = driver.execute_script(
                "return document.querySelector('#timeline-scroll').scrollLeft;"
            )
            assert drag_offset > 200, drag_offset
            travel = 60
            ActionChains(driver).click_and_hold(clip).move_by_offset(travel, 0).release().perform()
            settle(driver, "#shots-track")
            after_drag = stored_shot(server.base_url, project_id, visible)

            expected_start = before_drag["start"] + travel / scale
            assert abs(after_drag["start"] - expected_start) < 0.2, (
                f"a {travel}px drag at a {drag_offset}px scroll offset moved {visible} from "
                f"{before_drag['start']:.3f}s to {after_drag['start']:.3f}s, where "
                f"{expected_start:.3f}s was asked for. The pixel-to-seconds conversion is not "
                "accounting for the scroll offset."
            )
            assert abs(after_drag["duration"] - before_drag["duration"]) < 0.01, (
                "a move changed the shot's length", before_drag, after_drag
            )
            result["drag_at_offset"] = {
                "offset": drag_offset, "px_per_second": scale, "travel_px": travel,
                "start": [before_drag["start"], after_drag["start"]],
                "expected_start": expected_start,
            }

            # The right edge, at the same offset. Grabbing the handle by name rather than by a
            # guessed pixel, so a change to the handle's width cannot silently turn this into a
            # move that happens to pass.
            clip = driver.find_element(
                By.CSS_SELECTOR, f'#shots-track .shot-clip[data-shot-id="{resizing}"]'
            )
            handle = clip.find_element(By.CSS_SELECTOR, ".resize-handle.right")
            resize_offset = driver.execute_script(
                "return document.querySelector('#timeline-scroll').scrollLeft;"
            )
            assert resize_offset > 200, resize_offset
            visible_and_clickable(driver, handle, f"the right resize handle of {resizing}")
            before_resize = stored_shot(server.base_url, project_id, resizing)
            stretch = 45
            ActionChains(driver).click_and_hold(handle).move_by_offset(stretch, 0).release().perform()
            settle(driver, "#shots-track")
            after_resize = stored_shot(server.base_url, project_id, resizing)

            expected_duration = before_resize["duration"] + stretch / scale
            assert abs(after_resize["duration"] - expected_duration) < 0.2, (
                f"a {stretch}px edge drag at a {resize_offset}px scroll offset resized {resizing} "
                f"from {before_resize['duration']:.3f}s to {after_resize['duration']:.3f}s, where "
                f"{expected_duration:.3f}s was asked for."
            )
            assert abs(after_resize["start"] - before_resize["start"]) < 0.01, (
                "a right-edge resize moved the shot's start", before_resize, after_resize
            )
            result["resize_at_offset"] = {
                "offset": resize_offset, "stretch_px": stretch,
                "duration": [before_resize["duration"], after_resize["duration"]],
                "expected_duration": expected_duration,
            }

            # --- 6. The bar under the Monitor, at four widths ---------------------------------
            #
            # The Director's follow-up, 2026-08-21: the edit tools and the zoom were "up by the
            # Director Timeline header instead of down just below the [view] window in that bar".
            # Moving them puts eleven controls and two sliders in one row, and this application has
            # a recorded defect where three controls disappeared behind media queries -- so every
            # one of them is hit-tested rather than merely looked up. The bar wraps; nothing in it
            # is allowed to be hidden, squeezed to nothing, or covered.
            bar_widths: dict[str, object] = {}
            # 900 and 820 straddle the 860px breakpoint that stacks the layout, which is where the
            # last three vanishing controls were found. 900 is also where this run measured a
            # *pre-existing* horizontal overflow: the layout's 620px first-column floor plus the
            # 280px inspector needed 912px in a 750px panel, and `overflow: hidden` clipped the
            # song mute out of reach. The floor is 320px now.
            for width in (1600, 1280, 1024, 900, 820):
                driver.set_window_size(width, 1100)
                settle(driver, "#shots-track", quiet_ms=300)
                for control, what in BAR_CONTROLS.items():
                    found = driver.find_elements(By.ID, control)
                    assert found, f"#{control} is not in the document at {width}px wide"
                    visible_and_clickable(driver, found[0], f"{what} at {width}px wide")
                    assert control in driver.execute_script(BAR_CONTAINMENT), (
                        f"#{control} is no longer inside the bar under the Monitor at {width}px"
                    )
                bar_widths[str(width)] = driver.execute_script(BAR_GEOMETRY)
                # It may wrap -- that is the design -- but it may never spill past its own box or
                # past the panel, which is what "hidden by a media query" looked like last time.
                facts = bar_widths[str(width)]
                assert facts["scrollWidth"] <= facts["clientWidth"] + 1, (
                    f"the bar overflows its own width at {width}px instead of wrapping: {facts}"
                )
                assert facts["bottom"] <= facts["innerHeight"], (
                    f"the bar is below the bottom of the window at {width}px: {facts}"
                )
            result["bar_at_widths"] = bar_widths

            # Both sliders carry a *visible* name, and every icon-only button an accessible one.
            # A tooltip is not an accessible name and a glyph is not a label: that confusion is
            # what produced "i see what i think is a zoom slider that isnt functional".
            driver.set_window_size(1600, 1100)
            settle(driver, "#shots-track", quiet_ms=300)
            names = driver.execute_script(
                CONTROL_NAMES,
                ["zoom-slider", "master-volume", "split-shot", "duplicate-shot", "delete-shot",
                 "add-shot"],
            )
            for slider in ("zoom-slider", "master-volume"):
                assert names[slider]["labelText"], (
                    f"#{slider} has no visible label rendered beside it: {names[slider]}"
                )
                assert names[slider]["labelVisible"], (
                    f"#{slider}'s label is in the markup but paints nothing: {names[slider]}"
                )
            assert names["zoom-slider"]["labelText"] != names["master-volume"]["labelText"], (
                "the two sliders in this bar carry the same word, which is the confusion again"
            )
            for control in ("split-shot", "duplicate-shot", "delete-shot"):
                assert names[control]["ariaLabel"], (
                    f"#{control} is an icon button with no accessible name: {names[control]}"
                )
                assert names[control]["ariaLabel"] == names[control]["title"], (
                    f"#{control}'s tooltip and its accessible name say different things: "
                    f"{names[control]}"
                )
            # The destructive one still does not look like its neighbours.
            assert "danger-button" in names["delete-shot"]["className"], names["delete-shot"]
            assert names["delete-shot"]["color"] != names["split-shot"]["color"], (
                "the icon-only Delete paints the same as the other icon buttons"
            )
            result["control_names"] = names

            # Nothing was queued by any of it.
            assert not get_json(f"{server.base_url}/api/projects/{project_id}")["jobs"], (
                "this script queued something, and it is supposed to spend no GPU time at all"
            )

            console_gate(driver, NAME, result)
            report(NAME, result)
        finally:
            driver.quit()


if __name__ == "__main__":
    main()
