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

SeedVR2 completed a real 192-frame upscale at 1250×720. The following LTX VAE encode failed because width 1250 is not divisible by its 4-pixel patch size. The audited adapter inserts `ImageResizeKJv2` between SeedVR2 cleanup and all three LTX image consumers with `width=0`, `height=0`, and `divisible_by=16`; KJNodes therefore preserves the source dimensions and rounds 1250×720 to 1248×720. The combined graph is not exposed for submission because it still regenerates H3 from creator-specific media. A standalone approved-take LTX adapter remains pending.

## Readiness matrix

| Workflow | Catalog discovery | API payload | Unit tests | Live prior model validation | App submission |
|---|---:|---:|---:|---:|---:|
| Flux Image Gen | yes | yes | yes | models present | yes |
| Music 3 direct | yes | yes | yes | generated FLAC previously | yes |
| Krea multiview | yes | yes | yes | one-step sheet previously | yes |
| Director compile | yes | start/length timing | yes | live compiler accepts timing scaffold | timeline only |
| H3 text-only shot render | yes | explicit 15-node adapter | yes | schema/classes/models verified; app render pending | yes |
| H3 Ultra reference/audio shot | yes | explicit 18-node adapter | yes | live schema/classes and real vision path verified | yes |
| LTX 2.5 enhance | yes | combined export audited + boundary patch | yes | reached VAE encode; boundary diagnosed | no |
| SeedVR2/RTX/FILM | yes | pending | no | SeedVR2 192-frame upscale passed; later stages pending | no |
