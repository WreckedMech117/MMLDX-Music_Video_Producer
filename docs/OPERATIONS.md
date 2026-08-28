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

## Watch and approve a take

The finishing lane begins here. `GET /api/projects/{id}/shots/{shot_id}/take` streams a Shot's latest take **by id** — the server resolves its own `latest_output` through the same confined resolution the media route uses, so there is no path parameter to inject — and honours HTTP Range (Starlette's `FileResponse` serves real 206s; Edge was observed scrubbing on them in the browser gate). The player sits in the shot inspector beside the render controls.

`POST .../approve` and `POST .../unapprove` are bodyless. Approve writes `approved_output := latest_output` and `status := "approved"` together — nothing from the wire on the right-hand side — refusing 409 while a render is in flight (read from the job records, since a hand-edited status hides a live render) and 422 with no take. Approving twice is a byte-identical no-op. Un-approve clears both and returns the Shot to `complete`, which is the one way back: an approved Shot is refused by mark-ready, render-again (which names the approval), expansion and the assistant alike.

Approval is **explicit and reversible, never automatic** — FR-21's words. No render completion, poll tick or assistant tool can approve anything.

Browser QA note: `tests/e2e_shot_controls.py` now synthesizes a playable take with ffmpeg (must be on PATH) under an isolated `MVP_COMFY_ROOT`, so it never touches the real ComfyUI output tree.

Approval now **snapshots the window** (`approved_start`/`approved_duration`, AD-13): the decision is about this take *in this window*, and assembly refuses a shot whose window moved after approval — re-approve, or restore the window, and it assembles again.

## The Monitor and the over-render margin

The timeline has a **program viewer**: the Monitor above the transport plays the shot under the playhead against the master song. The master audio element is the clock; the video is a muted view of it — the take's own sampler audio stays audible in the per-shot inspector player, which is where its diagnostic value lives ("voices but no phonetics" found a real bug once). Gaps and unrendered shots show a named placeholder, never a stale frame.

Every shot take is now rendered **at least half a second longer than its window** (the Director's 2026-08-19 ruling — "do not generate a clip to exact or lesser length than the time it was given"): `duration + 0.5 s`, snapped up the 17k+5 grid, so a 3.75 s window renders 107 frames (4.458 s), never 90 again. For song-audio shots the conditioning window extends with the picture — up to a **quarter-second lead** ahead of the window when the song has room — so the whole take is performed against real song seconds and editable room exists at either end. The lead is recorded on the shot at submission (`latest_take_lead`); takes rendered before the margin correctly read 0.

**Fine-tuning**: the inspector's *Trim nudge* control slides which slice of the take fills the window, in frame steps, floored at the recorded lead. The Monitor previews the exact slice assembly will cut — `lead + nudge` is one rule on both sides, contract-tested. The nudge stays editable on an approved shot by design: it selects a slice of the approved file; the file itself stays immovable. A nudge that runs the cut off the end of the take is refused at assembly with the take's measured length in the sentence.

Two footnotes: the restore-song-audio stage windows by the **take**, not by the shot's window, as of 2026-08-21 — it sends `over_render_frames(duration)` frames of song from `start − latest_take_lead`, the same seconds `generate_h3` conditioned the take with, and refuses a take that records no lead (rendered before the margin, or a hand-picked clip) rather than guessing an offset. Not re-measured against a live render. And `extend` (VideoExtender) likely only extends forward, per the Director; it is recorded, not employed.

## Assemble the video

`POST /api/projects/{id}/assemble` — and the **Assembly bar at the foot of the timeline** — is the finishing lane's main act (FR-22): every approved take, trimmed to its shot's window, joined in shot order, with the **master song as the sole audio track** (shot audio is dropped at assembly; the Director's 2026-08-16 ruling). It runs **locally in ffmpeg, never on ComfyUI** (AD-9) — no GPU time, no queue entry, an idle ComfyUI throughout — and answers synchronously in seconds. `ffmpeg`/`ffprobe` must be on PATH.

The trim is not optional: grid alignment renders every clip longer than its window (a 4.0 s shot is 107 frames ≈ 4.458 s), and untrimmed joins drift ~11 % — twenty seconds over a three-minute song. Frame counts come from one cumulative 24 fps grid so per-clip rounding cannot accumulate; mixed-resolution takes are normalized to the largest-area take present, aspect preserved with centered padding, never stretched.

Refusals come **all at once** in one 422 — every unapproved shot, every stale or legacy approval, every missing take file, and every gap/overlap against the song, each by shot ID — so a 15-shot plan is fixed in one reading, not rationed one refusal at a time. State conflicts are 409s: open renders, or an assembly already running. The song's duration is ffprobe's reading of the file, never the stored field.

The export lands under `data/projects/{id}/media/exports/`, numbered and never overwritten, and **only after verification passes** — duration within one frame of the song, exactly one video and one audio stream — so a failed run leaves nothing that could be mistaken for a result. It is recorded as a `RenderJob` of kind `post` with an **empty `prompt_id`** (the local-work marker) carrying the consumed takes in `inputs`, and plays in the Assembly bar through the media route's Range service. Live-proven 2026-08-18 on `project_21e5a260c3a7` ("Assembly Live Proof"): the two real singing takes over a 7.5 s excerpt cut from exactly the windows they lip-synced to — 180 frames, 7.500 s vs a 7.500 s song, both takes byte-identical afterwards.

## Pin a frame on a singing shot

The dedicated keyframe modes (`image_to_video`, `first_last`) are the cheaper graphs and have **no song lip-sync** - the node has no audio input. To pin a frame on a shot that sings: use **references mode**, cite the picture in the `first` (or `last`) role alongside any identity references, and keep `use_song_audio` on. The picture rides as an ordinary reference slot and the structured prompt declares it the shot's first frame (`fully_preserved`), per MiniMax's guide 2.2.2 - the reference map writes that line for un-expanded shots, and the expansion specialist writes it for expanded ones. A keyframe picture counts against the node's 9-picture ceiling.

## Sections: the song's structure layer

The **SECTIONS track** holds the Director's structure marks — Intro/Verse/Chorus/Bridge/Outro — as real boxes: drag to move, drag an edge to resize, **edges snap to the shot boundaries below** (tolerance scales with zoom), **double-click empty track space to create**, click to select. A selected section owns the inspector panel: label, window, covered-shot count, and the **shared prompt** carried into every shot inside it. `PUT /api/projects/{id}/sections` replaces the list whole; overlaps are refused by name, gaps are legal and mean unknown.

Sections are the fix for the wrong-lyric lipsync found on the first full render run (a chorus-position shot expanded with the song's opening verse line — the submitted audio trim was exactly right; the *words* were guessed). The section's label pairs with the lyric sheet's own `[Tag]` blocks **by order of appearance within a label family** ("Verse 2" takes the second `[Verse]` block), and the expansion specialist may sing only from that block — `clip_position` picks the line(s) within it, and an empty block means *no words*, never a guess. Populate proposes sections when none are marked (repaired, then dropped on the track for dragging); marked sections are never replaced.

## Populate Timeline

`POST /api/projects/{id}/timeline/populate` — the **Populate Timeline** button in the Assets panel — lays out the whole plan from the Song, Treatment and Assets in one act (the Director's user workflow, stage 4). Destructive by design and doubly guarded: the button shows the warning (replaces every shot; first run or a deliberate redo) and the server refuses without `confirm_replace` in the same words. Locked or approved shots refuse populate entirely, by name, before the model is ever asked.

The model's layout is treated as **shape, never arithmetic**: its prompts and relative durations survive, but `populate_windows` repairs the geometry into what assembly later demands — contiguous from exactly 0 to exactly the song's end, every window inside H3's reliable 4–15 s range, the shot count clamped to the feasible band. Each tiled window draws its prompt from the proposal whose proportional span of the song contains it. Shots land as plain drafts; assistant fill (modes/citations/singing) and expansion remain the next acts.

Operational note from the first live run (2026-08-19): the local model happily proposes out-of-range windows (a 44.8 s shot, a whole-song shot) — the chat route 502s on those (`PlannedShot` caps duration at 30) while populate's repair absorbs anything; a retry or two on the chat lane is normal, and populate itself landed a 16-shot, exactly-tiling plan on its first attempt.

## Generate All

`POST /api/projects/{id}/generate/batch` — the queue panel's **Generate All** button — submits every ready shot as one batch (FR-4): one confirmation naming the count (server-enforced through `confirm_gpu`, so no client can spend hours of GPU by omission), each shot its own job and prompt id, all sharing one freshly minted `batch_id`. A shot whose submission refuses is **skipped by name with the route's own sentence and blocks nothing else** — the report lists both halves, and the toast relays it whole. Every submission rides the identical single-shot handlers, so no gate or payload rule exists twice.

**Replace existing takes** (the checkbox beside the button) widens the batch to settled shots (`complete`/`error`), re-opened through the render-again path; approved and locked shots are never touched and the report names them. **Flag for re-render** (in the shot inspector, AD-5) marks a shot whose take fell short; *Re-queue flagged (N)* resubmits exactly the flagged set, and each success clears that shot's flag — a refusal keeps it, and the batch draining never touches it.

FR-9 by construction: all submissions are kind `h3`, consecutive, in timeline order — nothing interleaves, nothing frees or unloads on the ComfyUI side, so the resident model stack survives the whole batch (measured: the second consecutive H3 render saved 150 s).

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

**Job state now reaches the interface without a click.** `GET /api/projects/{id}/render-status` is the AD-1 reconciliation endpoint: one `/queue` read per tick, `/history` only for open jobs absent from the queue, and an idle project generates **zero** ComfyUI requests. The browser polls it every 2 s **only while the project has non-terminal jobs** — the timer stands up when a job is queued and stands down when the last one settles — so completion lands on the asset card, the shot clip and the queue row with no click, and a toast names what finished. ComfyUI being down is a quiet `comfy_online: false`, never an error spray, and a poll tick is skipped while a shot write is in flight so it can never interleave with a sweep's read-to-save window. The manual **Refresh** button remains as one reconciliation call plus a reload (it used to fan out one request per job). A job absent from both `/queue` and `/history` — ComfyUI restarted mid-render — keeps its status rather than being invented an error; "Render again" is the way out, and the project keeps polling until it is resolved.

**Generation submits guard themselves now.** The Flux, Music and multiview submit controls disable and read "Queuing…" while a request is in flight. This closed a live defect: with no polling and no guard, a completed render was invisible, the silence invited a second click, and the form's fixed seed made ComfyUI render the identical image twice.

Refresh consults `/queue` whenever history is still empty, so an executing render reports `running`. If a job appears stuck, confirm against the ComfyUI server directly: the running entry carries the same prompt ID, and free VRAM drops sharply while the model stack is resident.

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

`PathchSageAttentionKJ` and the attention backend, corrected 2026-08-21. This paragraph used to say `sageattention` was not installed and that an enabled node aborts with `ModuleNotFoundError`. Both were true when written and neither is now: `sageattention 2.2.0+cu128torch2.7.1` has been in ComfyUI's embedded Python since 2026-08-19, and ComfyUI is launched with `--use-sage-attention`. Three consequences worth keeping straight:

- The adapters' `sage_attention: "disabled"` is **not** "no acceleration". At `disabled` the node returns the model untouched, writing no `optimized_attention_override`, so the render uses ComfyUI's global backend — which that launch flag makes SageAttention. To actually get PyTorch attention you have to override it, which is what `ModelAttentionBackend` does.
- `sageattn3` and `sageattn3_per_block_mean` import a **separate** `sageattn3` package that is not installed, and would fail at sampling time — after the checkpoint is loaded. The installed library's own dispatcher also notes its triton kernel is unusable on sm120, which is this card.
- `MVP_SAGE_ATTENTION` still patches every `PathchSageAttentionKJ` at submission (`create_app`'s one choke point) and is a *different* mechanism from the per-payload `attention` profile in `workflows.H3_ATTENTION_PROFILES`. Do not set both; `tests/measure_h3_attention.py` refuses to run while the environment variable is set, for that reason.

### Self-hosting browser QA (no server to start)

> **None of these run under `uv run pytest -q`, and that is still true of every assertion in
> them.** `pyproject.toml` sets `testpaths = ["tests"]` and pytest's default
> `python_files = test_*.py`, so no `tests/e2e_*.py` file is collected as a test. This is not
> theoretical. `e2e_effects_tab.py` broke on Epic 10's first slice — a stack-equality predicate met
> a wire that had gained `bindings: []` — and **four consecutive slices reported green gates over
> it** before an audit ran the harnesses. Two more, `e2e_seed_and_asset_tabs.py` and
> `e2e_shot_controls.py`, are failing today and predate Epic 10 entirely (see `deferred-work.md`).
> **Run the harnesses that touch what you changed, and say which you ran.**
>
> *Amended 2026-08-28, and the sentence being amended is the one that said a green run "says
> nothing about any of them".* Since `tests/test_e2e_harnesses.py`, a green run now says four
> things about all twenty-three, none of which needs a browser: **each one parses, each one
> imports** (with `selenium` stubbed, so a name a sibling harness stopped exporting fails the
> suite), **each one appears in the list below with the port it really starts on**, and **the
> collisions marked below are exactly the collisions there are**. It says nothing whatever about
> whether any of them *passes* — the two failing today are green under it — so the instruction in
> bold above is unchanged. See that module's own docstring for the full list of what it lets past.

```bash
uv run --with selenium python tests/e2e_shot_controls.py         # default port 8767
uv run --with selenium python tests/e2e_song_context.py          # default port 8768  ← collides
uv run --with selenium python tests/e2e_monitor.py               # default port 8768  ← collides
uv run --with selenium python tests/e2e_timeline_scroll.py       # default port 8769  ← collides
uv run --with selenium python tests/e2e_render_polling.py        # default port 8769  ← collides
uv run --with selenium python tests/e2e_take_swap.py             # default port 8770
uv run --with selenium python tests/e2e_timeline_edit.py         # default port 8771
uv run --with selenium python tests/e2e_song_analysis.py         # default port 8772
uv run --with selenium python tests/e2e_section_looks.py         # default port 8773
uv run --with selenium python tests/e2e_seed_and_asset_tabs.py   # default port 8774
uv run --with selenium python tests/e2e_clips_and_attach.py      # default port 8776
uv run --with selenium python tests/e2e_clip_overlap_and_split.py # default port 8777
uv run --with selenium python tests/e2e_effects_tab.py           # default port 8778
uv run --with selenium python tests/e2e_monitor_preview.py       # default port 8779  ← collides
uv run --with selenium python tests/e2e_shot_numbering.py        # default port 8780
uv run --with selenium python tests/e2e_chip_column.py           # default port 8781
uv run --with selenium python tests/e2e_chip_column_narrow.py    # default port 8782
uv run --with selenium python tests/e2e_effects_section_copy.py  # default port 8783
uv run --with selenium python tests/e2e_band_panel.py          # default port 8779  ← collides
uv run --with selenium python tests/e2e_preview_song_change.py   # default port 8784
```

These twenty **start and prove their own server** and take no base URL — `--port N` overrides. Order does not matter and they share no state; each creates a fresh temporary data root under `%TEMP%\mvp-<label>-<nonce>`, left behind as evidence.

**Three pairs share a default port** — 8768, 8769 and 8779, marked above. (`e2e_band_panel.py` was written for Epic 10 and left off this list through three slices, which is the omission the paragraph below already warns about; it landed on the Monitor preview's 8779.) `ManagedServer` refuses a bound port by name rather than reusing it, so the collision costs a failed start and never a run against the wrong server; it does mean those three pairs cannot run at the same time without `--port`. This list was five entries long and said "these five" while there were twelve, which is why the ports were never noticed to overlap. Two more were missing from it again on 2026-08-25 — the Effects tab's and the Monitor preview's — so **add the line when you add the script**; a gate nobody can find is a gate nobody runs.

`e2e_shot_numbering.py` and `e2e_chip_column.py` are the two Epic 9 gates whose only evidence used to be a PNG in a scratchpad. **The first is a destructive-action gate.** `Delete SHOT 05?` could be raised over a clip drawn `SHOT 02` because `shotLabel` counted manifest positions while `renderTimeline` counted song positions, and the contract test that covered `shotLabel` could not see it: its fixture had no `start` field, so the two orderings coincided and it passed under both the broken and the fixed rule. So this script never fixtures the divergence by hand — it presses `#split-shot` on the first of four contiguous clips, which puts the new half **last in the manifest and second in the song**, then drives the browser's real dialog (`EC.alert_is_present`, `alert.text`, `dismiss()` then `accept()`) and asserts the sentence names the clip that was clicked. No deletes nothing, yes deletes exactly that shot, both read back off the stored manifest, and `/readiness` is asked the same question so the number the model's own report carries is checked too. Because a native `confirm` is browser chrome and never lands in a viewport capture, the sentence Selenium read back is painted into the page for the screenshot — evidence already taken, drawn where it can be seen beside the clip.

`e2e_chip_column.py` gates the clip corner chips, which stack in a column up the right edge rather than sitting abreast. It reconstitutes the **pre-change rule in the live page** and compares rectangles, so "the lone chip did not move" is a measurement rather than two numbers that happen to agree; then injects a second and a fourth chip and measures each against the clip's own 82 px, the `overflow: hidden` that would otherwise conceal an escape, and the painted glyph rectangles of the prompt underneath. **Only one chip ships today** (`clipEffectsChip` draws `ƒ` and nothing else), so the multi-chip sections are an experiment about the stylesheet, marked `data-experiment` and removed again — and four chips is where the column runs out: 4×15 px plus 3×4 px of gap plus 5 px top and bottom is exactly 82. `e2e_chip_column_narrow.py` is its supplement and imports its fixture: the 40 px clip old rule against new, and four chips on it.

`e2e_effects_section_copy.py` gates Story 9.5's Section target on the copy control — a look copied to "named Shots **or the current Section**". It drives all five states in one project by selecting a different clip: no sections marked at all (driven first, before any are written, so it is the project's real state), a section holding the source and two others, a section holding a locked shot, a shot in no section, and a section of one. Then it replaces the 60 s song with a 24 s one under the Director's own confirmation, so the Chorus box describes seconds the track no longer has, and asserts the control goes on answering about the windows that are there. **Membership is never computed by the script.** `Project.shot_sections` is read off `GET /api/projects/{id}` and the ticked set is asserted against *that*, so a browser that started deciding for itself fails rather than agreeing by luck. Seven screenshots — the five states, the ticked set before the copy is confirmed, and the report after it — go to `test-artifacts/`. It queues nothing and never reaches `/prompt`.

`e2e_band_panel.py` is Epic 10's gate: the bind glyph, the band panel, the spectrum strip and the Drive readout under the Monitor. It writes a manifest and a sidecar and nothing else — ComfyUI is pointed at a dead port and never contacted, and no render is queued. Its strongest sections are **pixel censuses of two real canvases**: a canvas that threw halfway through drawing, or one measured at zero width inside a hidden box, is a correctly-sized empty box that every structural assertion would pass over, so the painted pixels are counted and the palette tokens told apart by channel. That census is what caught the readout's rest line being painted over by its own envelope, and its layout probe is what caught the Monitor collapsing to its 120px floor on every Shot with no binding.

`e2e_preview_song_change.py` gates the one question a Preview Clip's name answers — *what determines this picture?* — across a song change, and it needs **ffmpeg on PATH** for the same reason the Monitor preview's does. It exists because the server hashed `song_fingerprint` into every Shot's preview fingerprint while the client's `previewInputKey` carried no song at all: two answers to one question, which renamed the cached clip of every graded Shot on a re-analysis *and* left a bound Shot's Monitor playing a picture driven by a track the project no longer had. **The failure has no symptom of its own** — an import measures the song it writes, so nothing refuses and nothing is said; the only observable is a request that is never sent. So this script counts requests out of the browser's own resource timings, replaces the song through the Song workspace's real file input, and **never reloads the page**, because a refresh empties the Monitor's held clips and repairs the symptom without touching the cause. It drives both halves — a bound Shot re-asked for and coming back a different clip, and a graded but unbound Shot whose cached clip is left alone — then takes the measurement away entirely for the refusal path, and presses the Snap-to row's own `[Analyze song]` to bring it back. It queues nothing, contacts a dead ComfyUI port, and declares one deliberate 422.

`e2e_monitor_preview.py` needs **ffmpeg on PATH**: it synthesizes its own takes and every preview it drives is an ffmpeg transcode. It is the D2 gate and most of what that slice can be checked by — it samples the Monitor every animation frame and asserts on the picture itself, so a black flash, a frozen frame or a frame belonging to the previous Shot is a failed section rather than something a human has to notice.

Prerequisites: nothing listening on the port, Microsoft Edge plus its WebDriver, and `music_video_producer` importable from this checkout's `src/`. **ComfyUI does not need to be running** and no language-model host is needed. None of them spends GPU time or reaches `/prompt`.

Why they start their own server rather than accepting a URL: on 2026-08-17 a health check passed against an hour-old process still bound to the port, and a live check was one step from reporting a working feature broken on the strength of stale code. `tests/e2e_support.py`'s `ManagedServer` refuses a bound port **by name and start time**, verifies the listener is its own descendant (the `uv` trampoline means the real `python run.py` is a grandchild, so teardown is `taskkill /F /T` and then proves the port came free), and proves the responder writes into a data root this run created seconds ago. **A health check that only proves something is listening proves nothing about what.** Exit code 2 means refused before any assertion ran.

`e2e_timeline_scroll.py` rebuilds the Director's own plan's *shape* through routes — 30 shots over a 154.6 s song in 7 sections, enough to overflow any viewport — and gates the Timeline panel's viewport: that the scroll box's horizontal scrollbar and the Assembly bar are inside the window at 1600×1100/950/820, that a real wheel gesture moves the viewport along the song, that all four tracks move by the same offset and a SECTIONS box holds its alignment to the SHOTS clip beneath it, that the zoom slider and the `−`/`+` buttons both zoom and agree, that zoom holds the playhead (or the viewport centre) instead of jumping to zero, and — the regression guard for the coordinate maths — that a clip dragged and resized **at a non-zero scroll offset** stores the window that was dragged. Since the tools moved into the transport bar it also hit-tests **all fifteen controls in that bar at 1600/1280/1024/900/820 px** — the bar wraps rather than hiding anything — and asserts both sliders carry a visible label and every icon-only button an `aria-label` identical to its tooltip. It queues nothing and asserts `jobs` is empty at the end. **Read `project_59f14d19ff10` for its shape only; it never opens it.**

`e2e_take_swap.py` gates switching a shot between its own takes from the shot inspector, on a shot with **two real takes** synthesized locally by ffmpeg into an isolated `MVP_COMFY_ROOT` it owns. It clicks the take **row** — not the chip, which already worked — and reads `latest_output` back from the server, then checks that the Monitor followed, that Enter on the focused row swaps too, that the current row takes neither focus nor a click, and that the swap **still works after the inspector has been torn down and rebuilt** (the recorded stale-element failure mode in that panel). It declares two console errors by name: the Clips library points each take's `<video>` straight at ComfyUI's `/view`, which 404s whenever ComfyUI is down.

`e2e_shot_controls.py` seeds five shots and four assets through shipped routes and drives mark-ready/mark-draft, render-again and the multiview promote control — asserting each is rendered, hit-testable at its centre, correctly labelled, and that a disabled button the browser honours changes nothing server-side. It never clicks promote, and asserts `jobs` is empty at the end. `e2e_song_context.py` drives the lyrics/style editor, its counters, save, the per-field restores, the clearing confirmation through the browser's real dialog, and the VRAM eject toggle — writing `machine-preferences.json` inside its own root, so your stored choice is untouched. It causes exactly one deliberate 422 (the oversized sheet), declared to the console gate by name.

`e2e_song_analysis.py` gates analysing an existing song from the "Snap to" selector — the affordance Epic 8 shipped without, which left `POST /song/analyze` with no caller and every existing project with a song and no measurement. It imports a click track (which measures it), then **empties the analysis record in the manifest and deletes the sidecar**, so the un-analysed state is the real one every pre-Epic-8 project is in rather than a simulated one. It then asserts the Beats row names what is missing and offers the action, the Phrase gaps row names where transcription happens and offers no button that would not help, the reason resolves to an inert token rather than `--red` or `--amber`, a real keyboard press moves a tick without the row losing focus, and pressing the action puts marks on the band and clears the row **with no page reload** — proved by a `window` sentinel, which a navigation would take with it. It drives the re-measurement trap deliberately: the sidecar is deleted with the manifest record left intact, so the song fingerprint does not move and both client-side loaders would short-circuit on it. It finishes on the refusal, moving the media file out from under the manifest to take the route's real 404, and asserts the server's own sentence reaches the Director and nothing changed. Two deliberate 404s are declared to the console gate by name. It queues nothing and never reaches `/prompt`.

**Found by these scripts and since fixed, each on its own merits:**

- **A toast covered the shot inspector's controls.** `.toast-region` and `.toast` are now `pointer-events: none`, so a click passes through to the control beneath. Relocating the region was rejected: every corner of this layout holds a control in some workspace — the two inspectors bottom-right, the import and Flux actions bottom-left, the waveform actions top-right, the rail top-left — so moving it trades one collision for another and only in the workspace you happened to check. A toast here carries nothing clickable and self-removes after 4.2 s, so it has no claim on a pixel. The visual overlap remains by choice; the Director can now act through it. `e2e_shot_controls.py` **presses `#compile-shot` through a live toast** and asserts the press arrived, with a helper that records the toast really was standing over the button so the assertion cannot go vacuous.
- **Three controls vanished at narrow widths, and got three different answers.** The asset inspector was the only surface carrying promote, analyse and attach, so below 1180 px it now **reflows** to a full-width row under the library instead of `display: none`. The shot inspector holds the only mark-ready, render-again and compile controls and the only shot-prompt editor, so below 860 px it now **stacks** — the same query already collapses the timeline to one column. But `#vram-eject-note` is the opposite case: the topbar is a single fixed-height row the whole grid is sized from, with nowhere to reflow a 220 px sentence, and the setting is a standing machine preference rather than something needed mid-task — so the **toggle now hides with its note**. Hiding half a control is worse than hiding all of it. Asserted at 1600/1280/1024/820.
- **The app re-rendered after replies it never awaited.** `bindClip`'s pointerup saved the whole shot list on a plain selection click; that write's reply reloaded readiness, and *that* reply rebuilt the inspector long after the click looked finished. A selection is not an edit, so the save is now conditional on the drag actually having moved something, which deletes the sequence at its root. The rebuilds that remain are correct — readiness decides the blocked flag — so rather than suppress them, `renderShotInspector` now carries the focused control's id, value and caret across the `innerHTML` replacement. Without that, anything typed since the last `change` was lost to a reply to a request nobody made. Browser resource timings assert a selection issues **zero** writes and **zero** readiness reads, with a positive control so the counter cannot be blind.

Still true and worth copying in any future browser script: project load renders asynchronously, so `settle()` on a MutationObserver is the right way to wait rather than retrying. `wait_for_readiness` takes an optional fragment and a script must name its own plan's shot count — the app loads the first project in the root before a script selects the one it seeded, and `0 of 0 shots have a prompt.` satisfied the old default.

### Isolated first-run browser QA

The three scripts below still need a **manually started** server, unlike the two above. Run the app on port 8766 with an empty temporary data root, then execute:

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
