# Roadmap and Verification Status

## Implemented now

- [x] Standalone project root and runtime
- [x] Atomic project manifests
- [x] WAV/FLAC/MP3 upload
- [x] Browser waveform and automatic imported-song duration
- [x] Direct MiniMax Music 3 generation
- [x] OpenAI-compatible conversational treatment planner
- [x] Flux character/setting/prop/style generation
- [x] Media asset library
- [x] Krea QuadView multiview promotion
- [x] Direct shot add/drag/resize/split/duplicate/delete
- [x] Asset-to-shot references
- [x] Director timeline JSON and H3 frame alignment
- [x] Text-only H3 Director shot submission with explicit model dependencies
- [x] Persistent Comfy prompt IDs, job refresh, outputs, and errors
- [x] Purpose-built local editor UI

## Next vertical slice

1. Run one short, low-cost text-only H3 shot through the new adapter and inspect/probe its output.
2. Add Director image and custom-audio reference serialization using project-contained media.
3. Add take comparison and explicit approval.
4. Build a standalone LTX 2.5 enhancement graph that accepts an approved take instead of regenerating H3.
5. Run the repaired SeedVR2→dimension-normalization→LTX boundary live.

## Production editing

- [ ] Undo/redo command history
- [ ] Ripple editing and snapping modes
- [ ] Beat/BPM analysis and multiresolution waveform peaks
- [ ] Automatic verse/chorus/bridge candidates
- [ ] Lyrics alignment lane
- [ ] Markers and shot-range loop playback
- [ ] Thumbnail filmstrips
- [ ] Multi-select and batch operations
- [ ] Locked-field LLM change review

## Rendering and delivery

- [x] H3 text-only shot submission from a corrected selected-path adapter
- [ ] H3 image/reference/audio shot submission
- [ ] Audio range extraction per shot
- [ ] Multiple takes and approval
- [ ] LTX 2.3/2.5 enhancement
- [ ] SeedVR2 and RTX VSR
- [ ] FILM interpolation
- [ ] FFmpeg song-synchronized assembly
- [ ] Draft/master export presets
- [ ] ffprobe verification and contact sheet

## Verification status on 2026-08-16

- First-run project creation: repaired and verified in headless Edge against an empty isolated data root; all five workspaces opened with zero severe console errors.
- Python suite: 46 passing. Ruff and JavaScript syntax checks pass. Browser QA is also a required release gate; use the commands in `README.md`.
- Music 3: prior real eight-second integrated FLAC successful in ComfyUI.
- Flux API adapter: structurally matched to live `/object_info`; live smoke pending in this app.
- Krea: prior one-step character-sheet generation successful; current multiview adapter unit-tested; live app smoke pending.
- Director compiler: `MiniMaxH3DirectorCS` is registered on 8188; app timing now uses `start`/`length`, while full media/reference conversion remains pending.
- H3 adapter: all required node classes and model files are present; API/UI submission is wired, but no expensive render was automatically queued during integration.
- SeedVR2: real 192-frame upscale completed at 1250×720.
- LTX: downstream VAE encode failed on width 1250; the audited graph now normalizes to a multiple of 16 before all LTX image consumers.
- H3/LTX full renders: not complete and not claimed complete.

Update this document whenever a live verification changes readiness.
