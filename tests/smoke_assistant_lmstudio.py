"""One live Assistant ProducerBot turn against LM Studio, without starting the application.

`smoke_director_lmstudio.py`'s shape, for the call this feature adds. It is the cheapest way to
answer the three questions the suite cannot: does the loaded model **call the tool** rather than
answering in prose, does it **copy the asset ids** it was given rather than inventing them, and
does it write a prompt or transcribe the material it was handed.

Nothing is written anywhere. It builds a throwaway `Project` in memory, so no manifest is touched,
no route is called, and no GPU time is spent beyond the language model's own.

    uv run python tests/smoke_assistant_lmstudio.py

Requires a tool-capable model loaded in LM Studio and `MVP_LLM_MODEL` naming it.
"""

import asyncio
import json

from music_video_producer.config import Settings
from music_video_producer.director import DirectorClient
from music_video_producer.models import Asset, Project, Shot, Song
from music_video_producer.timeline import assistant_input

#: The Director's own sentence from the spec, verbatim, because the point of this probe is the
#: interaction they described rather than a prompt written to make the model look good.
REQUEST = "make that shot a B-roll of a grey wolf walking through a forest"


def probe_project() -> Project:
    """A project in the state the assistant is *for*: a library and a rough plan already exist."""
    project = Project(name="Assistant smoke")
    project.creative_brief = "A night drive that opens out into wilderness."
    project.treatment = "Three movements: the corridor, the threshold, the forest."
    project.style_bible = "Sodium amber, hard backlight, 35mm grain, handheld."
    project.song = Song(
        title="Signal Bloom", source="imported", duration=60, caption="Slow synth rock"
    )
    project.assets = [
        Asset(id="asset_wolf", name="Grey wolf", kind="prop", path="media/wolf.png"),
        Asset(id="asset_forest", name="Pine forest at dusk", kind="setting", path="media/forest.png"),
        Asset(id="asset_lead", name="Lead vocalist", kind="character", path="media/lead.png"),
    ]
    project.shots = [
        Shot(id="shot_probe", start=12, duration=5, prompt="New shot"),
        Shot(id="shot_after", start=17, duration=6, prompt="New shot"),
    ]
    return project


async def main() -> None:
    settings = Settings()
    project = probe_project()
    payload = assistant_input(project, shot_ids=["shot_probe"])
    client = DirectorClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        timeout=600,
    )
    try:
        turn = await client.assist(message=REQUEST, assistant_input=payload)
        print(
            json.dumps(
                {
                    "model": settings.llm_model,
                    # The one that decides whether the feature works at all.
                    "called_the_tool": bool(turn.fills),
                    "message": turn.message[:400],
                    "fills": [fill.model_dump(exclude_none=True) for fill in turn.fills],
                    # Anything here is a call the taxonomy refused. The route reports these as a
                    # notice with the raw arguments beside it; seeing them here is how the system
                    # prompt gets iterated.
                    "refused_by_the_taxonomy": turn.malformed,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    finally:
        await client.close()


asyncio.run(main())
