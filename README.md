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

Live GPU smokes are separate and deliberately manual — they are not pytest-collected and are not part of the gate set above, because each one spends real GPU minutes on your ComfyUI:

```bash
uv run python tests/smoke_songplanner_app.py http://127.0.0.1:8766 --confirm-gpu
uv run python tests/smoke_h3_app.py http://127.0.0.1:8766 --confirm-gpu
```

`smoke_songplanner_app.py` refuses to submit without `--confirm-gpu`, generates two short songs (invented then known-lyrics), and **creates one project per adapter** — a shared project's second run would clobber the first, since every music job targets the same `"song"` slot. Its per-variant JSON on stdout is the only record of which adapter produced which prompt, so keep it. `smoke_h3_app.py` is gated the same way and runs the H3 pre-flight audit before submitting. Full procedure in `docs/OPERATIONS.md`.

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

- [Agent instructions](AGENTS.md) — rules for AI agents working in this repository
- [Architecture](docs/ARCHITECTURE.md)
- [Workflow map](docs/WORKFLOW-MAP.md)
- [Data model](docs/DATA-MODEL.md)
- [Operations and recovery](docs/OPERATIONS.md)
- [LLM Director](docs/LLM-DIRECTOR.md)
- [Roadmap and verification status](docs/ROADMAP.md)
- [Development log](docs/DEVELOPMENT-LOG.md)
- [Implementation plan](.hermes/plans/2026-08-16_153000-music-video-producer.md)

## Honest status

The application currently runs real API-format adapters for Music 3, both SongPlanner song generators, Flux, Krea multiview, and text-only H3 Director shots.

Three routes are verified end to end from this application. The text-only H3 path: on 2026-08-16 a 3.75 second shot rendered through live ComfyUI 0.33.1 and returned a 90-frame video with synchronized audio. Both SongPlanner paths: on 2026-08-17 the invented-lyrics and known-lyrics adapters each produced a real song, measured by `ffprobe` at 29.989 seconds of 44.1 kHz stereo FLAC from a 30 second request — the model resolves its own length, and 30 seconds is the shortest song the workflow accepts. For both songs the ComfyUI `/view` URL the player uses returned decodable FLAC; a browser was not driven, so browser playback of a generated song is not claimed. Every other route is built and unit-tested but has not produced a real render from this application. Post-processing, final assembly, BPM/section analysis, and multi-take approval remain planned. Their controls are disabled or explicitly scoped rather than presented as complete.

The LTX 2.5 enhancement chain deserves its own sentence, because the honest answer is "the chain works, the app cannot drive it yet." On 2026-08-17 the full reference chain — H3 → SeedVR2 → dimension normalization → LTX 2.5 → FILM → RTX VSR — ran to success on live ComfyUI in 17 minutes 36 seconds, ending the failure that had killed three previous runs at the LTX `VAEEncode`. `ffprobe` measured the LTX stage at 2496×1408, exactly twice the normalized 1248×704, which is what confirms the boundary fix. But that run was submitted directly to ComfyUI against the audited reference graph, which regenerates the shot from creator-specific media. **Standalone LTX enhancement from this application is still not implemented** — it needs an adapter that takes an already-approved take as its input, which is roadmap item 7. So the chain is proven; the product feature is not.

Changing a project's song is now gated, because the song is the timing spine: shot windows are absolute seconds against it and Assembly synchronization derives from it. Once a project has both a song and shots, re-import, direct Music 3 generation, and SongPlanner generation all refuse with HTTP 409 unless the request carries `confirm_song_replacement`, and the refusal says what depends on the song. A first import, and any project with no shots, stays frictionless. `DELETE /api/projects/{id}/song` is a first-class removal path behind the same gate: it clears the project's reference, keeps every shot exactly as it is, and leaves the audio file on disk — removal detaches, it does not destroy media. No song operation adds, removes, or alters a shot field, and that is asserted by re-reading the manifest through a fresh store and comparing shots field-for-field. The generic full-project `PUT /api/projects/{id}` is gated too, in the narrow form that keeps ordinary saves working: it refuses only when the incoming song differs from the stored one and the project has shots, so a save carrying an unchanged song passes untouched. One limitation is stated rather than half-fixed: that same `PUT` can still wipe **shots** while carrying an unchanged song — protecting shot data through the generic save is a separate concern from song-replacement safety. The confirmations themselves are covered by offline tests only — the new "Remove song" control and the browser confirm dialogs have not been driven in a real browser.
