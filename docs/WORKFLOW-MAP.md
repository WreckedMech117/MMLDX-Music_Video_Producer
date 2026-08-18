# Workflow Map

All source workflows remain unchanged under the portable ComfyUI workflow tree and creator backup directories. Music Video Producer uses explicit API-format adapters in `src/music_video_producer/workflows.py`.

## API workflow acquisition policy

- `J:\Hermes-Remote\comfyui\workflowsbackup\API-Workflows\Flux-Image-Gen.json` is a valid API-format reference and confirms the Flux adapter topology.
- No further user export is required for Flux, direct Music 3, or Krea multiview; those adapters are validated against live `/object_info` schemas.
- The selected H3 Director export was received. Its virtual/shared editor wiring omitted four required API links, so the runtime adapter uses explicit model, CLIP, video-VAE, and audio-VAE nodes.
- Combined H3→LTX 2.3 and 2.5 exports were received. They contain creator-specific media and multiple output branches, so they are audited references rather than directly submitted templates.
- SeedVR2, FILM, and RTX VSR are small enough to build and validate directly once stage boundaries and resolution policy are fixed.
- API exports are controlled references, not files submitted blindly. Adapters remain versioned application code with explicit user-facing inputs.

## Flux image generation

**Saved source:** `user/default/workflows/Flux-Image-Gen.json`

**Purpose:** character concepts, settings, props, and style frames.

**Adapter path:** `build_flux_payload()`

**Models:**

- `flux1-dev-fp8.safetensors`
- `ViT-L-14-TEXT-detail-improved-hiT-GmP-TE-only-HF.safetensors`
- `t5xxl_fp16.safetensors`
- `ae.safetensors`

**Controls:** prompt, width, height, steps, guidance, seed, output prefix.

The editor-only rgthree group bypasser, label, notes, primitive controls, and optional Power LoRA Loader are not included in the API graph. LoRA selection is a planned addition.

## MiniMax Music 3

**Saved source:** `Audio/MiniMax Music 3/SongPlanner + MiniMax Music 3 - Balanced Official.json`

**Purpose:** create complete songs from caption/style direction and section-tagged lyrics.

**Adapter path:** `build_music3_payload()`

**Models:**

- `minimax_music3_dit_fp16.safetensors`
- `minimax_music3_text_encoder_pruned_int8_convrot.safetensors`
- `minimax_music3_dav.safetensors`

**Controls:** caption, lyrics, maximum duration, seed, output prefix. Sampling currently uses 30 Euler/simple steps with CFG 1.7.

The standalone form accepts prepared caption/lyrics directly. Conversational SongPlanner expansion is represented in the saved workflow and remains a later API adapter; it is not falsely claimed as active in the current direct payload.

## SongPlanner — invented lyrics (MiniMax Music 3)

**Immutable source:** `workflow_templates/reference_exports/songplanner-invented-user-export.json` (audited copy of the creator's "SongPlanner + MiniMax Music 3 - Quality BF16" API export; SHA-256 in the reference-exports `MANIFEST.md`).

**Purpose:** generate a complete song from just an idea — Gemma-3 writes the caption and lyrics inside ComfyUI (`M3SongPlanner`), and Music 3 renders them in the same graph.

**Adapter path:** `build_songplanner_invented_payload()` over the shared `_build_songplanner_core()` (the known-lyrics variant below is the second thin builder over the same core).

**Models:**

- `gemma_3_12B_it_fp4_mixed` (SongPlanner text encoder)
- `minimax_music3_text_encoder_bf16`
- `minimax_music3_dit_fp16`
- `minimax_music3_dav`

**Controls:** title, idea, duration (30–300 s), seed, output prefix. A genre hint is accepted by the API (`genre_hint`, max 160 chars) but is currently API-only — the Song workspace form never sends it. Planner defaults follow the audited export (female vocals, English, temperature 0.8, top-p 0.95, top-k 64); Music 3 sampling uses the export's 30 Euler/simple steps at CFG 1.7 with encode CFG 1.5.

The explicit 10-node adapter drops the export's UI-only preview nodes, the CR Text scratchpad, the SeedNode indirection, and the dead tiled-decode branch; the literal seed feeds the encoder and sampler directly, and the model-resolved duration (`MiniMaxMusic3TextEncode` output 1) still drives the latent length. The master is saved as FLAC (matching the live-verified direct adapter) instead of the export's mp3/V0. `tests/preflight_songplanner.py` audits classes, model combos, and numeric ranges against live `/object_info` and records `tests/fixtures/object_info.json` for offline validation. Combo note: 0.33.1 V3 nodes publish options at `input[1]["options"]`, while classic loaders still inline them at `input[0]` — the audit reads both.

**Duration range:** 30–300 s, read from the recorded `/object_info` schema — `M3SongPlanner.duration_seconds` is `min 30.0, max 300.0`, and the encoder it feeds (`MiniMaxMusic3TextEncode.max_duration`, 0.04–360 s) is wider, so the planner governs. This is not the range the reference export's literals suggest; ComfyUI rejects anything outside it at `/prompt` validation (`value_smaller_than_min`) before a node runs, which surfaced as an opaque HTTP 502. `SongPlannerRequest.duration` and the Song workspace form both carry 30–300 so the refusal happens locally as a 422, and `validate()` in the pre-flight now range-checks every numeric input of every node in both variants so a drifted bound is caught offline. Direct Music 3 (`MusicRequest`) keeps its own 4–360 s range — it does not use `M3SongPlanner`. `step` is not enforced anywhere: ComfyUI accepts off-step values.

**Seed range:** 0–4294967295 for SongPlanner. `M3SongPlanner.seed` is 32-bit, even though `MiniMaxMusic3TextEncode.seed` and `KSampler.seed` in the same payload are 64-bit and are fed the same literal — so the planner governs here as it does for duration, and a seed above the 32-bit ceiling would have failed live the same opaque way. `SongPlannerRequest.seed` and the Song workspace form's seed field both carry that ceiling.

**Every other route now carries the 64-bit ceiling.** `MusicRequest.seed`, `FluxRequest.seed`, and `MultiviewRequest.seed` all carry `le=0xFFFFFFFFFFFFFFFF` (18446744073709551615), matching the `MiniMaxMusic3TextEncode.seed` and `KSampler.seed` schema maxima. Direct Music 3 is no longer deliberately unbounded — leaving it open meant an out-of-range seed reached ComfyUI and came back as an opaque HTTP 502, the same failure mode the duration floor produced. Every route now refuses locally with a 422 instead. SongPlanner keeps the narrower 32-bit ceiling because `M3SongPlanner` is the binding constraint on that path, not because the others were relaxed. All bounds are read off the request models by `tests/test_frontend_contract.py`, so no form and route can drift apart.

**Why the JS reports seed bounds as strings.** `api.js` emits `seedMax` as `"18446744073709551615"` / `"4294967295"` — string literals, not numbers — and it looks like a mistake until you try the alternative. 18446744073709551615 is not exactly representable as a JavaScript double: it rounds to 18446744073709552000, a ceiling that *admits seeds the route refuses*, so the form would accept a value and then take a 422. Strings avoid the rounding entirely, the HTML `max` attribute is a string anyway, and the contract test parses with `int()` so precision is preserved end to end. Do not "clean this up" into numeric literals.

**Duration input `step`:** the Song workspace duration field carries `step="any"`. The routes take a float and enforce only `min`/`max`; HTML's default `step=1` would make the browser reject the fractional durations the route accepts, so the field would refuse valid input before it was ever sent.

## SongPlanner — known lyrics (MiniMax Music 3)

**Immutable source:** `workflow_templates/reference_exports/songplanner-known-lyrics-user-export.json` (audited copy of the creator's "SongPlanner + MiniMax Music 3 - Quality BF16-Known_Lyrics" API export; SHA-256 in the reference-exports `MANIFEST.md`).

**Purpose:** generate a cover or an already-written song — the Director supplies the finished lyric sheet, Gemma-3 still writes the caption from the idea, and Music 3 renders the supplied lyrics unchanged (FR-14).

**Adapter path:** `build_songplanner_known_lyrics_payload()` over the same shared `_build_songplanner_core()` as the invented variant. The audited export differs from the invented export only in node `45.lyrics` and preview-node `58.source` (planner output → CR Text lyric sheet); the adapter passes the lyric sheet as a literal to node 45, so both builders' payloads are identical except node 45's lyric input (asserted by unit test). Passing a non-string (including `None`) to the known builder raises `TypeError` rather than degrading to the invented payload.

**Models:**

- `gemma_3_12B_it_fp4_mixed` (SongPlanner text encoder — still writes the caption from the idea)
- `minimax_music3_text_encoder_bf16`
- `minimax_music3_dit_fp16`
- `minimax_music3_dav`

**Controls:** title, idea, lyrics, duration (30–300 s), seed, output prefix; `genre_hint` is accepted by the API as on the invented variant. Sampling settings are unchanged from the invented variant. The duration range is the same node-schema range documented on the invented variant.

**Lyric handling:** the route strips only leading and trailing whitespace from the submitted sheet — edge whitespace is not lyric content — and every interior character, blank line, and indent then reaches node 45 unchanged; the builder never rewrites, reflows, or truncates it, and the planner never sees it. The stripped sheet is what is stored in `Song.lyrics`, so the manifest and the payload always agree. Blank or whitespace-only lyrics are rejected (HTTP 422 at the route, `ValueError` in the builder); the sheet is bounded at 8000 characters both in the form markup and at the route. Selected in the Song workspace via the `SongPlanner — known lyrics` preset, which keeps the lyrics textarea visible and required and posts to the same `/generate/songplanner` route with a `lyrics` field.

## Krea 2 multiview character sheet

**Saved source:** `Video/Music Video Advanced/02 - Krea 2 Character Sheet.json`

**Purpose:** turn an approved character image into close-up/front/side/back recurring-character references.

**Adapter path:** `build_multiview_payload()`

**Models and LoRAs:**

- `krea2_turbo_fp8_scaled.safetensors`
- `qwen3vl_4b_fp8_scaled.safetensors`
- `qwen_image_vae.safetensors`
- `krea2/Krea2-realism-V2.safetensors`
- `krea2/QuadView_krea2_v1.safetensors`

**Flow:** project/Comfy output image → upload to Comfy input → KJ resize → Krea grounded encode → identity model patch → 10-step KSampler → decode → SaveImage.

**Controls:** source character, multiview instruction, seed, output prefix.

**Verified live from the application 2026-08-18.** An uploaded `kind=character` image (1536×1024) was promoted through `POST /api/projects/{id}/assets/{asset_id}/multiview`: job `job_cab46ecd37f5`, prompt `1471fa58-24a9-4810-8de7-de7c0861f7b1`, seed `20260818`, complete in 56.1 s, output `music-video-producer/project_9d237e8fbddb/assets/asset_d0486f608572-multiview_00001_.png` at 2,025,707 bytes, 1536×1024. Inspected: a genuine four-view sheet — face plate, front, profile, back — with wardrobe, palette and identity consistent across the views.

Note what makes the promotion usable, because it is easy to miss: the route creates the child Asset with `path=""` and only the ordinary job refresh copies `output_files[0]` onto it. Until that reconciliation runs, `resolve_asset_path` fails `is_file()` and attaching the sheet to a Shot 404s. A promotion is not finished when the job completes; it is finished when the job has been refreshed.

## MiniMax H3 Director

**Saved source:** `Video/MiniMax H3 Ultra & Director/01 - MiniMax H3 Ultra V2.json`

The app validates shot windows, creates Director-compatible `start`/`length` segments, and builds a self-contained 15-node H3 graph with explicit registered loaders. Ready text-only shots can be submitted from Queue after a GPU-cost confirmation. The job stores its prompt ID and latest output; completion does not imply approval.

This path is verified live. On 2026-08-16 a 3.75 s window at 640×384 and 4 steps produced a 90-frame h264 clip with synchronized 32 kHz AAC audio, confirmed by `ffprobe`. Two observations from that run:

- The graph passes `requested_frames` to `duration_frames` and `normalDurationFrames`. The verified window was chosen to sit exactly on the 17k+5 grid; off-grid windows are untested, and `DirectorTimeline.aligned_frames` is currently computed but unused by the payload.
- `VHS_VideoCombine` returns a Windows subfolder with backslashes, which the application joins to the filename with a forward slash, so stored paths mix separators. ComfyUI's `/view` accepts this and previews resolve correctly, so it is a cosmetic inconsistency rather than a defect.

## MiniMax H3 Ultra — References to Video

- Immutable source: `workflow_templates/reference_exports/h3-ultra-references-user-export.json`.
- The exported 29-node graph is valid API format and all classes are registered on the live ComfyUI server.
- The Fantastic H3 media loader supports nine ordered pictures, three reference videos with paired audio, and three standalone audio references.
- The application adapter removes disconnected/editor convenience branches and builds an explicit 18-node Ultra stage around `MiniMaxH3ReferenceToVideo`.
- Attached asset order determines `<Picture N>`, `<Video N>`, and `<Audio N>` numbering. Stable labels are compiled into the prompt, and the project master song can be included as a standalone audio reference.
- Local paths are resolved only from contained project media or contained ComfyUI output.
- `ref_image_size=match` is the default. `max` remains available for higher identity fidelity at substantially higher cost.
- **Sampling profiles (added 2026-08-18, neither rendered live from this application yet).** `H3Request.profile` selects one of two configurations, each reproducing a whole source of evidence rather than mixing them. `H3_REFERENCE_PROFILES` in `workflows.py` holds them as data.

  | Profile | LoRA | Strength | Scheduler | Sampler | Steps | Evidence |
  |---|---|---:|---|---|---:|---|
  | `default` | none | — | `simple` | `res_multistep` | 20 | `workflow_templates/reference_exports/h3-ultra-references-user-export.json` — node `2382` (`simple`, 20), node `2388` (`res_multistep`), and node `2383` `Power Lora Loader (rgthree)` holding `minimax_h3_turbo_4step_ckpt500_comfyui_pruned.safetensors` with **`"on": false`** |
  | `turbo` | `minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors` | 0.7 | `beta` | `euler` | 4 | `workflow_templates/reference_exports/h3-ltx25-user-export.json` — the in-repo copy of the Director's saved `Video/Music Video Advanced/04 - H3 Music Video - LTX 2.5 READY.json`, and what the tests actually read: node `5959` `LoraLoaderModelOnly` fed by the `ref2va` UNET loader (`127`), node `124` (`beta`, 4), node `123` (`euler`). The export's chain is `127 → 5959 → 142` (`PathchSageAttentionKJ`, `sage_attention: "disabled"`) `→ 5960`; the adapter has no attention node, so its LoRA feeds the shift directly |

  A third profile, **`turbo-references2v`**, reproduces the Director's canonical `MiniMaxH3Turbo References2V` mode: `minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors` at strength **1.0** with `simple`/8/`euler`, every value read out of `h3-turbo-references2v-user-export.json`. It is named after the source graph rather than after a speed claim, and **nothing has been rendered on it**. Two evidenced turbo bundles from two different Director graphs now ship side by side, each whole — the shipped `turbo` came from the LTX music-video pipeline and is the only one with a frame behind it.

  **The turbo profiles substitute the LoRA node class, and live schema is why.** Both carry their LoRA through `LoraLoaderModelOnly` even though `h3-turbo-references2v-user-export.json` uses `Power Lora Loader (rgthree)`. That is not a convenience: live `/object_info` publishes **no `lora_*` inputs at all** on the rgthree class — its `required` is empty and its LoRA rows are client-side widgets. A faithful copy would therefore move the filename and strength beyond the schema's reach, so `check_model_files` could no longer confirm the file is installed, the strength range check would govern a node the graph no longer uses, and `preflight.validate` would reject `lora_1` as an input that does not exist. The faithful copy would fail its own audit, and weakening the validator to admit it would delete the guarantee the audit exists for. The cost, stated rather than hidden: these profiles are evidenced in their **values**, not in their **wiring**.

  **No profile reproduces its export node-for-node, and none claims to** — a profile is a sampling bundle, not a graph. The turbo export also carries resolution plumbing, a frame-count expression, `SpectrumApplyMiniMaxH3`, and an **orphaned `fl2va` `UNETLoader` unreachable from its output**, which is exactly the hazard the reference-exports `MANIFEST.md` warns about for the 2026-08-17 export set. The tests derive only from the chain reachable from the `ref2va` loader, so an orphan cannot contaminate them.

  The default profile emits **exactly** the payload the adapter emitted before profiles existed; a digest taken at commit `7e25ad0` pins it in `tests/test_workflows.py`. The export's off-switch is the whole justification for it having no LoRA — that is a 20-step graph its author chose to render without one, and the LoRA it holds switched off is the *generic* H3 turbo LoRA, not the `ref2v` one the turbo profile applies.

  On the turbo profile the adapter inserts `mvp:lora` between `mvp:model` and `mvp:shift`, so every node downstream of the loader draws from the LoRA. Neither the LoRA in isolation nor either scheduler paired with the other profile's sampler has been rendered by anyone here, which is why the profile is one choice rather than four switches.

  **The `default` and `turbo` profiles were verified live from this application on 2026-08-18, one variable changed** (a third profile, `turbo-references2v`, was added later the same day and has **not** been rendered). The same Reference Sheet (`asset_21cc26355142`), the same shot, the same seed `20260819` and the same 640×384 / 3.75 s window were rendered on each profile. Turbo produced a coherent frame with the sheet's wardrobe carried faithfully — no undersampling and none of the panel bleed the earlier 4-step run *without* a LoRA produced, which is the evidence that the LoRA is doing the work the step count would otherwise do. The two frames differ in look rather than quality: turbo is higher-contrast and darker with a tighter composition and less legible background detail, the default is wider and brighter with more environmental fidelity.

  **The speed claim, measured rather than reasoned.** Turbo took **182.7 s** of ComfyUI execution against the default's **237.5 s** — a 23% saving from a 5× cut in sampling steps, because sampling is not what dominates this graph. Read "roughly a fifth of the sampling" as a statement about steps only; it is *not* a fifth of the render. An earlier 497.6 s figure for the default profile is not comparable to either — that run loaded models cold, and loading was most of it.

  Two deliberate non-changes, **both since corroborated** by the Director's per-mode API exports of 2026-08-17 (`J:/Hermes-Remote/comfyui/workflowsbackup/API-Workflows`). The adapter keeps `shift_audio=3` rather than the LTX music-video pipeline's `6` — and the canonical `MiniMaxH3 References2V` and `MiniMaxH3Turbo References2V` both use `3`, so the adapter matches the reference-to-video graphs rather than the pipeline that wraps one. The adapter also carries `PathchSageAttentionKJ` with `sage_attention: "disabled"`, which is what every canonical graph and the LTX export itself carry; an earlier note here described this as keeping the node "enabled", which was wrong — the only difference is that the adapter's node is present where the Director's editor graph bypasses it, and the setting is `disabled` on both sides.

  **A non-default profile on a text-only Shot is refused with a 422.** That path loads a different checkpoint pair through `MiniMaxH3DirectorCS`, the installed generic H3 turbo LoRAs are not the `ref2v` one, and nothing has been rendered that way. The boundary is *enforced*, not merely documented: accepting the request and quietly rendering the 20-step no-LoRA graph would spend a full-price GPU job and log it under a configuration that was never applied. Omitting the profile still renders as it always did, and attaching a reference to the same Shot makes the same profile submittable. `H3_DIRECTOR_DEFAULT_STEPS` keeps its 20.
- **Frame ceiling:** `MiniMaxH3ReferenceToVideo.length` is `min 5, max 3600` — exactly 150 s at 24 fps. Note the node's own tooltip puts its **trained** range at roughly 124–362 frames (about 5–15 s), so a window between 362 and 3600 frames is accepted by validation while being far outside what the model was trained for; the adapter refuses only the hard maximum. The adapter refuses above it locally as a 422 rather than letting ComfyUI reject the prompt at validation time and surface as an opaque 502. The nine/three/three limits and the `mvp:split` output indices are likewise the node's own — the autogrow maxima of `ref_images`/`ref_videos`/`ref_audios`, and the splitter's `picture_1…9`, `video_1…3`, `video_audio_1…3`, `audio_1…3` outputs.
- **Pre-flight:** `tests/preflight_h3_ultra.py` audits ten payload variants — eight on the default profile plus one for each turbo profile — the model files, and those constants against live `/object_info`; it reported `OK 176 nodes across 10 variants (19 classes)` on 2026-08-18, after the third profile was added (`OK 157 / 9 / 19` immediately before it). (It reported `OK 138 nodes across 8 variants (18 classes)` on the same day, immediately before the live render below, which is the shape it had before the turbo variant was added.) The turbo variant is what confirms `LoraLoaderModelOnly` is registered and `minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors` is installed before any GPU time — the filename reaches the check by being *loaded* by a payload, not by being listed in the audit. A sixth check compares `H3_LORA_STRENGTH_LIMITS` against `LoraLoaderModelOnly.strength_model`'s declared bounds on the live server, so the range a profile's strength is validated against cannot drift away from the range the node actually accepts — the same discipline the frame-ceiling check already applies. (An earlier line here read `OK 90 nodes across 5 variants (17 classes)` — that was the audit's first shape, before Story 3.1's review added the request-bound extremes and the two text-only Director variants.) It reads combo options from `[1]["options"]`, expands `COMFY_AUTOGROW_V3` groups into their numbered slots, and merges `VHS_VideoCombine`'s format-conditional inputs — without the last two it reported eight failures on a correct graph. No graph is submitted; the audit is not evidence of a render.
- **Verified live from the application 2026-08-18, and read the qualification.** Shot `shot_f5a5b9f4ab72` in project `project_9d237e8fbddb` carried one picture reference — the promoted Krea sheet above — plus the project master song via `use_song_audio`, at `duration=3.75`, 640×384, 4 steps, `ref_image_size="match"`, seed `20260819`. Job `job_cc558fd0ff89`, prompt `6dbe4ff7-08d2-468c-bd5b-8dce37bd68fd`, complete in 136.5 s. `ffprobe`: h264 640×384, `nb_frames` 90, 24/1 fps, video 3.750 s; aac 32000 Hz stereo, audio 3.744 s; container 561,942 bytes. Duration delta from the request 0.000 s, audio/video sync delta -0.006 s. `latest_output` was written and `approved_output` stayed empty.
  - **The audio reference is trimmed to the render window by the node.** The route passes the whole song file with no offsets and the master was 154 s long, yet the emitted audio is 3.744 s. This was an open unknown before the run.
  - **The render left three files** (`.png`, silent `.mp4`, muxed `-audio.mp4`) and only the `-audio.mp4` carries synchronized audio. On this run the job's `output_files` carried only that file, so `latest_output` landed on it — a measurement, not a guarantee. Choose a probe target by name.
  - **4 steps is not a production setting *on the default profile*.** The frame is heavily degraded. The window was chosen to prove the path for the least GPU time, and picture quality is not claimed from it. The override came from the smoke script, not the adapter: 4 steps was sent against the 20-step no-LoRA graph, which has nothing to compensate. `tests/smoke_h3_reference_app.py` now names a profile and sends no step count at all, so that mistake is not repeatable — but no run has yet been made on the turbo profile, so nothing here says 4 steps *with* the LoRA looks better.
  - **Unresolved, and not explained here:** in the first frame the sheet's four-panel layout appears to have carried into the composition — face plate on the left, vertical panel bands persisting, a figure standing inside one band — as though the layout were read as scene structure rather than purely as identity reference. Both 4 steps and `ref_image_size="match"` (which conditions the 1536×1024 sheet at its own aspect) plausibly contribute; neither is established as the cause. A production step count, `"max"` sizing, and a single-view reference are all untested against it.
  - **Never exercised live:** video references, paired video audio, more than one picture, `ref_image_size="max"`, and any window near the frame ceiling. Those remain schema-audited only.

## Vision continuity inspection

- The configured LM Studio vision model can inspect image references and four-frame contact sheets extracted from reference/generated videos.
- Inspections persist visible identity/environment details, continuity cues, prompt cues, and risks on the asset or latest take.
- Reviews are advisory records. They never change `approved_output` and never infer sensitive identity traits.

## LTX 2.5 and finishing

**Saved source:** `Video/Music Video Advanced/04 - H3 Music Video - LTX 2.5 READY.json`

The saved workflow and live logs show the intended route:

`H3 → SeedVR2 → dimension normalization → LTX 2.5 → FILM → RTX VSR → assembly`

SeedVR2 completed a real 192-frame upscale at 1250×720. The following LTX VAE encode failed because width 1250 is not divisible by its 4-pixel patch size, and the LTX 2.5 video VAE sets `crop_input=False`, so nothing auto-corrects the size the way LTX 2.3 does. The adapter inserts `ImageResizeKJv2` between SeedVR2 cleanup (`6112`) and all three LTX image consumers (`6116:6070` `VAEEncode`, `6116:4970` `LTXVImgToVideoInplace`, `6116:6073` `GetImageSize`) with `width=0`, `height=0`, `divisible_by=32`, and `keep_proportion="crop"`; KJNodes therefore derives the target from the source dimensions and rounds 1250×720 down to 1248×704.

**Why 32 and not 16 (changed 2026-08-17).** The LTX 2.5 VAE's total spatial compression is 32 — a 4-pixel patchify followed by three stride-2 stages (`comfy/sd.py:618` sets `downscale_ratio` to 32 in both spatial axes). Divisor 16 clears the patchify check but yields 1248×720, and 720/32 = 22.5, pushing a half cell through the conv stack. Divisor 32 gives 1248×704, exact at every stage.

**Crop, not stretch — the Director's ruling (2026-08-17).** The first fix used `keep_proportion: "resize"`, which resamples straight to the target with `crop="disabled"` and therefore *squashed* 1250×720 into 1248×704: a **2.07% anamorphic stretch**, aspect 1.73611 → 1.77273. The Director ruled that geometry must be preserved and that trimming a few pixels is the acceptable price, so the normalizer now uses **`keep_proportion: "crop"`** with `crop_position: "center"`.

What crop does differently: it centre-crops to the target aspect *first*, then resamples. For 1250×720 → 1248×704 that means cropping to 1250×705 (15 rows, split 7 top / 8 bottom) and resampling 705 → 704, leaving **0.02% residual distortion** instead of 2.07%. The cost is 15 rows of picture; the gain is that nothing is stretched.

**Both modes output exactly 1248×704, so dimensions cannot tell them apart.** This matters for anyone verifying the graph: the size assertions in this document hold under either setting, and only the `keep_proportion` value distinguishes a correct graph from a stretching one. Do not "simplify" `crop` back to `resize`.

Verified by executing the installed `ImageResizeKJv2` on a synthetic 1250×720 frame carrying a row-index ramp, rather than by re-deriving its arithmetic: crop retained source rows ~6–710 (0.02% residual distortion, 15 rows lost), resize retained all 720 rows at 2.07% distortion, and both returned 1248×704 with differing content. `keep_proportion: "crop"` is a real enum value on the live `/object_info` schema, and crop mode honours `width=0`/`height=0` the same way resize did — `"crop"` is absent from the branch that recomputes the target, so it falls through to `width = W; height = H` and then floors to the divisor. **No hardcoded resolution is needed**, which is what keeps the patch working on graphs that produce a different size.

**Sub-divisor edge case, sharper under crop.** A frame with an axis below 32 floors to 0 and then raises `ZeroDivisionError` in crop mode (it divides by the target height), where resize mode raised `ValueError: height and width must be > 0`. Both fail, but the crop-mode failure is the more obscure one, so passing `source_size` — which routes through `normalize_to_divisor` and floors at one divisor cell — matters more now, not less.

`workflow_templates/reference_exports/h3-ltx25-user-export.json` is immutable audited evidence and **captures the pre-fix graph**: it still wires `6112` straight into the subgraph. The normalization node is not in the file; `patch_ltx25_dimension_boundary` inserts it in memory at runtime. Re-exporting the file is the Director's action in ComfyUI, not the application's.

The Director's saved editor workflow was repaired in place on 2026-08-17: node `6133` `ImageResizeKJv2` (divisor 32) now sits between `6112` and subgraph instance `6116`, and node `142` `PathchSageAttentionKJ` is bypassed with its widget pinned to `disabled` (it had aborted three runs with `ModuleNotFoundError: sageattention`). The combined graph is not exposed for submission from the application because it still regenerates H3 from creator-specific media. A standalone approved-take LTX adapter remains pending.

**Pre-repair backup, recorded to the same SHA-256 convention as the audited exports.** Directory `J:\Hermes-Remote\comfyui\ComfyUI_windows_portable\ComfyUI\user\default\workflows\Video\Music Video Advanced\`:

| File | SHA-256 | Notes |
|---|---|---|
| `04 - H3 Music Video - LTX 2.5 READY.20260817-003310.bak` | `df9a0bfb61e4ea8d4e2da6757941b2c6177adfd18a0ea3dbb70c43eed8f508c6` | 98,872 bytes; the pre-repair graph, byte-identical to the original at copy time. Deliberately not named `.json` so ComfyUI's workflow browser does not list it as a workflow. |
| `04 - H3 Music Video - LTX 2.5 READY.json` | `2ae33cf817c989a6dc6cb96205b8880fdf7fde2b429a3e64592fddd242f66c2c` | 100,699 bytes; the repaired graph, 61 nodes. This hash changes the moment the Director saves from the ComfyUI editor — it records the agent's write, not a permanent invariant. |

**Live boundary run 2026-08-17.** The patched graph was submitted once as prompt `a64a0460-64e6-4a14-b207-e644bf9bda5d` and ran to `success` in 17 min 36 s. Measured with `ffprobe`: H3 base 1056×608 / 192 frames → SeedVR2 1250×720 / 192 frames → LTX 2.5 **2496×1408** / 185 frames → FILM + RTX VSR 3744×2112 / 369 frames at 48 fps. The LTX subgraph applies a 2× latent upsample, so 2496×1408 is exactly 2 × 1248×704 — measured confirmation the normalizer produced a 32-divisible size where the previous three runs died at `VAEEncode`. Note the frame count is not preserved: 192 in, 185 out (8k+1), which assembly must account for alongside H3's 17k+5 alignment.

**Where the 8k+1 grid actually comes from, found 2026-08-18.** The 192 → 185 frame change was recorded as a property of "the LTX stage". There is a more specific cause available: `VHS_LoadVideo`'s `format: "LTXV"` declares `frames: [8, 1]`, so the **loader conforms the clip to an 8k+1 grid on the way in** — 185 is 8 × 23 + 1. The `LTXVLoopingSampler`'s temporal tiling (tile 56, overlap 24) may also act on it, so this is a second and upstream cause rather than a replacement explanation, and nothing here asserts which dominates. What it does mean is that a graph loading video through VHS with the LTXV format is already on that grid before a sampler runs.

## LTX 2.5 enhancement (standalone)

**Immutable source:** `workflow_templates/reference_exports/ltx25-enhancer-user-export.json` (SHA-256 in the reference-exports `MANIFEST.md`).

**Purpose:** improve a take the Director already likes, without regenerating it. Until this existed, LTX was reachable only by re-running H3 inside the reference chain, so every attempt at a better final image cost a full H3 pass *and* produced a different picture.

**Adapter path:** `build_ltx25_enhance_payload(source_video, prefix)` — 18 nodes, the export's **reachable subgraph and nothing else**. `reachable_node_ids(graph, roots)` exists as a function rather than a comment so the tests and the pre-flight can both hold the adapter to that rule.

**Models, exactly four:** `ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors`, `ltx-2.5-video-vae-conv-bf16.safetensors`, `gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors`, and `ltx-2-19b-ic-lora-detailer.safetensors` at 0.9.

**Two orphans it does not load.** The export carries 20 nodes; `LatentUpscaleModelLoader` and an audio `VAELoaderKJ` are unreachable from its output. Despite the name there is **no latent upscale in the executed path** — the flow is `ImageScaleToMaxDimension` (lanczos, longest side 1920) → `VAEEncodeTiled` → `LTXVLoopingSampler` → `LTXVSpatioTemporalTiledVAEDecode`, with the detail coming from the LoRA. Audio reaches the saver directly from the loader.

**Reachability must be per node, not per class.** The orphaned audio VAE is a `VAELoaderKJ`, and so is the *reachable* video VAE. A class-level dependency rule would have silently dropped a model the graph genuinely needs.

**Two node substitutions, both forced by live schema.** `VHS_LoadVideo` cannot be reproduced: its `video` combo enumerates ComfyUI's **input** directory and a take lives under **output**, so the published options are empty for our purposes — `VHS_LoadVideoPath` is used instead, same four outputs in the same order. And the detailer's `Power Lora Loader (rgthree)` publishes no `lora_*` inputs at all, with the filename buried in a widget dict, so `LoraLoader` carries it (model *and* CLIP here, unlike the H3 reference adapter's model-only substitution). The consequence is the same one recorded for the turbo profiles: **evidenced in values, not in wiring.**

**The pre-flight caught its own blind spot before any GPU time.** Its first run reported three model dependencies instead of four, because the rgthree nesting hid the LoRA filename — exactly the failure the audit exists to catch, found offline rather than as an opaque 502.

**No Director-facing controls.** Sigmas (`0.909375, 0.725, 0.421875, 0.0`), cfg 1, `euler`, seed 0, an empty prompt and the 0.9 detailer strength are all reproduced and none are exposed; the spec marks tuning them as a separate decision. The route writes **nothing** to the Shot, so the take it enhances is untouched — the enhanced file appears only on the job's `output_files`.

**Frame count is measured, never asserted.** A guard test greps the adapter, the route, the pre-flight and the test files for any assertion that output frames equal input frames, and fails if one appears.

## Readiness matrix

| Workflow | Catalog discovery | API payload | Unit tests | Live prior model validation | App submission |
|---|---:|---:|---:|---:|---:|
| Flux Image Gen | yes | yes | yes | models present | yes |
| Music 3 direct | yes | yes | yes | generated FLAC previously | yes |
| SongPlanner invented lyrics | n/a | explicit 10-node adapter | yes | **live generation verified 2026-08-17** — 29.989 s FLAC, `ffprobe`-confirmed; classes/models validated against live `/object_info` 2026-08-16 | yes |
| SongPlanner known lyrics | n/a | same 10-node core, lyric sheet literal | yes | **live generation verified 2026-08-17** — 29.989 s FLAC, `ffprobe`-confirmed; same audited node classes/models as invented | yes |
| Krea multiview | yes | yes | yes | **live promotion verified from the app 2026-08-18** — 1536×1024 four-view sheet in 56.1 s, child Asset path populated by job refresh | yes |
| Director compile | yes | start/length timing | yes | live compiler accepts timing scaffold | timeline only |
| H3 text-only shot render | yes | explicit 15-node adapter | yes | **live render verified end to end 2026-08-16** | yes |
| H3 Ultra reference/audio shot | yes | explicit 18-node adapter, 19 on either turbo profile | yes | **live render verified end to end 2026-08-18** — one picture reference plus the master song, 90 frames at 3.750 s with synchronized audio; rendered at 4 steps on the **default** profile, so the path is proven and the picture is not usable, and character consistency is **not** demonstrated. The **turbo** sampling profile is schema-audited only — its LoRA is confirmed installed, and it has **never been rendered from this application** | yes |
| LTX 2.5 enhance | yes | combined export audited + boundary patch | yes | **full reference chain ran clean live 2026-08-17**; boundary passed at 1248×704 | no |
| SeedVR2/RTX/FILM | yes | pending | no | **all three stages ran live 2026-08-17** inside the reference chain | no |
