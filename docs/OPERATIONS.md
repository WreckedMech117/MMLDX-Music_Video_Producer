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
- `MVP_LLM_EJECT_BEFORE_RENDER` (**default on**) — pins the value at startup; the interface can change it thereafter, and does not override this variable across a restart. Plus `MVP_LLM_EJECT_EXECUTABLE` and `MVP_LLM_EJECT_TIMEOUT` (default 20 s)

Copy `.env.example` to the ignored `.env` file before startup. Editing the example alone does not affect runtime settings.

## VRAM: ejecting the language model before a render

The Director's language model and ComfyUI compete for the same VRAM, and an idle LM Studio can hold tens of gigabytes. Immediately before any payload is POSTed to `/prompt`, the application asks LM Studio to release whatever is resident, then **re-reads `GET /api/v1/models` to confirm it actually went**. A command that exits 0 while a model stays resident is reported as a failure, because that outcome is indistinguishable from doing nothing and the VRAM is the entire point.

It is on by default and **never fails a render**. Every failure path — no LM Studio, no `lms` CLI, a non-zero exit, a timeout, an unreadable listing, or a release that did not happen — logs once and submits anyway. It is skipped silently when nothing is loaded, and skipped while a Director call is in flight, because a render is not worth cancelling a call the Director is waiting on. A later Director call reloads the model on demand; `director.py` already handles LM Studio's "Model is unloaded" 400 by retrying against the loaded instance.

Watch it with `INFO` on `music_video_producer.vram`:

```text
INFO:     music_video_producer.vram - VRAM eject before render: released <model> (lms CLI reported: ...)
```

**There is a visible control** in the topbar beside the ComfyUI status. Unticking it stops the eject from the next submission onward, with no restart, and it reports what the last submission actually did — `Last render: released <model>`, or `no eject was attempted`. Worth turning off if you deliberately run a small model alongside renders and would rather keep it warm.

**Precedence, because the two can disagree: the environment decides how the application starts, the control decides what happens after.** An explicitly set `MVP_LLM_EJECT_BEFORE_RENDER` — from the environment, from `.env`, or passed to `Settings` — wins at startup over any stored choice; otherwise the last stored choice wins over the built-in default. A change made in the interface applies immediately and is stored, but the next start re-applies that same order, so **with the variable pinned, a change made here does not survive a restart**. The control's hover text says so rather than leaving you to discover it. The alternative — a stored choice permanently overriding the variable — was rejected because it makes a startup file silently inert. Note that "explicitly set" is read from pydantic's `model_fields_set`, not by comparing against the default: `MVP_LLM_EJECT_BEFORE_RENDER=1` and no variable at all are the same value and mean different things.

The choice is stored in `data/machine-preferences.json`, a sibling of `projects/` and **never** inside a project manifest — it describes this machine's card, not the video, and a shared project carrying "do not eject" would silently change how someone else's renders behave. A missing, unreadable or wrong-typed value reads as *no choice recorded* rather than as "off", so a corrupt file cannot quietly disable the eject.

**No free-VRAM figure is shown, deliberately.** Story 4.1 asked for one; it was dropped after measurement. Across one eject of a 4.71 GB model the reading fell 31.6 → 16.0 GB, because ComfyUI released its own cache at the same moment — a number that looks like evidence and is not. What the interface reports instead is which models were resident and whether they are gone, which is directly observed.

**The mechanism is the vendor CLI, and that is a considered choice rather than a first guess.** LM Studio's HTTP surface cannot be probed for an unload route: every unknown path returns **HTTP 200** with an error body, and a `GET` against a real `POST` route is indistinguishable from a `GET` against a path that does not exist, so no read-only probe can establish whether a REST unload exists. `lms.exe` itself contains no `api/v0` or `api/v1` route strings but does contain `ws://127.0.0.1:1234` and `unloadModel` — the vendor unloads over WebSocket RPC, not REST. The CLI sits behind a three-line protocol, so swapping in a proven REST or WebSocket unload later is one new class; the verification above never trusted the mechanism in the first place.

The practical consequence for planning a session is unchanged and still worth following by hand: do the text-heavy work — treatment, style bible, shot expansion — in one pass up front, so the model loads once rather than being evicted and reloaded around every render.

LM Studio supports JSON-schema structured output rather than the older `json_object` response mode. Music Video Producer sends the validated Director schema and, when LM Studio exposes a loaded instance as `model-name:N`, automatically reuses that instance instead of trying to load a duplicate copy.

## Backups

Back up both:

1. `F:\MusicVideoProducer\data\projects`
2. Relevant outputs under `J:\Hermes-Remote\comfyui\ComfyUI_windows_portable\ComfyUI\output\music-video-producer`

Project manifests reference Comfy outputs; backing up manifests alone preserves decisions but not generated media.

`data/machine-preferences.json` is deliberately **not** on that list. It holds choices that describe this machine — currently the VRAM eject toggle — and restoring it onto different hardware is not meaningful. Losing it costs nothing: every value in it falls back to its documented default.

## Recover a project

- Restore its complete `<project-id>` directory beneath `data/projects`.
- Restore referenced Comfy output subfolders.
- Restart only Music Video Producer, not ComfyUI.
- The project list is rebuilt by scanning `*/project.json`.

Malformed manifests are skipped during list operations rather than crashing the entire application. Inspect and repair the JSON from backup.

## Queue recovery

Jobs persist their Comfy prompt IDs. Use **Queue → Refresh** after an application restart. The backend reads `/history/<prompt-id>` and updates completion, outputs, or exact execution errors.

Comfy history can be cleared independently. If a prompt ID no longer exists, the job remains queued until a future reconciliation policy marks it stale; current code does not invent a completion.

## Mark a shot ready

`POST .../shots/{shot_id}/mark-ready` and `.../mark-draft`, both bodyless, plus a control in the shot inspector. This is how a shot reaches its **first** render. Until 2026-08-18 it had no path at all: `Shot.status` defaults to `draft`, the frontend only ever *read* `status === "ready"` to decide what the queue button may submit, and nothing in the interface ever wrote it — so every live render in this project's history was driven by a script or an API client.

Refusals, in order: a render **in flight** (409, read from the job records as well as the status, so a hand-edited status cannot hide one); a shot that has **already rendered** — that is render-again's transition, and the message says so rather than sending you to a route that would refuse you too; a **locked** shot; a shot carrying an **approval**, which is reachable even on a `draft` shot and would otherwise route around render-again's approval argument entirely; and finally the **prompt gate**, which is `batch.prompt_rejection` — the same judgement a render makes, not a second opinion that could drift from it.

Going back to `draft` has **no prompt gate**: un-committing a shot whose prompt you just emptied is exactly what you would want to do.

**Marking ready is not a certificate.** The render's own gate asks again, so a shot marked ready and then emptied is still refused at submission. Marking a `ready` shot ready again is a no-op that writes nothing — but if its prompt has since been emptied it refuses and names the prompt, rather than silently succeeding.

**Nothing auto-marks.** Not the Director applying a shot plan, not expansion writing prompts. Running expansion to see what the model suggests must never silently arm a whole plan for rendering. That is enforced by a test, not just by absence.

## Render a shot again

`POST /api/projects/{id}/shots/{shot_id}/render-again`, and a control in the shot inspector, re-open a settled shot for one more submission by writing exactly one field — `status` back to `ready`. Before this existed, comparing two takes meant hand-editing status through the generic shots route with an API client, which is what had to be done on 2026-08-18 to compare the two sampling profiles.

This is the same policy as **Mark a shot ready** above, on the other side of the first render — the two are a pair, not separate rules. The readiness gate is **not** a "render once" rule, it is a "do not render nonsense" rule, so re-opening a shot that already satisfied it is not a bypass. The prompt check is asked **again** from the prompt as it stands at that moment, not remembered from the first render — a prompt edited to nothing, or back to the `"New shot"` placeholder, is refused exactly as a first render would be.

Refusals, in the order they are checked: a job already **in flight** for that shot (409, decided from both the status *and* the job records, because a hand-walked-back status hides it); a shot that was never rendered (nothing to do); a **locked** shot; an **approved** take; and finally the prompt gate. The approval refusal is about meaning rather than mechanics — re-rendering over an approved take would leave the approval describing something that no longer exists.

**On the previous take: the application does not track takes.** ComfyUI writes numbered outputs (`_00001`, `_00002`), so the earlier file survives on disk; what changes is that `Shot.latest_output` stops pointing at it. Nothing in the manifest records that an earlier take existed. Take comparison and approval remain unbuilt (`docs/ROADMAP.md`).

A stale vision review no longer follows a shot across takes: `Shot.latest_review` is cleared when a new output displaces the file it describes. That defect predates this feature — only an API client could reach it before — but this control makes it a button.

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

### Adapter pre-flight audits

Read-only audits of an adapter's payload against the live schema. They read `/object_info` and nothing else — no graph is submitted, no GPU time is spent — so run them before any live render and after any ComfyUI or custom-node update.

```bash
uv run python tests/preflight_songplanner.py [base_url] [--record]
uv run python tests/preflight_h3_ultra.py [base_url] [--record]
```

Both default to `MVP_COMFY_URL`, then `http://127.0.0.1:8188`. Both print one `FAIL …` line per problem and exit non-zero, or one `OK <nodes> nodes across <variants> variants (<classes> classes) validated against <url>` line.

The shared rules live in `tests/preflight.py`: every node class registered, every fed input name present in the schema, every schema-required input fed, every combo value (model filenames included) among the options, every numeric literal inside its `min`/`max` and integral where the schema says INT, and every numeric literal resolving at least one bound — an input whose bounds vanished upstream is reported rather than silently skipped. Two schema shapes need expanding before that is true of anything richer than SongPlanner's graphs, and both produced *false* failures until the validator learned them:

- **Autogrow groups.** `MiniMaxH3ReferenceToVideo` publishes `ref_images` as a template plus an index range, not as nine keys, so `ref_images.ref_image_0` is not literally in the schema. The validator materialises the `prefix`+index slots and reports only an index past the template's `max`.
- **Format-conditional inputs.** `VHS_VideoCombine` publishes `crf`, `pix_fmt`, `save_metadata` and `trim_to_audio` under `format`'s options dict, keyed by the selected format. The validator merges the selected format's entries, which is also what makes `crf`'s 0–100 range checkable.

`preflight_h3_ultra.py` additionally compares the adapter's own constants against the live schema: its 9/3/3 per-kind limits against the autogrow maxima, every `mvp:split` output index against the splitter's `output_name` list, its 3600-frame ceiling against `length`'s declared maximum, and its four model filenames against the loaders' combo options.

A positional argument must be an `http(s)://` URL, so a mistyped flag is a usage message rather than a connection error. `--record` merges the audited classes into `tests/fixtures/object_info.json`, which the offline tests validate every builder's payload against. Recording **merges rather than replacing the file**, so one adapter's audit cannot delete another's coverage — but it does overwrite the entry for every class it names, which is how a moved bound gets picked up. The recorder reports changed entries as well as added ones, because a bound that shifts on an already-recorded class would otherwise be written in silently and the offline tests would quietly start agreeing with it. It writes via temp-and-replace, so an interrupted record cannot truncate the fixture both audits share.

One environment note: `F:` has coarse modification-time granularity, so a mutate-then-restore inside the same window can leave a stale `.pyc` that makes a later run execute code you already reverted. Clear `__pycache__` after any mutation experiment, or run with `PYTHONDONTWRITEBYTECODE=1`. and a **failing audit records nothing** — it prints `Fixture NOT recorded: the audit found problems`. After recording, shrink `UNRECORDED_CLASSES` in `tests/test_workflows.py` by exactly the newly recorded names; that list is the honest ledger of which classes nothing range-checks offline, and a test asserts it matches reality.

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

These are not pytest-collected and are never run as part of the automated suite. Each spends real GPU minutes on the user-managed ComfyUI — the reference smoke spends **two jobs**, the other two spend one job per variant. Run from the repo root with the app already serving and ComfyUI already up — no script here ever starts or stops ComfyUI.

```bash
uv run python tests/smoke_songplanner_app.py http://127.0.0.1:8766 --confirm-gpu
uv run python tests/smoke_h3_app.py http://127.0.0.1:8766 --confirm-gpu
uv run python tests/smoke_h3_reference_app.py http://127.0.0.1:8766 --confirm-gpu
```

`smoke_songplanner_app.py` **refuses to submit anything without `--confirm-gpu`** and exits with a usage message instead. It re-runs the `tests/preflight_songplanner.py` audit against the live ComfyUI, then submits exactly two short songs — invented variant first, then known-lyrics. It **creates one project per adapter**, because every `kind=music` job targets `"song"` and a shared project's second run would clobber the first. It prints exactly one JSON block per variant to stdout, and that block is the only record of which adapter produced which prompt ID — nothing in persisted state distinguishes a SongPlanner song from a direct Music 3 song, so capture the output. It `ffprobe`s both the file on disk and the ComfyUI `/view` URL the player actually fetches. Requested duration is not produced duration; the encoder resolves its own length, and the script reports the delta as a fact but fails a variant whose measurement is wildly off the request. It aborts non-zero on stderr — without spending a further generation — on pre-flight problems, ComfyUI being offline, an unexpected response shape, a job error, or the time ceiling.

`smoke_h3_reference_app.py` is the reference path's smoke and **it spends two GPU jobs, not one** — a Krea multiview promotion first, then one H3 reference render — so budget for both before you start it, and treat a retry after a failure as a new decision rather than a continuation. It refuses without `--confirm-gpu` before making any network call, locates `ffprobe`, checks its two staging sources on disk, checks the frame arithmetic against `align_h3_frames`, refuses a non-local `base_url` (its whole evidence is a file on this machine's ComfyUI output root), then reads `/api/health` and runs the H3 pre-flight audit — every one of those refusals is free. Staging goes through shipped routes only: create the project, import the master song from `input/music-video/audio/`, upload the character image from `input/music-video/characters/` as `kind=character`, promote it, poll the promotion and assert the child Asset's `path` was populated by the ordinary job refresh, write the Shot at `duration=3.75` with the sheet attached and `use_song_audio`, submit at 640×384 and 4 steps. Nothing is hand-written into a manifest. It prints exactly one JSON block to stdout, before its assertions, so a failing run still leaves the two prompt IDs behind; a promotion that ends in `error` reports the ComfyUI text and never attempts the render. The probe target is chosen **by name** — a completed shot leaves a `.png`, a silent `.mp4` and a muxed `-audio.mp4`, and only the last carries synchronized audio — and whether that file matches the `latest_output` the app's own reconciliation wrote is reported as a fact rather than assumed. Two things to expect from a passing run: the picture at 4 steps is heavily degraded, which is the point of a minimum-cost window and not a defect, and the smoke asserts nothing about picture quality or character likeness. See `docs/ROADMAP.md`'s 2026-08-18 section for what the one live run measured, including an unresolved observation about the sheet's layout appearing in the composition.

All three live-cost smokes refuse to submit without `--confirm-gpu`, and all three run their pre-flight audit before spending anything. `smoke_h3_app.py` aborts in this order: the cost gate before any network call at all, then a health read that stops if ComfyUI is offline, then the audit, and only then a submission — so every refusal is free. One caveat worth knowing: it submits the **Director** graph while the audit covers the **references-to-video** graph, so `MiniMaxH3DirectorCS` and the `fl2va` UNET it alone loads are only partly proven by that check.

## Quality gates

```bash
uv run pytest -q
uv run ruff check .
node --check src/music_video_producer/web/assets/app.js
```

The live GPU smokes above are **not** part of this gate set; they are run deliberately, by a human, with `--confirm-gpu`.
