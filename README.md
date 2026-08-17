# Music Video Producer

A standalone, local-first production editor for creating songs and complete AI-assisted music videos with the existing portable ComfyUI installation.

> This project is intentionally independent of Agent OS. It contains no Agent OS imports, routes, data paths, or runtime dependency.

## Current working vertical slice

- Create persistent production projects.
- Import WAV, FLAC, and MP3 masters with browser-decoded duration and waveform.
- Create songs through the installed MiniMax Music 3 pipeline using caption, lyrics, duration, and seed.
- Develop a treatment with a configurable OpenAI-compatible LLM; structured results become editable treatments, style bibles, and shots.
- Generate character, setting, prop, and style images through the saved `Flux-Image-Gen.json` model stack.
- Upload visual assets into a project library.
- Promote an approved character to the installed Krea 2 QuadView multiview workflow.
- Add, drag, resize, split, duplicate, delete, and inspect song-level shots.
- Attach project assets to shots and compile shot windows into Director timeline data.
- Persist ComfyUI prompt IDs, seeds, job states, outputs, and errors.
- Monitor Music 3, Flux, and Krea jobs without starting or stopping the user's ComfyUI process.
- Queue reviewed text-only H3 Director shots and persist their latest output separately from approval.
- Queue MiniMax H3 Ultra reference shots with up to nine ordered pictures, three videos, and three audio references, including the project master song.
- Inspect character/environment references and generated takes with the configured LM Studio vision model; persist continuity cues and risks without auto-approving outputs.
- Play imported or generated master audio through native, global, and timeline transports; seek from the waveform and restore playback after reload.
- Protect project/media paths, bound uploads, serialize shot saves, and reject stale full-project replacement.

## Start

1. Ensure the portable ComfyUI instance is running at `http://127.0.0.1:8188`.
2. Double-click `start-music-video-producer.bat`.
3. The application opens at `http://127.0.0.1:8765`.

Manual start:

```bash
cd F:/MusicVideoProducer
uv sync
uv run python run.py
```

## Configure

Copy `.env.example` to `.env`. The defaults point to:

```text
J:\Hermes-Remote\comfyui\ComfyUI_windows_portable\ComfyUI
http://127.0.0.1:8188
```

Director chat remains visibly unavailable until `MVP_LLM_BASE_URL` and `MVP_LLM_MODEL` are configured. API keys are read only from `.env` or environment variables; `.env` is ignored by Git.

## Test

```bash
uv run pytest -q
uv run ruff check .
node --check src/music_video_producer/web/assets/app.js
```

First-run browser QA uses an empty isolated data root and Microsoft Edge:

```bash
uv run --with selenium python tests/e2e_first_run.py http://127.0.0.1:8766
uv run --with selenium python tests/e2e_audio_playback.py http://127.0.0.1:8766
```

See `docs/OPERATIONS.md` for the isolated-server setup used by that test.

## Project storage

Each production is stored under:

```text
data/projects/<project-id>/
  project.json
  media/
    songs/
    assets/
```

`project.json` is the recoverable source of truth for creative documents, songs, assets, shots, render jobs, prompt IDs, seeds, and approved outputs. ComfyUI output media remains in ComfyUI's output tree and is referenced by relative path.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Workflow map](docs/WORKFLOW-MAP.md)
- [Data model](docs/DATA-MODEL.md)
- [Operations and recovery](docs/OPERATIONS.md)
- [LLM Director](docs/LLM-DIRECTOR.md)
- [Roadmap and verification status](docs/ROADMAP.md)
- [Development log](docs/DEVELOPMENT-LOG.md)
- [Implementation plan](.hermes/plans/2026-08-16_153000-music-video-producer.md)

## Honest status

The application currently runs real API-format adapters for Music 3, Flux, Krea multiview, and text-only H3 Director shots. H3 reference assets/audio, standalone LTX enhancement, post-processing, final assembly, BPM/section analysis, and multi-take approval remain planned. Their controls are disabled or explicitly scoped rather than presented as complete.
