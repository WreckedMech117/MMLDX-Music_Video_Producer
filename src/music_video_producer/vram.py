"""Release the language model's VRAM before a ComfyUI render.

The Director's language model and ComfyUI compete for the same card. LM Studio holds tens
of gigabytes while idle, so a render that starts with a model resident either fails to
allocate, spills into system RAM, or simply runs slowly. This module asks the host to let
go, then **observes** whether it did.

Two things about that observation carry the whole feature:

* The mechanism's own verdict is not evidence. `lms unload --all` exiting 0 while a model
  stays resident is indistinguishable from doing nothing at all, and the VRAM is the
  entire point — so the release is confirmed by re-reading the host's model listing, and
  "the command succeeded but something is still loaded" is recorded as a failure.
* A payload this module cannot parse is never read as "nothing is loaded". A tolerant
  parser that returns "empty" for a shape it does not understand would report every failed
  eject as a success, which is the same lie in a different costume.

Nothing here is ever allowed to stop a render. Every path returns an `EjectOutcome`, and
failures are logged exactly once rather than raised.

**On unloading over HTTP.** A REST unload would be strictly better than shelling out to a
vendor CLI, and it was tested for rather than assumed. LM Studio's HTTP server answers an
unknown path and a known-but-wrong-method path with the *same* body — `GET
/v1/chat/completions`, a route that provably exists for POST, returns `{"error":
"Unexpected endpoint or method. (GET /v1/chat/completions)"}`, byte-identical in form to
the reply for a nonsense path. So read-only probing cannot tell "there is no REST unload"
from "there is one, behind POST", and no status code from such a probe would be evidence
of anything. The corroborating evidence available without POSTing is the vendor's own CLI:
`lms.exe` contains no `api/v0` or `api/v1` route strings at all, but does contain
`ws://127.0.0.1:1234` and `unloadModel` — LM Studio's own tool unloads over its WebSocket
RPC channel, not over REST. On that basis the shipped mechanism is the CLI. It is confined
behind the three-line `Unloader` protocol, so a proven REST unload later is a new class and
one constructor argument, not a rewrite.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

#: Reading the model listing is one call to a process on this machine. A host that has not
#: answered in five seconds is not going to release anything useful before the render.
DEFAULT_PROBE_TIMEOUT = 5.0

#: Generous for `lms unload --all` — dropping a 22 GB model takes a second or two — while
#: still bounding the delay a wedged host can add to a submission.
DEFAULT_UNLOAD_TIMEOUT = 20.0


class EjectStatus(str, Enum):
    """Every way one eject attempt can end. All of them end with the render proceeding."""

    DISABLED = "disabled"
    NOT_CONFIGURED = "not-configured"
    DIRECTOR_BUSY = "director-busy"
    HOST_UNREACHABLE = "host-unreachable"
    NOTHING_LOADED = "nothing-loaded"
    RELEASED = "released"
    STILL_RESIDENT = "still-resident"
    MECHANISM_ABSENT = "mechanism-absent"
    MECHANISM_FAILED = "mechanism-failed"
    TIMED_OUT = "timed-out"
    UNREADABLE = "unreadable"


#: Ordinary states, not problems. A Director who never configured a language model must not
#: be told about VRAM on every render, and an eject that correctly found nothing to do has
#: nothing to report. Skipping while the Director is mid-call is a deliberate decision, not
#: a fault: a render is not worth cancelling a conversation the Director is waiting on.
SILENT_STATUSES = frozenset(
    {
        EjectStatus.DISABLED,
        EjectStatus.NOT_CONFIGURED,
        EjectStatus.DIRECTOR_BUSY,
        EjectStatus.HOST_UNREACHABLE,
        EjectStatus.NOTHING_LOADED,
    }
)

#: States where VRAM the render wanted is still held, or might be. `UNREADABLE` is here on
#: purpose: not knowing whether the model was released is a failure of this feature, not a
#: success, and reporting it as one is what would make the guarantee hollow.
FAILURE_STATUSES = frozenset(
    {
        EjectStatus.STILL_RESIDENT,
        EjectStatus.MECHANISM_ABSENT,
        EjectStatus.MECHANISM_FAILED,
        EjectStatus.TIMED_OUT,
        EjectStatus.UNREADABLE,
    }
)


@dataclass(frozen=True, slots=True)
class EjectOutcome:
    """What one eject attempt did, and what the host said afterwards."""

    status: EjectStatus
    detail: str = ""
    resident_before: tuple[str, ...] = field(default=())
    resident_after: tuple[str, ...] = field(default=())

    @property
    def released(self) -> bool:
        """True only when the host itself reported the models gone afterwards."""
        return self.status is EjectStatus.RELEASED

    @property
    def is_failure(self) -> bool:
        return self.status in FAILURE_STATUSES


class UnloaderUnavailable(RuntimeError):
    """No eject mechanism exists on this machine. A skipped optimisation, not a fault."""


class UnloaderFailed(RuntimeError):
    """The eject mechanism ran and reported failure."""


class Unloader(Protocol):
    """Whatever can ask the language-model host to drop its models.

    Deliberately three lines wide. A REST or WebSocket unload, once one can be *proven* to
    work, is another class satisfying this protocol — `LlmEjector` needs no change, and
    neither does the verification that follows it, because verification never trusted the
    unloader in the first place.
    """

    name: str

    async def unload_all(self, *, timeout: float) -> str:
        """Drop every resident model, or raise.

        Returns a short human phrase for the log. Raises `UnloaderUnavailable` when the
        mechanism is not installed, `TimeoutError` when it does not return within
        `timeout`, and `UnloaderFailed` (or anything else) when it ran and failed.
        """
        ...


class CliUnloader:
    """Unload by shelling out to LM Studio's own `lms` CLI.

    `--all` is not optional and is not configurable. Bare `lms unload` picks the single
    loaded model when there is exactly one, but *prompts interactively* when there are
    several — which, from a server process, is a command that never returns and burns the
    whole timeout. `stdin` is closed for the same reason.
    """

    name = "lms CLI"

    def __init__(
        self,
        executable: str | os.PathLike[str] | None = None,
        *,
        arguments: Sequence[str] = ("unload", "--all"),
    ) -> None:
        self._executable = str(executable) if executable else ""
        self._arguments = tuple(arguments)

    @staticmethod
    def discover() -> str:
        """The `lms` executable on this machine, or "" when there is none.

        Resolved per call rather than at startup so installing LM Studio while the
        application is running does not require a restart to benefit, and so a machine
        without it stays a skipped optimisation forever rather than a cached verdict.
        """
        found = shutil.which("lms")
        if found:
            return found
        candidate = Path.home() / ".lmstudio" / "bin" / ("lms.exe" if os.name == "nt" else "lms")
        return str(candidate) if candidate.is_file() else ""

    async def unload_all(self, *, timeout: float) -> str:
        executable = self._executable or self.discover()
        if not executable:
            raise UnloaderUnavailable(
                "no `lms` executable on PATH or at ~/.lmstudio/bin; "
                "the VRAM eject needs LM Studio's CLI installed"
            )
        command = (executable, *self._arguments)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as error:
            raise UnloaderUnavailable(f"`{executable}` could not be run: {error}") from error
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            # A hung unload must not outlive the request that started it, or the next
            # render inherits a process still holding the host's attention.
            process.kill()
            with suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=5.0)
            raise TimeoutError(
                f"`{executable}` did not return within {timeout:g}s and was killed"
            ) from None
        output = stdout.decode("utf-8", "replace").strip()
        if process.returncode != 0:
            raise UnloaderFailed(
                f"`{executable}` exited {process.returncode}: {output[:300] or 'no output'}"
            )
        return f"{self.name} reported: {output[:200] or 'nothing to say'}"


class _ProbeError(RuntimeError):
    """The host's model listing could not be read.

    `reachable` separates "there is no language-model host here", which is the ordinary
    state of a machine whose Director never configured one and is silent, from "the host
    answered with something this code cannot interpret", which is a failure worth naming.
    """

    def __init__(self, message: str, *, reachable: bool) -> None:
        super().__init__(message)
        self.reachable = reachable


def models_url(base_url: str) -> str:
    """The model listing URL implied by an OpenAI-compatible `base_url`.

    `/api/v1/models` is chosen over `/api/v0/models`. `v0` is LM Studio's explicitly
    experimental namespace and the one most likely to move under a version bump, and its
    per-model `state` answers "is this model loaded" when the question actually being asked
    is "what is resident right now" — which `loaded_instances` answers directly, naming the
    instances. `resident_models` still understands the `v0` shape, so a host that answers
    that way is read correctly rather than reported as empty.

    The `/v1` suffix of the chat base URL is stripped rather than the whole path, so a host
    reverse-proxied under a prefix resolves to `<prefix>/api/v1/models` and not to the
    proxy's root.
    """
    split = urlsplit(base_url.rstrip("/"))
    if not split.scheme or not split.netloc:
        return ""
    prefix = split.path.removesuffix("/v1")
    return urlunsplit((split.scheme, split.netloc, f"{prefix}/api/v1/models", "", ""))


def _instance_name(key: str, item: Any) -> str:
    if isinstance(item, dict):
        for name_field in ("identifier", "id", "instance_reference", "key"):
            value = item.get(name_field)
            if isinstance(value, str) and value:
                return value
    return key


def resident_models(payload: Any) -> tuple[str, ...]:
    """Every model instance the host reports as resident, from either listing shape.

    Raises `_ProbeError` for a payload whose residency cannot be read. That is the point of
    the function: returning an empty tuple for an unrecognised shape would make every
    unreadable answer look like a successful release, so the one thing this must never do
    is guess "nothing is loaded".
    """
    if not isinstance(payload, dict):
        raise _ProbeError("the model listing was not a JSON object", reachable=True)
    entries = payload.get("models")
    if not isinstance(entries, list):
        raise _ProbeError("the model listing carried no `models` array", reachable=True)
    resident: list[str] = []
    understood = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or entry.get("id") or "unnamed model")
        instances = entry.get("loaded_instances")
        if isinstance(instances, list):
            understood = True
            resident.extend(_instance_name(key, item) for item in instances)
            continue
        state = entry.get("state")
        if isinstance(state, str):
            understood = True
            if state == "loaded":
                resident.append(key)
    if entries and not understood:
        raise _ProbeError(
            "the model listing carried neither `loaded_instances` nor `state`, so what is "
            "resident cannot be read",
            reachable=True,
        )
    return tuple(resident)


class LlmEjector:
    """Ask the language-model host to release VRAM, and confirm over HTTP that it did."""

    def __init__(
        self,
        *,
        base_url: str,
        unloader: Unloader | None = None,
        enabled: bool = True,
        is_busy: Any = None,
        probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
        unload_timeout: float = DEFAULT_UNLOAD_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.enabled = enabled
        self.models_url = models_url(base_url) if base_url else ""
        self.unloader: Unloader = unloader if unloader is not None else CliUnloader()
        self.unload_timeout = unload_timeout
        self._is_busy = is_busy
        self._client = httpx.AsyncClient(timeout=probe_timeout, transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    async def eject(self) -> EjectOutcome:
        """Attempt one eject. Never raises, never blocks past its own timeouts.

        The broad catch is the last line of the "must never stop a render" guarantee: an
        injected unloader is arbitrary code, and there is no exception it can throw that is
        worth failing a GPU job over. `CancelledError` is a `BaseException` and still
        propagates, so a shutdown still shuts down.
        """
        try:
            outcome = await self._attempt()
        except Exception as error:  # noqa: BLE001 - a render outranks any eject failure
            outcome = EjectOutcome(EjectStatus.MECHANISM_FAILED, f"the eject raised {error!r}")
        self._record(outcome)
        return outcome

    async def _attempt(self) -> EjectOutcome:
        if not self.enabled:
            return EjectOutcome(EjectStatus.DISABLED, "eject before render is switched off")
        if not self.models_url:
            return EjectOutcome(EjectStatus.NOT_CONFIGURED, "no language-model host is configured")
        if self._is_busy is not None and self._is_busy():
            return EjectOutcome(EjectStatus.DIRECTOR_BUSY, "a Director request is in flight")

        # Read first, so nothing the application did not confirm was loaded is ever ejected,
        # and so the overwhelmingly common case — a Director who has not used the model this
        # session — costs one local HTTP call and no subprocess.
        try:
            before = await self._resident()
        except _ProbeError as error:
            if not error.reachable:
                return EjectOutcome(EjectStatus.HOST_UNREACHABLE, str(error))
            return EjectOutcome(EjectStatus.UNREADABLE, str(error))
        if not before:
            return EjectOutcome(EjectStatus.NOTHING_LOADED, "no model is resident")

        try:
            note = await self.unloader.unload_all(timeout=self.unload_timeout)
        except UnloaderUnavailable as error:
            return EjectOutcome(EjectStatus.MECHANISM_ABSENT, str(error), before)
        except TimeoutError as error:
            detail = str(error) or f"the eject did not return within {self.unload_timeout:g}s"
            return EjectOutcome(EjectStatus.TIMED_OUT, detail, before)
        except Exception as error:  # noqa: BLE001 - UnloaderFailed and anything else it throws
            return EjectOutcome(EjectStatus.MECHANISM_FAILED, str(error) or repr(error), before)

        # The mechanism's verdict is not evidence. Only the host's own listing is.
        try:
            after = await self._resident()
        except _ProbeError as error:
            return EjectOutcome(
                EjectStatus.UNREADABLE,
                f"the eject ran but the release could not be confirmed: {error}",
                before,
            )
        if after:
            return EjectOutcome(
                EjectStatus.STILL_RESIDENT,
                f"the eject reported success yet {', '.join(after)} is still resident",
                before,
                after,
            )
        return EjectOutcome(
            EjectStatus.RELEASED,
            f"released {', '.join(before)} ({note})",
            before,
        )

    async def _resident(self) -> tuple[str, ...]:
        try:
            response = await self._client.get(self.models_url)
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise _ProbeError(
                f"{self.models_url} answered {error.response.status_code}", reachable=True
            ) from error
        except httpx.HTTPError as error:
            raise _ProbeError(
                f"no language-model host answered at {self.models_url}: {error}", reachable=False
            ) from error
        try:
            payload = response.json()
        except ValueError as error:
            raise _ProbeError(
                f"{self.models_url} did not answer with JSON", reachable=True
            ) from error
        return resident_models(payload)

    @staticmethod
    def _record(outcome: EjectOutcome) -> None:
        """Log the outcome exactly once, or not at all when it is an ordinary state."""
        if outcome.status in SILENT_STATUSES:
            return
        if outcome.status is EjectStatus.RELEASED:
            logger.info("VRAM eject before render: %s", outcome.detail)
            return
        logger.warning(
            "VRAM eject before render failed (%s): %s. Submitting the render anyway.",
            outcome.status.value,
            outcome.detail,
        )
