import json

import httpx
import pytest

from music_video_producer.comfy import ComfyClient, ComfyError


@pytest.mark.asyncio
async def test_comfy_client_health_and_submit_prompt():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"system": {"comfyui_version": "0.33.1"}})
        if request.url.path == "/prompt":
            assert json.loads(request.content)["prompt"]["1"]["class_type"] == "SaveImage"
            return httpx.Response(200, json={"prompt_id": "prompt-123", "number": 9})
        return httpx.Response(404)

    client = ComfyClient("http://comfy.test", transport=httpx.MockTransport(handler))

    assert (await client.health())["online"] is True
    submitted = await client.submit({"1": {"class_type": "SaveImage", "inputs": {}}})
    assert submitted.prompt_id == "prompt-123"


@pytest.mark.asyncio
async def test_comfy_client_translates_downstream_errors():
    client = ComfyClient(
        "http://comfy.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(500, text="broken")),
    )

    with pytest.raises(ComfyError, match="500"):
        await client.queue()


@pytest.mark.asyncio
async def test_comfy_client_translates_invalid_json_responses():
    client = ComfyClient(
        "http://comfy.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="not-json")),
    )

    with pytest.raises(ComfyError, match="invalid JSON"):
        await client.submit({"1": {"class_type": "SaveImage", "inputs": {}}})


@pytest.mark.asyncio
async def test_comfy_client_reads_execution_error_from_history():
    payload = {
        "p1": {
            "status": {
                "status_str": "error",
                "messages": [["execution_error", {"node_type": "KSampler", "exception_message": "OOM"}]],
            },
            "outputs": {},
        }
    }
    client = ComfyClient(
        "http://comfy.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )

    result = await client.history("p1")

    assert result.status == "error"
    assert result.error == "KSampler: OOM"


@pytest.mark.asyncio
async def test_comfy_client_normalizes_successful_history_status():
    payload = {
        "p1": {
            "status": {"status_str": "success", "messages": []},
            "outputs": {"9": {"images": [{"filename": "done.png", "subfolder": "mvp"}]}},
        }
    }
    client = ComfyClient(
        "http://comfy.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )

    result = await client.history("p1")

    assert result.status == "complete"
    assert result.outputs[0]["filename"] == "done.png"
