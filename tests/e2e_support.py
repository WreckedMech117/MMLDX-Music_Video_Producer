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
            ["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=20
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].rsplit(":", 1)[-1] == str(port):
                try:
                    return int(parts[4])
                except ValueError:
                    return None
    return None


def _describe_process(pid: int) -> str:
    if sys.platform != "win32":
        return f"PID {pid}"
    try:
        output = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' | "
                "ForEach-Object { $_.CreationDate.ToString('s') + ' :: ' + $_.CommandLine }",
            ],
            capture_output=True,
            text=True,
            timeout=30,
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
        self.evidence: dict[str, object] = {}

    def __enter__(self) -> ManagedServer:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

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
        self._await_health()
        self._check_listener_is_our_child()
        self._check_server_owns_our_data_root()

    def _check_package_is_this_checkout(self) -> None:
        probe = subprocess.run(
            [sys.executable, "-c", "import music_video_producer as m; print(m.__file__)"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
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
        if pid != self.process.pid:
            raise StaleServer(
                f"{self.host}:{self.port} is served by {_describe_process(pid)}, not by the "
                f"server this script started (PID {self.process.pid}). Something took the port "
                "between the free check and the spawn."
            )

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
    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=15)
        log = getattr(self, "_log", None)
        if log is not None and not log.closed:
            log.close()

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


def clipped(driver, element) -> bool:
    """True when the element's text is wider than the box painting it, so part is not readable."""
    return bool(
        driver.execute_script(
            "return arguments[0].scrollWidth > arguments[0].clientWidth + 1;", element
        )
    )


def wait_for_toast(driver, wait, fragment: str) -> str:
    """The toast text containing `fragment`. Toasts self-remove after 4.2s, so this is prompt."""
    from selenium.webdriver.common.by import By

    def find(browser):
        for item in browser.find_elements(By.CSS_SELECTOR, "#toast-region .toast"):
            try:
                text = item.text
            except Exception:  # noqa: BLE001 - the toast can be removed mid-read
                continue
            if fragment in text:
                return text
        return False

    return wait.until(find, f"no toast carrying {fragment!r} appeared")


def console_gate(driver, name: str, result: dict) -> None:
    """Fail on any SEVERE console entry, and leave the whole log behind either way."""
    logs = driver.get_log("browser")
    severe = [entry for entry in logs if entry.get("level") == "SEVERE"]
    (artifact_dir() / f"{name}-console.json").write_text(json.dumps(logs, indent=2), encoding="utf-8")
    result["severe_console_errors"] = severe
    assert not severe, severe


def report(name: str, result: dict) -> None:
    (artifact_dir() / f"{name}-result.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, indent=2, default=str))
