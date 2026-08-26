---
stepsCompleted: [1, 2, 3, 4]
inputDocuments:
  - _bmad-output/planning-artifacts/prds/prd-MusicVideoProducer-effects-2026-08-21/prd.md
  - _bmad-output/planning-artifacts/prds/prd-MusicVideoProducer-effects-2026-08-21/addendum.md
  - _bmad-output/planning-artifacts/effects-director-rulings-2026-08-21.md
  - _bmad-output/planning-artifacts/effects-and-transitions-research-2026-08-21.md
  - _bmad-output/planning-artifacts/ux-designs/ux-effects-2026-08-21/DESIGN.md
  - _bmad-output/planning-artifacts/ux-designs/ux-effects-2026-08-21/EXPERIENCE.md
  - _bmad-output/planning-artifacts/architecture/architecture-MusicVideoProducer-effects-2026-08-21/ARCHITECTURE-SPINE.md
  - _bmad-output/planning-artifacts/architecture/architecture-MusicVideoProducer-effects-2026-08-21/BUILD-ORDER.md
---

# MusicVideoProducer - Epic Breakdown: Shot Effects and Transitions

## Overview

This document breaks down the **Shot Effects and Transitions** feature (25 FX requirements, 6 FX-NFRs) into implementable stories. It is a companion to `epics.md`, which covers Epics 1–7 of the base product and remains authoritative for those; epic numbering continues here at **Epic 8** so the two documents share one numbering space and a story ID is unambiguous across both.

Brownfield, and unusually well-supported: `assembly.py` already re-encodes every clip through a `-vf` chain (so effects are nearly free), already normalizes every intermediate to identical geometry, rate, SAR and pixel format (so `xfade`'s precondition holds by construction), and already resolves clip overlaps (so a transition needs no new timeline geometry). The architecture spine's AD-16…AD-31 fix the invariants.

**On the relationship to `BUILD-ORDER.md`.** That document's six slices A–F remain the *build* sequence and are unchanged. They are not the epic structure: slices B (chain builder) and D (preview) deliver nothing a Director can see on their own, and slices B, C and D touch the same files end to end — `effects.py`, the effects routes, `app.js`, `styles.css`. Organised by user value and consolidated for file overlap, they collapse into the four epics below. Each story names the slice it implements, so the build order survives the regrouping.

> **Sibling documents.** [`epics.md`](epics.md) holds Epics 1–7 (base product); [`epics-treatment.md`](epics-treatment.md) holds Epics 12–17 (Treatment Planning). One numbering space across all three.
>
> **Cross-feature note.** Story 8.1 ships **first** by the Director's decision, so Treatment Planning owns making the two song analyses share one trigger — see its Story 16.2. Nothing in this document changes because of it.

## Requirements Inventory

### Functional Requirements

FX-1: Analyze a Song into a Song Envelope — RMS, peak, spectral-flux proxy, per-band envelopes, onsets, beats, estimated BPM; cached, invalidated on song change, never blocking
FX-2: Show beat and onset markers against the waveform; display only, toggleable
FX-3: Snap Shot-boundary edits to beats alongside the playhead and phrase-gap targets; always an assist, never a constraint. **Corrected 2026-08-24 (R-15):** this said *lyric* boundaries. Verified against the pre-epic code — no lyric or phrase target existed on a boundary drag at all; the only one was the playhead magnet, and lyric/phrase snapping lived solely in the batch "Snap cuts" button. The shipped kinds are playhead, phrase gaps and beats. A lyric-word target was deliberately rejected: the batch snapper clamps into voiceless gaps, so offering word edges would be a second opinion about where a cut belongs
FX-4: Two tabs in the shot inspector — Shot Info (unchanged) and Effects — with tab selection surviving background rebuilds
FX-5: Build a Shot's Effect Stack — add, remove, reorder, individually disable; an empty stack builds the byte-identical export *command* it builds today (R-20: the mp4 itself is not byte-reproducible)
FX-6: Copy an Effect Stack to an explicit set of Shots, reporting what it did and naming refusals
FX-7: Refuse Effect edits on a locked Shot, stating the lock as the reason
FX-8: Grade family — LUT look, exposure, contrast, saturation, temperature, tint, lift/gamma/gain, monochrome
FX-9: Texture family — grain, vignette, bloom/halation, diffusion, banding suppression; treating the picture, not the letterbox padding
FX-10: Stylize family — RGB/chroma split, pixel shuffle, posterize, edge treatment, scanline/CRT; all off by default
FX-11: Geometry family — punch-in, slow zoom, handheld shake, dutch tilt, mirror; applied before scaling, never changing frame count
FX-12: Bind any Effect Parameter to a Band, with the manual value as its resting value and removal leaving no residue
FX-13: Choose the Band by centre, width and softness, shown against the song's own spectrum
FX-14: Choose the Drive — punch or sustain, explicitly, with a Trigger Floor and a bounded Depth
FX-15: Refuse binding without a Song Envelope, by name and with an action; retain stored bindings as unresolvable rather than dropping them
FX-16: Author a Transition by overlapping clips; the Overlap's length is the Transition's length; the region highlights blue
FX-17: Set a Transition Pair — setting one side sets the other to match across an Overlap, and says so
FX-18: One-sided transitions treat the Shot's own final or opening frames, followed or preceded by a hard cut
FX-19: A curated transition catalogue named in the Director's language, not an exhaustive dump
FX-20: Preview a Shot's Effect Stack as a looping clip through the real filter chain
FX-21: Preview a Transition across the boundary it spans
FX-22: Show the Drive envelope moving a bound parameter, aligned with the preview
FX-23: Effects are non-destructive and re-derivable; no Approved Output is ever rewritten
FX-24: Export refuses on unapplicable Effects, naming every Shot and reason in one report
FX-25: A completed export records the Effect and Transition state it was built from

### NonFunctional Requirements

FX-NFR-1: The frame grid is inviolable — assembled duration matches the Song within one frame for every combination of Effects and Transitions, in both the Overlap and no-Overlap case
FX-NFR-2: The export stays a stream-copy join — clips carrying no Effects are never re-encoded on account of a Transition elsewhere
FX-NFR-3: One engine describes an Effect — no Effect is approximated in the interface by a different mechanism than the one that renders it
FX-NFR-4: No new runtime dependency — everything through the ffmpeg binary already required and the language already in use. **Amended 2026-08-24 (R-8):** `numpy` is declared in `pyproject.toml`. It was already locked transitively via `faster-whisper`, so `uv.lock` gained no package and nothing new installs — the declaration made an existing fact honest. Not met literally
FX-NFR-5: Generated render inputs are pure and comparable — same project, same bytes, asserted by string comparison
FX-NFR-6: Preview stays inside a measured budget — under one second from change to looping clip for a Shot of typical length

### Additional Requirements

*From the architecture spine (AD-16…AD-31) and its inherited constraints.*

- Effects and transition fields live on `Shot` but are written **only** by dedicated routes; `replace_project` adopts them from the stored Shot via the established `_adopt_*` idiom (AD-16). That route's own comments record this hole found six times
- One pure chain builder with a fixed family order: `trim → GEOMETRY → scale → TEXTURE → GRADE → STYLIZE → pad → fps → setsar → format` (AD-17). Measured 2026-08-21: texture after `pad` leaves the letterbox bar at RGB (1,1,5); before `pad`, (0,0,0)
- A transition is baked into its own concat intermediate so the join keeps `-c:v copy`; `clip_frames_on_grid` is not modified (AD-18)
- The Overlap is the only transition geometry — no stored duration, no borrowing from the over-render margin, which external clips do not have (AD-19)
- The Song Envelope is a sidecar file; the manifest carries only a pointer, the rate, the band count, the BPM, and the song fingerprint (AD-20). Measured 2026-08-24 through the shipped extractor: manifests are 110–190 KB, an envelope is **405 KB** (469 KB on a real 202-second master), and the manifest rides a 2-second poll. The ~750 KB this line carried was an estimate
- Envelope validity is derived by fingerprint comparison, never stored as a flag (AD-21), following AD-11's read-time discipline
- A reactive binding compiles to a generated `sendcmd` script, passed as a **cwd-relative filename** — an absolute Windows path's drive colon breaks filter parsing and names the wrong filter (AD-22)
- Preview renders are a derived cache keyed by a fingerprint; staleness is recomputed, never stored; preview uses libx264 `ultrafast` CRF 28 at half the export's dimensions, and **not** NVENC, which is slower at these clip lengths (AD-23)
- At most one preview render in flight per project; a new request cancels the old, and a superseded render is discarded rather than played (AD-24)
- Two new pure modules, `audio.py` and `effects.py`; neither imports `app.py`, `batch.py`, or `assembly.py` (AD-25)
- The spectrum strip draws a whole-song band average, stored once in the envelope, so a copied binding keeps its meaning (AD-26)
- Effect specs are validated against a server-side catalogue before storage; nothing client-supplied is interpolated into a filter string, and a LUT is referenced by catalogue id, never by path (AD-27)
- One fingerprint function with explicitly ordered inputs; the song fingerprint is content-derived, never mtime (AD-28)
- Preview geometry derives from the **export** geometry, not the take's, so a Shot whose aspect differs previews with the letterbox it will ship with (AD-29)
- The outgoing Shot's `transition_out` is authoritative for a paired transition; a disagreeing pair reports the divergence rather than refusing (AD-30)
- The chain builder sorts the stack by family on read, so storage order is never load-bearing (AD-31)
- Inherited and binding: AD-9 (assembly is local ffmpeg, trim-then-concat, one cumulative grid), AD-11 (derived not persisted), AD-13 (approval is `approved_output`), AD-14 (guarded persistence boundary), AD-15 (ComfyUI untouched)

### UX Design Requirements

UX-DR1: Add `--blue #5b9bd5` as the sixth and final accent token, reserved permanently to transitions and reactive bindings; amend DESIGN.md's "no new accent colors" anti-goal in place with the argument for the exception
UX-DR2: Inspector tab strip built from a data array in the existing `ASSET_TABS` idiom, implemented as a real tablist — `role="tablist"`, arrow-key movement, `aria-selected`, panels bound by `aria-controls`
UX-DR3: Effect card component — drag handle, Consolas family micro-label, effect name, enable toggle, remove; disabled cards at 45% opacity with controls still readable
UX-DR4: Parameter row component — label, slider on `--line` track with `--acid` fill, Consolas numeric readout, and the `〜` bind glyph at the right edge
UX-DR5: Band panel component — inline beneath its parameter row, `--surface-2`, `--blue` left edge, containing spectrum strip, `punch | sustain` segmented control, floor and depth sliders, and an explicit `Remove binding`
UX-DR6: Spectrum strip canvas — whole-song average as `--dim` bars with the Band as a `--blue` region with softness falloff; draggable region, edges and softness handle; three labelled numeric inputs as the keyboard and screen-reader equivalent
UX-DR7: Drive readout canvas beneath the Monitor — envelope in `--blue`, Trigger Floor as a `--dim` hairline, `--acid` playhead drawn through, and the envelope drawn `--dim` where it falls below the floor; `aria-hidden` with its facts also stated in text
UX-DR8: Overlap band on the timeline — typed: `--blue` fill at 22% behind clip content with blue hairlines and a centred Consolas type label; untyped: `--line-strong` hatch with a `CUT` label and no blue; never a drag target, so clip-edge dragging is unaffected
UX-DR9: `ƒ` corner chip on clips carrying Effects, in the existing 14px idiom, reading order `✓ ƒ ⚑`; three chips is the corner's maximum
UX-DR10: Transition pair rows — `--blue` left edge with the Overlap's length when paired, `--dim` left edge with the one-sided explanation when not; both live, neither disabled
UX-DR11: Monitor `STALE` state — the previous picture continues playing with a Consolas corner label; never a frozen frame, never a spinner over black, never a percentage
UX-DR12: Microcopy per the Voice and Tone rules — automatic changes announced in the past tense naming both Shots, refusals naming the thing and the reason with an action, estimates labelled as estimates
UX-DR13: Focus-preserving rebuild extended to cover the active tab and any open band panel, so the two-second background reload never steals an in-progress edit
UX-DR14: Effect picker as a grouped list under four Consolas family headers, no thumbnails; copy-stack target chooser that names its targets explicitly, states the replacement before running, and reports refusals by Shot
UX-DR15: Accessibility floor — state never colour-alone (Overlap carries its type as text, `ƒ` is a glyph, bindings show `〜`); every drag has a keyboard path; no new motion introduced

### FR Coverage Map

FX-1: Epic 8 — Song Envelope extraction, cached and fingerprint-invalidated
FX-2: Epic 8 — beat and onset markers on the waveform
FX-3: Epic 8 — beat snapping for Shot-boundary edits
FX-4: Epic 9 — the two-tab shot inspector
FX-5: Epic 9 — Effect Stack editing
FX-6: Epic 9 — copying a Stack across Shots
FX-7: Epic 9 — locked-Shot refusal in the Effects tab
FX-8: Epic 9 — Grade family
FX-9: Epic 9 — Texture family
FX-10: Epic 9 — Stylize family
FX-11: Epic 9 — Geometry family
FX-12: Epic 10 — binding a parameter to a Band
FX-13: Epic 10 — Band selection against the song's spectrum
FX-14: Epic 10 — punch/sustain Drive with floor and depth
FX-15: Epic 10 — refusal without a Song Envelope, bindings retained
FX-16: Epic 11 — Overlap-authored Transitions with the blue band
FX-17: Epic 11 — the Transition Pair and its auto-match
FX-18: Epic 11 — one-sided transitions
FX-19: Epic 11 — the curated transition catalogue
FX-20: Epic 9 — looping Preview Clip through the real chain
FX-21: Epic 11 — Transition preview across the boundary
FX-22: Epic 10 — the Drive readout
FX-23: Epic 9 — non-destructive, re-derivable Effects
FX-24: Epic 9 — export refusals naming every Shot and reason
FX-25: Epic 9 — export provenance records the look
FX-NFR-1: Epic 11 — the frame grid across every Effect and Transition combination
FX-NFR-2: Epic 11 — the stream-copy join (guarded in Epic 9, threatened in Epic 11)
FX-NFR-3: Epic 9 — one engine describes an Effect
FX-NFR-4: Epic 8, Epic 9 — no new runtime dependency
FX-NFR-5: Epic 9, Epic 10 — pure, comparable generated render inputs
FX-NFR-6: Epic 9 — the measured preview budget

## Epic List

### Epic 8: The Song Becomes Measurable
The Director sees where the beats are and can put a cut on one. The song stops being a waveform and becomes a structure with named moments — which is what every later epic binds to, and what makes the standing "snap cuts to phrase boundaries" ruling mean something for the first time.
**FRs covered:** FX-1, FX-2, FX-3, FX-NFR-4

### Epic 9: One Look Across a Song
The Director gives a Shot a look and sees it, then carries that look across the whole video. Forty independently generated clips become one film. Consolidates the chain builder, the Effects tab and the preview, which are one component end to end and share every file they touch — the Director cannot judge a grade they cannot see, and a preview of nothing is nothing.
**FRs covered:** FX-4, FX-5, FX-6, FX-7, FX-8, FX-9, FX-10, FX-11, FX-20, FX-23, FX-24, FX-25, FX-NFR-3, FX-NFR-4, FX-NFR-5, FX-NFR-6

### Epic 10: The Picture Moves With the Music
The Director ties a parameter to a frequency band and the video answers the track — grain surging on the kick, the frame breathing with the bass — without animating anything by hand. The song becomes the automation.
**FRs covered:** FX-12, FX-13, FX-14, FX-15, FX-22, FX-NFR-5

### Epic 11: Cuts That Blend
The Director drags two clips together and the cut between them becomes a transition, visible on the timeline and exactly as long as the overlap. The only epic that touches the cumulative frame grid, and isolated for that reason.
**FRs covered:** FX-16, FX-17, FX-18, FX-19, FX-21, FX-NFR-1, FX-NFR-2

## Epic 8: The Song Becomes Measurable

The Director sees where the beats are and can put a cut on one. Independent of every other epic — it ships value with zero effects in the project — and it is the prerequisite every reactive binding later resolves against.

### Story 8.1: Analyze the Song into an Envelope

As the Director,
I want the application to measure my song's levels, onsets, beats and tempo,
So that later work can be tied to what the music actually does instead of to a guess.

**Acceptance Criteria:**

**Given** a Project with a Song on disk
**When** analysis runs
**Then** a Song Envelope is produced carrying, at a recorded analysis rate, RMS, peak, a spectral-flux proxy, per-band level envelopes, onset markers, beat markers, and one estimated BPM (FX-1)
**And** it also carries a whole-song per-band average as a small fixed-size array, for the band selector to draw (AD-26)
**And** the analysis rate and band count are recorded fields on the envelope, not constants, so tuning them later is not a migration
**And** the computation is in the application's own language and decoding goes through the ffmpeg binary already required (FX-NFR-4, AD-25) — **amended 2026-08-24 (R-8): `numpy` is declared; see FX-NFR-4**
**And** `audio.py` imports neither `app.py`, `batch.py`, nor `assembly.py` (AD-25).

**Given** a Song is imported or generated
**When** the Song is first stored
**Then** analysis is produced automatically, and it never blocks the interface, a render, a Batch, or an Assembly (FX-1).

**Given** an envelope for a 3-minute song
**When** it is persisted
**Then** it is written as a sidecar file under the project media dir, and the manifest carries only a defaulted `SongAnalysis` record — a pointer, the analysis rate, the band count, the estimated BPM, and the song fingerprint it was computed from (AD-20)
**And** `SongAnalysis` is the only model entity this story adds, and it carries a default so every existing `project.json` loads unchanged
**And** the envelope is never embedded in a Project response, and is served by its own read-only endpoint
**And** a test asserts the manifest's own size is not materially changed by the presence of an envelope.

**Given** a Project whose Song has been replaced
**When** anything reads the envelope
**Then** validity is decided by comparing the stored song fingerprint against the current Song's, computed by the one fingerprint function over content — file size and a hash of bytes, never mtime (AD-21, AD-28)
**And** a mismatch is reported as **absent**, never served as current
**And** nothing writes an invalidation flag.

**Given** an analysis that fails
**When** the failure is reported
**Then** the reason is named, the Project is otherwise unchanged, and nothing downstream treats the failure as an envelope of zeros (FX-1)
**And** the estimated BPM is presented as an estimate wherever it appears, and nothing refuses on its value.

### Story 8.2: Beats on the Waveform

As the Director,
I want to see the song's beats and onsets against the waveform,
So that I can see the structure I am cutting against instead of inferring it.

**Acceptance Criteria:**

**Given** a Project with a valid Song Envelope
**When** the Timeline workspace is shown
**Then** beat and onset markers are drawn against the existing waveform (FX-2)
**And** the markers are display only — nothing about any Shot changes when they are shown or hidden
**And** they can be turned off, and the setting persists.

**Given** a Project with no Song Envelope, or one invalidated by a song change
**When** the Timeline workspace is shown
**Then** no markers are drawn and no error is raised (FX-2, FX-15).

### Story 8.3: Snap Shot Boundaries to Beats

As the Director,
I want a Shot edge I am dragging to snap to a beat as readily as it snaps to anything else,
So that a cut lands on the music instead of near it.

**Acceptance Criteria:**

**Given** a valid Song Envelope and a Shot boundary being dragged
**When** the boundary passes near a beat marker
**Then** it snaps to that beat, alongside the playhead and phrase-gap targets (FX-3). *Amended 2026-08-24 (R-15): as written this described behaviour the application never had — see FX-3 above.*

**Given** the Director wants a boundary that is not on a beat
**When** the boundary is placed off every snap target
**Then** it is accepted, and nothing refuses or warns — snapping is an assist and never a constraint (FX-3, inheriting the trim-nudge posture)
**And** beat snapping can be disabled independently of the existing snap targets.

**Given** a Project with no Song Envelope
**When** a Shot boundary is edited
**Then** boundary editing behaves exactly as it does today (FX-3).

## Epic 9: One Look Across a Song

The Director gives a Shot a look, sees it, and carries it across the whole video. Standalone — it needs no song analysis and no transitions. This is the epic the feature is named for.

### Story 9.1: The Effects Tab, and a Shot That Carries a Grade

As the Director,
I want a second tab on the shot inspector where I can give a Shot a colour grade,
So that a Shot stops looking like whatever the model happened to produce.

**Acceptance Criteria:**

**Given** the shot inspector
**When** a Shot is selected
**Then** the panel presents two tabs, `Shot Info` and `Effects`, built from a data array in the existing `ASSET_TABS` idiom (FX-4, UX-DR2)
**And** `Shot Info` contains exactly what the inspector contains today, unchanged in content, order and behaviour
**And** the strip is a real tablist — `role="tablist"`, arrow-key movement between tabs, `aria-selected`, panels bound by `aria-controls` (UX-DR2, UX-DR15)
**And** the `Effects` tab shows a trailing count when the Shot carries anything.

**Given** an edit in progress in either tab
**When** the two-second background reload rebuilds the inspector
**Then** the active tab and the in-progress edit both survive, through the existing `captureInspectorEdit` / `restoreInspectorEdit` mechanism extended to cover them (FX-4, UX-DR13)
**And** selecting a different Shot returns to `Shot Info`.

**Given** the `Effects` tab on a Shot with an empty stack
**When** the Director adds a Grade effect from the picker
**Then** the picker is a grouped list under Consolas family headers with no thumbnails (UX-DR14)
**And** an Effect card is rendered with a drag handle, family micro-label, name, enable toggle and remove control (UX-DR3)
**And** each parameter is a row with a label, slider, Consolas numeric readout and an inert bind glyph at its right edge (UX-DR4).

**Given** the Grade family
**When** its catalogue is offered
**Then** it provides at minimum a LUT look, exposure, contrast, saturation, colour temperature, tint, lift/gamma/gain in some exposed form, and monochrome (FX-8)
**And** every parameter is bounded and every default is a visual no-op
**And** the same parameters on the same source produce the same output frame on every run
**And** a LUT is referenced by catalogue id, never by a client-supplied path (AD-27).

**Given** an `EffectSpec` arriving from the client
**When** it is validated
**Then** an unknown id, an unknown parameter, or a value outside its declared range is refused with a 422 naming the offender, before anything is stored (AD-27)
**And** nothing client-supplied is ever interpolated into a filter string.

**Given** a Shot carrying a Grade
**When** the project is exported
**Then** the grade is composed into the existing `-vf` chain in `trim_args` by the one pure builder in `effects.py`, which imports neither `app.py`, `batch.py`, nor `assembly.py` (AD-17, AD-25)
**And** the Shot's Approved Output file is not modified (FX-23)
**And** a Project with an empty stack everywhere builds the byte-identical ffmpeg **command** it builds today (FX-5, FX-23, R-20)
**And** that is asserted on the argv and on the filter graph's raw frames, never on the exported file, which is not byte-reproducible between two runs of one unchanged export.

**Given** the generic `PUT /api/projects/{project_id}`
**When** a body is submitted that omits `effects`, or invents them
**Then** the stored Shot's effects are preserved unchanged, adopted server-side via the established `_adopt_*` idiom (AD-16)
**And** a test asserts a full-project PUT omitting `effects` leaves every stack intact.

### Story 9.2: See the Grade Before Exporting

As the Director,
I want the Monitor to play the Shot with its effects applied,
So that I can judge a grade against the song instead of against my imagination.

**Acceptance Criteria:**

**Given** a Shot carrying an Effect Stack
**When** it is selected
**Then** the existing Monitor plays the **effected** picture, looping, produced by the same filter chain the export will run — at reduced dimensions and encoder quality, and differing in nothing else (FX-20, FX-NFR-3)
**And** the Preview Clip covers the Shot's exposed window, not the whole over-rendered take
**And** preview never writes to, replaces or modifies the Approved Output, never reaches ComfyUI, and never blocks an export or a Batch (FX-20, AD-24).

**Given** preview geometry must be chosen
**When** a Preview Clip is rendered
**Then** it is half the dimensions `assembly_plan` would choose for this project — the largest-area approved take — never half the previewed take's own dimensions, so a Shot whose aspect differs previews with the letterbox it will ship with (AD-29)
**And** where no export geometry is derivable, preview falls back to the take's own dimensions and says so.

**Given** the Effect Stack changes
**When** the preview becomes out of date
**Then** the previous picture continues playing with a Consolas `STALE` corner label — never a frozen frame, never a spinner over black, never a percentage (UX-DR11)
**And** staleness is decided by recomputing a fingerprint over the take, window, offset, **the chain the stack composes to**, bindings, envelope fingerprint, transition and preview geometry, in that order, by the one fingerprint function — nothing stores a stale flag (AD-23, AD-28)
**And** the fourth slot is the composed chain rather than the stored stack *(amended 2026-08-26)*, because a corrected composer or catalogue default changes the picture without changing the spec — Epic 9's own scanlines fix was served stale from cache for exactly that reason.

**Given** a parameter being dragged
**When** changes arrive faster than renders complete
**Then** at most one preview render is in flight per project, a new request cancels the in-flight one, and a superseded render is discarded rather than played (FX-20, AD-24)
**And** a Preview Clip for a Shot of typical length is ready in under one second from the change that invalidated it, measured rather than asserted (FX-NFR-6).

**Given** the preview cache
**When** it is deleted
**Then** the only consequence is a re-render; it is never an input to an export (AD-23)
**And** preview renders use libx264 `ultrafast` CRF 28 and not a hardware encoder, which is slower at these clip lengths (AD-23).

**Given** a preview render that fails
**When** the failure is reported
**Then** its reason is named and the Effect Stack is untouched (FX-20).

### Story 9.3: Texture, Stylize and Geometry Families

As the Director,
I want grain, glitch and camera moves as well as colour,
So that I can hide the plastic sheen of generated footage and put motion where the model gave me none.

> **Split 2026-08-25 by Director ruling.** Five of the effects this story names cannot be expressed
> by the chain as built, and never could have been: **slow zoom** (Geometry), **bloom/halation**
> (Texture), and **edge treatment**, **scanline/CRT** and **pixel shuffle/sort** (Stylize). Each
> needs either a *branched* filtergraph (`split`/`blend`) or the clip's own **duration**, and the
> chain is a single comma-joined linear graph spliced into an argv that carries neither. They move
> to **Story 9.7**, which is the change to what `trim_args` is handed; this story keeps the fifteen
> a linear chain composes, all of which shipped in slice B. **9.3 is complete except for those
> five, and they are blocked on 9.7 rather than on any work here.**

**Acceptance Criteria:**

**Given** the Texture family
**When** its catalogue is offered
**Then** it provides at minimum grain, vignette, soft-focus diffusion and banding suppression (FX-9)
**And** bloom/halation is delivered by Story 9.7, which gives the chain the branch it needs.

**Given** the Stylize family
**When** its catalogue is offered
**Then** it provides at minimum RGB/chroma split and posterize (FX-10)
**And** pixel shuffle/sort, edge treatment and the scanline/CRT look are delivered by Story 9.7
**And** every Stylize effect is off by default and none is applied implicitly by any other family.

**Given** the Geometry family
**When** its catalogue is offered
**Then** it provides at minimum punch-in, handheld shake, dutch tilt and mirror/flip (FX-11)
**And** slow zoom is delivered by Story 9.7, which gives the chain the clip duration it needs
**And** a Geometry effect never changes the Shot's frame count, its window, or its position on the timeline
**And** geometry that would sample outside the source frame is bounded so it cannot expose an undefined edge.

**Given** the chain builder composing any combination of families
**When** the filter chain is produced
**Then** the stage order is exactly `trim, GEOMETRY, scale, TEXTURE, GRADE, STYLIZE, pad, fps, setsar, format` (AD-17)
**And** a test asserts geometry precedes `scale`, so a punch-in samples the take's own pixels
**And** a test asserts every treatment precedes `pad`, by sampling the letterbox bar of a padded export and requiring it to be pure black — measured 2026-08-21, texture after `pad` leaves it at RGB (1,1,5) and before `pad` at (0,0,0) (FX-9, AD-17).

**Given** any effect chain
**When** it is built
**Then** it is asserted in tests by string comparison, in the same way `assembly.py` already pins its argv (FX-NFR-5).

### Story 9.4: Stack Editing, Reordering and Disabling

As the Director,
I want to reorder my effects and switch one off without losing it,
So that I can experiment without rebuilding a stack I spent time on.

**Acceptance Criteria:**

**Given** a Shot with several Effects
**When** the Director reorders them
**Then** reordering works by drag on the card handle and by `Alt+Up` / `Alt+Down` when a card is focused (FX-5, UX-DR15)
**And** an order the render chain forbids is not offered — the picker and drop targets respect family ordering, so an illegal order cannot be expressed rather than being expressed and then rejected (FX-5).

**Given** an Effect Stack stored out of family order — by a copy, a hand edit, or an older client
**When** the chain is built
**Then** the builder sorts the stack by family before composing and preserves the Director's order within each family, so storage order is never load-bearing (AD-31).

**Given** an Effect the Director wants to silence
**When** it is disabled
**Then** it is retained with all its parameters and contributes nothing to preview or export (FX-5)
**And** the card drops to 45% opacity with its controls still readable (UX-DR3).

**Given** a Shot whose every Effect is removed
**When** the stack is empty
**Then** the Shot returns to the empty state and not to a residual one, and builds the byte-identical export **command** it builds today (FX-5, FX-23, R-20 — the command, never the encoded file).

### Story 9.5: Copy a Look Across the Video

As the Director,
I want to apply one Shot's look to every other Shot,
So that forty independently generated clips read as one film.

**Acceptance Criteria:**

**Given** a Shot with an Effect Stack the Director is happy with
**When** `Copy stack to…` is used
**Then** the target set is explicit — named Shots or the current Section, never a bare "all" (FX-6, UX-DR14)
**And** the control states what will happen before it runs, naming replacement rather than merge
**And** the report names the count applied and every Shot refused, by ID (FX-6).

**Given** a locked Shot in the target set
**When** the copy runs
**Then** that Shot is refused by name and left unchanged (FX-6, FX-7).

**Given** a locked Shot
**When** its Effects tab is opened
**Then** every writing control is disabled, the panel states the lock as the reason, and the stack remains readable (FX-7)
**And** unlocking restores editing with the stack intact.

**Given** a completed clip on the timeline carrying Effects
**When** the timeline is scanned
**Then** the clip shows a Consolas effects corner chip in the existing 14px idiom, reading order approved-effects-flagged (UX-DR9)
**And** the chip is a glyph and not a tint, so the state is never colour-alone (UX-DR15).

### Story 9.6: Honest Export with Effects

As the Director,
I want an export to refuse loudly rather than quietly drop an effect I configured,
So that what I see is what ships, and a six-month-old project still tells me what it was built from.

**Acceptance Criteria:**

**Given** an Effect that cannot be applied — a missing LUT file, an unresolvable binding, a transition type not valid for its boundary
**When** an export is requested
**Then** it refuses and names the Shot and the reason, in one report covering every such problem rather than one at a time (FX-24)
**And** an export never silently drops an Effect the Director configured
**And** the report is built as an extensible list of checks, so the binding case (Epic 10) and the transition case (Epic 11) register into it later without reshaping it — this story ships the missing-LUT case and the report itself, and is complete without either later epic.

**Given** a completed export
**When** its provenance is read
**Then** it records the Effect and Transition state it was built from (FX-25)
**And** an export made before this feature existed is reported as carrying no Effects, not as carrying unknown ones.

**Given** a Project with Effects on some Shots and none on others
**When** it is exported
**Then** clips carrying no Effects are not re-encoded a second time on account of anything elsewhere in the timeline (FX-NFR-2)
**And** export wall-clock for a Project with no Effects is unchanged from today's, measured rather than asserted.

**Given** a Project manifest
**When** it is read on another machine or after a reload
**Then** everything needed to reproduce the export's look is present in it, and nothing about an Effect lives only in the interface (FX-23).

### Story 9.7: What the Chain Is Handed

As the Director,
I want a slow zoom, a bloom, a scanline pass and a shake that does not stutter,
So that the looks the product promised are actually available and the ones I have stop breaking at a seam.

> **Split out of Story 9.3 on 2026-08-25.** Two problems, found separately, share one root: the
> chain is a single comma-joined **linear** graph, and it is handed neither a **branch** nor the
> clip's **offset and duration**. Five promised effects need one or the other, and a shot that
> becomes two clips replays its own motion. Solving them apart means opening the same splice twice.
> **Story 9.3's remaining five effects are blocked on this story**, which is why this one is
> numbered after it and sequenced before it.

**Acceptance Criteria:**

**Given** the chain builder
**When** it composes a stage that needs more than one input
**Then** it can express a branched filtergraph — `split` into a treated and an untreated leg, recombined by `blend` — without any composer reaching outside the two splice points `trim_args` already exposes (AD-17, AD-25)
**And** `effects.py` still imports nothing but the standard library, and `assembly.py` still does not import it back.

**Given** a clip being composed
**When** the chain is built for it
**Then** the composer is handed that clip's **offset within its Shot** and its **duration**, so a stage can be a function of where the clip sits rather than only of the effect's values
**And** a Shot with no effects still builds the byte-identical command it builds today (FX-5, R-20).

**Given** a Shot that `assembly_plan` resolved into two or more clips, because a later Shot nests inside it
**When** a time-dependent effect is composed for it
**Then** the motion is continuous across the seam — `handheld_shake` does not snap back to phase zero and `grain` does not replay an identical noise sequence — because each clip's stage is offset by where that clip begins in the Shot
**And** a test asserts the two clips' stage text differs by exactly that offset, rather than being byte-identical as it is today.

**Given** the Texture family
**When** its catalogue is offered
**Then** it provides bloom/halation, completing FX-9's stated minimum.

**Given** the Stylize family
**When** its catalogue is offered
**Then** it provides pixel shuffle or sort, edge treatment and a scanline/CRT look, completing FX-10's stated minimum
**And** every one of them is off by default and none is applied implicitly by any other family.

**Given** the Geometry family
**When** its catalogue is offered
**Then** it provides slow zoom, completing FX-11's stated minimum
**And** it never changes the Shot's frame count, its window, or its position on the timeline
**And** geometry that would sample outside the source frame is bounded so it cannot expose an undefined edge.

**Given** any branched chain
**When** it is built
**Then** it is asserted in tests by string comparison, as every other chain in this application is (FX-NFR-5)
**And** the assembled video still matches the song to within one frame, for every combination of effects.

## Epic 10: The Picture Moves With the Music

The Director ties a parameter to a frequency band and the video answers the track. Builds on Epics 8 and 9; requires neither Epic 11 nor anything later.

### Story 10.1: Bind a Parameter to a Band

As the Director,
I want any effect parameter to be driven by the music instead of held at a number,
So that the video moves with the track without my animating anything.

**Acceptance Criteria:**

**Given** any parameter of any Effect in any family
**When** its bind glyph is clicked
**Then** a band panel opens inline beneath that row, with a `--blue` left edge marking it as reactive (FX-12, UX-DR5)
**And** `ParameterBinding` is added to the model as the only entity this story creates, defaulted so every existing manifest loads unchanged, and written only by the dedicated binding route (AD-16)
**And** only one band panel is open at a time; opening another closes the first
**And** no parameter is specially privileged and none is excluded by category (FX-12).

**Given** a parameter being bound
**When** the binding is configured
**Then** the Drive is `punch` or `sustain`, chosen explicitly with neither preselected, because nothing infers a drive mode (FX-14)
**And** a Trigger Floor is settable, below which the Drive contributes nothing
**And** Depth is settable and bounded so a binding cannot drive a parameter outside its own declared range
**And** the parameter's manual value becomes its resting value, and the binding moves it from there by Depth (FX-12).

**Given** `punch` drive on a heavily limited master
**When** the drive is computed
**Then** it responds to transients rather than absolute level — measured as level above its own running average — so it flashes on hits rather than sitting pinned high (FX-14)
**And** `sustain` engages only after its band holds above a level for a hold time, and survives dips for a sustain time.

**Given** a binding
**When** the render input is generated
**Then** it compiles to a `sendcmd` commands file produced by a pure function of (Song Envelope, binding, Shot window), asserted in tests by string comparison exactly as ffmpeg argv already is (AD-22, FX-NFR-5)
**And** the file is passed to ffmpeg as a **bare relative filename with the process cwd set to its directory** — an absolute Windows path's drive-letter colon parses as a filter option separator and fails naming a filter that is not the problem (AD-22)
**And** there is exactly one mechanism; no expression-based second path is introduced (FX-NFR-3).

**Given** a bound parameter
**When** the binding is removed
**Then** the parameter returns to its resting value with no residue (FX-12)
**And** a Shot with no bindings exports identically to one where the feature does not exist.

### Story 10.2: Choose the Band Against the Song's Spectrum

As the Director,
I want to pick a frequency band by seeing it on my song's own spectrum,
So that a band is something I look at rather than three numbers I guess at.

**Acceptance Criteria:**

**Given** an open band panel
**When** the spectrum strip is drawn
**Then** it renders the song's **whole-song** per-band average as `--dim` bars, identical in every Shot's panel, with the selected Band drawn over it as a `--blue` region whose edges fall off according to softness (FX-13, UX-DR6, AD-26)
**And** per-Shot spectra are not stored or drawn, so a binding copied to another Shot keeps its meaning.

**Given** the spectrum strip
**When** the Director adjusts the Band
**Then** dragging the region body moves centre, dragging its edges sets width, and a handle sets softness (FX-13)
**And** two Effects on the same Shot may listen to different Bands independently.

**Given** a Director working without a mouse or with a screen reader
**When** the band panel is used
**Then** centre, width and softness are also exposed as three labelled numeric inputs, arrow-key adjustable and readable, as the canvas's equivalent (UX-DR6, UX-DR15).

### Story 10.3: See What Is Driving

As the Director,
I want to see the envelope that is moving a parameter,
So that I can tell whether it is firing on the hits or flickering on noise.

**Acceptance Criteria:**

**Given** a Shot carrying at least one Parameter Binding
**When** it is selected
**Then** a Drive readout is drawn immediately beneath the Monitor, spanning the Shot's window (FX-22, UX-DR7)
**And** the envelope is drawn in `--blue`, the Trigger Floor as a `--dim` hairline, and the existing `--acid` playhead through it, so envelope and picture read against one time axis
**And** where the envelope falls below the floor it draws `--dim`, so a silenced passage looks silenced rather than merely low
**And** the signal drawn is the same one the export will use, not an illustration of one (FX-22, FX-NFR-3).

**Given** a Shot with no bindings
**When** it is selected
**Then** the readout is absent, not empty (FX-22).

**Given** a screen reader
**When** the readout is encountered
**Then** the canvas is `aria-hidden` and the facts it conveys — where the drive peaks, and whether it fires at all — are also stated in text on the band panel (UX-DR7, UX-DR15).

### Story 10.4: Bindings Survive a Song Change

As the Director,
I want a song change to disable my bindings honestly rather than delete them,
So that re-analyzing brings my work back instead of my having to rebuild it.

**Acceptance Criteria:**

**Given** a Project with no Song Envelope
**When** the Director clicks a bind glyph
**Then** the glyph is present but inert, and clicking it opens a one-line refusal naming the reason with an action — `No song analysis yet — bands need it. [Analyze song]` (FX-15, UX-DR12)
**And** the glyph is never hidden, because a hidden control teaches nothing about what the product can do.

**Given** a Project whose Song has been replaced, invalidating the envelope
**When** its Shots' bindings are read
**Then** the bindings are **retained and reported unresolvable**, never dropped and never silently zeroed (FX-15)
**And** re-analyzing the Song makes them live again with their stored values intact.

**Given** an unresolvable binding
**When** an export is requested
**Then** the export refuses, naming the Shot and the reason alongside every other such problem in one report (FX-24, FX-15).

## Epic 11: Cuts That Blend

Two clips dragged together become a transition. The only epic touching `assembly_plan` and the cumulative frame grid — it merges alone, behind its own verification pass.

### Story 11.1: An Overlap Becomes a Transition

As the Director,
I want two overlapping clips to blend rather than hard-cut,
So that a cut the song does not accent stops jarring.

**Acceptance Criteria:**

**Given** two Shots whose windows overlap and a Transition type set on the boundary
**When** the project is assembled
**Then** `assembly_plan` emits three entries — A truncated to the Overlap's start, a transition segment, and B from the Overlap's end (AD-18)
**And** the transition segment is rendered by its own pinned argv from A's and B's overlapping frames through `xfade`, at the same normalized geometry, rate, SAR and pixel format as every other intermediate
**And** the concat list joins them with `-c:v copy` unchanged, so no clip is re-encoded a second time (FX-NFR-2, AD-18).

**Given** any combination of Overlaps and Transitions
**When** the export is verified
**Then** the assembled file's duration matches the Song within one frame (FX-NFR-1)
**And** the transition segment's frame count is exactly the Overlap's frames on the existing cumulative grid, with `clip_frames_on_grid` unmodified
**And** the existing grid assertions pass unchanged and gain cases for: no overlap, one overlap, adjacent overlaps, an overlap at the song's start, an overlap at its end, and a one-sided transition beside a paired one (FX-NFR-1).

**Given** an Overlap with no Transition type set
**When** the project is assembled
**Then** it resolves exactly as it does today — a hard cut, later Shot on top (FX-16).

**Given** the transition catalogue
**When** it is offered
**Then** it is a curated subset named in the Director's language, covering at minimum dissolve, fade through black, fade through white, blur, and directional wipes and slides (FX-19)
**And** an entry that is pair-only appears in the list and refuses one-sided use with its reason, rather than being silently absent.

**Given** more than two Shots overlapping at one point
**When** assembly runs
**Then** the case is refused with a stated reason rather than left undefined (FX-16).

**Given** a Transition has to be stored before it can be assembled
**When** the model is extended
**Then** `TransitionSpec` is added along with `Shot.transition_in` and `Shot.transition_out`, all defaulted so every existing manifest loads unchanged (AD-16)
**And** they are written only by a dedicated `.../shots/{shot_id}/transitions` route, and `replace_project` adopts them from the stored Shot via the same `_adopt_*` idiom that protects `effects` (AD-16)
**And** a test asserts a full-project PUT omitting the transition fields leaves them intact
**And** the route is sufficient to set and clear a Transition without any interface, so this story is completable and testable before Story 11.3 exists.

### Story 11.2: The Overlap on the Timeline

As the Director,
I want to see the transition on the timeline as a region I created by dragging,
So that its length is something I set by hand and can see.

**Acceptance Criteria:**

**Given** the design token set
**When** the overlap treatment is implemented
**Then** `--blue: #5b9bd5` is added as the sixth and final accent, reserved to transitions and reactive bindings, and DESIGN.md's "no new accent colors" anti-goal is amended in place with the argument for the exception (UX-DR1).

**Given** two clips dragged until they overlap, with a Transition type set
**When** the timeline is drawn
**Then** the overlapping region shows a `--blue` fill at 22% with 1px `--blue` top and bottom edges and a centred Consolas type label (FX-16, UX-DR8)
**And** the band draws **behind** clip content, so state borders and the corner chips stay fully legible on top of it
**And** the band is not a drag target, so the existing clip edges remain the only handles.

**Given** an Overlap with no Transition type set
**When** the timeline is drawn
**Then** it shows a `--line-strong` hatch and a Consolas `CUT` label, with no blue — an untyped overlap is still a hard cut and must not borrow the transition's treatment (UX-DR8).

**Given** the application's error states are outlines
**When** the overlap band is reviewed against them
**Then** it is a soft fill and carries its type as text, so it cannot be misread as an error and its state is never colour-alone (UX-DR8, UX-DR15).

**Given** an Overlap that is removed by dragging a clip away
**When** the timeline redraws
**Then** the band disappears and the Transition Pair's stored types are retained (FX-16).

### Story 11.3: The Transition Pair

As the Director,
I want setting one side of a blend to set the other,
So that the two Shots describing one transition can never disagree.

**Acceptance Criteria:**

**Given** the Effects tab on a Shot
**When** the transition controls are shown
**Then** two rows are presented, `Transition in` and `Transition out`, each selecting from the catalogue (FX-17, UX-DR10)
**And** where an Overlap exists the row carries a `--blue` left edge and the Overlap's length in Consolas.

**Given** Shot A overlapping Shot B
**When** the Director sets A's `Transition out`
**Then** B's `Transition in` is set to match, and the interface says so in the existing toast idiom, in the past tense, naming both Shots (FX-17, UX-DR12)
**And** the reverse holds when B's `Transition in` is set first
**And** a Transition Pair across an Overlap can never hold two different types.

**Given** a manifest whose pair disagrees — hand-edited, or a partially applied write
**When** the project is exported
**Then** the outgoing Shot's `transition_out` is authoritative and is used, and the divergence is reported once (AD-30)
**And** the export is not refused, so an editable manifest cannot produce an undecidable export.

### Story 11.4: One-Sided Transitions

As the Director,
I want a transition out on a clip with nothing to blend into to still do something deliberate,
So that a blur-out before a hard cut is an editorial choice rather than a broken pair.

**Acceptance Criteria:**

**Given** a Shot with a Transition set and no Overlap on that boundary
**When** the project is assembled
**Then** the transition treats that clip's own final frames — a blur out blurs, a fade out fades — followed by a hard cut, applied as a single-input filter on the clip's own intermediate with no `xfade` (FX-18, AD-19)
**And** the named type is honoured and never quietly substituted
**And** it consumes no timeline length, borrows no frames from a neighbour, and changes no Shot's window or frame count (FX-18, FX-NFR-1).

**Given** the transition rows on a Shot with no Overlap
**When** they are shown
**Then** the row carries a `--dim` left edge and states what will actually happen — `No overlap — this treats shot 04's last frames, then cuts.` (FX-18, UX-DR10)
**And** the control is live rather than disabled
**And** its length is bounded by the Shot's own duration and by nothing invisible.

### Story 11.5: Preview a Transition

As the Director,
I want to see the blend before I export,
So that I choose a transition by looking at it rather than by its name.

**Acceptance Criteria:**

**Given** a Shot with a Transition, paired or one-sided
**When** the boundary is previewed
**Then** the Preview Clip covers a window spanning the boundary, so the outgoing Shot, the Transition and the incoming Shot play as one continuous piece (FX-21)
**And** the Transition previewed is the same one the export will build, by name and by duration (FX-NFR-3).

**Given** a one-sided Transition
**When** it is previewed
**Then** it previews as what it actually is — the treated frames followed by a hard cut (FX-21, FX-18).

**Given** the Overlap being lengthened by dragging
**When** the preview updates
**Then** its Transition length follows the Overlap, and the transition row's length readout follows with it (FX-21, FX-16).
