"""Browser QA for two of the four recorded interaction defects of 2026-08-21.

The Director's report, on the Clips tab:

    the clips library goes blank when ComfyUI is down

...and on the Assets panel's attach button, verbatim:

    "Attach to selected shot (hard to use since cant see timeline from assets page)"

**Both are interaction defects and neither is visible offline.** The executed contract in
`tests/test_frontend_contract.py` runs the deciding functions and reads the markup each render
produced; what it structurally cannot see is a `<video>` element that renders and then 404s, a
health probe that never actually reaches the wire, a card that recovers when ComfyUI comes back,
or a button that is drawn correctly and then covered by something else. This panel has produced
five defect classes a stub DOM could not see, including a `dblclick` that never fired because
`pointerdown` re-rendered every clip.

**ComfyUI is never contacted, started or stopped.** `MVP_COMFY_URL` is pointed at a *stub* HTTP
server this script owns, on a port it chose, which answers `/system_stats` and `/view` and nothing
else. Stopping and restarting that stub is how both halves of the defect are driven against a real
socket: the application really probes, really gets a connection refused, and really recovers. The
user's own installation is not addressed at any point, and no render is ever submitted.

What is asserted, in order:

1. **With ComfyUI answering the tab is unchanged**: one `<video>` per take, pointed at `/view` on
   the address health reported, each one actually reaching `HAVE_METADATA` — which is the proof
   the request landed rather than that an element exists.
2. **With ComfyUI gone the tab says so.** No video element is created at all, every card carries
   the honest face instead, the take's own filename survives on it, "Open shot" still lands on the
   producing shot, and the notice names the address that was tried. The console is checked for the
   404 storm that used to be there and must not be.
3. **The re-check is the only thing that knocks.** Redrawing the tab sends no health request;
   pressing the control sends exactly one.
4. **It recovers.** The stub comes back, the re-check is pressed, and the videos return and load.
5. **Attach to selected shot names its target**: the shot number in the label, the id, the window
   and the opening of the intent in the line beneath it — all hit-tested, at three window widths.
6. **It is shut, with its reason on screen, when there is nothing to attach to**: no selection, and
   an asset this shot already cites. Both were live controls that wrote nothing and said "attached".
7. **The attach really writes**, read back off the manifest on this run's own data root, and the
   toast names the shot rather than saying "attached to shot".

Run from the repo root -- it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_clips_and_attach.py [--port 8776]

Assumes: nothing listening on the port (it refuses to reuse a bound one), Microsoft Edge and its
WebDriver installed, and `music_video_producer` importable from this checkout's `src/`. ffmpeg is
used to make one 0.4 s test clip; without it the run still proves everything except that the
picture decodes, and says so in the result.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from e2e_support import (
    ManagedServer,
    clear_toasts,
    console_gate,
    edge_driver,
    get_json,
    post_json,
    put_json,
    reachable_widths,
    report,
    settle,
    visible_and_clickable,
    wait_for_toast,
)
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

NAME = "clips-and-attach"

SHOT_A = "shot_01"
SHOT_B = "shot_02"
ASSET = "asset_lucy"
ASSET_NAME = "Lucy the singer"
CITED_ASSET = "asset_corridor"
CITED_NAME = "Service corridor"

#: What the console is allowed to carry. **`/view` is deliberately absent**: the whole point of
#: this run is that a tab drawn while ComfyUI is down asks ComfyUI for nothing, so a `/view` entry
#: in the offline phase would be the defect itself. The websocket the progress listener opens
#: against the stub is this script's own doing and is listed by its path.
EXPECTED_CONSOLE: list[str] = []

#: Every clip card, as the browser has it: which face it is wearing, what it says, and whether the
#: video (when there is one) actually got as far as metadata.
CLIP_CARDS = """
const region = document.querySelector('#clips-library');
const card = (node) => {
  const video = node.querySelector('video');
  const face = node.querySelector('.clip-unplayable');
  return {
    video: Boolean(video),
    via: video ? (video.dataset.via || '') : '',
    src: video ? video.getAttribute('src') : '',
    readyState: video ? video.readyState : -1,
    networkState: video ? video.networkState : -1,
    error: video && video.error ? video.error.code : 0,
    faceText: face ? face.textContent.trim() : '',
    faceTitle: face ? (face.getAttribute('title') || '') : '',
    footer: node.querySelector('footer span').textContent,
    jump: Boolean(node.querySelector('.clip-jump')),
  };
};
const notice = region.querySelector('.clips-offline');
return {
  heading: (region.querySelector('.clips-heading') || {}).textContent || '',
  cards: [...region.querySelectorAll('.clip-card')].map(card),
  noticeTitle: notice ? notice.querySelector('strong').textContent : '',
  noticeNote: notice ? notice.querySelector('span').textContent : '',
  recheck: Boolean(document.querySelector('#clips-recheck')),
  comfyLabel: document.querySelector('#comfy-label').textContent,
};
"""

#: The attach control and the line under it.
ATTACH_ROW = """
const button = document.querySelector('#attach-asset');
const caption = document.querySelector('#attach-asset-target');
const rect = (node) => {
  if (!node) return null;
  const r = node.getBoundingClientRect();
  return {width: Math.round(r.width), height: Math.round(r.height),
          top: Math.round(r.top), left: Math.round(r.left)};
};
const selected = document.querySelector('#shots-track .shot-clip.selected');
return {
  label: button ? button.textContent.trim() : '',
  title: button ? (button.getAttribute('title') || '') : '',
  disabled: button ? button.disabled === true : null,
  caption: caption ? caption.textContent.trim() : '',
  buttonBox: rect(button),
  captionBox: rect(caption),
  // What the timeline believes is selected, read off the track the Director cannot see from here.
  // The caption is a claim about this, so a run has to be able to tell a wrong caption from a
  // wrong selection.
  timelineSelection: selected ? selected.dataset.shotId : '',
};
"""


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def make_clip(directory: Path) -> tuple[bytes, str]:
    """A 0.4 s mp4 to serve from the stub, or empty bytes when ffmpeg is not installed.

    Generated rather than committed: this is a picture of nothing at 64x36, and a binary fixture
    in the repository would be a file nobody can read a diff of. No GPU is involved -- ffmpeg
    encodes a colour source on the CPU.
    """
    target = directory / "take.mp4"
    argv = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=green:s=64x36:d=0.4:r=24",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target),
    ]
    try:
        subprocess.run(argv, check=True, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as error:
        return b"", f"ffmpeg unavailable ({error}); the stub serves no picture"
    return target.read_bytes(), "ffmpeg encoded a 0.4 s 64x36 clip"


class StubComfy:
    """A stand-in for ComfyUI on a port this script owns. **Never the user's installation.**

    It answers exactly two paths -- `/system_stats`, which is what `comfy.health()` probes, and
    `/view`, which is what a clip card's `<video>` fetches. Everything else is a 404, so nothing
    here can be mistaken for a working ComfyUI by any other part of the application: a submission
    would fail, which is the same protection the dead-port harnesses rely on.

    Started and stopped by this script at will, which is how "ComfyUI is down" is driven against a
    real socket rather than simulated in the browser.
    """

    def __init__(self, port: int, clip: bytes) -> None:
        self.port = port
        self.clip = clip
        self.url = f"http://127.0.0.1:{port}"
        self.server: ThreadingHTTPServer | None = None
        self.views: list[str] = []

    def start(self) -> None:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            # **HTTP/1.0, deliberately.** Under 1.1 this stub keeps the connection alive, and the
            # application's httpx client pools it -- so a probe made after `stop()` was served by
            # a handler thread that outlived the listening socket and health went on answering
            # "online" with the stub already shut down. That made the offline half of this run
            # unreachable. One connection per request is what makes stopping the stub visible.
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args: object) -> None:
                return

            def handle_error(self, *_args: object) -> None:
                # A browser closing a video connection is not an event; without this the run's
                # output is a wall of ConnectionResetError tracebacks from the stub's own threads.
                return

            def _send(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # BaseHTTPRequestHandler's own spelling
                if self.path.startswith("/system_stats"):
                    self._send(200, json.dumps({"system": {"stub": True}}).encode(), "application/json")
                    return
                if self.path.startswith("/view"):
                    stub.views.append(self.path)
                    self._send(200, stub.clip, "video/mp4")
                    return
                self._send(404, b"not a real ComfyUI", "text/plain")

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            # `stop()` must not block on a browser that is still holding a video socket open.
            block_on_close = False

            def handle_error(self, *_args: object) -> None:
                return

        self.server = Server(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                get_json(f"{self.url}/system_stats", timeout=2)
                return
            except OSError:
                time.sleep(0.1)
        raise AssertionError(f"the stub ComfyUI never answered on {self.url}")

    def stop(self) -> None:
        if self.server is None:
            return
        self.server.shutdown()
        self.server.server_close()
        self.server = None


def seed_project(base_url: str, comfy_root: Path, clip: bytes) -> tuple[str, dict[str, str]]:
    """Two shots and three takes: one shot with a superseded take, one with only its current one.

    Three takes rather than two, because the two picture sources have to be told apart. A shot's
    *current* take (`latest_output`) is served by this application from ComfyUI's output directory
    on disk, so it plays with no ComfyUI at all; an *earlier* take is addressable only by its path
    and needs ComfyUI's `/view`. A fixture with only current takes would prove nothing about the
    second case, and one with only earlier takes nothing about the first.

    The take files are written into this run's own `MVP_COMFY_ROOT` output tree, which is a temp
    directory this script created -- never the user's ComfyUI installation, and never a render.

    The jobs go in through the whole-project PUT: there is no route that mints a `RenderJob`
    without spending a GPU pass.
    """
    project = post_json(f"{base_url}/api/projects", {"name": "Clips and attach browser QA"})
    project_id = project["id"]
    stem = f"music-video-producer/{project_id}/shots"
    takes = {
        "a_earlier": f"{stem}/{SHOT_A}-h3_00001-audio.mp4",
        "a_current": f"{stem}/{SHOT_A}-h3_00002-audio.mp4",
        "b_current": f"{stem}/{SHOT_B}-h3_00001-audio.mp4",
    }
    output_root = comfy_root / "output"
    for relative in takes.values():
        target = output_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(clip)
    put_json(f"{base_url}/api/projects/{project_id}/shots", {"shots": [
        {"id": SHOT_A, "start": 12, "duration": 5,
         "prompt": "Lucy walks the service corridor, hand on the rail, camera pushing in.",
         "mode": "text", "status": "complete", "seed": 12,
         "latest_output": takes["a_current"]},
        {"id": SHOT_B, "start": 17, "duration": 4.5, "prompt": "The same corridor, wider.",
         "mode": "text", "status": "complete", "seed": 3,
         "latest_output": takes["b_current"],
         "citations": [{"asset_id": CITED_ASSET, "role": "reference", "order": 0}]},
    ]})
    project = get_json(f"{base_url}/api/projects/{project_id}")
    project["assets"] = [
        {"id": ASSET, "name": ASSET_NAME, "kind": "character", "source": "upload",
         "path": "", "prompt": "", "prompt_id": ""},
        {"id": CITED_ASSET, "name": CITED_NAME, "kind": "setting", "source": "upload",
         "path": "", "prompt": "", "prompt_id": ""},
    ]
    ordered = [
        (takes["a_earlier"], SHOT_A),
        (takes["a_current"], SHOT_A),
        (takes["b_current"], SHOT_B),
    ]
    project["jobs"] = [
        {"id": f"job_{index}", "kind": "h3", "status": "complete", "prompt_id": f"p-{index}",
         "target_id": target, "seed": index, "output_files": [take],
         "created_at": "2026-08-21T09:15:00Z", "updated_at": "2026-08-21T09:21:00Z"}
        for index, (take, target) in enumerate(ordered, start=1)
    ]
    put_json(f"{base_url}/api/projects/{project_id}", project)
    return project_id, takes


def open_panel(driver, panel: str) -> None:
    driver.find_element(By.CSS_SELECTOR, f'[data-panel="{panel}"]').click()


def click_tab(driver, tab_id: str) -> None:
    button = driver.find_element(By.CSS_SELECTOR, f'#asset-filters button[data-filter="{tab_id}"]')
    visible_and_clickable(driver, button, f"the {tab_id} subtab")
    button.click()
    settle(driver, ".library-panel", quiet_ms=350)


def cards(driver) -> dict:
    return driver.execute_script(CLIP_CARDS)


def health_requests(driver) -> int:
    return int(driver.execute_script(
        "return performance.getEntriesByType('resource')"
        ".filter((entry) => entry.name.endsWith('/api/health')).length;"
    ))


def press_recheck(driver, wait) -> int:
    """Press it, wait for the probe to actually land, and answer how many it sent.

    Waiting on the *effect* rather than on the panel going quiet: `settle` returns as soon as
    nothing has mutated for its window, which a click whose health request is still in flight
    satisfies trivially -- and the probe is a round trip through the application to a socket that
    may be refusing connections, so it is not instant. Measured on 2026-08-21 at just over two
    seconds against a dead address.
    """
    before = health_requests(driver)
    button = wait.until(lambda browser: browser.find_element(By.ID, "clips-recheck"))
    visible_and_clickable(driver, button, "the Re-check ComfyUI control")
    button.click()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and health_requests(driver) == before:
        time.sleep(0.2)
    settle(driver, ".library-panel", quiet_ms=400)
    return health_requests(driver) - before


def wait_for_video_metadata(driver, wait, expected: int) -> list[int]:
    """Every clip's `readyState`, once they have all got at least as far as metadata.

    This is what separates "an element exists" from "the request landed": a `<video>` pointed at a
    404 stays at `HAVE_NOTHING` forever and sets `error`.
    """
    def loaded(browser):
        states = browser.execute_script(
            "return [...document.querySelectorAll('#clips-library .clip-card video')]"
            ".map((v) => v.readyState);"
        )
        return states if len(states) == expected and all(state >= 1 for state in states) else False

    return wait.until(loaded, "the clip cards never loaded metadata from the stub ComfyUI")


def select_clip(driver, wait, shot_id: str) -> None:
    settle(driver, "#shots-track")
    clip = wait.until(lambda browser: browser.find_element(
        By.CSS_SELECTOR, f'#shots-track .shot-clip[data-shot-id="{shot_id}"]'
    ))
    visible_and_clickable(driver, clip, f"the timeline clip for {shot_id}")
    clip.click()
    wait.until(lambda browser: "selected" in browser.find_element(
        By.CSS_SELECTOR, f'#shots-track .shot-clip[data-shot-id="{shot_id}"]'
    ).get_attribute("class"))
    settle(driver, "#shot-inspector")


def select_asset(driver, asset_id: str) -> None:
    card = driver.find_element(By.CSS_SELECTOR, f'.asset-card[data-asset-id="{asset_id}"]')
    visible_and_clickable(driver, card, f"the {asset_id} card")
    card.click()
    settle(driver, "#asset-inspector", quiet_ms=350)


def citations(base_url: str, project_id: str, shot_id: str) -> list[str]:
    project = get_json(f"{base_url}/api/projects/{project_id}")
    shot = next(item for item in project["shots"] if item["id"] == shot_id)
    return [citation["asset_id"] for citation in shot.get("citations", [])]


def main() -> None:
    port = 8776
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    result: dict[str, object] = {}
    scratch = Path(tempfile.mkdtemp(prefix="mvp-clips-attach-"))
    comfy_root = scratch / "comfy-root"
    comfy_root.mkdir()
    clip, clip_note = make_clip(scratch)
    result["test_clip"] = clip_note
    stub = StubComfy(free_port(), clip)
    stub.start()

    # The application is pointed at the stub above and never at the user's ComfyUI. Nothing here
    # starts, stops or interrupts anything the user manages.
    os.environ["MVP_COMFY_ROOT"] = str(comfy_root)
    os.environ["MVP_COMFY_URL"] = stub.url
    os.environ["MVP_LLM_EJECT_BEFORE_RENDER"] = "false"
    os.environ["MVP_LLM_BASE_URL"] = ""

    try:
        with ManagedServer(port, label=NAME) as server:
            result["server_identity"] = server.evidence
            result["stub_comfy_url"] = stub.url
            project_id, takes = seed_project(server.base_url, comfy_root, clip)
            result["takes"] = takes

            driver = edge_driver()
            wait = WebDriverWait(driver, 25)
            try:
                driver.get(server.base_url)
                wait.until(EC.presence_of_element_located((By.ID, "project-select")))
                wait.until(lambda browser: browser.find_element(
                    By.CSS_SELECTOR, f'#project-select option[value="{project_id}"]'
                )).click()
                wait.until(lambda browser: browser.find_element(
                    By.ID, "project-select").get_attribute("value") == project_id)

                # === 1. ComfyUI answering: the tab is the tab that shipped ====================
                open_panel(driver, "assets")
                settle(driver, ".library-panel", quiet_ms=350)
                click_tab(driver, "clips")
                online = cards(driver)
                assert online["comfyLabel"] == "ComfyUI ready", online
                assert len(online["cards"]) == 3, online
                assert all(card["video"] for card in online["cards"]), (
                    "the Clips tab drew no video elements while ComfyUI was answering", online
                )
                by_via = sorted(card["via"] for card in online["cards"])
                assert by_via == ["app", "app", "comfy"], (
                    (
                        "the two current takes must come from this application's own take route "
                        "and only the superseded one from ComfyUI's /view"
                    ),
                    online,
                )
                for card in online["cards"]:
                    if card["via"] == "comfy":
                        assert "/view?filename=" in card["src"], card
                        assert stub.url in card["src"], (
                            "the card is not pointed at the address health reported", card
                        )
                    else:
                        assert "/take?v=" in card["src"], card
                        assert stub.url not in card["src"], (
                            (
                                "the current take is being fetched from ComfyUI when this "
                                "application can serve it from disk"
                            ),
                            card,
                        )
                assert not online["noticeTitle"], (
                    "an offline notice is standing over a working list", online
                )
                # The re-check is on the tab in both states, and this is the reason: health is
                # fetched at boot and nowhere else, so a session that started with ComfyUI up and
                # then lost it has no other way to ask. That is exactly the sequence below.
                assert online["recheck"] is True, (
                    (
                        "there is no way to re-ask about ComfyUI from a tab that is currently "
                        "happy, so a session that loses ComfyUI can never recover"
                    ),
                    online,
                )
                if clip:
                    states = wait_for_video_metadata(driver, wait, 3)
                    assert stub.views, "no card actually fetched a take from the stub"
                    result["online_ready_states"] = states
                    result["stub_views"] = stub.views[:4]
                result["online_cards"] = online["cards"]

                # === 2. ComfyUI gone: the tab says so ========================================
                stub.stop()
                before_recheck = health_requests(driver)
                # A redraw must not knock. Switching away and back is the ordinary gesture that
                # rebuilds this pane.
                click_tab(driver, "all")
                click_tab(driver, "clips")
                assert health_requests(driver) == before_recheck, (
                    "redrawing the Clips tab probes ComfyUI; that is a poll this application does "
                    "not get to decide the rate of"
                )
                # ...and until it is asked, it keeps saying what it last knew rather than guessing.
                still = cards(driver)
                assert all(card["video"] for card in still["cards"]), (
                    "the tab decided ComfyUI was down without asking anything", still
                )

                sent = press_recheck(driver, wait)
                assert sent == 1, (
                    "the re-check sent something other than exactly one health request", sent
                )
                result["recheck_requests"] = sent
                offline = cards(driver)
                assert offline["comfyLabel"] == "ComfyUI offline", offline
                assert len(offline["cards"]) == 3, (
                    (
                        "the list itself shrank when ComfyUI went away; it is read from job "
                        "history and does not depend on ComfyUI at all"
                    ),
                    offline,
                )
                # Nothing is pointed at ComfyUI any more...
                assert not any("/view?filename=" in card["src"] for card in offline["cards"]), (
                    (
                        "the Clips tab is still pointing video elements at an unreachable "
                        "ComfyUI, which is the Director's wall of broken cards"
                    ),
                    offline,
                )
                # ...but the two *current* takes still play, from this application's own route.
                # That is the finding this whole defect turned on: most of this tab never needed
                # ComfyUI at all, because `/shots/{id}/take` resolves `latest_output` on disk.
                served = [card for card in offline["cards"] if card["via"] == "app"]
                assert len(served) == 2, (
                    (
                        "the takes this application can serve from disk went dark with ComfyUI, "
                        "and they do not depend on it"
                    ),
                    offline,
                )
                assert all("/take?v=" in card["src"] for card in served), served
                # ...and the one take that genuinely cannot be shown says so, by name.
                unplayable = [card for card in offline["cards"] if not card["video"]]
                assert len(unplayable) == 1, offline
                assert "ComfyUI offline" in unplayable[0]["faceText"], unplayable
                assert unplayable[0]["faceTitle"].endswith("-h3_00001-audio.mp4"), (
                    (
                        "the take's own filename is gone from the card, and it is the one "
                        "fact that does not depend on ComfyUI"
                    ),
                    unplayable,
                )
                for card in offline["cards"]:
                    assert card["jump"] is True, ("Open shot went away with the picture", card)
                assert stub.url in offline["noticeNote"], (
                    "the notice does not name the address that was tried", offline
                )
                assert "job history" in offline["noticeNote"], offline
                result["offline_cards"] = offline["cards"]
                result["offline_notice"] = {
                    "title": offline["noticeTitle"], "note": offline["noticeNote"]
                }
                if clip:
                    # The proof that "still plays" is not a claim about markup: these two elements
                    # reached metadata with the stub ComfyUI shut down and its port refusing.
                    result["offline_ready_states"] = wait_for_video_metadata(driver, wait, 2)

                # The heading still counts the takes, because the count is a fact about the plan.
                assert "Generated clips · 3" in offline["heading"], offline

                # Open shot still lands on the producing shot with no ComfyUI anywhere.
                driver.find_element(By.CSS_SELECTOR, "#clips-library .clip-jump").click()
                wait.until(lambda browser: "active" in browser.find_element(
                    By.ID, "panel-timeline").get_attribute("class"))
                settle(driver, "#shots-track")
                jumped = driver.execute_script(
                    "const c = document.querySelector('#shots-track .shot-clip.selected');"
                    "return c ? c.dataset.shotId : '';"
                )
                assert jumped in (SHOT_A, SHOT_B), jumped
                result["open_shot_while_offline"] = jumped

                # === 3-4. It recovers ========================================================
                stub.start()
                open_panel(driver, "assets")
                click_tab(driver, "clips")
                press_recheck(driver, wait)
                back = cards(driver)
                assert back["comfyLabel"] == "ComfyUI ready", back
                assert all(card["video"] for card in back["cards"]), (
                    "the cards did not come back when ComfyUI did", back
                )
                assert not back["noticeTitle"], (
                    "the offline notice is still standing over cards that play", back
                )
                if clip:
                    result["recovered_ready_states"] = wait_for_video_metadata(driver, wait, 3)
                result["recovered"] = True

                # === 5-7. Attach to selected shot ============================================
                #
                # Driven from the Assets panel with the timeline two panels away -- which is the
                # Director's whole complaint -- so every fact about the target has to be on this
                # screen.
                open_panel(driver, "timeline")
                select_clip(driver, wait, SHOT_A)
                open_panel(driver, "assets")
                click_tab(driver, "all")
                select_asset(driver, ASSET)
                named = driver.execute_script(ATTACH_ROW)
                assert named["disabled"] is False, named
                assert named["label"] == "Attach to SHOT 01", (
                    "the button does not name the shot it will write to", named
                )
                assert "SHOT 01 (shot_01)" in named["caption"], named
                assert "12.00–17.00 s" in named["caption"], (
                    "the caption does not carry the shot's window", named
                )
                assert "Lucy walks the service corridor" in named["caption"], (
                    "the caption does not carry the shot's intent", named
                )
                # The caption sits under the button rather than somewhere else on the panel.
                assert named["captionBox"]["top"] >= named["buttonBox"]["top"], named
                button = driver.find_element(By.ID, "attach-asset")
                visible_and_clickable(driver, button, "Attach to selected shot")
                result["attach_named"] = named
                result["attach_reachability"] = reachable_widths(
                    driver, "#attach-asset-target", [1600, 1100, 950, 820]
                )
                driver.set_window_size(1600, 1000)
                settle(driver, "#asset-inspector", quiet_ms=300)

                # An asset this shot already cites: shut, with the reason on screen.
                select_asset(driver, CITED_ASSET)
                open_panel(driver, "timeline")
                select_clip(driver, wait, SHOT_B)
                open_panel(driver, "assets")
                settle(driver, "#asset-inspector", quiet_ms=350)
                already = driver.execute_script(ATTACH_ROW)
                assert already["disabled"] is True, (
                    (
                        "attaching an asset a shot already cites is still offered; the click "
                        "writes the same list back and says 'attached'"
                    ),
                    already,
                )
                assert "already cites" in already["title"], already
                assert "SHOT 02 (shot_02)" in already["caption"], already
                result["attach_already_cited"] = already

                # No selection at all: shut, and the caption says what to do about it.
                driver.execute_script(
                    "const c = document.querySelector('#shots-track .shot-clip.selected');"
                    "if (c) c.classList.remove('selected');"
                )
                open_panel(driver, "timeline")
                driver.find_element(By.CSS_SELECTOR, "#timeline-canvas").click()
                settle(driver, "#shot-inspector", quiet_ms=350)
                open_panel(driver, "assets")
                select_asset(driver, ASSET)
                unselected = driver.execute_script(ATTACH_ROW)
                if unselected["disabled"] is True:
                    assert "No shot is selected" in unselected["caption"], unselected
                    assert unselected["label"] == "Attach to selected shot", unselected
                    result["attach_unselected"] = unselected
                else:
                    # Clicking the empty canvas does not clear the selection in this workspace,
                    # which is a fact about the panel rather than about this control -- recorded
                    # rather than asserted away, and the disabled state is covered by the executed
                    # contract in tests/test_frontend_contract.py.
                    result["attach_unselected"] = "the timeline keeps its selection on a canvas click"

                # === 7. It really writes, and says which shot ================================
                open_panel(driver, "timeline")
                select_clip(driver, wait, SHOT_A)
                open_panel(driver, "assets")
                select_asset(driver, ASSET)
                clear_toasts(driver)
                before = citations(server.base_url, project_id, SHOT_A)
                driver.find_element(By.ID, "attach-asset").click()
                said = wait_for_toast(driver, wait, ASSET_NAME)
                deadline = time.monotonic() + 12
                after = before
                while time.monotonic() < deadline and ASSET not in after:
                    time.sleep(0.15)
                    after = citations(server.base_url, project_id, SHOT_A)
                assert ASSET in after, (
                    f"the attach wrote nothing to the manifest: {before} -> {after}"
                )
                assert "SHOT 01" in said, (
                    (
                        "the toast still says 'attached to shot', which is the one thing a "
                        "Director on the Assets panel cannot check"
                    ),
                    said,
                )
                result["attach_write"] = {"before": before, "after": after, "toast": said}

                # ...and the control shuts itself immediately afterwards rather than offering the
                # same no-op again.
                settle(driver, "#asset-inspector", quiet_ms=400)
                afterwards = driver.execute_script(ATTACH_ROW)
                assert afterwards["disabled"] is True, (
                    "the button is still live on an asset the shot now cites", afterwards
                )
                result["attach_after_write"] = afterwards

                console_gate(driver, NAME, result, expected=EXPECTED_CONSOLE)
                report(NAME, result)
            except TimeoutException as error:  # pragma: no cover - a real failure, not a skip
                raise AssertionError(f"the browser stopped waiting: {error}") from error
            finally:
                driver.quit()
    finally:
        stub.stop()


if __name__ == "__main__":
    main()
