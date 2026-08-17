import json

import httpx
import pytest

from music_video_producer.director import DirectorClient, DirectorUnavailable


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
