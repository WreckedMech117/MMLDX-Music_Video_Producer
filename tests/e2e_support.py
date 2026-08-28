"""Shared scaffolding for the browser QA scripts: a server you can trust, and hit-testing.

Two problems this file exists to solve, both of which cost real time on 2026-08-17.

**A health check that proves *something* is listening proves nothing about *what*.** A server
started for a probe earlier in the day was still bound to the port; the freshly started one lost
the bind and exited, and `/api/health` answered 200 from hour-old code. Every assertion after that
was about a build nobody had.

So no script here is handed a base URL. `ManagedServer` starts the app itself and refuses to
proceed unless all four of these hold:

1. `music_video_producer` resolves to *this* checkout's `src/`, asked of the very interpreter the
   server will run under. An installed copy elsewhere on `sys.path` is the quiet way to serve code
   that is not the code on screen.
2. Nothing is listening on the port before the spawn. Occupied is a hard failure that names the
   process holding it, never a silent reuse -- that reuse is the whole bug.
3. The process listening *after* the spawn is our own child. Read from the OS on Windows, where
   these scripts run; skipped rather than faked elsewhere, and the skip is reported in the result.
4. The server writes into the data root this run created seconds ago. A project is created through
   the API and its manifest must appear under our root on disk. No process that predates this run
   can satisfy that, whatever it is serving.

Any of those failing raises `StaleServer` before a browser is opened.

**A stub DOM cannot see the screen.** The offline harness in `tests/test_frontend_contract.py`
executes the deciding logic, which is why the logic is well covered and why re-proving it in a
browser would add nothing. What it structurally cannot catch is a control that never renders, a
selector that matches nothing, a button hidden by CSS, a handler bound to an element that no longer
exists, or a control that renders and is then covered by something else. `visible_and_clickable`
below is the answer to the last one: it scrolls the control into view and asks the *browser* what
is at the middle of it. A button under an overlay passes every assertion about its own attributes
and fails this one.
"""

from __future__ import annotations

import contextlib
import json
import mimetypes
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Self

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = REPO_ROOT / "test-artifacts"
PACKAGE_INIT = REPO_ROOT / "src" / "music_video_producer" / "__init__.py"

#: Where the isolated data roots go. One per run, named with a nonce, so the identity proof in
#: `ManagedServer` cannot be satisfied by anything that started before this run did.
SCRATCH_ROOT = Path(os.environ.get("TEMP") or os.environ.get("TMPDIR") or ".").resolve()


class StaleServer(RuntimeError):
    """The thing answering on the port is not the thing this script started."""


def artifact_dir() -> Path:
    ARTIFACT_DIR.mkdir(exist_ok=True)
    return ARTIFACT_DIR


def _listening(host: str, port: int, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((host, port)) == 0


def _listener_pid(port: int) -> int | None:
    """The PID listening on `port`, or None when this platform cannot be asked cheaply.

    Windows only, which is where these scripts run. Returning None elsewhere is deliberate: a
    guess would be worse than an admitted gap, and the gap is reported in the result JSON.
    """
    if sys.platform != "win32":
        return None
    try:
        output = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in output.splitlines():
        parts = line.split()
        listening = len(parts) >= 5 and parts[0] == "TCP" and parts[3].upper() == "LISTENING"
        if listening and parts[1].rsplit(":", 1)[-1] == str(port):
            try:
                return int(parts[4])
            except ValueError:
                return None
    return None


def _process_tree(root: int) -> set[int]:
    """`root` and every descendant of it, as the OS sees them right now.

    Needed because the interpreter these scripts run under is a uv trampoline: `Popen` returns the
    trampoline's PID and the real `python run.py` -- the process that binds the port -- is its
    child. A check that demanded the listener *be* our PID rejected our own server, and a
    `terminate()` aimed at the trampoline alone would leave the grandchild holding the port, which
    is the stale-server bug this file exists to prevent.
    """
    found = {root}
    if sys.platform != "win32":
        return found
    try:
        output = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-CimInstance Win32_Process | ForEach-Object "
                    "{ $_.ProcessId.ToString() + ' ' + $_.ParentProcessId.ToString() }"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return found
    children: dict[int, list[int]] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    queue = [root]
    while queue:
        for child in children.get(queue.pop(), []):
            if child not in found:
                found.add(child)
                queue.append(child)
    return found


def _describe_process(pid: int) -> str:
    if sys.platform != "win32":
        return f"PID {pid}"
    script = (
        f"Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' | "
        "ForEach-Object { $_.CreationDate.ToString('s') + ' :: ' + $_.CommandLine }"
    )
    try:
        output = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        output = ""
    return f"PID {pid} ({output})" if output else f"PID {pid}"


class ManagedServer:
    """The app, started by this script, proven to be the code in this checkout.

    Used as a context manager. `base_url` and `data_root` are what the calling script drives.
    """

    def __init__(self, port: int, host: str = "127.0.0.1", label: str = "e2e") -> None:
        self.port = port
        self.host = host
        self.label = label
        self.data_root = SCRATCH_ROOT / f"mvp-{label}-{uuid.uuid4().hex[:12]}"
        self.base_url = f"http://{host}:{port}"
        self.log_path = artifact_dir() / f"{label}-server.log"
        self.process: subprocess.Popen[bytes] | None = None
        self.tree: set[int] = set()
        self.evidence: dict[str, object] = {}

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, exc_type: object, *_rest: object) -> None:
        self.stop(quiet=exc_type is not None)

    # -- startup ---------------------------------------------------------------------------
    def start(self) -> None:
        self._check_package_is_this_checkout()
        self._check_port_is_free()
        self.data_root.mkdir(parents=True, exist_ok=False)
        environment = dict(os.environ)
        environment["MVP_APP_PORT"] = str(self.port)
        environment["MVP_APP_HOST"] = self.host
        environment["MVP_DATA_ROOT"] = str(self.data_root)
        self._log = self.log_path.open("wb")
        self.process = subprocess.Popen(
            [sys.executable, "run.py"],
            cwd=REPO_ROOT,
            env=environment,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )
        # Anything from here on leaves a server running if it raises, and a server left running is
        # exactly the listener the next run must not be tempted to reuse. So it is torn down here
        # rather than by a `__exit__` a failed `__enter__` never reaches.
        try:
            self._await_health()
            self._check_listener_is_our_child()
            self._check_server_owns_our_data_root()
        except BaseException:
            self.stop(quiet=True)
            raise

    def _check_package_is_this_checkout(self) -> None:
        probe = subprocess.run(
            [sys.executable, "-c", "import music_video_producer as m; print(m.__file__)"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if probe.returncode != 0:
            raise StaleServer(
                f"{sys.executable} cannot import music_video_producer, so it cannot serve this "
                f"checkout:\n{probe.stderr.strip()}"
            )
        resolved = Path(probe.stdout.strip()).resolve()
        if resolved != PACKAGE_INIT.resolve():
            raise StaleServer(
                "The interpreter these tests would start the server with imports "
                f"music_video_producer from {resolved}, not from this checkout's "
                f"{PACKAGE_INIT}. Anything it served would be code other than the code under test."
            )
        self.evidence["package_source"] = str(resolved)

    def _check_port_is_free(self) -> None:
        if not _listening(self.host, self.port):
            self.evidence["port_was_free"] = True
            return
        pid = _listener_pid(self.port)
        held = _describe_process(pid) if pid else "an unidentified process"
        raise StaleServer(
            f"{self.host}:{self.port} is already bound by {held}. Refusing to run against it: a "
            "server that answers /api/health proves only that something is listening, not that it "
            "is running this checkout. Stop that process, or pass a different --port."
        )

    def _await_health(self, timeout: float = 60.0) -> None:
        assert self.process is not None
        deadline = time.monotonic() + timeout
        last: str = "no answer yet"
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise StaleServer(
                    f"The server exited with code {self.process.returncode} before it answered. "
                    f"Its output is in {self.log_path}:\n{self._log_tail()}"
                )
            try:
                payload = get_json(f"{self.base_url}/api/health", timeout=5)
            except (urllib.error.URLError, OSError, ValueError) as error:
                last = str(error)
                time.sleep(0.4)
                continue
            self.evidence["health"] = {"app": payload.get("app"), "version": payload.get("version")}
            return
        raise StaleServer(
            f"The server did not answer on {self.base_url} within {timeout:.0f}s ({last}). "
            f"Its output is in {self.log_path}:\n{self._log_tail()}"
        )

    def _check_listener_is_our_child(self) -> None:
        assert self.process is not None
        pid = _listener_pid(self.port)
        if pid is None:
            # Admitted rather than assumed. The data-root proof below still stands on its own.
            self.evidence["listener_pid_checked"] = False
            return
        self.evidence["listener_pid_checked"] = True
        self.tree = _process_tree(self.process.pid)
        if pid not in self.tree:
            raise StaleServer(
                f"{self.host}:{self.port} is served by {_describe_process(pid)}, which is not the "
                f"server this script started (PID {self.process.pid}) nor any child of it. "
                "Something took the port between the free check and the spawn."
            )
        self.evidence["listener_pid"] = pid

    def _check_server_owns_our_data_root(self) -> None:
        """The proof no pre-existing process can fake: write through the API, read from our disk.

        The data root was created by this run under a nonce, so a manifest appearing inside it can
        only have been written by a process that was told about it after this script started.
        """
        nonce = f"server identity {uuid.uuid4().hex[:8]}"
        project = post_json(f"{self.base_url}/api/projects", {"name": nonce})
        manifest = self.data_root / "projects" / project["id"] / "project.json"
        if not manifest.is_file():
            raise StaleServer(
                f"The server on {self.base_url} accepted a project but wrote no manifest under "
                f"{self.data_root}. It is using a different data root, which means it is not the "
                "process this script started."
            )
        stored = json.loads(manifest.read_text(encoding="utf-8"))
        if stored.get("name") != nonce:
            raise StaleServer(
                f"{manifest} does not hold the project this run just created; the server on "
                f"{self.base_url} is not the one writing there."
            )
        self.evidence["identity_project"] = project["id"]
        self.evidence["data_root"] = str(self.data_root)

    # -- teardown --------------------------------------------------------------------------
    def stop(self, quiet: bool = False) -> None:
        """Take the whole process tree down, and prove the port came back.

        The tree, not the process: `Popen` holds a uv trampoline whose child is the server, and
        terminating only the trampoline leaves the server bound -- manufacturing the very stale
        listener the next run would then be tempted to reuse. A port that does not come free is
        therefore reported loudly rather than left for the next script to trip over.
        """
        if self.process is not None and self.process.poll() is None:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
            else:
                self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=20)
        log = getattr(self, "_log", None)
        if log is not None and not log.closed:
            log.close()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not _listening(self.host, self.port, timeout=0.3):
                return
            time.sleep(0.3)
        holder = _listener_pid(self.port)
        message = (
            f"{self.host}:{self.port} is still bound after this run stopped its server"
            + (f" -- {_describe_process(holder)}" if holder else "")
            + ". Kill it before the next run, or that run will be refused."
        )
        if quiet:
            print(f"WARNING: {message}", file=sys.stderr)
            return
        raise StaleServer(message)

    def _log_tail(self, lines: int = 25) -> str:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "(no server log)"
        return "\n".join(text.splitlines()[-lines:])

    def server_log(self) -> str:
        return self._log_tail(lines=10_000)


# -- HTTP helpers, dependency-free on purpose (the app itself ships no client library) --------


def _read(request: urllib.request.Request, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise AssertionError(f"{request.method} {request.full_url} -> {error.code}: {detail}") from error


def get_json(url: str, timeout: float = 30) -> dict:
    return _read(urllib.request.Request(url, method="GET"), timeout)


def post_json(url: str, payload: dict | None = None, timeout: float = 30) -> dict:
    body = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    return _read(request, timeout)


def put_json(url: str, payload: dict, timeout: float = 30) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="PUT"
    )
    return _read(request, timeout)


def post_multipart(url: str, fields: dict[str, str], file: tuple[str, Path], timeout: float = 120) -> dict:
    """One multipart POST, hand-built. `file` is (field name, path on disk)."""
    boundary = f"----mvp{uuid.uuid4().hex}"
    field_name, path = file
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
        )
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field_name}\"; "
        f"filename=\"{path.name}\"\r\nContent-Type: {content_type}\r\n\r\n".encode()
    )
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        url,
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    return _read(request, timeout)


# -- browser helpers -------------------------------------------------------------------------


def edge_driver(width: int = 1600, height: int = 1100):
    from selenium import webdriver

    options = webdriver.EdgeOptions()
    options.add_argument("--headless=new")
    options.add_argument(f"--window-size={width},{height}")
    options.set_capability("ms:loggingPrefs", {"browser": "ALL"})
    return webdriver.Edge(options=options)


#: Asks the browser what is actually painted at the middle of a control, and whether the control
#: is where a Director could reach it. Returns the facts so a script can record them; raises with
#: what was found on top when something else owns that pixel.
HIT_TEST = """
const target = arguments[0];
target.scrollIntoView({block: 'center', inline: 'center'});
const box = target.getBoundingClientRect();
const x = box.left + box.width / 2;
const y = box.top + box.height / 2;
const topmost = document.elementFromPoint(x, y);
const describe = (node) => {
  if (!node) return null;
  const id = node.id ? '#' + node.id : '';
  const cls = node.className && typeof node.className === 'string'
    ? '.' + node.className.trim().split(/\\s+/).join('.') : '';
  return node.tagName.toLowerCase() + id + cls;
};
let owner = topmost;
let contained = false;
while (owner) {
  if (owner === target) { contained = true; break; }
  owner = owner.parentElement;
}
const style = getComputedStyle(target);
return {
  width: box.width,
  height: box.height,
  in_viewport: box.top >= 0 && box.left >= 0
    && box.bottom <= window.innerHeight && box.right <= window.innerWidth,
  topmost: describe(topmost),
  hit: contained,
  display: style.display,
  visibility: style.visibility,
  opacity: style.opacity,
  pointer_events: style.pointerEvents,
};
"""


def visible_and_clickable(driver, element, what: str) -> dict:
    """Assert a Director could see this control and land a click on it. Returns the measurements.

    Deliberately more than `is_displayed()`: Selenium calls an element displayed while another
    element sits on top of it, and "the control renders but something covers it" is one of the
    failure modes a brand-new control carries and the offline harness cannot reach.
    """
    assert element is not None, f"{what} is not in the document at all"
    assert element.is_displayed(), f"{what} is in the document but not displayed"
    facts = driver.execute_script(HIT_TEST, element)
    assert facts["width"] > 0 and facts["height"] > 0, f"{what} has no painted area: {facts}"
    assert facts["in_viewport"], f"{what} is outside the viewport even after scrolling: {facts}"
    assert facts["visibility"] != "hidden", f"{what} is visibility:hidden: {facts}"
    assert float(facts["opacity"]) > 0.05, f"{what} is transparent: {facts}"
    assert facts["pointer_events"] != "none", f"{what} does not take pointer events: {facts}"
    assert facts["hit"], (
        f"{what} renders but a click at its centre would land on {facts['topmost']} instead. "
        "Something is covering it."
    )
    return facts


#: Installs one MutationObserver per selector and remembers when it last fired. Re-installed
#: automatically after a navigation, because `window.__e2eQuiet` goes with the document.
_WATCH = """
const selector = arguments[0];
const root = document.querySelector(selector);
if (!root) return false;
window.__e2eQuiet = window.__e2eQuiet || {};
if (window.__e2eQuiet[selector] && window.__e2eQuiet[selector].root === root) return true;
const record = {at: Date.now(), root};
const observer = new MutationObserver(() => { record.at = Date.now(); });
observer.observe(root, {childList: true, subtree: true, attributes: true, characterData: true});
window.__e2eQuiet[selector] = record;
return true;
"""
_QUIET_FOR = """
const record = (window.__e2eQuiet || {})[arguments[0]];
return record ? Date.now() - record.at : -1;
"""


def settle(driver, selector: str, quiet_ms: int = 700, timeout: float = 25.0) -> None:
    """Wait until nothing has changed inside `selector` for `quiet_ms`.

    The workspace re-renders on replies it does not await. Selecting a clip writes the shot list
    back, and the *reply* to that write reloads readiness, whose reply calls `renderTimeline` --
    which rebuilds the shot inspector's contents a second time, well after the click looked
    finished. Elements found in between go stale, and a script that merely retried would be
    hiding a race rather than waiting for the screen to stop moving.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not driver.execute_script(_WATCH, selector):
            time.sleep(0.1)
            continue
        quiet = driver.execute_script(_QUIET_FOR, selector)
        if quiet >= quiet_ms:
            return
        time.sleep(max(0.05, (quiet_ms - quiet) / 1000))
    raise AssertionError(f"{selector} never stopped re-rendering within {timeout:.0f}s")


def covering_element(driver, element) -> str | None:
    """What sits on top of this control right now, or None when nothing does.

    Written on 2026-08-18 to record an overlap nobody had ruled on yet: `.toast-region` is
    `position: fixed` in the bottom-right corner at `z-index: 50`, which is where the shot
    inspector's own action buttons are, so the toast a control raises sat over the control that
    raised it. The ruling since is that a toast takes no clicks, and both scripts assert this
    comes back None while one is standing over the control. Still returns rather than raises, so a
    script can record the overlap alongside the verdict.
    """
    facts = driver.execute_script(HIT_TEST, element)
    return None if facts["hit"] else str(facts["topmost"])


#: Every toast whose painted box intersects the target's, with the pointer-events value the
#: browser resolved for it. Geometry only -- it says nothing about who would receive a click.
_TOASTS_OVER = """
const box = arguments[0].getBoundingClientRect();
return [...document.querySelectorAll('#toast-region .toast')]
  .map((toast) => ({toast, rect: toast.getBoundingClientRect()}))
  .filter(({rect}) => rect.left < box.right && rect.right > box.left
    && rect.top < box.bottom && rect.bottom > box.top)
  .map(({toast}) => toast.className + ' [pointer-events: '
    + getComputedStyle(toast).pointerEvents + '] ' + toast.textContent.slice(0, 60));
"""


def toasts_over(driver, element) -> list[str]:
    """Which toasts are standing over this control right now, by geometry alone.

    The companion to `covering_element`, and the reason the toast assertion is not vacuous:
    "nothing is intercepting the click" is only worth asserting while something is genuinely on
    top of the control, and this is what reports whether anything is. A run where this comes back
    empty has proven the corner is clear rather than that the overlap is harmless -- both are
    acceptable outcomes, and a reader can tell which one happened.
    """
    return [str(entry) for entry in driver.execute_script(_TOASTS_OVER, element)]


def resource_hits(driver, suffix: str) -> int:
    """How many requests this page has made to a URL ending in `suffix`, since it loaded.

    Read out of the browser's own resource timings rather than a server log, because what is being
    asserted is what the *client* chose to send. Used to prove a negative: selecting a clip must
    not write the whole shot list back, because the reply to that write reloads readiness and the
    reply to that rebuilds the inspector long after the click looked finished.
    """
    return int(
        driver.execute_script(
            "return performance.getEntriesByType('resource')"
            ".filter((entry) => entry.name.endsWith(arguments[0])).length;",
            suffix,
        )
    )


def clear_toasts(driver, timeout: float = 8.0) -> None:
    """Wait out every toast on screen -- **the ones that go on their own**, which is not all of
    them.

    Both halves of what this used to claim were wrong, found on 2026-08-28 by a script that waited
    eight seconds for a refusal to clear and then asserted about the page underneath it. `toast()`
    in `app.js` reads: *"Long reports earn longer on screen, **errors stay until dismissed**, and
    every toast dismisses on click."* So an `error` toast has **no timer at all** and never
    self-removes, and every toast **is** dismissible -- it carries `title="Click to dismiss"` and a
    click handler that removes it.

    This function is left as a *wait* rather than made to click, because that is what its callers
    mean by it and a shared helper that started clicking would change what four other scripts are
    measuring. It returns quietly when the timeout passes, so a caller that needs an **error**
    toast gone has to click it -- see `e2e_preview_song_change.dismiss_toasts` for that.

    Kept even though `.toast-region` no longer takes pointer events: a toast still *covers* what is
    under it, so a hit test run with one up would be measuring the wrong thing anywhere the script
    is not deliberately testing the overlap.
    """
    from selenium.webdriver.common.by import By

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not driver.find_elements(By.CSS_SELECTOR, "#toast-region .toast"):
            return
        time.sleep(0.25)


def reachable_widths(driver, selector: str, widths: list[int], height: int = 900) -> dict[str, str]:
    """At which window widths is this control still on screen and clickable?

    Reported rather than asserted, because a control hidden at a narrow width is usually a
    deliberate responsive choice -- but it is a choice nothing offline can see, and `styles.css`
    hides three of the controls these scripts cover behind media queries. Recording the widths is
    how that stays visible to whoever reads the run.

    Leaves the window at the first width in the list.
    """
    from selenium.webdriver.common.by import By

    verdicts: dict[str, str] = {}
    for width in widths:
        driver.set_window_size(width, height)
        inner = driver.execute_script("return window.innerWidth;")
        found = driver.find_elements(By.CSS_SELECTOR, selector)
        if not found or not found[0].is_displayed():
            verdict = "not displayed"
        else:
            try:
                facts = visible_and_clickable(driver, found[0], selector)
            except AssertionError as error:
                verdict = str(error).split(":")[0][:110]
            else:
                verdict = "reachable" if facts["hit"] else "covered"
        # Keyed by the window width asked for, with the viewport it produced alongside it: the
        # two differ by the chrome, and keying on the viewport makes the result unpredictable.
        verdicts[str(width)] = f"{verdict} (viewport {inner}px)"
    driver.set_window_size(widths[0], height)
    return verdicts


def wait_for_readiness(driver, wait, fragment: str = "a prompt") -> str:
    """Block until the readiness fetch for the loaded project has landed.

    Not cosmetic. `loadProject` fires the readiness GET without awaiting it, and its reply calls
    `renderTimeline`, which rebuilds the clips *and* the shot inspector. Any element found before
    that lands goes stale under the script's feet -- which is what happened the first time this ran.
    The region reads "not checked" until the report arrives and the shot counts afterwards, so its
    text is the signal.

    Pass `fragment` when another project's report could satisfy the default one. The app loads the
    first project in the root before a script selects the one it seeded, and an empty plan renders
    "0 of 0 shots have a prompt." -- which contains the default fragment, so a script that took it
    as its own signal carried on while its real report was still in flight, and had its clips
    rebuilt underneath it. Something naming this plan's own shot count is the honest wait.
    """
    from selenium.webdriver.common.by import By

    wait.until(
        lambda browser: fragment
        in browser.find_element(By.ID, "plan-readiness").get_attribute("textContent"),
        f"no readiness report carrying {fragment!r} landed, so the timeline could re-render at "
        "any moment",
    )
    return driver.find_element(By.ID, "plan-readiness").get_attribute("textContent")


def clipped(driver, element) -> bool:
    """True when the element's text is wider than the box painting it, so part is not readable."""
    return bool(
        driver.execute_script(
            "return arguments[0].scrollWidth > arguments[0].clientWidth + 1;", element
        )
    )


def wait_for_toast(driver, wait, fragment: str) -> str:
    """The toast text containing `fragment`. Toasts self-remove after 4.2s, so this is prompt."""
    from selenium.common.exceptions import StaleElementReferenceException
    from selenium.webdriver.common.by import By

    def find(browser):
        for item in browser.find_elements(By.CSS_SELECTOR, "#toast-region .toast"):
            text = ""
            # A toast removes itself after 4.2 s, so one can vanish between the find and the read.
            with contextlib.suppress(StaleElementReferenceException):
                text = item.text
            if fragment in text:
                return text
        return False

    return wait.until(find, f"no toast carrying {fragment!r} appeared")


def console_gate(driver, name: str, result: dict, expected: list[str] | None = None) -> None:
    """Fail on any SEVERE console entry, and leave the whole log behind either way.

    `expected` holds fragments of entries this script *caused on purpose* -- a refusal it drove the
    route into, for instance. They are separated out and reported under their own key rather than
    filtered away silently, because a gate that quietly drops what it does not want to see is not
    a gate. Anything unlisted still fails.
    """
    logs = driver.get_log("browser")
    severe = [entry for entry in logs if entry.get("level") == "SEVERE"]
    wanted = [
        entry for entry in severe
        if any(fragment in entry.get("message", "") for fragment in expected or [])
    ]
    unexpected = [entry for entry in severe if entry not in wanted]
    (artifact_dir() / f"{name}-console.json").write_text(json.dumps(logs, indent=2), encoding="utf-8")
    result["severe_console_errors"] = unexpected
    if wanted:
        result["expected_console_errors"] = [entry["message"] for entry in wanted]
    assert not unexpected, unexpected


def report(name: str, result: dict) -> None:
    (artifact_dir() / f"{name}-result.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, indent=2, default=str))
