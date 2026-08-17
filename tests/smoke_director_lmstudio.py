import asyncio
import json

from music_video_producer.config import Settings
from music_video_producer.director import DirectorClient


async def main() -> None:
    settings = Settings()
    client = DirectorClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        timeout=180,
    )
    try:
        result = await client.plan(
            message="Create one five-second opening shot of a singer under a single amber warehouse light.",
            project_context={"name": "Director smoke", "song": {"duration": 5}, "shots": []},
        )
        print(
            json.dumps(
                {
                    "configured": bool(settings.llm_base_url and settings.llm_model),
                    "model": settings.llm_model,
                    "shot_count": len(result.shots),
                    "first_duration": result.shots[0].duration if result.shots else None,
                    "message": result.message[:160],
                }
            )
        )
    finally:
        await client.close()


asyncio.run(main())
