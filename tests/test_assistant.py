"""Assistant ProducerBot: the tool surface, the guards it inherits, and the report it produces.

Its own module rather than more of `test_api.py` because the guarantees here are one feature's and
they span three layers — the provider reply, the route, and the composer controls in the browser —
so the file that has to stay coherent is the one about the feature.

`test_api.py` and `test_frontend_contract.py` are imported for their harnesses rather than
duplicating them: `make_client` is the app-with-a-double, and `run_workspace` boots `app.js` against
a stub DOM so the UI guarantees are *executed* rather than grepped for.
"""

import json
import re
from pathlib import Path
from typing import get_args

import httpx
import pytest
from pydantic import ValidationError
from test_api import FakeDirector, make_client, upload_asset
from test_frontend_contract import API_JS, INDEX_HTML, run_module, run_workspace

from music_video_producer.app import (
    ASSISTANT_APPLIED_NOTICE,
    ASSISTANT_DUPLICATE_NOTICE,
    ASSISTANT_EMPTY_FILL_NOTICE,
    ASSISTANT_EMPTY_MESSAGE,
    ASSISTANT_MALFORMED_NOTICE,
    ASSISTANT_MISSING_TARGET_NOTICE,
    ASSISTANT_OMITTED_NOTICE,
    ASSISTANT_OUT_OF_SCOPE_NOTICE,
    ASSISTANT_SPECIFICATION_NOTICE,
    ASSISTANT_UNKNOWN_ASSET_NOTICE,
    ASSISTANT_WITHOUT_SHOTS,
    ASSISTANT_WITHOUT_TOOL_CALL_NOTICE,
    DIRECTOR_CONTEXT_EXCLUDE,
    EXPANSION_LOCKED_NOTICE,
    EXPANSION_RENDERED_NOTICE,
    AssistantRequest,
    assistant_prompt_rejection,
    shot_render_provenance,
    shot_write_refusal,
)
from music_video_producer.assistant_prompt import (
    ASSISTANT_SYSTEM_PROMPT,
    EXPAND_PROMPTS_DESCRIPTION,
    PROMPT_CRAFT,
)
from music_video_producer.batch import PLACEHOLDER_PROMPT, shot_label
from music_video_producer.director import (
    EXPAND_PROMPTS_TOOL,
    FILL_SHOTS_TOOL,
    AssistantTurn,
    DirectorClient,
    DirectorError,
    DirectorUnavailable,
    ShotExpansionRequest,
    ShotFill,
    assistant_tools,
    parse_assistant_reply,
)
from music_video_producer.models import (
    SHOT_MODE_SPECS,
    Asset,
    AssetCitation,
    AssetRole,
    Project,
    Shot,
    ShotMode,
    SingingState,
    Song,
)
from music_video_producer.timeline import assistant_input

FILL = "/api/projects/{project}/assistant/fill"


# ---------------------------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------------------------


def turn(*fills: dict, message: str = "Filled it in.", malformed: tuple[str, ...] = ()) -> AssistantTurn:
    """One assistant answer, built the way the provider parser would have built it."""
    return AssistantTurn(
        message=message,
        fills=[ShotFill(**fill) for fill in fills],
        malformed=list(malformed),
    )


class FillingDirector(FakeDirector):
    """Answers with a fixed turn, recording the request and the payload it was handed.

    `RevisingDirector`'s pattern applied to this call: what actually reached the model is only
    assertable if the double keeps it.
    """

    def __init__(self, answer: AssistantTurn | None = None, *, error: Exception | None = None):
        self.answer = answer if answer is not None else turn()
        self.error = error
        self.inputs: list[dict] = []
        self.messages: list[str] = []

    async def assist(self, *, message, assistant_input):
        self.messages.append(message)
        self.inputs.append(assistant_input)
        if self.error:
            raise self.error
        return self.answer


def producer_project(store, *, name: str = "ProducerBot") -> Project:
    """A project in the state this feature is *for*: a library and a rough plan already exist."""
    project = store.create(Project(name=name))
    project.creative_brief = "A night drive that opens into wilderness."
    project.treatment = "Three movements: the corridor, the threshold, the forest."
    project.style_bible = "Sodium amber, hard backlight, 35mm grain."
    project.song = Song(title="Signal Bloom", source="imported", duration=60, caption="Slow synth rock")
    project.assets = [
        Asset(id="asset_wolf", name="Grey wolf", kind="character", path="media/wolf.png"),
        Asset(id="asset_forest", name="Pine forest", kind="setting", path="media/forest.png"),
    ]
    project.shots = [
        Shot(id="shot_one", start=0, duration=5, prompt=PLACEHOLDER_PROMPT),
        Shot(id="shot_two", start=5, duration=6, prompt=""),
    ]
    store.save(project)
    return project


def reply_text(project: Project) -> str:
    """The whole of the last assistant turn: its prose and every notice sentence."""
    return next(
        message.content for message in reversed(project.messages) if message.role == "assistant"
    )


def reply_notices(project: Project):
    return next(
        message.notices for message in reversed(project.messages) if message.role == "assistant"
    )


# ---------------------------------------------------------------------------------------------
# The tool surface
# ---------------------------------------------------------------------------------------------


def test_the_tool_schema_is_generated_from_the_shot_taxonomy_and_not_hand_written():
    """The design decision this story waited for shot modes to land for.

    A tool that takes a mode as a free string turns a malformed model output into a plausible
    mistake that reaches the manifest; a tool that takes it from `ShotMode` turns the same output
    into a validation error at the edge. So the enums on the wire have to *be* the enums in
    `models.py` — derived, never transcribed — and this asserts it for all three.
    """
    tools = assistant_tools()
    assert [tool["function"]["name"] for tool in tools] == [FILL_SHOTS_TOOL, EXPAND_PROMPTS_TOOL]
    parameters = tools[0]["function"]["parameters"]
    shot = parameters["$defs"]["ShotFill"]["properties"]
    citation = parameters["$defs"]["ShotCitationFill"]["properties"]

    def enum_of(schema):
        """The enum list, through the `anyOf` wrapper an optional field's schema carries."""
        if "enum" in schema:
            return schema["enum"]
        return next(branch["enum"] for branch in schema["anyOf"] if "enum" in branch)

    assert enum_of(shot["mode"]) == list(get_args(ShotMode))
    assert enum_of(shot["singing"]) == list(get_args(SingingState))
    assert enum_of(citation["role"]) == list(get_args(AssetRole))
    # Every mode in the taxonomy is offerable, including the ones with no adapter: planning one is
    # allowed and is refused where GPU time would be spent, never here.
    assert set(enum_of(shot["mode"])) == set(SHOT_MODE_SPECS)
    # Only the id is required. Every other field means "leave it alone" when absent, which is what
    # makes a partial answer safe rather than destructive.
    assert parameters["$defs"]["ShotFill"]["required"] == ["shot_id"]


def test_the_tool_schema_carries_field_guidance_and_not_the_source_docstrings():
    """The docstrings above `ShotFill` are addressed to the next maintainer, not to the model.

    Pydantic renders a model docstring as the schema object's `description`, so leaving them in
    would ship a paragraph of internal argument — citing `AssetCitation` by name — to a local model
    on every turn as though it were instruction. The per-field sentences are written for the model
    and stay.
    """
    parameters = assistant_tools()[0]["function"]["parameters"]
    assert "description" not in parameters
    for definition in parameters["$defs"].values():
        assert "description" not in definition
    assert "AssetCitation" not in json.dumps(parameters)
    assert parameters["$defs"]["ShotFill"]["properties"]["shot_id"]["description"]
    assert parameters["$defs"]["ShotFill"]["properties"]["singing"]["description"]


def test_a_malformed_tool_call_is_reported_rather_than_discarding_the_good_ones_beside_it():
    """Every shape a local model actually produces, and none of them may raise or lose a sibling.

    The load-bearing case is the second: one entry naming a mode the taxonomy has never had must
    not take the good entry in the same call with it, which is why entries are validated one at a
    time rather than as a list.
    """

    def call(arguments, *, name=FILL_SHOTS_TOOL):
        return {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": name,
                "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
            },
        }

    parsed = parse_assistant_reply(
        {
            "content": "Filled the wolf shot.",
            "tool_calls": [
                call({"shots": [{"shot_id": "shot_good", "mode": "text_to_video"}]}),
                call(
                    {
                        "shots": [
                            {"shot_id": "shot_bad", "mode": "b_roll"},
                            {"shot_id": "shot_also_good", "prompt": "A wolf crosses the clearing"},
                        ]
                    }
                ),
                call({"shots": [{"shot_id": "s", "citations": [{"asset_id": "a", "role": "hero"}]}]}),
                call("{not json"),
                call({"shot_id": "shot_flat", "mode": "references"}),
                call({"shots": []}, name="render_shot"),
            ],
        }
    )

    assert parsed.message == "Filled the wolf shot."
    assert [fill.shot_id for fill in parsed.fills] == ["shot_good", "shot_also_good"]
    # The good entry beside the bad one kept its prompt, and its untouched fields stayed `None` —
    # which is what "leave it alone" is made of.
    assert parsed.fills[1].prompt == "A wolf crosses the clearing"
    assert (parsed.fills[1].mode, parsed.fills[1].singing, parsed.fills[1].citations) == (None, None, None)
    assert len(parsed.malformed) == 5
    # The refused output is kept verbatim, which is what the route puts in a notice's `raw`.
    assert any("b_roll" in entry for entry in parsed.malformed)
    assert any("hero" in entry for entry in parsed.malformed)
    assert "{not json" in parsed.malformed
    assert any("render_shot" in entry for entry in parsed.malformed)


def test_a_reply_with_no_tool_call_is_an_empty_turn_rather_than_an_error():
    """The model chatting instead of calling is a thing to report, not a 502.

    `tool_choice` is deliberately `auto`, so this is an ordinary outcome. The provider shapes around
    it are covered too: `tool_calls` absent, `null`, and not a list.
    """
    for reply in (
        {"content": "I would make it a wolf shot."},
        {"content": "I would make it a wolf shot.", "tool_calls": None},
        {"content": "I would make it a wolf shot.", "tool_calls": "fill_shots"},
    ):
        parsed = parse_assistant_reply(reply)
        assert parsed.fills == []
        assert parsed.message == "I would make it a wolf shot."
    # `content` is `null` on every reply that carries tool calls, which is exactly why `_content`
    # cannot serve this path: it raises for anything that is not a string.
    tool_only = parse_assistant_reply(
        {
            "content": None,
            "tool_calls": [
                {
                    "type": "function",
                    "function": {"name": FILL_SHOTS_TOOL, "arguments": '{"shots":[{"shot_id":"a"}]}'},
                }
            ],
        }
    )
    assert tool_only.message == ""
    assert [fill.shot_id for fill in tool_only.fills] == ["a"]


@pytest.mark.asyncio
async def test_assist_sends_the_persona_the_tools_and_the_builder_output_verbatim():
    """The wire shape of one assistant turn, and the three things it must carry."""
    sent: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "Made it a wolf shot.",
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": FILL_SHOTS_TOOL,
                                        "arguments": json.dumps(
                                            {
                                                "shots": [
                                                    {
                                                        "shot_id": "shot_wolf",
                                                        "mode": "text_to_video",
                                                        "prompt": "A grey wolf walks through wet pine",
                                                    }
                                                ]
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    project = Project(name="Wolf")
    project.shots = [Shot(id="shot_wolf", start=0, duration=5, prompt=PLACEHOLDER_PROMPT)]
    payload = assistant_input(project, shot_ids=["shot_wolf"])
    director = DirectorClient(
        base_url="http://llm.test/v1",
        model="local-director",
        transport=httpx.MockTransport(handler),
    )
    answer = await director.assist(
        message="make that a B-roll of a grey wolf in a forest", assistant_input=payload
    )

    assert sent["messages"][0] == {"role": "system", "content": ASSISTANT_SYSTEM_PROMPT}
    body = json.loads(sent["messages"][1]["content"])
    assert body["request"] == "make that a B-roll of a grey wolf in a forest"
    assert body["plan"] == payload
    assert sent["tools"] == assistant_tools()
    assert sent["tool_choice"] == "auto"
    # No `response_format`: the tool call *is* the structured output, and asking for a JSON object
    # as well is how a provider is talked into answering with one instead of calling the tool.
    assert "response_format" not in sent
    assert answer.fills[0].mode == "text_to_video"


@pytest.mark.asyncio
async def test_the_assistant_is_unavailable_and_unusable_in_the_same_ways_the_director_is():
    """503 without configuration, 502 for a reply that cannot be read. The route maps both."""
    with pytest.raises(DirectorUnavailable, match="not configured"):
        await DirectorClient(base_url="", model="").assist(message="fill it", assistant_input={})

    async def handler(request: httpx.Request) -> httpx.Response:
        # A reply whose `message` is not an object, which `_content` would never reach because it
        # only ever looks at `content`.
        return httpx.Response(200, json={"choices": [{"message": "fill_shots"}]})

    director = DirectorClient(
        base_url="http://llm.test/v1",
        model="local-director",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(DirectorError, match="invalid response"):
        await director.assist(message="fill it", assistant_input={})


# ---------------------------------------------------------------------------------------------
# The purpose-built payload
# ---------------------------------------------------------------------------------------------


def test_the_payload_is_trimmed_purpose_built_and_scoped_to_the_selection(tmp_path: Path):
    """`assistant_input`, held to `expansion_input`'s standard and to this feature's scope.

    The recorded root cause of Director degradation is rich context, so this asserts the *absences*
    as hard as the contents: no production state on a shot, no path or provenance on an asset, and
    no shot the Director did not select.
    """
    _, store, _ = make_client(tmp_path)
    project = producer_project(store)
    project.shots[1].status = "complete"
    project.shots[1].latest_output = "out/shot_two_00001.mp4"
    project.shots[1].prompt_id = "p-9"
    project.shots.append(Shot(id="shot_three", start=11, duration=4, prompt="A ridge line"))
    store.save(project)

    payload = assistant_input(store.get(project.id), shot_ids=["shot_one"])

    assert [entry["shot_id"] for entry in payload["shots"]] == ["shot_one"]
    entry = payload["shots"][0]
    # Named as the timeline names it, so the model and the reply's notices talk about one thing.
    assert entry["label"] == "SHOT 01 (shot_one)"
    assert entry["current_mode"] == "text_to_video"
    assert entry["current_prompt"] == PLACEHOLDER_PROMPT
    assert entry["singing"] == "unknown"
    assert entry["song_fraction"] == 0.0
    # The neighbour is in the payload as adjacency and a window, never as a prompt.
    assert entry["neighbours"]["next"] == {"shot_id": "shot_two", "start": 5.0, "end": 11.0}
    assert "current_prompt" not in entry["neighbours"]["next"]
    for withheld in ("status", "prompt_id", "latest_output", "latest_review", "approved_output"):
        assert withheld not in entry, withheld
    # Not even a derived flag: the route refuses a rendered shot on its own evidence, and a flag
    # would put back the production state the trimming exists to keep out.
    assert "rendered" not in json.dumps(payload)
    assert "p-9" not in json.dumps(payload)

    # The library, because citing is the whole point — and nothing about where it lives on disk.
    assert [asset["asset_id"] for asset in payload["assets"]] == ["asset_wolf", "asset_forest"]
    assert set(payload["assets"][0]) == {"asset_id", "name", "kind"}
    assert "media/wolf.png" not in json.dumps(payload)

    # The taxonomy as data. The schema constrains the mode to the enum; only this says what arity a
    # mode has, and which modes can be planned but not yet rendered.
    modes = {entry["mode"]: entry for entry in payload["modes"]}
    assert set(modes) == set(SHOT_MODE_SPECS)
    assert modes["first_middle_last"]["renderable"] is False
    assert modes["references"]["renderable"] is True
    assert [role["role"] for role in modes["first_middle_last"]["roles"]] == ["first", "middle", "last"]


def test_the_payload_describes_an_asset_from_its_inspection_and_bounds_what_it_carries(tmp_path: Path):
    """A library of forty assets at full length is a project dump wearing a different key."""
    from music_video_producer.models import VisionInspectionRecord
    from music_video_producer.timeline import ASSISTANT_DESCRIPTION_LIMIT

    _, store, _ = make_client(tmp_path)
    project = producer_project(store)
    project.assets[0].prompt = "a" * 900
    project.assets[0].vision = VisionInspectionRecord(summary="A grey wolf in half-light. " * 40)
    project.assets[1].prompt = "A dense pine forest at dusk"
    store.save(project)

    assets = {entry["asset_id"]: entry for entry in assistant_input(store.get(project.id), shot_ids=[])["assets"]}

    # The inspection wins over the generation prompt: it describes what the picture *is* rather
    # than what was asked for, and the two disagree often enough to matter.
    assert assets["asset_wolf"]["description"].startswith("A grey wolf in half-light.")
    assert len(assets["asset_wolf"]["description"]) <= ASSISTANT_DESCRIPTION_LIMIT + 1
    assert "a" * 100 not in assets["asset_wolf"]["description"]
    # No inspection, so the generation prompt is the honest answer.
    assert assets["asset_forest"]["description"] == "A dense pine forest at dusk"


# ---------------------------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------------------------


def test_one_shot_and_a_plain_request_sets_that_shot_alone(tmp_path: Path):
    """The Director's own interaction, end to end. The matrix's "One shot, plain request" row.

    Mode, prompt and roles land on the selected shot; every other shot is byte-identical
    afterwards; and no GPU time is spent, because the Director's own description puts image
    generation *after* this as their next act.
    """
    director = FillingDirector(
        turn(
            {
                "shot_id": "shot_one",
                "mode": "references",
                "prompt": "A grey wolf crosses a wet pine forest in low sodium light",
                "citations": [{"asset_id": "asset_wolf", "role": "reference"}],
            },
            message="Made it a wolf B-roll against the forest plate.",
        )
    )
    client, store, comfy = make_client(tmp_path, director)
    project = producer_project(store)
    before = store.get(project.id).shots[1].model_dump()

    response = client.post(
        FILL.format(project=project.id),
        json={"message": "make that a B-roll of a grey wolf in a forest", "shot_ids": ["shot_one"]},
    )

    assert response.status_code == 200, response.text
    stored = store.get(project.id)
    filled = stored.shots[0]
    assert filled.mode == "references"
    assert filled.prompt.startswith("A grey wolf crosses")
    assert [(item.asset_id, item.role) for item in filled.citations] == [("asset_wolf", "reference")]
    # The projection the whole render path speaks was kept in step by the model's own validator.
    assert filled.asset_ids == ["asset_wolf"]
    # No other shot changed, and nothing about this one moved that was not named.
    assert stored.shots[1].model_dump() == before
    assert filled.status == "draft"
    assert filled.seed == 0
    assert filled.locked is False

    # Not a render, on the one path that most looks like it should be.
    assert comfy.prompts == []
    assert stored.jobs == []

    # The thread carries the Director's own turn and a `change` notice naming the shot and the
    # fields — the report, per shot, that the frozen block asks for.
    assert [message.role for message in stored.messages[-2:]] == ["user", "assistant"]
    assert stored.messages[-2].content == "make that a B-roll of a grey wolf in a forest"
    notices = reply_notices(stored)
    assert [notice.kind for notice in notices] == ["change"]
    assert "SHOT 01 (shot_one)" in notices[0].text
    assert "References to video" in notices[0].text
    assert "prompt written" in notices[0].text
    assert "1 reference" in notices[0].text
    assert "Nothing was rendered" in notices[0].text
    assert director.messages == ["make that a B-roll of a grey wolf in a forest"]


def test_a_locked_shot_is_refused_in_the_words_a_directors_own_click_gets(tmp_path: Path):
    """The matrix's lock row, in both directions the lock can be met.

    The wording is `expand_shot_prompts`' own, reused rather than reworded: it is what every
    automated write to a Shot already says, and a second wording for one rule is how the two start
    describing different rules. `shot_write_refusal` is the shared decision behind both.
    """
    director = FillingDirector(
        turn(
            {"shot_id": "shot_one", "mode": "text_to_video", "prompt": "A locked corridor"},
            {"shot_id": "shot_two", "mode": "text_to_video", "prompt": "An open ridge at dawn"},
        )
    )
    client, store, comfy = make_client(tmp_path, director)
    project = producer_project(store)
    project.shots[0].locked = True
    store.save(project)
    before = store.get(project.id).shots[0].model_dump()

    response = client.post(
        FILL.format(project=project.id),
        json={"message": "fill these in", "shot_ids": ["shot_one", "shot_two"]},
    )

    assert response.status_code == 200
    stored = store.get(project.id)
    assert stored.shots[0].model_dump() == before
    assert stored.shots[1].prompt == "An open ridge at dawn"
    refusals = [notice for notice in reply_notices(stored) if notice.kind == "refusal"]
    assert any(
        notice.text == EXPANSION_LOCKED_NOTICE.format(shots="SHOT 01 (shot_one)")
        for notice in refusals
    ), refusals
    assert comfy.prompts == []


def test_a_selection_that_can_only_be_refused_never_reaches_the_model(tmp_path: Path):
    """One locked shot must not cost a model call to be told it is locked.

    Refused before the call, in the sentences the reply would have carried, so the refusal before
    and the notice after say the same thing about the same shot.
    """
    director = FillingDirector(turn({"shot_id": "shot_one", "prompt": "Should never be written"}))
    client, store, comfy = make_client(tmp_path, director)
    project = producer_project(store)
    project.shots[0].locked = True
    project.shots[1].latest_output = "out/shot_two_00001.mp4"
    store.save(project)
    before = store.get(project.id).model_dump()

    response = client.post(
        FILL.format(project=project.id),
        json={"message": "fill these in", "shot_ids": ["shot_one", "shot_two"]},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert EXPANSION_LOCKED_NOTICE.format(shots="SHOT 01 (shot_one)") in detail
    assert EXPANSION_RENDERED_NOTICE.format(shots="SHOT 02 (shot_two)") in detail
    assert director.inputs == []
    assert comfy.prompts == []
    # Nothing was written, including the chat thread: a refused turn is not a turn.
    assert store.get(project.id).model_dump() == before


@pytest.mark.parametrize(
    "provenance",
    [
        {"status": "ready"},
        {"status": "complete"},
        {"prompt_id": "p-1"},
        {"latest_output": "out/take_00001.mp4"},
        {"approved_output": "out/take_00001.mp4"},
    ],
)
def test_a_shot_carrying_render_provenance_is_refused_consistently_with_expansion(
    tmp_path: Path, provenance: dict
):
    """The matrix's provenance row, over every signal `shot_render_provenance` reads.

    Rewriting the prompt of a shot something already depends on is provenance loss: nothing fails,
    and afterwards the take and the prompt beside it simply disagree. Note the consequence the
    docs already record — marking a shot `ready` takes it out of the assistant's reach too.
    """
    director = FillingDirector(
        turn(
            {"shot_id": "shot_one", "prompt": "Should never be written"},
            {"shot_id": "shot_two", "prompt": "An open ridge at dawn"},
        )
    )
    client, store, comfy = make_client(tmp_path, director)
    project = producer_project(store)
    for field, value in provenance.items():
        setattr(project.shots[0], field, value)
    store.save(project)
    before = store.get(project.id).shots[0].model_dump()
    assert shot_render_provenance(store.get(project.id).shots[0])

    response = client.post(
        FILL.format(project=project.id),
        json={"message": "fill these in", "shot_ids": ["shot_one", "shot_two"]},
    )

    assert response.status_code == 200
    stored = store.get(project.id)
    assert stored.shots[0].model_dump() == before
    assert any(
        notice.text == EXPANSION_RENDERED_NOTICE.format(shots="SHOT 01 (shot_one)")
        for notice in reply_notices(stored)
    )
    assert comfy.prompts == []


def test_the_assistant_refuses_a_shot_approved_through_the_approve_route(tmp_path: Path):
    """The provenance refusal against a *route-made* approval, not a hand-built one.

    The parametrised provenance test above writes `approved_output` onto a Shot directly, which
    proves the guard reads the field and nothing about whether the approve route writes the field
    the guard reads. This drives the whole chain through shipped routes — mark-ready, submit,
    completion, approve — and then watches the assistant refuse what the route wrote; un-approve
    is the one way the Shot comes back into reach, and that is driven too.
    """
    from test_api import approve, land_take, mark_ready, submit_h3, unapprove

    director = FillingDirector(
        turn({"shot_id": "shot_one", "prompt": "Should never be written over an approval"})
    )
    client, store, comfy = make_client(tmp_path, director)
    project = producer_project(store)
    project.shots[0].prompt = "A grey wolf crosses a wet pine forest"
    store.save(project)
    assert mark_ready(client, project.id, "shot_one").status_code == 200
    submitted = submit_h3(client, project.id, "shot_one")
    assert submitted.status_code == 202
    land_take(client, comfy, project.id, submitted.json()["id"], "shot_one-h3_00001.mp4")
    assert approve(client, project.id, "shot_one").status_code == 200
    approved = store.get(project.id).shots[0].model_dump()

    response = client.post(
        FILL.format(project=project.id),
        json={"message": "rewrite the wolf shot", "shot_ids": ["shot_one"]},
    )

    # Every selected Shot is refusable, so the refusal lands before the model is spent — and it
    # names the provenance rule, exactly as a hand-built approval is refused.
    assert response.status_code == 422
    assert EXPANSION_RENDERED_NOTICE.format(shots="SHOT 01 (shot_one)") in response.json()["detail"]
    assert store.get(project.id).shots[0].model_dump() == approved
    assert len(comfy.prompts) == 1

    # Un-approve is the one way back into the assistant's reach... except that a Shot with a
    # take keeps its render provenance, so it stays refused — the approval is not the only
    # marker, and clearing it must not quietly hand a rendered Shot to an automated writer.
    assert unapprove(client, project.id, "shot_one").status_code == 200
    still = client.post(
        FILL.format(project=project.id),
        json={"message": "rewrite the wolf shot", "shot_ids": ["shot_one"]},
    )
    assert still.status_code == 422
    assert EXPANSION_RENDERED_NOTICE.format(shots="SHOT 01 (shot_one)") in still.json()["detail"]


def test_the_selection_is_the_scope_and_the_model_cannot_widen_it(tmp_path: Path):
    """The guard that stops tool-calling widening what the assistant can act *on*.

    `shot_two` is real, unlocked and perfectly writable. It was not selected, so it is refused —
    which is a stronger guarantee than a consent boolean, because a boolean says "you may write"
    and this says "you may write *here*".
    """
    director = FillingDirector(
        turn(
            {"shot_id": "shot_one", "prompt": "A grey wolf crosses a wet pine forest"},
            {"shot_id": "shot_two", "prompt": "Written to a shot nobody selected"},
            {"shot_id": "shot_invented", "prompt": "Written to a shot that does not exist"},
        )
    )
    client, store, comfy = make_client(tmp_path, director)
    project = producer_project(store)
    before = store.get(project.id).shots[1].model_dump()

    response = client.post(
        FILL.format(project=project.id),
        json={"message": "fill in the wolf shot", "shot_ids": ["shot_one"]},
    )

    assert response.status_code == 200
    stored = store.get(project.id)
    assert stored.shots[0].prompt.startswith("A grey wolf")
    assert stored.shots[1].model_dump() == before
    # And nothing was created for the invented id: the model may not make a shot.
    assert [shot.id for shot in stored.shots] == ["shot_one", "shot_two"]
    out_of_scope = [
        notice
        for notice in reply_notices(stored)
        if ASSISTANT_OUT_OF_SCOPE_NOTICE.split("{")[0] in notice.text
    ]
    assert len(out_of_scope) == 1
    assert "shot_two" in out_of_scope[0].text and "shot_invented" in out_of_scope[0].text
    assert out_of_scope[0].kind == "refusal"
    # The model was only ever shown the selection, so it had no way to know shot_two exists.
    assert [entry["shot_id"] for entry in director.inputs[0]["shots"]] == ["shot_one"]
    assert comfy.prompts == []


def test_an_asset_that_does_not_exist_is_reported_and_nothing_else_on_that_shot_lands(tmp_path: Path):
    """The matrix's asset row, including its second clause, which is the one with teeth.

    "Nothing else on that shot is applied" is deliberate: an invented id means this answer was not
    written against the library the Director has, so keeping its mode and prompt would leave a shot
    that reads as filled in and cannot be built.
    """
    director = FillingDirector(
        turn(
            {
                "shot_id": "shot_one",
                "mode": "references",
                "prompt": "A grey wolf crosses a wet pine forest",
                "singing": "not_singing",
                "citations": [
                    {"asset_id": "asset_wolf", "role": "reference"},
                    {"asset_id": "asset_hallucinated", "role": "reference"},
                ],
            },
            {"shot_id": "shot_two", "prompt": "An open ridge at dawn"},
        )
    )
    client, store, comfy = make_client(tmp_path, director)
    project = producer_project(store)
    before = store.get(project.id).shots[0].model_dump()

    response = client.post(
        FILL.format(project=project.id),
        json={"message": "fill these in", "shot_ids": ["shot_one", "shot_two"]},
    )

    assert response.status_code == 200
    stored = store.get(project.id)
    # Not the mode, not the prompt, not the performance, not the good half of the citations.
    assert stored.shots[0].model_dump() == before
    # No asset was invented to make the citation valid, either.
    assert [asset.id for asset in stored.assets] == ["asset_wolf", "asset_forest"]
    # And a refusal on one did not silently drop the rest.
    assert stored.shots[1].prompt == "An open ridge at dawn"
    refusal = next(
        notice
        for notice in reply_notices(stored)
        if ASSISTANT_UNKNOWN_ASSET_NOTICE.split("{")[0] in notice.text
    )
    assert "asset_hallucinated" in refusal.text
    assert "SHOT 01 (shot_one)" in refusal.text
    assert comfy.prompts == []


def test_a_citation_that_was_already_dangling_does_not_block_todays_answer(tmp_path: Path):
    """Only the ids *this answer* introduced are refused.

    An asset deleted out from under a shot yesterday is the inspector's report to make. Refusing
    today's fill for it would turn an unrelated stale reference into a permanent block on the shot.
    """
    director = FillingDirector(turn({"shot_id": "shot_one", "prompt": "A grey wolf crosses wet pine"}))
    client, store, _ = make_client(tmp_path, director)
    project = producer_project(store)
    project.shots[0].citations = [AssetCitation(asset_id="asset_deleted", role="reference")]
    store.save(project)

    response = client.post(
        FILL.format(project=project.id),
        json={"message": "write its prompt", "shot_ids": ["shot_one"]},
    )

    assert response.status_code == 200
    stored = store.get(project.id)
    assert stored.shots[0].prompt == "A grey wolf crosses wet pine"
    # The stale citation is untouched, not silently dropped: re-pointing or removing it is the
    # Director's decision.
    assert [item.asset_id for item in stored.shots[0].citations] == ["asset_deleted"]
    assert not any(
        ASSISTANT_UNKNOWN_ASSET_NOTICE.split("{")[0] in notice.text
        for notice in reply_notices(stored)
    )


def test_a_mode_it_cannot_render_is_planned_here_and_refused_at_the_render(tmp_path: Path):
    """The matrix's no-adapter row: allowed, because planning is the point.

    Plannable *and* unrenderable is a deliberate pair. What must never happen is the other failure —
    a mode that looks renderable and is not — and that refusal lives where GPU time would be spent.
    """
    client, store, comfy = make_client(tmp_path)
    project = producer_project(store)
    lead = upload_asset(client, project.id, "Lead", "character", "lead.png")
    director = FillingDirector(
        turn(
            {
                "shot_id": "shot_one",
                "mode": "first_middle_last",
                "prompt": "The lead turns from the window to the door",
                "citations": [
                    {"asset_id": lead["id"], "role": "first"},
                    {"asset_id": lead["id"], "role": "middle"},
                    {"asset_id": lead["id"], "role": "last"},
                ],
            }
        )
    )
    client, store, comfy = make_client(tmp_path, director)
    project = store.get(project.id)
    project.shots[0].status = "draft"
    store.save(project)

    response = client.post(
        FILL.format(project=project.id),
        json={"message": "make it a first/middle/last shot", "shot_ids": ["shot_one"]},
    )

    assert response.status_code == 200
    stored = store.get(project.id)
    assert stored.shots[0].mode == "first_middle_last"
    assert [item.role for item in stored.shots[0].citations] == ["first", "middle", "last"]
    # And the refusal is where it belongs, with nothing sent to ComfyUI.
    stored.shots[0].status = "ready"
    store.save(stored)
    refused = client.post(f"/api/projects/{project.id}/shots/shot_one/generate/h3", json={})
    assert refused.status_code == 422
    assert "First / middle / last" in refused.json()["detail"]
    assert comfy.prompts == []


def test_a_shot_left_unspecified_for_its_mode_is_flagged_and_still_applied(tmp_path: Path):
    """Reported, never repaired, and never a refusal here — `mode_specification_problems`' rule.

    Inventing which of two images is the middle one is exactly the guess a role exists to stop, and
    refusing the whole fill would throw away a prompt the Director can use.
    """
    director = FillingDirector(
        turn(
            {
                "shot_id": "shot_one",
                "mode": "first_middle_last",
                "prompt": "The lead turns from the window to the door",
                "citations": [{"asset_id": "asset_wolf", "role": "first"}],
            }
        )
    )
    client, store, comfy = make_client(tmp_path, director)
    project = producer_project(store)

    response = client.post(
        FILL.format(project=project.id),
        json={"message": "make it a first/middle/last shot", "shot_ids": ["shot_one"]},
    )

    assert response.status_code == 200
    stored = store.get(project.id)
    assert stored.shots[0].mode == "first_middle_last"
    flag = next(
        notice
        for notice in reply_notices(stored)
        if ASSISTANT_SPECIFICATION_NOTICE.split("{")[0] in notice.text
    )
    assert flag.kind == "flag"
    # The server's own sentences, so this reads exactly as the shot inspector reads.
    assert "First / middle / last needs 1 middle frame, and this shot cites 0." in flag.text
    assert comfy.prompts == []


@pytest.mark.parametrize("prompt", ["", "   ", PLACEHOLDER_PROMPT, '{"prompt": "a wolf"}'])
def test_a_prompt_the_gate_refuses_takes_the_whole_answer_for_that_shot_with_it(
    tmp_path: Path, prompt: str
):
    """The prompt gate, met through `batch.prompt_rejection` rather than reimplemented.

    All-or-nothing per shot, because applying the mode and the citations from an answer whose
    prompt was refused leaves a shot that reads as filled in and cannot be rendered. The placeholder
    is in this list on purpose: a model echoing back the `current_prompt` it was shown is ordinary
    local-model behaviour, and it would otherwise be stored as a real prompt and then blocked at the
    queue for having none.
    """
    assert assistant_prompt_rejection(prompt)
    director = FillingDirector(
        turn(
            {
                "shot_id": "shot_one",
                "mode": "references",
                "prompt": prompt,
                "citations": [{"asset_id": "asset_wolf", "role": "reference"}],
            },
            {"shot_id": "shot_two", "prompt": "An open ridge at dawn"},
        )
    )
    client, store, comfy = make_client(tmp_path, director)
    project = producer_project(store)
    before = store.get(project.id).shots[0].model_dump()

    response = client.post(
        FILL.format(project=project.id),
        json={"message": "fill these in", "shot_ids": ["shot_one", "shot_two"]},
    )

    assert response.status_code == 200
    stored = store.get(project.id)
    assert stored.shots[0].model_dump() == before
    assert stored.shots[1].prompt == "An open ridge at dawn"
    rejection = next(
        notice for notice in reply_notices(stored) if notice.text.startswith("NOT applied to")
    )
    assert "SHOT 01 (shot_one)" in rejection.text
    # The refused text travels in `raw`, which the next Director call's context strips.
    assert rejection.raw == (prompt if prompt.strip() else "")
    assert comfy.prompts == []


def test_a_bulk_fill_judges_every_shot_individually_and_names_every_one(tmp_path: Path):
    """The matrix's "Many shots at once" and "A tool call that fails" rows, together.

    Seven shots, seven different outcomes in one turn. Two things are asserted that no single-shot
    test can reach: a refusal on one shot did not drop the good ones beside it, and **every shot
    the Director selected is named in the reply** — applied, refused, discarded, omitted or empty.
    Silence about a shot the Director explicitly picked is the failure this feature may not have.
    """
    director = FillingDirector(
        turn(
            {"shot_id": "shot_ok", "mode": "text_to_video", "prompt": "A ridge line at first light"},
            {"shot_id": "shot_locked", "prompt": "Never written"},
            {"shot_id": "shot_rendered", "prompt": "Never written"},
            {"shot_id": "shot_badasset", "citations": [{"asset_id": "asset_nope", "role": "reference"}]},
            {"shot_id": "shot_badprompt", "prompt": PLACEHOLDER_PROMPT},
            {"shot_id": "shot_empty"},
            {"shot_id": "shot_ok", "prompt": "A contradictory second answer"},
            malformed=('{"shot_id": "shot_omitted", "mode": "b_roll"}',),
        )
    )
    client, store, comfy = make_client(tmp_path, director)
    project = store.create(Project(name="Bulk"))
    project.assets = [Asset(id="asset_wolf", name="Grey wolf", kind="character", path="media/wolf.png")]
    project.shots = [
        Shot(id="shot_ok", start=0, duration=5, prompt=PLACEHOLDER_PROMPT),
        Shot(id="shot_locked", start=5, duration=5, prompt=PLACEHOLDER_PROMPT, locked=True),
        Shot(id="shot_rendered", start=10, duration=5, prompt="Already shot", latest_output="out/a_00001.mp4"),
        Shot(id="shot_badasset", start=15, duration=5, prompt=PLACEHOLDER_PROMPT),
        Shot(id="shot_badprompt", start=20, duration=5, prompt=PLACEHOLDER_PROMPT),
        Shot(id="shot_empty", start=25, duration=5, prompt=PLACEHOLDER_PROMPT),
        Shot(id="shot_omitted", start=30, duration=5, prompt=PLACEHOLDER_PROMPT),
    ]
    store.save(project)
    selection = [shot.id for shot in project.shots] + ["shot_deleted"]
    before = {shot.id: shot.model_dump() for shot in store.get(project.id).shots}

    response = client.post(
        FILL.format(project=project.id),
        json={"message": "fill in the whole plan", "shot_ids": selection},
    )

    assert response.status_code == 200
    stored = store.get(project.id)
    by_id = {shot.id: shot for shot in stored.shots}
    # Exactly one shot changed, and it is the one whose answer survived every guard.
    assert by_id["shot_ok"].prompt == "A ridge line at first light"
    assert by_id["shot_ok"].mode == "text_to_video"
    for shot_id in ("shot_locked", "shot_rendered", "shot_badasset", "shot_badprompt", "shot_empty", "shot_omitted"):
        assert by_id[shot_id].model_dump() == before[shot_id], shot_id

    # Every selected shot is named, under the name the timeline draws.
    reply = reply_text(stored)
    for shot in stored.shots:
        assert shot_label(stored, shot) in reply, shot.id
    assert "shot_deleted" in reply
    kinds = {notice.kind for notice in reply_notices(stored)}
    assert kinds == {"change", "refusal", "flag"}
    for wording in (
        ASSISTANT_OMITTED_NOTICE,
        ASSISTANT_EMPTY_FILL_NOTICE,
        ASSISTANT_DUPLICATE_NOTICE,
        ASSISTANT_MISSING_TARGET_NOTICE,
        ASSISTANT_MALFORMED_NOTICE,
    ):
        assert any(wording.split("{")[0] in notice.text for notice in reply_notices(stored)), wording
    # First answer wins on a duplicate, so the contradiction is reported rather than applied.
    assert "A contradictory second answer" not in reply
    assert comfy.prompts == []
    assert stored.jobs == []


def test_a_failure_part_way_through_a_bulk_fill_leaves_nothing_half_applied(tmp_path: Path):
    """One terminal save, so "nothing half-applied" is structural rather than a promise.

    The first shot's answer is good and is judged and built; the second raises while it is being
    read. The good one is *not* on the manifest afterwards, and the mechanism is the single
    `store.save` at the end of the route: saving as it went would leave one shot filled in, no
    reply naming it, and a Director looking at a 500. Mutation-checked in exactly that direction —
    a `store.save(project)` moved inside the judging loop fails this test.

    The two-phase build (stage every candidate, commit them together) is the second half of the
    same guarantee and is deliberately *not* what this test proves: with one terminal save, an
    in-loop commit to the in-memory project is still never persisted. It is there so the in-memory
    object a later reader sees is never half-written either.

    The failure is injected as a fill that raises when its `prompt` is read, because that is the
    only honest way to reach mid-loop: everything the model can send is validated at the edge.
    """

    class ExplodingFill:
        """A tool call that dies half-way through being read. Not a shape a model can send."""

        shot_id = "shot_two"
        mode = None
        singing = None
        citations = None

        @property
        def prompt(self):
            raise RuntimeError("boom")

    answer = turn({"shot_id": "shot_one", "prompt": "A ridge line at dawn"})
    # Appended rather than passed to the constructor: this is deliberately not a `ShotFill`, so it
    # has to get past validation the way only an internal fault could.
    answer.fills.append(ExplodingFill())
    client, store, comfy = make_client(tmp_path, FillingDirector(answer))
    project = producer_project(store)
    before = store.get(project.id).model_dump()

    with pytest.raises(RuntimeError, match="boom"):
        client.post(
            FILL.format(project=project.id),
            json={"message": "fill these in", "shot_ids": ["shot_one", "shot_two"]},
        )

    # Not the good shot, not the thread, not `updated_at` — and certainly not a render.
    after = store.get(project.id).model_dump()
    assert after == before
    assert after["shots"][0]["prompt"] == PLACEHOLDER_PROMPT
    assert comfy.prompts == []


def test_the_assistant_never_infers_whether_the_performer_is_singing(tmp_path: Path):
    """The one field nothing in this codebase may guess at.

    Setting it is a *visible act* — reported in the applied notice — and that is what makes it
    different from the model deciding on the Director's behalf. A tool call that omits it leaves
    whatever the shot already says, which for an unset shot is `unknown` and is deliberately not
    "not singing": the LTX enhancer measurably moves lip position, so a wrong value in either
    direction costs the Director something real.
    """
    director = FillingDirector(
        turn(
            {"shot_id": "shot_one", "mode": "references", "prompt": "The lead sings into a wet lens"},
            {"shot_id": "shot_two", "singing": "singing", "prompt": "The lead sings the chorus"},
        )
    )
    client, store, _ = make_client(tmp_path, director)
    project = producer_project(store)

    response = client.post(
        FILL.format(project=project.id),
        json={"message": "fill these in", "shot_ids": ["shot_one", "shot_two"]},
    )

    assert response.status_code == 200
    stored = store.get(project.id)
    # A prompt full of singing, a references mode, a lead vocalist in the library — and the field is
    # still untouched, because nothing derives it from any of those.
    assert stored.shots[0].singing == "unknown"
    # Set only because the tool call carried it, and said out loud in the reply.
    assert stored.shots[1].singing == "singing"
    assert "performance recorded as singing" in reply_text(stored)

    # And the source is still free of the inference the existing guard greps for, including the two
    # files this feature added.
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in Path("src/music_video_producer").rglob("*.py")
    )
    for guess in ('singing = "singing"', 'singing = "not_singing"', 'singing="singing"'):
        assert guess not in source, guess


def test_the_assistant_spends_no_gpu_time_on_any_path(tmp_path: Path):
    """The frozen block's Ask First, asserted over every outcome this route has.

    Not a render, not an image, not a promotion — and not a status change either, because a shot
    silently reaching `ready` is a shot the queue would submit without anyone deciding to.
    """
    outcomes = (
        turn({"shot_id": "shot_one", "mode": "text_to_video", "prompt": "A ridge line at dawn"}),
        turn({"shot_id": "shot_one", "prompt": PLACEHOLDER_PROMPT}),
        turn({"shot_id": "shot_one", "citations": [{"asset_id": "asset_nope"}]}),
        turn({"shot_id": "shot_elsewhere", "prompt": "Out of scope"}),
        turn({"shot_id": "shot_one"}),
        turn(message="I would make it a wolf shot.", malformed=('{"mode": "b_roll"}',)),
        turn(message=""),
    )
    for answer in outcomes:
        director = FillingDirector(answer)
        client, store, comfy = make_client(tmp_path / str(id(answer)), director)
        project = producer_project(store)
        response = client.post(
            FILL.format(project=project.id),
            json={"message": "fill it in", "shot_ids": ["shot_one"]},
        )
        assert response.status_code == 200, response.text
        stored = store.get(project.id)
        assert comfy.prompts == [], answer
        assert comfy.uploads == [], answer
        assert stored.jobs == [], answer
        assert [shot.status for shot in stored.shots] == ["draft", "draft"], answer
        assert [shot.approved_output for shot in stored.shots] == ["", ""], answer
        assert [shot.prompt_id for shot in stored.shots] == ["", ""], answer
        # And nothing outside the shots: no Song written, no asset created, no document replaced.
        assert stored.song.title == "Signal Bloom"
        assert [asset.id for asset in stored.assets] == ["asset_wolf", "asset_forest"]
        assert stored.treatment == "Three movements: the corridor, the threshold, the forest."
        assert stored.style_bible == "Sodium amber, hard backlight, 35mm grain."


def test_a_refused_tool_call_is_reported_and_kept_out_of_the_next_directors_context(tmp_path: Path):
    """The notice contract, applied to the shape this feature adds.

    The refused arguments are the degraded output the guard exists to keep out of the next prompt,
    so they travel in the notice's `raw` — which `DIRECTOR_CONTEXT_EXCLUDE` strips whole — and never
    in `content`, which `director_chat` ships straight back to the model.
    """
    director = FillingDirector(
        turn(message="", malformed=('{"shot_id": "shot_one", "mode": "b_roll_wide"}',))
    )
    client, store, _ = make_client(tmp_path, director)
    project = producer_project(store)

    response = client.post(
        FILL.format(project=project.id),
        json={"message": "fill it in", "shot_ids": ["shot_one"]},
    )

    assert response.status_code == 200
    stored = store.get(project.id)
    malformed = next(
        notice
        for notice in reply_notices(stored)
        if ASSISTANT_MALFORMED_NOTICE.split("{")[0] in notice.text
    )
    assert malformed.kind == "refusal"
    assert "b_roll_wide" in malformed.raw
    assert "b_roll_wide" not in reply_text(stored)
    # And it is out of the dump the next chat turn is handed.
    context = stored.model_dump(mode="json", exclude=DIRECTOR_CONTEXT_EXCLUDE)
    assert "b_roll_wide" not in json.dumps(context)
    # The model returned no sentence of its own, so the reply is not a bare separator.
    assert reply_text(stored).startswith(ASSISTANT_EMPTY_MESSAGE)


def test_a_reply_with_no_tool_call_says_so_rather_than_changing_nothing_silently(tmp_path: Path):
    director = FillingDirector(turn(message="I would make it a wolf shot."))
    client, store, _ = make_client(tmp_path, director)
    project = producer_project(store)

    response = client.post(
        FILL.format(project=project.id),
        json={"message": "fill it in", "shot_ids": ["shot_one"]},
    )

    assert response.status_code == 200
    stored = store.get(project.id)
    assert stored.shots[0].prompt == PLACEHOLDER_PROMPT
    assert any(
        notice.text == ASSISTANT_WITHOUT_TOOL_CALL_NOTICE for notice in reply_notices(stored)
    )
    assert reply_text(stored).startswith("I would make it a wolf shot.")


def test_shots_deleted_while_the_model_was_thinking_are_reported_not_written(tmp_path: Path):
    """The re-read after the await, and the one case it is the whole guard for."""

    class DeletingDirector(FillingDirector):
        def __init__(self, store, answer):
            super().__init__(answer)
            self.store = store

        async def assist(self, *, message, assistant_input):
            answer = await super().assist(message=message, assistant_input=assistant_input)
            project = self.store.get(self.project_id)
            project.shots = [shot for shot in project.shots if shot.id != "shot_two"]
            self.store.save(project)
            return answer

    client, store, comfy = make_client(tmp_path)
    director = DeletingDirector(
        store,
        turn(
            {"shot_id": "shot_one", "prompt": "A ridge line at dawn"},
            {"shot_id": "shot_two", "prompt": "Written to a shot that is gone"},
        ),
    )
    client, store, comfy = make_client(tmp_path, director)
    director.store = store
    project = producer_project(store)
    director.project_id = project.id

    response = client.post(
        FILL.format(project=project.id),
        json={"message": "fill these in", "shot_ids": ["shot_one", "shot_two"]},
    )

    assert response.status_code == 200
    stored = store.get(project.id)
    assert [shot.id for shot in stored.shots] == ["shot_one"]
    assert stored.shots[0].prompt == "A ridge line at dawn"
    assert any(
        ASSISTANT_MISSING_TARGET_NOTICE.split("{")[0] in notice.text
        for notice in reply_notices(stored)
    )
    assert comfy.prompts == []


def test_a_selection_naming_nothing_this_project_has_is_refused_before_any_call(tmp_path: Path):
    director = FillingDirector(turn({"shot_id": "shot_one", "prompt": "Never written"}))
    client, store, comfy = make_client(tmp_path, director)
    project = producer_project(store)

    response = client.post(
        FILL.format(project=project.id),
        json={"message": "fill it in", "shot_ids": ["shot_gone"]},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == ASSISTANT_WITHOUT_SHOTS
    assert director.inputs == []
    assert comfy.prompts == []


def test_no_language_model_makes_the_assistant_unavailable_rather_than_broken(tmp_path: Path):
    """The matrix's last row. 503 for unconfigured, 502 for unusable, and nothing written either way."""
    for error, expected in (
        (DirectorUnavailable("LLM director is not configured."), 503),
        (DirectorError("LLM director returned an invalid response: Expecting value"), 502),
    ):
        director = FillingDirector(error=error)
        client, store, comfy = make_client(tmp_path / str(expected), director)
        project = producer_project(store)
        before = store.get(project.id).model_dump()

        response = client.post(
            FILL.format(project=project.id),
            json={"message": "fill it in", "shot_ids": ["shot_one"]},
        )

        assert response.status_code == expected
        assert store.get(project.id).model_dump() == before
        assert comfy.prompts == []


def test_the_request_cannot_mean_every_shot_by_omission():
    """`shot_ids` is required and non-empty, which is what makes the selection the consent.

    A defaulted empty list would make "fill in the shots" a request whose scope nobody stated, and
    the natural reading of a scope nobody stated is "all of them".
    """
    with pytest.raises(ValidationError):
        AssistantRequest(message="fill it in")
    with pytest.raises(ValidationError):
        AssistantRequest(message="fill it in", shot_ids=[])
    with pytest.raises(ValidationError):
        AssistantRequest(message="", shot_ids=["shot_one"])
    assert AssistantRequest(message="fill it in", shot_ids=["shot_one"]).shot_ids == ["shot_one"]


def test_both_automated_writers_refuse_exactly_the_same_shots(tmp_path: Path):
    """One decision, two callers. A divergence here is a guard hole by construction.

    The assistant is the *wider* capability — it sets modes and citations, not only prompts — so a
    second copy of this rule would show up as the assistant writing to a shot expansion refuses.
    """
    cases = [
        (Shot(start=0, duration=5), None),
        (Shot(start=0, duration=5, locked=True), "locked"),
        (Shot(start=0, duration=5, status="ready"), "rendered"),
        (Shot(start=0, duration=5, status="queued"), "rendered"),
        (Shot(start=0, duration=5, prompt_id="p-1"), "rendered"),
        (Shot(start=0, duration=5, latest_output="out/a.mp4"), "rendered"),
        (Shot(start=0, duration=5, approved_output="out/a.mp4"), "rendered"),
        (Shot(start=0, duration=5, locked=True, latest_output="out/a.mp4"), "locked"),
    ]
    for shot, expected in cases:
        assert shot_write_refusal(shot) == expected, shot
        # Composed from exactly the two facts expansion reads, in the precedence both routes report
        # by: a lock is a decision the Director made and provenance is a fact about media, so when
        # both apply the lock is the sentence worth reading.
        assert shot_write_refusal(shot) == (
            "locked" if shot.locked else "rendered" if shot_render_provenance(shot) else None
        ), shot
    # The last case is the precedence itself: locked *and* rendered reports the lock.
    assert cases[-1][0].locked and shot_render_provenance(cases[-1][0])


# ---------------------------------------------------------------------------------------------
# The system prompt, which is a deliverable
# ---------------------------------------------------------------------------------------------


def test_the_system_prompt_lives_where_it_can_be_iterated_and_describes_the_payload_it_is_sent():
    """The Director's bet is on the persona, so the prompt is a deliverable rather than a constant.

    Two things are asserted. It lives in its own module with no interpolation to keep in step, so
    changing it is a one-file edit that touches no transport. And every key `assistant_input` builds
    is *named* in it — a payload whose semantics the model has to infer is a payload whose quality
    is hoped for rather than requested, which is the standard `EXPANSION_SYSTEM_PROMPT` is already
    held to.
    """
    module = Path("src/music_video_producer/assistant_prompt.py").read_text(encoding="utf-8")
    assert "ASSISTANT_SYSTEM_PROMPT" in module
    # The craft rules are addressable on their own, because they are the half most likely to be
    # rewritten between two live runs.
    assert PROMPT_CRAFT in ASSISTANT_SYSTEM_PROMPT

    project = Project(name="Keys")
    project.song = Song(title="S", source="imported", duration=30, lyrics="words", caption="sound")
    project.assets = [Asset(id="asset_a", name="A", kind="character", path="p")]
    project.shots = [Shot(id="shot_a", start=0, duration=5, prompt=PLACEHOLDER_PROMPT)]
    payload = assistant_input(project, shot_ids=["shot_a"])
    for key in payload:
        assert key in ASSISTANT_SYSTEM_PROMPT, key
    for key in payload["shots"][0]:
        assert key in ASSISTANT_SYSTEM_PROMPT, key
    for key in payload["modes"][0]:
        assert key in ASSISTANT_SYSTEM_PROMPT, key

    # It states what it cannot do, in the frozen block's own vocabulary, so the model does not
    # claim any of them happened.
    for forbidden in ("render", "generate an image", "approve", "mark a shot ready", "delete a shot"):
        assert forbidden in ASSISTANT_SYSTEM_PROMPT, forbidden
    # And it says the only way to change anything is the tool, which is the failure the no-tool
    # notice exists to report.
    assert FILL_SHOTS_TOOL in ASSISTANT_SYSTEM_PROMPT


def test_the_prompt_does_not_pre_empt_the_literalism_it_is_meant_to_be_watched_for():
    """A deliberate absence, recorded so it is not "fixed" by someone who has not read the note.

    The Design Notes are explicit: a model handed a lyric sheet may transcribe lines into prompts,
    as it did for expansion, and that is to be watched on real output rather than pre-empted. The
    sheet is therefore sent and the prompt says nothing about it. The module docstring is where the
    fix goes if a live run shows it is needed.
    """
    assert "lyric" not in ASSISTANT_SYSTEM_PROMPT.lower().split("plan carries")[0]
    module = Path("src/music_video_producer/assistant_prompt.py").read_text(encoding="utf-8")
    assert "watch" in module.lower() or "live run" in module.lower()


# ---------------------------------------------------------------------------------------------
# The composer controls, executed
# ---------------------------------------------------------------------------------------------


def test_the_prefill_control_is_shut_with_nothing_selected_and_writes_the_composer_with_one():
    """The matrix's two prefill rows, *executed* against the workspace rather than read.

    "The control is absent or shut, not a silent no-op" is a claim about a rendered control, and a
    source read cannot tell a control that is shut from one whose handler happens to return early.
    So `app.js` is booted against the stub DOM, the render is called, and the button is read.
    """
    result = run_workspace("""
      state.project = {
        id: 'project_1', name: 'P', assets: [{ id: 'asset_wolf', name: 'Grey wolf', kind: 'character' }],
        shots: [
          { id: 'shot_one', start: 12, duration: 5, prompt: 'New shot', mode: null, citations: [], status: 'draft' },
          { id: 'shot_two', start: 17, duration: 5, prompt: 'A ridge line', mode: 'references',
            citations: [{ asset_id: 'asset_wolf', role: 'first', order: 0 }], status: 'draft' },
        ],
      };
      state.selectedShotId = null;
      app.syncAssistantControls();
      const shut = {
        prefill: at('#prefill-shot').disabled, prefillTitle: at('#prefill-shot').title,
        fill: at('#assistant-fill').disabled, fillTitle: at('#assistant-fill').title,
        bulk: at('#assistant-fill-all').disabled,
      };
      state.selectedShotId = 'shot_one';
      app.syncAssistantControls();
      const open = { prefill: at('#prefill-shot').disabled, fill: at('#assistant-fill').disabled };
      requests.length = 0;
      at('#chat-form').elements.message.value = '';
      fire('#prefill-shot:click', {});
      const composed = at('#chat-form').elements.message.value;
      // A Director who typed first keeps their sentence: the context is the preamble to it.
      at('#chat-form').elements.message.value = 'make it a wolf B-roll';
      fire('#prefill-shot:click', {});
      const kept = at('#chat-form').elements.message.value;
      state.selectedShotId = 'shot_two';
      app.syncAssistantControls();
      fire('#prefill-shot:click', {});
      const second = at('#chat-form').elements.message.value;
      console.log(JSON.stringify({ shut, open, composed, kept, second, sent: requests.length }));
    """)

    assert result["shut"]["prefill"] is True
    assert result["shut"]["fill"] is True
    assert "Select a shot" in result["shut"]["prefillTitle"]
    assert "Select a shot" in result["shut"]["fillTitle"]
    # No shots are writable in a project with none selected? They are -- the bulk control is scoped
    # to the plan rather than to the selection, so it is the one control a bare load can offer.
    assert result["shut"]["bulk"] is False
    assert result["open"]["prefill"] is False
    assert result["open"]["fill"] is False

    # The composer really was filled, with the shot's context, as text a Director could have typed.
    assert result["composed"].startswith("SHOT 01 (shot_one) runs from 12s to 17s, 5s long.")
    assert "text to video shot and cites no assets" in result["composed"]
    # The placeholder is not a prompt anyone wrote, and is not reported as one.
    assert "It has no prompt yet." in result["composed"]
    assert result["composed"].endswith("Make it ")
    assert result["kept"].endswith("make it a wolf B-roll")
    # A cited shot names its assets by name and by role, in the Director's own vocabulary.
    assert "references to video shot, citing Grey wolf as its first frame" in result["second"]
    assert "Its prompt reads: A ridge line" in result["second"]
    # Prefill is a convenience, not a channel: nothing was sent.
    assert result["sent"] == 0


def test_the_fill_control_sends_the_selection_and_nothing_else():
    """The scope the server enforces, decided in the browser by the control that was pressed.

    Also the two states it must refuse before the click: a locked shot and one carrying a take.
    """
    project = {
        "id": "project_1",
        "name": "P",
        "assets": [],
        "messages": [
            {
                "id": "m1",
                "role": "assistant",
                "content": "Done.\n\n---\nAssistant ProducerBot filled in 2 shot(s):\nSHOT 01",
                "notices": [],
            }
        ],
        "shots": [
            {"id": "shot_one", "start": 0, "duration": 5, "prompt": "New shot", "status": "draft", "citations": []},
            {"id": "shot_locked", "start": 5, "duration": 5, "prompt": "x", "status": "draft", "locked": True, "citations": []},
            {"id": "shot_done", "start": 10, "duration": 5, "prompt": "x", "status": "complete", "citations": []},
        ],
        "jobs": [],
    }
    result = run_workspace(
        """
      state.health = { llm: { configured: true }, comfy: { online: true, url: 'http://c' } };
      state.project = JSON.parse(JSON.stringify(__PROJECT__));
      state.selectedShotId = 'shot_one';
      app.syncAssistantControls();
      const single = { disabled: at('#assistant-fill').disabled };
      requests.length = 0;
      await fire('#assistant-fill:click', { currentTarget: at('#assistant-fill') });
      const sentNothing = requests.length;
      at('#chat-form').elements.message.value = 'make it a wolf B-roll';
      await fire('#assistant-fill:click', { currentTarget: at('#assistant-fill') });
      const one = requests.map((entry) => ({ path: entry.path, method: entry.method, body: entry.body }));
      requests.length = 0;
      at('#chat-form').elements.message.value = 'fill in the plan';
      await fire('#assistant-fill-all:click', { currentTarget: at('#assistant-fill-all') });
      const bulk = requests.map((entry) => ({ path: entry.path, body: entry.body }));
      state.selectedShotId = 'shot_locked';
      app.syncAssistantControls();
      const locked = { disabled: at('#assistant-fill').disabled, title: at('#assistant-fill').title };
      state.selectedShotId = 'shot_done';
      app.syncAssistantControls();
      const done = { disabled: at('#assistant-fill').disabled, title: at('#assistant-fill').title };
      console.log(JSON.stringify({ single, sentNothing, one, bulk, locked, done }));
    """.replace("__PROJECT__", json.dumps(project)),
        {"/api/projects/project_1/assistant/fill": {"body": project}},
    )

    assert result["single"]["disabled"] is False
    # An empty composer is refused in the browser rather than sent as a 422 about a hidden field.
    assert result["sentNothing"] == 0
    sent = [entry for entry in result["one"] if "assistant/fill" in entry["path"]]
    assert len(sent) == 1
    assert sent[0]["method"] == "POST"
    assert json.loads(sent[0]["body"]) == {
        "message": "make it a wolf B-roll",
        "shot_ids": ["shot_one"],
    }
    # The bulk control sends exactly the shots the server would accept, so a plan with a locked and
    # a rendered shot in it does not spend a model call learning that.
    bulk = [entry for entry in result["bulk"] if "assistant/fill" in entry["path"]]
    assert json.loads(bulk[0]["body"])["shot_ids"] == ["shot_one"]
    # Both refusals are pre-empted, as a disabled control carrying the reason.
    assert result["locked"]["disabled"] is True
    assert "locked" in result["locked"]["title"]
    assert result["done"]["disabled"] is True
    assert "take" in result["done"]["title"]


def test_the_browser_and_the_route_agree_about_which_shots_may_be_written_to(tmp_path: Path):
    """`shotWriteRefusal` executed against `shot_write_refusal`, over the same shots.

    A drifted mirror has two failure modes and both are invisible from either side alone: a control
    offered for a shot the route refuses, and a shot the route would accept with no control for it.
    """
    shots = [
        {"id": "a", "status": "draft"},
        {"id": "b", "status": "draft", "locked": True},
        {"id": "c", "status": "ready"},
        {"id": "d", "status": "queued"},
        {"id": "e", "status": "complete"},
        {"id": "f", "status": "approved"},
        {"id": "g", "status": "draft", "prompt_id": "p-1"},
        {"id": "h", "status": "draft", "latest_output": "out/a.mp4"},
        {"id": "i", "status": "draft", "approved_output": "out/a.mp4"},
        {"id": "j", "status": "error"},
    ]
    browser = run_module(
        """
      import { shotWriteRefusal } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify(__SHOTS__.map((shot) => shotWriteRefusal(shot))));
    """.replace("__SHOTS__", json.dumps(shots))
    )
    server = [
        shot_write_refusal(Shot(start=0, duration=5, **{k: v for k, v in shot.items() if k != "id"}))
        for shot in shots
    ]
    assert browser == server


def test_the_composer_carries_the_three_assistant_controls_under_the_shared_wordings():
    """The markup cannot import a constant, so the three labels and the claim are asserted here.

    The "Nothing is rendered" claim is the one spelling every assistant and expansion control makes:
    a Director deciding whether an assistant button spends GPU minutes must not read two sentences.
    """
    markup = INDEX_HTML.read_text(encoding="utf-8")
    row = re.search(r'<div class="assistant-row">.*?</div>', markup, re.DOTALL)
    assert row, "the composer no longer carries the assistant controls"
    constants = run_module("""
      import { ASSISTANT_FILL_ALL_CONTROL, ASSISTANT_FILL_ALL_LABEL, ASSISTANT_FILL_CONTROL,
               ASSISTANT_FILL_LABEL, ASSISTANT_PREFILL_CONTROL, ASSISTANT_PREFILL_LABEL,
               ASSISTANT_WITHOUT_SHOT, ASSISTANT_WITHOUT_WRITABLE_SHOTS, SHOT_EXPANSION_NO_RENDER }
        from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        prefill: ASSISTANT_PREFILL_CONTROL, prefillLabel: ASSISTANT_PREFILL_LABEL,
        fill: ASSISTANT_FILL_CONTROL, fillLabel: ASSISTANT_FILL_LABEL,
        bulk: ASSISTANT_FILL_ALL_CONTROL, bulkLabel: ASSISTANT_FILL_ALL_LABEL,
        withoutShot: ASSISTANT_WITHOUT_SHOT, withoutWritable: ASSISTANT_WITHOUT_WRITABLE_SHOTS,
        noRender: SHOT_EXPANSION_NO_RENDER,
      }));
    """)
    body = row.group(0)
    for selector, label in (
        ("prefill", "prefillLabel"),
        ("fill", "fillLabel"),
        ("bulk", "bulkLabel"),
    ):
        assert f'id="{constants[selector].lstrip("#")}"' in body, constants[selector]
        assert f">{constants[label]}</button>" in body, constants[label]
    # Shipped disabled, with the reason, so a first paint before any render is shut rather than a
    # button whose only outcome is a refusal.
    assert body.count("disabled") == 3
    assert constants["withoutShot"] in body
    assert constants["withoutWritable"] in body
    assert constants["noRender"] in body
    # And none of them is the chat form's submit: the Director send and the assistant send are
    # deliberately different acts.
    assert body.count('type="button"') == 3
    assert "submit" not in body


def test_the_toast_reads_its_count_out_of_a_real_server_notice(tmp_path: Path):
    """The count the Director reads is the count the reply carries, by construction.

    A diff cannot tell a re-fill that landed the same mode and the same citations from a turn where
    every call was refused, and the toast is the loudest thing on screen.
    """
    director = FillingDirector(
        turn(
            {"shot_id": "shot_one", "prompt": "A ridge line at dawn"},
            {"shot_id": "shot_two", "prompt": "A wolf at the treeline"},
        )
    )
    client, store, _ = make_client(tmp_path, director)
    project = producer_project(store)
    response = client.post(
        FILL.format(project=project.id),
        json={"message": "fill these in", "shot_ids": ["shot_one", "shot_two"]},
    )
    assert response.status_code == 200

    toasts = run_module(
        """
      import { assistantToast } from './src/music_video_producer/web/assets/api.js';
      console.log(JSON.stringify({
        filled: assistantToast(__PROJECT__),
        unchanged: assistantToast({ messages: [{ role: 'assistant', content: 'Nothing landed.' }] }),
      }));
    """.replace("__PROJECT__", json.dumps(response.json()))
    )

    assert toasts["filled"].startswith("Assistant ProducerBot filled in 2 shots.")
    assert "Nothing is rendered" in toasts["filled"]
    assert "No shot was changed" in toasts["unchanged"]
    # The marker really is a substring of the server's own applied notice, so the two cannot drift.
    assert ASSISTANT_APPLIED_NOTICE.startswith(
        run_module("""
          import { ASSISTANT_APPLIED_MARKER } from './src/music_video_producer/web/assets/api.js';
          console.log(JSON.stringify(ASSISTANT_APPLIED_MARKER));
        """)
    )


def test_the_assistant_route_is_reachable_from_exactly_one_place_in_the_client():
    """One call site, so the scope decision cannot be made twice and differently."""
    source = API_JS.read_text(encoding="utf-8")
    assert source.count("assistant/fill") == 1
    app_source = Path("src/music_video_producer/web/assets/app.js").read_text(encoding="utf-8")
    assert app_source.count("api.assistantFill(") == 1


# ---------------------------------------------------------------------------------------------
# The expansion tool: a conversational request reaching the specialist
# ---------------------------------------------------------------------------------------------

GOOD_H3 = (
    "integrated_multimodal_description: [Shot 1] A grey wolf crosses the clearing under low "
    "amber light; the camera drifts with it, handheld.\n"
    "overall_soundscape: Dry needles compress underfoot. Wind moves through the branches.\n"
    "non_diegetic_music: A low cello figure at a slow tempo, swelling once and receding."
)


def expanding_turn(*shot_ids: str, message: str = "Expanding those.", **kwargs) -> AssistantTurn:
    """One assistant answer that calls `expand_prompts` for the named shots."""
    return AssistantTurn(
        message=message,
        expansions=[ShotExpansionRequest(shot_id=shot_id) for shot_id in shot_ids],
        **kwargs,
    )


class ExpandingProducerDirector(FillingDirector):
    """`FillingDirector` that can also answer the specialist's call, recording every one."""

    def __init__(self, answer=None, *, answers: dict | None = None, error=None):
        super().__init__(answer, error=error)
        self.answers = answers or {}
        self.expansions: list[dict] = []

    async def expand_shot(self, *, shot_input, system_prompt, **_):
        self.expansions.append(shot_input)
        return self.answers.get(shot_input["shot"]["id"], GOOD_H3)


def test_the_expansion_tool_is_typed_and_offered_beside_the_fill_tool():
    """Two tools, both generated rather than hand-written, and the second one refusing to carry
    anything it has no business setting.

    `ShotFill` is typed to the shot vocabulary because a mode and a role are things a model can get
    wrong in words. An expansion has no vocabulary at all -- everything it needs is on the shot --
    so the only thing there is to validate is that a shot was named, and the schema says exactly
    that and nothing more. A tool that also took, say, a mode would be a second way to write a
    field the fill tool already owns, through a route that never validates it as a whole Shot.
    """
    tools = {tool["function"]["name"]: tool["function"] for tool in assistant_tools()}

    assert set(tools) == {FILL_SHOTS_TOOL, EXPAND_PROMPTS_TOOL}
    parameters = tools[EXPAND_PROMPTS_TOOL]["parameters"]
    entry = parameters["$defs"]["ShotExpansionRequest"]
    assert list(entry["properties"]) == ["shot_id"]
    assert entry["required"] == ["shot_id"]
    assert list(parameters["properties"]) == ["shots"]
    # The maintainer-facing docstrings are stripped here too, for `_model_facing_schema`'s reason.
    assert "description" not in parameters
    assert "description" not in entry
    # And the wire description is the one beside the persona, so the two agree about what calling
    # it means.
    assert tools[EXPAND_PROMPTS_TOOL]["description"] == EXPAND_PROMPTS_DESCRIPTION


def test_an_expansion_call_that_does_not_fit_the_vocabulary_is_a_refusal_not_a_guess():
    """The typed surface's whole purpose, applied to the second tool: a bad entry is discarded and
    reported, and the good entries beside it survive."""
    parsed = parse_assistant_reply({
        "content": "Expanding.",
        "tool_calls": [
            {"function": {"name": EXPAND_PROMPTS_TOOL, "arguments": json.dumps({"shots": [
                {"shot_id": "shot_one"},
                {"shot_id": ""},
                {"nothing": "useful"},
                {"shot_id": "shot_two"},
            ]})}},
        ],
    })

    assert [item.shot_id for item in parsed.expansions] == ["shot_one", "shot_two"]
    assert len(parsed.malformed) == 2
    assert parsed.fills == []


def test_both_tools_can_be_called_in_one_turn_and_land_in_their_own_lists():
    parsed = parse_assistant_reply({
        "content": "Written and expanded.",
        "tool_calls": [
            {"function": {"name": FILL_SHOTS_TOOL, "arguments": json.dumps(
                {"shots": [{"shot_id": "shot_one", "prompt": "A wolf crosses the clearing."}]}
            )}},
            {"function": {"name": EXPAND_PROMPTS_TOOL, "arguments": json.dumps(
                {"shots": [{"shot_id": "shot_one"}]}
            )}},
        ],
    })

    assert [fill.shot_id for fill in parsed.fills] == ["shot_one"]
    assert [item.shot_id for item in parsed.expansions] == ["shot_one"]
    assert parsed.malformed == []


def test_the_tool_reaches_the_specialist_once_per_shot_and_writes_only_h3_prompt(tmp_path: Path):
    """ProducerBot is the surface and the specialist is in its box.

    One call per shot on the server, the intent untouched, and the expansion in its own field.
    """
    director = ExpandingProducerDirector(expanding_turn("shot_one", "shot_two"))
    client, store, _ = make_client(tmp_path, director=director)
    project = producer_project(store)
    project.shots[0].prompt = "A wolf crosses the clearing."
    project.shots[1].prompt = "Lucy turns to camera."
    store.save(project)

    response = client.post(
        FILL.format(project=project.id),
        json={"message": "Expand those two into H3 prompts", "shot_ids": ["shot_one", "shot_two"]},
    )

    assert response.status_code == 200
    assert [held["shot"]["id"] for held in director.expansions] == ["shot_one", "shot_two"]
    stored = store.get(project.id)
    assert [shot.h3_prompt for shot in stored.shots] == [GOOD_H3, GOOD_H3]
    assert [shot.prompt for shot in stored.shots] == [
        "A wolf crosses the clearing.", "Lucy turns to camera."
    ]
    assert "H3 prompts written for 2 shot(s)" in reply_text(stored)


def test_the_tool_expands_from_the_intent_the_same_turn_just_wrote(tmp_path: Path):
    """Order is the point: fills first, then expansions, so a shot filled in and expanded in one
    turn is expanded from the intent this turn wrote rather than from the one it replaced."""
    answer = turn({"shot_id": "shot_one", "prompt": "A grey wolf crosses the clearing at dusk."})
    answer.expansions = [ShotExpansionRequest(shot_id="shot_one")]
    director = ExpandingProducerDirector(answer)
    client, store, _ = make_client(tmp_path, director=director)
    project = producer_project(store)

    client.post(
        FILL.format(project=project.id),
        json={"message": "Write shot one and expand it", "shot_ids": ["shot_one"]},
    )

    assert director.expansions[0]["shot"]["intent"] == "A grey wolf crosses the clearing at dusk."
    stored = store.get(project.id)
    assert stored.shots[0].prompt == "A grey wolf crosses the clearing at dusk."
    assert stored.shots[0].h3_prompt == GOOD_H3


def test_the_tool_cannot_expand_a_shot_the_turn_did_not_select(tmp_path: Path):
    """The guard that stops a tool widening what the assistant can act *on*.

    `shot_two` is real, unlocked and perfectly writable. It is not in this turn's selection, so it
    is out of reach -- and no model call is spent on it either.
    """
    director = ExpandingProducerDirector(expanding_turn("shot_one", "shot_two"))
    client, store, _ = make_client(tmp_path, director=director)
    project = producer_project(store)
    project.shots[0].prompt = "A wolf crosses the clearing."
    project.shots[1].prompt = "Lucy turns to camera."
    store.save(project)

    client.post(
        FILL.format(project=project.id),
        json={"message": "Expand shot one", "shot_ids": ["shot_one"]},
    )

    assert [held["shot"]["id"] for held in director.expansions] == ["shot_one"]
    stored = store.get(project.id)
    assert stored.shots[0].h3_prompt == GOOD_H3
    assert stored.shots[1].h3_prompt == ""
    assert "not among the shots this request selected" in reply_text(stored)


def test_the_tool_meets_every_refusal_a_directors_own_click_meets(tmp_path: Path):
    """A tool that cannot be refused is a guard hole.

    A locked shot, a rendered one and one with no intent, all selected, all refused -- by
    `shot_write_refusal` and the prompt gate the inspector's own button goes through, in the order
    phase one pinned.
    """
    director = ExpandingProducerDirector(
        expanding_turn("shot_locked", "shot_rendered", "shot_blank", "shot_open")
    )
    client, store, _ = make_client(tmp_path, director=director)
    project = producer_project(store)
    project.shots = [
        Shot(id="shot_locked", start=0, duration=5, prompt="Do not touch", locked=True),
        Shot(id="shot_rendered", start=5, duration=5, prompt="Already shot",
             prompt_id="abc", status="complete"),
        Shot(id="shot_blank", start=10, duration=5, prompt=PLACEHOLDER_PROMPT),
        Shot(id="shot_open", start=15, duration=5, prompt="A wolf crosses the clearing."),
    ]
    store.save(project)

    client.post(
        FILL.format(project=project.id),
        json={
            "message": "Expand all four",
            "shot_ids": ["shot_locked", "shot_rendered", "shot_blank", "shot_open"],
        },
    )

    # Only the open one was ever sent to the specialist.
    assert [held["shot"]["id"] for held in director.expansions] == ["shot_open"]
    stored = store.get(project.id)
    assert [shot.h3_prompt for shot in stored.shots] == ["", "", "", GOOD_H3]
    text = reply_text(stored)
    assert "they are locked" in text
    assert "already depends on the prompt" in text
    assert "no intent to expand from" in text


def test_a_malformed_expansion_from_the_tool_is_reported_and_never_stored(tmp_path: Path):
    director = ExpandingProducerDirector(
        expanding_turn("shot_one"), answers={"shot_one": "A wolf. 35mm, grainy."}
    )
    client, store, _ = make_client(tmp_path, director=director)
    project = producer_project(store)
    project.shots[0].prompt = "A wolf crosses the clearing."
    store.save(project)

    client.post(
        FILL.format(project=project.id),
        json={"message": "Expand shot one", "shot_ids": ["shot_one"]},
    )

    stored = store.get(project.id)
    assert stored.shots[0].h3_prompt == ""
    assert "not a well-formed H3 prompt" in reply_text(stored)
    # The refused text is inspectable and is not in the sentence the next call reads.
    assert "A wolf. 35mm, grainy." not in reply_text(stored)
    assert any(notice.raw == "A wolf. 35mm, grainy." for notice in reply_notices(stored))


def test_a_turn_that_only_expands_is_not_reported_as_prose_with_no_tool_call(tmp_path: Path):
    """It did call a tool. Telling the Director it answered in prose would be false, and would send
    them to ask again for something that already happened."""
    director = ExpandingProducerDirector(expanding_turn("shot_one"))
    client, store, _ = make_client(tmp_path, director=director)
    project = producer_project(store)
    project.shots[0].prompt = "A wolf crosses the clearing."
    store.save(project)

    client.post(
        FILL.format(project=project.id),
        json={"message": "Expand shot one", "shot_ids": ["shot_one"]},
    )

    assert ASSISTANT_WITHOUT_TOOL_CALL_NOTICE not in reply_text(store.get(project.id))


def test_the_assistant_is_told_which_shots_are_expanded_and_never_the_expansion(tmp_path: Path):
    """A boolean, because the tool is useless without it and the text is what degrades the model.

    MiniMax's own worked examples run past a thousand characters each; a plan of them in every turn
    is the largest context regression available here, and it is what `SHOT_DIRECTOR_WITHHELD`
    exists to prevent. This payload is not the exception to that rule.
    """
    project = Project(name="Expanded")
    project.shots = [
        Shot(id="shot_one", start=0, duration=5, prompt="A wolf crosses the clearing.",
             h3_prompt=GOOD_H3),
        Shot(id="shot_two", start=5, duration=5, prompt="Lucy turns to camera."),
    ]

    built = assistant_input(project, shot_ids=["shot_one", "shot_two"])

    assert [entry["expanded"] for entry in built["shots"]] == [True, False]
    serialised = json.dumps(built, ensure_ascii=False)
    assert GOOD_H3 not in serialised
    assert "integrated_multimodal_description" not in serialised
    # The intent is still there, which is what makes withholding the expansion defensible at all.
    assert "A wolf crosses the clearing." in serialised


def test_a_whitespace_only_expansion_does_not_count_as_expanded():
    """`reference_prompt` treats whitespace as absent, so the flag must agree -- otherwise the
    assistant is told a shot is expanded while the render submits its intent."""
    project = Project(name="Blank")
    project.shots = [Shot(id="shot_one", start=0, duration=5, prompt="A wolf.", h3_prompt="  \n ")]

    built = assistant_input(project, shot_ids=["shot_one"])

    assert built["shots"][0]["expanded"] is False


def test_a_fill_cites_the_identity_sheet_and_says_so(tmp_path: Path):
    """The identity-sheet rule reaches the assistant too, and it does not do it quietly.

    This is the second writer of citations from a model's answer, and it has populate's defect:
    the library offers the source picture and the sheet promoted from it as two rows, and a shot
    conditioned on the single frame is using the weaker of the two. The substitution is one
    function (`models.prefer_identity_sheets`) so both callers cannot drift; the notice is what
    keeps it from being a silent rewrite of an id the model named out loud. A subject with no
    sheet is untouched, which is the control that matters.
    """
    from music_video_producer.app import ASSISTANT_IDENTITY_SHEET_NOTICE

    director = FillingDirector(
        turn(
            {
                "shot_id": "shot_one",
                "citations": [
                    {"asset_id": "asset_wolf", "role": "reference"},
                    {"asset_id": "asset_sheet", "role": "reference"},
                ],
            },
            {"shot_id": "shot_two", "citations": [{"asset_id": "asset_forest", "role": "reference"}]},
        )
    )
    client, store, _comfy = make_client(tmp_path, director)
    project = producer_project(store, name="Sheets")
    project.assets.append(
        Asset(id="asset_sheet", name="Grey wolf · multiview", kind="character",
              path="out/sheet.png", source="krea-multiview", parent_id="asset_wolf")
    )
    store.save(project)

    response = client.post(
        FILL.format(project=project.id),
        json={"message": "cite the wolf and the forest", "shot_ids": ["shot_one", "shot_two"]},
    )

    assert response.status_code == 200, response.text
    stored = store.get(project.id)
    # Both of the model's ids collapse onto the sheet: the source is the sheet, once.
    assert [item.asset_id for item in stored.shots[0].citations] == ["asset_sheet"]
    # The forest has no sheet, so nothing about it changed.
    assert [item.asset_id for item in stored.shots[1].citations] == ["asset_forest"]
    text = reply_text(stored)
    assert ASSISTANT_IDENTITY_SHEET_NOTICE.format(shots=shot_label(stored, stored.shots[0])) in text
    assert shot_label(stored, stored.shots[1]) not in text.split("Cited the promoted")[1]


def test_a_fill_whose_prompt_is_refused_reports_no_substitution(tmp_path: Path):
    """The substitution is reported at the commit point, never at the substitution.

    A fill whose prompt is rejected is discarded whole — mode, citations and all — so a notice
    saying its citations were re-pointed would be reporting a change to a shot nothing was
    written to.
    """
    director = FillingDirector(
        turn(
            {
                "shot_id": "shot_one",
                "prompt": '{"prompt": "a wolf"}',
                "citations": [{"asset_id": "asset_wolf", "role": "reference"}],
            },
        )
    )
    client, store, _comfy = make_client(tmp_path, director)
    project = producer_project(store, name="Refused")
    project.assets.append(
        Asset(id="asset_sheet", name="Grey wolf · multiview", kind="character",
              path="out/sheet.png", source="krea-multiview", parent_id="asset_wolf")
    )
    store.save(project)

    response = client.post(
        FILL.format(project=project.id),
        json={"message": "cite the wolf", "shot_ids": ["shot_one"]},
    )

    assert response.status_code == 200, response.text
    stored = store.get(project.id)
    assert stored.shots[0].citations == []
    assert "Cited the promoted identity sheet" not in reply_text(stored)


def test_a_keyframe_citation_is_never_swapped_for_a_contact_sheet(tmp_path: Path):
    """`first`, `last` and `middle` name a concrete frame the render pins the picture to.

    Substituting a four-panel sheet for a shot's first frame would make the first frame a contact
    sheet, which is the opposite of what the role asks for. Only `reference` is re-pointed.
    """
    from music_video_producer.models import prefer_identity_sheets

    sheets = {"asset_wolf": "asset_sheet"}
    keyframes = [
        AssetCitation(asset_id="asset_wolf", role="first", order=0),
        AssetCitation(asset_id="asset_wolf", role="last", order=1),
    ]
    assert prefer_identity_sheets(keyframes, sheets) == keyframes
    mixed = [*keyframes, AssetCitation(asset_id="asset_wolf", role="reference", order=2)]
    assert [item.asset_id for item in prefer_identity_sheets(mixed, sheets)] == [
        "asset_wolf",
        "asset_wolf",
        "asset_sheet",
    ]
    # No sheets at all: the same citations back, field for field.
    assert prefer_identity_sheets(mixed, {}) == mixed
