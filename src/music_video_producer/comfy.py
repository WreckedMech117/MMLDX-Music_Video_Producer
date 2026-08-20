from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

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
        return HistoryResult(
            prompt_id=prompt_id,
            status=status,
            outputs=outputs,
            error=error,
            raw=entry,
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
