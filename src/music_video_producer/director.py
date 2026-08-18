from __future__ import annotations

import json
from base64 import b64encode
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from .assistant_prompt import ASSISTANT_SYSTEM_PROMPT, FILL_SHOTS_DESCRIPTION
from .models import AssetRole, ShotMode, SingingState


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


#: The one tool Assistant ProducerBot has. One rather than several, and the count is a decision:
#:
#: every extra tool is another shape a local model can get wrong, and the four things this replaces
#: — declare a mode, write a prompt, cite assets in roles, record the performance — are the four
#: halves of *one* answer to "what is this shot". Split across four tools, a model that chose
#: `first_middle_last` and then failed to make the second call leaves a shot declared as something
#: its citations cannot satisfy; together, the whole specification is one call that is applied or
#: refused as a unit. Nothing else the assistant might plausibly be given is allowed: approving a
#: take, marking a shot ready, deleting a shot, writing a Song and anything that spends GPU time are
#: all outside this feature, so the honest surface really is one tool.
FILL_SHOTS_TOOL = "fill_shots"


class ShotCitationFill(BaseModel):
    """One library Asset the model wants a Shot to cite, in one role.

    Mirrors `models.AssetCitation` field for field, and deliberately does not *reuse* it: this is
    the wire contract for model output and that one is the manifest's. They agree today, and the
    day they stop agreeing is the day a field the model may not set — added to `AssetCitation` for
    the manifest's sake — would otherwise become settable by a tool call, silently.
    """

    asset_id: str = Field(
        min_length=1,
        description="The id of an asset in the library, copied verbatim from plan.assets.",
    )
    role: AssetRole = Field(
        default="reference",
        description="What this asset is for in this shot. See plan.asset_roles.",
    )
    order: int = Field(
        default=0, description="Position among this shot's citations in the same role."
    )


class ShotFill(BaseModel):
    """Everything the model wants to change about one Shot, addressed to it **by id**.

    Every field but `shot_id` is `None`-defaulted, and `None` means *leave it alone* rather than
    *clear it*. That is what makes a partial answer safe: a model that sets only a mode must not
    thereby blank the prompt a Director wrote by hand.

    The types are the whole point of this model existing. `mode`, `role` and `singing` are the
    `Literal`s from `models.py`, so a mode the taxonomy does not have is a `ValidationError` at the
    edge — reported to the Director as a refused tool call, with the raw arguments kept beside it —
    rather than a plausible-looking string that reaches the manifest and is discovered at render.
    """

    shot_id: str = Field(
        min_length=1,
        description="The id of one of the shots in plan.shots, copied verbatim. Any other id is discarded.",
    )
    mode: ShotMode | None = Field(
        default=None, description="What kind of shot this is. Omit to leave the mode as it is."
    )
    prompt: str | None = Field(
        default=None,
        description="One paragraph of plain prose describing the shot. Omit to leave the prompt as it is.",
    )
    # Nothing infers this. The model may *set* it, which is a visible act reported in the reply;
    # `None` is the normal answer and leaves whatever the Shot already says. See `models.SingingState`.
    singing: SingingState | None = Field(
        default=None,
        description=(
            "Whether the performer sings in this shot. Set it only when the request or the shot's "
            "own material says so; omit it otherwise, which is the normal case."
        ),
    )
    # Replaces the Shot's whole citation list when present, because a role change and a removal are
    # both expressible only as "here is the new list" — and absent when the model is not touching
    # the citations at all, which is why this is `None` rather than `[]`.
    citations: list[ShotCitationFill] | None = Field(
        default=None,
        description=(
            "The complete list of library assets this shot should cite, replacing whatever it "
            "cites now. Omit the key to leave its citations alone."
        ),
    )


class FillShotsArguments(BaseModel):
    """The `fill_shots` argument object, and the source of its JSON schema.

    A list rather than one Shot per call: the bulk fill is the case this feature exists for, and a
    model that has to emit thirty separate tool calls to fill thirty shots will emit some other
    number. One call, judged per entry.
    """

    shots: list[ShotFill] = Field(description="One entry per shot to fill in.")


def _model_facing_schema(model: type[BaseModel]) -> dict[str, Any]:
    """`model`'s JSON schema with the class docstrings taken back out.

    Pydantic renders a model's docstring as the schema object's `description`, which is right for
    a document nobody reads and wrong for this one: the docstrings above are arguments addressed to
    the next person editing this file — they cite `AssetCitation`, name a failure mode, and run to
    a paragraph each — and every character of them would be sent to a local model on every turn as
    though it were instruction. Only the *class* descriptions are dropped, so the per-field
    `Field(description=…)` sentences, which are written for the model and for nobody else, survive.
    """
    schema = model.model_json_schema()
    schema.pop("description", None)
    for definition in schema.get("$defs", {}).values():
        if isinstance(definition, dict):
            definition.pop("description", None)
    return schema


def assistant_tools() -> list[dict[str, Any]]:
    """The tool surface as it goes on the wire, generated from the taxonomy rather than written.

    `FillShotsArguments`' schema carries the `Literal` enums as `enum` lists, so adding a mode to
    `SHOT_MODE_SPECS` or a role to `AssetRole` changes what the model is allowed to say without
    anyone editing a schema by hand. A hand-written copy here would be the second definition of the
    taxonomy, and the one that goes stale in the direction that lets a malformed call through.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": FILL_SHOTS_TOOL,
                "description": FILL_SHOTS_DESCRIPTION,
                "parameters": _model_facing_schema(FillShotsArguments),
            },
        }
    ]


class AssistantTurn(BaseModel):
    """One assistant answer: what it said, what it wants applied, and what it got wrong.

    `malformed` is the reason this is not simply `list[ShotFill]`. A tool call that does not fit the
    vocabulary is the *expected* local-model failure, not an exception: discarding the whole turn
    for one bad entry would throw away every good one beside it, and raising would leave the
    Director with a 502 and no idea which shot the model fumbled. Each entry is the raw arguments
    of one rejected call or one rejected shot, kept so the route can put it in a notice's `raw` —
    the field `DIRECTOR_CONTEXT_EXCLUDE` strips, so it is inspectable without being fed back.
    """

    message: str = ""
    fills: list[ShotFill] = Field(default_factory=list)
    malformed: list[str] = Field(default_factory=list)


def _raw_argument(value: object) -> str:
    """One rejected tool call or shot entry, as a string a notice can carry."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def parse_assistant_reply(reply: dict[str, Any]) -> AssistantTurn:
    """Split one provider reply into applied-able fills and refused calls. Pure and I/O-free.

    Every branch here is a shape a local model actually produces, and none of them may raise:
    `tool_calls` absent (the model chatted instead), a tool name that is not ours, `arguments` as a
    JSON string or as an already-decoded object (providers differ), an argument object with no
    `shots` key, and a single entry inside a good list that names a mode the taxonomy has never
    had. The last one is the case the whole typed surface exists for, and it is why entries are
    validated **one at a time**: validating the list as a whole would let one bad mode discard
    twenty-nine good shots.
    """
    content = reply.get("content")
    turn = AssistantTurn(message=content.strip() if isinstance(content, str) else "")
    calls = reply.get("tool_calls")
    for call in calls if isinstance(calls, list) else []:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict) or function.get("name") != FILL_SHOTS_TOOL:
            turn.malformed.append(_raw_argument(call))
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                turn.malformed.append(_raw_argument(function.get("arguments")))
                continue
        entries = arguments.get("shots") if isinstance(arguments, dict) else None
        if not isinstance(entries, list):
            turn.malformed.append(_raw_argument(arguments))
            continue
        for entry in entries:
            try:
                turn.fills.append(ShotFill.model_validate(entry))
            except ValidationError:
                turn.malformed.append(_raw_argument(entry))
    return turn


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

    @staticmethod
    def _reply(response: httpx.Response) -> dict[str, Any]:
        """The whole assistant message of one completion, or raise for anything that is not one.

        `_content` cannot serve here and the difference is the point: it raises whenever `content`
        is not a string, and a reply carrying tool calls has `content: null` by construction. This
        returns the message object so the caller can read `tool_calls` and `content` together, and
        it type-checks the object for `_content`'s reason — a provider answering with a bare array
        or a scalar must be a 502 naming the reply, not an `AttributeError` inside a route.
        """
        response.raise_for_status()
        payload = response.json()
        message = payload["choices"][0]["message"]
        if not isinstance(message, dict):
            raise TypeError("the reply carried no message object")
        return message

    async def assist(
        self, *, message: str, assistant_input: dict[str, Any]
    ) -> AssistantTurn:
        """One Assistant ProducerBot turn: the Director's request, the plan, and the tool surface.

        Deliberately **one round trip**. The usual agentic shape — call the tool, hand the results
        back, let the model summarise — was rejected here for two reasons that both bite locally:
        it doubles the wall-clock time of a call a Director is watching, and the second turn's
        context would be the tool results, which is precisely the "rich context" recorded as the
        root cause of Director degradation. The per-shot report the Director reads is built by the
        route from what it actually applied, which is a better summary than the model could write
        and cannot claim anything that did not happen.

        `tool_choice` is `"auto"` rather than forced. Forcing the call would guarantee one, and
        would also force one on a request the model cannot answer; the no-tool case is reported as
        a notice instead, which is the honest version. This is a one-word change if a live run
        shows the model chatting rather than calling — exactly the kind of thing the system prompt
        is meant to be iterated against.

        `assistant_input` is passed through inside `plan` verbatim: it is built by
        `timeline.assistant_input`, which is pure and trimmed on purpose, so a test asserting the
        route sent the builder's output is asserting about the payload the model really saw.
        """
        if not self.base_url or not self.model:
            raise DirectorUnavailable(
                "LLM director is not configured. Set MVP_LLM_BASE_URL and MVP_LLM_MODEL."
            )
        headers = self._headers()
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": ASSISTANT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"request": message, "plan": assistant_input}, ensure_ascii=False
                    ),
                },
            ],
            "temperature": 0.7,
            "tools": assistant_tools(),
            "tool_choice": "auto",
        }
        try:
            response = await self._completion(body=body, headers=headers)
            return parse_assistant_reply(self._reply(response))
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
