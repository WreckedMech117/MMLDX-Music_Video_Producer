"""The VRAM eject before a ComfyUI render.

Two families of test live here, and the second matters more than the first.

The first proves the eject does what it claims: it fires only when something is resident,
and it believes the host's model listing rather than the mechanism's exit code.

The second proves the eject never costs a render. Every failure mode is replayed through a
real `ComfyClient.submit` against a mock transport, asserting the prompt was queued anyway.
A suite that only proved the eject works would not notice the day it started breaking
renders — which is the strictly worse failure, because a held gigabyte is a slow render and
a swallowed submission is no render at all.

Nothing here contacts LM Studio, ComfyUI, or the `lms` CLI. The subprocess tests drive the
real `CliUnloader` against `sys.executable`, so the process plumbing — exit codes, kills on
timeout, closed stdin — is genuinely exercised without touching the vendor tool.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

import httpx
import pytest

from music_video_producer.comfy import ComfyClient
from music_video_producer.config import Settings
from music_video_producer.vram import (
    CliUnloader,
    EjectStatus,
    LlmEjector,
    UnloaderFailed,
    UnloaderUnavailable,
    models_url,
    resident_models,
)

BASE_URL = "http://lmstudio.test:1234/v1"
MODELS_PATH = "/api/v1/models"


def listing(*keys: str) -> dict:
    """An `/api/v1/models` body in LM Studio's real shape, with `keys` resident."""
    return {
        "models": [
            {
                "type": "llm",
                "key": key,
                "loaded_instances": [{"identifier": f"{key}:1"}] if loaded else [],
            }
            for key, loaded in (
                *((key, True) for key in keys),
                ("some-model-on-disk", False),
            )
        ]
    }


class FakeUnloader:
    """A mechanism whose behaviour each test dictates, and which records being asked."""

    name = "fake"

    def __init__(self, *, raises: BaseException | None = None, delay: float = 0.0) -> None:
        self.raises = raises
        self.delay = delay
        self.calls: list[float] = []

    async def unload_all(self, *, timeout: float) -> str:
        self.calls.append(timeout)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        return "fake unloaded everything"


class Host:
    """A stand-in LM Studio whose listing can change between the two probes.

    `bodies` is consumed one entry per request, the last entry repeating. That is what lets
    a single fixture express "loaded, then empty" (a real release) and "loaded, then still
    loaded" (a mechanism that lied) without any conditional logic in the test.
    """

    def __init__(self, *bodies: dict | Exception) -> None:
        self.bodies = list(bodies)
        self.requests: list[str] = []

    def transport(self) -> httpx.MockTransport:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request.url.path)
            body = self.bodies[min(len(self.requests) - 1, len(self.bodies) - 1)]
            if isinstance(body, Exception):
                raise body
            return httpx.Response(200, json=body)

        return httpx.MockTransport(handler)


def make_ejector(host: Host, unloader=None, **kwargs) -> LlmEjector:
    return LlmEjector(
        base_url=BASE_URL,
        unloader=unloader if unloader is not None else FakeUnloader(),
        transport=host.transport(),
        **kwargs,
    )


# --------------------------------------------------------------------------------------
# Reading what is resident
# --------------------------------------------------------------------------------------


def test_models_url_is_derived_from_the_chat_base_url():
    assert models_url("http://127.0.0.1:1234/v1") == "http://127.0.0.1:1234/api/v1/models"
    assert models_url("http://127.0.0.1:1234/v1/") == "http://127.0.0.1:1234/api/v1/models"
    # A proxied host keeps its prefix rather than resolving to the proxy's root.
    assert models_url("https://box/lmstudio/v1") == "https://box/lmstudio/api/v1/models"
    assert models_url("") == ""
    assert models_url("not-a-url") == ""


def test_resident_models_reads_the_v1_loaded_instances_shape():
    assert resident_models(listing()) == ()
    assert resident_models(listing("qwen")) == ("qwen:1",)


def test_resident_models_also_reads_the_v0_state_shape():
    """A host answering the other documented shape must be read, not reported as empty."""
    payload = {
        "models": [
            {"id": "loaded-one", "state": "loaded"},
            {"id": "cold-one", "state": "not-loaded"},
        ]
    }
    assert resident_models(payload) == ("loaded-one",)


@pytest.mark.parametrize(
    "payload",
    [
        ["not", "an", "object"],
        {"data": []},
        {"models": [{"key": "mystery", "residency": "who knows"}]},
    ],
)
def test_an_unreadable_listing_never_reads_as_nothing_loaded(payload):
    """The one thing the parser must never do is guess "empty" for a shape it cannot read.

    An empty tuple here would make every unreadable answer look like a successful release —
    the same lie as trusting an exit code, told by a different part of the system.
    """
    with pytest.raises(RuntimeError):
        resident_models(payload)


# --------------------------------------------------------------------------------------
# The eject decision
# --------------------------------------------------------------------------------------


async def test_nothing_loaded_skips_the_mechanism_entirely_and_says_nothing(caplog):
    host = Host(listing())
    unloader = FakeUnloader()
    with caplog.at_level(logging.DEBUG, logger="music_video_producer.vram"):
        outcome = await make_ejector(host, unloader).eject()

    assert outcome.status is EjectStatus.NOTHING_LOADED
    assert unloader.calls == []
    assert host.requests == [MODELS_PATH]
    assert caplog.records == []


async def test_a_loaded_model_is_ejected_and_the_release_is_confirmed_over_http():
    host = Host(listing("qwen"), listing())
    unloader = FakeUnloader()
    outcome = await make_ejector(host, unloader).eject()

    assert outcome.status is EjectStatus.RELEASED
    assert outcome.released is True
    assert outcome.resident_before == ("qwen:1",)
    assert unloader.calls == [20.0]
    # Twice: once to decide there was anything to do, once to confirm it was done.
    assert host.requests == [MODELS_PATH, MODELS_PATH]


async def test_a_mechanism_that_lies_is_a_failure_not_a_success(caplog):
    """`unload_all` returned happily and the model is still there. This is the whole story.

    An eject that trusted its mechanism would report success here while the render competed
    for the same VRAM it always did — indistinguishable from never having shipped.
    """
    host = Host(listing("qwen"))  # still resident on the second probe
    unloader = FakeUnloader()
    with caplog.at_level(logging.INFO, logger="music_video_producer.vram"):
        outcome = await make_ejector(host, unloader).eject()

    assert outcome.status is EjectStatus.STILL_RESIDENT
    assert outcome.released is False
    assert outcome.is_failure is True
    assert outcome.resident_after == ("qwen:1",)
    assert unloader.calls == [20.0]
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert "still resident" in caplog.records[0].getMessage()


async def test_an_absent_host_is_skipped_silently(caplog):
    host = Host(httpx.ConnectError("connection refused"))
    unloader = FakeUnloader()
    with caplog.at_level(logging.DEBUG, logger="music_video_producer.vram"):
        outcome = await make_ejector(host, unloader).eject()

    assert outcome.status is EjectStatus.HOST_UNREACHABLE
    assert unloader.calls == []
    assert caplog.records == []


async def test_an_absent_mechanism_is_recorded_once_and_not_raised(caplog):
    host = Host(listing("qwen"))
    unloader = FakeUnloader(raises=UnloaderUnavailable("no `lms` executable"))
    with caplog.at_level(logging.INFO, logger="music_video_producer.vram"):
        outcome = await make_ejector(host, unloader).eject()

    assert outcome.status is EjectStatus.MECHANISM_ABSENT
    assert len(caplog.records) == 1
    assert "mechanism-absent" in caplog.records[0].getMessage()
    # Named, then dropped: the eject does not go on to probe again or retry.
    assert host.requests == [MODELS_PATH]


async def test_a_failing_mechanism_is_recorded_once_and_not_raised(caplog):
    host = Host(listing("qwen"))
    unloader = FakeUnloader(raises=UnloaderFailed("exited 1: something broke"))
    with caplog.at_level(logging.INFO, logger="music_video_producer.vram"):
        outcome = await make_ejector(host, unloader).eject()

    assert outcome.status is EjectStatus.MECHANISM_FAILED
    assert len(caplog.records) == 1


async def test_a_hanging_mechanism_is_abandoned_within_the_bound(caplog):
    host = Host(listing("qwen"))
    unloader = FakeUnloader(raises=TimeoutError("did not return within 0.05s"))
    with caplog.at_level(logging.INFO, logger="music_video_producer.vram"):
        outcome = await make_ejector(host, unloader, unload_timeout=0.05).eject()

    assert outcome.status is EjectStatus.TIMED_OUT
    assert unloader.calls == [0.05]
    assert len(caplog.records) == 1


async def test_a_mechanism_that_throws_something_unexpected_cannot_escape():
    """An injected unloader is arbitrary code; nothing it raises may reach the render."""
    host = Host(listing("qwen"))
    unloader = FakeUnloader(raises=MemoryError("not even this"))
    outcome = await make_ejector(host, unloader).eject()

    assert outcome.status is EjectStatus.MECHANISM_FAILED


async def test_a_broken_busy_gate_cannot_escape_the_eject_or_stop_the_render():
    """`is_busy` is injected by the application, so it is arbitrary code like the unloader.

    The application supplies `getattr(director, "busy", False)` against a Director that may
    be a double, a subclass, or a property that throws. Nothing before the unloader is
    inside the per-mechanism catches, so this is what the outer catch in `eject` is for —
    and without this test that catch was unexercised and could be deleted unnoticed.
    """

    def is_busy() -> bool:
        raise RuntimeError("the busy gate is broken")

    ejector = make_ejector(Host(listing("qwen")), is_busy=is_busy)
    outcome = await ejector.eject()
    assert outcome.status is EjectStatus.MECHANISM_FAILED

    comfy = Comfy(before_submit=ejector.eject)
    submission = await comfy.client.submit({"1": {"class_type": "SaveImage", "inputs": {}}})
    assert submission.prompt_id == "prompt-1"


async def test_a_release_that_cannot_be_confirmed_is_not_claimed_as_one(caplog):
    """The host stopped answering after the unload. That is unknown, and unknown is failure."""
    host = Host(listing("qwen"), httpx.ConnectError("gone"))
    with caplog.at_level(logging.INFO, logger="music_video_producer.vram"):
        outcome = await make_ejector(host).eject()

    assert outcome.status is EjectStatus.UNREADABLE
    assert outcome.released is False
    assert len(caplog.records) == 1


async def test_an_unreadable_listing_stops_the_eject_before_it_unloads_anything(caplog):
    """Nothing was confirmed loaded, so nothing is ejected — the boundary, not an accident."""
    host = Host({"models": [{"key": "mystery"}]})
    unloader = FakeUnloader()
    with caplog.at_level(logging.INFO, logger="music_video_producer.vram"):
        outcome = await make_ejector(host, unloader).eject()

    assert outcome.status is EjectStatus.UNREADABLE
    assert unloader.calls == []
    assert len(caplog.records) == 1


async def test_no_eject_while_a_director_request_is_in_flight(caplog):
    host = Host(listing("qwen"))
    unloader = FakeUnloader()
    with caplog.at_level(logging.DEBUG, logger="music_video_producer.vram"):
        outcome = await make_ejector(host, unloader, is_busy=lambda: True).eject()

    assert outcome.status is EjectStatus.DIRECTOR_BUSY
    assert unloader.calls == []
    assert host.requests == []
    assert caplog.records == []


async def test_disabled_touches_neither_the_host_nor_the_mechanism(caplog):
    host = Host(listing("qwen"))
    unloader = FakeUnloader()
    with caplog.at_level(logging.DEBUG, logger="music_video_producer.vram"):
        outcome = await make_ejector(host, unloader, enabled=False).eject()

    assert outcome.status is EjectStatus.DISABLED
    assert host.requests == []
    assert unloader.calls == []
    assert caplog.records == []


async def test_an_unconfigured_host_never_probes_anything():
    unloader = FakeUnloader()
    ejector = LlmEjector(base_url="", unloader=unloader)
    outcome = await ejector.eject()

    assert outcome.status is EjectStatus.NOT_CONFIGURED
    assert unloader.calls == []
    await ejector.close()


async def test_repeated_submissions_cost_one_probe_each_after_the_first():
    """Nothing reloads the model between queued renders, so later ejects find nothing."""
    host = Host(listing("qwen"), listing())
    unloader = FakeUnloader()
    ejector = make_ejector(host, unloader)

    assert (await ejector.eject()).status is EjectStatus.RELEASED
    assert (await ejector.eject()).status is EjectStatus.NOTHING_LOADED
    assert (await ejector.eject()).status is EjectStatus.NOTHING_LOADED
    assert len(unloader.calls) == 1
    assert len(host.requests) == 4


# --------------------------------------------------------------------------------------
# The CLI mechanism itself, without the vendor tool
# --------------------------------------------------------------------------------------


async def test_cli_unloader_reports_a_clean_exit():
    unloader = CliUnloader(sys.executable, arguments=("-c", "print('Unloaded 1 model.')"))
    note = await unloader.unload_all(timeout=30)
    assert "Unloaded 1 model." in note


async def test_cli_unloader_raises_on_a_non_zero_exit():
    unloader = CliUnloader(
        sys.executable, arguments=("-c", "import sys; print('nope'); sys.exit(3)")
    )
    with pytest.raises(UnloaderFailed, match="exited 3"):
        await unloader.unload_all(timeout=30)


async def test_cli_unloader_kills_a_process_that_hangs():
    """A wedged CLI is abandoned on a bound, not waited on, and does not outlive the call."""
    unloader = CliUnloader(sys.executable, arguments=("-c", "import time; time.sleep(60)"))
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await unloader.unload_all(timeout=0.5)
    assert time.monotonic() - started < 20


async def test_cli_unloader_closes_stdin_so_an_interactive_prompt_cannot_wedge_it():
    """Bare `lms unload` prompts when several models are loaded. Closed stdin ends that."""
    unloader = CliUnloader(
        sys.executable, arguments=("-c", "import sys; print(repr(sys.stdin.read()))")
    )
    note = await unloader.unload_all(timeout=30)
    assert "''" in note


async def test_cli_unloader_reports_a_missing_executable_as_absent_not_broken():
    unloader = CliUnloader(str(Path("no-such-directory") / "lms-does-not-exist"))
    with pytest.raises(UnloaderUnavailable):
        await unloader.unload_all(timeout=30)


def test_cli_unloader_discovery_returns_a_string_or_nothing(monkeypatch, tmp_path):
    """Discovery is per call, and a machine without LM Studio yields "" rather than raising."""
    monkeypatch.setattr("shutil.which", lambda _: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert CliUnloader.discover() == ""


# --------------------------------------------------------------------------------------
# The submission still happens. Every time.
# --------------------------------------------------------------------------------------


class Comfy:
    """A real `ComfyClient` over a mock transport, recording what reached `/prompt`."""

    def __init__(self, before_submit=None) -> None:
        self.submitted: list[dict] = []
        self.client = ComfyClient(
            "http://comfy.test",
            transport=httpx.MockTransport(self._handler),
            before_submit=before_submit,
        )

    async def _handler(self, request: httpx.Request) -> httpx.Response:
        self.submitted.append({"path": request.url.path})
        return httpx.Response(200, json={"prompt_id": "prompt-1", "number": 1})


@pytest.mark.parametrize(
    ("name", "host", "unloader", "expected"),
    [
        ("nothing loaded", Host(listing()), FakeUnloader(), EjectStatus.NOTHING_LOADED),
        (
            "released",
            Host(listing("qwen"), listing()),
            FakeUnloader(),
            EjectStatus.RELEASED,
        ),
        (
            "host absent",
            Host(httpx.ConnectError("refused")),
            FakeUnloader(),
            EjectStatus.HOST_UNREACHABLE,
        ),
        (
            "mechanism absent",
            Host(listing("qwen")),
            FakeUnloader(raises=UnloaderUnavailable("no lms")),
            EjectStatus.MECHANISM_ABSENT,
        ),
        (
            "mechanism lies",
            Host(listing("qwen")),
            FakeUnloader(),
            EjectStatus.STILL_RESIDENT,
        ),
        (
            "mechanism fails",
            Host(listing("qwen")),
            FakeUnloader(raises=UnloaderFailed("exited 1")),
            EjectStatus.MECHANISM_FAILED,
        ),
        (
            "mechanism hangs",
            Host(listing("qwen")),
            FakeUnloader(raises=TimeoutError("hung")),
            EjectStatus.TIMED_OUT,
        ),
        (
            "listing unreadable",
            Host({"models": [{"key": "mystery"}]}),
            FakeUnloader(),
            EjectStatus.UNREADABLE,
        ),
    ],
)
async def test_the_render_is_submitted_whatever_the_eject_does(name, host, unloader, expected):
    """The acceptance criterion that outranks every other one in this file."""
    outcomes = []

    ejector = make_ejector(host, unloader, unload_timeout=0.05)

    async def hook():
        outcomes.append(await ejector.eject())

    comfy = Comfy(before_submit=hook)
    submission = await comfy.client.submit({"1": {"class_type": "SaveImage", "inputs": {}}})

    assert submission.prompt_id == "prompt-1", name
    assert comfy.submitted == [{"path": "/prompt"}], name
    assert outcomes[0].status is expected, name


async def test_a_hook_that_raises_outright_still_submits(caplog):
    """Belt and braces. `LlmEjector.eject` cannot raise, but `submit` does not rely on that."""

    async def hook():
        raise RuntimeError("the hook itself is broken")

    comfy = Comfy(before_submit=hook)
    with caplog.at_level(logging.WARNING, logger="music_video_producer.comfy"):
        submission = await comfy.client.submit({"1": {"class_type": "SaveImage", "inputs": {}}})

    assert submission.prompt_id == "prompt-1"
    assert comfy.submitted == [{"path": "/prompt"}]
    assert "submitting anyway" in caplog.records[0].getMessage()


async def test_a_hook_that_hangs_is_bounded_by_the_hook_and_the_render_still_goes():
    """`submit` imposes no timeout of its own, so the bound must live inside the hook.

    That is deliberate — `submit` cannot know what a hook's honest budget is — and it is why
    `LlmEjector` bounds both its probes and its unload, and why the CLI hang test above
    measures the wall clock. Here the hook hits its own bound, the timeout is absorbed, and
    the prompt is queued a hair later rather than never.
    """

    async def hook():
        await asyncio.wait_for(asyncio.sleep(60), timeout=0.05)

    comfy = Comfy(before_submit=hook)
    started = time.monotonic()
    submission = await comfy.client.submit({"1": {"class_type": "SaveImage", "inputs": {}}})

    assert submission.prompt_id == "prompt-1"
    assert comfy.submitted == [{"path": "/prompt"}]
    assert time.monotonic() - started < 20


async def test_the_eject_runs_before_the_prompt_is_sent_not_after():
    """Ordering is the feature. VRAM released after the job is queued releases nothing."""
    order: list[str] = []
    host = Host(listing("qwen"), listing())
    ejector = make_ejector(host)

    async def hook():
        order.append("eject")
        await ejector.eject()

    async def handler(request: httpx.Request) -> httpx.Response:
        order.append("submit")
        return httpx.Response(200, json={"prompt_id": "p", "number": 1})

    client = ComfyClient(
        "http://comfy.test", transport=httpx.MockTransport(handler), before_submit=hook
    )
    await client.submit({"1": {"class_type": "SaveImage", "inputs": {}}})

    assert order == ["eject", "submit"]


async def test_a_client_with_no_hook_behaves_exactly_as_before():
    comfy = Comfy()
    submission = await comfy.client.submit({"1": {"class_type": "SaveImage", "inputs": {}}})
    assert submission.prompt_id == "prompt-1"


# --------------------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------------------


def test_create_app_wires_one_ejector_into_the_default_comfy_client(tmp_path: Path):
    """One hook covers all five submission routes and any route added after them."""
    from music_video_producer.app import create_app
    from music_video_producer.store import ProjectStore

    settings = Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy")
    app = create_app(settings=settings, store=ProjectStore(tmp_path))

    assert app.state.comfy.before_submit == app.state.ejector.eject


def test_an_injected_comfy_client_gets_no_hook_so_existing_suites_stay_offline(tmp_path: Path):
    """Why `tests/test_api.py` gained no dependency on a language-model host."""
    from music_video_producer.app import create_app
    from music_video_producer.store import ProjectStore

    class InjectedComfy:
        before_submit = "untouched"

    settings = Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy")
    app = create_app(settings=settings, store=ProjectStore(tmp_path), comfy=InjectedComfy())

    assert app.state.comfy.before_submit == "untouched"


def test_the_director_busy_gate_survives_a_director_double(tmp_path: Path):
    """An injected Director has never heard of `busy`; the gate must read that as idle."""
    from music_video_producer.app import create_app
    from music_video_producer.store import ProjectStore

    settings = Settings(data_root=tmp_path, comfy_root=tmp_path / "comfy")
    app = create_app(
        settings=settings, store=ProjectStore(tmp_path), director=object()
    )

    assert app.state.ejector._is_busy() is False


async def test_the_director_reports_itself_busy_only_while_a_call_is_outstanding():
    from music_video_producer.director import DirectorClient

    seen: list[bool] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(client.busy)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    client = DirectorClient(
        base_url="http://llm.test/v1",
        model="m",
        transport=httpx.MockTransport(handler),
    )

    assert client.busy is False
    await client._completion(body={"model": "m"}, headers={})
    assert seen == [True]
    assert client.busy is False


async def test_the_director_busy_counter_unwinds_when_a_call_fails():
    """A raised counter would wedge the eject off for the life of the process."""
    from music_video_producer.director import DirectorClient

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = DirectorClient(
        base_url="http://llm.test/v1",
        model="m",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(httpx.ConnectError):
        await client._completion(body={"model": "m"}, headers={})
    assert client.busy is False


def test_the_eject_is_on_by_default_and_can_be_switched_off(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("MVP_LLM_EJECT_BEFORE_RENDER", raising=False)
    assert Settings(data_root=tmp_path).llm_eject_before_render is True

    monkeypatch.setenv("MVP_LLM_EJECT_BEFORE_RENDER", "false")
    assert Settings(data_root=tmp_path).llm_eject_before_render is False
