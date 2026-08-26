"""Drive a read-modify-write race between two real requests, without a clock.

Every entry this module serves is the same defect: a route reads the manifest, does something
that takes time, and writes the whole manifest back from the copy it read. Anything saved in
between is reverted, silently, with both requests answering 200. The 2026-08-19 incident is
that shape — one background shot save reverted thirty-two prompts and four singing flags — and
`ShotListRequest.updated_at` was the first guard against it.

Proving one of those windows shut needs the window held open on purpose. A test that fired two
requests and hoped the scheduler interleaved them would flake in both directions: green because
the race did not happen is the worst possible pass here. So the first request is parked at a
chosen point inside itself, the second runs to completion against a real `TestClient`, and only
then is the first let go.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


class Interleaved:
    """Hold one request at a chosen point while a second request runs to the end.

    `pause` is called from inside whatever the first request is doing when the second must
    land — a manifest read, an ffprobe, a `/prompt` round trip — and blocks there. It fires
    exactly once, on the first call, which needs no thread-local arming because `run` does not
    start the second request until the first is already parked inside it.

    Each `TestClient` request outside a `with` block opens its own blocking portal, so the two
    requests run on separate event loops and parking one cannot stall the other. That is also
    why `pause` may block an `async def`: the loop it stops belongs to the parked request.

    `fired` is asserted by every caller. An empty one means the second writer never landed in
    the window, so whatever the test went on to assert was measured against no race at all.
    """

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.released = threading.Event()
        self.fired: list[bool] = []

    def pause(self) -> None:
        if self.fired:
            return
        self.fired.append(True)
        self.reached.set()
        assert self.released.wait(60), "the second request never finished"

    def run(
        self, first: Callable[[], Any], second: Callable[[], Any], timeout: float = 60
    ) -> tuple[Any, Any]:
        """`first`'s answer and `second`'s, with `second` committed inside `first`'s window."""
        outcome: dict[str, Any] = {}
        failure: list[BaseException] = []

        def run_first() -> None:
            try:
                outcome["first"] = first()
            except BaseException as error:  # noqa: BLE001 - reported, never swallowed
                failure.append(error)
            finally:
                # Set unconditionally: a first request that raised or returned without ever
                # reaching `pause` must not leave the main thread waiting out the timeout.
                self.reached.set()

        thread = threading.Thread(target=run_first, daemon=True)
        thread.start()
        try:
            assert self.reached.wait(timeout), "the first request never reached the pause"
            outcome["second"] = second()
        finally:
            self.released.set()
            thread.join(timeout)
        if failure:
            raise failure[0]
        return outcome["first"], outcome.get("second")


def park_the_next_read(store, gate: Interleaved) -> None:
    """Park the next manifest read on `gate`, for a route with no await to park at.

    `ProjectStore.get` delegates to `read_for_update`, so patching that one instance attribute
    covers both spellings and every route reaches the gate the same way. Patched on the
    instance the app holds, so nothing the test wrote during setup is affected.
    """
    original = store.read_for_update

    def read(project_id: str):
        held = original(project_id)
        gate.pause()
        return held

    store.read_for_update = read
