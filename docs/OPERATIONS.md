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

SeedVR2 preserves aspect ratio and can emit dimensions that are not valid for the downstream LTX VAE. The observed run completed 192 SeedVR2 frames at 1250×720, then failed at LTX `VAEEncode` with `einops.EinopsError: can't divide axis of length 1250 in chunks of 4`. The LTX 2.5 video VAE sets `crop_input=False`, so unlike LTX 2.3 nothing auto-corrects the size.

The fix is a KJ resize after SeedVR2 with `width=0`, `height=0`, and `divisible_by=32`, which produces 1248×704. **Use 32, not 16.** The VAE's total spatial compression is 32 (4-pixel patchify plus three stride-2 stages); 16 gives 1248×720 and 720/32 = 22.5, which clears the patchify check but pushes a half cell through the conv stack. The resize must feed every LTX image consumer — `VAEEncode`, `LTXVImgToVideoInplace`, and `GetImageSize` — not just the encoder.

**Verified live 2026-08-17.** Prompt `a64a0460-64e6-4a14-b207-e644bf9bda5d` ran the full reference chain to `success` in 17 min 36 s with no errors. `ffprobe` on the outputs: H3 1056×608 / 192 frames → SeedVR2 1250×720 / 192 frames → LTX 2.5 2496×1408 / 185 frames → FILM + RTX VSR 3744×2112 / 369 frames at 48 fps. The LTX subgraph's 2× latent upsample makes 2496×1408 exactly 2 × 1248×704, so the produced file is the evidence the divisor is right.

**Trap: the boundary does not preserve frame count — 192 in, 185 out.** LTX lands on an 8k+1 grid (185 = 8 × 23 + 1) just as H3 lands on 17k+5. Two consequences when operating this chain: assembly trim math must handle both grids, and a verification step must never assert that LTX output has the same frame count as its input — a shrink is correct behaviour here, not a dropped-frame bug. Measured durations shorten with it: 8.000 s in, 7.708 s out.

**Aspect: use `keep_proportion: "crop"`, not `"resize"`.** The Director ruled on 2026-08-17 that geometry is preserved and trimmed pixels are the acceptable price. `"resize"` resamples straight to the target (`crop="disabled"`) and squashes 1250×720 into 1248×704 — a 2.07% anamorphic stretch. `"crop"` centre-crops to the target aspect first (1250×705, 15 rows split 7 top / 8 bottom) then resamples 705 → 704, leaving 0.02% residual distortion.

**Both settings produce exactly 1248×704, so you cannot tell them apart from dimensions.** When checking this graph, read the `keep_proportion` widget on node `6133` — a size check will pass either way. `crop_position` must be `center` so the trim is split between top and bottom rather than taken off one edge.

**Sub-divisor frames fail differently now.** An axis below 32 floors to 0 and raises `ZeroDivisionError` under crop mode, where resize mode raised `ValueError: height and width must be > 0`. If you see a bare `ZeroDivisionError` out of `ImageResizeKJv2`, the input frame is smaller than one divisor cell — pass an explicit normalized size rather than letting the node derive it.

Both the repo adapter (`patch_ltx25_dimension_boundary`) and the Director's saved workflow `04 - H3 Music Video - LTX 2.5 READY.json` carry divisor 32. The audited reference export still shows the pre-fix wiring by design; the patch is applied in memory. Standalone LTX submission from the application remains disabled until it accepts an approved take rather than creator-specific source media.

Keep optional `PathchSageAttentionKJ` nodes bypassed unless a compatible `sageattention` installation has been verified — `sageattention` is not installed in ComfyUI's embedded Python, and an enabled node aborts the run with `ModuleNotFoundError: sageattention`.

### Isolated first-run browser QA

Run the app on port 8766 with an empty temporary data root, then execute:

```bash
DATA_ROOT="$LOCALAPPDATA/Temp/mvp-e2e-data"
MVP_APP_PORT=8766 MVP_DATA_ROOT="$DATA_ROOT" uv run python run.py
uv run --with selenium python tests/e2e_first_run.py http://127.0.0.1:8766
uv run --with selenium python tests/e2e_audio_playback.py http://127.0.0.1:8766
uv run --with selenium python tests/e2e_epic2_surfaces.py http://127.0.0.1:8766 "$DATA_ROOT"
```

Each script creates a project, drives it, captures browser logs, fails on any `SEVERE` console entry, and writes artifacts under `test-artifacts/`.

`e2e_first_run.py` visits every workspace. `e2e_audio_playback.py` imports a synthesised WAV, reloads, and asserts playback actually advances. `e2e_epic2_surfaces.py` takes the data-root path as a second argument because it seeds a Director reply by writing the manifest directly — notices can only be produced by a live model, and `PUT /api/projects/{id}` deliberately refuses client-supplied messages, so writing the file is the only way to fixture one without a model.

Note the ordering hazard, which used to make this runbook wrong: `e2e_audio_playback.py` originally waited unconditionally for the new-project dialog, which the app opens only when the data root holds no projects. Run after `e2e_first_run.py` — exactly what this list tells you to do — it timed out and the audio gate silently never ran. It now opens the dialog itself when one is not already open, so the scripts can be run in sequence against one root.

### Live GPU smokes (manual, cost real GPU minutes)

These are not pytest-collected and are never run as part of the automated suite. Each spends real GPU minutes on the user-managed ComfyUI. Run from the repo root with the app already serving and ComfyUI already up — neither script ever starts or stops ComfyUI.

```bash
uv run python tests/smoke_songplanner_app.py http://127.0.0.1:8766 --confirm-gpu
uv run python tests/smoke_h3_app.py http://127.0.0.1:8766
```

`smoke_songplanner_app.py` **refuses to submit anything without `--confirm-gpu`** and exits with a usage message instead. It re-runs the `tests/preflight_songplanner.py` audit against the live ComfyUI, then submits exactly two short songs — invented variant first, then known-lyrics. It **creates one project per adapter**, because every `kind=music` job targets `"song"` and a shared project's second run would clobber the first. It prints exactly one JSON block per variant to stdout, and that block is the only record of which adapter produced which prompt ID — nothing in persisted state distinguishes a SongPlanner song from a direct Music 3 song, so capture the output. It `ffprobe`s both the file on disk and the ComfyUI `/view` URL the player actually fetches. Requested duration is not produced duration; the encoder resolves its own length, and the script reports the delta as a fact but fails a variant whose measurement is wildly off the request. It aborts non-zero on stderr — without spending a further generation — on pre-flight problems, ComfyUI being offline, an unexpected response shape, a job error, or the time ceiling.

`smoke_h3_app.py` has **no cost gate**: it takes only an optional base URL and submits an H3 render as soon as it is invoked. Treat running it as the confirmation. Bringing it behind the same `--confirm-gpu` flag is an open cleanup, tracked in `docs/ROADMAP.md`.

## Quality gates

```bash
uv run pytest -q
uv run ruff check .
node --check src/music_video_producer/web/assets/app.js
```

The live GPU smokes above are **not** part of this gate set; they are run deliberately, by a human, with `--confirm-gpu`.
