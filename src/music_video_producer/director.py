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


class ExpandedShot(BaseModel):
    """One written prompt, addressed to a Shot **by id**.

    `PlannedShot` carries no id, which is why the chat route merges its shots positionally.
    Expansion cannot: it writes free text, so a merge that lands on the wrong Shot after a
    concurrent add, delete or split fails silently and forever. The id is the whole point of
    this model existing separately.
    """

    shot_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)


class ShotExpansion(BaseModel):
    """The result of one whole-plan expansion call.

    `shots` has no default on purpose. It is the entire payload, so a reply that omits the
    key is a failed call and must raise here rather than arrive as an empty list that the
    route would then report as "every Shot was omitted".
    """

    message: str
    shots: list[ExpandedShot]


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


# One whole-plan pass, because per-Shot calls cannot see each other and cross-Shot variance is
# the point: a plan of twelve shots that each independently "hold the identity" reads as twelve
# takes of one shot. The constants to hold fixed and the axes to vary are named explicitly
# rather than left to the model, and the id contract is stated twice — in the schema and here —
# because a prompt keyed to the wrong Shot is free text that fails silently.
EXPANSION_SYSTEM_PROMPT = """You are the creative director inside a local music-video production editor.
You are given the whole plan at once and must write one render-ready prompt per shot.

The input is a JSON object with these keys:
- creative_brief, treatment, style_bible: the creative work every prompt must embed.
- song: the master song, when one exists. Always its title and its duration, the length in
  seconds. It also carries lyrics, the song's words exactly as written, and caption, a
  description of how the song sounds — each present only when the song has one, so treat an
  absent key as unknown rather than as empty. The words and the sound say what the video is
  about; draw imagery, subject and mood from them. Neither carries timing: a section tag
  inside the sheet is structure, not a time, and the shot windows below are the only timing.
- h3_shot_window: the shot length in seconds the renderer is most reliable within.
- shots: every shot in the plan, ordered by when it happens in the song.

Each entry in shots has:
- shot_id: the shot's identity. Copy it verbatim into your answer; it is how your prompt is
  matched to a shot. A wrong or invented id is discarded.
- index: the shot's position in that song order, counting from 0.
- start, end, duration: absolute seconds against the song.
- song_fraction: how far through the song the shot starts, from 0 at the opening to 1 at the
  end. Absent when the song's length is unknown. Use it to place the shot on the song's energy
  curve: early shots establish, middle shots develop, late shots resolve or release.
- neighbours: the shot ids and windows immediately before and after this one, so you can make
  each prompt deliberately different from what it cuts from and into. Their full entries are in
  shots, at index - 1 and index + 1.
- current_prompt: what the shot says today. Often empty or a placeholder like "New shot"; when
  it holds real direction, keep its intent and write it properly rather than replacing it.
- locked: true when the shot must not be rewritten. Return no entry for a locked shot.
- outside_h3_window: true when the shot is shorter or longer than h3_shot_window. Write for the
  length the shot actually is; do not ask for a different one.

Return exactly one JSON object with keys: message, shots. Each entry in shots has shot_id and
prompt, and prompt is plain prose — never JSON, never a list of fields.
Hold these continuity constants fixed in every prompt: identity, wardrobe, palette, lens.
Vary these axes across the plan so the shots do not read as takes of one shot: action, framing,
energy. Two adjacent shots must not share a framing.
Do not retime anything, do not invent shots, and do not claim anything was rendered.
The message should briefly explain the through-line you wrote across the plan."""


#: A replacement shorter than this fraction of an existing document is treated as degraded.
MIN_REPLACEMENT_RATIO = 0.4


def document_rejection(candidate: str, existing: str) -> str:
    """Return why ``candidate`` must not replace ``existing``, or "" when it may.

    Local models occasionally emit a serialised JSON structure into a string field, or
    collapse a document to a fragment. Either silently destroys creative work, so both
    are rejected rather than persisted. An empty target accepts any first draft.
    """
    stripped = candidate.strip()
    if stripped.startswith(("{", "[")):
        try:
            json.loads(stripped)
        except ValueError:
            pass
        else:
            return "the model returned JSON instead of prose"
    if not existing.strip():
        return ""
    if len(stripped) < MIN_REPLACEMENT_RATIO * len(existing.strip()):
        return (
            f"the replacement is {len(stripped)} characters against "
            f"{len(existing.strip())} existing, below the "
            f"{int(MIN_REPLACEMENT_RATIO * 100)}% floor"
        )
    return ""


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
        self._in_flight = 0

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def busy(self) -> bool:
        """True while any request to the language-model host is outstanding.

        The VRAM eject before a render consults this and steps aside when it is true.
        Releasing the model out from under a call the Director is waiting on trades a
        conversation for a render, which is the wrong trade — and the model would only be
        reloaded moments later anyway, having achieved nothing.

        A counter, not a flag: a Director can have an expansion and a vision inspection in
        the air at once, and a flag would report the host idle the moment the first of them
        returned while the second was still holding VRAM.
        """
        return self._in_flight > 0

    async def _completion(
        self, *, body: dict[str, Any], headers: dict[str, str]
    ) -> httpx.Response:
        """POST one chat completion, retrying once against LM Studio's loaded instance id.

        LM Studio refuses with 400 "Model is unloaded" when the configured id is not the
        loaded one, and answers `/models` with the loaded id suffixed (`model:2`). One
        implementation for all three call sites: `plan`, `expand` and `inspect_image` had
        this block copied verbatim, so a fix to the retry only ever reached one of them.

        `body` is copied rather than mutated for the retry, so a caller's request body is not
        silently rewritten to an instance id that will not exist on the next call.

        The whole method is counted as in-flight, retry included, because `busy` exists to
        keep the VRAM eject away from a live call — and the retry is when the call is at its
        most fragile. `finally` rather than a decrement on the happy path: an exception that
        left the counter raised would wedge the eject off permanently for the life of the
        process.
        """
        self._in_flight += 1
        try:
            response = await self._client.post(
                f"{self.base_url}/chat/completions", headers=headers, json=body
            )
            if response.status_code == 400 and (
                "Failed to load model" in response.text or "Model is unloaded" in response.text
            ):
                models = await self._client.get(f"{self.base_url}/models", headers=headers)
                models.raise_for_status()
                # Shape-checked rather than assumed. `/models` is whatever the configured
                # provider answers with, and a bare JSON array or scalar makes `.get` raise
                # AttributeError — which is outside every caller's caught tuple, so the
                # recovery path for one provider quirk would crash as a 500 on another. No
                # usable id simply means no retry: the original 400 is returned and reported
                # as the provider error it is.
                listing = models.json()
                entries = listing.get("data", []) if isinstance(listing, dict) else []
                loaded = next(
                    (
                        item["id"]
                        for item in entries
                        if isinstance(item, dict)
                        and str(item.get("id", "")).startswith(f"{self.model}:")
                    ),
                    "",
                )
                if loaded:
                    response = await self._client.post(
                        f"{self.base_url}/chat/completions",
                        headers=headers,
                        json={**body, "model": loaded},
                    )
            return response
        finally:
            self._in_flight -= 1

    @staticmethod
    def _content(response: httpx.Response) -> str:
        """The assistant text of one completion, or raise for anything that is not text.

        `content` is `null` on a refusal, on a length-truncated reply, and whenever a provider
        answers with tool calls instead of text. `json.loads(None)` raises `TypeError`, which no
        caller used to catch — so the one shape a local model produces most often on a bad day
        was the only one that reached the Director as a 500 rather than the 502 every other
        malformed reply gets. Each caller now catches `TypeError` as well, which covers both this
        explicit raise and any other non-string that reaches `json.loads`.
        """
        response.raise_for_status()
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError("the reply carried no message content")
        return content

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def plan(self, *, message: str, project_context: dict[str, Any]) -> DirectorResult:
        if not self.base_url or not self.model:
            raise DirectorUnavailable(
                "LLM director is not configured. Set MVP_LLM_BASE_URL and MVP_LLM_MODEL."
            )
        headers = self._headers()
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
            response = await self._completion(body=body, headers=headers)
            return DirectorResult.model_validate(json.loads(self._content(response)))
        except (
            httpx.HTTPError,
            KeyError,
            IndexError,
            TypeError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            raise DirectorError(f"LLM director returned an invalid response: {error}") from error

    async def expand(self, *, expansion_input: dict[str, Any]) -> ShotExpansion:
        """Write one prompt per Shot, in a single whole-plan call.

        `expansion_input` is passed through verbatim: it is built by `timeline.expansion_input`,
        which is pure, trimmed on purpose, and the thing tests assert on. Nothing is added to
        it here, or the assertion that the route sent the builder's output would be true of a
        payload the model never saw.
        """
        if not self.base_url or not self.model:
            raise DirectorUnavailable(
                "LLM director is not configured. Set MVP_LLM_BASE_URL and MVP_LLM_MODEL."
            )
        headers = self._headers()
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": EXPANSION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(expansion_input, ensure_ascii=False),
                },
            ],
            "temperature": 0.7,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "shot_expansion",
                    "strict": True,
                    "schema": ShotExpansion.model_json_schema(),
                },
            },
        }
        try:
            response = await self._completion(body=body, headers=headers)
            return ShotExpansion.model_validate(json.loads(self._content(response)))
        except (
            httpx.HTTPError,
            KeyError,
            IndexError,
            TypeError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            raise DirectorError(f"LLM director returned an invalid response: {error}") from error

    async def inspect_image(
        self, *, image: bytes, mime_type: str, purpose: str
    ) -> VisionInspection:
        if not self.base_url or not self.model:
            raise DirectorUnavailable(
                "Vision inspection is not configured. Set MVP_LLM_BASE_URL and MVP_LLM_MODEL."
            )
        headers = self._headers()
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
            response = await self._completion(body=body, headers=headers)
            return VisionInspection.model_validate(json.loads(self._content(response)))
        except (
            httpx.HTTPError,
            KeyError,
            IndexError,
            TypeError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            raise DirectorError(f"Vision inspector returned an invalid response: {error}") from error
