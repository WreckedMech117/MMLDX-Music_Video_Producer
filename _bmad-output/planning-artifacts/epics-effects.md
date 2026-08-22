---
stepsCompleted: [1]
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

## Requirements Inventory

### Functional Requirements

FX-1: Analyze a Song into a Song Envelope — RMS, peak, spectral-flux proxy, per-band envelopes, onsets, beats, estimated BPM; cached, invalidated on song change, never blocking
FX-2: Show beat and onset markers against the waveform; display only, toggleable
FX-3: Snap Shot-boundary edits to beats alongside lyric and phrase boundaries; always an assist, never a constraint
FX-4: Two tabs in the shot inspector — Shot Info (unchanged) and Effects — with tab selection surviving background rebuilds
FX-5: Build a Shot's Effect Stack — add, remove, reorder, individually disable; empty stack exports byte-identically to today
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
FX-NFR-4: No new runtime dependency — everything through the ffmpeg binary already required and the language already in use
FX-NFR-5: Generated render inputs are pure and comparable — same project, same bytes, asserted by string comparison
FX-NFR-6: Preview stays inside a measured budget — under one second from change to looping clip for a Shot of typical length

### Additional Requirements

*From the architecture spine (AD-16…AD-31) and its inherited constraints.*

- Effects and transition fields live on `Shot` but are written **only** by dedicated routes; `replace_project` adopts them from the stored Shot via the established `_adopt_*` idiom (AD-16). That route's own comments record this hole found six times
- One pure chain builder with a fixed family order: `trim → GEOMETRY → scale → TEXTURE → GRADE → STYLIZE → pad → fps → setsar → format` (AD-17). Measured 2026-08-21: texture after `pad` leaves the letterbox bar at RGB (1,1,5); before `pad`, (0,0,0)
- A transition is baked into its own concat intermediate so the join keeps `-c:v copy`; `clip_frames_on_grid` is not modified (AD-18)
- The Overlap is the only transition geometry — no stored duration, no borrowing from the over-render margin, which external clips do not have (AD-19)
- The Song Envelope is a sidecar file; the manifest carries only a pointer, the rate, the band count, the BPM, and the song fingerprint (AD-20). Measured: manifests are 110–190 KB, an envelope is ~750 KB, and the manifest rides a 2-second poll
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
