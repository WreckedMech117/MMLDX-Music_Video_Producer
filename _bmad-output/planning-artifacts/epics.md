---
stepsCompleted: [1, 2, 3, 4]
inputDocuments:
  - _bmad-output/planning-artifacts/prds/prd-MusicVideoProducer-2026-08-16/prd.md
  - _bmad-output/planning-artifacts/prds/prd-MusicVideoProducer-2026-08-16/addendum.md
  - _bmad-output/planning-artifacts/briefs/brief-MusicVideoProducer-2026-08-16/brief.md
  - docs/ROADMAP.md
  - docs/ARCHITECTURE.md
  - docs/WORKFLOW-MAP.md
  - _bmad-output/planning-artifacts/ux-designs/ux-mvp-2026-08-16/DESIGN.md
  - _bmad-output/planning-artifacts/ux-designs/ux-mvp-2026-08-16/EXPERIENCE.md
---

# MusicVideoProducer - Epic Breakdown

## Overview

This document provides the complete epic and story breakdown for MusicVideoProducer, decomposing the PRD (26 FRs, 2 NFRs) into implementable stories. Brownfield: a working application exists, the text-only H3 render path is verified live, and seven defects were fixed on 2026-08-16. Stories reflect the *gap*, not a rebuild — FRs the codebase already satisfies are marked and get verification-level stories or none.

## Requirements Inventory

### Functional Requirements

FR-1: Wizard progress derived purely from Project state; no stored progress field
FR-2: Each Wizard step presents the real workspace, not a copy
FR-3: Wizard escapable at any step; never reappears past first completed render
FR-4: Submit every ready Shot as one Batch after a single cost confirmation
FR-5: Regenerate a single Shot in place without touching any other Shot
FR-6: Report running vs queued truthfully; show exact ComfyUI errors *(backend fixed 2026-08-16; UI surfacing remains)*
FR-7: Completed Shots take their place on the timeline live during an active Batch
FR-8: Flag Shots for Regeneration while the Batch that produced them still runs
FR-9: Preserve ComfyUI model residency — never interleave workflow kinds within a Batch *(residency measured: 438s cold / 288s warm)*
FR-10: Request LM Studio model unload before a render Batch; report observed free VRAM
FR-11: Name the loaded language model at render confirmation; informational, never a gate
FR-12: Import WAV/FLAC/MP3 master with reliable duration *(implemented; verified in browser QA)*
FR-13: Generate a Song with invented lyrics (SongPlanner variant)
FR-14: Generate a Song from known lyrics — covers (Known_Lyrics variant)
FR-15: Reject degraded LLM output — JSON-as-prose, <40% collapse *(implemented + live-verified 2026-08-16)*
FR-16: Never silently destroy a creative document; prior version recoverable in session
FR-17: Flag planned Shots outside H3's 4–15s window *(implemented 2026-08-16)*
FR-18: Promote an approved character to a Reference Sheet *(implemented; live render pending)*
FR-19: Attach ordered references to a Shot with deterministic numbering *(implemented)*
FR-20: Render reference-driven Shots through the H3 Ultra path *(adapter built; never rendered live)*
FR-21: Explicit approval of a Shot's Latest Output; reversible; never automatic
FR-22: Assemble Approved Outputs into one Song-synchronized video, trimming each clip to its Shot window (grid overrun measured: 4.0s → 4.458s)
FR-23: Apply Finishing to an Approved Output without regenerating the Shot — sequenced last, with drop condition
FR-24: Complete render provenance surviving restart *(implemented)*
FR-25: Report missing media honestly; skip malformed manifests *(partially implemented — manifest skip exists, missing-media UI does not)*
FR-26: Story expansion — every Shot fully prompted with deliberate variance before Batch submission; empty prompts block, near-duplicates flagged

### NonFunctional Requirements

NFR-1: Application stays usable while a Batch renders — no blocking calls, bounded timeouts, 40-Shot reconciliation without perceptible degradation
NFR-2: Project state survives crashes and concurrent writes *(implemented — atomic replace, serialized shot saves; regression-guarded, not re-built)*

### Additional Requirements

- ComfyUI is user-managed: never started, stopped, or interrupted by the application (Policy, AGENTS.md)
- Adapters are explicit API-format payloads; saved editor JSON is never submitted to `/prompt`
- `workflow_templates/reference_exports/` is immutable audited evidence
- No Agent OS coupling; GPL Director extension frontend is never copied
- SongPlanner variants map to `SongPlanner + MiniMax Music 3 - Quality BF16.json` and `...BF16-Known_Lyrics.json`
- Finishing prerequisite: a standalone LTX adapter accepting an Approved Output; the audited `divisible_by=32` boundary patch already exists (was 16 until 2026-08-17; the live run at prompt `a64a0460-64e6-4a14-b207-e644bf9bda5d` measured 2496×1408 = exactly 2 × 1248×704, where 16 would have produced 2496×1440)
- LM Studio unload assumed available via its local API (PRD assumption; degrade to warning if not)

### UX Design Requirements

A UX design contract exists at `_bmad-output/planning-artifacts/ux-designs/ux-mvp-2026-08-16/` (`DESIGN.md` + `EXPERIENCE.md`), settled with the Director on 2026-08-16. The four load-bearing decisions — the rail doubles as the wizard with a guidance banner, clip state via border + corner chips in the existing palette, on-clip hover actions with F/A keys for flag/approve, and a pre-flight modal for batch confirmation — are folded into the story ACs of Epics 2, 4, 5, and 6.

### FR Coverage Map

FR-12: Epic 1 — verified as-is, no new story
FR-13: Epic 1 — SongPlanner invented-lyrics adapter
FR-14: Epic 1 — Known_Lyrics adapter
FR-15: Epic 2 — done; backend regression ACs in Story 2.4
FR-16: Epic 2 — reviewable replacement + session recovery
FR-17: Epic 2 — done; backend regression ACs in Story 2.4
FR-26: Epic 2 — story expansion + submission gate
FR-18: Epic 3 — existing; exercised by live verification story
FR-19: Epic 3 — existing; exercised by live verification story
FR-20: Epic 3 — live H3 Ultra render verification
FR-4:  Epic 4 — batch submission
FR-5:  Epic 4 — targeted regeneration
FR-6:  Epic 4 — UI surfacing of running/error state
FR-7:  Epic 4 — live timeline population
FR-8:  Epic 4 — in-flight flagging
FR-9:  Epic 4 — same-kind batch ordering
FR-10: Epic 4 — LM Studio unload before Batch
FR-11: Epic 4 — VRAM consumer named at confirmation
NFR-1: Epic 4 — responsiveness under an active Batch
FR-21: Epic 5 — approval
FR-22: Epic 5 — trim + assemble + verify
FR-24: Epic 5 — provenance regression guard
FR-25: Epic 5 — missing-media reporting
NFR-2: Epic 5 — integrity regression guard
FR-1:  Epic 6 — derived step
FR-2:  Epic 6 — real-workspace composition
FR-3:  Epic 6 — escape and non-recurrence
FR-23: Epic 7 — standalone Finishing adapter + chain

> **Companion documents.** Two features are broken down separately, continuing this numbering so story IDs are unique across all three files:
>
> - **Shot Effects and Transitions** — [`epics-effects.md`](epics-effects.md), Epics 8–11, Stories 8.1–11.5
> - **Treatment Planning** — [`epics-treatment.md`](epics-treatment.md), Epics 12–17, Stories 12.1–17.2

## Epic List

### Epic 1: Any Song Becomes the Spine
The Director gets a master song into a project three ways — import, invented lyrics, or known lyrics (covers) — with reliable duration and provenance.
**FRs covered:** FR-12 (verified as-is), FR-13, FR-14

### Epic 2: A Render-Ready Shot Plan
Conversation becomes a complete, varied, fully prompted Shot Plan — with no way for the language model to destroy creative work and no way to spend GPU time on empty or copy-paste prompts.
**FRs covered:** FR-15 (done), FR-16, FR-17 (done), FR-26

### Epic 3: Continuity Proven, Not Promised
The reference path — character to Reference Sheet to reference-driven Shot — produces a real render on live ComfyUI, making character consistency a demonstrated capability instead of a unit-tested claim.
**FRs covered:** FR-18, FR-19, FR-20

### Epic 4: One-Pass Video, Watched Live
The Director submits the whole Shot Plan as one Batch (GPU coordination handled at confirmation), watches completed Shots land on the timeline while later ones render, flags failures without interrupting, and regenerates only what failed. The product's highest-value interaction.
**FRs covered:** FR-4, FR-5, FR-6, FR-7, FR-8, FR-9, FR-10, FR-11, NFR-1

### Epic 5: A Finished, Durable Video
Approved takes become one song-synchronized video file — trimmed to defeat grid drift — and the project that produced it remains honest and recoverable months later.
**FRs covered:** FR-21, FR-22, FR-24, FR-25, NFR-2

### Epic 6: The Production Wizard
A new project reaches its first rendered Shot through a guided path composed from the real workspaces, derived from project state, escapable, and never seen again once outgrown.
**FRs covered:** FR-1, FR-2, FR-3

### Epic 7: Finishing (Drop-Conditioned)
Approved renders pass through the upscale/interpolate/enhance chain without being regenerated. Sequenced last; drops to v2 if the standalone approved-take adapter is not proven when all else is done.
**FRs covered:** FR-23

## Epic 1: Any Song Becomes the Spine

The Director gets a master song into a project three ways — import, invented lyrics, or known lyrics — with reliable duration and provenance. Import (FR-12) is implemented and browser-QA-verified; this epic adds the two SongPlanner generation variants the Director split on disk.

### Story 1.1: Generate a Song with Invented Lyrics

As the Director,
I want to generate a complete song from a caption and style direction, letting the model write the lyrics,
So that a production can start from nothing but an idea.

**Acceptance Criteria:**

**Given** the SongPlanner invented-lyrics workflow (`SongPlanner + MiniMax Music 3 - Quality BF16.json`) audited as a checksummed reference export
**When** the Director submits a caption, maximum duration, and seed from the Song workspace
**Then** an explicit API-format payload (never the saved editor JSON) is submitted to ComfyUI
**And** unit tests validate the payload against a recorded `/object_info` fixture, with a live pre-flight audit (Story 3.1 pattern) checking classes and models against the running server before first submission
**And** the resulting RenderJob records prompt ID, seed, and kind `music` (FR-13, FR-24).

**Given** the generation completes
**When** the job is refreshed
**Then** the Song's path points at the ComfyUI output and it plays through the existing transport.

### Story 1.2: Generate a Cover from Known Lyrics

As the Director,
I want to generate a song from lyrics I supply,
So that covers and already-written songs get the same treatment as invented ones.

**Acceptance Criteria:**

**Given** the Known_Lyrics workflow variant audited as a checksummed reference export
**When** the Director supplies lyrics with the caption
**Then** the supplied lyrics appear verbatim in the submitted payload's lyric input, asserted by unit test (FR-14)
**And** the variant is selectable independently of the invented-lyrics path
**And** the two adapters share all structure except lyric handling, verified by a unit test comparing their payloads.

### Story 1.3: Live Song-Generation Smoke and Import Regression

As the Director,
I want one short real song generated through each variant from this application,
So that song generation is verified capability, not unit-tested claim.

**Acceptance Criteria:**

**Given** live ComfyUI and explicit GPU-cost confirmation
**When** one short (≤30 s) song is generated through each adapter
<!-- Amended 2026-08-17 on live evidence: was "≤16 s", which is unachievable. `M3SongPlanner.duration_seconds`
     carries `min: 30.0` in the live /object_info schema, so a 16 s request is rejected by ComfyUI at prompt
     validation (`value_smaller_than_min`) before any GPU work. 30 s is the shortest song this adapter can
     produce; both variants were verified live at 30 s (measured 29.989 s). -->

**Then** both outputs exist on disk, play in the application, and `docs/ROADMAP.md` records the verification.

**Given** an imported WAV whose duration the browser could not decode
**When** the application restarts
**Then** the duration is still available via the `ffprobe` fallback (FR-12 regression guard).

**Given** a project with existing Shots
**When** the Director replaces or removes the Song
**Then** the action requires explicit confirmation naming that Shot windows and Assembly synchronization depend on it, and no Shot data is deleted.

## Epic 2: A Render-Ready Shot Plan

Conversation becomes a complete, varied, fully prompted Shot Plan. The data-loss guard (FR-15) and window flagging (FR-17) shipped 2026-08-16 with tests; this epic adds document recovery, story expansion, and the submission gate.

### Story 2.1: Reviewable Document Replacement with Session Recovery

As the Director,
I want replacing a non-empty Treatment or Style Bible to be a visible, reversible act,
So that no Director call can cost me creative work — even one that passes the degradation guard.

**Acceptance Criteria:**

**Given** a project with a non-empty Treatment
**When** a Director call returns a replacement that passes `document_rejection()`
**Then** the previous version is retained and recoverable within the session (FR-16)
**And** the chat reply states which documents changed
**And** a restore action returns the prior version without a Director call.

**Given** a locked field
**When** any Director result is applied
**Then** the locked field is unmodified (FR-16).

### Story 2.2: Story Expansion into Fully Prompted Shots

As the Director,
I want to expand the Treatment, Style Bible, and shot windows into a render-ready prompt per Shot,
So that the video's text is fully planned — with deliberate shot-to-shot variance matched to the song — before any GPU time is spent.

**Acceptance Criteria:**

**Given** a project with a Treatment, Style Bible, and timed Shot windows
**When** the Director invokes story expansion
**Then** every Shot receives a fully written prompt embedding the Style Bible's continuity constants (identity, wardrobe, palette, lens) while varying per-Shot action, framing, and energy (FR-26)
**And** the expansion input verifiably includes each Shot's position within the Song — and section boundaries when analysis exists — so structure can inform variance deterministically
**And** expanded prompts land as editable Shot prompts reviewed through the existing shot inspector, and the expansion never queues a render
**And** expanded prompts pass through the existing degradation and window checks (FR-15, FR-17).

### Story 2.3: Shot Plan Readiness Gate

As the Director,
I want an explicit readiness check over the Shot Plan before submission,
So that a Batch can never contain empty or copy-paste prompts.

**Acceptance Criteria:**

**Given** a Shot Plan containing a Shot with an empty prompt
**When** readiness is evaluated
**Then** the plan is reported not ready, naming the blocking Shots (FR-26)
**And** any Shot submission path refuses an empty-prompt Shot.

**Given** two Shots whose prompts are identical after lowercasing and whitespace collapse, or whose token overlap exceeds 90%
**When** readiness is evaluated
**Then** both are flagged as lacking variance, and the Director can differentiate or accept them deliberately — the flag warns, only emptiness blocks (FR-26).

### Story 2.4: Director Safety Notices Are Unmissable

As the Director,
I want rejection and window notices rendered distinctly in the Treatment workspace,
So that a protective refusal never reads like a normal chat reply.

**Acceptance Criteria:**

**Given** a Director call whose result triggered a document rejection, an empty-shot-list notice, or a window flag
**When** the reply renders in the Treatment workspace
**Then** the notice block is visually distinct from the assistant prose, and the raw rejected output is inspectable (FR-15, FR-17 surfacing)
**And** regression tests cover notice rendering in the frontend contract suite.

**Given** the shipped guard logic
**When** the backend regression suite runs
**Then** tests assert JSON-as-prose rejection, the <40% collapse floor, the empty-target skip, the prose-claims-shots mismatch notice, and the 4–15 s window flag (FR-15, FR-17 regression).

## Epic 3: Continuity Proven, Not Promised

The reference path — character to Reference Sheet to reference-driven Shot — produces a real render on live ComfyUI. Promotion (FR-18) and attachment (FR-19) are implemented; the H3 Ultra adapter (FR-20) has never rendered live. This epic retires that risk at minimum cost.

### Story 3.1: H3 Ultra Pre-Flight Audit

As the Director,
I want every class, model, and reference-file path in the H3 Ultra payload validated against the live server before spending GPU time,
So that a reference render cannot fail for a reason a free check would have caught.

**Acceptance Criteria:**

**Given** live ComfyUI
**When** the pre-flight audit runs
**Then** all H3 Ultra node classes and model files are confirmed present via `/object_info`, reading combo options from the `[1]["options"]` shape
**And** each attached reference resolves to a real file within project media or contained ComfyUI output (FR-19)
**And** the deterministic `<Picture N>`/`<Video N>`/`<Audio N>` numbering is asserted for a fixed attachment order (FR-19)
**And** exceeding the 9-picture/3-video/3-audio limits produces a clear 422 message, asserted in tests (FR-19)
**And** unit assertions cover the routing rule — a Shot with attached Assets builds the reference payload, one with none builds the text-only payload (FR-20)
**And** promotion assertions cover that a Reference Sheet records its parent Asset and leaves the source Asset unmodified (FR-18).

### Story 3.2: First Live Reference-Driven Render

As the Director,
I want one reference-driven Shot rendered live using a character Reference Sheet,
So that character consistency becomes demonstrated capability.

**Acceptance Criteria:**

**Given** a promoted Reference Sheet (FR-18) attached to a ready Shot, explicit GPU-cost confirmation, and a minimum-cost window (~4 s, 640×384, low steps)
**When** the Shot is submitted through the reference path (FR-20)
**Then** ComfyUI accepts the 18-node graph, and the completed output is verified with `ffprobe` for frames, duration, and synchronized audio
**And** `latest_output` is written while `approved_output` stays empty
**And** `docs/ROADMAP.md` records either the verified render or the exact ComfyUI error alongside its shipped fix or filed defect — readiness is never silently overstated.

## Epic 4: One-Pass Video, Watched Live

The product's highest-value interaction: submit the whole Shot Plan as one Batch, watch completed Shots land on the timeline while later ones render, flag failures without interrupting, regenerate only what failed. GPU coordination lives at the confirmation surface. Residency is measured (438 s cold / 288 s warm), so ordering — not machinery — preserves it.

### Story 4.1: Render Confirmation with GPU Coordination

As the Director,
I want the render confirmation to free and report VRAM before I commit,
So that a 31 GB model stack never collides with a resident language model I forgot about.

**Acceptance Criteria:**

**Given** the configured LM Studio endpoint has a model loaded
**When** a render confirmation is shown
**Then** the loaded model is named, with a statement that it holds VRAM outside ComfyUI's control (FR-11)
**And** free VRAM from ComfyUI `/system_stats` is displayed as context, never as a gate
**And** confirming the Batch automatically requests LM Studio unload its models before submission — with a visible skip control — then re-reads and reports observed free VRAM rather than assuming success (FR-10).

**Given** no model is loaded on the configured endpoint
**When** the confirmation is shown
**Then** no warning is displayed (FR-11).

**Given** no language-model endpoint is configured, or the unload fails
**When** the confirmation is shown
**Then** no error blocks rendering — the risk is stated and the Director may proceed (FR-10, FR-11).

### Story 4.2: Submit the Shot Plan as One Batch

As the Director,
I want every ready Shot submitted as a single confirmed Batch,
So that rendering a whole video is one act of intent, not forty.

**Acceptance Criteria:**

**Given** a Shot Plan that passes the readiness gate (Story 2.3)
**When** the Director confirms once, seeing the Shot count
**Then** each ready Shot is submitted as its own RenderJob with its own prompt ID (FR-4)
**And** Shots are grouped by payload kind — text-only H3 Shots contiguous, H3 Ultra reference Shots contiguous — with no other workflow kind interleaved, and no free/unload/interrupt call issued mid-Batch (FR-9; the two H3 payloads load different UNET sets, so mixing them mid-run costs ~150 s per eviction)
**And** a Shot failing validation is skipped and reported by ID without blocking the rest (FR-4).

### Story 4.3: Truthful Render State in the Interface

As the Director,
I want the Queue and timeline to show which Shots are executing, waiting, complete, or failed — with exact errors,
So that a twelve-minute render never looks like a stuck queue.

**Acceptance Criteria:**

**Given** a prompt executing on ComfyUI with no history entry yet
**When** the interface refreshes jobs on its polling interval
**Then** that Shot displays as running, distinct from queued (FR-6; backend shipped 2026-08-16)
**And** a failed Shot displays the exact ComfyUI execution error on the Shot itself
**And** refresh happens automatically during an active Batch without a manual button press.

**Given** the application restarts while a Batch is partially drained
**When** jobs refresh after restart
**Then** in-flight prompts are re-located via `/queue`, completed Shots reconcile from history, flags persist, and no job is invented or lost (FR-24, NFR-2).

### Story 4.4: Live Timeline Population

As the Director,
I want each completed Shot to take its place on the timeline the moment it finishes,
So that I can watch the video grow while it renders.

**Acceptance Criteria:**

**Given** an active Batch with some Shots complete
**When** a Shot's job reconciles to complete
**Then** its output appears on the timeline without a reload or navigation (FR-7)
**And** playback runs from the start through every completed Shot while later Shots still render
**And** the boundary between rendered and pending Shots is visible on the timeline
**And** a later completion never disturbs playback of earlier Shots (FR-7, NFR-1).

### Story 4.5: Flag In-Flight, Regenerate in Place

As the Director,
I want to flag bad Shots while the Batch still runs, then re-render only those,
So that fixing three Shots never costs me the other thirty.

**Acceptance Criteria:**

**Given** an active Batch and a completed Shot the Director dislikes
**When** the Shot is flagged
**Then** the flag persists on the Shot without cancelling, pausing, or disturbing the Batch (FR-8)
**And** the Shot's prompt, references, and seed remain editable while the Batch runs.

**Given** the Batch has drained and flagged Shots exist
**When** the Director resubmits the flagged set
**Then** only those Shots re-render, each replacing its own `latest_output` and leaving every other Shot and every `approved_output` untouched (FR-5).

**Given** flagged Shots exist while the Batch is still active
**When** the Director attempts resubmission
**Then** the action is refused with a plain message until the Batch drains, preserving same-kind ordering (FR-9).

### Story 4.6: Responsive Under a Full Batch

As the Director,
I want the application fully usable while forty Shots render,
So that reviewing and flagging during a Batch is real, not theoretical.

**Acceptance Criteria:**

**Given** a simulated 40-Shot Batch against a mocked ComfyUI
**When** reconciliation runs
**Then** a probe asserts `/api/health` answers within 500 ms while a mocked ComfyUI request is held open, all outbound calls carry bounded timeouts, and a hanging request never hangs the application (NFR-1)
**And** browser QA demonstrates navigation, playback, and Shot editing remain interactive during the simulated Batch.

## Epic 5: A Finished, Durable Video

Approved takes become one song-synchronized file, and the project that produced it stays honest and recoverable. Trim is mandatory: a 4.0 s Shot renders at 4.458 s, so untrimmed joins drift ~11% — twenty seconds over a three-minute song.

### Story 5.1: Explicit Take Approval

As the Director,
I want to approve or un-approve a Shot's latest output with one action,
So that what enters the final video is always my decision.

**Acceptance Criteria:**

**Given** a Shot with a `latest_output`
**When** the Director approves it
**Then** `approved_output` is set to that take, reversibly, and never by render completion (FR-21)
**And** the timeline shows approval state per Shot
**And** any Shot lacking an Approved Output is listed as blocking Assembly (FR-21).

### Story 5.2: Assemble the Video

As the Director,
I want every Approved Output trimmed and joined into one video synchronized to the Song,
So that the production ends as a single watchable file.

**Acceptance Criteria:**

**Given** every Shot has an Approved Output
**When** Assembly runs
**Then** each clip is trimmed to its Shot's window before joining, defeating grid overrun (FR-22)
**And** the assembled file's duration matches the Song within one frame
**And** the output is verified with `ffprobe` after writing; failure is reported, never presented as success
**And** no Shot's Approved Output is modified (FR-22)
**And** the assembly is recorded as a RenderJob of kind `post` carrying its inputs (Shot IDs and take paths), output path, status, exact error, and timestamps — prompt ID and seed are empty by design for local ffmpeg work (FR-24, adapted).

**Given** a Shot whose window changed after its take was approved
**When** Assembly is requested
**Then** the stale take is reported by Shot ID and Assembly refuses until it is re-approved (FR-22).

**Given** a Shot Plan whose windows leave gaps or overlaps against the Song
**When** Assembly is requested
**Then** the uncovered or conflicting ranges are reported instead of producing a silently mistimed file (FR-22).

### Story 5.3: Honest Recovery and Missing Media

As the Director,
I want missing media reported plainly and project integrity regression-guarded,
So that a six-month-old project tells me the truth about its own state.

**Acceptance Criteria:**

**Given** a Shot or Asset whose recorded file no longer exists on disk
**When** the project is viewed
**Then** the item displays as missing, not blank (FR-25).

**Given** the existing atomic-write and serialized-save behavior (NFR-2)
**When** the regression suite runs
**Then** tests assert temp-file-plus-replace writes, stale-revision rejection, malformed-manifest skip, and that a RenderJob's full provenance set — kind, prompt ID, seed, target, output paths, status, exact error, timestamps — survives restart as a set (FR-24, FR-25, NFR-2).

## Epic 6: The Production Wizard

A guided path composed from the real workspaces — derived from project state, escapable, never seen again once outgrown. Reconciled with the Operate/Command-Inspect decision in `docs/ARCHITECTURE.md`: sequencing, not decoration.

### Story 6.1: Derived Wizard State

As the Director,
I want the wizard's current step computed purely from the project manifest,
So that it is resumable for free and can never desynchronize from reality.

**Acceptance Criteria:**

**Given** any project manifest
**When** the wizard step is resolved
**Then** it is a pure function of project state — no Song → Song step; Song but no Shots → Treatment step; Shots but no completed render → Render step — with no persisted progress field (FR-1)
**And** deleting the Song returns the project to the Song step
**And** a project with one completed RenderJob never opens in the wizard (FR-3).

### Story 6.2: Steps Compose the Real Workspaces

As the Director,
I want each wizard step to present the actual workspace scoped to that step's decision,
So that the wizard teaches the editor instead of hiding it.

**Acceptance Criteria:**

**Given** the wizard at any step
**When** the step renders
**Then** it presents the same component the full editor uses for that workspace (FR-2)
**And** an action taken inside the wizard produces semantically equal project state to the same action in the editor, excluding volatile fields such as timestamps
**And** a skip control at every step moves to the full editor without altering project state (FR-3).

### Story 6.3: First-Run Journey Verified in a Browser

As the Director,
I want the song-to-first-Shot path verified end to end in headless Edge,
So that the wizard path is verified end to end up to submission, with the post-render exit exercised against a mocked completion — SM-3's full claim is then measured by the first live wizard-driven render.

**Acceptance Criteria:**

**Given** an empty isolated data root on port 8766
**When** the first-run browser QA drives the wizard from project creation through Song, Treatment, Cast, and Shots to the render confirmation (submission mocked)
**Then** every step is reached without leaving the wizard and with zero severe console errors
**And** with submission mocked to complete, the wizard resolves past the Render step and never reappears for that project (FR-3)
**And** the ComfyUI-offline edge case shows a plain unavailable state at the step that needs it, not fake progress.

## Epic 7: Finishing (Drop-Conditioned)

Approved renders pass through the enhancement chain without being regenerated. Sequenced last by decision: if Story 7.1's adapter is not proven when all other MVP work is done, Finishing ships in v2 and the first video ships unfinished.

### Story 7.1: Standalone Approved-Take Enhancement Adapter (Go/No-Go Spike)

As the Director,
I want an LTX 2.5 enhancement graph that accepts an already-approved take as input,
So that Finishing improves my video instead of regenerating it from creator-specific media.

**Acceptance Criteria:**

**Given** an Approved Output file and the audited `divisible_by=32` boundary patch
**When** the standalone adapter builds its graph
**Then** the graph's input is the approved take — no H3 regeneration nodes present (FR-23)
**And** one short live run (explicit GPU confirmation) either completes and is `ffprobe`-verified, or the exact failure is recorded
**And** the GO/NO-GO outcome and drop-condition consequence are written to `docs/ROADMAP.md`.

> **Amended 2026-08-17 — divisor was `16` in the ratified text.** The live boundary run (prompt `a64a0460-64e6-4a14-b207-e644bf9bda5d`, `success` in 17 min 36 s) disproved 16: the LTX 2.5 VAE's total spatial compression is 32, so 16 leaves height 720 (720/32 = 22.5). `ffprobe` measured **2496×1408**, exactly 2 × 1248×704 through the subgraph's 2× latent upsample; 16 would have given 2496×1440. Two further facts this story must absorb: the boundary does **not** preserve frame count (192 in, 185 out, an 8k+1 grid) so `ffprobe` verification must not assert equal frame counts; and `keep_proportion: "resize"` **stretches** rather than crops, taking aspect 1.7368 → 1.7727. See `docs/WORKFLOW-MAP.md` and ADR AD-12.

### Story 7.2: Finishing as a Route on Approved Takes

As the Director,
I want Finishing invocable per approved Shot from the Queue, with failures leaving originals intact,
So that enhancement is a safe, optional quality multiplier.

**Acceptance Criteria:**

**Given** Story 7.1 concluded GO
**When** the Director applies Finishing to an approved Shot
**Then** a RenderJob of kind matching the Finishing stage is created with full provenance (FR-24)
**And** dimensions are normalized to each downstream stage's constraints before that stage runs (FR-23)
**And** a Finishing failure leaves the input Approved Output untouched and reports the exact error (FR-23)
**And** the finished result becomes a new selectable take — approval of it remains the Director's explicit act (FR-21).

