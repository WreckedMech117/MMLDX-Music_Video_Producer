"""Treatment planning: three tools that cannot be mistaken for each other (story 14.1).

Its own module for `test_assistant.py`'s reason — the guarantees here belong to one feature and
span three layers: the tool schemas that go on the wire, the parser that reads a reply back, and
the route that turns one into a stored turn.

**What is under test is a set of required lists.** AD-38 is not a stylistic preference: on this
model an optional field and a dropped field are the same bytes, and this repository has three
measured instances of paying for that. So most of what follows asserts about grammar rather than
about behaviour, and the tests that matter most are the ones that would fail if a field quietly
became optional.

`test_api.py`'s `make_client` is imported rather than duplicated: the route half of this feature is
only really tested through the application it is registered on.
"""

import json

import httpx
import pytest
from pydantic import BaseModel, ValidationError
from test_api import FakeDirector, make_client

from music_video_producer.app import (
    DIRECTOR_CONTEXT_EXCLUDE,
    DOCUMENT_LABELS,
    PLANNING_EMPTY_MESSAGE,
    PLANNING_WITHOUT_CONSENT_NOTICE,
    PLANNING_WITHOUT_TOOL_CALL_NOTICE,
    PlanningRequest,
    document_change_notice,
    document_first_draft_notice,
    document_lock_refusal,
)
from music_video_producer.director import (
    ASK_DIRECTOR_TOOL,
    PLANNING_TOOL_NAMES,
    PROPOSE_ASSETS_TOOL,
    WRITE_BRIEF_TOOL,
    AskDirectorArguments,
    AssetProposal,
    DirectorClient,
    DirectorError,
    DirectorUnavailable,
    PlanningTurn,
    ProposeAssetsArguments,
    WriteBriefArguments,
    _strict_tool_schema,
    parse_planning_reply,
    planning_tools,
)
from music_video_producer.models import Project, Song
from music_video_producer.planning_prompt import PLANNING_SYSTEM_PROMPT

TURN = "/api/projects/{project}/planning/turn"

BRIEF = "A night drive that opens into wilderness. One driver, one wolf, no dialogue."
REVISED = (
    "A night drive that opens into wilderness, told in three movements: the corridor of "
    "headlights, the threshold at the treeline, and the forest that swallows the car whole. "
    "One driver, one wolf, no dialogue, and no daylight anywhere in it."
)
#: A third candidate, deliberately as long as `REVISED` and not a shortening of it. A much shorter
#: candidate is refused by `document_rejection`'s ratio floor *before* consent is ever consulted,
#: and this test would then pass for a reason it was not written to assert — the silence rule
#: shares its branch with the consent refusal, so a rejected candidate produces no notice at all.
THIRD = (
    "A daylight desert crossing, told in three movements: the highway shimmer, the salt flat at "
    "noon, and the storm front that closes over the car entirely. One driver, one hawk, no "
    "dialogue, and no darkness anywhere in it."
)


# ---------------------------------------------------------------------------------------------
# Doubles and fixtures
# ---------------------------------------------------------------------------------------------


def ask(*questions: str) -> dict:
    """One `ask_director` tool call, as a provider puts it on the wire."""
    return {
        "type": "function",
        "function": {
            "name": ASK_DIRECTOR_TOOL,
            "arguments": json.dumps({"questions": list(questions)}),
        },
    }


def write(**arguments: object) -> dict:
    """One `write_brief` call. Takes the arguments verbatim so a test can *drop* a field."""
    return {
        "type": "function",
        "function": {"name": WRITE_BRIEF_TOOL, "arguments": json.dumps(arguments)},
    }


def propose(*assets: dict) -> dict:
    """One `propose_assets` call, entries verbatim for the same reason."""
    return {
        "type": "function",
        "function": {
            "name": PROPOSE_ASSETS_TOOL,
            "arguments": json.dumps({"assets": list(assets)}),
        },
    }


def reply(*calls: dict, content: str = "Here is what I think.") -> dict:
    """One provider message: the model's prose and whatever tools it called."""
    return {"content": content, "tool_calls": list(calls)}


def turn(**fields: object) -> PlanningTurn:
    """One planning answer, built the way the provider parser would have built it."""
    return PlanningTurn(**{"message": "Here is what I think.", **fields})


class PlanningDirector(FakeDirector):
    """Answers with a fixed turn, recording what it was handed.

    `FillingDirector`'s pattern on the planning surface: what actually reached the model is only
    assertable if the double keeps it, and "consent is never remembered" is a claim about a
    sequence of requests, so the double has to survive more than one.
    """

    def __init__(self, *answers: PlanningTurn, error: Exception | None = None):
        self.answers = list(answers) or [turn()]
        self.error = error
        self.contexts: list[dict] = []
        self.messages: list[str] = []

    async def assist_planning(self, *, message, project_context):
        self.messages.append(message)
        self.contexts.append(project_context)
        if self.error:
            raise self.error
        return self.answers[min(len(self.messages) - 1, len(self.answers) - 1)]


def planning_project(store, *, brief: str = BRIEF) -> Project:
    """A project at the stage this feature is for: a Brief exists and is being revised."""
    project = store.create(Project(name="Planning"))
    project.creative_brief = brief
    project.treatment = "Three movements: the corridor, the threshold, the forest."
    project.style_bible = "Sodium amber, hard backlight, 35mm grain."
    project.song = Song(title="Signal Bloom", source="imported", duration=60)
    store.save(project)
    return project


def last_reply(project: Project):
    """The last assistant turn of a project, as the stored `TreatmentMessage`."""
    return next(message for message in reversed(project.messages) if message.role == "assistant")


def notice_text(project: Project) -> str:
    return " ".join(notice.text for notice in last_reply(project).notices)


# ---------------------------------------------------------------------------------------------
# The required lists, which are the whole slice
# ---------------------------------------------------------------------------------------------


def properties_and_required(schema: dict) -> list[tuple[str, set, set]]:
    """Every object in a tool's schema — the arguments and each `$defs` entry — as name/props/req."""
    objects = [(schema.get("title", "arguments"), schema)]
    objects.extend(schema.get("$defs", {}).items())
    return [
        (name, set(body.get("properties", {})), set(body.get("required", [])))
        for name, body in objects
        if "properties" in body
    ]


def test_the_planning_surface_is_three_tools_and_the_parser_accepts_exactly_those():
    """Three tools, named, and the parser's vocabulary derived from the surface rather than typed.

    A fourth tool put on the wire without the parser learning it would send every one of its calls
    to `malformed` — silently, and with the Director told the model got it wrong. A second list is
    how that happens, so there is not one.
    """
    names = [tool["function"]["name"] for tool in planning_tools()]
    assert names == [ASK_DIRECTOR_TOOL, WRITE_BRIEF_TOOL, PROPOSE_ASSETS_TOOL]
    assert PLANNING_TOOL_NAMES == tuple(names)
    for tool in planning_tools():
        assert tool["type"] == "function"
        assert tool["function"]["description"].strip()


def test_every_planning_tool_requires_every_field_it_declares():
    """**The load-bearing assertion of story 14.1.**

    AD-38: each planning tool has its own strict schema with *every* field promoted. An optional
    field here would be a field this model is free — and correct — to omit, and the omission would
    arrive looking like a smaller answer rather than a broken one. That is the shape that made
    `DirectorResult.shots` the root cause of every empty-shots failure.

    Checked at both levels, arguments object and array entries, because `AssetProposal` is a model
    shared with `stage_manager`: the day somebody adds a defaulted field to it for Slice F's sake
    is the day this tool would otherwise stop requiring it.
    """
    for tool in planning_tools():
        for name, properties, required in properties_and_required(
            tool["function"]["parameters"]
        ):
            assert properties, f"{name} declares no fields"
            assert properties == required, (
                f"{tool['function']['name']}: {name} would go on the wire with "
                f"{sorted(properties - required)} optional"
            )


def test_no_planning_tool_has_a_field_that_could_write_the_treatment_or_the_style_bible():
    """TP-10, made structural rather than checked (spec constraint 3).

    "Planning writes the Brief and proposals, never the Treatment or the Style bible" is enforced
    by the write tool *having no field for them*. A check somewhere in a route is a check somebody
    can forget, move or weaken; a key the model has no way to send is none of those things.

    Field **names**, not the JSON blob: the descriptions say the words "treatment" and "style
    bible" out loud, on purpose, because the model has to be told what it is not for.
    """
    fields = {
        field
        for tool in planning_tools()
        for _name, properties, _required in properties_and_required(
            tool["function"]["parameters"]
        )
        for field in properties
    }
    assert "treatment" not in fields
    assert "style_bible" not in fields
    assert "creative_brief" in fields
    assert "creative_brief" not in set(WriteBriefArguments.model_fields) - {"creative_brief"}
    assert set(WriteBriefArguments.model_fields) == {"creative_brief"}


def test_a_promotion_naming_a_field_the_schema_does_not_have_raises():
    """`_promoted`'s guarantee, reached through the tool helper, and the reason it exists.

    A promotion that silently did nothing would reproduce the original failure exactly: a caller
    that believes it required a field, a decoder that was never told, and nothing anywhere that
    says so. So naming the Treatment here is a `ValueError`, not a tool that quietly cannot write
    one.
    """
    with pytest.raises(ValueError, match="no field"):
        _strict_tool_schema(WriteBriefArguments, require=("treatment",))
    with pytest.raises(ValueError, match="no field"):
        _strict_tool_schema(
            ProposeAssetsArguments, require=("assets",), require_each={"assets": ("colour",)}
        )


def test_a_field_left_out_of_a_required_list_is_refused_rather_than_shipped_optional():
    """The mutation this slice would otherwise be decoration without, executed as a test.

    `_promoted` catches a name that is *wrong*. Nothing catches a name that is *missing* — and
    missing is the direction with no symptom: the field becomes optional, the constrained decoder
    is free to drop it, and the call arrives as a smaller answer. So `_strict_tool_schema` refuses
    to emit a planning tool with any optional field, and this is that refusal shown failing.

    **Asserted first on a shipped model, and that is the half that took a correction.** The
    obvious form of this test — delete a name from a shipped `require` tuple and watch something
    fail — passes on its own with `_promoted` alone, because `_promoted` *folds* a caller's names
    into whatever Pydantic already produced and every field of these three models is required for
    free. `_strict_tool_schema` clears the inherited list before promoting, so the tuple is the
    sole statement of what the wire requires. Without that one line the whole list is
    documentation wearing a mechanism's clothes.

    Then on the shape the future edit makes: a field added later with a default, which is how
    every optional field in this codebase came to be optional.
    """
    with pytest.raises(ValueError, match="creative_brief"):
        _strict_tool_schema(WriteBriefArguments, require=())
    with pytest.raises(ValueError, match="kind"):
        _strict_tool_schema(
            ProposeAssetsArguments,
            require=("assets",),
            require_each={"assets": ("name", "prompt")},
        )

    class LaterField(BaseModel):
        kept: str
        added_later: str = ""

    both = _strict_tool_schema(LaterField, require=("kept", "added_later"))
    assert both["required"] == ["kept", "added_later"]

    with pytest.raises(ValueError, match="added_later"):
        _strict_tool_schema(LaterField, require=("kept",))

    class Entry(BaseModel):
        needed: str
        added_later: str = ""

    class Holder(BaseModel):
        entries: list[Entry]

    with pytest.raises(ValueError, match="added_later"):
        _strict_tool_schema(
            Holder, require=("entries",), require_each={"entries": ("needed",)}
        )


def test_the_proposal_kind_reaches_the_decoder_as_an_enum_and_not_as_a_free_string():
    """`AssetProposal.kind` is a `Literal`, which *guides sampling* rather than only refusing.

    The strict schema reaches LM Studio's constrained decoder, so an enum steers the model toward
    the four kinds this application has. A free string would turn the same output into a plausible
    value that reaches a manifest and is discovered later.
    """
    parameters = planning_tools()[2]["function"]["parameters"]
    proposal = parameters["$defs"]["AssetProposal"]
    assert proposal["properties"]["kind"]["enum"] == ["character", "setting", "prop", "style"]
    assert parameters["properties"]["assets"]["minItems"] == 1


def test_the_tool_schemas_carry_field_guidance_and_not_the_source_docstrings():
    """The paragraphs above these models are addressed to the next maintainer, not to a model.

    They cite AD numbers, name Slice F and run to a paragraph each; every character would be sent
    to a local model on every turn as though it were instruction.
    """
    for tool in planning_tools():
        parameters = tool["function"]["parameters"]
        assert "description" not in parameters
        for definition in parameters.get("$defs", {}).values():
            assert "description" not in definition
        assert "AD-38" not in json.dumps(parameters)
        assert "Slice F" not in json.dumps(parameters)
    assert planning_tools()[1]["function"]["parameters"]["properties"]["creative_brief"][
        "description"
    ]


def test_a_write_call_that_drops_its_one_field_is_a_validation_failure():
    """Spec acceptance: a tool call missing a required field is a failure, not a silent no-op.

    Asserted on the argument model directly as well as through the parser, because the parser's
    job is only to *route* the failure. The refusal itself is the schema's.
    """
    with pytest.raises(ValidationError):
        WriteBriefArguments.model_validate({})
    with pytest.raises(ValidationError):
        WriteBriefArguments.model_validate({"creative_brief": ""})
    with pytest.raises(ValidationError):
        AskDirectorArguments.model_validate({"questions": []})
    with pytest.raises(ValidationError):
        AskDirectorArguments.model_validate({"questions": [""]})
    with pytest.raises(ValidationError):
        ProposeAssetsArguments.model_validate({"assets": []})


# ---------------------------------------------------------------------------------------------
# Parsing one reply
# ---------------------------------------------------------------------------------------------


def test_a_question_only_turn_is_complete_and_is_not_a_write_whose_field_was_dropped():
    """**The acceptance criterion AD-38 exists for**, and both halves are asserted together.

    A turn that only asks writes nothing and is a success. A turn that meant to write and lost its
    document field also writes nothing — and under one tool with an optional key those two are the
    same bytes. Here they are two different records: one carries questions and no malformed call,
    the other carries a malformed call and no questions, and only the second is anything to fix.
    """
    asked = parse_planning_reply(reply(ask("Who is on screen?", "What must never appear?")))
    dropped = parse_planning_reply(reply(write()))

    assert asked.wrote_nothing() and dropped.wrote_nothing()
    assert asked.questions == ["Who is on screen?", "What must never appear?"]
    assert asked.malformed == []
    assert dropped.questions == []
    assert len(dropped.malformed) == 1
    assert asked != dropped


def test_a_write_call_carries_the_whole_brief_and_nothing_else():
    parsed = parse_planning_reply(reply(write(creative_brief=REVISED)))
    assert parsed.brief == REVISED
    assert not parsed.wrote_nothing()
    assert parsed.questions == [] and parsed.proposals == [] and parsed.malformed == []


def test_a_second_write_call_in_one_turn_is_refused_rather_than_overwriting_the_first():
    """Last-write-wins would silently discard a whole proposed document, which is the one outcome
    this parser is least willing to produce — and there is no sane merge of two briefs."""
    parsed = parse_planning_reply(
        reply(write(creative_brief=REVISED), write(creative_brief="Something else entirely."))
    )
    assert parsed.brief == REVISED
    assert len(parsed.malformed) == 1
    assert "Something else entirely." in parsed.malformed[0]


def test_one_malformed_call_does_not_discard_the_good_ones_beside_it():
    """Every shape a local model actually produces, and none of them may raise or lose a sibling."""
    parsed = parse_planning_reply(
        reply(
            {"type": "function", "function": {"name": "delete_everything", "arguments": "{}"}},
            {"type": "function", "function": {"name": ASK_DIRECTOR_TOOL, "arguments": "{oops"}},
            {"type": "function", "function": {"name": WRITE_BRIEF_TOOL, "arguments": "[]"}},
            ask("Who is on screen?"),
            write(creative_brief=REVISED),
            propose(
                {"kind": "character", "name": "Grey wolf", "prompt": "A grey wolf in wet pine."},
                {"kind": "hologram", "name": "Ghost", "prompt": "A ghost."},
                {"kind": "setting", "name": "Treeline", "prompt": "A treeline at midnight."},
            ),
        )
    )
    assert parsed.questions == ["Who is on screen?"]
    assert parsed.brief == REVISED
    assert [proposal.name for proposal in parsed.proposals] == ["Grey wolf", "Treeline"]
    assert len(parsed.malformed) == 4
    assert parsed.message == "Here is what I think."


def test_the_parser_survives_every_reply_shape_that_carries_no_usable_call():
    """None of these may raise: a reply is provider output and has no contract this can rely on."""
    assert parse_planning_reply({}).wrote_nothing()
    assert parse_planning_reply({"content": None, "tool_calls": None}).message == ""
    assert parse_planning_reply({"content": "  spaced  "}).message == "spaced"
    assert parse_planning_reply(reply("not-a-call")).malformed  # type: ignore[arg-type]
    # Arguments already decoded rather than sent as a JSON string: providers differ, and both
    # shapes reach this parser in practice.
    decoded = parse_planning_reply(
        {
            "content": "",
            "tool_calls": [
                {
                    "type": "function",
                    "function": {"name": ASK_DIRECTOR_TOOL, "arguments": {"questions": ["Why?"]}},
                }
            ],
        }
    )
    assert decoded.questions == ["Why?"]
    # An `ask_director` call with an empty list is "asked nothing and wrote nothing", which is the
    # silent no-op the whole taxonomy exists to make impossible.
    assert parse_planning_reply(reply(ask())).malformed


# ---------------------------------------------------------------------------------------------
# The wire: persona, tools, and the two failure mappings
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assist_planning_sends_the_persona_the_three_tools_and_the_project_verbatim():
    sent: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": reply(write(creative_brief=REVISED))}]},
        )

    client = DirectorClient(
        base_url="http://llm.test/v1",
        model="local-director",
        transport=httpx.MockTransport(handler),
    )
    answer = await client.assist_planning(
        message="tighten the brief", project_context={"creative_brief": BRIEF}
    )

    assert sent["messages"][0] == {"role": "system", "content": PLANNING_SYSTEM_PROMPT}
    body = json.loads(sent["messages"][1]["content"])
    assert body["request"] == "tighten the brief"
    assert body["plan"] == {"creative_brief": BRIEF}
    assert sent["tools"] == planning_tools()
    assert sent["tool_choice"] == "auto"
    # No `response_format`: the tool call *is* the structured output, and asking for a JSON object
    # as well is how a provider is talked into answering with one instead of calling the tool.
    assert "response_format" not in sent
    assert answer.brief == REVISED


@pytest.mark.asyncio
async def test_planning_is_unavailable_and_unusable_in_the_same_ways_the_director_is():
    with pytest.raises(DirectorUnavailable, match="not configured"):
        await DirectorClient(base_url="", model="").assist_planning(
            message="plan it", project_context={}
        )

    async def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    client = DirectorClient(
        base_url="http://llm.test/v1", model="m", transport=httpx.MockTransport(broken)
    )
    with pytest.raises(DirectorError):
        await client.assist_planning(message="plan it", project_context={})


# ---------------------------------------------------------------------------------------------
# The route: consent, the lock, and what a stored turn looks like
# ---------------------------------------------------------------------------------------------


def test_a_write_with_consent_writes_the_brief_and_says_so(tmp_path):
    """Spec acceptance: with consent, the Brief is written and the turn carries a notice."""
    director = PlanningDirector(turn(brief=REVISED))
    client, store, comfy = make_client(tmp_path, director=director)
    project = planning_project(store)

    response = client.post(
        TURN.format(project=project.id),
        json={"message": "tighten the brief", "apply_documents": True},
    )

    assert response.status_code == 200
    stored = store.get(project.id)
    assert stored.creative_brief == REVISED
    # Captured on apply, so the Brief the Director had is recoverable from the moment a machine
    # could take it away.
    assert stored.creative_brief_previous == BRIEF
    assert document_change_notice([DOCUMENT_LABELS["creative_brief"]]) in notice_text(stored)
    assert stored.treatment == "Three movements: the corridor, the threshold, the forest."
    assert not comfy.prompts


def test_a_write_without_consent_is_refused_and_nothing_is_recorded(tmp_path):
    """Spec acceptance: without consent it is refused — and no recovery slot is spent either.

    Spending the slot on a refusal would turn a protective refusal into the data loss it prevents.
    """
    director = PlanningDirector(turn(brief=REVISED))
    client, store, _ = make_client(tmp_path, director=director)
    project = planning_project(store)

    response = client.post(
        TURN.format(project=project.id), json={"message": "tighten the brief"}
    )

    assert response.status_code == 200
    stored = store.get(project.id)
    assert stored.creative_brief == BRIEF
    assert stored.creative_brief_previous == ""
    assert PLANNING_WITHOUT_CONSENT_NOTICE.format(
        document=DOCUMENT_LABELS["creative_brief"]
    ) in notice_text(stored)
    assert last_reply(stored).notices[0].kind == "refusal"


def test_consent_is_per_request_and_is_never_remembered(tmp_path):
    """**AD-35, and the property Slice D is most likely to lose.**

    Planning Mode is *frontend* state. The server never stores, infers or remembers consent, so a
    second request with the flag off is refused however emphatically the first one carried it —
    and nothing about the first request survives on the `Project` for the second to read.
    """
    director = PlanningDirector(turn(brief=REVISED), turn(brief=THIRD))
    client, store, _ = make_client(tmp_path, director=director)
    project = planning_project(store)

    client.post(
        TURN.format(project=project.id), json={"message": "first", "apply_documents": True}
    )
    written = store.get(project.id)
    assert written.creative_brief == REVISED

    client.post(TURN.format(project=project.id), json={"message": "second"})

    after = store.get(project.id)
    assert after.creative_brief == REVISED, "consent from an earlier request wrote a later one"
    assert PLANNING_WITHOUT_CONSENT_NOTICE.format(
        document=DOCUMENT_LABELS["creative_brief"]
    ) in notice_text(after)
    # And it is not stored anywhere to be read back: the request model is the only carrier.
    assert "apply_documents" not in after.model_dump()
    assert PlanningRequest(message="x").apply_documents is False
    assert PlanningRequest(message="x", apply_documents=None).apply_documents is False


def test_a_locked_brief_is_refused_by_the_rule_that_refuses_a_director_reply(tmp_path):
    """Spec acceptance, and constraint 4: Slice A's `document_lock_refusal`, not a second answer.

    The sentence asserted here is the *function's*, so a divergence between what refuses a chat
    reply and what refuses a planning write cannot be introduced without failing this.
    """
    director = PlanningDirector(turn(brief=REVISED))
    client, store, _ = make_client(tmp_path, director=director)
    project = planning_project(store)
    project.creative_brief_locked = True
    store.save(project)

    client.post(
        TURN.format(project=project.id),
        json={"message": "tighten it anyway", "apply_documents": True},
    )

    stored = store.get(project.id)
    assert stored.creative_brief == BRIEF
    assert stored.creative_brief_previous == ""
    assert document_lock_refusal(stored, "creative_brief") in notice_text(stored)


def test_a_question_only_turn_through_the_route_writes_nothing_and_is_a_success(tmp_path):
    """A complete turn: 200, an ordinary assistant message, and nothing changed anywhere."""
    director = PlanningDirector(
        turn(questions=["Who is on screen?", "What must never appear in it?"])
    )
    client, store, _ = make_client(tmp_path, director=director)
    project = planning_project(store)

    response = client.post(
        TURN.format(project=project.id),
        json={"message": "what do you need to know?", "apply_documents": True},
    )

    assert response.status_code == 200
    stored = store.get(project.id)
    assert stored.creative_brief == BRIEF
    assert stored.creative_brief_previous == ""
    text = notice_text(stored)
    assert "Who is on screen?" in text and "What must never appear in it?" in text
    # A `flag`, not a `refusal`: nothing was refused, and teaching the Director to read the honest
    # answer as a malfunction is the failure this tool exists to avoid.
    assert [notice.kind for notice in last_reply(stored).notices] == ["flag"]


def test_a_planning_turn_is_an_ordinary_message_carrying_structured_notices(tmp_path):
    """AD-43: no parallel record, and no announcement parsed back out of prose.

    The joined `content` still carries every sentence — that string is what saved projects hold
    and what the client's marker helpers read — but the structure is the `notices` list beside it.
    """
    director = PlanningDirector(turn(brief=REVISED, questions=["Is amber right?"]))
    client, store, _ = make_client(tmp_path, director=director)
    project = planning_project(store)

    client.post(
        TURN.format(project=project.id), json={"message": "go", "apply_documents": True}
    )

    stored = store.get(project.id)
    assert [message.role for message in stored.messages] == ["user", "assistant"]
    stored_reply = last_reply(stored)
    assert [notice.kind for notice in stored_reply.notices] == ["change", "flag"]
    for notice in stored_reply.notices:
        assert notice.text in stored_reply.content
    assert stored_reply.content.startswith("Here is what I think.")


def test_a_proposal_is_reported_as_going_nowhere_because_it_does(tmp_path):
    """Constraint 7: this slice produces and validates proposals; Slice F gives them somewhere to
    live. "3 assets proposed" on its own reads exactly like three assets appeared."""
    director = PlanningDirector(
        turn(
            proposals=[
                AssetProposal(kind="character", name="Grey wolf", prompt="A grey wolf in pine."),
                AssetProposal(kind="setting", name="Treeline", prompt="A treeline at midnight."),
            ]
        )
    )
    client, store, _ = make_client(tmp_path, director=director)
    project = planning_project(store)

    client.post(TURN.format(project=project.id), json={"message": "what is missing?"})

    stored = store.get(project.id)
    assert "asset_proposals" not in stored.model_dump()
    assert stored.assets == []
    text = notice_text(stored)
    assert "Grey wolf" in text and "Treeline" in text
    assert "nothing was added to the library" in text
    # Proposing needs no consent, because proposing writes nothing.
    assert [notice.kind for notice in last_reply(stored).notices] == ["flag"]


def test_a_malformed_call_is_reported_with_its_raw_output_and_never_fed_back(tmp_path):
    """The raw travels in the notice's `raw`, which `DIRECTOR_CONTEXT_EXCLUDE` strips.

    `director_chat` used to paste model output into `content` — the one string in this application
    guaranteed to be handed back to the model on the next turn — so the guard against "JSON in
    context begets JSON" was the thing supplying it. This asserts the second turn's payload.
    """
    garbage = '{"creative_brief": null, "treatment": "not yours to write"}'
    director = PlanningDirector(turn(malformed=[garbage]), turn(questions=["And now?"]))
    client, store, _ = make_client(tmp_path, director=director)
    project = planning_project(store)

    client.post(TURN.format(project=project.id), json={"message": "revise it"})
    stored = store.get(project.id)
    notice = last_reply(stored).notices[0]
    assert notice.kind == "refusal"
    assert notice.raw == garbage
    assert garbage not in last_reply(stored).content

    client.post(TURN.format(project=project.id), json={"message": "again"})
    assert "not yours to write" not in json.dumps(director.contexts[1])
    assert "notices" in DIRECTOR_CONTEXT_EXCLUDE["messages"]["__all__"]


def test_a_turn_with_no_tool_call_at_all_is_distinguished_from_a_question(tmp_path):
    """One is the model choosing the tool that writes nothing; the other is it reaching for none.
    Collapsing them would hide the case where the persona needs iterating."""
    director = PlanningDirector(turn(message=""))
    client, store, _ = make_client(tmp_path, director=director)
    project = planning_project(store)

    client.post(TURN.format(project=project.id), json={"message": "thoughts?"})

    stored = store.get(project.id)
    assert PLANNING_WITHOUT_TOOL_CALL_NOTICE in notice_text(stored)
    # And a reply that returned no sentence of its own must not be a bare separator and notices.
    assert last_reply(stored).content.startswith(PLANNING_EMPTY_MESSAGE)


def test_a_first_draft_does_not_promise_a_previous_version_that_is_not_there(tmp_path):
    """A blank Brief accepts any first draft, so the slot it captures is empty and a restore would
    refuse. Calling that a replacement whose previous version can be restored is a promise broken
    by the very next click."""
    director = PlanningDirector(turn(brief=REVISED))
    client, store, _ = make_client(tmp_path, director=director)
    project = planning_project(store, brief="")

    client.post(
        TURN.format(project=project.id), json={"message": "start it", "apply_documents": True}
    )

    stored = store.get(project.id)
    assert stored.creative_brief == REVISED
    assert stored.creative_brief_previous == ""
    text = notice_text(stored)
    assert document_first_draft_notice([DOCUMENT_LABELS["creative_brief"]]) in text
    assert document_change_notice([DOCUMENT_LABELS["creative_brief"]]) not in text


def test_a_brief_echoed_back_unchanged_is_not_a_replacement(tmp_path):
    """Spending the single recovery slot on an echo would annihilate the genuinely recoverable
    version with a copy of the live one, and announcing it would be a change nobody can find."""
    director = PlanningDirector(turn(brief=f"  {BRIEF}  "))
    client, store, _ = make_client(tmp_path, director=director)
    project = planning_project(store)
    project.creative_brief_previous = "The genuinely recoverable brief."
    store.save(project)

    client.post(
        TURN.format(project=project.id), json={"message": "again", "apply_documents": True}
    )

    stored = store.get(project.id)
    assert stored.creative_brief == BRIEF
    assert stored.creative_brief_previous == "The genuinely recoverable brief."
    assert last_reply(stored).notices == []


def test_the_route_maps_an_unconfigured_and_an_unusable_model_the_way_chat_does(tmp_path):
    client, store, _ = make_client(
        tmp_path, director=PlanningDirector(error=DirectorUnavailable("not configured"))
    )
    project = planning_project(store)
    assert client.post(TURN.format(project=project.id), json={"message": "go"}).status_code == 503

    client, store, _ = make_client(
        tmp_path / "second", director=PlanningDirector(error=DirectorError("bad reply"))
    )
    project = planning_project(store)
    assert client.post(TURN.format(project=project.id), json={"message": "go"}).status_code == 502


def test_an_empty_message_is_refused_before_the_model_is_called(tmp_path):
    director = PlanningDirector()
    client, store, _ = make_client(tmp_path, director=director)
    project = planning_project(store)

    assert (
        client.post(TURN.format(project=project.id), json={"message": ""}).status_code == 422
    )
    assert director.messages == []


def test_the_prompt_carries_the_project_without_its_recovery_slots(tmp_path):
    """`director_chat`'s exclusion, and it is not an optimisation: the dump is the whole project,
    so leaving the slots in would echo a second full copy of every document into every prompt —
    the rich-context degradation `document_rejection` was written to catch."""
    director = PlanningDirector()
    client, store, _ = make_client(tmp_path, director=director)
    project = planning_project(store)
    project.creative_brief_previous = "An older brief nobody should be prompted with."
    store.save(project)

    client.post(TURN.format(project=project.id), json={"message": "what do you see?"})

    context = director.contexts[0]
    assert context["creative_brief"] == BRIEF
    assert "creative_brief_previous" not in context
    # The user's own turn is in the snapshot the prompt was built from, so the model sees the
    # message it is answering.
    assert context["messages"][-1]["content"] == "what do you see?"
