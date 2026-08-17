# Development Log

## 2026-08-17 — Song replacement safety, and a missing removal path

Closed under `_bmad-output/implementation-artifacts/spec-1-3c-song-replacement-safety.md`. Every path that changed a project's Song overwrote it unconditionally and silently, and there was no way to remove a Song at all except hand-crafting a full-project `PUT`. The epic's "no Shot data is deleted" guarantee held by accident rather than by design or test.

- Added one guard, `_require_song_replacement_confirmation(project, confirmed)` in `app.py`, raising 409 with a single shared message, `SONG_REPLACEMENT_CONSEQUENCE`, that names both dependencies: Shot windows are absolute seconds against the current song, and Assembly synchronization derives from it. It also says what is *not* at risk — no shot data is deleted, no shot window is moved — because a Director who fears losing work avoids the operation instead of understanding it. One rule, three call sites, no duplicated wording.
- The gate only fires when the project has **both** a Song and Shots. A first import, and any Shot-less project, stays frictionless; there is nothing whose timing a change can invalidate yet. Confirmation is a request-level flag (`confirm_song_replacement`, a form field on the import route and a request field on both generate routes), not stored state — the server states the consequence and refuses without acknowledgement, which is the `document_rejection` notice pattern combined with the `updated_at` 409's "refusal carries the reason" shape.
- Placement of the guard is load-bearing, not incidental. In `upload_song` it runs before `_copy_upload`, because songs are written under their own filename with no index prefix (unlike assets): a guard after the copy would have overwritten the audio it was refusing to replace. In `generate_music` and `generate_songplanner` it runs before `comfy.submit`, so a refusal costs no GPU time.
- Added `DELETE /api/projects/{project_id}/song` behind the same gate — the removal path that did not exist. It nulls `project.song`, leaves every Shot untouched, and **leaves the audio file on disk**: removal detaches, it does not destroy media, so a mistaken removal is undone by re-importing the same file. It 404s when there is no song to remove, whether or not the flag is set.
- The browser states the same consequence before sending, matching the existing `window.confirm` precedent for destructive and expensive acts. The wording lives in one exported constant, `SONG_CHANGE_CONSEQUENCE` in `api.js`, and the rule lives in one pure exported function, `songChangeNeedsConfirmation(project)`, mirroring the server's condition. `app.js` has a single `confirmSongChange(question)` helper used by the import, generate and remove handlers. It returns `proceed` and `confirmed` separately on purpose: only a real acknowledgement is sent as `confirm_song_replacement`, never a blanket `true`, or a stale local project (shots added elsewhere since it loaded) could defeat the server's gate without anyone reading the consequence.
- Added a "Remove song" control to the Song panel's waveform actions, disabled while the project has no song, and the generate path now asks the song question separately from the existing SongPlanner GPU-cost confirm — replacement happens at submit time, GPU cost is a different question, and no new render confirmation was introduced.
- Added nine collected tests. Five in `tests/test_api.py` covering the whole I/O matrix: all three unconfirmed replacement paths 409 together (with the uploaded filename deliberately colliding with the existing song's, so the pre-copy placement is what the test proves), confirmed replacement through all three routes, first import and Shot-less replacement/removal needing no flag, unconfirmed then confirmed removal, and removal with no song 404ing. Every mutating case re-reads through a **fresh `ProjectStore`** and compares `shot.model_dump(mode="json")` field-for-field; the refusal case also asserts the audio bytes are unchanged, and the removal case asserts the file still exists. Four in `tests/test_frontend_contract.py`: the consequence constant names both dependencies on both sides, the JS gate's truth table, a parity test that **executes** the Python and JavaScript gates over the same four project states and asserts they agree, and a source-level test that all three handlers call `confirmSongChange` *before* they send and pass the acknowledgement through rather than hardcoding it.
- **Initially not gated, then closed the same day.** The generic `PUT /api/projects/{id}` was left out on the grounds that it is the normal save path and gating it would break ordinary saves. Review demonstrated against the live app that it removed a Song with no acknowledgement (`PUT song=None` → 200, Song null, one Shot intact), leaving the epic's guarantee one HTTP call short of true. It is now gated in the narrow form that preserves ordinary saves: the guard applies only when the incoming Song differs from the stored one **and** the project has Shots, so an unchanged Song round-trips untouched and adding a first Song is not a replacement. Song replacement is therefore gated on all five routes that can change or remove a Song: song upload, direct Music 3, SongPlanner, `DELETE /song`, and the full-project `PUT`.
- **Still out of scope, and a different concern:** that same `PUT` can wipe **Shots** while carrying an unchanged Song. Shot-data protection through the generic save is not Song-replacement safety and is not addressed here.
- An in-flight music job is now matched to its Song by `prompt_id` before its output is reconciled, so removing or replacing a Song mid-render can neither attach a completed render to the wrong Song nor silently drop it — the output stays recoverable on the job.
- Stored song filenames now carry an index prefix, so a same-named re-import no longer destroys the previous audio.
- `uv run pytest` 117 passing, `uv run ruff check .` clean, `node --check` clean on `app.js` and `api.js`. That 117 is a shared-tree figure, not this change's own arithmetic — concurrent work was landing in the same tree; `docs/ROADMAP.md` remains the authority for the current count. Scope of the claim: these are offline tests. **The "Remove song" control and the three confirm dialogs have not been driven in a real browser**, so only the executable contract is claimed for the UI half.

## 2026-08-17 — LTX 2.5 boundary: crop instead of stretch

The Director ruled on the aspect tradeoff the boundary repair below surfaced: geometry is preserved, and trimming a few pixels is the acceptable price. The previous entry's stretch measurements stand as the record of what that run did.

- Read the schema and the source before changing anything, rather than guessing the enum. `keep_proportion="crop"` is a real value on live `/object_info`, and crop mode honours `width=0`/`height=0` exactly as resize did — `"crop"` is absent from the branch that recomputes the target, so it falls through to `width = W; height = H` and then floors to the divisor. **No hardcoded resolution was needed**, which was the open question: the patch still derives the target from whatever the graph produces.
- Switched `keep_proportion` from `"resize"` to `"crop"` in `patch_ltx25_dimension_boundary` and in saved-workflow node `6133`, keeping the two in agreement. `LTX25_DIVISOR = 32`, `width`/`height=0`, and `crop_position="center"` are unchanged — but `crop_position` now does real work, splitting the trimmed rows 7 top / 8 bottom instead of being inert. Backed the saved workflow up first to `04 - H3 Music Video - LTX 2.5 READY.20260817-072043.bak` (100,699 bytes, sha256 `2ae33cf817c989a6dc6cb96205b8880fdf7fde2b429a3e64592fddd242f66c2c`) and re-ran the full structural verification: 24 checks, all pass, node count unchanged at 61, no links changed, only node `6133` differs.
- **Verified the geometry by executing the installed node**, not by re-deriving its arithmetic — which is the criticism `deferred-work.md` correctly made of the earlier test. Running `ImageResizeKJv2` on a synthetic 1250×720 frame carrying a row-index ramp: crop retained source rows ~6–710 (15 lost) at **0.02%** residual distortion; resize retained all 720 rows at **2.07%**; both returned 1248×704 with differing content.
- **Ran it live once** (queue confirmed clear first). Prompt `0be806a0-4d5a-4c31-8338-7326534896ea`, `success` in 12 min 06 s, no errors. Every dimension matched the stretch run exactly — H3 1056×608/192f → SeedVR2 1250×720/192f → LTX **2496×1408**/185f/7.708 s → FILM+RTX VSR 3744×2112/369f — which is the expected result, since crop and stretch produce identical sizes. The boundary passed `VAEEncode` again.
- Because dimensions cannot distinguish the two modes, added an independent check: frame 40 of the crop run is best matched by a **~1.7% vertical zoom** of frame 40 of the stretch run (unimodal correlation peak at vertical scale 1.016–1.018, versus 0.9457 at identity). A real crop, in the predicted direction, close to the predicted 2.13%. Stated to its level: the two runs' diffusion detail differs because their inputs differ, so this A/B confirms *that* a crop happened while the node-level 0.02% is the precise figure.
- Found a new edge case the switch introduces: a sub-divisor frame with derived size raises `ZeroDivisionError` under crop mode (it divides by the floored target height) where resize mode raised `ValueError: height and width must be > 0`. Both fail, but the crop-mode failure is more obscure, so passing `source_size` matters more now, not less. Recorded in the docstring, `docs/OPERATIONS.md`, and the spec change log.
- Reworked the two LTX tests: one now pins the crop configuration (the only thing that distinguishes a correct graph from a stretching one), and the divisor test routes through `normalize_to_divisor` — the function the patch actually uses — instead of recomputing the flooring inline and asserting its own arithmetic.
- Wall clock fell 17:36 → 12:06 between the two runs. Not a crop effect; the second run reused a warm model stack.

## 2026-08-17 — LTX 2.5 dimension boundary repair

Closed under `_bmad-output/implementation-artifacts/spec-ltx25-dimension-boundary-repair.md`. The ratified planning artifacts it contradicted — `ARCHITECTURE-SPINE.md` AD-12, `epics.md:63` and the Story 7.1 Given clause, and the architecture `.memlog.md` — were amended to divisor 32 with the live evidence cited, rather than silently edited.

- Raised `divisible_by` from 16 to 32 in `patch_ltx25_dimension_boundary`. The LTX 2.5 video VAE's total spatial compression is 32 — a 4-pixel patchify followed by three stride-2 stages, and `comfy/sd.py:618` sets its `downscale_ratio` to 32 in both spatial axes. Divisor 16 clears the patchify check but leaves 1248×720, and 720/32 = 22.5 pushes a half cell through the conv stack; 32 gives 1248×704, exact at every stage.
- Root cause re-confirmed from `user/comfyui.log` line 1091 (2026-08-17 00:15:06), three identical failures: `ResolutionSelector` at 0.6 MP / multiple-32 yields 1056×608, SeedVR2 scales the shortest edge to 720 giving 720/608 × 1056 = 1250, and 1250 % 4 = 2. LTX 2.5's VAE sets `crop_input=False`, so unlike LTX 2.3 nothing auto-corrects it.
- Repaired the Director's saved editor workflow `04 - H3 Music Video - LTX 2.5 READY.json`, which had never received the fix the repo already carried. A byte-identical timestamped `.bak` was written beside it first (98,872 bytes, sha256 `df9a0bfb61e4ea8d…`), named without a `.json` suffix so ComfyUI's workflow browser does not list it. New node `6133` `ImageResizeKJv2` (`width=0`, `height=0`, `lanczos`, `keep_proportion=resize`, `divisible_by=32`, `device=cpu`) now sits between node `6112` `easy cleanGpuUsed` and subgraph instance `6116`; link `15327` was retargeted to the new node and new link `15328` carries its output into the subgraph's `image` input, which feeds all three LTX image consumers (`6070` `VAEEncode`, `4970` `LTXVImgToVideoInplace`, `6073` `GetImageSize`).
- Bypassed node `142` `PathchSageAttentionKJ` in the same file (mode 4, widget pinned to `disabled`). It had aborted three runs with `ModuleNotFoundError: sageattention`, and the repo adapter already disables it.
- Verified the repair by re-parsing: valid JSON, 60 → 61 nodes, no duplicate link ids, every top-level link endpoint and every node-declared link id resolvable, the subgraph definitions byte-equal, and exactly two pre-existing nodes (`142`, `6116`) plus one link (`15327`) touched.
- `workflow_templates/reference_exports/h3-ltx25-user-export.json` was deliberately **not** re-exported or edited. It is immutable audited evidence and captures the pre-fix graph; the normalization node is inserted in memory at submission time. A fresh export is the Director's action in ComfyUI.
- This change updates the divisor assertion and **adds exactly one collected test**, pinning 1250×720 → 1248×704. `ruff check .` and `node --check` clean. On suite totals: this work shared a tree with two other work streams, so no total here is this change's own arithmetic — the committed pre-session baseline was 84, and the settled figure once all three streams landed is **103 passing**, recorded in `docs/ROADMAP.md`, which remains the authority.
- **Ran the boundary live and it passed** (ROADMAP item #8, one submission). Prompt `a64a0460-64e6-4a14-b207-e644bf9bda5d`, submitted only after `/queue` showed nothing running or pending; `success` in 17 min 36 s with no errors in `comfyui.log`. Measured with `ffprobe`, not predicted: H3 base 1056×608 / 192 frames / 8.000 s → SeedVR2 1250×720 / 192 frames (the exact size that raised `einops.EinopsError` three times) → LTX 2.5 **2496×1408** / 185 frames / 7.708 s → FILM + RTX VSR 3744×2112 / 369 frames at 48 fps / 7.688 s. The LTX subgraph's 2× latent upsample makes 2496×1408 exactly 2 × 1248×704, so the produced file is direct evidence the normalizer emitted a 32-divisible size; divisor 16 would have produced 2496×1440.
- Recorded two consequences found by the run rather than predicted by the spec. **The boundary does not preserve frame count** — 192 frames in, 185 out (8k+1), 8.000 s → 7.708 s. Assembly must account for this the way it already must for H3's 17k+5 alignment; now tracked as next-slice item 10 in `docs/ROADMAP.md` rather than living only in prose. And **the boundary stretches the frame**: `keep_proportion: "resize"` resamples with `crop="disabled"`, so 1250×720 → 1248×704 is a 2.1% horizontal stretch (aspect 1.73611 → 1.77273, x scale 0.9984, y scale 0.97778). The earlier "without changing the intended aspect materially" wording was inherited from the divisor-16 text and was wrong for 32 — 16 was 0.16% off, 32 is 2.1% off. Corrected in `docs/WORKFLOW-MAP.md` and `docs/OPERATIONS.md`. Whether the stretch is visible has not been assessed; nobody has looked at the frames, and the docs say so.
- The run exercised the reference graph, which regenerates H3 from creator-specific media. Application-driven LTX submission is still not implemented and is not claimed; the standalone approved-take adapter remains ROADMAP item #7.

## 2026-08-17 — FR-12 import duration regression guard, and the frontend defect it found

Closed under `_bmad-output/implementation-artifacts/spec-1-3b-import-duration-regression-guard.md`. FR-12 was listed as verified, but the verification only covered the server. Writing the missing frontend guard turned up a real defect.

- Found the defect: the browser defeated the server's own ffprobe fallback. The `#song-file` change handler toasted on a failed decode but never cleared `state.audioBuffer`, and the import handler sent `state.audioBuffer?.duration || 0`. So importing an undecodable file into a project that already had a decodable song sent the **previous song's** duration; `app.py`'s `resolved_duration = duration if duration > 0 else _media_duration(target)` saw a non-zero value, ffprobe never ran, and a wrong length was persisted. Song duration is the timing spine every Shot window, playback sync, and Assembly derives from, so a wrong value is worse than a missing one.
- Fixed it in two places. The decode-failure path now nulls `state.audioBuffer`, re-renders, and resets the displayed duration to `—`, so nothing survives a failure that could be mistaken for a measurement of the new file. The sent value now comes from a pure exported `songImportDuration({ decoded })` in `api.js`, which returns 0 for a missing buffer and for any non-finite or non-positive duration, and ignores every other piece of state handed to it.
- Also corrected a display bug in the same handler: the success path set `#duration-value` and *then* called `renderSong()`, which immediately overwrote it with the previously loaded project song's duration. The two statements are now ordered so the newly chosen file's length is what stays on screen.
- Added four collected tests, each mutation-checked by reintroducing the defect and confirming the test fails. Two in `tests/test_frontend_contract.py`: the pure decision executed under the node harness (a decodable buffer sends its length; a failed decode sends 0; a failed decode **with a prior buffer present** still sends 0, plus the NaN/Infinity/negative/zero shapes), and a source-level companion pinning that the handlers drop the buffer and delegate rather than reading the duration raw. Two in `tests/test_api.py`: an upload with `duration=0` into a project that already holds a 187.5 s song, re-read through a **fresh `ProjectStore`** over the same data root to prove the probed value reached the manifest and replaced the old one — the previous route test only inspected the response body, which is the in-memory object the handler had just built — and an ffprobe-unavailable case that patches `subprocess.run` to raise `FileNotFoundError` for `ffprobe` only, asserting the import still succeeds and stores 0 rather than a fabricated number.
- Left the ffprobe helper's silent-`0.0`-on-failure contract untouched; callers depend on it, and the new test exercises that path rather than replacing it.
- Checked the "Ask First" condition and it did not fire. The one saved project on disk with an imported song stores 154.5984375 s against `ffprobe`'s 154.644898 s for the same MP3 — a 46 ms decoder-granularity difference from a correct browser decode of *its own* file, not an inherited length. No migration decision is needed and nothing on disk was repaired.
- `uv run pytest` 108 passing, `uv run ruff check .` clean, `node --check` clean on `app.js` and `api.js`. That 108 is a shared-tree figure, not this change's arithmetic — the concurrent LTX divisor work was landing in the same tree; `docs/ROADMAP.md` remains the authority for the current count. Scope of the claim: these are offline tests. **The undecodable-file path has not been driven in a real browser**, so only the executable contract is claimed for it; the existing browser QA still covers the decodable path only.

## 2026-08-17 — First live songs from both SongPlanner adapters

- Added `tests/smoke_songplanner_app.py`: a manual, stdlib-only script (not pytest-collected, like `smoke_h3_app.py`) that drives the running application over HTTP, one short song through each SongPlanner adapter. It requires an explicit `--confirm-gpu` flag because it bypasses the browser's cost confirmation, re-runs the `tests/preflight_songplanner.py` audit rather than duplicating it, and aborts before submitting if `/api/health` reports ComfyUI offline, if `ffprobe` is missing, or if the audit finds a problem.
- Generated the first real songs this application has ever produced. Invented lyrics: prompt `d7f86956-243c-45bf-ad48-ddf327588d3b`, seed `20260816`, complete in 216.7 s, `music-video-producer/project_8d4fa036ab9d/songs/Invented Smoke_00001.flac`, `ffprobe` flac 44100 Hz 2 ch **29.989 s** 2,496,336 bytes. Known lyrics: prompt `d09f929e-0794-4502-889a-ee1593e76bba`, seed `20260817`, complete in 185.0 s, `music-video-producer/project_cb0ff891977a/songs/Known Lyrics Smoke_00001.flac`, `ffprobe` flac 44100 Hz 2 ch **29.989 s** 2,617,365 bytes. ComfyUI 0.33.1 at `127.0.0.1:8188`, app on port 8766 with an isolated data root; pre-flight passed `OK 20 nodes across 2 variants (10 classes)`.
- Used one project per adapter deliberately: every `kind=music` job targets `"song"`, so a shared project's second run would clobber the first. Nothing in persisted state distinguishes a SongPlanner song from a direct Music 3 song, so the script's own JSON output is the only record of which adapter produced which prompt ID.
- Confirmed requested length is not produced length. Both requests asked for 30 s and `Song.duration` stores `30.0`, while the audio measures 29.989 s (independently re-probed at 29.988571 s) — node 49 takes its `seconds` from the encoder's model-resolved output while the route stores the requested value. Both numbers are recorded rather than one being treated as a failure.
- Verified playback to exactly the level the evidence supports: after job refresh the app points the player at the ComfyUI `/view` URL, and for both songs that URL returned HTTP 200 with a byte count matching the file on disk, and those returned bytes were themselves `ffprobe`-decodable FLAC. A browser was not driven, so browser playback is not claimed.
- Did not meet the epic's "≤16 s" target, and it is not achievable: `M3SongPlanner.duration_seconds` has a schema floor of `min: 30.0`. A first attempt at 16 s was rejected by ComfyUI at prompt validation with `value_smaller_than_min`, before any GPU time was spent. The floor is recorded in the story's Spec Change Log and the epic's wording stands pending human ratification.
- That rejection exposed a real gap rather than a one-off: the pre-flight audit validates classes, input names, and combo values, but never numeric `min`/`max` ranges. Closed separately under `_bmad-output/implementation-artifacts/spec-songplanner-duration-bounds.md`, which corrected the route's request bounds to 30–300 s and the planner seed ceiling to 4294967295.
- Follow-on hardening beyond that spec's scope, landed the same day: `MusicRequest.seed`, `FluxRequest.seed`, and `MultiviewRequest.seed` gained `le=0xFFFFFFFFFFFFFFFF`, matching the 64-bit `MiniMaxMusic3TextEncode.seed` / `KSampler.seed` schema maxima, so an out-of-range seed on any route is now a local 422 rather than an opaque 502. SongPlanner keeps its narrower 32-bit ceiling because `M3SongPlanner.seed` is the binding constraint there. The duration input gained `step="any"` so the browser stops rejecting the fractional durations the route accepts. Rationale and the reason the JS reports seed bounds as strings — 18446744073709551615 is not exactly representable as a JS double and rounds *up*, advertising a ceiling the route would refuse — are recorded in `docs/WORKFLOW-MAP.md`.
- This story adds no collected tests; the script is manual by design. The suite stood at 92 passing when this story was written, `ruff check .` clean. That figure is this story's snapshot, not the current gate: once all three 2026-08-17 work streams landed the settled count is **103 passing**, recorded in `docs/ROADMAP.md`, which is the authority for the current number.

## 2026-08-16 — Defect sweep after the first verified render

- Fixed the data-loss defect in Director chat: `document_rejection()` refuses a Treatment or Style Bible that parses as JSON or collapses below 40% of the document it would replace, and the reply reports what was kept and why.
- Diagnosed the root cause as self-reinforcing. The whole project is sent as context, so a Style Bible once stored as JSON caused the model to keep returning JSON. Verified both directions live: prose in context gave clean prose 3/3, JSON in context gave JSON 2/2 and the guard rejected both.
- Fixed executing renders reporting `queued` by consulting `/queue` when history is still empty. Verified live against a running prompt.
- Fixed `apply_shots` applying nothing silently when the model returns an empty shot list.
- Fixed the payload sending requested rather than grid-aligned frames; `align_h3_frames()` output now crosses the workflow boundary.
- Fixed mixed path separators in stored output paths.
- Added flagging for planned shots outside MiniMax H3's 4–15 second window, without rewriting the proposal.
- Restored `ruff check .` as a real gate by excluding vendored agent tooling, which had introduced 39 errors outside application code.
- Established that `sageattention` is not installed in ComfyUI's embedded Python, so the `PathchSageAttentionKJ` pin to `disabled` is correct rather than a leftover.
- Recorded the Production Wizard reconciliation in `docs/ARCHITECTURE.md`: the Operate / Command-Inspect decision rejects decorative surfaces, not sequencing.
- Test suite 52 → 62.

## 2026-08-16 — First verified end-to-end render, and version control

- Rendered the first real H3 shot ever submitted from this application: 3.75 s, 90 frames, 640×384, 4 steps, seed 12345, against live ComfyUI 0.33.1.
- Confirmed the output with `ffprobe`: h264 640×384, 90 frames, 3.750 s, synchronized AAC audio at 32 kHz.
- Confirmed the job reconciled to `complete`, wrote `latest_output`, and left `approved_output` empty as designed.
- Pre-flighted all thirteen node classes and five model files against live `/object_info` before spending GPU time.
- Found that job refresh reports `queued` for the whole of an executing render, because it reads history rather than the queue.
- Found that stored output paths mix separators; ComfyUI's `/view` tolerates it, so previews still resolve.
- Noted that the H3 payload sends `requested_frames` rather than `aligned_frames`; the verified window was deliberately on-grid, so off-grid behavior is still unknown.
- Initialized version control, which the repository had never had despite a `.gitignore` and documentation referring to Git.
- Added `AGENTS.md` agent instructions.

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
