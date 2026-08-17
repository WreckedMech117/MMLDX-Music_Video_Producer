import json

import httpx
import pytest

from music_video_producer.director import (
    EXPANSION_SYSTEM_PROMPT,
    DirectorClient,
    DirectorError,
    DirectorUnavailable,
    ExpandedShot,
    ShotExpansion,
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


def test_the_expansion_prompt_describes_every_key_the_input_actually_carries():
    """A payload whose semantics the model must infer is a variance mechanism that is hoped for.

    `song_fraction` and `neighbours` exist to drive deliberate cross-shot variance, which is the
    whole point of the story, and `locked` is only worth sending if something asks the model to
    skip locked Shots. Driven off a real `expansion_input` payload rather than a hand-written
    list, so a key added to the builder without a line in the prompt fails here.
    """
    project = Project(name="Prompted")
    project.song = Song(title="Spine", source="imported", duration=120)
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
