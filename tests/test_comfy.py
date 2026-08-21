import asyncio
import json
from itertools import pairwise
from pathlib import Path

import httpx
import pytest

from music_video_producer.comfy import (
    ComfyClient,
    ComfyError,
    ComfyProgressListener,
    ComfyWebSocket,
    ProgressTracker,
    WebSocketClosed,
    progress_from_message,
)


@pytest.mark.asyncio
async def test_comfy_client_health_and_submit_prompt():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"system": {"comfyui_version": "0.33.1"}})
        if request.url.path == "/prompt":
            assert json.loads(request.content)["prompt"]["1"]["class_type"] == "SaveImage"
            return httpx.Response(200, json={"prompt_id": "prompt-123", "number": 9})
        return httpx.Response(404)

    client = ComfyClient("http://comfy.test", transport=httpx.MockTransport(handler))

    assert (await client.health())["online"] is True
    submitted = await client.submit({"1": {"class_type": "SaveImage", "inputs": {}}})
    assert submitted.prompt_id == "prompt-123"


@pytest.mark.asyncio
async def test_comfy_client_translates_downstream_errors():
    client = ComfyClient(
        "http://comfy.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(500, text="broken")),
    )

    with pytest.raises(ComfyError, match="500"):
        await client.queue()


@pytest.mark.asyncio
async def test_comfy_client_translates_invalid_json_responses():
    client = ComfyClient(
        "http://comfy.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="not-json")),
    )

    with pytest.raises(ComfyError, match="invalid JSON"):
        await client.submit({"1": {"class_type": "SaveImage", "inputs": {}}})


@pytest.mark.asyncio
async def test_comfy_client_reads_execution_error_from_history():
    payload = {
        "p1": {
            "status": {
                "status_str": "error",
                "messages": [["execution_error", {"node_type": "KSampler", "exception_message": "OOM"}]],
            },
            "outputs": {},
        }
    }
    client = ComfyClient(
        "http://comfy.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )

    result = await client.history("p1")

    assert result.status == "error"
    assert result.error == "KSampler: OOM"


@pytest.mark.asyncio
async def test_comfy_client_normalizes_successful_history_status():
    payload = {
        "p1": {
            "status": {"status_str": "success", "messages": []},
            "outputs": {"9": {"images": [{"filename": "done.png", "subfolder": "mvp"}]}},
        }
    }
    client = ComfyClient(
        "http://comfy.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )

    result = await client.history("p1")

    assert result.status == "complete"
    assert result.outputs[0]["filename"] == "done.png"


@pytest.mark.asyncio
async def test_queue_state_distinguishes_running_from_pending():
    """History is empty for both waiting and executing prompts; only the queue separates them."""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/queue"
        return httpx.Response(
            200,
            json={
                "queue_running": [[0, "running-id", {}, {}]],
                "queue_pending": [[1, "pending-id", {}, {}]],
            },
        )

    client = ComfyClient("http://comfy", transport=httpx.MockTransport(handler))
    assert await client.queue_state("running-id") == "running"
    assert await client.queue_state("pending-id") == "queued"
    assert await client.queue_state("finished-id") == "absent"
    await client.close()


# ----------------------------------------------------------------------------------------------
# Live render progress: ComfyUI's WebSocket, parsed, tracked, and reconnected to.
#
# The fixture these tests read is not invented. `tests/fixtures/comfy_progress_frames.jsonl` is
# the verbatim, in-order capture of every non-monitor frame ComfyUI 0.33.1 broadcast during one
# real Flux render on 2026-08-20 (prompt 9b9da177-83fc-40b2-8099-71f52bd123cf, 512x512, 10 steps),
# read off the socket by `ComfyWebSocket` itself. Parsing evidence rather than a hand-written
# guess is the whole point: the first implementation of this parser passed a suite of invented
# messages and was measured wrong against these ones.
# ----------------------------------------------------------------------------------------------

CAPTURE = Path(__file__).resolve().parent / "fixtures" / "comfy_progress_frames.jsonl"

#: The prompt the capture belongs to.
CAPTURED_PROMPT = "9b9da177-83fc-40b2-8099-71f52bd123cf"


def captured_frames() -> list[dict]:
    return [
        json.loads(line)
        for line in CAPTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_the_captured_shapes_are_the_two_this_parser_claims_to_read():
    """The observed wire format, asserted as observed -- not as documented, not as remembered.

    ComfyUI 0.33.1 emits BOTH forms for one render: the `progress_state` map that
    `comfy_execution/progress.py` sends on every node transition, and the older single-node
    `progress` message. Reading only the one that source file advertises would have thrown away
    half the evidence, and reading only the older one would have made this build's primary
    channel invisible."""
    frames = captured_frames()
    kinds = {frame["type"] for frame in frames}
    assert kinds == {"status", "progress_state", "progress"}, kinds

    # The step-counting `progress_state` message, exactly as it arrived. The fixture holds all
    # twelve nodes ComfyUI was reporting by then; node 15 is the sampler.
    sampling = next(
        frame
        for frame in frames
        if frame["type"] == "progress_state"
        and frame["data"]["nodes"].get("15", {}).get("max") == 10
    )
    assert sampling["data"]["prompt_id"] == CAPTURED_PROMPT
    assert sampling["data"]["nodes"]["15"]["state"] == "running"
    assert set(sampling["data"]["nodes"]["15"]) == {
        "value",
        "max",
        "state",
        "node_id",
        "prompt_id",
        "display_node_id",
        "parent_node_id",
        "real_node_id",
    }
    # And an ordinary node in the same message: a one-unit bar, filled the instant it started.
    assert sampling["data"]["nodes"]["13"]["max"] == 1.0

    legacy = next(frame for frame in frames if frame["type"] == "progress")
    assert legacy["data"] == {
        "value": 1,
        "max": 10,
        "prompt_id": CAPTURED_PROMPT,
        "node": "15",
    }


def test_the_captured_render_derives_ten_rising_percentages_and_stops_at_a_hundred():
    """The whole capture through the tracker, in order. This is the acceptance evidence.

    Ten steps, ten percentages, 10 through 100, never backwards and never above 100 -- and
    nothing at all before the sampler starts, because until then no node in the graph is counting
    anything. The earlier averaging rule read 100% off the very first message here."""
    tracker = ProgressTracker()
    derived: list[int] = []
    for frame in captured_frames():
        if tracker.apply(frame) is not None:
            percent = tracker.percent(CAPTURED_PROMPT)
            if not derived or derived[-1] != percent:
                derived.append(percent)

    assert derived == [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert all(later >= earlier for earlier, later in pairwise(derived))
    assert max(derived) <= 100


def test_each_of_the_two_captured_forms_derives_the_same_ten_percentages_alone():
    """Both readers, separately. The build emits both forms for one render, so a suite that only
    ever replays the whole capture can lose either reader and still see the right numbers coming
    out of the other -- which is exactly what a mutation of the `progress_state` branch did."""
    frames = captured_frames()
    derived = {}
    for kind in ("progress_state", "progress"):
        tracker = ProgressTracker()
        seen: list[int] = []
        for frame in frames:
            if frame["type"] != kind:
                continue
            if tracker.apply(frame) is not None:
                percent = tracker.percent(CAPTURED_PROMPT)
                if not seen or seen[-1] != percent:
                    seen.append(percent)
        derived[kind] = seen

    assert derived["progress_state"] == [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert derived["progress"] == [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]


def test_one_node_reporting_past_its_own_maximum_cannot_inflate_the_whole_prompt():
    """The per-node clamp, isolated. Two samplers, one of them reporting forty of twenty steps:
    the honest answer is that half the declared work is done, and an unclamped sum would call it
    finished. The aggregate clamp cannot catch this -- 40/40 is a perfectly legal ratio."""
    tracker = ProgressTracker()
    tracker.apply(
        {
            "type": "progress_state",
            "data": {
                "prompt_id": "p1",
                "nodes": {
                    "a": {"value": 40, "max": 20, "state": "running"},
                    "b": {"value": 0, "max": 20, "state": "running"},
                },
            },
        }
    )

    assert tracker.percent("p1") == 50


def test_nothing_is_reported_until_a_node_actually_counts_steps():
    """Unknown, not zero, while ComfyUI is loading models -- the honest answer, and a different
    one from `0`. The capture spends about twenty-four of its twenty-six seconds here."""
    tracker = ProgressTracker()
    for frame in captured_frames():
        if frame["type"] != "progress_state":
            continue
        if any(node.get("max", 0) > 1 for node in frame["data"]["nodes"].values()):
            break
        tracker.apply(frame)
        assert tracker.percent(CAPTURED_PROMPT) is None
    assert tracker.snapshot() == {}


def test_a_reported_zero_is_a_different_answer_from_no_report_at_all():
    """`0` means "started, no step done"; `None` means "nobody has said anything". The interface
    draws a real 0% for one and the plain RENDERING word for the other, so the two must not
    collapse into each other anywhere on the way."""
    tracker = ProgressTracker()
    assert tracker.percent("p1") is None

    tracker.apply({"type": "progress", "data": {"value": 0, "max": 20, "prompt_id": "p1"}})
    assert tracker.percent("p1") == 0
    assert tracker.snapshot() == {"p1": 0}
    assert tracker.percent("never-submitted") is None


def test_progress_is_attributed_by_prompt_id_and_never_crosses_between_jobs():
    """A batch of renders is the normal case. Two prompts, interleaved, each keeps its own."""
    tracker = ProgressTracker()
    for value in (2, 4, 6, 8):
        tracker.apply({"type": "progress", "data": {"value": value, "max": 20, "prompt_id": "a"}})
        tracker.apply(
            {"type": "progress", "data": {"value": value * 2, "max": 20, "prompt_id": "b"}}
        )

    assert tracker.snapshot() == {"a": 40, "b": 80}
    tracker.forget("a")
    assert tracker.percent("a") is None
    assert tracker.percent("b") == 80


def test_a_percentage_never_moves_backwards_within_one_prompt():
    """ComfyUI's denominator can grow -- a second sampler, a re-reported node -- and a bar that
    goes backwards reads as a bug. The floor is still a true statement about work already done."""
    tracker = ProgressTracker()
    tracker.apply({"type": "progress", "data": {"value": 18, "max": 20, "prompt_id": "p1"}})
    tracker.apply({"type": "progress", "data": {"value": 1, "max": 20, "prompt_id": "p1"}})
    assert tracker.percent("p1") == 90


@pytest.mark.parametrize(
    "message",
    [
        None,
        "not a dict",
        b"bytes",
        42,
        {},
        {"type": "progress"},
        {"type": "progress", "data": None},
        {"type": "progress_state", "data": {"nodes": {}}},
        {"type": "progress_state", "data": {"prompt_id": "p", "nodes": {}}},
        {"type": "progress_state", "data": {"prompt_id": "p", "nodes": "not a map"}},
        {
            "type": "progress_state",
            "data": {"prompt_id": "", "nodes": {"1": {"value": 1, "max": 4}}},
        },
        {"type": "progress_state", "data": {"prompt_id": "p", "nodes": {"1": "not a node"}}},
        # Every node a one-unit bar: real, and not a measurement of anything.
        {
            "type": "progress_state",
            "data": {"prompt_id": "p", "nodes": {"1": {"value": 1, "max": 1}}},
        },
        {"type": "progress", "data": {"value": 1, "max": 1, "prompt_id": "p"}},
        {"type": "progress", "data": {"value": 1, "max": 0, "prompt_id": "p"}},
        {"type": "progress", "data": {"value": 1, "max": -5, "prompt_id": "p"}},
        {"type": "progress", "data": {"value": "3", "max": "20", "prompt_id": "p"}},
        {"type": "progress", "data": {"value": True, "max": 20, "prompt_id": "p"}},
        {"type": "progress", "data": {"value": 1, "max": float("nan"), "prompt_id": "p"}},
        {"type": "progress", "data": {"value": 1, "max": float("inf"), "prompt_id": "p"}},
        {"type": "progress", "data": {"value": float("nan"), "max": 20, "prompt_id": "p"}},
        {"type": "progress", "data": {"value": 1, "max": 20}},
        # The other events on that socket, none of which carries a fraction.
        {"type": "status", "data": {"status": {"exec_info": {"queue_remaining": 1}}}},
        {"type": "executing", "data": {"node": "15", "prompt_id": "p"}},
        {"type": "executed", "data": {"node": "9", "prompt_id": "p", "output": {}}},
        {"type": "execution_error", "data": {"prompt_id": "p"}},
        {"type": "crystools.monitor", "data": {"cpu_utilization": 8.3}},
        # A shape from some future build, which must be dropped rather than guessed at.
        {"type": "progress_v2", "data": {"prompt_id": "p", "fraction": 0.5}},
    ],
)
def test_unreadable_messages_are_ignored_rather_than_raised(message):
    """A parser that can raise inside a socket loop can take the socket down with it. Every
    unknown, malformed or fractionless message answers None -- and never a fabricated number."""
    assert progress_from_message(message) is None
    tracker = ProgressTracker()
    assert tracker.apply(message) is None
    assert tracker.snapshot() == {}


def test_a_value_past_its_maximum_is_clamped_rather_than_reported_over_a_hundred():
    tracker = ProgressTracker()
    tracker.apply({"type": "progress", "data": {"value": 99, "max": 20, "prompt_id": "p1"}})
    assert tracker.percent("p1") == 100


def test_the_tracker_is_bounded_so_a_long_session_cannot_grow_it_without_limit():
    tracker = ProgressTracker(capacity=3)
    for index in range(6):
        tracker.apply(
            {"type": "progress", "data": {"value": 5, "max": 20, "prompt_id": f"p{index}"}}
        )
    assert sorted(tracker.snapshot()) == ["p3", "p4", "p5"]
    assert tracker.percent("p0") is None


def test_the_listener_ingests_raw_text_and_drops_anything_that_is_not_json():
    listener = ComfyProgressListener("http://comfy", ProgressTracker())
    assert listener.ingest('{"type":"progress","data":{"value":5,"max":20,"prompt_id":"p"}}') == "p"
    assert listener.tracker.percent("p") == 25
    assert listener.ingest("<html>not json</html>") is None
    assert listener.ingest(b"\xff\xfe not utf-8") is None
    assert listener.ingest("") is None
    assert listener.tracker.percent("p") == 25


class ScriptedSocket:
    """A socket double: yields the frames it was given, then fails the way it was told to."""

    def __init__(self, frames, ending=None):
        self._frames = list(frames)
        self._ending = ending or WebSocketClosed("peer went away")
        self.closed = False

    async def receive(self):
        if self._frames:
            return self._frames.pop(0)
        raise self._ending

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_a_refused_socket_backs_off_instead_of_spinning_and_shows_no_percentage():
    """ComfyUI down -- at boot or later -- is the ordinary case, and it must cost a retry on a
    growing delay and nothing else. No percentage is the only consequence."""
    delays: list[float] = []
    attempts = 0

    async def refuse():
        nonlocal attempts
        attempts += 1
        raise OSError("connection refused")

    async def sleep(seconds):
        delays.append(seconds)
        if len(delays) >= 6:
            raise asyncio.CancelledError

    listener = ComfyProgressListener(
        "http://comfy", connect=refuse, sleep=sleep, min_backoff=1.0, max_backoff=8.0
    )
    with pytest.raises(asyncio.CancelledError):
        await listener.run()

    assert delays == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]
    assert attempts == 6
    assert listener.tracker.snapshot() == {}


@pytest.mark.asyncio
async def test_a_socket_that_drops_mid_render_reconnects_and_keeps_the_floor_it_had():
    """A ComfyUI restart mid-render, and the reconnect that follows. The percentage already
    learned is not thrown away, and the second connection carries on from it."""
    sockets = [
        ScriptedSocket(['{"type":"progress","data":{"value":5,"max":20,"prompt_id":"p"}}']),
        ScriptedSocket(['{"type":"progress","data":{"value":15,"max":20,"prompt_id":"p"}}']),
    ]
    opened = []
    delays: list[float] = []

    async def connect():
        if not sockets:
            raise asyncio.CancelledError
        socket = sockets.pop(0)
        opened.append(socket)
        return socket

    async def sleep(seconds):
        delays.append(seconds)

    listener = ComfyProgressListener("http://comfy", connect=connect, sleep=sleep)
    with pytest.raises(asyncio.CancelledError):
        await listener.run()

    assert [socket.closed for socket in opened] == [True, True]
    assert listener.tracker.percent("p") == 75
    # Traffic on a connection resets the backoff, so an ordinary drop is retried promptly -- and
    # the floor of one second is what keeps even a flapping socket off a hot loop.
    assert delays == [1.0, 1.0]


@pytest.mark.asyncio
async def test_a_socket_that_accepts_and_delivers_nothing_backs_off_like_one_that_refuses():
    """The flapping case, which is the one that can spin hot. ComfyUI accepts the connection and
    drops it immediately -- a restart in progress, a proxy in front of it, a half-open port. The
    backoff resets on *traffic*, not on a successful connect, so a socket that says nothing is
    retried on the same growing delay as a socket that refuses outright."""
    delays: list[float] = []

    async def connect():
        if len(delays) >= 4:
            raise asyncio.CancelledError
        return ScriptedSocket([])

    async def sleep(seconds):
        delays.append(seconds)

    listener = ComfyProgressListener(
        "http://comfy", connect=connect, sleep=sleep, min_backoff=1.0, max_backoff=8.0
    )
    with pytest.raises(asyncio.CancelledError):
        await listener.run()

    assert delays == [1.0, 2.0, 4.0, 8.0]


@pytest.mark.asyncio
async def test_a_socket_that_only_talks_nonsense_never_crashes_the_loop():
    """Frames this build does not recognise are dropped, one at a time, forever if need be."""
    socket = ScriptedSocket(
        [
            "not json at all",
            '{"type":"crystools.monitor","data":{"cpu_utilization":8.3}}',
            '{"type":"progress_v2","data":{"prompt_id":"p","fraction":0.5}}',
            None,  # a binary frame: traffic, but nothing to read
            '{"type":"progress","data":{"value":10,"max":20,"prompt_id":"p"}}',
        ]
    )

    async def connect():
        if socket.closed:
            raise asyncio.CancelledError
        return socket

    async def sleep(_seconds):
        return None

    listener = ComfyProgressListener("http://comfy", connect=connect, sleep=sleep)
    with pytest.raises(asyncio.CancelledError):
        await listener.run()

    assert listener.tracker.percent("p") == 50


@pytest.mark.asyncio
async def test_starting_and_stopping_the_listener_leaks_neither_task_nor_socket():
    """App shutdown. The task is cancelled, the socket is closed, and `stop` is safe to call on
    a listener that was never started.

    The socket blocks forever rather than ending itself, which is the whole point: a scripted
    socket that raises on the first read finishes the task on its own, and then "the task is
    done" is true whether or not anything cancelled it. Only a read that never returns makes
    `task.cancel()` the one thing that can end this."""

    class BlockingSocket:
        def __init__(self):
            self.closed = False

        async def receive(self):
            await asyncio.Event().wait()  # never set: only cancellation ends this

        async def close(self):
            self.closed = True

    socket = BlockingSocket()

    async def connect():
        return socket

    async def sleep(_seconds):
        await asyncio.sleep(0)

    listener = ComfyProgressListener("http://comfy", connect=connect, sleep=sleep)
    await listener.stop()  # never started: a no-op, not an error

    listener.start()
    listener.start()  # idempotent
    await asyncio.sleep(0)
    # Held before `stop`, which clears the reference: "the listener says it is not running" is
    # not the same claim as "the task is actually finished", and only the second one is the
    # difference between a clean shutdown and a task left reading a socket forever.
    task = listener._task
    await listener.stop()
    assert task.done() is True
    assert socket.closed is True
    assert listener.running is False
    assert listener.stops == 2  # the no-op call above counts: `stop` is safe either way


# --- The wire itself, against a real localhost server that speaks RFC 6455 --------------------


async def serve_websocket(frames, handshake=b"HTTP/1.1 101 Switching Protocols\r\n\r\n"):
    """A one-connection server that completes the upgrade and writes `frames` verbatim."""
    received: list[bytes] = []

    async def handle(reader, writer):
        while True:
            line = await reader.readline()
            received.append(line)
            if line in (b"\r\n", b""):
                break
        writer.write(handshake)
        for frame in frames:
            writer.write(frame)
        await writer.drain()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, received


def ws_frame(payload: bytes, opcode: int = 0x1, fin: bool = True) -> bytes:
    return bytes([(0x80 if fin else 0) | opcode, len(payload)]) + payload


@pytest.mark.asyncio
async def test_the_websocket_client_reads_text_ping_and_close_off_a_real_socket():
    frames = [
        ws_frame(b'{"type":"status","data":{}}'),
        ws_frame(b'{"type":"prog', 0x1, False),
        ws_frame(b'ress","data":{}}', 0x0, True),
        ws_frame(b"\x00\x01\x02", 0x2),  # binary: traffic with nothing to read
        ws_frame(b"hi", 0x9),  # ping
        ws_frame(b"", 0x8),  # close
    ]
    server, received = await serve_websocket(frames)
    port = server.sockets[0].getsockname()[1]
    async with server:
        socket = await ComfyWebSocket.connect(f"http://127.0.0.1:{port}", client_id="probe")
        assert await socket.receive() == '{"type":"status","data":{}}'
        assert await socket.receive() == '{"type":"progress","data":{}}'
        assert await socket.receive() is None
        # Matched on the wording, not just the type: without the close-opcode branch a close
        # frame falls through to the unknown-opcode arm and raises the *same class* for the
        # wrong reason, which a bare `pytest.raises` would call a pass.
        with pytest.raises(WebSocketClosed, match="closed the progress socket"):
            await socket.receive()
        await socket.close()

    request = b"".join(received).decode("ascii")
    assert request.startswith("GET /ws?clientId=probe HTTP/1.1")
    assert "Upgrade: websocket" in request
    assert "Sec-WebSocket-Version: 13" in request


@pytest.mark.asyncio
async def test_a_server_that_refuses_the_upgrade_is_a_closed_socket_and_not_a_crash():
    server, _ = await serve_websocket([], b"HTTP/1.1 400 Bad Request\r\n\r\n")
    port = server.sockets[0].getsockname()[1]
    async with server:
        with pytest.raises(WebSocketClosed, match="did not upgrade"):
            await ComfyWebSocket.connect(f"http://127.0.0.1:{port}", client_id="probe")


@pytest.mark.asyncio
async def test_an_absurd_frame_length_is_refused_rather_than_allocated():
    """A 64-bit length field is sixteen exabytes of rope. The connection ends; the app does not.

    Under a deadline, because the failure this guards against is not an exception -- it is a read
    that never returns. Without the size check the client sits in `readexactly` waiting for a
    terabyte that is never coming, and a mutation test with no timeout waits with it: measured,
    that hung a whole mutation run for forty-five minutes and looked like a slow suite."""
    server, _ = await serve_websocket([bytes([0x81, 0xFF]) + (2**40).to_bytes(8, "big")])
    port = server.sockets[0].getsockname()[1]
    async with server:
        socket = await ComfyWebSocket.connect(f"http://127.0.0.1:{port}", client_id="probe")
        with pytest.raises(WebSocketClosed, match="Refusing"):
            await asyncio.wait_for(socket.receive(), timeout=5)
        await socket.close()
