# Roadmap and Verification Status

## Implemented now

- [x] Standalone project root and runtime
- [x] Atomic project manifests
- [x] WAV/FLAC/MP3 upload
- [x] Browser waveform and automatic imported-song duration
- [x] Direct MiniMax Music 3 generation
- [x] SongPlanner invented-lyrics generation (FR-13) — unit-tested against a recorded `/object_info` fixture; live generation not yet verified (pending Story 1.3's ≤16 s run)
- [x] SongPlanner known-lyrics cover generation (FR-14) — unit-tested (lyric sheet reaches the payload unchanged apart from edge-whitespace stripping, payload equality with the invented variant except lyric handling, route and preset coverage); live generation not yet verified (pending Story 1.3)
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

1. ~~Run one short, low-cost text-only H3 shot through the new adapter and inspect/probe its output.~~ **Done 2026-08-16** — see the verification section below.
2. ~~Report running renders as `running` rather than `queued`.~~ **Done 2026-08-16** — refresh consults `/queue` when history is empty; verified live.
3. ~~Decide whether the payload should send `aligned_frames`.~~ **Done 2026-08-16** — it now does. A live off-grid render confirming it is recorded below.
4. Run the H3 Ultra reference path live; only the text-only path has real evidence.
5. Add Director image and custom-audio reference serialization using project-contained media.
6. Add take comparison and explicit approval.
7. Build a standalone LTX 2.5 enhancement graph that accepts an approved take instead of regenerating H3.
8. Run the repaired SeedVR2→dimension-normalization→LTX boundary live.

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
- Python suite: 84 passing. Ruff and JavaScript syntax checks pass. Browser QA is also a required release gate; use the commands in `README.md`.
- Music 3: prior real eight-second integrated FLAC successful in ComfyUI.
- SongPlanner invented lyrics (FR-13): adapter, route, and Song-workspace variant selector implemented 2026-08-16. `tests/preflight_songplanner.py` confirmed all 10 node classes and every model/combo value against live ComfyUI 0.33.1 `/object_info` and recorded `tests/fixtures/object_info.json`; unit tests validate payloads offline against that fixture. **No live generation has been run** — the short end-to-end run is Story 1.3.
- SongPlanner known lyrics (FR-14): second thin builder over the shared core, optional `lyrics` on the songplanner route, and a third Song-workspace preset implemented 2026-08-16. The audited Known_Lyrics export (SHA-256 verified) differs from the invented export only in node `45.lyrics`/preview `58.source`; unit tests assert payload equality except lyric handling and pin the lyric-handling contract exactly — the route strips leading/trailing whitespace only, and interior blank lines and indentation reach the payload unchanged. The offline `/object_info` fixture validates the known variant via the public builder. **No live generation has been run** — live verification is pending Story 1.3.
- Flux API adapter: structurally matched to live `/object_info`; live smoke pending in this app.
- Krea: prior one-step character-sheet generation successful; current multiview adapter unit-tested; live app smoke pending.
- Director compiler: `MiniMaxH3DirectorCS` is registered on 8188; app timing now uses `start`/`length`, while full media/reference conversion remains pending.
- H3 text-only adapter: **verified live end to end on 2026-08-16.** A 3.75 s / 90-frame shot at 640×384 and 4 steps was submitted from the application to ComfyUI 0.33.1, accepted with HTTP 202, executed on the RTX 5090, and returned `music-video-producer/<project>/shots/<shot>-h3_00001-audio.mp4`. `ffprobe` confirms h264 640×384, 90 frames, 3.750 s, with synchronized AAC audio at 32 kHz. Wall clock was roughly twelve minutes, dominated by loading the ~31 GB model stack; free VRAM fell from 32 GB to 1.1 GB during sampling. The job reconciled to `complete`, `latest_output` was written, and `approved_output` correctly stayed empty.
- H3 frame grid: **off-grid alignment verified live 2026-08-16.** Two 4.0 s shots (96 requested frames, off the 17k+5 grid) were submitted after the payload was changed to send `DirectorTimeline.aligned_frames`. Both rendered at exactly 107 frames, `ffprobe` duration 4.458 s. Note the consequence: an aligned clip is longer than its shot window, so assembly must trim.
- Model residency: **measured 2026-08-16.** Two identical 107-frame H3 renders submitted back to back completed in 438 s and 288 s. ComfyUI keeps the model stack resident between consecutive prompts, saving roughly 150 s per subsequent render. No warm-batching machinery is required; the requirement is only to avoid interleaving other workflow kinds into a shot batch.
- SageAttention: `sageattention` is **not installed** in ComfyUI's embedded Python, so the `PathchSageAttentionKJ` pin to `disabled` is correct rather than a leftover error. `triton` and torch 2.7.0+cu128 are present, so it is installable; whether a Blackwell-compatible build helps remains an unmeasured spike.
- Director defect sweep: seven defects found and fixed on 2026-08-16 — creative-document data loss, `queued`-while-running, silent empty shot application, unaligned frames, mixed path separators, unbounded planned shot durations, and a `ruff check .` gate broken by vendored tooling. See `docs/DEVELOPMENT-LOG.md`.
- SeedVR2: real 192-frame upscale completed at 1250×720.
- LTX: downstream VAE encode failed on width 1250; the audited graph now normalizes to a multiple of 16 before all LTX image consumers.
- H3/LTX full renders: not complete and not claimed complete.

Update this document whenever a live verification changes readiness.
