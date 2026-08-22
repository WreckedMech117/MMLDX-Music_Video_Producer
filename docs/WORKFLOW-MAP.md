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

**Duration headroom, and the two inputs it separates.** `M3SongPlanner.duration_seconds` and `MiniMaxMusic3TextEncode.max_duration` take the same kind of number and mean different things: the planner is told **how long a song to write**, the encoder is given a **latent ceiling** the song may finish before. The adapter passed the target to both, so lyrics running slightly long lost their ending. `SongPlannerRequest.duration_headroom` (default 1.5, range 1.0–12.0) is now the multiplier between them — the planner always receives the requested duration unchanged and only the ceiling moves. A headroom of 1.0 reproduces the pre-headroom payload **byte for byte**, which took a deliberate line: multiplying an integer `120` by 1.0 yields `120.0` and changes the wire bytes while every `==` assertion still passes.

**The evidence disagrees with itself, which is why this is a field rather than a constant.** `Music-Video.md` states the 50%-headroom rule **four times**, once first-hand — *"I learned this the hard way when the ending got cut off"* — yet is not internally consistent (60 s of lyrics needs "90 or more" in one place, "80–90" in another), and **both audited exports set `duration_seconds` and `max_duration` equal at 200**, i.e. no headroom at all. Neither reading has live evidence behind it: both SongPlanner renders on record sat at the 30 s floor and returned 29.989 s, exactly where the setting cannot show. 1.5 ships as the documented claim carried forward, not as a verified number, and settling it needs a long, lyric-dense song.

**The form shows the product, and bounds neither field against the other.** The Song workspace carries an **Encoder headroom ×** control on both SongPlanner presets and sends `duration_headroom` on every submission, so the route's default is never applied invisibly. The product must still stay inside the encoder's 360 s ceiling — 300 s still requires a headroom of 1.2 or less — but the form now *shows* that product against the ceiling and refuses an out-of-range pair locally, naming both ways out (`…450 s — over the encoder's 360 s maximum. Lower the headroom to 1.2, or the duration to 240 s.`) rather than spending a submit on a 422.

Bounding either field against the other was considered and rejected: whichever field follows becomes a trap, because raising the headroom would slide the duration's `max` under a number already in its box, leaving only two bad exits — silently rewriting what the Director typed, which is the quiet shortening this whole feature exists to prevent, or a box holding a value its own `max` forbids. **Neither input is subordinate; the schema bounds their *product***, so the product is what the form reports. This belongs beside the seed-maxima-as-strings note as the same class of decision: the form must promise exactly what the route accepts, and where that cannot be expressed as a `max`, it is expressed as a visible derived number instead. Nothing is ever clamped.

**The 1.5-versus-1.0 question is now settleable by ear**, which it was not before: a long, lyric-dense song at 300 s is submittable at 1.0 and at anything up to 1.2, and that is the first range where the setting can show at all.

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

  **What `disabled` actually means, corrected 2026-08-21.** It is not "no acceleration". KJNodes' `patch` returns the model *untouched* at `disabled` (`model_optimization_nodes.py:124`), writing no `optimized_attention_override`, so the sampler falls through to ComfyUI's **global** backend. This ComfyUI is launched `--use-sage-attention` (`/system_stats` argv; `Using sage attention` in `ComfyUI/user/comfyui.log` at every retained start, including `2026-08-20 09:00:34` inside the batch the render-cost table came from). So every H3 render this project has timed ran on **SageAttention**, and the node's job is to decline to override, not to switch anything off.

  **The attention backend is now a profile, and the default is byte-identical.** `build_h3_reference_payload`, `build_h3_keyframe_payload` and `build_h3_image_edit_payload` each take `attention=` and resolve it through `workflows.H3_ATTENTION_PROFILES`. `build_h3_director_payload` takes none and emits no attention node at all, which is unchanged and deliberate.

  | profile | node | value | chain position |
  |---|---|---|---|
  | `default` | `PathchSageAttentionKJ` | `disabled`, `allow_compile: False` | after the sigma shift |
  | `pytorch` | `ModelAttentionBackend` | `pytorch attention` | **before** the sigma shift |
  | `comfy-kitchen` | `ModelAttentionBackend` | `comfy kitchen attention` | **before** the sigma shift |
  | `sage-auto` | `PathchSageAttentionKJ` | `auto`, `allow_compile: False` | after the sigma shift |
  | `sage-fp8-cuda++` | `PathchSageAttentionKJ` | `sageattn_qk_int8_pv_fp8_cuda++`, `allow_compile: False` | after the sigma shift |

  **The position is read, not chosen.** `PathchSageAttentionKJ` sits after the shift in `h3-ultra-references-user-export.json` (node `2372` is fed by shift `2373`), which is where this adapter has always emitted it — that is why the default's bytes do not move. `ModelAttentionBackend` sits *before* the shift in the Director's Comfy Kitchen graph, `J:/Hermes-Remote/comfyui/workflowsbackup/ComfyKitchen/MiniMax-H3 TXT2VID IMG2VID (Full)- 20260818.json`, chain-walked by MODEL link on 2026-08-21: `215 UNETLoader → 6006 LoraLoaderModelOnly (muted) → 6088 Power Lora Loader → 6095 ModelAttentionBackend → 6007 MiniMaxH3SigmaShift (12/3) → 214 BasicGuider / 212 BasicScheduler → 213 SamplerCustomAdvanced`. That graph's Sage branch (node `223`, muted, `["auto", false]`) is a **parallel alternative**, not a stack.

  **Exactly one attention node is ever emitted, and that is mechanical rather than stylistic.** Both classes write the same `model_options["transformer_options"]["optimized_attention_override"]` — `ModelAttentionBackend` via `ModelPatcher.set_model_optimized_attention` (`comfy/model_patcher.py:688`), `PathchSageAttentionKJ` directly (`model_optimization_nodes.py:134`). Two in one chain do not compose; the later one wins, silently. Which is why the Kitchen graph branches, and why a profile is a whole node rather than a set of knobs.

  **Five of the eight sage kernels are deliberately unreachable.** `sageattn3` / `sageattn3_per_block_mean` import a *separate* `sageattn3` package that is not installed and would raise at sampling time, after the checkpoint is loaded; the installed `sageattention 2.2.0`'s own dispatcher says its triton kernel "is currently not usable on sm120"; and the two lower-accumulator CUDA kernels are not what that dispatcher selects for this architecture — `core.py:152` routes sm120 to `sageattn_qk_int8_pv_fp8_cuda(pv_accum_dtype="fp32+fp16", qk_quant_gran="per_warp")`, which is what `sage-auto` gets and what `sage-fp8-cuda++` names by hand at the default `per_thread` granularity.

  **Nothing has been rendered on any non-default profile as this is written.** Each is schema-audited by `tests/preflight_h3_ultra.py` against live `/object_info` — node registered, input names matching, combo value in the published list — and a schema audit is not evidence of a frame. `tests/measure_h3_attention.py` is the A/B; it refuses to submit without `--confirm-gpu`, and it treats a run that ComfyUI silently fell back to PyTorch on as **inconclusive** rather than as "no difference", because `ModelAttentionBackend.VALIDATE_INPUTS` returns `True` for any string.

  **The frame is selected, not typed.** A reference request carries either an explicit `width`+`height` **or** `megapixels`/`aspect_ratio`/`multiple`, never both — supplying both is a 422 rather than a silent precedence, and half an explicit frame is refused too, since `width` alone would otherwise render 640×608. The default is the Director's own selection: **0.6 MP, 16:9, multiple 32 → 1056×608**, reproducing the 2026-08-17 boundary measurement exactly (`33×32` by `19×32`).

  `select_resolution` reproduces the built-in `ResolutionSelector` (`comfy_extras/nodes_resolution.py`) rather than approximating it, and three details are load-bearing: the megapixel is **1024²**, not 1,000,000; the rounding is Python's `round`, which is **banker's rounding** and differs from `floor(x + 0.5)` wherever a half-cell lands on .5; and each axis rounds **independently**, so 0.6 MP at 16:9 yields 0.64 MP at 1.737:1 — *larger* than asked and *not* 16:9. That drift is the node's behaviour, and reproducing it faithfully matters more than landing nearer the nominal figure: a "fix" toward nominal would silently stop matching the Director's frame. The pre-flight checks the aspect options, the megapixel range and the multiple's **`step`** against live `/object_info`, because ComfyUI validates min and max but *not* step — an off-grid frame is the one geometry error that reaches the GPU instead of the validator.

  **The text-only Director path keeps 1344×768 and refuses the selector fields** with a 422. `MiniMaxH3DirectorCS` sizes its own frame through `custom_width`/`custom_height`/`divisible_by`, and no frame from that graph has ever been measured at 0.6 MP. Accepting the field and resolving it anyway would spend a full-price render at an unevidenced size and log it as though it had been chosen.

  **H3 generates its own output audio, and that is by design.** `VHS_VideoCombine.audio` is fed by a `VAEDecodeAudio` on the sampler's own latent, in the adapter and in both canonical exports (`h3-references2v`, `h3-ultra-references`) alike. A song attached through `use_song_audio` reaches the model as **`ref_audios` conditioning** — which is what drives the lip movement — and is deliberately **not** the output track. Measured 2026-08-18: the rendered clip's audio correlates with the source at **≈ 0.01 at every lag within ±1 s** and is 3.4× louder, so it is a regeneration rather than a copy. Anything asserting that the output audio should resemble the master song is wrong, and it would be easy to write such a test by accident.

  The consequence is a **missing pipeline step**: a finished cut needs the real track muxed back over the generated one, which is what the Director's `LTX2.5 AudioReplacer` graph does and which nothing here implements yet. It is recorded in `docs/ROADMAP.md` rather than folded into the adapter.

  **A non-default profile on a text-only Shot is refused with a 422.** That path loads a different checkpoint pair through `MiniMaxH3DirectorCS`, the installed generic H3 turbo LoRAs are not the `ref2v` one, and nothing has been rendered that way. The boundary is *enforced*, not merely documented: accepting the request and quietly rendering the 20-step no-LoRA graph would spend a full-price GPU job and log it under a configuration that was never applied. Omitting the profile still renders as it always did, and attaching a reference to the same Shot makes the same profile submittable. `H3_DIRECTOR_DEFAULT_STEPS` keeps its 20.
- **Frame ceiling:** `MiniMaxH3ReferenceToVideo.length` is `min 5, max 3600` — exactly 150 s at 24 fps. Note the node's own tooltip puts its **trained** range at roughly 124–362 frames (about 5–15 s), so a window between 362 and 3600 frames is accepted by validation while being far outside what the model was trained for; the adapter refuses only the hard maximum. The adapter refuses above it locally as a 422 rather than letting ComfyUI reject the prompt at validation time and surface as an opaque 502. The nine/three/three limits and the `mvp:split` output indices are likewise the node's own — the autogrow maxima of `ref_images`/`ref_videos`/`ref_audios`, and the splitter's `picture_1…9`, `video_1…3`, `video_audio_1…3`, `audio_1…3` outputs.
- **Pre-flight:** `tests/preflight_h3_ultra.py` audits ten payload variants — eight on the default profile plus one for each turbo profile — the model files, and those constants against live `/object_info`; it reported `OK 176 nodes across 10 variants (19 classes)` on 2026-08-18, after the third profile was added (`OK 157 / 9 / 19` immediately before it). (It reported `OK 138 nodes across 8 variants (18 classes)` on the same day, immediately before the live render below, which is the shape it had before the turbo variant was added.) The turbo variant is what confirms `LoraLoaderModelOnly` is registered and `minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors` is installed before any GPU time — the filename reaches the check by being *loaded* by a payload, not by being listed in the audit. A sixth check compares `H3_LORA_STRENGTH_LIMITS` against `LoraLoaderModelOnly.strength_model`'s declared bounds on the live server, so the range a profile's strength is validated against cannot drift away from the range the node actually accepts — the same discipline the frame-ceiling check already applies. (An earlier line here read `OK 90 nodes across 5 variants (17 classes)` — that was the audit's first shape, before Story 3.1's review added the request-bound extremes and the two text-only Director variants.) It reads combo options from `[1]["options"]`, expands `COMFY_AUTOGROW_V3` groups into their numbered slots, and merges `VHS_VideoCombine`'s format-conditional inputs — without the last two it reported eight failures on a correct graph. No graph is submitted; the audit is not evidence of a render.
- **Verified live from the application 2026-08-18, and read the qualification.** Shot `shot_f5a5b9f4ab72` in project `project_9d237e8fbddb` carried one picture reference — the promoted Krea sheet above — plus the project master song via `use_song_audio`, at `duration=3.75`, 640×384, 4 steps, `ref_image_size="match"`, seed `20260819`. Job `job_cc558fd0ff89`, prompt `6dbe4ff7-08d2-468c-bd5b-8dce37bd68fd`, complete in 136.5 s. `ffprobe`: h264 640×384, `nb_frames` 90, 24/1 fps, video 3.750 s; aac 32000 Hz stereo, audio 3.744 s; container 561,942 bytes. Duration delta from the request 0.000 s, audio/video sync delta -0.006 s. `latest_output` was written and `approved_output` stayed empty.
  - ~~**The audio reference is trimmed to the render window by the node.**~~ **This was wrong, corrected 2026-08-18 by reading the source.** The route passed the whole song with no offsets, the master was 154 s, and the emitted audio was 3.744 s — from which this concluded the node trims. It does not. `comfy_extras/nodes_minimax_h3.py` hands standalone `ref_audios` straight to `_encode_ref_audio`, which resamples and VAE-encodes the **entire waveform** with no truncation anywhere; reference *videos* are cut to `frame_count`, their audio is not, and neither is standalone audio. The 3.744 s figure is the **generated** latent's own length from `temporal_shape(length)` — 90 frames at 24 fps — and is independent of the reference entirely. So a 3.75 s shot was conditioned on **154 seconds of song riding through every sampling step**. Two coincidental numbers were read as cause and effect, and the mistake survived because it made a plausible story.
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

## MiniMax H3 keyframe (first frame / first+last)

**Immutable source:** `workflow_templates/reference_exports/h3-first-last-user-export.json` (SHA in `MANIFEST.md`; 28 nodes, 27 reachable — the single orphan is the *inherited* `ref2va` loader).

**Adapter path:** `build_h3_keyframe_payload` — 18 `mvp:` nodes serving both `first_last` and, per the Director's 2026-08-18 ruling, `image_to_video` (re-routed from the planned LTX I2V path; that evidence stays imported as the alternative). Frames resolve from the Shot's **citations by role** — `first` and `last` — never positionally.

**Checkpoint:** `minimax_h3_fl2va_pruned_int8_convrot.safetensors`, a dedicated first/last model. Bundle: `simple`/20/`res_multistep`, no LoRA and no profile (no evidence exists for one), `shift_video: 12`, `shift_audio: 3`, saver crf 19. Geometry shares `select_resolution`'s measured 0.6 MP / 16:9 / 32 default (1056×608); the export's in-graph sizing derives an unmeasured size from whatever image arrives and was deliberately not reproduced.

**Keyframe shots cannot lip-sync, and that is the node's fact, not a policy.** Live `/object_info`: `MiniMaxH3ImageToVideo` takes `clip/vae/prompt/width/height/length` plus **optional** `first_frame` and `last_frame` — first-only is schema-legal, which is what makes one adapter serve both modes — and **no audio input of any kind** exists on it or its conditioning path. `use_song_audio` is refused in `mode_specification_problems`' words rather than silently dropped, the mode-select labels end "no song lip-sync", and the pre-flight re-reads both schema facts live every run so a ComfyUI upgrade that adds audio would surface as a check failure, not a missed opportunity. Output audio exists but is sampler-generated, as on the text-only path. Consequence for planning, **corrected by the Director on 2026-08-18**: the dedicated keyframe modes have no song lip-sync — but that is a property of the simple path, not a ceiling. MiniMax's guide §2.2.2 uses a reference picture *as* a shot's first frame, keyframe or last frame, declared in the structured prompt ("<Picture 2> is the first frame of [Shot 1], showing …", retention `fully_preserved`) — on `MiniMaxH3ReferenceToVideo`, the node that takes the windowed master song. **Keyframes and lip-sync combine in references mode.** The dedicated keyframe adapter remains the efficient audio-less path; a singing shot that needs its first or last frame pinned uses references mode with `first`/`last`-role citations (see the keyframes-in-references story).

**Spectrum, now a checked fact instead of a one-line note.** `SpectrumApplyMiniMaxH3` sits enabled in this export *and in both reference exports*, and the shipped reference adapter omits it. The keyframe adapter mirrors that omission deliberately, and a chain-walk test asserts all three at once: enabled in the export, absent from both adapters' payloads, named in the stated-drops list — so the two H3 adapters cannot silently diverge on it.

**`length` ceiling:** the node's own 5–3600 on the 17k+5 grid, refused locally at 422 rather than surfacing as ComfyUI's opaque 502.

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

**Verified live 2026-08-18, and the measurement carries a warning.** One 3.75 s H3 take was enhanced: 640×384 / 90 frames → **1920×1152 / 89 frames**, 3.750 s → 3.708 s, audio surviving as aac, 549 KB → 2.98 MB. Aspect is preserved exactly (1.6667 both sides) and the longest side lands on 1920 as configured. **89 = 8 × 11 + 1**, which confirms the predicted mechanism: the VHS loader's `format: "LTXV"` puts the clip on the 8k+1 grid at load, and the input's 90 is H3's own 17k+5 grid (17 × 5 + 5). The two generator families genuinely disagree about frame counts and the conversion is real. `Shot.latest_output` still pointed at the H3 take afterwards — the enhancement is a sibling file, not a replacement.

> **It is not an upscale, and it will break lip-sync.** The enhanced clip is markedly sharper — corset lacing, arm-guard studs, individual hair strands, roof trusses and light fixtures all resolve — but it is also a **different performance**. Sampled at frames 20, 44 and 70, the source is mid-vowel with teeth visible while the enhanced clip has the mouth closed, consistently rather than as a one-off. The cause is visible in the graph rather than inferred: `ManualSigmas` begins at **0.909375**, a high starting noise level, so this is substantial re-generation and not a light detail pass. For a music video whose entire premise is H3's audio-driven lip-sync, that is disqualifying on any shot where the performer is singing. Treat it as usable on B-roll, objects, scenery and non-vocal shots, and as unproven-to-harmful on vocal ones. Lowering the starting sigma is the obvious lever and is explicitly an Ask First tuning decision, not something to change quietly.

## LTX 2.5 video extension (standalone)

**Immutable source:** `workflow_templates/reference_exports/ltx25-videoextender-user-export.json` (61 nodes). Its silent sibling `ltx25-videoextender-noaudio-user-export.json` (65 nodes) is audited alongside and **not** built — see below. SHA-256 for both in the reference-exports `MANIFEST.md`, and pinned again in `tests/test_workflows.py`.

**Purpose:** continue an existing take past its own end. H3 cannot render a window longer than about 15 s, so a shot that wants to run twenty seconds has had no path at all. This is that path — and the Director's own framing is that it is a **special case for a music video** and a **capability worth having ready** for story-based production, where a twenty-second dialogue-free establishing shot is ordinary. Build it as a general extender, not as a fix for an urgent music-video problem.

**Nothing has been rendered.** The adapter is **schema-audited only**: every node class and every one of its five model files was validated against live `/object_info` on 2026-08-20, and both payload variants validate clean. No LTX render was submitted, no GPU time was spent, and no frame from this graph has been measured.

**Adapter path:** `build_ltx25_extend_payload(source_video, prefix, extend_seconds, reference_seconds=3, prompt="", seed=…, frame_rate=24, width=1920, height=1080, include_audio=True)` — 49 nodes with audio, 46 without, and both are their own complete reachable subgraph. `source_video` is a path to an existing take; per AD-12 the caller hands it the **approved** file where approval exists, and this builder deliberately knows nothing about takes, shots or the master song.

**No route and no UI.** Deliberately: when extension is offered, and to which takes, is a separate editorial decision.

**How it works.** The loader reads the take at a forced `frame_rate` and every frame is stretched to `width × height`. The batch is split — a *head* (everything but the last `reference_seconds`) and a *tail* of `reference_seconds × frame_rate + 1` frames. The tail, at half scale, is the model's conditioning; `LTXVAudioVideoMask` marks the seconds after it as the region to invent, `max_length="pad"` grows the latent to hold them, and eight steps from sigma 1.0 fill them in at low resolution. `LTXVLatentUpsampler` doubles the result, the tail's first frame is re-anchored by `LTXVImgToVideoInplace`, and three more steps from sigma 0.85 refine it. The output is the untouched **head** followed by the model's rendering of the **tail plus the new seconds**.

**Models, exactly five:** `ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors`, `ltx-2.5-video-vae-conv-bf16.safetensors`, `ltx-2.5-audio-vae-bf16.safetensors`, `gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors`, and `ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors`. All five confirmed installed against live combo options.

**`LatentUpscaleModelLoader` is reached here, and that is the reachability finding.** It is an *orphan* in the enhancer export and in the audio-replacer export, and both of those adapters are right to leave its model out of their dependency lists. In this graph node `16` feeds `LTXVLatentUpsampler`, which is the whole low-res-then-upscale shape of the thing. Same class name, opposite answers; only the walk can tell them apart, and a remembered rule of thumb would have dropped a model this graph loads. `tests/test_workflows.py` asserts all three exports at once.

**`ManualSigmas`, and what it means.** Two schedules: the base pass runs `1.0, 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0` — full noise, eight steps — and the refine pass runs `0.85, 0.7250, 0.4219, 0.0`. For comparison the enhancer starts at 0.909375, and *that* was measured to move lip position. So this graph re-generates at least as hard, and it does so over the reference tail as well as the new seconds: nothing splices the source's last frames back in, they come out of the model.

> This is recorded as a **characteristic, not a defect**. Extension suits a long b-roll cut — an establishing shot, an environment, a texture — and would visibly disturb a performance shot, because the seam second is re-generated and the new seconds are invented outright. Lip-sync in the extended section will not survive, and the Director's ruling is that a shot with continuous singing past 15 s is an unusual ask. It is a note for the user's judgement; **nothing in the code refuses a shot on these grounds** and no machinery was added to protect lip-sync.

**Why the audio export and not the silent one.** The chosen export has **zero orphans** — all 61 nodes reachable from its saver, so nothing about which nodes to build is a judgement call. The no-audio export reaches 54 of 65, and its 11 orphans are its abandoned audio tail: an edit in progress rather than a finished graph. It also mis-wires `LTXVEmptyLatentAudio.frame_rate` to the "Extend video by (seconds)" constant (10) instead of the framerate constant (24), on both instances — transcribing that reproduces a defect and correcting it invents a graph nobody rendered. And it saves nothing: the silent path still loads all five model files, audio VAE included, still runs an audio branch (fed with silence), and adds two third-party `easy clean*` VRAM nodes. It is **available** and audited as a counter-example; it is not built.

**`include_audio` decides only what reaches the saver.** True (the export's shape) writes the source's own track, loudness-normalised to −16 LUFS, with the model's invented continuation concatenated after it. False drops the audio decode, trim and concat and the saver's optional `audio` link, writing picture only — what a cut destined for a separate sound pass wants. It does **not** reproduce the no-audio export, which additionally replaces the audio *conditioning* with an empty latent. Either way the take must carry an audio track: the conditioning tail is encoded through the audio VAE in both shapes.

**Six node classes replaced or dropped, each stated.** `VHS_LoadVideo → VHS_LoadVideoPath` for the reason the other two LTX adapters record (its combo enumerates ComfyUI's *input* directory; takes live under *output*). `Power Lora Loader (rgthree)` is **dropped rather than substituted** — unlike the enhancer's, this export's node carries no `lora_*` entry at all, so it applies nothing. `SimpleCalculatorKJ → CM_IntBinaryOperation` / `CM_FloatBinaryOperation`, because its `variables` is a `COMFY_AUTOGROW_V3` group keyed by `names` with neither a `prefix` nor a `max`, which the shared pre-flight cannot expand — its values would ride into ComfyUI unchecked. `ResizeImageMaskNode → ImageScaleBy`, same reason for a `COMFY_DYNAMICCOMBO_V3` sub-input. `ImpactImageInfo` and `CM_IntToFloat` folded away — the first measures a batch `GetImageSizeAndCount` already measures, the second only widened integers the adapter now folds. A pre-flight check confirms **every replaced class is installed**, so each substitution is provably about schema shape and not about a missing node.

**Four numbers folded into Python.** The export computes six in graph nodes; four are functions of the caller's own arguments because `force_rate` pins the loaded rate to `frame_rate`. `tests/test_workflows.py` re-derives all four from the export's own expressions and constants — and again at three frame rates the export never carried, so a rate hardcoded in the adapter cannot hide behind the export's 24. The two that genuinely need the file (the head's frame count, where the reference audio starts) stay in the graph.

**Output length is measured, never asserted.** `format="LTXV"` conforms the loaded count to 8n+1 before anything else, the mask pads to a latent boundary rather than an exact second, and a fractional `extend_seconds` has no exact frame count to land on. The existing frame-count guard test covers `tests/preflight_ltx25_extend.py` as well.

**Audit:** `uv run python tests/preflight_ltx25_extend.py [base_url] [--record]`. Five checks beyond the shared per-node validation: model files read out of the payload, the container-extension list against the node's own, five restated ceilings against the inputs that declare them, the replaced classes confirmed registered, and the reachability argument across both exports. Each check is driven against a mutated schema in the suite, and blanking any one of them is shown to turn a red audit green — so a gutted-but-green audit fails a test.

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
| LTX 2.5 extend | n/a | explicit 49-node adapter (46 silent), the audio export's whole reachable subgraph | yes | **schema-audited only 2026-08-20** — 43 classes and all five model files validated against live `/object_info`, both variants clean. **Nothing rendered**; no frame from this graph has been measured | no route, no UI |
| SeedVR2/RTX/FILM | yes | pending | no | **all three stages ran live 2026-08-17** inside the reference chain | no |
