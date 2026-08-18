"""Browser QA for AD-1's render polling: a completion reaches the screen with no click at all.

The live defect this gates: a Flux character rendered on ComfyUI — twice, identically — while the
asset card said RENDERING forever, because nothing in the frontend ever polled. The offline
harness executes every deciding function (`hasActiveRenderJobs`, `applyRenderStatus`, the tick
itself under a stub DOM), so this script asserts the things a stub DOM structurally cannot see:
that a real `setInterval` really fires in a real browser, that its ticks reach the real route,
that the pixels a Director watches actually change when a render lands, and that the two negative
guarantees hold on the wire — an idle project issues zero polling requests, and a dead ComfyUI
produces no toast spray.

**Nothing reaches `/prompt` and no GPU is spent.** ComfyUI here is a scripted double this script
runs on its own port: `/queue` and `/history` answer from a state machine the script flips, and a
`POST /prompt` is counted as a violation — the run fails if the count is ever non-zero. The app
under test is pointed at the double through `MVP_COMFY_URL`, which `ManagedServer` passes through
the environment; the Director's real ComfyUI is never contacted.

The render job itself is seeded through the full-project PUT rather than the generate route,
because the generate route would submit to `/prompt` — the same fabrication
`tests/e2e_shot_controls.py` records for its pending asset.

Run from the repo root — it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_render_polling.py [--port 8769]

Assumes: nothing listening on the port or the port above it (the ComfyUI double binds port+1),
Microsoft Edge and its WebDriver installed, and `music_video_producer` importable from this
checkout's `src/`.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from e2e_support import (
    ManagedServer,
    StaleServer,
    console_gate,
    get_json,
    post_json,
    put_json,
    report,
    resource_hits,
    wait_for_toast,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

NAME = "render-polling"

#: A 1x1 PNG — the "rendered" output the ComfyUI double serves from /view, so the landed asset
#: card holds an <img> the browser genuinely fetches and paints.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

PROMPT_ID = "e2e-poll-flux-1"
OUTPUT = {"filename": "e2e-singer_00001_.png", "subfolder": "e2e-poll", "type": "output"}


class ComfyDouble:
    """A scripted ComfyUI on its own port: `/queue` and `/history` only, from a state machine.

    Modes, flipped by the script:

    * ``pending``  — the prompt sits in ``queue_pending``; history is empty.
    * ``down``     — ``/queue`` and ``/history`` answer 500, the way a dead server reads to the
      app's httpx client (connection refused is transport-flavoured; a 500 exercises the same
      `ComfyError` path without racing socket teardown).
    * ``complete`` — the queue is empty and ``/history/{PROMPT_ID}`` carries the finished entry.

    Every ``POST /prompt`` is counted as a violation; the run fails if the count moves.
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self.state = {"mode": "pending"}
        self.hits = {"queue": 0, "history": 0, "view": 0, "prompt": 0, "system_stats": 0}
        lock = threading.Lock()
        double = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # quiet
                pass

            def _json(self, payload: dict, status: int = 200) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                with lock:
                    mode = double.state["mode"]
                if path == "/system_stats":
                    double.hits["system_stats"] += 1
                    self._json({"system": {"comfyui_version": "double"}})
                elif path == "/queue":
                    double.hits["queue"] += 1
                    if mode == "down":
                        self._json({"error": "down"}, status=500)
                    elif mode == "pending":
                        self._json(
                            {"queue_running": [], "queue_pending": [[1, PROMPT_ID, {}]]}
                        )
                    else:
                        self._json({"queue_running": [], "queue_pending": []})
                elif path.startswith("/history/"):
                    double.hits["history"] += 1
                    if mode == "down":
                        self._json({"error": "down"}, status=500)
                    elif mode == "complete" and path.endswith(PROMPT_ID):
                        self._json(
                            {
                                PROMPT_ID: {
                                    "status": {
                                        "status_str": "success",
                                        "completed": True,
                                        "messages": [],
                                    },
                                    "outputs": {"9": {"images": [OUTPUT]}},
                                }
                            }
                        )
                    else:
                        self._json({})
                elif path.startswith("/view"):
                    double.hits["view"] += 1
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(PNG)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(PNG)
                else:
                    self._json({"error": f"unexpected GET {path}"}, status=404)

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                if path == "/prompt":
                    # The one thing this script must never cause. Counted, and refused.
                    double.hits["prompt"] += 1
                    self._json({"error": "this QA run must not submit prompts"}, status=500)
                else:
                    self._json({"error": f"unexpected POST {path}"}, status=404)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def set_mode(self, mode: str) -> None:
        self.state["mode"] = mode


def seed(base_url: str) -> dict:
    """Two projects through shipped routes — and, at this stage, no job anywhere.

    The RenderJob is deliberately NOT seeded here: the app auto-loads whichever project the
    list returns first, and the idle-silence measurement below is only a measurement if no
    project in the root can legitimately poll while it runs. `start_render` adds the job when
    the script is ready to watch it.
    """
    idle = post_json(f"{base_url}/api/projects", {"name": "Idle plan (no renders)"})
    active = post_json(f"{base_url}/api/projects", {"name": "Active Flux render"})
    active["assets"] = [
        {
            "name": "Lead singer",
            "kind": "character",
            "path": "",
            "source": "flux-image-gen",
            "prompt": "portrait of the lead singer",
            "prompt_id": PROMPT_ID,
        }
    ]
    active = put_json(f"{base_url}/api/projects/{active['id']}", active)
    asset_id = active["assets"][0]["id"]
    return {"idle": idle["id"], "active": active["id"], "asset": asset_id}


def start_render(base_url: str, project_id: str, asset_id: str) -> None:
    """Fabricate the in-flight render through the full-project PUT.

    The generate route would POST to `/prompt`, which this run forbids, so the job is written
    with exactly the state a real submission leaves behind: an Asset with `prompt_id` and no
    `path`, and a queued RenderJob tied to it — the same fabrication
    `tests/e2e_shot_controls.py` records for its pending asset.
    """
    project = get_json(f"{base_url}/api/projects/{project_id}")
    project["jobs"] = [
        {
            "kind": "flux",
            "status": "queued",
            "prompt_id": PROMPT_ID,
            "target_id": asset_id,
            "seed": 7,
        }
    ]
    put_json(f"{base_url}/api/projects/{project_id}", project)


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


def toast_texts(driver) -> list[str]:
    return [
        item.text
        for item in driver.find_elements(By.CSS_SELECTOR, "#toast-region .toast")
    ]


def main() -> None:
    port = 8769
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    comfy_port = port + 1

    result: dict[str, object] = {}
    double = ComfyDouble(comfy_port)
    double.start()
    previous_comfy_url = os.environ.get("MVP_COMFY_URL")
    os.environ["MVP_COMFY_URL"] = f"http://127.0.0.1:{comfy_port}"
    try:
        with ManagedServer(port, label=NAME) as server:
            result["server_identity"] = server.evidence
            result["comfy_double"] = os.environ["MVP_COMFY_URL"]
            fixture = seed(server.base_url)

            driver = None
            try:
                from e2e_support import edge_driver

                driver = edge_driver()
                wait = WebDriverWait(driver, 25)
                driver.get(server.base_url)

                # --- An idle project issues zero polling requests -------------------------
                select_project(driver, wait, fixture["idle"])
                # Three poll intervals' worth of silence. The claim is a negative, so the
                # window has to be long enough that a scheduled poll would certainly have
                # fired inside it.
                time.sleep(3 * 2.0 + 0.6)
                idle_hits = resource_hits(driver, "/render-status")
                assert idle_hits == 0, (
                    f"an idle project sent {idle_hits} polling request(s); the contract is zero"
                )
                result["idle_project_poll_requests"] = idle_hits

                # --- Selecting the active project starts polling, unasked ----------------
                # The job goes onto the manifest only now, so nothing could poll during the
                # silence measurement whatever project the app auto-loaded first.
                start_render(server.base_url, fixture["active"], fixture["asset"])
                select_project(driver, wait, fixture["active"])
                driver.find_element(By.CSS_SELECTOR, '[data-panel="assets"]').click()
                card = wait.until(
                    EC.presence_of_element_located(
                        (
                            By.CSS_SELECTOR,
                            f'.asset-card[data-asset-id="{fixture["asset"]}"] .asset-thumb',
                        )
                    )
                )
                assert "RENDERING" in card.text, card.text
                wait.until(
                    lambda browser: resource_hits(browser, "/render-status") >= 1,
                    "no polling request went out within 25s of loading a project with an "
                    "open render job",
                )
                result["polling_started_without_click"] = True

                # The job row reads what the double's queue says, reached by the poll alone.
                driver.find_element(By.CSS_SELECTOR, '[data-panel="queue"]').click()
                # Lower-cased: `.job-status` is uppercased by CSS, so the rendered text is
                # "QUEUED" where the manifest says "queued" -- the same trap `.shot-status`
                # documents in e2e_shot_controls.
                wait.until(
                    lambda browser: any(
                        row.text.strip().lower() in {"queued", "running"}
                        for row in browser.find_elements(By.CSS_SELECTOR, "#job-list .job-status")
                    ),
                    "the queue panel never showed the open job's live status",
                )

                # --- A dead ComfyUI degrades quietly: polling continues, nothing toasts ---
                double.set_mode("down")
                hits_when_down = resource_hits(driver, "/render-status")
                time.sleep(2 * 2.0 + 0.6)
                sprayed = toast_texts(driver)
                assert sprayed == [], (
                    f"a dead ComfyUI put toasts on screen every tick: {sprayed}"
                )
                assert resource_hits(driver, "/render-status") > hits_when_down, (
                    "polling stopped while ComfyUI was down; the tick must keep asking"
                )
                result["comfy_down_ticks_were_silent"] = True

                # --- The render lands: every surface updates with no click ---------------
                double.set_mode("complete")
                completion_toast = wait_for_toast(driver, wait, "Render complete")
                result["completion_toast"] = completion_toast
                wait.until(
                    lambda browser: any(
                        row.text.strip().lower() == "complete"
                        for row in browser.find_elements(By.CSS_SELECTOR, "#job-list .job-status")
                    ),
                    "the job row never reached complete",
                )
                driver.find_element(By.CSS_SELECTOR, '[data-panel="assets"]').click()
                image = wait.until(
                    EC.presence_of_element_located(
                        (
                            By.CSS_SELECTOR,
                            f'.asset-card[data-asset-id="{fixture["asset"]}"] .asset-thumb img',
                        )
                    )
                )
                assert OUTPUT["filename"] in (image.get_attribute("src") or ""), (
                    image.get_attribute("src")
                )
                # The browser really fetched and painted the file, not just an <img> tag.
                wait.until(
                    lambda browser: browser.execute_script(
                        "return arguments[0].complete && arguments[0].naturalWidth > 0;", image
                    ),
                    "the landed asset image never painted",
                )
                result["asset_card_landed_without_click"] = True

                # And the server holds what the screen shows.
                stored = get_json(f"{server.base_url}/api/projects/{fixture['active']}")
                assert stored["assets"][0]["path"] == "e2e-poll/e2e-singer_00001_.png"
                assert stored["jobs"][0]["status"] == "complete"
                result["manifest_reconciled"] = True

                # --- The last job settled, so polling stands down -------------------------
                # The settling tick may already be in flight; give it a beat to finish before
                # measuring the silence.
                time.sleep(1.0)
                settled_hits = resource_hits(driver, "/render-status")
                time.sleep(3 * 2.0 + 0.6)
                after = resource_hits(driver, "/render-status")
                assert after == settled_hits, (
                    f"polling continued after the last job settled ({settled_hits} -> {after})"
                )
                result["polling_stopped_after_settle"] = True

                # --- The standing constraint: nothing reached /prompt ---------------------
                assert double.hits["prompt"] == 0, double.hits
                result["comfy_double_hits"] = dict(double.hits)
                assert double.hits["queue"] > 0, "the reconciler never read the double's queue"

                console_gate(driver, NAME, result)
                result["verdict"] = "PASS"
            finally:
                if driver is not None:
                    driver.quit()
    finally:
        double.stop()
        if previous_comfy_url is None:
            os.environ.pop("MVP_COMFY_URL", None)
        else:
            os.environ["MVP_COMFY_URL"] = previous_comfy_url

    report(NAME, result)


if __name__ == "__main__":
    try:
        main()
    except StaleServer as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        sys.exit(2)
