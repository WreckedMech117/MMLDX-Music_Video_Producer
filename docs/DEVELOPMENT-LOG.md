# Development Log

## 2026-08-16 — H3 Ultra multi-reference and vision continuity

- Preserved and checksummed the new MiniMax References-to-Video API export.
- Audited all 29 nodes and confirmed every class is registered in live ComfyUI.
- Added an explicit 18-node Ultra adapter for ordered character, environment, video, and audio references.
- Added master-song audio references and deterministic `<Picture N>`, `<Video N>`, and `<Audio N>` prompt maps.
- Added persistent LM Studio vision inspection for assets and generated takes; video inspection uses a four-frame contact sheet.
- Ran a real vision smoke inspection on the Krea multiview output and received validated continuity cues and risks.
- Kept automated review separate from editorial approval.

## 2026-08-16 — Persistent audio transport and LM Studio verification

- Reproduced imported-audio playback failure in Edge: the application had no media element or transport implementation, and its decoded buffer existed only in the current file-selection session.
- Added a persistent native audio element backed by the project media endpoint, synchronized global/timeline controls, waveform seeking, live playheads, and reload-time waveform hydration.
- Added backend `ffprobe` duration recovery when the browser submits an unknown duration.
- Added browser QA that imports a real WAV fixture, reloads the application, starts playback, observes advancing time, and checks severe console errors.
- Activated the ignored runtime `.env` from the user-supplied LM Studio example values.
- Updated Director structured output to LM Studio's JSON-schema mode and added loaded-instance reuse for `model-name:N` IDs.
- Ran a real LM Studio Director smoke request and received one validated five-second shot.

## 2026-08-16 — API exports and H3 submission slice

- Imported immutable, checksummed reference copies of Flux, H3 Director, H3→LTX 2.3, and H3→LTX 2.5 API exports.
- Audited every exported class against live ComfyUI `/object_info`; all classes are registered.
- Found the H3 Director export omitted required CLIP/video-VAE/audio-VAE links through virtual/shared editor wiring.
- Replaced that wiring with a self-contained 15-node text-only H3 adapter using explicit installed model loaders.
- Added a persistent H3 shot-submission endpoint and automated coverage for confirmed bulk queueing of ready text-only shots.
- Added `Shot.latest_output` so completion remains distinct from explicit approval.
- Added and tested the SeedVR2→LTX dimension-normalization patch across VAE encode, image-to-video, and image-size consumers.
- Kept combined LTX exports reference-only because they contain creator media paths and regenerate H3 instead of accepting an approved take.

## 2026-08-16 — First-run repair and independent review

- Fixed the async form-event lifetime bug that caused `event.currentTarget.reset()` to dereference `null` after project creation.
- Added real Edge first-run QA covering project creation and all five workspaces.
- Normalized ComfyUI `success` history to application `complete` state and corrected output subfolder preview URLs.
- Blocked Windows project-ID traversal and arbitrary local-file reads through forged asset manifests.
- Added upload size/type limits, serialized shot saves, document-only persistence, and stale full-project revision rejection.
- Made Director chat preserve existing shots by default and preserve shot provenance during explicit plan application.
- Corrected Director segment timing to `start` plus `length`.
- Diagnosed the real finishing failure: SeedVR2 succeeded at 1250×720; downstream LTX VAE encode rejected width 1250.
- Confirmed the Flux API export and documented selected-path export requirements for H3 Director and LTX 2.5.
- Disabled or relabeled UI controls whose backend stages are not implemented.

## 2026-08-16 — Standalone foundation

- Established `F:\MusicVideoProducer` as an independent application root.
- Explicitly excluded Agent OS from architecture, code, storage, and runtime.
- Created the persistent implementation plan under `.hermes/plans`.
- Selected FastAPI plus dependency-free ES modules for a local-first editor.
- Implemented project models and atomic filesystem persistence.
- Implemented bounded ComfyUI health, prompt, queue, history, output, and upload operations.
- Mapped the separated `Flux-Image-Gen.json` workflow into a controlled API payload.
- Mapped direct MiniMax Music 3 generation into a controlled API payload.
- Mapped the Krea 2 QuadView character sheet into a controlled API payload.
- Implemented configurable structured LLM treatment planning.
- Implemented shot timing validation, H3 17k+5 frame alignment, and timeline conversion.
- Built the initial high-polish Song, Treatment, Assets, Timeline, and Queue workspaces.
- Implemented browser waveform rendering and direct timeline clip drag/resize.
- Added project launch, setup, architecture, workflow, data, operations, LLM, and roadmap documentation.

## Test discipline

Each backend vertical slice was introduced with a failing test and then implemented. The full suite and browser validation commands are recorded in the README and operations guide.

## Known incomplete work

- H3 Director render submission
- LTX and post-processing payloads
- Song structure/BPM analysis
- Final timeline assembly/export
- Full multiview and Flux smoke renders from the application itself

These remain visible in the roadmap and are not represented as complete.
