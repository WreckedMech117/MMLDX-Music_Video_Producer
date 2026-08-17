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

## Vision continuity inspection

- The configured LM Studio vision model can inspect image references and four-frame contact sheets extracted from reference/generated videos.
- Inspections persist visible identity/environment details, continuity cues, prompt cues, and risks on the asset or latest take.
- Reviews are advisory records. They never change `approved_output` and never infer sensitive identity traits.

## LTX 2.5 and finishing

**Saved source:** `Video/Music Video Advanced/04 - H3 Music Video - LTX 2.5 READY.json`

The saved workflow and live logs show the intended route:

`H3 → SeedVR2 → dimension normalization → LTX 2.5 → FILM → RTX VSR → assembly`

SeedVR2 completed a real 192-frame upscale at 1250×720. The following LTX VAE encode failed because width 1250 is not divisible by its 4-pixel patch size, and the LTX 2.5 video VAE sets `crop_input=False`, so nothing auto-corrects the size the way LTX 2.3 does. The adapter inserts `ImageResizeKJv2` between SeedVR2 cleanup (`6112`) and all three LTX image consumers (`6116:6070` `VAEEncode`, `6116:4970` `LTXVImgToVideoInplace`, `6116:6073` `GetImageSize`) with `width=0`, `height=0`, and `divisible_by=32`; KJNodes therefore preserves the source dimensions and rounds 1250×720 down to 1248×704.

**Why 32 and not 16 (changed 2026-08-17).** The LTX 2.5 VAE's total spatial compression is 32 — a 4-pixel patchify followed by three stride-2 stages (`comfy/sd.py:618` sets `downscale_ratio` to 32 in both spatial axes). Divisor 16 clears the patchify check but yields 1248×720, and 720/32 = 22.5, pushing a half cell through the conv stack. Divisor 32 gives 1248×704, exact at every stage.

**The tradeoff, stated plainly: 32 costs a ~2% anamorphic stretch.** `keep_proportion: "resize"` does not crop — KJNodes resamples straight to the target with `crop="disabled"` — so the frame is stretched, not trimmed. Measured: x scale 1248/1250 = 0.9984, y scale 704/720 = 0.97778, so aspect goes 1.73611 → **1.77273**, a 2.1% relative horizontal stretch against the source (H3's own 1056×608 is 1.73684). Divisor 16 would have been far gentler on aspect (1248×720 = 1.73333, 0.16% off) but does not divide the VAE stack. Exact division was chosen over aspect fidelity because 16 fails outright at `VAEEncode`. **Whether the stretch is visible has not been assessed** — no one has watched the output frames side by side, so treat "2.1%" as the measured number and nothing more. If it turns out to matter, the fix is `keep_proportion: "crop"` with `divisible_by=32` — trading 16 rows of frame for exact aspect — not a return to 16.

`workflow_templates/reference_exports/h3-ltx25-user-export.json` is immutable audited evidence and **captures the pre-fix graph**: it still wires `6112` straight into the subgraph. The normalization node is not in the file; `patch_ltx25_dimension_boundary` inserts it in memory at runtime. Re-exporting the file is the Director's action in ComfyUI, not the application's.

The Director's saved editor workflow was repaired in place on 2026-08-17: node `6133` `ImageResizeKJv2` (divisor 32) now sits between `6112` and subgraph instance `6116`, and node `142` `PathchSageAttentionKJ` is bypassed with its widget pinned to `disabled` (it had aborted three runs with `ModuleNotFoundError: sageattention`). The combined graph is not exposed for submission from the application because it still regenerates H3 from creator-specific media. A standalone approved-take LTX adapter remains pending.

**Pre-repair backup, recorded to the same SHA-256 convention as the audited exports.** Directory `J:\Hermes-Remote\comfyui\ComfyUI_windows_portable\ComfyUI\user\default\workflows\Video\Music Video Advanced\`:

| File | SHA-256 | Notes |
|---|---|---|
| `04 - H3 Music Video - LTX 2.5 READY.20260817-003310.bak` | `df9a0bfb61e4ea8d4e2da6757941b2c6177adfd18a0ea3dbb70c43eed8f508c6` | 98,872 bytes; the pre-repair graph, byte-identical to the original at copy time. Deliberately not named `.json` so ComfyUI's workflow browser does not list it as a workflow. |
| `04 - H3 Music Video - LTX 2.5 READY.json` | `2ae33cf817c989a6dc6cb96205b8880fdf7fde2b429a3e64592fddd242f66c2c` | 100,699 bytes; the repaired graph, 61 nodes. This hash changes the moment the Director saves from the ComfyUI editor — it records the agent's write, not a permanent invariant. |

**Live boundary run 2026-08-17.** The patched graph was submitted once as prompt `a64a0460-64e6-4a14-b207-e644bf9bda5d` and ran to `success` in 17 min 36 s. Measured with `ffprobe`: H3 base 1056×608 / 192 frames → SeedVR2 1250×720 / 192 frames → LTX 2.5 **2496×1408** / 185 frames → FILM + RTX VSR 3744×2112 / 369 frames at 48 fps. The LTX subgraph applies a 2× latent upsample, so 2496×1408 is exactly 2 × 1248×704 — measured confirmation the normalizer produced a 32-divisible size where the previous three runs died at `VAEEncode`. Note the frame count is not preserved: 192 in, 185 out (8k+1), which assembly must account for alongside H3's 17k+5 alignment.

## Readiness matrix

| Workflow | Catalog discovery | API payload | Unit tests | Live prior model validation | App submission |
|---|---:|---:|---:|---:|---:|
| Flux Image Gen | yes | yes | yes | models present | yes |
| Music 3 direct | yes | yes | yes | generated FLAC previously | yes |
| SongPlanner invented lyrics | n/a | explicit 10-node adapter | yes | **live generation verified 2026-08-17** — 29.989 s FLAC, `ffprobe`-confirmed; classes/models validated against live `/object_info` 2026-08-16 | yes |
| SongPlanner known lyrics | n/a | same 10-node core, lyric sheet literal | yes | **live generation verified 2026-08-17** — 29.989 s FLAC, `ffprobe`-confirmed; same audited node classes/models as invented | yes |
| Krea multiview | yes | yes | yes | one-step sheet previously | yes |
| Director compile | yes | start/length timing | yes | live compiler accepts timing scaffold | timeline only |
| H3 text-only shot render | yes | explicit 15-node adapter | yes | **live render verified end to end 2026-08-16** | yes |
| H3 Ultra reference/audio shot | yes | explicit 18-node adapter | yes | live schema/classes and real vision path verified | yes |
| LTX 2.5 enhance | yes | combined export audited + boundary patch | yes | **full reference chain ran clean live 2026-08-17**; boundary passed at 1248×704 | no |
| SeedVR2/RTX/FILM | yes | pending | no | **all three stages ran live 2026-08-17** inside the reference chain | no |
