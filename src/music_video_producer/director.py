from __future__ import annotations

import json
from base64 import b64encode
from collections.abc import Sequence
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from .assistant_prompt import (
    ASSISTANT_SYSTEM_PROMPT,
    EXPAND_PROMPTS_DESCRIPTION,
    FILL_SHOTS_DESCRIPTION,
)
from .models import AssetRole, ShotMode, SingingState

#: Default completion budget for one H3 expansion. Large because it has to cover a
#: reasoning phase as well as the answer: a model on the Director's machine spent 899
#: of 900 tokens thinking and returned nothing, and all 6000 of a 6000-token budget the
#: same way. It answered cleanly at 4000 with thinking disabled.
H3_EXPANSION_MAX_TOKENS = 4000

class DirectorUnavailable(RuntimeError):
    pass


class DirectorError(RuntimeError):
    pass


class DirectorBudgetExhausted(DirectorError):
    """The model spent its whole completion budget reasoning and returned no answer.

    A subclass rather than a message-match because a *caller* has to tell this apart from
    every other `DirectorError`: it is the one provider failure that is worth retrying —
    measured at roughly 1 call in 6 on this project's machine, and independent across calls
    because sampling is — where a transport error or a rejected request body will fail the
    same way on every attempt. Matching on the message text would make that decision
    hostage to a wording chosen for the Director to read.
    """


#: The corrective follow-up on a retry after the checker refused an answer. Sent as a user
#: turn *after* the failed answer itself is replayed as an assistant turn, so the model is
#: correcting a concrete text it can see rather than being asked to reroll from nothing.
#: The problems are the checker's own sentences, verbatim: they name the field and the rule,
#: which is exactly the target a rewrite needs.
H3_RETRY_PROMPT = (
    "That answer was rejected by the format checker and was not saved. What is wrong with "
    "it, in the checker's own words:\n{problems}\n"
    "Rewrite the prompt so every one of those problems is fixed. Keep everything that was "
    "already right. Return only the corrected prompt, nothing else."
)


class PlannedShot(BaseModel):
    start: float = Field(ge=0)
    duration: float = Field(gt=0, le=30)
    prompt: str = Field(min_length=1)
    # Whether a character sings the song on camera in this shot. Safe to ask of the model
    # here — unlike the assistant's tool calls, this rides `response_format: json_schema
    # strict`, where the constrained decoder *forces* every key to be emitted, so the
    # boolean-dropout disease the fill workaround exists for cannot occur on this path
    # (the run-2 audit's observation). Populate maps it onto `singing`/`use_song_audio`.
    performance: bool = False


class PlannedSection(BaseModel):
    """A song-structure window the model proposes: Intro/Verse/Chorus/Bridge/Outro.

    Wider duration bound than a shot's — a chorus can legitimately run a minute — and
    the route repairs whatever arrives (sorting, clamping, truncating overlaps) rather
    than refusing, because a section proposal is scaffolding, not a render."""

    label: str = Field(min_length=1, max_length=60)
    start: float = Field(ge=0)
    duration: float = Field(gt=0)
    prompt: str = ""


class DirectorResult(BaseModel):
    message: str
    treatment: str
    style_bible: str
    shots: list[PlannedShot] = Field(default_factory=list)
    # Populated only when the caller asks for structure (Populate Timeline); the chat
    # route ignores it, and the default keeps every existing chat reply validating.
    sections: list[PlannedSection] = Field(default_factory=list)


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


#: Assistant ProducerBot's first tool, and for a long time its only one. One rather than several,
#: and the count was a decision:
#:
#: every extra tool is another shape a local model can get wrong, and the four things this replaces
#: — declare a mode, write a prompt, cite assets in roles, record the performance — are the four
#: halves of *one* answer to "what is this shot". Split across four tools, a model that chose
#: `first_middle_last` and then failed to make the second call leaves a shot declared as something
#: its citations cannot satisfy; together, the whole specification is one call that is applied or
#: refused as a unit. Nothing else the assistant might plausibly be given is allowed: approving a
#: take, marking a shot ready, deleting a shot, writing a Song and anything that spends GPU time are
#: all outside this feature.
FILL_SHOTS_TOOL = "fill_shots"

#: The second tool, and the reason the count above went from one to two rather than staying at one.
#:
#: It is not a fifth half of "what is this shot". Filling a shot in *writes the intent*; expanding
#: it *transforms an intent that already exists* into H3's structured format, through a different
#: system prompt, a different payload and one model call per shot. Folding it into `fill_shots`
#: would have made a single tool call mean "write this, and also spend N further calls on it",
#: which is exactly the kind of hidden second act a Director cannot decline.
#:
#: ProducerBot is the surface and the specialist is in its box: this tool is how a conversational
#: request reaches the specialist at all. It routes through the same `shot_write_refusal` and the
#: same prompt gate a Director's own click meets — a tool that cannot be refused is a guard hole —
#: and, exactly like `fill_shots`, it may only name shots the turn's selection already contains.
EXPAND_PROMPTS_TOOL = "expand_prompts"


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


class ShotExpansionRequest(BaseModel):
    """One Shot the model wants expanded into H3's structured format, addressed **by id**.

    Deliberately carries nothing else. `ShotFill` is typed to the shot vocabulary because a mode,
    a role and a performance state are all things a model can plausibly get wrong in words; an
    expansion has no such vocabulary to get wrong — everything it needs is already on the Shot and
    its project, which is why the route this reaches sends no body either. The *only* thing the
    model contributes is which shot, so the only thing to validate is that it named one.

    A separate model from `ShotFill` rather than a reuse of it, for `ShotCitationFill`'s reason:
    they agree on `shot_id` today, and the day `ShotFill` gains a field is the day reusing it would
    make that field settable through a tool that has no business setting anything.
    """

    shot_id: str = Field(
        min_length=1,
        description="The id of one of the shots in plan.shots, copied verbatim. Any other id is discarded.",
    )


class ExpandPromptsArguments(BaseModel):
    """The `expand_prompts` argument object, and the source of its JSON schema.

    A list, on `FillShotsArguments`' argument: a model that has to emit thirty separate tool calls
    to expand thirty shots will emit some other number. One call naming many shots — and then, on
    the server, **one model call per shot**, which is the whole point of pass two and is not
    something this schema can express or the model needs to know.
    """

    shots: list[ShotExpansionRequest] = Field(description="One entry per shot to expand.")


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
        },
        {
            "type": "function",
            "function": {
                "name": EXPAND_PROMPTS_TOOL,
                "description": EXPAND_PROMPTS_DESCRIPTION,
                "parameters": _model_facing_schema(ExpandPromptsArguments),
            },
        },
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
    #: The shots the model asked to have expanded into H3's format, in the order it named them.
    #: Kept apart from `fills` rather than merged into it, because the two are applied by different
    #: machinery at different costs: a fill is a field assignment, an expansion is a model call.
    expansions: list[ShotExpansionRequest] = Field(default_factory=list)
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

    Both tools are parsed by the same loop, keyed off the name the model called. The two argument
    objects share the `shots` key on purpose — one shape for the model to learn — and differ only
    in the model each entry is validated against, which is what decides whether a bad entry lands
    in `fills`, in `expansions`, or in `malformed`.
    """
    accepted: dict[str, type[BaseModel]] = {
        FILL_SHOTS_TOOL: ShotFill,
        EXPAND_PROMPTS_TOOL: ShotExpansionRequest,
    }
    content = reply.get("content")
    turn = AssistantTurn(message=content.strip() if isinstance(content, str) else "")
    calls = reply.get("tool_calls")
    for call in calls if isinstance(calls, list) else []:
        function = call.get("function") if isinstance(call, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if not isinstance(function, dict) or name not in accepted:
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
        model = accepted[name]
        for entry in entries:
            try:
                validated = model.model_validate(entry)
            except ValidationError:
                turn.malformed.append(_raw_argument(entry))
                continue
            if isinstance(validated, ShotFill):
                turn.fills.append(validated)
            else:
                turn.expansions.append(validated)
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


# The Stage Manager (the Director's user workflow, stage 3): one pass over what the project
# holds, proposing the supporting assets the video still needs. A job description like the
# expansion specialist's — meant to be edited against real output. The example block is a
# prescription, not decoration: this model family follows examples and ignores abstract
# instructions (measured three-for-three on prompt rules, and again on tool booleans).
STAGE_MANAGER_PROMPT = """You are the stage manager inside a local music-video production editor.
You are handed the whole project: creative brief, treatment, style bible, the song's words,
the asset library as it stands, and the shot plan. Your one job: propose the supporting
image assets the video still needs — alternate character looks, extra camera-relevant
angles, secondary locations, props the treatment implies — that the library does not hold.

Return exactly one JSON object with keys: message, assets.
- message: one or two sentences on what the library is missing and why these fill it.
- assets: a list of proposals. Each has:
  - name: a short library name, distinct from every existing asset's name.
  - kind: one of "character", "setting", "prop", "style".
  - prompt: a complete text-to-image prompt for a single still image. Write it like this
    example, concrete and self-contained: "A tall female rock singer with wild curly blonde
    hair, black leather corset and bright red leather boots, standing full-body in a dark
    moonlit warehouse, cool slate-blue light, warm amber rim light on her face, 35mm film
    grain, photorealistic." Name colors, wardrobe, lighting and framing explicitly; never
    reference other images, assets, or shots — the image model sees only this text.

Propose only what the treatment actually needs. Never duplicate an existing asset's
subject; propose the missing angle, outfit, corner or prop instead. Fewer, better
proposals beat filler."""


class AssetProposal(BaseModel):
    # A `Literal`, deliberately: the strict json_schema reaches LM Studio's constrained
    # decoder, so an enum here *guides sampling* toward valid kinds rather than merely
    # refusing invalid ones after the fact.
    kind: Literal["character", "setting", "prop", "style"]
    name: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1)


class StageManagerResult(BaseModel):
    message: str
    assets: list[AssetProposal] = Field(default_factory=list)


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
- section: when the director has marked the song's structure, this shot's section — its label
  (verse, chorus, bridge...) and the section's shared visual prompt. Honor the shared prompt in
  every choice for this shot; it is the look the whole section carries. Absent when unmarked.
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
        # 300 rather than 90 since 2026-08-19: the loaded model began reasoning
        # unconditionally (`enable_thinking: false` stopped taking effect after an LM
        # Studio-side change), and a full H3 expansion's reasoning phase alone can run
        # past 90 s. The reasoning now *terminates* — measured the same day — so waiting
        # is correct where truncating the budget is not.
        timeout: float = 300,
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

    async def stage_manager(
        self, *, project_context: dict[str, Any], count: int
    ) -> StageManagerResult:
        """One Stage Manager pass: what supporting assets does this video still need.

        `plan`'s transport, `plan`'s error translation, a different job description and a
        different strict schema. `count` rides in the request text rather than the schema
        because it is guidance ("up to N"), not a shape — the route truncates the answer
        to it either way, so an over-eager model costs nothing.
        """
        if not self.base_url or not self.model:
            raise DirectorUnavailable(
                "LLM director is not configured. Set MVP_LLM_BASE_URL and MVP_LLM_MODEL."
            )
        headers = self._headers()
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": STAGE_MANAGER_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "request": (
                                f"Propose up to {count} supporting image assets this "
                                "video still needs."
                            ),
                            "project": project_context,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": 0.7,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "stage_manager_result",
                    "strict": True,
                    "schema": StageManagerResult.model_json_schema(),
                },
            },
        }
        try:
            response = await self._completion(body=body, headers=headers)
            return StageManagerResult.model_validate(json.loads(self._content(response)))
        except (
            httpx.HTTPError,
            KeyError,
            IndexError,
            TypeError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            raise DirectorError(f"LLM director returned an invalid response: {error}") from error

    async def expand(
        self, *, expansion_input: dict[str, Any], system_prompt: str | None = None
    ) -> ShotExpansion:
        """Write one prompt per Shot, in a single whole-plan call.

        `expansion_input` is passed through verbatim: it is built by `timeline.expansion_input`,
        which is pure, trimmed on purpose, and the thing tests assert on. Nothing is added to
        it here, or the assertion that the route sent the builder's output would be true of a
        payload the model never saw.

        `system_prompt` selects the persona over the same transport and the same
        `ShotExpansion` contract: `None` is the story pass (pass one), and the DP pass
        (`dp_prompt.DP_SYSTEM_PROMPT`) rides the identical wire because "revised intents
        addressed by shot id" is exactly its output shape too.
        """
        if not self.base_url or not self.model:
            raise DirectorUnavailable(
                "LLM director is not configured. Set MVP_LLM_BASE_URL and MVP_LLM_MODEL."
            )
        headers = self._headers()
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt or EXPANSION_SYSTEM_PROMPT},
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

    async def expand_shot(
        self,
        *,
        shot_input: dict[str, Any],
        system_prompt: str,
        max_tokens: int = H3_EXPANSION_MAX_TOKENS,
        rejected: str = "",
        rejected_problems: Sequence[str] = (),
    ) -> str:
        """Turn one Shot's intent into an H3-format prompt. Returns the text, unparsed.

        ``rejected`` and ``rejected_problems`` make one call a *corrective retry*: when
        ``rejected`` is non-empty, the failed answer is replayed as an assistant turn and
        `H3_RETRY_PROMPT` follows as a user turn carrying the checker's sentences, so the
        model has a concrete text and a named defect to fix rather than a fresh roll. The
        caller owns the loop and the attempt budget — this method stays one call either
        way, because the checker that decides "rejected" lives beside the caller, not here.

        Text out, not JSON. An H3 prompt is a document with its own grammar, so wrapping it
        in a JSON schema would only add an escaping layer for the model to get wrong on top
        of a format it is already being asked to get right. What comes back is checked by
        `h3_prompt.check`, at the route, where a failure can be reported to the Director
        rather than swallowed here.

        `max_tokens` is generous on purpose, and this is the one place a caller is likely to
        want it larger still. Measured against the model loaded on the Director's machine on
        2026-08-18: a reasoning model spent **899 of 900** tokens reasoning and returned an
        empty `content` with `finish_reason: "length"`, and at 6000 it spent all 6000 the same
        way. A budget that looks generous for the answer can still be nowhere near enough for
        the thinking in front of it.

        `chat_template_kwargs.enable_thinking` is an LM Studio / vLLM extension, not part of
        the OpenAI schema. It is sent because it is the only thing that worked: `/no_think` in
        the prompt did not suppress reasoning at all on that model, and this did. A provider
        that rejects unknown body keys will 400, which is why the error below names the key —
        the fix is to drop it, and a reader should not have to guess that.
        """
        if not self.base_url or not self.model:
            raise DirectorUnavailable(
                "LLM director is not configured. Set MVP_LLM_BASE_URL and MVP_LLM_MODEL."
            )
        headers = self._headers()
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(shot_input, ensure_ascii=False)},
        ]
        if rejected:
            messages.append({"role": "assistant", "content": rejected})
            messages.append(
                {
                    "role": "user",
                    "content": H3_RETRY_PROMPT.format(
                        problems="\n".join(f"- {problem}" for problem in rejected_problems)
                    ),
                }
            )
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.6,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        try:
            response = await self._completion(body=body, headers=headers)
            message = self._reply(response)
        except (httpx.HTTPError, KeyError, IndexError, TypeError) as error:
            # `str(error)` alone is not enough: httpx.ReadTimeout stringifies to "",
            # which once produced the message "invalid response: ." about what was
            # actually a timeout. The class name is the part that always says something.
            raise DirectorError(
                "LLM director returned an invalid response: "
                f"{type(error).__name__}: {error}. If the provider rejected the request "
                "body, note that chat_template_kwargs is an LM Studio / vLLM extension "
                "and may need removing."
            ) from error

        text = (message.get("content") or "").strip()
        if text:
            return text

        # Empty content is not one failure, and the two need different words. A model that
        # filled its budget with reasoning and never answered is a *budget* problem — the
        # prompt may be perfect — and saying "invalid response" about it sends the reader to
        # rewrite a prompt that was fine. Measured on this project's own machine, which is why
        # the number is named rather than described.
        reasoning = (message.get("reasoning_content") or "").strip()
        if reasoning:
            raise DirectorBudgetExhausted(
                f"The model spent its whole {max_tokens}-token budget reasoning and returned "
                "no prompt. Raise the budget, or disable the model's thinking phase, or use a "
                "model that does not reason before answering."
            )
        raise DirectorError("LLM director returned an empty prompt.")

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
