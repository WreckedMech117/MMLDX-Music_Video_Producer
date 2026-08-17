from __future__ import annotations

import json
from base64 import b64encode
from typing import Any

import httpx
from pydantic import BaseModel, Field


class DirectorUnavailable(RuntimeError):
    pass


class DirectorError(RuntimeError):
    pass


class PlannedShot(BaseModel):
    start: float = Field(ge=0)
    duration: float = Field(gt=0, le=30)
    prompt: str = Field(min_length=1)


class DirectorResult(BaseModel):
    message: str
    treatment: str
    style_bible: str
    shots: list[PlannedShot] = Field(default_factory=list)


class VisionInspection(BaseModel):
    summary: str
    identity: list[str] = Field(default_factory=list)
    environment: list[str] = Field(default_factory=list)
    continuity_cues: list[str] = Field(default_factory=list)
    prompt_cues: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


SYSTEM_PROMPT = """You are the creative director inside a local music-video production editor.
Turn conversational direction into an editable, coherent treatment and shot plan.
Return exactly one JSON object with keys: message, treatment, style_bible, shots.
Each shot has start (seconds), duration (seconds), and prompt. Prefer 4–15 second shots for
MiniMax H3. Respect the supplied song duration and existing decisions. Do not claim anything
was rendered. Keep identities, wardrobe, setting, color, camera, and motion continuity explicit.
The message should briefly explain the creative changes you made."""


class DirectorClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: float = 90,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    async def plan(self, *, message: str, project_context: dict[str, Any]) -> DirectorResult:
        if not self.base_url or not self.model:
            raise DirectorUnavailable(
                "LLM director is not configured. Set MVP_LLM_BASE_URL and MVP_LLM_MODEL."
            )
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"request": message, "project": project_context}, ensure_ascii=False
                    ),
                },
            ],
            "temperature": 0.7,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "director_result",
                    "strict": True,
                    "schema": DirectorResult.model_json_schema(),
                },
            },
        }
        try:
            response = await self._client.post(
                f"{self.base_url}/chat/completions", headers=headers, json=body
            )
            if response.status_code == 400 and (
                "Failed to load model" in response.text or "Model is unloaded" in response.text
            ):
                models = await self._client.get(f"{self.base_url}/models", headers=headers)
                models.raise_for_status()
                loaded = next(
                    (
                        item["id"]
                        for item in models.json().get("data", [])
                        if str(item.get("id", "")).startswith(f"{self.model}:")
                    ),
                    "",
                )
                if loaded:
                    body["model"] = loaded
                    response = await self._client.post(
                        f"{self.base_url}/chat/completions", headers=headers, json=body
                    )
            response.raise_for_status()
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            return DirectorResult.model_validate(json.loads(content))
        except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError, ValueError) as error:
            raise DirectorError(f"LLM director returned an invalid response: {error}") from error

    async def inspect_image(
        self, *, image: bytes, mime_type: str, purpose: str
    ) -> VisionInspection:
        if not self.base_url or not self.model:
            raise DirectorUnavailable(
                "Vision inspection is not configured. Set MVP_LLM_BASE_URL and MVP_LLM_MODEL."
            )
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Inspect production reference images conservatively. Record only visible "
                        "identity, wardrobe, environment, lighting, composition, continuity cues, "
                        "prompt cues, and risks. Do not identify real people or infer sensitive traits."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"Inspect this {purpose} for music-video consistency.",
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{b64encode(image).decode('ascii')}"
                            },
                        },
                    ],
                },
            ],
            "temperature": 0.2,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "vision_inspection",
                    "strict": True,
                    "schema": VisionInspection.model_json_schema(),
                },
            },
        }
        try:
            response = await self._client.post(
                f"{self.base_url}/chat/completions", headers=headers, json=body
            )
            if response.status_code == 400 and (
                "Failed to load model" in response.text or "Model is unloaded" in response.text
            ):
                models = await self._client.get(f"{self.base_url}/models", headers=headers)
                models.raise_for_status()
                loaded = next(
                    (
                        item["id"]
                        for item in models.json().get("data", [])
                        if str(item.get("id", "")).startswith(f"{self.model}:")
                    ),
                    "",
                )
                if loaded:
                    body["model"] = loaded
                    response = await self._client.post(
                        f"{self.base_url}/chat/completions", headers=headers, json=body
                    )
            response.raise_for_status()
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            return VisionInspection.model_validate(json.loads(content))
        except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError, ValueError) as error:
            raise DirectorError(f"Vision inspector returned an invalid response: {error}") from error
