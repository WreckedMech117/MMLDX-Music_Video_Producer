import json
from copy import deepcopy

import httpx
import pytest

from music_video_producer.director import (
    EXPANSION_SYSTEM_PROMPT,
    DirectorClient,
    DirectorError,
    DirectorResult,
    DirectorUnavailable,
    ExpandedShot,
    SectionLooks,
    ShotExpansion,
    StageManagerResult,
    VisionInspection,
    constrained_schema,
    director_result_schema,
    extract_json,
)
from music_video_producer.models import Project, Shot, Song
from music_video_producer.timeline import expansion_input


@pytest.mark.asyncio
async def test_director_returns_validated_structured_treatment():
    response_body = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "message": "I expanded the chorus into a visual release.",
                            "treatment": "A performer crosses from confinement into open desert.",
                            "style_bible": "Sodium amber, hard backlight, 35mm grain.",
                            "shots": [
                                {"start": 0, "duration": 5, "prompt": "Slow push through a narrow hall"},
                                {"start": 5, "duration": 6, "prompt": "Wide desert performance at dawn"},
                            ],
                        }
                    )
                }
            }
        ]
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert body["model"] == "local-director"
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["name"] == "director_result"
        assert body["response_format"]["json_schema"]["schema"]["required"] == [
            "message",
            "treatment",
            "style_bible",
        ]
        return httpx.Response(200, json=response_body)

    director = DirectorClient(
        base_url="http://llm.test/v1",
        model="local-director",
        transport=httpx.MockTransport(handler),
    )
    result = await director.plan(message="Make the chorus open up", project_context={"song": {"duration": 11}})

    assert result.shots[1].start == 5
    assert result.treatment.startswith("A performer")


@pytest.mark.asyncio
async def test_director_is_explicitly_unavailable_without_configuration():
    director = DirectorClient(base_url="", model="")

    with pytest.raises(DirectorUnavailable, match="not configured"):
        await director.plan(message="Make a video", project_context={})
    # Expansion refuses the same way, so the route can map it to the same 503 rather than
    # discovering an unconfigured director as an opaque connection error.
    with pytest.raises(DirectorUnavailable, match="not configured"):
        await director.expand(expansion_input={"shots": []})


@pytest.mark.asyncio
async def test_expand_asks_for_prompts_keyed_by_shot_id_and_sends_the_input_verbatim():
    """The wire shape of the expansion call, and the one thing that makes the merge safe.

    The schema is generated from the pydantic model, so `shot_id` being required is the same
    fact the route relies on when it refuses to merge positionally. The user content is the
    builder's payload and nothing else: anything added here would make the route test that
    asserts what was handed to `expand` true of a payload the model never saw.
    """
    payload = {
        "treatment": "Three movements.",
        "shots": [{"shot_id": "shot_first", "index": 0, "start": 0, "end": 5}],
    }
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.update(body)
        assert request.url.path == "/v1/chat/completions"
        schema = body["response_format"]["json_schema"]
        assert body["response_format"]["type"] == "json_schema"
        assert schema["name"] == "shot_expansion"
        assert schema["schema"]["required"] == ["message", "shots"]
        # The id is required on every entry, which is what "keyed by shot id, never by
        # position" means at the wire boundary.
        assert schema["schema"]["$defs"]["ExpandedShot"]["required"] == ["shot_id", "prompt"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "message": "Held the identity, moved the framing.",
                                    "shots": [
                                        {
                                            "shot_id": "shot_first",
                                            "prompt": "Slow push through a narrow hall",
                                        }
                                    ],
                                }
                            )
                        }
                    }
                ]
            },
        )

    director = DirectorClient(
        base_url="http://llm.test/v1",
        model="local-director",
        transport=httpx.MockTransport(handler),
    )
    result = await director.expand(expansion_input=payload)

    assert result.shots == [
        ExpandedShot(shot_id="shot_first", prompt="Slow push through a narrow hall")
    ]
    assert isinstance(result, ShotExpansion)
    assert json.loads(seen["messages"][1]["content"]) == payload
    # The system prompt is what makes the plan cohere: the constants to hold and the axes to
    # move are named, rather than left for the model to infer per shot.
    system = seen["messages"][0]["content"]
    assert system == EXPANSION_SYSTEM_PROMPT
    for constant in ("identity", "wardrobe", "palette", "lens"):
        assert constant in system, constant
    for axis in ("action", "framing", "energy"):
        assert axis in system, axis


@pytest.mark.asyncio
async def test_director_reuses_loaded_lm_studio_instance():
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"data": [{"id": "local-director:2"}, {"id": "other-model"}]},
            )
        body = json.loads(request.content)
        if body["model"] == "local-director":
            return httpx.Response(400, json={"error": "Model is unloaded."})
        assert body["model"] == "local-director:2"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "message": "Loaded instance reused.",
                                    "treatment": "One continuous shot.",
                                    "style_bible": "Amber light.",
                                    "shots": [],
                                }
                            )
                        }
                    }
                ]
            },
        )

    director = DirectorClient(
        base_url="http://llm.test/v1",
        model="local-director",
        transport=httpx.MockTransport(handler),
    )
    result = await director.plan(message="Make a video", project_context={})

    assert result.message == "Loaded instance reused."
    assert calls == [
        ("POST", "/v1/chat/completions"),
        ("GET", "/v1/models"),
        ("POST", "/v1/chat/completions"),
    ]


def test_the_chat_schema_requires_neither_shots_nor_sections():
    """The chat route's grammar, pinned, so nothing can quietly harden it.

    `DirectorResult` is shared: Populate Timeline cannot proceed without `shots` and now
    says so in its own schema, but the *chat* route must not. A Director who asks "what
    would you change about the second verse?" deserves an answer, and a strict schema that
    required `shots` would force the constrained decoder to invent a shot list to close the
    object — a worse bug than the empty-`shots` one the required set was added to fix.

    Asserted against the schema `plan` sends with no `response_schema`, not against the
    pydantic model, because the schema is the thing on the wire and the two only agree
    while nobody has edited the builder.
    """
    schema = director_result_schema()

    assert schema["required"] == ["message", "treatment", "style_bible"]
    assert "shots" not in schema["required"]
    assert "sections" not in schema["required"]
    # Both fields are still *offered*; optional is not absent.
    assert {"shots", "sections"} <= set(schema["properties"])
    # And no count floor is smuggled in on the chat path.
    assert "minItems" not in schema["properties"]["shots"]
    # The default is byte-identical to what the route sent before the builder existed.
    assert schema == DirectorResult.model_json_schema()


def test_director_result_schema_promotes_exactly_what_a_caller_requires():
    """The builder, which exists because `default_factory=list` kept `shots` out of
    `required` and the constrained decoder was therefore right to omit it.

    Each required set is a different call's contract — the structure pass needs `sections`
    and is told to leave `shots` empty, the shots pass is the mirror — which is why this is
    a parameter rather than one hardened model.
    """
    shots_only = director_result_schema(require=("shots",))
    assert shots_only["required"] == ["message", "treatment", "style_bible", "shots"]

    both = director_result_schema(require=("shots", "sections"))
    assert both["required"] == ["message", "treatment", "style_bible", "shots", "sections"]

    # The structure pass's set: `sections` demanded, `shots` pointedly left optional so a
    # call told to leave the list empty is not forced to fill it.
    sections_only = director_result_schema(require=("sections",))
    assert sections_only["required"] == ["message", "treatment", "style_bible", "sections"]
    assert "shots" not in sections_only["required"]

    # Property order, not call order — a `required` list whose order drifts with the
    # caller's argument order is a wire payload that changes for no reason.
    assert director_result_schema(require=("sections", "shots"))["required"] == both["required"]

    # Already-required fields are never duplicated.
    assert director_result_schema(require=("message",))["required"] == [
        "message",
        "treatment",
        "style_bible",
    ]

    # `minItems` is opt-in and lands on the shots array alone. Measured against LM Studio
    # on 2026-08-20: the constrained decoder honours it, and it also pads with entries that
    # fail `PlannedShot`, which is why nothing sets it by default.
    floored = director_result_schema(require=("shots",), min_shots=12)
    assert floored["properties"]["shots"]["minItems"] == 12
    assert "minItems" not in floored["properties"]["sections"]
    assert "minItems" not in shots_only["properties"]["shots"]

    # No variant may leak into the next one, or the chat route inherits populate's grammar
    # from whichever call happened to run first.
    assert "minItems" not in DirectorResult.model_json_schema()["properties"]["shots"]
    assert director_result_schema()["required"] == ["message", "treatment", "style_bible"]


@pytest.mark.asyncio
async def test_plan_sends_the_callers_schema_and_still_validates_a_director_result():
    """The wire half: a caller's required set reaches `response_format`, and the reply is
    still parsed as an ordinary `DirectorResult` so no caller handles a second type."""
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "message": "Laid out.",
                                    "treatment": "T",
                                    "style_bible": "S",
                                    "shots": [{"start": 0, "duration": 5, "prompt": "Wide."}],
                                    "sections": [
                                        {"label": "Verse", "start": 0, "duration": 60}
                                    ],
                                }
                            )
                        }
                    }
                ]
            },
        )

    director = DirectorClient(
        base_url="http://llm.test/v1",
        model="local-director",
        transport=httpx.MockTransport(handler),
    )
    result = await director.plan(
        message="Lay out the plan",
        project_context={},
        response_schema=director_result_schema(require=("shots", "sections"), min_shots=12),
    )

    schema = seen["response_format"]["json_schema"]
    # The name is unchanged: it labels the answer's kind, and the required set is not a
    # different kind of answer.
    assert schema["name"] == "director_result"
    assert schema["strict"] is True
    assert schema["schema"]["required"] == [
        "message",
        "treatment",
        "style_bible",
        "shots",
        "sections",
    ]
    assert schema["schema"]["properties"]["shots"]["minItems"] == 12
    assert isinstance(result, DirectorResult)
    assert result.shots[0].prompt == "Wide."
    assert result.sections[0].label == "Verse"


def test_the_expansion_prompt_describes_every_key_the_input_actually_carries():
    """A payload whose semantics the model must infer is a variance mechanism that is hoped for.

    `song_fraction` and `neighbours` exist to drive deliberate cross-shot variance, which is the
    whole point of the story, and `locked` is only worth sending if something asks the model to
    skip locked Shots. Driven off a real `expansion_input` payload rather than a hand-written
    list, so a key added to the builder without a line in the prompt fails here.

    The `song` block is walked as well as the top level, because it is the one nested object in
    this payload that carries meaning of its own: the lyric sheet and the style description are
    the bulkiest thing the call sends, and a field the prompt never names is a field the model
    may quietly ignore — which would make sending it look done while doing nothing.
    """
    project = Project(name="Prompted")
    project.song = Song(
        title="Spine",
        source="imported",
        duration=120,
        lyrics="[Verse 1]\nCold rail, the platform hums",
        caption="Downtempo industrial pop, tape saturation.",
    )
    project.shots = [
        Shot(id="shot_a", start=0, duration=5, prompt="Corridor"),
        Shot(id="shot_b", start=30, duration=6, prompt="Threshold", locked=True),
    ]
    payload = expansion_input(project)

    # Whitespace-collapsed, because the prompt is hard-wrapped and a phrase that spans a line
    # break is still a phrase the model reads.
    described = " ".join(EXPANSION_SYSTEM_PROMPT.split())
    for key in payload:
        assert key in described, key
    for key in payload["shots"][0]:
        assert key in described, key
    # The song block is only worth its tokens if the model is told what it has and what to do
    # with it. Driven off the built block, so a key added to the song without a line in the
    # prompt fails here exactly as a top-level one does.
    #
    # Scoped to the `- song:` entry rather than searched for anywhere in the prompt, because a
    # whole-text search is satisfied by a word that happens to appear in some other sentence —
    # which is how "lyrics" could be struck from the list the model reads as its manifest of
    # what the input holds while this test stayed green. The list is the thing under test.
    described_song = described.split("- song:")[1].split(" - ")[0]
    assert set(payload["song"]) == {"title", "duration", "lyrics", "caption"}
    for key in payload["song"]:
        assert key in described_song, key
    # Named *and* aimed: the words are context for what the video is about, and they are not a
    # clock — a section tag in a lyric sheet is the one thing nothing in this path may retime.
    assert "draw imagery, subject and mood from them" in described_song
    assert "a section tag inside the sheet is structure, not a time" in described_song
    # And the two keys that only pay for themselves if the model is told what to do with them.
    assert "Return no entry for a locked shot" in described
    assert "energy curve" in described
    # The id contract is stated in prose as well as in the schema, because a prompt keyed to the
    # wrong Shot is free text that fails silently.
    assert "Copy it verbatim" in described


@pytest.mark.asyncio
async def test_a_reply_carrying_no_message_content_is_an_error_not_a_crash():
    """`content: null` is what a refusal, a truncated reply and a tool-call reply all look like.

    `json.loads(None)` raises `TypeError`, which used to escape every caller's caught tuple — so
    the most common bad-day shape was the only one that reached the Director as a 500 while
    every other malformed reply became a 502. Asserted across all three calls, because the
    decoding is now shared and a guard on one would look like a guard on all.
    """
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": None}}]})

    director = DirectorClient(
        base_url="http://llm.test/v1",
        model="local-director",
        transport=httpx.MockTransport(handler),
    )

    for label, call in (
        ("plan", lambda: director.plan(message="Make a video", project_context={})),
        ("expand", lambda: director.expand(expansion_input={"shots": []})),
        (
            "inspect_image",
            lambda: director.inspect_image(image=b"x", mime_type="image/png", purpose="p"),
        ),
    ):
        with pytest.raises(DirectorError, match="no message content"):
            await call()
        assert label


@pytest.mark.asyncio
async def test_a_models_listing_of_an_unexpected_shape_does_not_escape_as_an_attribute_error():
    """`/models` is whatever the configured provider answers with.

    A bare JSON array makes `.get("data")` raise `AttributeError`, which is outside the caught
    tuple — so a recovery path written for one provider's quirk would crash as a 500 on another.
    Both levels are exercised: the listing itself, and the entries inside a well-shaped listing,
    because a guard on only the outer one leaves the inner crash reachable by any provider that
    answers with a list of plain model names.

    No usable id means no retry: the provider's own 400 is what the Director is told about.
    """
    for label, listing in (
        ("bare array", ["local-director:2", 7]),
        ("scalar", "local-director:2"),
        ("array of names", {"data": ["local-director:2", 7, None]}),
    ):
        calls = []

        async def handler(request: httpx.Request, listing=listing, calls=calls) -> httpx.Response:
            calls.append((request.method, request.url.path))
            if request.method == "GET":
                return httpx.Response(200, json=listing)
            return httpx.Response(400, json={"error": "Model is unloaded."})

        director = DirectorClient(
            base_url="http://llm.test/v1",
            model="local-director",
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(DirectorError):
            await director.expand(expansion_input={"shots": []})
        # It looked, found nothing usable, and reported the provider's own refusal.
        assert calls == [("POST", "/v1/chat/completions"), ("GET", "/v1/models")], label


@pytest.mark.asyncio
async def test_a_reply_without_a_shots_key_is_a_failed_call_rather_than_an_empty_expansion():
    """`ShotExpansion.shots` has no default on purpose, and this is what says so.

    With a default, a reply that omitted the key entirely would validate as an empty list, and
    the route would then report it as "the model omitted every shot" — a confident, wrong
    diagnosis of a call that simply failed.
    """
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"message": "Done."})}}]},
        )

    director = DirectorClient(
        base_url="http://llm.test/v1",
        model="local-director",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(DirectorError, match="shots"):
        await director.expand(expansion_input={"shots": []})


@pytest.mark.asyncio
async def test_every_call_reuses_the_loaded_instance_through_the_one_retry_helper():
    """The retry was copied verbatim into each call site; a third copy is what forced it out.

    Asserted per call rather than once, because an extraction that left one site behind would
    still pass a test that only exercised `plan` — and the site left behind is the one that then
    fails against a real LM Studio with an unloaded model. The retried request must also carry
    the *loaded* id, or the retry is a second identical failure.
    """
    for label, call, reply in (
        (
            "plan",
            lambda director: director.plan(message="Make a video", project_context={}),
            {"message": "m", "treatment": "t", "style_bible": "s", "shots": []},
        ),
        (
            "expand",
            lambda director: director.expand(expansion_input={"shots": []}),
            {"message": "m", "shots": []},
        ),
        (
            "inspect_image",
            lambda director: director.inspect_image(
                image=b"png-data", mime_type="image/png", purpose="character reference"
            ),
            {
                "summary": "A vocalist.",
                "identity": [],
                "environment": [],
                "continuity_cues": [],
                "prompt_cues": [],
                "risks": [],
            },
        ),
    ):
        calls = []
        models = []

        # Bound as defaults rather than closed over: one handler per iteration, recording into
        # that iteration's own lists.
        async def handler(
            request: httpx.Request, reply=reply, calls=calls, models=models
        ) -> httpx.Response:
            calls.append((request.method, request.url.path))
            if request.method == "GET":
                return httpx.Response(200, json={"data": [{"id": "local-director:2"}]})
            body = json.loads(request.content)
            models.append(body["model"])
            if body["model"] == "local-director":
                return httpx.Response(400, json={"error": "Model is unloaded."})
            return httpx.Response(
                200, json={"choices": [{"message": {"content": json.dumps(reply)}}]}
            )

        director = DirectorClient(
            base_url="http://llm.test/v1",
            model="local-director",
            transport=httpx.MockTransport(handler),
        )

        await call(director)

        assert calls == [
            ("POST", "/v1/chat/completions"),
            ("GET", "/v1/models"),
            ("POST", "/v1/chat/completions"),
        ], label
        assert models == ["local-director", "local-director:2"], label


@pytest.mark.asyncio
async def test_director_inspects_reference_image_with_multimodal_content():
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        content = body["messages"][1]["content"]
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert body["response_format"]["json_schema"]["name"] == "vision_inspection"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "A vocalist in a silver jacket.",
                                    "identity": ["short dark hair", "silver jacket"],
                                    "environment": ["warehouse", "amber practical lights"],
                                    "continuity_cues": ["keep the jacket zipped"],
                                    "prompt_cues": ["medium close-up", "warm backlight"],
                                    "risks": ["face partly shadowed"],
                                }
                            )
                        }
                    }
                ]
            },
        )

    director = DirectorClient(
        base_url="http://llm.test/v1",
        model="vision-director",
        transport=httpx.MockTransport(handler),
    )
    result = await director.inspect_image(
        image=b"png-data", mime_type="image/png", purpose="character reference"
    )

    assert result.identity == ["short dark hair", "silver jacket"]
    assert result.risks == ["face partly shadowed"]


def test_document_rejection_blocks_json_masquerading_as_prose():
    """Reproduces the observed defect: the model returns serialised JSON in a prose field."""
    from music_video_producer.director import document_rejection

    degraded = '[{"style":"moody","color_palette":["amber","teal"]}]'
    assert "JSON" in document_rejection(degraded, "An existing style bible with real prose.")
    # Also rejected when there is nothing to lose, because it is still not prose.
    assert document_rejection(degraded, "") != ""


def test_document_rejection_blocks_collapsed_replacement():
    from music_video_producer.director import document_rejection

    existing = "x" * 500
    assert document_rejection("y" * 100, existing) != ""  # 20%, under the 40% floor
    assert document_rejection("y" * 400, existing) == ""  # 80%, accepted


def test_document_rejection_accepts_any_first_draft():
    from music_video_producer.director import document_rejection

    assert document_rejection("Short but the first one.", "") == ""
    assert document_rejection("Short but the first one.", "   ") == ""


def test_document_rejection_allows_prose_starting_with_a_bracket():
    from music_video_producer.director import document_rejection

    prose = "[Opening] The performer steps into frame under a single amber bulb."
    assert document_rejection(prose, "An existing treatment of similar length here.") == ""


@pytest.mark.asyncio
async def test_expand_shot_replays_a_rejected_answer_as_a_corrective_turn():
    """The retry's wire shape: the failed text as an assistant turn, then `H3_RETRY_PROMPT`
    carrying the checker's own sentences as the next user turn.

    That order is the mechanism -- the model is correcting a concrete answer it can see in its
    own conversation, not being asked to reroll from nothing. And without `rejected`, the call
    stays exactly two messages, so an ordinary first attempt carries no ghost of a retry.
    """
    from music_video_producer.director import H3_RETRY_PROMPT

    bodies = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "a corrected prompt"}}]}
        )

    director = DirectorClient(
        base_url="http://llm.test/v1",
        model="local-director",
        transport=httpx.MockTransport(handler),
    )

    await director.expand_shot(shot_input={"shot": {"id": "s1"}}, system_prompt="Rules.")
    assert [message["role"] for message in bodies[0]["messages"]] == ["system", "user"]

    await director.expand_shot(
        shot_input={"shot": {"id": "s1"}},
        system_prompt="Rules.",
        rejected="A malformed answer.",
        rejected_problems=("No [Shot 1] opening.", "overall_soundscape is missing."),
    )
    messages = bodies[1]["messages"]
    assert [message["role"] for message in messages] == [
        "system", "user", "assistant", "user",
    ]
    assert messages[2]["content"] == "A malformed answer."
    assert messages[3]["content"] == H3_RETRY_PROMPT.format(
        problems="- No [Shot 1] opening.\n- overall_soundscape is missing."
    )


@pytest.mark.asyncio
async def test_a_reasoning_budget_exhaustion_is_its_own_error_kind():
    """`DirectorBudgetExhausted`, a `DirectorError` subclass, because a caller has to tell the
    one retryable provider failure apart from the ones that will fail identically next time --
    and matching on message text would make that decision hostage to a wording."""
    from music_video_producer.director import DirectorBudgetExhausted

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "reasoning_content": "thinking, at great length, about wolves",
                        },
                        "finish_reason": "length",
                    }
                ]
            },
        )

    director = DirectorClient(
        base_url="http://llm.test/v1",
        model="local-director",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(DirectorBudgetExhausted, match="budget reasoning"):
        await director.expand_shot(shot_input={"shot": {"id": "s1"}}, system_prompt="Rules.")
    assert issubclass(DirectorBudgetExhausted, DirectorError)

    # An empty answer with no reasoning behind it stays the generic error: retrying it is not
    # the budget case's bet, and the caller's catch distinguishes the two by type.
    async def empty_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    empty = DirectorClient(
        base_url="http://llm.test/v1",
        model="local-director",
        transport=httpx.MockTransport(empty_handler),
    )
    with pytest.raises(DirectorError) as caught:
        await empty.expand_shot(shot_input={"shot": {"id": "s1"}}, system_prompt="Rules.")
    assert not isinstance(caught.value, DirectorBudgetExhausted)


# --- The JSON extraction ladder -------------------------------------------------------
#
# Every case below is a reply shape a local model actually produces. The one that forced the
# ladder is `test_extract_json_recovers_the_object_from_reasoning_chatter`: `enable_thinking:
# false` stopped taking effect on 2026-08-19 and the loaded model began reasoning *inside*
# `message.content` before answering, so `json.loads` refused a whole string that had a
# perfectly good object sitting in the middle of it.


def test_extract_json_parses_a_clean_reply_exactly_as_json_loads_would():
    """The happy path, pinned. `response_format: json_schema strict` produces exactly this,
    and the ladder must never cost it anything: rung 1 is `json.loads` and nothing else."""
    payload = {"message": "Held the identity.", "shots": [{"shot_id": "s1", "prompt": "Hall"}]}
    text = json.dumps(payload)

    assert extract_json(text) == json.loads(text) == payload
    # Surrounding whitespace is stripped, which is what `json.loads` does anyway.
    assert extract_json(f"\n  {text}\n\n") == payload
    # Non-object values still round-trip, so the ladder is not secretly object-only.
    assert extract_json("[1, 2, 3]") == [1, 2, 3]


def test_extract_json_parses_a_fenced_reply():
    """A markdown fence, with and without the `json` language tag, and with prose in front."""
    payload = {"message": "Done.", "shots": []}

    assert extract_json(f"```json\n{json.dumps(payload)}\n```") == payload
    assert extract_json(f"```\n{json.dumps(payload)}\n```") == payload
    assert extract_json(f"Here is the plan:\n\n```json\n{json.dumps(payload)}\n```\n") == payload
    # Cut off mid-fence - a length-truncated reply still has usable content in front of the
    # truncation, so the closing fence is optional.
    assert extract_json(f"```json\n{json.dumps(payload)}") == payload


def test_a_fence_beats_a_json_fragment_quoted_in_the_reasoning_in_front_of_it():
    """Why the fence rung is tried *before* the balanced scan rather than instead of it.

    A reasoning model quotes a fragment of the schema while it thinks and then answers inside a
    fence. Both fragments are valid JSON; only the fence says which one is the answer. Scanning
    first would return the model's scratch note as the plan.
    """
    reply = (
        'First let me restate the shape I owe: {"message": "<one sentence>", "shots": []}. '
        "That is the skeleton, not the answer.\n\n"
        "Now the real plan:\n\n"
        '```json\n{"message": "Three movements, one identity.", '
        '"shots": [{"shot_id": "shot_first", "prompt": "Slow push through a narrow hall"}]}\n```\n'
    )

    result = extract_json(reply)
    assert result["message"] == "Three movements, one identity."
    assert result["shots"][0]["shot_id"] == "shot_first"


def test_extract_json_recovers_the_object_from_reasoning_chatter():
    """The recorded 2026-08-19 regression, in the shape it actually arrives in.

    The model reasons in prose inside `message.content` and then answers. `json.loads` fails on
    the whole string; `raw_decode` at the object's own offset does not care what is around it.
    """
    reply = (
        "Okay, let me think about this one. The plan has two shots and the brief asks for a "
        "single continuous identity, so wardrobe and palette stay fixed across both.\n\n"
        "Shot shot_first is the opener at 0-5s, so it should establish rather than resolve. "
        "Shot shot_second cuts from it, which means it must not repeat the framing.\n\n"
        "I'll return the object now.\n"
        '{"message": "Held the identity, moved the framing.", "shots": '
        '[{"shot_id": "shot_first", "prompt": "Slow push through a narrow hall"}, '
        '{"shot_id": "shot_second", "prompt": "Wide desert performance at dawn"}]}'
    )

    result = extract_json(reply)
    assert result["message"] == "Held the identity, moved the framing."
    assert [shot["shot_id"] for shot in result["shots"]] == ["shot_first", "shot_second"]


def test_extract_json_ignores_chatter_after_the_object():
    """The mirror case: the model answers and then keeps talking. `raw_decode` consumes the
    value and stops, so whatever follows is simply not part of it."""
    reply = (
        '{"summary": "A vocalist in a silver jacket.", "identity": ["silver jacket"], '
        '"environment": [], "continuity_cues": [], "prompt_cues": [], "risks": []}\n\n'
        "Let me know if you want me to go deeper on the lighting."
    )

    assert extract_json(reply)["identity"] == ["silver jacket"]


def test_the_balanced_scan_is_a_parser_not_a_brace_counter():
    """Braces inside string values must not end the value early.

    This is not hypothetical for this project: an H3 prompt is a document with its own
    punctuation, and a naive scan that counted `{` against `}` would truncate any prompt that
    mentions one - silently, producing a shorter but still-valid-looking object.
    """
    payload = {
        "message": "One sign, one brace.",
        "shots": [
            {
                "shot_id": "shot_first",
                "prompt": "Neon sign reading {OPEN ALL NIGHT}, and a stray } before a { in graffiti",
            }
        ],
    }
    reply = "Thinking about the sign copy...\n\n" + json.dumps(payload) + "\n\nDone."

    assert extract_json(reply) == payload


def test_extract_json_still_raises_on_a_genuinely_unparseable_reply():
    """Nothing decodes, so the first failure is re-raised unchanged - callers catch
    `json.JSONDecodeError`/`ValueError` and translate it, and a silent `None` here would turn a
    failed call into a confidently wrong empty answer."""
    for label, reply in (
        ("prose only", "I am not going to answer that."),
        ("empty", ""),
        ("fragment", 'Here you go: {"message": "unterminated'),
        ("brace prose", "The schema wants {message, treatment, style_bible}."),
    ):
        with pytest.raises(json.JSONDecodeError):
            extract_json(reply)
        assert label
    # Non-strings raise `TypeError`, which is inside every caller's caught tuple, rather than
    # an `AttributeError` on `.strip()`, which is inside none of them.
    with pytest.raises(TypeError):
        extract_json(None)


@pytest.mark.asyncio
async def test_reasoning_chatter_reaches_every_structured_call_site_as_a_parsed_answer():
    """The ladder is wired into all four `_content` call sites, not one of them.

    Asserted per call because the regression is host-wide: when the loaded model starts
    reasoning in `content`, it does so on `plan`, `stage_manager`, `expand` and
    `inspect_image` alike, and a ladder on only the first would leave the other three as 502s.
    """
    preamble = (
        "Let me work through what the project already holds before I answer.\n\n"
        "The treatment is set and the palette is fixed, so the only open question is framing.\n\n"
        "Here is the object:\n"
    )
    for label, call, reply, check in (
        (
            "plan",
            lambda director: director.plan(message="Make a video", project_context={}),
            {"message": "Planned.", "treatment": "t", "style_bible": "s", "shots": []},
            lambda result: result.message == "Planned.",
        ),
        (
            "stage_manager",
            lambda director: director.stage_manager(project_context={}, count=2),
            {
                "message": "The library has no wide of the warehouse.",
                "assets": [
                    {"kind": "setting", "name": "Warehouse wide", "prompt": "A dark warehouse."}
                ],
            },
            lambda result: result.assets[0].name == "Warehouse wide",
        ),
        (
            "expand",
            lambda director: director.expand(expansion_input={"shots": []}),
            {"message": "m", "shots": [{"shot_id": "shot_first", "prompt": "Narrow hall"}]},
            lambda result: result.shots[0].shot_id == "shot_first",
        ),
        (
            "inspect_image",
            lambda director: director.inspect_image(
                image=b"png-data", mime_type="image/png", purpose="character reference"
            ),
            {
                "summary": "A vocalist.",
                "identity": ["silver jacket"],
                "environment": [],
                "continuity_cues": [],
                "prompt_cues": [],
                "risks": [],
            },
            lambda result: result.identity == ["silver jacket"],
        ),
    ):

        async def handler(request: httpx.Request, reply=reply) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": preamble + json.dumps(reply)}}]},
            )

        director = DirectorClient(
            base_url="http://llm.test/v1",
            model="local-director",
            transport=httpx.MockTransport(handler),
        )

        assert check(await call(director)), label


@pytest.mark.asyncio
async def test_a_reply_with_no_json_anywhere_is_still_the_same_director_error():
    """The ladder must not make failure quieter. Prose with no object in it is a failed call
    and has to arrive at the route as the same `DirectorError` it did before the ladder."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "I'd rather talk about the lighting, honestly."}}
                ]
            },
        )

    director = DirectorClient(
        base_url="http://llm.test/v1",
        model="local-director",
        transport=httpx.MockTransport(handler),
    )

    for call in (
        lambda: director.plan(message="Make a video", project_context={}),
        lambda: director.stage_manager(project_context={}, count=2),
        lambda: director.expand(expansion_input={"shots": []}),
        lambda: director.inspect_image(image=b"x", mime_type="image/png", purpose="p"),
    ):
        with pytest.raises(DirectorError, match="invalid response"):
            await call()


@pytest.mark.asyncio
async def test_a_rejected_response_format_is_retried_once_without_it():
    """Some OpenAI-compatible servers refuse `response_format` outright with a 400.

    One schema-free retry, and then the ladder is what makes the unconstrained reply usable -
    which is why this reply is deliberately the reasoning-chatter shape rather than clean JSON:
    dropping the schema is exactly what invites the chatter.

    `response_format` is *not* dropped by default and this is where that is pinned: the first
    request still carries the strict schema, because it reaches LM Studio's constrained decoder
    on the setup this project runs on, and that is stronger than anything parsing can recover.
    """
    bodies = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "response_format" in body:
            return httpx.Response(
                400,
                json={"error": "'response_format' of type 'json_schema' is not supported"},
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                "The brief asks for one continuous identity, so I will hold "
                                "the wardrobe and move only the framing.\n\n"
                                '{"message": "Schema-free but still an object.", '
                                '"treatment": "A performer crosses into open desert.", '
                                '"style_bible": "Sodium amber.", "shots": []}'
                            )
                        }
                    }
                ]
            },
        )

    director = DirectorClient(
        base_url="http://llm.test/v1",
        model="local-director",
        transport=httpx.MockTransport(handler),
    )
    result = await director.plan(message="Make a video", project_context={})

    assert result.message == "Schema-free but still an object."
    # Exactly two attempts: the strict one first, then one without the key. Not three, and not
    # a schema-free first attempt.
    assert len(bodies) == 2
    assert bodies[0]["response_format"]["json_schema"]["name"] == "director_result"
    assert "response_format" not in bodies[1]
    # Everything else about the request is unchanged; only the one key is dropped.
    assert bodies[1]["messages"] == bodies[0]["messages"]
    assert bodies[1]["model"] == bodies[0]["model"]


@pytest.mark.asyncio
async def test_a_400_against_a_body_that_never_carried_response_format_is_not_retried():
    """The fallback is keyed to the thing it removes.

    `expand_shot` sends no `response_format` at all - there is nothing to drop - so a 400 there
    is the provider's answer and must be reported, not re-sent. An unconditional retry would
    double every failing call's wall-clock cost for nothing.
    """
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(400, json={"error": "chat_template_kwargs is not supported"})

    director = DirectorClient(
        base_url="http://llm.test/v1",
        model="local-director",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(DirectorError):
        await director.expand_shot(shot_input={"shot": {"id": "s1"}}, system_prompt="Rules.")
    assert len(calls) == 1


#: One valid reply per strict schema this application sends, keyed by the name the schema
#: goes on the wire under. Minimal on purpose: the audit below is about the *request*, and a
#: reply that carried more than the parse needs would invite reading it as the contract.
MINIMAL_REPLIES = {
    "director_result": {"message": "m", "treatment": "t", "style_bible": "s"},
    "stage_manager_result": {"message": "m", "assets": []},
    "vision_inspection": {"summary": "s"},
    "shot_expansion": {"message": "m", "shots": []},
    "section_looks": {"message": "m", "looks": []},
}


async def sent_strict_schemas() -> dict[str, dict]:
    """Every `response_format: json_schema` this client sends, captured off the wire.

    Off the wire rather than rebuilt from the pydantic models, because the wire is where
    the constrained decoder reads it and the two agree only while nobody has edited a call
    site. A schema asserted from `model_json_schema()` would have passed happily throughout
    the entire measured `shots: []` failure.
    """
    sent: dict[str, dict] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        spec = body["response_format"]["json_schema"]
        assert spec["strict"] is True, spec["name"]
        sent[spec["name"]] = spec["schema"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps(MINIMAL_REPLIES[spec["name"]])}}
                ]
            },
        )

    director = DirectorClient(
        base_url="http://llm.test/v1",
        model="local-director",
        transport=httpx.MockTransport(handler),
    )
    await director.plan(message="What would you change?", project_context={})
    await director.stage_manager(project_context={}, count=2)
    await director.expand(expansion_input={"shots": []})
    await director.inspect_image(image=b"x", mime_type="image/png", purpose="p")
    await director.section_looks(looks_input={"sections": []})
    return sent


@pytest.mark.asyncio
async def test_every_constrained_decoder_schema_declares_exactly_what_its_caller_needs():
    """The audit, expressed as assertions. **This is the guard, not the fix.**

    A Pydantic field with any default is absent from `model_json_schema()["required"]`, and
    that schema rides `response_format: json_schema strict` into LM Studio's constrained
    decoder — so a field with a default is a field the model is *correct* to omit, however
    hard the prompt asks for it. That has now bitten twice: `DirectorResult.shots`
    (empty on 2 of 3 rolls; 0 of 17 combined asks delivered both halves) and
    `PlannedShot.performance` (a whole model omitting the key on 4 of 5 rolls, 15 rolls /
    179 shots, 2026-08-20). Both were asked for in words the entire time.

    So every schema is pinned by **both** halves: the complete property set and the exact
    required list, at every level. The property set is what makes this a guard against the
    *next* one — a field added to any of these models fails this test until whoever added it
    says out loud whether the caller can proceed without it.
    """
    sent = await sent_strict_schemas()
    assert set(sent) == {
        "director_result",
        "stage_manager_result",
        "shot_expansion",
        "vision_inspection",
        "section_looks",
    }

    # ---- director_result, chat use: nothing is required beyond the prose fields. -------
    # A Director asking "what would you change about the second verse?" deserves an answer,
    # and requiring `shots` here would force the decoder to invent a shot list to close the
    # object. The route merges whatever arrives and proceeds with nothing.
    chat = sent["director_result"]
    assert set(chat["properties"]) == {
        "message",
        "treatment",
        "style_bible",
        "shots",
        "sections",
    }
    assert chat["required"] == ["message", "treatment", "style_bible"]
    assert chat["$defs"]["PlannedShot"]["required"] == ["start", "duration", "prompt"]
    assert chat["$defs"]["PlannedSection"]["required"] == ["label", "start", "duration"]

    # ---- director_result, populate's use: the caller cannot proceed without either. ----
    # `shots` — 502 and nothing written. `sections` — asked for only when unknown, and the
    # required set follows the ask. `performance` — mapped onto every written shot's
    # `singing`, so an omitted key wrote `not_singing` across the whole plan by accident.
    # `PlannedSection.prompt` — the section's shared look, which the shots inside it are
    # told to carry; omitted, the section lands blank.
    # `PlannedShot.assets` — the structural citation field. Populate builds every shot's
    # citations from it, so a decoder free to close a shot object without it is a decoder
    # free to send the plan back to the prose scan it was written to replace.
    populate = director_result_schema(require=("shots", "sections"))
    assert populate["required"] == [
        "message",
        "treatment",
        "style_bible",
        "shots",
        "sections",
    ]
    assert set(populate["$defs"]["PlannedShot"]["properties"]) == {
        "start",
        "duration",
        "prompt",
        "performance",
        "assets",
    }
    assert populate["$defs"]["PlannedShot"]["required"] == [
        "start",
        "duration",
        "prompt",
        "performance",
        "assets",
    ]
    assert set(populate["$defs"]["PlannedSection"]["properties"]) == {
        "label",
        "start",
        "duration",
        "prompt",
    }
    assert populate["$defs"]["PlannedSection"]["required"] == [
        "label",
        "start",
        "duration",
        "prompt",
    ]

    # ---- stage_manager_result: `assets` is the entire answer. --------------------------
    # An empty one is a 502 at the route ("no proposals"), so the caller cannot proceed
    # without it — and `default_factory=list` had kept it out of `required` since the day
    # it was written. Every field of a proposal was already required, having no default.
    stage = sent["stage_manager_result"]
    assert set(stage["properties"]) == {"message", "assets"}
    assert stage["required"] == ["message", "assets"]
    assert set(stage["$defs"]["AssetProposal"]["properties"]) == {"kind", "name", "prompt"}
    assert stage["$defs"]["AssetProposal"]["required"] == ["kind", "name", "prompt"]

    # ---- shot_expansion: complete by construction, and the counter-example. ------------
    # Nothing on it carries a default, so Pydantic required all of it without anyone
    # asking. This is what the other three would have looked like written the same way.
    expansion = sent["shot_expansion"]
    assert set(expansion["properties"]) == {"message", "shots"}
    assert expansion["required"] == ["message", "shots"]
    assert set(expansion["$defs"]["ExpandedShot"]["properties"]) == {"shot_id", "prompt"}
    assert expansion["$defs"]["ExpandedShot"]["required"] == ["shot_id", "prompt"]

    # ---- vision_inspection: every observation list the system prompt names. ------------
    # The caller stores all six on the asset and the inspector renders them, so an omitted
    # `risks` was recorded and displayed as "no risks found" by an inspection that never
    # considered them. An empty list is still a legitimate answer; silence is not one.
    vision = sent["vision_inspection"]
    assert set(vision["properties"]) == {
        "summary",
        "identity",
        "environment",
        "continuity_cues",
        "prompt_cues",
        "risks",
    }
    assert vision["required"] == [
        "summary",
        "identity",
        "environment",
        "continuity_cues",
        "prompt_cues",
        "risks",
    ]

    # ---- section_looks: the answer, and the per-section decision inside it. -------------
    # `looks` is the whole payload — a reply without it filled nothing — and `prompt` is the
    # field the feature exists to write; both carried defaults and were therefore outside
    # `required` for free, the third instance of the same hole. `message` stays optional
    # because the route never reads it. Note what `prompt` being required does *not* mean:
    # it has no `minLength`, so `""` is still a legal answer, and that is deliberate — the
    # decoder must make the model decide about every section, and "the treatment does not
    # describe this one" has to remain sayable or the alternative is an invented look.
    looks = sent["section_looks"]
    assert set(looks["properties"]) == {"message", "looks"}
    assert looks["required"] == ["looks"]
    assert set(looks["$defs"]["SectionLook"]["properties"]) == {
        "section_id",
        "label",
        "prompt",
    }
    assert looks["$defs"]["SectionLook"]["required"] == ["section_id", "label", "prompt"]
    assert "minLength" not in looks["$defs"]["SectionLook"]["properties"]["prompt"]
    # And the model's own schema is the counter-example it was built against: without the
    # promotion, the field the caller cannot proceed without is the one the decoder may omit.
    assert "prompt" not in SectionLooks.model_json_schema()["$defs"]["SectionLook"]["required"]


def test_a_promoted_field_the_schema_does_not_have_raises_instead_of_doing_nothing():
    """The guard on the guard.

    A promotion that silently does nothing reproduces the exact shape of the bug it exists
    to fix: a caller that believes it required a field, a decoder that was never told, and
    nothing anywhere that says otherwise. So a name that is not a field is an error, at
    both levels, and so is naming something that is not an array of objects.
    """
    with pytest.raises(ValueError, match="performance"):
        constrained_schema(DirectorResult, require=("performance",))
    with pytest.raises(ValueError, match="singing"):
        constrained_schema(DirectorResult, require_each={"shots": ("singing",)})
    with pytest.raises(ValueError, match="message"):
        constrained_schema(DirectorResult, require_each={"message": ("anything",)})
    # `risks` is an array of *strings*: it has no entry schema to promote anything into.
    with pytest.raises(ValueError, match="risks"):
        constrained_schema(VisionInspection, require_each={"risks": ("whatever",)})


def test_no_callers_required_set_can_leak_into_another():
    """Every variant is built from a deep copy, including the shared `$defs`.

    Promoting a field on `PlannedShot` mutates a nested dict, and the chat route inheriting
    populate's per-shot grammar from whichever call happened to run first would be a worse
    bug than the one being fixed — and an invisible one, because both callers would still
    get an object that validates.
    """
    hardened = director_result_schema(require=("shots", "sections"), min_shots=12)
    assert hardened["$defs"]["PlannedShot"]["required"] == [
        "start",
        "duration",
        "prompt",
        "performance",
        "assets",
    ]

    assert DirectorResult.model_json_schema()["$defs"]["PlannedShot"]["required"] == [
        "start",
        "duration",
        "prompt",
    ]
    fresh = director_result_schema()
    assert fresh == DirectorResult.model_json_schema()
    assert fresh["$defs"]["PlannedShot"]["required"] == ["start", "duration", "prompt"]
    assert fresh["$defs"]["PlannedSection"]["required"] == ["label", "start", "duration"]
    assert "minItems" not in fresh["properties"]["shots"]

    # And the same for the other two models the builder is now used on.
    assert constrained_schema(StageManagerResult, require=("assets",))["required"] == [
        "message",
        "assets",
    ]
    assert StageManagerResult.model_json_schema()["required"] == ["message"]
    assert VisionInspection.model_json_schema()["required"] == ["summary"]


def test_the_builder_never_mutates_the_schema_it_was_handed():
    """The deep copy, pinned against the one thing that makes it look unnecessary.

    Pydantic rebuilds `model_json_schema()` on every call today, so *deleting* the copy
    changes no observable behaviour and no ordinary test can catch it — the previous test
    passes either way. That is precisely why this one exists: the day Pydantic caches the
    schema, or the day a caller hands in one it holds, the copy is the only thing standing
    between populate's hardened grammar and the chat route's — and the failure would be
    silent, because both callers would still receive an object that validates.

    So the model is stood in for by something that *does* return the same dict every time,
    which is the shape the copy defends against.
    """
    cached = DirectorResult.model_json_schema()
    before = deepcopy(cached)

    class CachedSchemaModel:
        @staticmethod
        def model_json_schema() -> dict:
            return cached

    hardened = constrained_schema(
        CachedSchemaModel,
        require=("shots",),
        require_each={"shots": ("performance",)},
        min_items={"shots": 12},
    )

    assert hardened["required"][-1] == "shots"
    assert hardened["$defs"]["PlannedShot"]["required"][-1] == "performance"
    assert hardened["properties"]["shots"]["minItems"] == 12
    # And not one byte of what it was handed moved.
    assert cached == before
