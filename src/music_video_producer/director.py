from __future__ import annotations

import json
import re
from base64 import b64encode
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Annotated, Any, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from .assistant_prompt import (
    ASSISTANT_SYSTEM_PROMPT,
    EXPAND_PROMPTS_DESCRIPTION,
    FILL_SHOTS_DESCRIPTION,
)
from .models import AssetRole, ShotMode, SingingState
from .planning_prompt import (
    ASK_DIRECTOR_DESCRIPTION,
    PLANNING_SYSTEM_PROMPT,
    PROPOSE_ASSETS_DESCRIPTION,
    WRITE_BRIEF_DESCRIPTION,
)

#: Default completion budget for one H3 expansion. Large because it has to cover a
#: reasoning phase as well as the answer: a model on the Director's machine spent 899
#: of 900 tokens thinking and returned nothing, and all 6000 of a 6000-token budget the
#: same way. It answered cleanly at 4000 with thinking disabled.
H3_EXPANSION_MAX_TOKENS = 4000

#: The sampling temperature every planning call has always used, now named because one
#: caller deliberately departs from it. Changing this changes the chat route too.
PLAN_TEMPERATURE = 0.7


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
    # Whether a character sings the song on camera in this shot. Populate maps it onto
    # `singing`/`use_song_audio`.
    #
    # The default is `False` and that default is **not** a statement about the shot: it is
    # what Pydantic gives a key nobody sent. The comment that used to sit here said the
    # constrained decoder "forces every key to be emitted" and was measurably wrong — a
    # field carrying a default is not in `required`, so the grammar never asks for it and
    # a decoder that omits it is correct. Measured 2026-08-20, 15 rolls / 179 shots across
    # three models: one model omitted the key on 4 of 5 rolls (every shot silently
    # non-performance), and two set it `true` on all twelve shots of a roll — an
    # all-or-nothing tell rather than a judgement. `director_result_schema` promotes it
    # into `PlannedShot.required` for every caller that demands a shot list, so the model
    # has to make the decision per shot instead of falling through this default.
    performance: bool = False
    # Which library assets this shot uses, named **here** instead of in the prose.
    #
    # The defect this closes, reported by the Director on a live plan (2026-08-21): 24 of 30
    # prompts carried an asset's display name, because a citation was only ever created when
    # the model typed that label into the shot's creative writing. The attachment mechanism
    # *was* the prose, so the prose read like inventory — "Extreme close up of Crimson Lips
    # Close-up while HarderFaster performs", "Blue Haze Atmosphere surrounding the Dusk
    # Warehouse Bed". A field the model fills structurally is somewhere to put the label that
    # is not the sentence a renderer will read.
    #
    # Entries are matched against the roster the caller offered, by **id or by exact display
    # name**, and anything matching neither is dropped rather than guessed at. Both are
    # accepted because they are one fact seen from two sides: the roster in the populate
    # instruction is written by name, and the project context dump beside it carries the ids,
    # so whichever the model copies it is copying something it was shown. An unresolvable
    # entry cannot invent an asset; it can only fail to cite one, and the prose scan
    # (`models.assets_for_proposal`) is still there to catch exactly that case.
    #
    # `list` default, promoted into the grammar by `PLANNED_SHOT_DECISIONS`, and the pairing
    # is `SectionLook.prompt`'s exactly: required means the decoder must emit the key, so the
    # model decides about every shot, while the empty list stays expressible for a shot that
    # genuinely uses nothing from the library. Leaving it defaulted-and-optional would
    # reproduce the hole this module has now found six times.
    assets: list[str] = Field(
        default_factory=list,
        description=(
            "The exact names (or ids) of the project assets this shot uses. This is the "
            "only place an asset may be named; the prompt itself must not contain these "
            "labels. Empty when the shot uses no library asset."
        ),
    )


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


#: The name the planning schema goes on the wire under. One name for every variant below,
#: because it is a label for the provider's logs and not a contract: three call sites and
#: two tests already pin `director_result`, and renaming it per variant would make the wire
#: shape of a *required set* look like a different kind of answer.
DIRECTOR_RESULT_SCHEMA_NAME = "director_result"


def _promoted(schema: dict[str, Any], require: Sequence[str]) -> list[str]:
    """``schema``'s ``required`` list with ``require`` folded in, in *property* order.

    Property order rather than call order, so a `required` list does not drift with the
    order a caller happened to name its fields in — that would be a wire payload changing
    for no reason. Already-required names are never duplicated.

    An unknown name raises rather than being ignored. A promotion that silently does
    nothing is precisely the failure this whole module is repairing: `shots` was asked for
    in words for three measured runs while the grammar never mentioned it, and a typo here
    would reproduce that shape exactly — a caller that believes it required a field, a
    decoder that was never told, and nothing anywhere that says so.
    """
    properties = schema.get("properties", {})
    unknown = sorted(name for name in require if name not in properties)
    if unknown:
        raise ValueError(
            f"{schema.get('title', 'schema')} has no field(s) {', '.join(unknown)} to require"
        )
    already = schema.get("required", [])
    return [name for name in properties if name in already or name in require]


def _entry_schema(schema: dict[str, Any], prop: str) -> dict[str, Any]:
    """The object schema of one entry of ``schema``'s ``prop`` array, resolved through `$defs`.

    Raises for anything that is not an array of objects, for `_promoted`'s reason: naming a
    scalar field here can only be a mistake, and the mistake must not present as a promotion
    that quietly did nothing.
    """
    items = schema.get("properties", {}).get(prop, {}).get("items")
    reference = items.get("$ref", "") if isinstance(items, dict) else ""
    if not reference:
        raise ValueError(f"{prop!r} is not an array of objects with their own schema")
    return schema["$defs"][reference.rsplit("/", 1)[-1]]


def constrained_schema(
    model: type[BaseModel],
    *,
    require: Sequence[str] = (),
    require_each: Mapping[str, Sequence[str]] | None = None,
    min_items: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """``model``'s JSON schema with a caller's required set promoted into the grammar.

    Every `response_format: json_schema strict` body in this module whose caller has a
    requirement goes through here — `ShotExpansion` does not, because every field on it is
    required already, which is what a model written without defaults gets for free, and the
    chat route does not, because it requires nothing and its grammar must stay the model's
    own. The reason for the rest is one root cause that has now recurred twice. A Pydantic
    field with **any** default is absent from ``model_json_schema()["required"]``; that
    schema is what reaches LM Studio's *constrained decoder*; so a field the caller cannot
    proceed without is one the model is free — and correct — to omit, no matter how firmly
    the prompt asks for it.
    First measured on `DirectorResult.shots` (empty on 2 of 3 rolls, 0 of 17 combined asks
    delivered both halves), then on `PlannedShot.performance` (a whole model omitting the
    key on 4 of 5 rolls, 15 rolls / 179 shots, 2026-08-20).

    Three axes, because that is what the measured call sites need and nothing more:

    * ``require`` promotes top-level fields.
    * ``require_each`` promotes fields on the *entries* of an array property —
      ``{"shots": ("performance",)}`` — which is where a per-item decision like
      "is this shot a performance" actually lives.
    * ``min_items`` sets ``minItems`` on an array. Measured, not assumed: the decoder
      honours it (twelve shots on 4 of 4 calls against ``minItems: 12``) and it is also a
      loaded gun, because the same probe showed numeric bounds are *not* enforced — the
      padding entries carried ``duration: 0`` and ``duration: 1200``, and 3 of those 4
      replies failed `PlannedShot` validation whole. Length is grammar; plausibility is
      not, and an unparseable reply is a 502 where a short one is a guided retry.

    What this deliberately does **not** do is make the Pydantic field required. The wire
    grammar and the parse contract are different questions: a provider that ignores strict
    schemas, or the schema-free retry `_completion` falls back to, must still produce a
    result the caller can validate. Tightening the model would turn a degraded answer into
    an exception, which is a worse failure than the one being fixed.

    The schema is deep-copied before anything is touched, so no variant can leak into the
    next caller's — the chat route inheriting populate's grammar from whichever call ran
    first is exactly the kind of bug this function must not introduce.
    """
    schema = deepcopy(model.model_json_schema())
    schema["required"] = _promoted(schema, require)
    for prop, fields in (require_each or {}).items():
        if not fields:
            continue
        entry = _entry_schema(schema, prop)
        entry["required"] = _promoted(entry, fields)
    for prop, minimum in (min_items or {}).items():
        if minimum > 0:
            schema["properties"][prop]["minItems"] = minimum
    return schema


#: The per-shot decision a caller that demands a shot list also demands *per shot*.
#:
#: Tied to `shots` being required rather than exposed as its own parameter, because the two
#: are one question: the only caller that cannot proceed without a shot list is Populate,
#: and Populate maps `performance` onto `singing` for every shot it writes. A caller that is
#: merely *offered* shots — the chat route — is offered them with `performance` optional,
#: exactly as it always was.
#:
#: `assets` joins it for the identical reason one level down from the identical hole: it is
#: the structural citation field, Populate builds every shot's citations from it, and a field
#: carrying `default_factory=list` is absent from `PlannedShot`'s `required` for free — which
#: is the exact shape measured on `DirectorResult.shots`, `PlannedShot.performance`,
#: `PlannedSection.prompt`, `stage_manager.assets` and `SectionLook.prompt`. Required here
#: does not mean "cite something": the empty list is legal and is a shot that uses nothing.
#: It means the decoder may not close a shot object without having *considered* the question.
PLANNED_SHOT_DECISIONS = ("performance", "assets")

#: The same, one level down, for a section: `prompt` is the section's shared visual look,
#: which both populate instructions ask for in words ("a one-sentence shared visual prompt")
#: and which `PlannedSection.prompt = ""` kept out of the grammar. An omitted one lands as a
#: section box with no look, and the look is what the shots inside it are told to carry.
PLANNED_SECTION_DECISIONS = ("prompt",)


def director_result_schema(
    *, require: Sequence[str] = (), min_shots: int = 0
) -> dict[str, Any]:
    """`DirectorResult`'s JSON schema with ``require`` promoted into ``required``.

    This exists because of a measured, three-run failure that nobody's prompt wording could
    have fixed. `shots` and `sections` both carry ``default_factory=list``, so Pydantic does
    not mark them required, so ``model_json_schema()["required"]`` is
    ``["message", "treatment", "style_bible"]`` — and that schema is what rides
    ``response_format: json_schema strict`` to LM Studio's constrained decoder. A reply
    containing no ``shots`` at all was therefore *correct* against the grammar it was
    decoded under. Populate measured `shots: []` on 1 of 3 single-call rolls (run 2), both
    halves delivered on 0 of 9 rolls across two runs, and 8 of 8 empty on a second model.
    The schema never asked for the field.

    A builder rather than a second `BaseModel`, and the reason is that the two populate
    stages want **different** required sets: the structure stage cannot proceed without
    `sections` and must not be forced to invent shots, and the shots stage is the exact
    mirror. Dedicated models would mean one subclass per required set — each re-declaring
    fields whose only difference is a default — and the parse target would still be
    `DirectorResult`, because that is what the route validates and merges. Deriving from
    `model_json_schema()` also means a field added to `DirectorResult` reaches the wire
    without anyone editing a hand-written schema, which is the failure this whole function
    is repairing in a different guise.

    What must **not** happen here is `DirectorResult.shots` becoming required globally. The
    chat route (`plan`'s default) shares this model, and a Director asking a question has
    every right to an answer with no shot list; forcing one would be a worse bug than the
    one being fixed. So the default of ``require`` is empty and the default schema is
    byte-identical to what has always been sent.

    ``min_shots`` sets ``minItems`` on the shots array. Measured against LM Studio on
    2026-08-20, not assumed: the constrained decoder **honours it** — a request asking in
    words for two shots against ``minItems: 12`` came back with twelve on 4 of 4 calls. It
    is a real count guarantee and it is also a loaded gun, because the same probe showed the
    decoder does *not* enforce numeric bounds (`exclusiveMinimum`, `maximum`): the padding
    entries carried ``duration: 0`` and ``duration: 1200``, and **3 of those 4 replies
    failed `PlannedShot` validation whole**. Length is grammar; plausibility is not, and an
    unparseable reply is a 502 where a short one is a guided retry. Left at 0 by default for
    that reason, and callers that set it are choosing a hard floor over a parseable answer.

    Requiring `shots` or `sections` also requires the *per-entry* decisions on them
    (`PLANNED_SHOT_DECISIONS`, `PLANNED_SECTION_DECISIONS`), because the same hole runs one
    level down and was found there on 2026-08-20: `PlannedShot.performance` and
    `PlannedSection.prompt` both carry defaults, so both were absent from their entry
    schema's ``required``, so both were asked for in the instruction and never in the
    grammar. The chat route requires neither array and is therefore byte-identical to what
    it has always sent, entries included.
    """
    return constrained_schema(
        DirectorResult,
        require=require,
        require_each={
            "shots": PLANNED_SHOT_DECISIONS if "shots" in require else (),
            "sections": PLANNED_SECTION_DECISIONS if "sections" in require else (),
        },
        min_items={"shots": min_shots},
    )


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


# ---------------------------------------------------------------------------------------------
# Treatment planning: three tools that cannot be mistaken for each other (AD-38, story 14.1)
# ---------------------------------------------------------------------------------------------
#
# **The required lists below are the load-bearing part of this feature, not the prompts.** On this
# model an *optional field* and a *dropped field* are the same bytes, and this repository has paid
# for that three times:
#
# * `DirectorResult` never required `shots`, which was the root cause of **every** empty-shots
#   failure — asked for in words across three measured runs while the grammar never mentioned it.
# * `fill_shots` applied modes and citations while silently omitting `use_song_audio` and
#   `singing` twice, with the narration claiming otherwise.
# * `_promoted` raises on an unknown name because a promotion that quietly does nothing
#   reproduces that shape exactly.
#
# So *"asked a question and wrote nothing"* is a **different tool**, not a missing key. Three
# tools, each strict, and `_strict_tool_schema` refuses to emit one that has an optional field at
# all — see it. TP-10 ("planning writes the Brief and proposals, never the Treatment or the Style
# bible") is likewise structural: `WriteBriefArguments` has no field for either, so there is no
# check anybody can forget to run.

#: The tool that asks and writes nothing. Its existence *is* AD-38: a question-only turn has to be
#: a shape of its own, because the alternative — one tool with the document field left out — is
#: byte-identical to a write whose document field the model dropped.
ASK_DIRECTOR_TOOL = "ask_director"

#: The one tool in this application that a machine writes a creative document with. Named for the
#: document rather than for the act, so that "which tool can write the Style bible" has the answer
#: *none* by inspection of this list rather than by reading a schema.
WRITE_BRIEF_TOOL = "write_brief"

#: Proposals. It writes nothing either — see `ProposeAssetsArguments` for what happens to one.
PROPOSE_ASSETS_TOOL = "propose_assets"


class AskDirectorArguments(BaseModel):
    """The `ask_director` argument object, and the source of its JSON schema.

    One field, required, and non-empty at both levels — the list and every string in it. A call
    with no questions is "asked nothing and wrote nothing", which is the silent no-op this whole
    taxonomy exists to make impossible; it is a `ValidationError` at the edge instead, reported to
    the Director as a refused call with the raw arguments beside it.
    """

    questions: list[Annotated[str, Field(min_length=1)]] = Field(
        min_length=1,
        description=(
            "The questions you want the Director to answer, one per entry, in plain language. "
            "Ask for what you actually need to write a better brief."
        ),
    )


class WriteBriefArguments(BaseModel):
    """The `write_brief` argument object, and the source of its JSON schema.

    **What is absent here is the point.** There is no `treatment` field and no `style_bible`
    field, so TP-10 holds by the shape of the call rather than by a check in a route that somebody
    could forget to write, move or weaken. A model cannot ask for something it has no key for.

    One field, and it is the whole document. `min_length=1` is what makes `PlanningTurn.brief`
    unambiguous: a validated write always carries text, so the empty string can mean *no write was
    proposed* and nothing else.
    """

    creative_brief: str = Field(
        min_length=1,
        description=(
            "The complete new creative brief, replacing the existing one in full. Not a patch and "
            "not a paragraph to append."
        ),
    )


class ProposeAssetsArguments(BaseModel):
    """The `propose_assets` argument object, and the source of its JSON schema.

    Reuses `AssetProposal` rather than mirroring it, unlike `ShotCitationFill`, and the difference
    is which side the model is on: `AssetCitation` is a *manifest* record whose fields the model
    must not gain the power to set, whereas `AssetProposal` is already exactly and only "what a
    model proposed" — `stage_manager` produces it from a model reply today. There is no second
    audience to drift away from.

    **What happens to a proposal today: nothing is persisted.** `Project` has no `asset_proposals`
    field, and this slice does not add one — where proposals *live* is Slice F (AD-36, AD-46). A
    proposal produced here is validated, counted and named in the turn's notice, and then it exists
    only in the chat thread the Director reads. Nothing is written to the library, no image is
    generated, and re-asking produces different proposals. Do not build anything on the assumption
    that a proposal survives the turn until Slice F gives it somewhere to survive in.
    """

    assets: list[AssetProposal] = Field(
        min_length=1,
        description="One entry per asset you are proposing. Send only assets the library lacks.",
    )


def _refuse_optional_fields(schema: dict[str, Any], label: str) -> None:
    """Raise unless every property of ``schema`` is in its ``required`` list.

    **This is what makes the promotion bite rather than decorate.** `_promoted` already refuses an
    unknown *name*; what it cannot see is a name its caller forgot to write down, and that omission
    is invisible in exactly the direction that matters — the field simply becomes optional, the
    decoder is free to drop it, and the tool call arrives looking like a smaller answer rather than
    a broken one. AD-38 says *every* field of a planning tool is required, so this asserts the
    whole rule instead of trusting three hand-kept tuples to stay exhaustive.

    It runs at import, because `PLANNING_TOOL_NAMES` is built from `planning_tools()` at module
    scope. A name dropped from a `require` list therefore stops the application from starting, on
    the precedent of the `Song`/`Shot` field classification gate, rather than shipping a tool whose
    grammar is quietly looser than its docstring.
    """
    required = schema.get("required", [])
    optional = [name for name in schema.get("properties", {}) if name not in required]
    if optional:
        raise ValueError(
            f"{label} would go on the wire with optional field(s) {', '.join(optional)}; "
            "every field of a planning tool is required (AD-38)"
        )


def _strict_tool_schema(
    model: type[BaseModel],
    *,
    require: Sequence[str],
    require_each: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """One planning tool's wire schema: `_model_facing_schema`, with every field promoted.

    `constrained_schema`'s two axes and `_model_facing_schema`'s docstring-stripping, joined
    because a tool needs both and neither alone: the response-format helper does not strip the
    paragraphs above (which are addressed to the next person editing this file, not to a model),
    and the tool helper promotes nothing.

    Deep-copied for `constrained_schema`'s reason — no variant may leak into the next caller's
    schema — and checked for exhaustiveness at both levels, which is the only reason the explicit
    `require` lists earn their place beside a rule that could otherwise be "require everything".
    An explicit list is a statement of intent that `_promoted` can catch a typo in; the check is
    what catches the opposite mistake, a name left out.

    **The inherited `required` list is cleared first, and that one line is what makes the
    promotion load-bearing rather than decorative.** `_promoted` *folds* a caller's names into
    whatever Pydantic already produced, which is right where it is used — `constrained_schema`
    tightens a grammar the model owns — and wrong here. A field with no default is in
    `model_json_schema()["required"]` for free, so on a model written without defaults every name
    in `require` would be a name the schema already had: deleting one would change nothing, no
    test could fail, and the whole list would be documentation that looks like a mechanism. The
    list is *stated* here instead. Delete a name and this function raises, at import, before the
    application starts.
    """
    schema = deepcopy(_model_facing_schema(model))
    schema.pop("required", None)
    schema["required"] = _promoted(schema, require)
    _refuse_optional_fields(schema, model.__name__)
    for prop, fields in (require_each or {}).items():
        entry = _entry_schema(schema, prop)
        entry.pop("required", None)
        entry["required"] = _promoted(entry, fields)
        _refuse_optional_fields(entry, f"{model.__name__}.{prop} entries")
    return schema


def planning_tools() -> list[dict[str, Any]]:
    """The planning tool surface as it goes on the wire, generated rather than written.

    `assistant_tools`' argument, and one more that is specific to this surface: `AssetProposal`'s
    `kind` is a `Literal`, so it reaches LM Studio's constrained decoder as an `enum` and *guides
    sampling* toward the four kinds this application has rather than only refusing a fifth after
    the fact. A hand-written copy of that list here would be the second definition of the taxonomy,
    and the one that goes stale in the direction that admits a malformed call.
    """
    return [
        {
            "type": "function",
            "function": {"name": name, "description": description, "parameters": parameters},
        }
        for name, description, parameters in (
            (
                ASK_DIRECTOR_TOOL,
                ASK_DIRECTOR_DESCRIPTION,
                _strict_tool_schema(AskDirectorArguments, require=("questions",)),
            ),
            (
                WRITE_BRIEF_TOOL,
                WRITE_BRIEF_DESCRIPTION,
                _strict_tool_schema(WriteBriefArguments, require=("creative_brief",)),
            ),
            (
                PROPOSE_ASSETS_TOOL,
                PROPOSE_ASSETS_DESCRIPTION,
                _strict_tool_schema(
                    ProposeAssetsArguments,
                    require=("assets",),
                    require_each={"assets": ("kind", "name", "prompt")},
                ),
            ),
        )
    ]


#: The tool names `parse_planning_reply` accepts, **derived from the surface** rather than listed
#: beside it: a fourth tool put on the wire without the parser learning it would send every one of
#: its calls to `malformed`, silently, and a second list is how that happens.
PLANNING_TOOL_NAMES: tuple[str, ...] = tuple(
    tool["function"]["name"] for tool in planning_tools()
)


class PlanningTurn(BaseModel):
    """One planning answer: what it said, what it asked, what it wants written, what it got wrong.

    Four fields where a single tool with optional keys would have had one, and that is the whole
    of AD-38 in one class. `questions` non-empty with `brief` empty is *asked and wrote nothing* —
    a complete, successful turn. `brief` empty on a turn that meant to write is not representable
    at all: `WriteBriefArguments.creative_brief` has `min_length=1`, so a `write_brief` call whose
    document field the model dropped fails validation and lands in `malformed`, where the Director
    is told about it. Those two outcomes are the same bytes under a one-tool design.

    `malformed` carries `AssistantTurn.malformed`'s contract exactly: a call that does not fit the
    vocabulary is the *expected* local-model failure rather than an exception, one bad entry must
    not discard the good ones beside it, and each entry is raw model output kept for a notice's
    `raw` — the field `DIRECTOR_CONTEXT_EXCLUDE` strips, so it is inspectable without being fed
    back into the next call.

    `proposals` is not persisted by anything. See `ProposeAssetsArguments`.
    """

    message: str = ""
    #: The Director-facing questions of a question-only turn, in the order the model asked them.
    questions: list[str] = Field(default_factory=list)
    #: The proposed replacement Brief, or `""` for *no write was proposed*. Unambiguous because a
    #: validated `write_brief` call always carries text.
    brief: str = ""
    proposals: list[AssetProposal] = Field(default_factory=list)
    malformed: list[str] = Field(default_factory=list)

    def wrote_nothing(self) -> bool:
        """True when this turn proposed no document write — asking, proposing, or neither.

        A method rather than `not turn.brief` at each call site, because the question a caller is
        asking is about the *turn*, and `brief == ""` is an encoding of the answer rather than the
        answer. It is also the sentence a test can name.
        """
        return not self.brief


def parse_planning_reply(reply: dict[str, Any]) -> PlanningTurn:
    """Split one provider reply into questions, a Brief, proposals and refused calls. I/O-free.

    `parse_assistant_reply`'s contract on the other surface, and every branch here is a shape a
    local model actually produces: no `tool_calls` at all (it chatted instead), a tool name that is
    not ours, `arguments` as a JSON string or as an already-decoded object, an argument object
    missing its one key, and a single bad entry inside an otherwise good list. None of them raises.

    Entries are validated **one at a time** wherever there is a list, for the reason the shot
    surface has to: validating the list whole would let one proposal with a fifth `kind` discard
    nine good ones beside it.

    A second `write_brief` call in one turn is refused rather than allowed to overwrite the first.
    Last-write-wins would be a silent discard of an entire proposed document — the one outcome
    this module is least willing to produce — and there is no sane merge of two whole briefs.
    """
    content = reply.get("content")
    turn = PlanningTurn(message=content.strip() if isinstance(content, str) else "")
    calls = reply.get("tool_calls")
    for call in calls if isinstance(calls, list) else []:
        function = call.get("function") if isinstance(call, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if not isinstance(function, dict) or name not in PLANNING_TOOL_NAMES:
            turn.malformed.append(_raw_argument(call))
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                turn.malformed.append(_raw_argument(function.get("arguments")))
                continue
        if not isinstance(arguments, dict):
            turn.malformed.append(_raw_argument(arguments))
            continue
        if name == WRITE_BRIEF_TOOL:
            try:
                written = WriteBriefArguments.model_validate(arguments)
            except ValidationError:
                turn.malformed.append(_raw_argument(arguments))
                continue
            if turn.brief:
                turn.malformed.append(_raw_argument(arguments))
                continue
            turn.brief = written.creative_brief
            continue
        key = "questions" if name == ASK_DIRECTOR_TOOL else "assets"
        entries = arguments.get(key)
        if not isinstance(entries, list) or not entries:
            turn.malformed.append(_raw_argument(arguments))
            continue
        for entry in entries:
            if name == ASK_DIRECTOR_TOOL:
                question = entry.strip() if isinstance(entry, str) else ""
                if question:
                    turn.questions.append(question)
                else:
                    turn.malformed.append(_raw_argument(entry))
                continue
            try:
                turn.proposals.append(AssetProposal.model_validate(entry))
            except ValidationError:
                turn.malformed.append(_raw_argument(entry))
    return turn


# The Treatment is already written per section — the live project's is literally an
# **Intro:** paragraph, a **Verse 1 & Verse 2:** paragraph, a **Chorus 1 & Chorus 2:**
# paragraph and so on — so this pass invents nothing. It *distributes* prose that exists
# onto boxes that exist, and the two hard rules below are what stop it doing anything else:
# address a section by the id it was given (the `ExpandedShot.shot_id` contract, for the
# same reason — a look merged onto the wrong box is free text that fails silently), and
# return an empty prompt rather than a plausible one for a section the treatment never
# describes. A grouped heading is not an ambiguity to resolve: "Verse 1 & Verse 2" means
# both verses take that look, and two sections carrying the same sentence is the correct
# answer, not a duplicate to break.
SECTION_LOOKS_PROMPT = """You are the creative director inside a local music-video production editor.
The song's structure is already marked: the director has laid out the sections on the
timeline. The treatment is already written. Your one job is to say, for each section, what
that section looks like — the shared look every shot inside it will carry.

You invent nothing. Everything you write must already be in the treatment or the style
bible; you are distributing prose that exists onto boxes that exist.

The input is a JSON object with these keys:
- treatment: the video's plan, usually written section by section. A heading naming more
  than one section ("Verse 1 & Verse 2") applies to every section it names.
- style_bible: the fixed visual language — palette, lighting, lenses, wardrobe, locations.
  It applies to the whole video, so it is context, not a section's look on its own.
- sections: the marked sections, in song order. Each has:
  - section_id: the section's identity. Copy it verbatim into your answer; it is how your
    look is matched to a section. A wrong or invented id is discarded.
  - label: the section's name, as the director marked it. Labels repeat and are numbered
    by the director's own hand ("Verse", "Verse 2"); match the treatment's headings to
    these labels, never to their order.
  - start, end, duration: absolute seconds against the song.
  - prompt: the look this section already carries, when it has one.

Return exactly one JSON object with keys: message, looks.
- message: one or two sentences on how you read the treatment onto the sections.
- looks: one entry per section, in the same order, each with:
  - section_id: copied verbatim from the section it answers.
  - label: that same section's label, copied verbatim. It is checked against the id; a
    mismatch is discarded.
  - prompt: one or two sentences naming the setting, the camera and the mood this section
    shares — concrete and self-contained, in the treatment's own terms. Never name a shot,
    a lyric, a time or an asset.

If the treatment does not describe a section, return that section with prompt set to the
empty string. An empty prompt is a real answer and the honest one; a plausible sentence for
a section nobody wrote about is the one thing you must not produce."""


class SectionLook(BaseModel):
    """One section's shared look, addressed to a `SongSection` **by id**.

    `label` is not decoration and not redundancy: it is a checksum. The id is the address,
    exactly as on `ExpandedShot`, but a model copying ids down a numbered list can copy the
    right-looking one off the wrong row, and section labels repeat by design — the lyric
    sheet's `[Tag]` blocks yield "Verse", "Verse 2", "Chorus", "Chorus 2". Carrying both
    means the route can refuse a look whose id and label disagree instead of writing
    Chorus 2's setting onto the Bridge, which is worse than leaving the Bridge empty.

    `prompt` has a default so that ``""`` is expressible, and `SECTION_LOOK_DECISIONS`
    promotes it into the grammar anyway — the pairing is the whole point. Required means
    the decoder must emit the key, so the model has to *decide* about every section; the
    empty string being legal means the decision may be "the treatment does not say".
    """

    section_id: str = Field(
        min_length=1,
        description="The id of the section this look is for, copied verbatim.",
    )
    label: str = Field(
        min_length=1,
        description="That section's label, copied verbatim. Checked against the id.",
    )
    prompt: str = Field(
        default="",
        description=(
            "The section's shared look, one or two sentences. Empty when the treatment "
            "does not describe this section."
        ),
    )


class SectionLooks(BaseModel):
    """The result of one whole-structure section-look pass."""

    message: str = ""
    looks: list[SectionLook] = Field(default_factory=list)


#: The per-entry decision, and it is the same hole one level down that
#: `PLANNED_SECTION_DECISIONS` names: `SectionLook.prompt` carries a default, so Pydantic
#: leaves it out of `required`, so the constrained decoder would be free to close each entry
#: without it — an id and a label and no look, which is what the route already has.
SECTION_LOOK_DECISIONS = ("prompt",)

#: The name the section-look schema goes on the wire under.
SECTION_LOOKS_SCHEMA_NAME = "section_looks"


def section_looks_schema() -> dict[str, Any]:
    """`SectionLooks`' schema with everything the route cannot proceed without required.

    `looks` is the entire answer — a reply without it is a call that did nothing — and
    `prompt` is the field the whole feature is about. Both carry defaults, so both were
    outside `required` for free, which is precisely the shape measured on
    `DirectorResult.shots` and `PlannedShot.performance`. `message` is left optional: the
    route never reads it, and a required narration field is one more thing to get wrong.
    """
    return constrained_schema(
        SectionLooks,
        require=("looks",),
        require_each={"looks": SECTION_LOOK_DECISIONS},
    )


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


#: One fenced block of a markdown reply, with its optional language tag dropped and its closing
#: fence optional — a reply that was cut off mid-fence still has usable content in front of the
#: truncation. Non-greedy, so the *first* complete block is matched rather than everything
#: between the first fence and the last one.
_CODE_FENCE = re.compile(r"```[A-Za-z0-9_+.-]*[ \t]*\r?\n?(.*?)(?:```|\Z)", re.DOTALL)


def extract_json(text: str) -> Any:
    """Decode the JSON value in one model reply, which is not always the whole reply.

    A ladder, tried in order, because each rung costs something the rung above it does not and
    only the first rung is what a well-behaved provider needs:

    1. ``json.loads`` on the stripped text. This is the happy path and it is byte-identical to
       what this module did before the ladder existed: a reply that is exactly one JSON object —
       which is what `response_format: json_schema strict` produces — never reaches rung 2.
    2. The contents of a markdown code fence. Tried *before* the scan below and not merely
       instead of it: when a model writes prose that quotes a JSON fragment and then answers
       inside a fence, the fence is the only thing that says which of the two is the answer.
    3. A balanced scan. `json.JSONDecoder().raw_decode` is pointed at each ``{`` and ``[`` in
       turn and the first offset that decodes wins. It is a real parser, so a brace inside a
       string value — an H3 prompt is full of them — cannot end the value early, and chatter on
       either side of the object is simply not part of what it consumed.

    Rung 3 is the one the recorded 2026-08-19 regression needs. `enable_thinking: false` stopped
    taking effect that day and the loaded model began reasoning *in `message.content`* before
    answering, so the payload is ``<paragraphs of reasoning> {"real": "json"}`` and `json.loads`
    refuses the whole string over chatter that is sitting beside a perfectly good object.

    Nothing decodes and the first failure is re-raised unchanged: callers catch
    `json.JSONDecodeError`/`ValueError` and translate it into a `DirectorError`, and inventing a
    new message here would only make a genuinely unparseable reply report itself differently.
    Pure and I/O-free.
    """
    if not isinstance(text, str):
        # `_content` already refuses non-strings for its own documented reason; this is here so
        # that a *different* caller cannot turn one into an `AttributeError` on `.strip()`, which
        # is outside every caught tuple. `TypeError` is inside all of them.
        raise TypeError("the reply carried no message content")

    stripped = text.strip()
    try:
        return json.loads(stripped)
    except ValueError as error:
        first_failure = error

    for fenced in _CODE_FENCE.findall(stripped):
        try:
            return json.loads(fenced.strip())
        except ValueError:
            continue

    decoder = json.JSONDecoder()
    for index, character in enumerate(stripped):
        if character not in "{[":
            continue
        try:
            value, _end = decoder.raw_decode(stripped, index)
        except ValueError:
            continue
        return value

    raise first_failure


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

    @staticmethod
    def _is_unloaded_model(response: httpx.Response) -> bool:
        """True for the one 400 whose cause LM Studio names: the configured id is not loaded."""
        return response.status_code == 400 and (
            "Failed to load model" in response.text or "Model is unloaded" in response.text
        )

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

        A second, independent fallback follows it: an *unexplained* 400 against a body that
        carried `response_format` is retried once with that key removed. Some
        OpenAI-compatible servers reject the key outright, and a request the server will not
        even accept is worth one schema-free attempt — `extract_json` is what makes the
        unconstrained reply usable. It is a fallback and never the default: the strict
        `json_schema` reaches LM Studio's constrained decoder, which is stronger than anything
        parsing can recover, and dropping it by choice would give that up on the setup this
        project actually runs on. "Unexplained" is load-bearing: a 400 that already said
        "Model is unloaded" has a known cause that has just been handled, so re-sending it
        without the schema would be a third request that fails for the reason the second one
        did — and would bury the provider's own refusal under a different one.

        The whole method is counted as in-flight, retries included, because `busy` exists to
        keep the VRAM eject away from a live call — and the retry is when the call is at its
        most fragile. `finally` rather than a decrement on the happy path: an exception that
        left the counter raised would wedge the eject off permanently for the life of the
        process.
        """
        self._in_flight += 1
        try:
            sent = body
            response = await self._client.post(
                f"{self.base_url}/chat/completions", headers=headers, json=sent
            )
            if self._is_unloaded_model(response):
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
                    # Carried forward rather than discarded, so the schema-free retry below —
                    # if it is needed at all — still addresses the loaded instance.
                    sent = {**sent, "model": loaded}
                    response = await self._client.post(
                        f"{self.base_url}/chat/completions", headers=headers, json=sent
                    )
            if (
                response.status_code == 400
                and "response_format" in sent
                and not self._is_unloaded_model(response)
            ):
                sent = {key: value for key, value in sent.items() if key != "response_format"}
                response = await self._client.post(
                    f"{self.base_url}/chat/completions", headers=headers, json=sent
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

    async def plan(
        self,
        *,
        message: str,
        project_context: dict[str, Any],
        temperature: float = PLAN_TEMPERATURE,
        response_schema: dict[str, Any] | None = None,
    ) -> DirectorResult:
        """One planning call. `temperature` is a parameter rather than a constant because a
        *guided retry* is a different kind of ask than a first attempt: the caller has
        already told the model exactly what was wrong, so sampling variety is no longer the
        point and obedience is. Populate Timeline lowers it on its one retry; every other
        caller takes the default and is byte-identical to what it always sent.

        `response_schema` is the same idea one level down: the strict schema reaches LM
        Studio's *constrained decoder*, so which fields it marks required decides what the
        model is physically able to emit — not what it is asked for. `None` sends
        `DirectorResult`'s own schema, which requires neither `shots` nor `sections` and is
        exactly right for the chat route, where a Director's question deserves an answer
        without an invented shot list. Callers that cannot proceed without a field pass
        `director_result_schema(require=...)`; see its docstring for the measurement.
        Whatever the schema demands, the reply is still validated as a `DirectorResult`, so
        a caller never has to handle a second result type."""
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
            "temperature": temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": DIRECTOR_RESULT_SCHEMA_NAME,
                    "strict": True,
                    "schema": (
                        DirectorResult.model_json_schema()
                        if response_schema is None
                        else response_schema
                    ),
                },
            },
        }
        try:
            response = await self._completion(body=body, headers=headers)
            return DirectorResult.model_validate(extract_json(self._content(response)))
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
                    # `assets` is the entire answer — an empty one is a 502 at the route —
                    # and `default_factory=list` kept it out of `required`, which is the
                    # `shots` hole in a second schema. The words above ask for proposals;
                    # this is the grammar agreeing with them.
                    "schema": constrained_schema(
                        StageManagerResult, require=("assets",)
                    ),
                },
            },
        }
        try:
            response = await self._completion(body=body, headers=headers)
            return StageManagerResult.model_validate(extract_json(self._content(response)))
        except (
            httpx.HTTPError,
            KeyError,
            IndexError,
            TypeError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            raise DirectorError(f"LLM director returned an invalid response: {error}") from error

    async def section_looks(self, *, looks_input: dict[str, Any]) -> SectionLooks:
        """One section-look pass: what does each marked section of the song look like.

        `plan`'s transport, `plan`'s error translation, a different job description and a
        different strict schema — `stage_manager`'s shape, for `stage_manager`'s reason.
        The input is built by the caller and is deliberately *not* the project dump: this
        pass reads the treatment, the style bible and the section boxes and nothing else,
        so there is no asset roster for it to leak a library name out of and no shot list
        for it to write about (`populate`'s measured naming leak, in a smaller room).

        Temperature is `plan`'s: this is a creative read of prose, not a count to obey.
        """
        if not self.base_url or not self.model:
            raise DirectorUnavailable(
                "LLM director is not configured. Set MVP_LLM_BASE_URL and MVP_LLM_MODEL."
            )
        headers = self._headers()
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SECTION_LOOKS_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(looks_input, ensure_ascii=False),
                },
            ],
            "temperature": PLAN_TEMPERATURE,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": SECTION_LOOKS_SCHEMA_NAME,
                    "strict": True,
                    "schema": section_looks_schema(),
                },
            },
        }
        try:
            response = await self._completion(body=body, headers=headers)
            return SectionLooks.model_validate(extract_json(self._content(response)))
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
            return ShotExpansion.model_validate(extract_json(self._content(response)))
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

    async def assist_planning(
        self, *, message: str, project_context: dict[str, Any]
    ) -> PlanningTurn:
        """One planning turn: the Director's request, the project, and the three planning tools.

        `assist`'s transport with `planning_tools()` in place of `assistant_tools()`, and the same
        two decisions for the same measured reasons. **One round trip**, because the agentic shape
        doubles the wall-clock of a call a Director is watching and would put the tool results into
        the second turn's context — the recorded root cause of Director degradation. And
        `tool_choice: "auto"` rather than forced, which matters more here than it does there: a
        forced call on this surface would make the model pick one of three acts on a turn where
        the honest answer is prose, and `ask_director` is close enough to "say something" that it
        would be picked. The no-tool case is reported as a notice instead.

        `project_context` is `director_chat`'s dump — the whole project minus
        `DIRECTOR_CONTEXT_EXCLUDE` — rather than a trimmed builder of its own, because planning is
        about the documents and the library rather than about a selection of shots. A builder that
        trims this for a long pass belongs with the pass that needs it (Slice E).

        Everything this returns is a *proposal*. Nothing here writes: the consent gate, the Brief's
        lock and the rejection guard all live in the route, per AD-35, so that a request without
        explicit consent is refused no matter what any earlier request said.
        """
        if not self.base_url or not self.model:
            raise DirectorUnavailable(
                "LLM director is not configured. Set MVP_LLM_BASE_URL and MVP_LLM_MODEL."
            )
        headers = self._headers()
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": PLANNING_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"request": message, "plan": project_context}, ensure_ascii=False
                    ),
                },
            ],
            "temperature": PLAN_TEMPERATURE,
            "tools": planning_tools(),
            "tool_choice": "auto",
        }
        try:
            response = await self._completion(body=body, headers=headers)
            return parse_planning_reply(self._reply(response))
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
                    # Every observation list is named in the system prompt above and every
                    # one of them carried `default_factory=list`, so the grammar asked for
                    # none of them: an inspection that never considered risks recorded
                    # itself as an inspection that found none, and the inspector panel
                    # renders that as "Risks: None". Requiring the keys costs an empty
                    # array on the wire and buys the difference between "looked, nothing
                    # there" and "never looked". The lists may still be empty — that is a
                    # finding; silence is not.
                    "schema": constrained_schema(
                        VisionInspection,
                        require=(
                            "identity",
                            "environment",
                            "continuity_cues",
                            "prompt_cues",
                            "risks",
                        ),
                    ),
                },
            },
        }
        try:
            response = await self._completion(body=body, headers=headers)
            return VisionInspection.model_validate(extract_json(self._content(response)))
        except (
            httpx.HTTPError,
            KeyError,
            IndexError,
            TypeError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            raise DirectorError(f"Vision inspector returned an invalid response: {error}") from error
