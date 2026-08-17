import asyncio
import json
from pathlib import Path

from music_video_producer.config import Settings
from music_video_producer.director import DirectorClient

IMAGE = Path(
    r"J:\Hermes-Remote\comfyui\ComfyUI_windows_portable\ComfyUI\output"
    r"\music-video-setup\krea2_smoke_00001_.png"
)


async def main() -> None:
    settings = Settings()
    client = DirectorClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        timeout=180,
    )
    try:
        result = await client.inspect_image(
            image=IMAGE.read_bytes(),
            mime_type="image/png",
            purpose="character multiview consistency sheet",
        )
        print(json.dumps(result.model_dump(), indent=2))
    finally:
        await client.close()


asyncio.run(main())
