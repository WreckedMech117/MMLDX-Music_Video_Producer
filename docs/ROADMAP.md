# Roadmap and Verification Status

## Implemented now

- [x] Standalone project root and runtime
- [x] Atomic project manifests
- [x] WAV/FLAC/MP3 upload
- [x] Browser waveform and automatic imported-song duration
- [x] Direct MiniMax Music 3 generation
- [x] SongPlanner invented-lyrics generation (FR-13) — **verified live 2026-08-17**: a 30 s request produced a 29.989 s FLAC through the running application; see the verification section below
- [x] SongPlanner known-lyrics cover generation (FR-14) — **verified live 2026-08-17**: a 30 s request with a lyric sheet produced a 29.989 s FLAC through the running application; see the verification section below
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
8. ~~Run the repaired SeedVR2→dimension-normalization→LTX boundary live.~~ **Done 2026-08-17** — the full reference chain ran clean in 17 min 36 s; measured dimensions in the verification section below.
9. ~~Run one short song through each SongPlanner adapter and probe the produced audio.~~ **Done 2026-08-17** — both adapters verified live at the model's 30 s floor rather than the epic's ≤16 s target; see the verification section below.
10. Teach assembly the LTX frame-count grid. The LTX boundary does not preserve frame count — the live run measured 192 frames in, 185 out, an 8k+1 grid — so assembly trim math must handle it alongside H3's existing 17k+5 alignment, and no verification step may assert that LTX output has the same frame count as its input. Currently documented as a consequence in `docs/WORKFLOW-MAP.md`, `docs/OPERATIONS.md`, and `docs/DEVELOPMENT-LOG.md` with no code owning it.
11. Put `tests/smoke_h3_app.py` behind the same `--confirm-gpu` gate `tests/smoke_songplanner_app.py` uses. It currently submits a real render as soon as it is invoked, which is the one live-cost script with no cost gate.

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
- Python suite: 84 passing as of this section's date. Historical — see the 2026-08-17 section for the current count.
- Music 3: prior real eight-second integrated FLAC successful in ComfyUI.
- SongPlanner invented lyrics (FR-13): adapter, route, and Song-workspace variant selector implemented 2026-08-16. `tests/preflight_songplanner.py` confirmed all 10 node classes and every model/combo value against live ComfyUI 0.33.1 `/object_info` and recorded `tests/fixtures/object_info.json`; unit tests validate payloads offline against that fixture. **Live generation verified 2026-08-17** — see the SongPlanner live smoke entry below.
- SongPlanner known lyrics (FR-14): second thin builder over the shared core, optional `lyrics` on the songplanner route, and a third Song-workspace preset implemented 2026-08-16. The audited Known_Lyrics export (SHA-256 verified) differs from the invented export only in node `45.lyrics`/preview `58.source`; unit tests assert payload equality except lyric handling and pin the lyric-handling contract exactly — the route strips leading/trailing whitespace only, and interior blank lines and indentation reach the payload unchanged. The offline `/object_info` fixture validates the known variant via the public builder. **Live generation verified 2026-08-17** — see the SongPlanner live smoke entry below.
- Flux API adapter: structurally matched to live `/object_info`; live smoke pending in this app.
- Krea: prior one-step character-sheet generation successful; current multiview adapter unit-tested; live app smoke pending.
- Director compiler: `MiniMaxH3DirectorCS` is registered on 8188; app timing now uses `start`/`length`, while full media/reference conversion remains pending.
- H3 text-only adapter: **verified live end to end on 2026-08-16.** A 3.75 s / 90-frame shot at 640×384 and 4 steps was submitted from the application to ComfyUI 0.33.1, accepted with HTTP 202, executed on the RTX 5090, and returned `music-video-producer/<project>/shots/<shot>-h3_00001-audio.mp4`. `ffprobe` confirms h264 640×384, 90 frames, 3.750 s, with synchronized AAC audio at 32 kHz. Wall clock was roughly twelve minutes, dominated by loading the ~31 GB model stack; free VRAM fell from 32 GB to 1.1 GB during sampling. The job reconciled to `complete`, `latest_output` was written, and `approved_output` correctly stayed empty.
- H3 frame grid: **off-grid alignment verified live 2026-08-16.** Two 4.0 s shots (96 requested frames, off the 17k+5 grid) were submitted after the payload was changed to send `DirectorTimeline.aligned_frames`. Both rendered at exactly 107 frames, `ffprobe` duration 4.458 s. Note the consequence: an aligned clip is longer than its shot window, so assembly must trim.
- Model residency: **measured 2026-08-16.** Two identical 107-frame H3 renders submitted back to back completed in 438 s and 288 s. ComfyUI keeps the model stack resident between consecutive prompts, saving roughly 150 s per subsequent render. No warm-batching machinery is required; the requirement is only to avoid interleaving other workflow kinds into a shot batch.
- SageAttention: `sageattention` is **not installed** in ComfyUI's embedded Python, so the `PathchSageAttentionKJ` pin to `disabled` is correct rather than a leftover error. `triton` and torch 2.7.0+cu128 are present, so it is installable; whether a Blackwell-compatible build helps remains an unmeasured spike.
- Director defect sweep: seven defects found and fixed on 2026-08-16 — creative-document data loss, `queued`-while-running, silent empty shot application, unaligned frames, mixed path separators, unbounded planned shot durations, and a `ruff check .` gate broken by vendored tooling. See `docs/DEVELOPMENT-LOG.md`.
- SeedVR2: real 192-frame upscale completed at 1250×720.
- LTX: downstream VAE encode failed on width 1250. Fixed and verified live on 2026-08-17 — see the next section.

## Verification status on 2026-08-17

- Python suite: **103 passing, 0 failing**, measured on a settled tree with all three work streams landed (LTX boundary repair, SongPlanner live smoke, SongPlanner request bounds). `ruff check .` clean; `node --check` clean on both `app.js` and `api.js`. Browser QA is also a required release gate; use the commands in `README.md`. Live GPU smokes are manual, gated on `--confirm-gpu`, and not part of this gate set.
- SongPlanner live smoke (FR-13 and FR-14): **both adapters verified live end to end on 2026-08-17** by `tests/smoke_songplanner_app.py`, driving the running application on port 8766 against ComfyUI 0.33.1 at `127.0.0.1:8188` with an isolated data root. The pre-flight audit passed first (`OK 20 nodes across 2 variants (10 classes)`). One project per adapter, because every `kind=music` job targets `"song"` and a shared project's second run would clobber the first. Nothing in persisted state marks a song as SongPlanner's, so this mapping is the record:
  - Invented (`build_songplanner_invented_payload`), `project_8d4fa036ab9d` / `job_7c607624bd5d`, prompt `d7f86956-243c-45bf-ad48-ddf327588d3b`, seed `20260816`, complete in 216.7 s → `music-video-producer/project_8d4fa036ab9d/songs/Invented Smoke_00001.flac`. `ffprobe`: flac, 44100 Hz, 2 channels, **29.989 s**, 2,496,336 bytes.
  - Known lyrics (`build_songplanner_known_lyrics_payload`), `project_cb0ff891977a` / `job_8c35a0c5fdf6`, prompt `d09f929e-0794-4502-889a-ee1593e76bba`, seed `20260817`, complete in 185.0 s → `music-video-producer/project_cb0ff891977a/songs/Known Lyrics Smoke_00001.flac`. `ffprobe`: flac, 44100 Hz, 2 channels, **29.989 s**, 2,617,365 bytes.
  - Requested length is not produced length: both requests asked for 30 s and `Song.duration` stores `30.0`, while the produced audio measures 29.989 s (re-probed independently at 29.988571 s). The encoder resolves its own length; record the measured value.
  - Playback evidence, stated exactly to its level: after job refresh the app points the player at the ComfyUI `/view` URL, and for both songs that URL returned HTTP 200 with a byte count matching the file on disk, and those returned bytes were themselves `ffprobe`-decodable FLAC. **A browser was not driven, so browser playback is not claimed.**
  - The epic's "≤16 s" target was **not** met and is not achievable: `M3SongPlanner.duration_seconds` carries a schema floor of `min: 30.0`, and a first attempt at 16 s was rejected by ComfyUI at prompt validation (`value_smaller_than_min`) before any GPU time was spent. The floor is recorded in the story's Spec Change Log; the epic's wording stands pending human ratification.
- SongPlanner request bounds: the rejected 16 s attempt showed that numeric schema ranges were never validated offline — the pre-flight audit checks classes, input names, and combo values, but not numeric `min`/`max`. Closed under `_bmad-output/implementation-artifacts/spec-songplanner-duration-bounds.md`, which corrected the route's request bounds to 30–300 s and the planner seed ceiling to 4294967295.
- LTX 2.5 divisor correction: the boundary patch carried `divisible_by=16`, which clears the VAE's 4-pixel patchify but leaves height 720 against a total spatial compression of 32 (720/32 = 22.5). Raised to 32 in `patch_ltx25_dimension_boundary` and in the Director's saved workflow, which had never received the fix at all. `workflow_templates/reference_exports/h3-ltx25-user-export.json` deliberately still captures the pre-fix graph — the patch is applied in memory at submission time. The ratified planning artifacts (`ARCHITECTURE-SPINE.md` AD-12, `epics.md`) were amended to 32 with the live evidence cited, closed under `_bmad-output/implementation-artifacts/spec-ltx25-dimension-boundary-repair.md`.
- LTX 2.5 boundary: **verified live end to end on 2026-08-17.** The patched reference graph (`patch_ltx25_dimension_boundary` applied in memory, divisor 32) was submitted once to ComfyUI 0.33.1 as prompt `a64a0460-64e6-4a14-b207-e644bf9bda5d`; it ran to `success` in 17 min 36 s with no errors. Measured chain, from `ffprobe` on the produced files rather than from prediction:
  - H3 base 1056×608, 192 frames, 8.000 s (`temp/minimax-h3-music/vid_00003-audio.mp4`).
  - SeedVR2 1250×720, 192 frames — the exact size that raised `einops.EinopsError` on the three previous runs (`temp/.../vid_00004-audio.mp4`).
  - LTX 2.5 output 2496×1408, 185 frames, 7.708 s (`output/minimax-h3-music/vid_00003-audio.mp4`). The LTX subgraph applies a 2× latent upsample, so 2496×1408 is exactly 2 × **1248×704** — direct measured confirmation the normalizer produced a 32-divisible size. Divisor 16 would have given 1248×720 and therefore 2496×1440.
  - FILM + RTX VSR 3744×2112, 369 frames, 48 fps, 7.688 s (`output/minimax-h3-music/vid-vfi_00001-audio.mp4`).
  - Frame count is not preserved across the boundary: 192 frames in, 185 out (8k+1), 8.000 s → 7.708 s. Assembly must account for this, as it already must for H3's 17k+5 alignment. Tracked as next-slice item 10.
  - Aspect is not preserved either, and the cost is measured: `keep_proportion: "resize"` resamples without cropping, so 1250×720 → 1248×704 stretches the frame 2.1% horizontally (aspect 1.73611 → 1.77273). Divisor 16 would have been gentler on aspect but does not divide the VAE stack. Whether the stretch is visible has **not** been assessed — no one has looked at the frames.
  - This exercised the reference graph, which regenerates H3 from creator-specific media. **Standalone LTX submission from the application is still not implemented** and is not claimed.
- H3/LTX full renders: the reference LTX 2.5 chain now completes; application-driven LTX submission is not complete and not claimed complete.

Update this document whenever a live verification changes readiness.
