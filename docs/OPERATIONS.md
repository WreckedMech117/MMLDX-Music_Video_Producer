# Operations and Recovery

## Start order

1. Start the existing portable ComfyUI using its normal user-managed launcher.
2. Confirm `http://127.0.0.1:8188/system_stats` responds.
3. Start Music Video Producer with `start-music-video-producer.bat`.
4. Confirm the top-right ComfyUI indicator turns lime and says **ComfyUI ready**.

Music Video Producer never starts, stops, restarts, interrupts, or kills ComfyUI.

## Environment

Copy `.env.example` to `.env` and adjust values. `.env` is ignored and must not be committed.

Key settings:

- `MVP_COMFY_URL`
- `MVP_COMFY_ROOT`
- `MVP_DATA_ROOT`
- `MVP_LLM_BASE_URL`
- `MVP_LLM_MODEL`
- `MVP_LLM_API_KEY`
- `MVP_MAX_UPLOAD_BYTES` (default 2 GiB)

Copy `.env.example` to the ignored `.env` file before startup. Editing the example alone does not affect runtime settings.

LM Studio supports JSON-schema structured output rather than the older `json_object` response mode. Music Video Producer sends the validated Director schema and, when LM Studio exposes a loaded instance as `model-name:N`, automatically reuses that instance instead of trying to load a duplicate copy.

## Backups

Back up both:

1. `F:\MusicVideoProducer\data\projects`
2. Relevant outputs under `J:\Hermes-Remote\comfyui\ComfyUI_windows_portable\ComfyUI\output\music-video-producer`

Project manifests reference Comfy outputs; backing up manifests alone preserves decisions but not generated media.

## Recover a project

- Restore its complete `<project-id>` directory beneath `data/projects`.
- Restore referenced Comfy output subfolders.
- Restart only Music Video Producer, not ComfyUI.
- The project list is rebuilt by scanning `*/project.json`.

Malformed manifests are skipped during list operations rather than crashing the entire application. Inspect and repair the JSON from backup.

## Queue recovery

Jobs persist their Comfy prompt IDs. Use **Queue → Refresh** after an application restart. The backend reads `/history/<prompt-id>` and updates completion, outputs, or exact execution errors.

Comfy history can be cleared independently. If a prompt ID no longer exists, the job remains queued until a future reconciliation policy marks it stale; current code does not invent a completion.

## Troubleshooting

### ComfyUI offline

- Check the configured URL.
- Check port ownership before launching another server.
- Do not terminate an unknown process on 8188.

### Director unavailable

This is expected until an OpenAI-compatible endpoint and model are configured. The application returns a truthful 503 and keeps treatment editing available.

### Director says a document was not replaced

Working as intended. `director.document_rejection()` refuses a Treatment or Style Bible that parses as JSON, or that collapses below 40% of the length of the document it would replace. The chat reply states which document was kept, why, and includes the raw model output.

The failure this guards against is self-reinforcing: the whole project is sent as context, so once a Style Bible has been stored as JSON the model keeps returning JSON. The guard blocks the bad write but cannot repair an already-corrupted document — edit the Style Bible by hand once to break the loop, after which responses return to prose.

### Workflow rejected

- Ensure the user-managed 8188 instance was restarted after custom-node installation.
- Check `/object_info` for the class type named in the Comfy error.
- Check exact model filenames against `docs/WORKFLOW-MAP.md`.
- Do not submit editor JSON directly to `/prompt`.

### Distinguishing a running render from a waiting one

Job refresh reads `/history/<prompt-id>`, and ComfyUI writes no history entry until a prompt finishes. History alone therefore cannot tell an executing render from a pending one, and before this was fixed a twelve-minute H3 render reported `queued` throughout.

Refresh now consults `/queue` whenever history is still empty, so an executing render reports `running`. If a job appears stuck, confirm against the ComfyUI server directly: the running entry carries the same prompt ID, and free VRAM drops sharply while the model stack is resident.

### Checking models with /object_info

ComfyUI 0.33.1 returns combo inputs as `["COMBO", {"options": [...]}]`. Older code that reads the option list from index `0` gets the string `"COMBO"` and silently reports every model as missing. Read options from `[1]["options"]`, and treat a "everything is missing" result as a parser bug before concluding the models are absent.

### Media preview missing

Refresh the corresponding job first. Generated media paths are copied from Comfy history only after completion.

### Imported song will not play

Imported songs are served from the project-contained media endpoint with byte-range support and loaded into the persistent `master-audio` element. Native controls, the header transport, and the timeline transport share one playhead. If browser decoding cannot determine duration before upload, the backend uses `ffprobe` so duration remains available after restart.

### LTX VAE shape mismatch after SeedVR2

SeedVR2 preserves aspect ratio and can emit dimensions that are not valid for the downstream LTX VAE. The observed run completed 192 SeedVR2 frames at 1250×720, then failed at LTX `VAEEncode` because 1250 is not divisible by four. The audited reference adapter inserts a KJ resize after SeedVR2 with `width=0`, `height=0`, and `divisible_by=16`, which produces 1248×720 without changing the intended aspect materially. Standalone LTX submission remains disabled until it accepts an approved take rather than creator-specific source media.

Keep optional `PathchSageAttentionKJ` nodes bypassed unless a compatible `sageattention` installation has been verified.

### Isolated first-run browser QA

Run the app on port 8766 with an empty temporary data root, then execute:

```bash
MVP_APP_PORT=8766 MVP_DATA_ROOT="$LOCALAPPDATA/Temp/mvp-e2e-data" uv run python run.py
uv run --with selenium python tests/e2e_first_run.py http://127.0.0.1:8766
uv run --with selenium python tests/e2e_audio_playback.py http://127.0.0.1:8766
```

The test creates a project, enters it, visits every workspace, captures browser logs, and writes artifacts under `test-artifacts/`.

## Quality gates

```bash
uv run pytest -q
uv run ruff check .
node --check src/music_video_producer/web/assets/app.js
```
