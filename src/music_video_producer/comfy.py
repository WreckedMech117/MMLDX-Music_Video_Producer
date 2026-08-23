from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import math
import os
import struct
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

logger = logging.getLogger(__name__)


class ComfyError(RuntimeError):
    """A downstream ComfyUI transport or execution error."""


@dataclass(slots=True)
class Submission:
    prompt_id: str
    number: int | None = None


@dataclass(slots=True)
class HistoryResult:
    prompt_id: str
    status: str
    outputs: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    #: Whether ComfyUI's history actually holds this prompt. ``status`` alone cannot say:
    #: an unknown prompt answers "queued", which is also what a genuinely waiting prompt
    #: answers — and that ambiguity is how a job whose prompt died with a crashed queue
    #: stayed "queued" in the manifest forever (met three times live on 2026-08-19/20).
    known: bool = True
    #: ComfyUI's own execution clock for this prompt, in epoch **milliseconds**, or `None`
    #: where the answer does not carry one. See `execution_span_ms` for where they come from
    #: and for what was checked before this was written.
    started_ms: int | None = None
    finished_ms: int | None = None

    @property
    def elapsed_seconds(self) -> float | None:
        """How long ComfyUI spent *executing* this prompt, or `None` if it did not say.

        Queue wait is excluded by construction — `execution_start` is stamped when the prompt
        leaves the queue — which is exactly what makes this the number to compare one render
        against another with, and what the record's own `created_at`→settle span is not.

        `None` rather than `0.0` for "not reported", because zero is a legitimate reading for a
        fully cached prompt and a caller must be able to tell the two apart. A span that runs
        backwards is also `None`: the two stamps come off `time.time()`, which a clock
        adjustment can move, and a negative duration is not a measurement.
        """
        if self.started_ms is None or self.finished_ms is None:
            return None
        span = (self.finished_ms - self.started_ms) / 1000.0
        return span if span >= 0 else None


#: The ``status.messages`` events that end a prompt's execution. `execution_interrupted` is one
#: of them: a cancel is a real end, and how long a render ran before it was cancelled is a
#: measurement worth keeping — see `RenderJob.render_seconds` on what it does and does not mean.
#:
#: **Reachable only through an interrupt this application did not issue.** Its own cancel route
#: stamps the record's span and makes the job terminal before anything reads `/history`, and
#: `stamp_job_settled` is idempotent, so the `comfy` value arrives too late to be adopted. What
#: reaches this branch is somebody pressing Cancel in ComfyUI's own interface, or another client
#: calling `/interrupt` — which is exactly the case this application cannot otherwise measure,
#: and the reason the event stays in the set rather than being dropped as dead.
_EXECUTION_END_EVENTS = frozenset(
    {"execution_success", "execution_error", "execution_interrupted"}
)

#: The smallest number that can be a millisecond stamp of a real moment: 2001-09-09, the day the
#: Unix epoch in milliseconds passed ten to the twelve. It does not pass ten to the thirteen
#: until 2286.
#:
#: The shape this rejects: a build, or a custom node, stamping `status.messages` in **seconds**
#: instead of milliseconds. Every other malformed shape in this function degrades to `None`, but
#: that one would divide by a thousand and record a 32 s render as `0.032 s` — sourced `comfy`,
#: shown without a `≤`, and confidently wrong, which is worse than no measurement at all and is
#: the precise failure mode this whole module was written to retire. No such build is in hand;
#: the floor is one comparison and it turns a silent lie into the same `None` everything else
#: malformed already answers.
_MILLISECOND_STAMP_FLOOR = 1_000_000_000_000


def execution_span_ms(status_data: Any) -> tuple[int | None, int | None]:
    """``(started_ms, finished_ms)`` out of one ``/history`` entry's ``status`` object.

    **Checked, not assumed**, twice. Read from the Director's own portable install's source on
    2026-08-21: `PromptExecutor.add_message` stamps ``"timestamp": int(time.time() * 1000)`` onto
    *every* status message it appends, `execute()` clears `status_messages` and emits
    ``execution_start`` before the first node runs, and `PromptQueue.task_done` copies the whole
    list into the history entry as ``status.messages``. Identical in 0.33.1 and in 0.33.3, which
    the Director updated to the same day.

    Then **confirmed against the live server on 0.33.3** with one 512x512 4-step Flux still: the
    history entry carried ``['execution_start', 'execution_cached', 'execution_success']``, this
    function read the span as 32.431 s, and an independent stopwatch around the submission
    measured 33.140 s — the recorded render sitting inside the observed window, +0.709 s of poll
    granularity and transport. So a finished prompt's history really does carry its own execution
    clock, and this application no longer has to reconstruct render costs from output-file mtimes.
    There is no per-prompt *duration* field — only these two stamps — and there is no start stamp
    at all on a prompt still executing.

    ``extra_data.create_time`` (server.py) is a third stamp, the enqueue moment in milliseconds.
    It is deliberately not read here: `RenderJob.created_at` already records enqueue from this
    side, and a queue wait derived from a foreign clock's zero point is worse evidence than one
    derived from our own.

    Deliberately forgiving, on `progress_from_message`'s rule: this reads a foreign wire format
    from a component the Director upgrades independently, so a missing key, a message that is
    not a two-element pair, a non-integer timestamp, a timestamp too small to be milliseconds
    (`_MILLISECOND_STAMP_FLOOR`) or a shape from some future build answers `None` rather than
    raising. A timing is an enhancement; nothing about a render may fail because ComfyUI phrased
    its history differently — and nothing may quietly record the wrong unit as a measurement.
    """
    if not isinstance(status_data, dict):
        return (None, None)
    messages = status_data.get("messages")
    if not isinstance(messages, list):
        return (None, None)
    started: int | None = None
    finished: int | None = None
    for message in messages:
        if not isinstance(message, (list, tuple)) or len(message) != 2:
            continue
        event, data = message
        if not isinstance(data, dict):
            continue
        stamp = data.get("timestamp")
        # `bool` is an `int` in Python and `True` is not a timestamp.
        if not isinstance(stamp, int) or isinstance(stamp, bool):
            continue
        # And a number too small to be a millisecond stamp is a build using some other unit,
        # not a moment in 1970. See `_MILLISECOND_STAMP_FLOOR`.
        if stamp < _MILLISECOND_STAMP_FLOOR:
            continue
        if event == "execution_start":
            started = stamp
        elif event in _EXECUTION_END_EVENTS:
            # The *last* ending wins. A prompt emits one, but reading the last of whatever is
            # there cannot be wrong where reading the first can.
            finished = stamp
    return (started, finished)


class ComfyClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        before_submit: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        """`before_submit` runs once immediately before each prompt is sent.

        It exists so a resource optimisation the application owns — today, releasing the
        language model's VRAM — covers every submission route through one wiring, including
        routes added later. It is injected rather than imported so this client stays
        ignorant of any other provider: reaching into an LM Studio client from inside the
        ComfyUI client would tie two unrelated services together and would make testing the
        eject require a ComfyUI server.
        """
        self.base_url = base_url.rstrip("/")
        self.before_submit = before_submit
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _run_before_submit(self) -> None:
        """Run the pre-submission hook, absorbing anything it does.

        The hook is an optimisation; the render is the work. A render must never fail to
        happen because a hook failed, so every exception is swallowed here as well as
        inside the hook itself — this is the only guarantee that holds for a hook this
        client did not write. `CancelledError` is a `BaseException` and still propagates,
        so shutdown is not absorbed along with it.
        """
        if self.before_submit is None:
            return
        try:
            await self.before_submit()
        except Exception as error:  # noqa: BLE001 - the render outranks the optimisation
            logger.warning("Pre-submission hook failed; submitting anyway: %r", error)

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._client.request(method, path, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as error:
            detail = error.response.text[:500]
            raise ComfyError(f"ComfyUI returned {error.response.status_code}: {detail}") from error
        except httpx.HTTPError as error:
            raise ComfyError(f"Cannot reach ComfyUI at {self.base_url}: {error}") from error

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as error:
            raise ComfyError("ComfyUI returned invalid JSON") from error

    async def health(self) -> dict[str, Any]:
        try:
            response = await self._request("GET", "/system_stats")
            payload = self._json(response)
        except ComfyError as error:
            return {"online": False, "url": self.base_url, "error": str(error)}
        return {"online": True, "url": self.base_url, "stats": payload}

    async def submit(self, prompt: dict[str, Any], *, client_id: str | None = None) -> Submission:
        body: dict[str, Any] = {"prompt": prompt}
        if client_id:
            body["client_id"] = client_id
        # After the payload is built and the caller's own validation has passed, and before
        # a single byte goes to ComfyUI. A submission refused by validation must not have
        # cost the Director their loaded model, and a model released after the prompt is
        # queued releases nothing the render can use.
        await self._run_before_submit()
        response = await self._request("POST", "/prompt", json=body)
        payload = self._json(response)
        prompt_id = payload.get("prompt_id") if isinstance(payload, dict) else None
        if not prompt_id:
            raise ComfyError("ComfyUI prompt response did not contain a prompt_id")
        return Submission(prompt_id=prompt_id, number=payload.get("number"))

    async def queue(self) -> dict[str, Any]:
        return self._json(await self._request("GET", "/queue"))

    async def queue_state(self, prompt_id: str) -> str:
        """Locate ``prompt_id`` in the live queue.

        ComfyUI writes no history entry until a prompt finishes, so history alone cannot
        tell an executing render from a waiting one. Returns "running", "queued", or
        "absent" when the prompt is in neither bucket.
        """
        payload = await self.queue()
        for key, state in (("queue_running", "running"), ("queue_pending", "queued")):
            for item in payload.get(key, []):
                if isinstance(item, list) and any(part == prompt_id for part in item):
                    return state
        return "absent"

    async def object_info(self) -> dict[str, Any]:
        return self._json(await self._request("GET", "/object_info"))

    async def cancel(self, prompt_id: str) -> None:
        """Take one prompt out of ComfyUI's hands: dequeue it, and interrupt it when it
        is the one running. Idempotent — cancelling a prompt ComfyUI no longer knows is a
        no-op, because the caller's job record is what actually settles."""
        payload = self._json(await self._request("GET", "/queue"))
        running = any(
            part == prompt_id
            for item in payload.get("queue_running", [])
            if isinstance(item, list)
            for part in item
            if isinstance(part, str)
        )
        await self._request("POST", "/queue", json={"delete": [prompt_id]})
        if running:
            await self._request("POST", "/interrupt")

    async def history(self, prompt_id: str) -> HistoryResult:
        payload = self._json(await self._request("GET", f"/history/{quote(prompt_id)}"))
        entry = payload.get(prompt_id)
        if not entry:
            return HistoryResult(prompt_id=prompt_id, status="queued", raw=payload, known=False)
        status_data = entry.get("status", {})
        status = status_data.get("status_str", "complete" if entry.get("outputs") else "running")
        if status == "success":
            status = "complete"
        outputs: list[dict[str, Any]] = []
        for node_id, node_outputs in entry.get("outputs", {}).items():
            for media_key in ("images", "audio", "gifs", "video"):
                for item in node_outputs.get(media_key, []):
                    outputs.append({"node_id": node_id, "kind": media_key, **item})
        error = ""
        for message in status_data.get("messages", []):
            if len(message) == 2 and message[0] == "execution_error":
                data = message[1]
                error = f"{data.get('node_type', 'Node')}: {data.get('exception_message', 'error')}"
                break
        started_ms, finished_ms = execution_span_ms(status_data)
        return HistoryResult(
            prompt_id=prompt_id,
            status=status,
            outputs=outputs,
            error=error,
            raw=entry,
            started_ms=started_ms,
            finished_ms=finished_ms,
        )

    async def upload(self, filename: str, content: bytes, content_type: str) -> dict[str, Any]:
        files = {"image": (filename, content, content_type)}
        data = {"type": "input", "overwrite": "false"}
        return self._json(await self._request("POST", "/upload/image", files=files, data=data))

    def output_url(self, item: dict[str, Any]) -> str:
        filename = quote(str(item["filename"]))
        subfolder = quote(str(item.get("subfolder", "")))
        media_type = quote(str(item.get("type", "output")))
        return f"{self.base_url}/view?filename={filename}&subfolder={subfolder}&type={media_type}"


# ----------------------------------------------------------------------------------------------
# Live render progress.
#
# ComfyUI's HTTP surface can say *whether* a prompt is queued or running and nothing about how
# far through it is: `/prompt` returns a queue count, `/queue` returns membership, and
# `/internal/progress` is a 404 on 0.33.1. The only per-step channel this build has is the
# WebSocket at `/ws`, so this half of the module opens one, reads it, and holds the answer in
# memory for the AD-1 poll to pick up.
#
# **Three properties, each load-bearing:**
#
# * *Nothing here is persisted.* A percentage is derived state that is stale the moment it is
#   read, and writing one into the manifest would bump `Project.updated_at` twice a second —
#   which `PUT /api/projects/{id}` compares, so every tick would arm an optimistic-concurrency
#   collision against whatever the Director is editing. See `ProgressTracker`.
# * *Nothing here can break a render.* The socket is an enhancement bolted beside the existing
#   transport, never in front of it. It is never awaited by a submission, never consulted by the
#   reconciler, and every failure it can have — refused, dropped, upgraded to a shape nobody
#   recognises — degrades to "no percentage is shown" and to nothing else.
# * *Nothing here touches ComfyUI's lifecycle.* It connects, reads, and reconnects. It never
#   submits, interrupts, or clears anything; ComfyUI is user-managed.
# ----------------------------------------------------------------------------------------------

#: Frames larger than this end the connection rather than being buffered. ComfyUI's JSON status
#: messages are kilobytes; only a binary preview image could approach this, and a listener that
#: can be made to allocate a gigabyte by a malformed length field is a listener that can take the
#: application down. The reconnect that follows costs a second.
MAX_WS_FRAME_BYTES = 8 * 1024 * 1024


class WebSocketClosed(RuntimeError):
    """The ComfyUI progress socket ended, cleanly or otherwise. Never fatal to anything."""


def progress_from_message(message: Any) -> tuple[str, int] | None:
    """One ComfyUI WebSocket message → ``(prompt_id, percent)``, or ``None`` for everything else.

    Pure, and deliberately forgiving: this reads a *foreign* wire format from a component the
    Director upgrades independently of this application, so an unrecognised message, a missing
    field, a string where a number was expected, or a shape from some future build is answered
    with ``None`` — never an exception. A progress reader that can raise inside a socket loop is
    a progress reader that can take the socket down and lose the percentages it exists to carry.

    **The shapes, as observed live against ComfyUI 0.33.1** (see the 2026-08-20 development-log
    entry, which quotes captured messages):

    * ``{"type": "progress_state", "data": {"prompt_id": "...", "nodes": {"<id>": {"value": 3.0,
      "max": 20.0, "state": "running", ...}}}}`` — the build's primary channel, emitted by
      `comfy_execution/progress.py`'s `WebUIProgressHandler` on every node start, step and
      finish. ``nodes`` holds every *non-pending* node, so the sampler's step counter arrives
      alongside the one-unit nodes around it.
    * ``{"type": "progress", "data": {"value": 3, "max": 20, "prompt_id": "...", "node": "31"}}``
      — the older single-node form. 0.33.1 does not emit it; it is read anyway so that a
      downgrade, or a custom node that still sends it, keeps working.

    The live capture also recorded ``status`` and, from an installed custom node,
    ``crystools.monitor`` once a second. Neither carries a fraction. Nor do ``executing``,
    ``executed``, ``execution_start``, ``execution_cached``, ``execution_success``,
    ``execution_error`` or ``feature_flags``, and all of them are ignored here. The job's
    *settlement* is `/queue` and `/history`'s business, exactly as it was before this socket
    existed; nothing in this module decides a job's status.

    **Only nodes that count steps are counted** — the ones reporting ``max > 1``. Everything else
    is skipped, and a message with no such node returns ``None``, which the interface draws as
    *unknown* rather than as a number.

    That rule comes straight off the capture, and the alternative was measured to be wrong. The
    observed Flux graph is fourteen nodes; ComfyUI reports each one as ``0/1`` then ``1/1`` as it
    is walked, and reports a node only once it has *started*. Averaging over the whole reported
    map therefore reads ``1/1 = 100%`` on the very first message — which is exactly what the
    first implementation of this function did, and the live run showed it pinned at 100% for
    twenty-five of the render's twenty-six seconds. A denominator that only exists once the work
    has begun cannot measure the work.

    What it can measure is a step counter, and one node in each of this application's graphs has
    one: the sampler, which is also where nearly all of the time goes (in the capture, node 15
    ran ten steps across roughly the last twenty-four seconds of a twenty-six second render). So
    the answer is ``Σvalue / Σmax`` across the step-counting nodes, and *unknown* while models are
    still loading — because at that point nothing has said anything a percentage could be made of,
    and inventing one is the thing this feature must not do.

    **The stated cost.** A graph with two sequential samplers would read 100% when the first one
    finished, and `ProgressTracker`'s monotonic floor would hold it there while the second ran.
    Every adapter in `workflows.py` builds exactly one sampling node, so nothing this application
    submits behaves that way; the render is still plainly marked RENDERING throughout, and the
    job's real status comes from `/queue` as it always did.
    """
    if not isinstance(message, dict):
        return None
    data = message.get("data")
    if not isinstance(data, dict):
        return None
    kind = message.get("type")
    if kind == "progress_state":
        prompt_id = data.get("prompt_id")
        nodes = data.get("nodes")
        if not isinstance(prompt_id, str) or not prompt_id or not isinstance(nodes, dict):
            return None
        done = 0.0
        total = 0.0
        for state in nodes.values():
            if not isinstance(state, dict):
                continue
            fraction = _node_fraction(state.get("value"), state.get("max"))
            if fraction is None:
                continue
            node_done, node_total = fraction
            done += node_done
            total += node_total
        if total <= 0:
            return None
        return prompt_id, _percent(done, total)
    if kind == "progress":
        prompt_id = data.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            return None
        fraction = _node_fraction(data.get("value"), data.get("max"))
        if fraction is None:
            return None
        done, total = fraction
        return prompt_id, _percent(done, total)
    return None


def _node_fraction(value: Any, maximum: Any) -> tuple[float, float] | None:
    """``(done, total)`` for one step-counting node, or ``None`` when it counts no steps.

    `bool` is excluded explicitly: it is an `int` in Python, and a `True` that arrived where a
    step count belongs would silently count as one completed step.

    ``max <= 1`` is the "not a step counter" case and the reason this returns ``None`` for most
    of a graph: ComfyUI gives every ordinary node a one-unit bar it fills the instant it starts.
    Counting those is what made the first live run read 100% while the render had barely begun.
    """
    if isinstance(value, bool) or isinstance(maximum, bool):
        return None
    if not isinstance(value, (int, float)) or not isinstance(maximum, (int, float)):
        return None
    total = float(maximum)
    done = float(value)
    # NaN and the infinities are refused outright: `math.isfinite` is the only check that
    # catches a JSON `Infinity` before it becomes a percentage of nothing.
    if not math.isfinite(total) or not math.isfinite(done) or total <= 1:
        return None
    return min(max(done, 0.0), total), total


def _percent(done: float, total: float) -> int:
    """The rounded percentage, with no clamp of its own — deliberately.

    A clamp here would be unreachable code, and unreachable code is a guard nobody can test:
    `_node_fraction` has already bounded every contribution into ``0 <= done <= total``, so the
    sum obeys the same bounds and the ratio cannot leave ``[0, 1]``. Clamping in both places was
    measured by mutation: with the per-node clamp present, removing this one changed nothing any
    test could see. The per-node clamp is the one that matters, because it is what stops a single
    node reporting past its own maximum from inflating the whole prompt's figure.
    """
    return round(100.0 * done / total)


class ProgressTracker:
    """Live percentages, keyed by ``prompt_id``, held **only** in this process's memory.

    Not a model field, not a manifest key, not a row anywhere. `RenderJob.progress` exists and is
    written by the local ffmpeg export (AD-9) — that one is a persisted number because an export
    is this application's own work and the record is its only witness. A ComfyUI percentage is
    not: it changes several times a second, it is meaningless the instant the render settles, and
    persisting it would make `store.save` — and therefore `Project.updated_at`, which
    `PUT /api/projects/{id}` compares — move on a timer. The startup healer already refuses to
    save a project it changed nothing on for exactly this reason.

    Attribution is by ``prompt_id`` and by nothing else, so a batch of concurrent renders keeps
    one entry each and no job can ever read another's number.

    Monotonic per prompt: a reported percentage never moves backwards. The denominator genuinely
    grows as ComfyUI starts more nodes (see `progress_from_message`), so a raw reading can dip;
    a bar that goes backwards reads as a bug, and the floor is still a true statement about work
    already done.

    Bounded: `capacity` entries, oldest evicted first. A long session submits an unbounded number
    of prompts and this map must not grow with it.
    """

    def __init__(self, capacity: int = 64) -> None:
        self.capacity = max(1, capacity)
        self._percent: OrderedDict[str, int] = OrderedDict()

    def apply(self, message: Any) -> str | None:
        """Fold one WebSocket message in. Returns the prompt it moved, or ``None``."""
        parsed = progress_from_message(message)
        if parsed is None:
            return None
        prompt_id, percent = parsed
        held = self._percent.get(prompt_id)
        if held is not None and percent < held:
            percent = held
        self._percent[prompt_id] = percent
        self._percent.move_to_end(prompt_id)
        while len(self._percent) > self.capacity:
            self._percent.popitem(last=False)
        return prompt_id

    def percent(self, prompt_id: str) -> int | None:
        """This prompt's percentage, or ``None`` — which means *unknown*, not zero.

        The distinction is the whole honesty of the feature. ``0`` is "ComfyUI has reported this
        render and no step of it is done"; ``None`` is "nobody has said anything" — a socket that
        never connected, a prompt still waiting in the queue, a build whose messages this module
        does not recognise. The interface shows the plain RENDERING word for ``None`` and a real
        ``0%`` for zero, and never invents a number for either.
        """
        return self._percent.get(prompt_id)

    def snapshot(self) -> dict[str, int]:
        return dict(self._percent)

    def forget(self, prompt_id: str) -> None:
        self._percent.pop(prompt_id, None)

    def clear(self) -> None:
        self._percent.clear()


class ComfyWebSocket:
    """A minimal RFC 6455 client, read-only, over `asyncio.open_connection`.

    Hand-rolled rather than added as a dependency. This application ships with four runtime
    packages and a frontend with none; `httpx` — already here — does not speak WebSocket, and the
    alternative was pulling `websockets` in for one read loop against one localhost service. What
    is actually needed is small and closed: connect, read text frames, answer a ping, close. It
    sends no application data at all, so the masking path exists only for the control frames a
    conforming client must mask.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    @classmethod
    async def connect(
        cls, base_url: str, *, client_id: str, timeout: float = 10.0
    ) -> ComfyWebSocket:
        parts = urlsplit(base_url)
        secure = parts.scheme in ("https", "wss")
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if secure else 80)
        path = f"{parts.path.rstrip('/')}/ws?clientId={quote(client_id)}"
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=secure or None), timeout=timeout
        )
        socket = cls(reader, writer)
        try:
            await asyncio.wait_for(socket._handshake(host, port, path), timeout=timeout)
        except BaseException:
            await socket.close()
            raise
        return socket

    async def _handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._writer.write(request.encode("ascii"))
        await self._writer.drain()
        status = await self._reader.readline()
        if b" 101" not in status:
            raise WebSocketClosed(
                f"ComfyUI did not upgrade the progress socket: {status.decode('latin-1').strip()!r}"
            )
        while True:
            line = await self._reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break

    async def receive(self) -> str | None:
        """The next text message, or ``None`` for a frame that carries no text.

        ``None`` rather than a filtered loop so the caller still sees that the connection is
        alive — a binary preview frame is traffic, and traffic is what tells the reconnect logic
        the socket is healthy. Raises `WebSocketClosed` when the peer goes away.
        """
        payload = bytearray()
        message_opcode = 0
        while True:
            fin, opcode, chunk = await self._read_frame()
            if opcode == 0x8:
                raise WebSocketClosed("ComfyUI closed the progress socket")
            if opcode == 0x9:
                await self._send_frame(0xA, chunk)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                message_opcode = opcode
                payload = bytearray(chunk)
            elif opcode == 0x0:
                payload += chunk
            else:
                raise WebSocketClosed(f"Unknown WebSocket opcode {opcode:#x}")
            if not fin:
                continue
            if message_opcode != 0x1:
                return None
            try:
                return payload.decode("utf-8")
            except UnicodeDecodeError:
                return None

    async def _read_frame(self) -> tuple[bool, int, bytes]:
        header = await self._readexactly(2)
        first, second = header[0], header[1]
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", await self._readexactly(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", await self._readexactly(8))[0]
        if length > MAX_WS_FRAME_BYTES:
            raise WebSocketClosed(f"Refusing a {length}-byte WebSocket frame")
        mask = await self._readexactly(4) if masked else b""
        payload = await self._readexactly(length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return fin, opcode, payload

    async def _readexactly(self, count: int) -> bytes:
        try:
            return await self._reader.readexactly(count)
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as error:
            raise WebSocketClosed(f"ComfyUI progress socket ended: {error!r}") from error

    async def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        header += mask
        header += bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._writer.write(bytes(header))
        await self._writer.drain()

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._send_frame(0x8)
        with contextlib.suppress(Exception):
            self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()


class ComfyProgressListener:
    """Keeps one socket open to ComfyUI and folds what it says into a `ProgressTracker`.

    **Every failure mode is the same failure mode: no percentage.** ComfyUI down at boot — a very
    ordinary state, since the Director starts it separately — is a connect error, a backoff, and
    a retry; a restart mid-render is a drop and a reconnect; an unrecognised message is dropped by
    `progress_from_message`. Nothing here is awaited by a submission and nothing here can raise
    into one. The reconciler goes on reading `/queue` and `/history`, jobs settle, outputs land.

    The backoff never spins hot: it doubles from `min_backoff` to `max_backoff` and resets only
    when a connection actually delivered a message, so a socket that accepts and instantly drops
    backs off exactly like one that refuses.

    No `client_id` is sent with submissions, deliberately. ComfyUI targets execution messages at
    the submitting client's socket when a prompt carried a `client_id` and **broadcasts them to
    every socket when it did not** (`server.py`'s `send_json`, `sid=None`). Every submission this
    application makes omits it, so these messages are broadcast — which is why this listener sees
    them without one byte of any submission changing, and why the Director's own ComfyUI browser
    tab goes on showing the same progress it always did. Claiming the client id would have taken
    that away from them to gain nothing.
    """

    def __init__(
        self,
        base_url: str,
        tracker: ProgressTracker | None = None,
        *,
        client_id: str | None = None,
        connect: Callable[[], Awaitable[Any]] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        min_backoff: float = 1.0,
        max_backoff: float = 30.0,
        connect_timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.tracker = tracker or ProgressTracker()
        self.client_id = client_id or f"mvp-{uuid.uuid4().hex[:12]}"
        self.min_backoff = min_backoff
        self.max_backoff = max_backoff
        self.connect_timeout = connect_timeout
        self._connect = connect
        self._sleep = sleep or asyncio.sleep
        self._task: asyncio.Task[None] | None = None
        self._socket: Any = None
        #: Connection attempts and delivered messages, for the log line and for the tests that
        #: prove a refused socket backs off instead of spinning. `stops` counts completed calls
        #: to `stop`, which is the only observable an app-shutdown test has: the lifespan hook
        #: leaves nothing else behind, and a hook that silently stopped calling it would
        #: otherwise leak a task past shutdown with every assertion still green.
        self.attempts = 0
        self.messages = 0
        self.stops = 0

    def ingest(self, raw: Any) -> str | None:
        """One raw frame → the tracker. Text is decoded as JSON; anything else is dropped."""
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return None
        return self.tracker.apply(raw)

    async def open(self) -> Any:
        if self._connect is not None:
            return await self._connect()
        return await ComfyWebSocket.connect(
            self.base_url, client_id=self.client_id, timeout=self.connect_timeout
        )

    @property
    def running(self) -> bool:
        """Whether a listening task is currently alive. Nothing decides anything on this — it is
        for the shutdown test and for a log line."""
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Begin listening. Idempotent, and it cannot fail — the first connect is the task's."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="comfy-progress")

    async def stop(self) -> None:
        """Cancel the task and close the socket. Leaks neither across an app shutdown."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        socket, self._socket = self._socket, None
        if socket is not None:
            with contextlib.suppress(Exception):
                await socket.close()
        self.stops += 1

    async def run(self) -> None:
        backoff = self.min_backoff
        while True:
            self.attempts += 1
            try:
                socket = await self.open()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - every reachability failure is the same
                logger.debug("ComfyUI progress socket unavailable: %r", error)
                await self._sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
                continue
            self._socket = socket
            try:
                while True:
                    raw = await socket.receive()
                    self.messages += 1
                    backoff = self.min_backoff
                    if raw is not None:
                        self.ingest(raw)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - a dropped socket is not this app's error
                logger.debug("ComfyUI progress socket closed: %r", error)
            finally:
                self._socket = None
                with contextlib.suppress(Exception):
                    await socket.close()
            await self._sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff)
