---
title: Music Video Producer
status: draft
created: 2026-08-16
updated: 2026-08-16
---

# PRD: Music Video Producer

## 0. Document Purpose

This PRD is for the Director (sole builder and operator) and for the downstream BMad workflows — architecture, UX, and epic/story creation — that consume it. It is a brownfield PRD: a working application already exists, and on 2026-08-16 its text-only render path was verified end to end against live ComfyUI. Requirements here describe the gap between that application and a finished music video, not a system built from nothing.

It builds on, and does not duplicate, the product brief at `_bmad-output/planning-artifacts/briefs/brief-MusicVideoProducer-2026-08-16/` and the technical documentation in `docs/` — `ARCHITECTURE.md`, `WORKFLOW-MAP.md`, `DATA-MODEL.md`, `LLM-DIRECTOR.md`, `OPERATIONS.md`, `ROADMAP.md`. Where this PRD contradicts an existing document, the contradiction is called out explicitly rather than left for a reader to discover.

Vocabulary is anchored in §3 Glossary and used verbatim throughout. Features are grouped in §4 with globally numbered FRs nested beneath them. Inferences are tagged `[ASSUMPTION]` inline and indexed in §9.

## 1. Vision

Music Video Producer turns a song into a finished music video on hardware the user already owns, driving a ComfyUI installation the user already controls. No cloud service, no subscription, no quota, no content policy, no third party in the loop.

The hard part of AI music video is not generating a clip — that is solved. The hard part is everything structural around the clip: keeping one character recognizably the same person across dozens of shots, holding those shots against a real song's timing, remembering which seed produced the take that worked, and getting from a song to an assembled video without hand-operating a node graph forty times. ComfyUI renders anything and remembers nothing about a production. This application is the memory and the structure.

The intended feel is two-speed. A **Production Wizard** carries a new project through its structural decisions — song, treatment, cast, shot plan, first render — without the user reading documentation. From there the full editor takes over, where any individual shot can be tuned and re-rendered by eye. Fast to start, deep to finish.

## 2. Target User

### 2.1 Jobs To Be Done

- **Functional:** turn my song into a watchable music video without operating a node graph once per shot.
- **Functional:** keep my character looking like the same person from the first shot to the last.
- **Functional:** fix the three shots that came out wrong without redoing the other thirty.
- **Functional:** come back in six months and rebuild or revise any shot from what the project recorded.
- **Contextual:** run all of it on my own machine, with no account, no bill, and no rules about what I may generate.
- **Emotional:** finish something. Have a video that exists and that I would show people.

### 2.2 Non-Users (v1)

- Clients and commercial production work — no delivery, review-link, invoicing, or licensing concerns.
- Teams — no collaboration, permissions, or concurrent editing.
- Anyone without local GPU capacity sufficient for the model stack.
- Anyone wanting a general-purpose video editor. This edits its own pipeline's output, not arbitrary footage.

`[ASSUMPTION: "shareable later" means distributing the application for others to run on their own hardware, not hosting it for them.]`

### 2.3 Key User Journeys

- **UJ-1. The Director gets a first shot on screen without reading anything.**
  - **Persona + context:** the Director has a finished song, a working ComfyUI, and no memory of how this application is laid out.
  - **Entry state:** application open at a fresh project, ComfyUI confirmed reachable.
  - **Path:** the Production Wizard opens at Song and asks for a master or a generated one → Treatment, where a few sentences of direction become an editable treatment and shot plan → Cast, where a character image is generated and promoted to a Reference Sheet → Shots, where the plan is laid against the waveform → Render, where the queue is submitted after a cost confirmation.
  - **Climax:** the first Shot completes and plays back in place, in the timeline, against the song.
  - **Resolution:** the Wizard steps aside; the project is a normal project in the full editor.
  - **Edge case:** ComfyUI is not running. The Wizard says so plainly at the step that needs it and does not pretend to progress.

- **UJ-2. The Director renders a whole video in one pass and fixes what failed.**
  - **Persona + context:** references are set up, the shot plan is settled, and the Director wants the video to exist.
  - **Entry state:** a project with a song, an approved cast, and a full Shot list.
  - **Path:** submits the whole Shot list as one batch → watches Shots land on the timeline one by one as they finish → plays the video from the top through everything rendered so far while later Shots are still generating → flags two Shots that came out wrong, without interrupting the batch → when the batch drains, adjusts those two and re-renders only them.
  - **Climax:** the timeline is complete, and the Director assembles the approved Shots into one video synchronized to the song.
  - **Resolution:** a finished file on disk, with every Shot traceable to its prompt, seed, and workflow.
  - **Edge case:** a Shot fails with a ComfyUI execution error. The exact error is shown on that Shot; the rest of the batch is unaffected.

- **UJ-3. The Director returns to a project six months later.**
  - **Persona + context:** models have changed; the Director wants one Shot redone.
  - **Entry state:** the project is opened from disk; some ComfyUI outputs may have been cleaned up.
  - **Path:** opens the Shot, reads the recorded prompt, seed, and workflow → re-renders it → compares against the surrounding Shots.
  - **Climax:** the Shot is rebuilt without reconstructing the reasoning behind it.
  - **Resolution:** the project is coherent again, and missing media is reported honestly rather than silently.

## 3. Glossary

- **Project** — one production. Owns exactly one Song, a set of Assets, an ordered set of Shots, its creative documents, and its Render Jobs. Persisted as a recoverable manifest.
- **Song** — the master audio for a Project, either imported or generated. There is exactly one per Project, and it is the timing spine for every Shot.
- **Treatment** — the editable prose description of the video's concept.
- **Style Bible** — the editable continuity record: colour, lighting, lenses, wardrobe, locations.
- **Asset** — a piece of media belonging to a Project: character, setting, prop, style frame, image, audio, or video. Uploaded or generated.
- **Reference Sheet** — a multiview Asset derived from an approved character Asset, used to hold identity across Shots. Linked to its source character.
- **Shot** — a timed window against the Song with its own prompt, references, and seed. The unit of rendering and of regeneration.
- **Shot Plan** — the ordered set of Shots covering a Project's Song.
- **Render Job** — one submission to ComfyUI, carrying its prompt ID, seed, target, status, outputs, and any error.
- **Latest Output** — the most recent completed render for a Shot. Never implies the Director accepted it.
- **Approved Output** — the render the Director explicitly accepted for a Shot. Only Approved Outputs are assembled.
- **Regeneration** — re-rendering a single Shot in place, leaving every other Shot untouched.
- **Production Wizard** — the guided path that sequences the existing workspaces for a new Project.
- **Assembly** — joining Approved Outputs in Shot order into one video synchronized to the Song.
- **Finishing** — optional post-render quality stages applied to rendered Shots: upscaling, interpolation, enhancement.
- **Batch** — one submission of many Shots to ComfyUI as a single act of the Director's intent. Individual Shots within a Batch complete independently.

## 4. Features

### 4.1 Production Wizard

**Description:** A guided path that carries a new Project from empty to first completed Shot without requiring documentation. The Wizard does not reimplement the workspaces — it sequences them. Each step presents the real Song, Treatment, Assets, Timeline, or Queue workspace, scoped to that step's decision. Realizes UJ-1.

Its current step is derived from Project state, not stored: a Project with no Song is at Song, a Project with a Song but no Shot Plan is at Treatment, and so on. This makes it resumable with no extra machinery and impossible to desynchronize from reality.

This reconciles with the standing decision in `docs/ARCHITECTURE.md` that the interface is an "Operate / Command-Inspect editor, not a dashboard." That decision rejects decorative surfaces; it does not reject sequencing. `[ASSUMPTION: ARCHITECTURE.md is to be updated to record this reconciliation rather than left to contradict the implementation.]`

**Functional Requirements:**

#### FR-1: Derived wizard progress

The Director can see which production step a Project is at, computed from Project state. Realizes UJ-1.

**Consequences (testable):**
- Step is a pure function of the Project manifest; no wizard progress field is persisted.
- A Project with no Song resolves to the Song step; with a Song and no Shots, to the Treatment step; with Shots and no completed Render Job, to the Render step.
- Deleting the Song from a Project returns it to the Song step.

#### FR-2: Wizard step presents the real workspace

The Director works in the actual workspace at each step, not a simplified copy. Realizes UJ-1.

**Consequences (testable):**
- Each step renders the same component the full editor uses for that workspace.
- An action taken inside a Wizard step produces the same Project state as the same action in the full editor.

#### FR-3: Wizard is escapable and non-recurring

The Director can leave the Production Wizard at any step and will not be returned to it for a Project that has progressed past it.

**Consequences (testable):**
- A skip control is present at every step and moves to the full editor without altering Project state.
- A Project with at least one completed Render Job does not open in the Wizard.

**Notes:** `[NOTE FOR PM]` The Wizard's step boundaries assume Cast (character generation and Reference Sheet promotion) is a distinct step from Assets generally. Confirm during UX.

### 4.2 Batch Rendering and Targeted Regeneration

**Description:** The Director's working model is to render the whole video in one pass, then fix what failed. Rendering several takes of the same Shot for comparison is explicitly rejected: it costs a great deal of GPU time for little value when the Director can simply look at a Shot and re-roll it. Realizes UJ-2.

This reverses `docs/ROADMAP.md`'s "Multiple takes and approval" item. Take history and comparison UI are out; in their place, Regeneration must be cheap, targeted, and safe.

**Functional Requirements:**

#### FR-4: Submit a Shot Plan as one batch

The Director can submit every ready Shot in a Project as a single queued batch after one cost confirmation. Realizes UJ-2.

**Consequences (testable):**
- One confirmation covers the batch; the Director is not prompted per Shot.
- The confirmation states the number of Shots being submitted.
- Each Shot produces its own Render Job with its own prompt ID.
- A Shot that fails validation is reported and skipped without blocking the rest of the batch.

#### FR-5: Regenerate a single Shot in place

The Director can re-render one Shot without affecting any other Shot. Realizes UJ-2, UJ-3.

**Consequences (testable):**
- Regeneration replaces that Shot's Latest Output and leaves every other Shot's outputs and state unchanged.
- Editing a Shot's prompt, references, or seed and regenerating does not alter Shot timing or neighbouring Shots.
- A Shot's Approved Output is not overwritten by Regeneration; approving the new render is a separate act.

#### FR-6: Report render progress truthfully

The Director can distinguish a Shot that is executing from one that is merely waiting.

**Consequences (testable):**
- A Render Job whose prompt is currently executing on ComfyUI reports `running`, not `queued`.
- State is reconciled against ComfyUI's queue as well as its history.
- A Shot that failed shows the exact ComfyUI execution error.

**Notes:** FR-6 fixes an observed defect: during the verified 2026-08-16 render, a twelve-minute execution reported `queued` for its entire duration because reconciliation read only ComfyUI's history.

#### FR-7: Populate the timeline live as the batch renders

Each Shot takes its place on the timeline the moment it completes, while the rest of the batch is still rendering. Realizes UJ-2.

**Consequences (testable):**
- A completed Shot's output appears on the timeline without the Director reloading or re-navigating.
- The Director can play the video from the start through every Shot completed so far while later Shots are still executing.
- The boundary between completed and not-yet-rendered Shots is visible on the timeline.
- Playback of completed Shots is unaffected when a later Shot completes.

#### FR-8: Flag a Shot for regeneration during an active batch

The Director can mark a Shot as needing Regeneration while the batch that produced it is still running. Realizes UJ-2.

**Consequences (testable):**
- Flagging a Shot during an active batch does not cancel, pause, or disturb the batch.
- Flagged Shots are collected and can be resubmitted as a follow-up batch once the current one drains.
- Editing a flagged Shot's prompt, references, or seed while the batch runs is permitted and persists.

**Notes:** This turns batch rendering from fire-and-wait into a reviewable stream — the Director reviews the first half of a video while the second half is still being made. It is the highest-value interaction in the product and should be treated as such in architecture.

#### FR-9: Batch a Shot queue without redundant model reloading

The Director can render a Shot Plan without paying the model-loading cost once per Shot.

**Consequences (testable):**
- Shots in a Batch are submitted so that ComfyUI's existing model residency is preserved: consecutive Shots of the same kind are not interleaved with prompts of another kind.
- A Batch of N same-kind Shots does not unload and reload the model stack between them.
- Nothing in the Batch path issues a ComfyUI free, unload, or interrupt call.

**Notes:** Measured 2026-08-16, and the measurement removed the requirement's teeth. Two identical 107-frame renders back to back took 438 s and 288 s — ComfyUI already keeps the stack resident, and the second run saved 150 s. So no batching or warm-pool machinery is needed; the requirement is only to avoid defeating what already works. `[NOTE FOR PM]` The corollary matters more than the FR: interleaving Flux or Music 3 renders into an H3 Batch would evict the stack and cost roughly 150 s per eviction.

### 4.3 GPU Resource Coordination

**Description:** Two independent systems compete for the same VRAM. ComfyUI holds the render stack; LM Studio holds the Director and vision models. Neither knows about the other, and ComfyUI cannot free what LM Studio is holding. During the verified 2026-08-16 render the H3 stack left only 1.1 GB free of 32 GB — a margin thin enough that a resident language model is a real failure risk, not a theoretical one.

**Functional Requirements:**

#### FR-10: Free language-model VRAM before rendering

The Director can have the language and vision models unloaded from VRAM before a render batch begins.

**Consequences (testable):**
- Before submitting a render batch, the application requests that the configured LM Studio instance unload its loaded models.
- The application never assumes the unload succeeded; it reports the observed free VRAM before proceeding.
- Unloading is skipped without error when no language-model endpoint is configured.
- The Director is not blocked from rendering if the unload cannot be performed; the risk is reported instead.

#### FR-11: Name the competing VRAM consumer before rendering

The Director is told, before committing to an expensive render, that a language model is still resident in VRAM.

**Consequences (testable):**
- The render confirmation queries the configured language-model endpoint for loaded models.
- When any model is loaded, the confirmation names it and states that it is holding VRAM outside ComfyUI's control.
- When no model is loaded, no warning is shown.
- The warning is informational; the Director may proceed.
- The confirmation also shows free VRAM as reported by ComfyUI, as context rather than as a gate.

**Notes:** Detection is preferred over a VRAM threshold because the competing consumer is identifiable and actionable, whereas a threshold number would need re-tuning for every model and every machine. `[NOTE FOR PM]` This does not detect other VRAM consumers such as a browser or a second application; those remain the Director's responsibility. `[ASSUMPTION: LM Studio's local API exposes a model-unload operation; if it does not, FR-10 reduces to this warning plus instructions.]`

### 4.4 Song Creation

**Description:** A Project's Song is either imported or generated. Generation covers two distinct situations, which the Director has split into separate ComfyUI workflows: writing a song from a caption where the lyrics are invented, and producing a song where the lyrics are already known — a cover, or words the Director has already written.

**Functional Requirements:**

#### FR-12: Import a master Song

The Director can import a WAV, FLAC, or MP3 as the Project's Song, with its duration established reliably.

**Consequences (testable):**
- Duration is available after an application restart, including when the browser could not determine it at upload time.
- The imported Song plays through the transport and is seekable from the waveform.

#### FR-13: Generate a Song with invented lyrics

The Director can generate a Song from a caption and style direction, letting the model write the lyrics.

**Consequences (testable):**
- Caption, maximum duration, and seed are Director-supplied.
- The resulting Song is recorded with its prompt ID and seed.

#### FR-14: Generate a Song from known lyrics

The Director can generate a Song from lyrics they supply, for a cover or an already-written song.

**Consequences (testable):**
- Supplied lyrics are used as given and are not rewritten by the model.
- This path is selectable independently of FR-13 and maps to the known-lyrics workflow variant.

**Notes:** `[ASSUMPTION: the two SongPlanner variants differ only in whether lyrics are supplied or invented; if they differ structurally, each needs its own adapter.]` These map to the two saved variants `SongPlanner + MiniMax Music 3 - Quality BF16.json` and `SongPlanner + MiniMax Music 3 - Quality BF16-Known_Lyrics.json`. The application's current direct Music 3 payload covers neither SongPlanner variant.

### 4.5 Creative Direction

**Description:** A locally-hosted language model turns conversational direction into an editable Treatment, Style Bible, and Shot Plan. It creates records; it never spends GPU time on renders and never decides that a Shot is good.

The current implementation has a confirmed data-loss defect. With full Project context supplied, the model returns the Style Bible as serialized JSON rather than prose — reproduced on three consecutive attempts — and the application assigns it over the existing document without validation. In the same failure the returned Shot list was empty while the model's own prose described a four-beat sequence. Creative documents are destroyed with no undo.

**Functional Requirements:**

#### FR-15: Validate language-model output before persisting

Output from the language model is checked for structural sanity before it replaces anything.

**Consequences (testable):**
- A Treatment or Style Bible whose content parses as JSON is rejected as degraded, and nothing is overwritten.
- A replacement shorter than 40% of the length of the non-empty document it would replace is rejected as degraded.
- Either rejection is reported to the Director with the raw model output available for inspection.
- A response whose prose claims Shots while the structured Shot list is empty is reported as a mismatch rather than applied.
- Both checks are skipped when the target document is empty, so a first draft is never rejected for being short.

#### FR-16: Never destroy a creative document silently

An existing non-empty Treatment or Style Bible is not replaced without the Director's awareness.

**Consequences (testable):**
- Replacing a non-empty creative document is a reviewable act, not a side effect of sending a message.
- The prior version of a replaced document is recoverable within the session.
- Locked fields are never modified.

#### FR-17: Keep planned Shots within a renderable range

Shots proposed by the language model fall within the range the renderer handles reliably.

**Consequences (testable):**
- A proposed Shot shorter than 4 seconds or longer than 15 seconds is flagged before it reaches the timeline.
- The Director is told which proposed Shots were flagged and why.
- Flagging does not silently rewrite the proposal; the Director decides whether to split, trim, or keep it.

**Notes:** Observed during diagnosis — the model proposed a single 20-second Shot for a 20-second request, well outside H3's reliable 4–15 second window, and validation permitted it because the planning limit is 30 seconds.

### 4.6 Continuity and Cast

**Description:** Identity is held as a data relationship rather than as prompting skill. A character Asset is approved, promoted to a Reference Sheet, and that sheet is what Shots refer to. Realizes UJ-2.

**Functional Requirements:**

#### FR-18: Promote a character to a Reference Sheet

The Director can turn an approved character Asset into a multiview Reference Sheet linked to its source.

**Consequences (testable):**
- The resulting Asset records its parent character Asset.
- The source Asset remains unmodified.

#### FR-19: Attach references to a Shot

The Director can attach character, setting, prop, video, and audio Assets to a Shot, in an order that determines how they are labelled to the renderer.

**Consequences (testable):**
- Attachment order determines reference numbering deterministically; the same attachment order always produces the same numbering.
- Limits are enforced with a clear message rather than a failed submission.
- Only media contained within the Project or within ComfyUI's own output is accepted as a reference path.

#### FR-20: Render reference-driven Shots

The Director can render a Shot that uses attached references rather than text alone. Realizes UJ-2.

**Consequences (testable):**
- A Shot with attached Assets renders through the reference path; a Shot with none renders through the text-only path.
- The Project's Song can be attached as an audio reference for synchronization.

**Notes:** The reference path is built and unit-tested but has never produced a live render from this application. Only the text-only path has live evidence.

### 4.7 Assembly and Finishing

**Description:** Approved Outputs are joined in Shot order into a single video synchronized to the Song. Finishing stages improve rendered Shots before assembly. Realizes UJ-2.

**Functional Requirements:**

#### FR-21: Approve a Shot's output

The Director can explicitly accept a Shot's Latest Output as its Approved Output.

**Consequences (testable):**
- Render completion never sets an Approved Output.
- Approval is reversible.
- A Shot with no Approved Output is reported as blocking Assembly.

#### FR-22: Assemble a video

The Director can join every Approved Output in Shot order into one video synchronized to the Song. Realizes UJ-2.

**Consequences (testable):**
- Assembly refuses to run, naming the specific Shots, when any Shot lacks an Approved Output.
- Each Approved Output is trimmed to its Shot's window before joining, because grid alignment makes a rendered clip longer than the Shot that requested it.
- The assembled file's duration matches the Song within one frame.
- Assembly does not modify any Shot's Approved Output.
- The assembled file is verified after writing, and a failed verification is reported rather than presented as success.

**Notes:** The trim is not optional. Measured 2026-08-16: a 4.0 second Shot renders as 107 frames at 4.458 seconds, because MiniMax H3 requires frame counts on a 17k+5 grid. Joining untrimmed clips would accumulate roughly 11% drift, so a three-minute video would finish about twenty seconds out of sync with its Song.

#### FR-23: Apply Finishing to rendered Shots

The Director can apply upscaling, enhancement, and interpolation to a rendered Shot before Assembly.

**Consequences (testable):**
- Finishing accepts an already-rendered Approved Output as its input and does not regenerate the Shot.
- Dimensions are normalized to the downstream stage's constraints before that stage runs.
- A Finishing failure leaves the input Approved Output intact.

**Sequencing and drop condition.** Finishing is in MVP scope at the Director's explicit instruction, reversing the product brief's proposal to drop it if unproven. Because its prerequisite does not yet exist, it is sequenced **last** — after Assembly (FR-22) produces a complete video from unfinished Shots. This ordering protects SM-1, which is binary and gates everything else.

The drop condition is stated so the reversal does not silently become a schedule risk: **if the standalone approved-take adapter is not working by the time every other MVP item is complete, the first complete video ships without Finishing and Finishing moves to v2.** Dropping it costs quality on one video; blocking on it costs the video.

**Notes:** The known blocker is that existing combined exports regenerate the Shot from creator-specific media instead of accepting an approved input; a standalone adapter is the prerequisite. The dimension-boundary failure (SeedVR2 emitting 1250×720 into an LTX VAE requiring a multiple of four) is already diagnosed and patched in an audited reference, so it is not the risk — the adapter is. `[NOTE FOR PM]` Consider proving the adapter early as a spike, so viability is known while there is still time to react.

### 4.8 Provenance and Recovery

**Description:** A Project is recoverable from its manifest, and any Shot can be rebuilt from what was recorded. Realizes UJ-3.

**Functional Requirements:**

#### FR-24: Record complete render provenance

Every Render Job records the information needed to rebuild it. Realizes UJ-3.

**Consequences (testable):**
- Each Render Job stores workflow kind, prompt ID, seed, target, output paths, status, exact error, and timestamps.
- Provenance survives an application restart.

#### FR-25: Report missing media honestly

The Director is told when referenced media no longer exists rather than shown a silent failure. Realizes UJ-3.

**Consequences (testable):**
- A Shot whose recorded output is absent from disk is displayed as missing, not as blank.
- A malformed Project manifest is skipped during listing without crashing the application.

## 4A. Cross-Cutting NFRs

System-wide qualities not owned by any single feature. Deliberately short: only the two the Director judged load-bearing are stated as requirements. Render economics and storage growth are real constraints but remain open questions (§8 Q1, Q2, Q5) rather than requirements, because no measurement yet supports a number.

### NFR-1: The application stays usable while a Batch renders

Rendering must never make the application unresponsive. This is a prerequisite for FR-7 and FR-8 — live timeline population and in-flight flagging are worthless if the interface freezes during the batch that produces them.

**Consequences (testable):**
- Navigation between workspaces, playback of completed Shots, and editing of Shot prompts and references all remain available while a Batch is executing.
- No render operation blocks the event loop; submission and reconciliation are asynchronous.
- A ComfyUI request that hangs does not hang the application; all outbound calls are bounded by a timeout.
- Reconciling a Batch of 40 Shots does not degrade interface responsiveness perceptibly.

### NFR-2: Project state survives crashes and concurrent writes

A Project manifest is never left partially written, and concurrent edits do not corrupt it.

**Consequences (testable):**
- Manifest writes are atomic — a crash mid-write leaves either the previous manifest or the new one, never a partial file.
- Concurrent Shot saves are serialized; two in-flight saves cannot interleave into a corrupt Shot list.
- A stale full-Project replacement is rejected rather than silently overwriting newer state.
- A malformed manifest is skipped during Project listing rather than crashing the application.

**Notes:** The existing implementation already satisfies most of NFR-2 — `store.py` writes via temp file plus atomic replace, and shot saves are serialized. It is stated here because it was never written down as a requirement, and unstated invariants get refactored away.

## 5. Non-Goals (Explicit)

- **Not a cloud product.** No hosted component, no account system, no telemetry, no remote access.
- **Not multi-user.** No collaboration, permissions, or concurrent editing.
- **Not a general video editor.** It edits this pipeline's output, not imported footage.
- **Not a ComfyUI manager.** The application never starts, stops, restarts, interrupts, or kills ComfyUI, and never bundles it.
- **Not a take-comparison tool.** Rendering multiple takes of a Shot for side-by-side selection is explicitly rejected — Regeneration replaces it.
- **Not a model evaluator.** Render quality is the model's business. This product is measured on structure, continuity, recoverability, and how it feels to operate across a whole song.
- **Not a live render preview.** Live timeline population (FR-7) means completed Shots appear as they finish. It does not mean previewing a Shot mid-generation, scrubbing partial output, or streaming frames from an executing prompt.

## 6. MVP Scope

### 6.1 In Scope

- The Production Wizard as a guided path over the existing workspaces.
- Batch submission of a Shot Plan, and targeted Regeneration of individual Shots.
- Live timeline population during an active batch, with in-flight flagging for Regeneration.
- Truthful render state, including distinguishing running from queued.
- GPU resource coordination — freeing language-model VRAM before rendering, and reporting headroom.
- Song generation in both variants: invented lyrics and known lyrics.
- Repair of the Director: validated output, and no silent destruction of creative documents.
- Reference-driven Shot rendering, proven live.
- Explicit approval, and Assembly of Approved Outputs into one song-synchronized video.
- The Finishing chain, accepting approved renders as input.
- Complete provenance and honest reporting of missing media.

### 6.2 Out of Scope for MVP

- Multi-take rendering and comparison — rejected outright, not deferred.
- Undo/redo command history, ripple editing, snapping. `[NOTE FOR PM]` Emotionally load-bearing for editing feel; revisit if timeline permits.
- BPM and section analysis, automatic verse/chorus detection, lyrics alignment lanes.
- Thumbnail filmstrips, markers, loop playback, multi-select batch operations.
- Draft versus master export presets.
- Any sharing, packaging, or distribution of the application to other users — a stated later goal, not v1.

## 7. Success Metrics

**Primary**

- **SM-1**: One complete music video — a real song rendered, approved, and assembled into a single watchable file in this application. Binary, and nothing else counts until it is true. Validates FR-4, FR-21, FR-22.
- **SM-2**: Character consistency — a character is recognizably the same person across the Shots of a finished video, judged by eye by the Director. `[ASSUMPTION: judged by eye; no identity-scoring metric is defined.]` Validates FR-18, FR-19, FR-20.

**Secondary**

- **SM-3**: Time to first Shot — a new Project reaches its first completed Shot without the Director leaving the Production Wizard. Validates FR-1, FR-2.
- **SM-4**: Regeneration is cheap — fixing a bad Shot costs one Shot's render time and requires no rework of surrounding Shots. Validates FR-5.
- **SM-5**: Rebuildability — any Shot in a finished Project can be re-rendered months later from recorded provenance alone. Validates FR-24.
- **SM-6**: Faster than by hand — producing a video in the application beats driving ComfyUI manually for the same song. `[ASSUMPTION: measured once as wall-clock from song to assembled draft, not as a repeatable benchmark.]` Validates FR-4, FR-9.
- **SM-7**: Review overlaps rendering — the Director can watch and judge completed Shots while later Shots are still being generated, and flag work without interrupting the batch. Validates FR-7, FR-8.
- **SM-8**: No creative document is ever lost to a Director call. Zero occurrences of an existing Treatment or Style Bible being replaced by degraded model output. Validates FR-15, FR-16.

**Counter-metrics (do not optimize)**

- **SM-C1**: Render quality must not be pursued at the cost of finishing a video. Counterbalances SM-2. Raising resolution, steps, or Finishing depth until a song becomes impractical to render is a failure, not an improvement.
- **SM-C2**: Wizard coverage must not grow. Counterbalances SM-3. Adding steps until the Wizard becomes the primary interface would defeat the editor the product is built around.
- **SM-C3**: Automation must not absorb editorial judgment. Counterbalances SM-4. The system must never mark a Shot approved on the Director's behalf, however confident a heuristic becomes.

## 8. Open Questions

1. ~~Does ComfyUI keep the model stack resident between consecutive prompts of the same kind?~~ **Answered 2026-08-16: yes.** Two identical 107-frame H3 renders submitted back to back took 438 s cold and 288 s warm — the second saved 150 s, or 34%. FR-9 therefore reduces to "do not defeat this behaviour," and no batching machinery needs building. The remaining question is narrower: how long residency survives an idle gap, and whether interleaving a different workflow kind evicts the stack.
2. What is the resolution and step policy for a delivery-quality render, as opposed to the 640×384/4-step configuration used for verification?
3. Should the H3 payload send aligned frames rather than requested frames? `DirectorTimeline.aligned_frames` is computed but unused, and only an on-grid window has been verified.
4. How is a Shot marked as needing Regeneration — purely by the Director's eye, or assisted by the existing vision inspection?
5. What storage does a full production consume at delivery resolution, and does anything need pruning?
6. Does Assembly need audio from the Shots, from the Song, or a mix? Rendered Shots carry their own generated audio.
7. Which Finishing stages are mandatory versus optional per Shot?
8. ~~Can SageAttention be enabled?~~ **Answered 2026-08-16.** `sageattention` is not installed in ComfyUI's embedded Python, so the `disabled` pin is currently correct rather than a leftover from an error — enabling it as-is would fail. `triton` and torch 2.7.0+cu128 are present, so the package is installable, and the live node offers `sageattn_qk_int8_pv_fp16_triton` among its options. Remaining question is narrower: is a Blackwell-compatible `sageattention` build available for this torch version, and does it measurably speed up one identical render? That is a spike, not an open design question.
9. Does LM Studio's local API expose a model-unload operation? FR-10 reduces to a warning if it does not.
10. Why does the language model degrade under full Project context? The Style Bible corruption reproduces reliably with rich context and not with thin context, but the mechanism — context length, the serialized Project payload, or the model itself — is unestablished. Fixing the symptom (FR-15, FR-16) is separable from fixing the cause.
11. Should the Director model be reloaded automatically after a render batch completes, or left to the Director?

## 9. Assumptions Index

- §4.1 — `ARCHITECTURE.md` will be updated to record the Wizard reconciliation rather than left contradicting the implementation.
- §4.2 FR-9 — the batching target is deferred pending measurement of ComfyUI's model residency; if models stay resident, no batching machinery is built.
- §4.3 FR-10 — LM Studio's local API is assumed to expose a model-unload operation.
- §4.4 — the two SongPlanner variants are assumed to differ only in whether lyrics are supplied or invented; if they differ structurally, each needs its own adapter.
- §2 — "shareable later" means distributing the application for others to run themselves, not hosting it for them.
- §7 SM-6 — "faster than by hand" is measured once as wall-clock from song to assembled draft, not as a repeatable benchmark.
- §7 SM-2 — character consistency is judged by eye; no identity scoring metric is defined.
