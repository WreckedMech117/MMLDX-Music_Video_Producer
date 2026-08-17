---
title: "Addendum: Music Video Producer Brief"
status: draft
created: 2026-08-16
updated: 2026-08-16
---

# Addendum

Depth that belongs to downstream documents (PRD, architecture, epics) rather than the brief.

## The wizard / editor tension — for architecture and UX

The Director asked for "an easy to use *wizard*-like workflow in the beginning" that becomes "editable, tunable shot by shot" with a "professional functional GUI."

`docs/ARCHITECTURE.md` currently states the opposite in one respect:

> The interface is an **Operate / Command-Inspect** editor, not a dashboard.

and the implementation plan reinforces it:

> No landing-page hero or decorative dashboard metrics.

These are not necessarily in conflict, but they will become so if a wizard is built as a second UI. The reconciliation worth carrying into architecture:

- The original decision rejects *decorative* surfaces — hero panels, vanity metrics. It does not reject *sequencing*.
- A wizard that composes the five existing workspaces in order (Song → Treatment → Assets → Timeline → Queue), driven by real project state, honors both. Each step is the real workspace, scoped and ordered, not a parallel implementation.
- The wizard should be derived from project state rather than stored progress: what step you are on is a function of whether a song exists, whether a treatment exists, whether characters are approved. This makes it resumable for free and impossible to desynchronize.
- It must be escapable at any point, and it should not reappear for a project that has moved past it.

Recommendation for the architect: treat this as a routing and empty-state concern over the existing workspaces, not a new surface. Decide explicitly, and update `ARCHITECTURE.md` either way — a standing decision contradicted in practice is worse than either choice.

## Verification evidence — 2026-08-16

Captured because the brief's claims rest on it.

Pre-flight against live ComfyUI 0.33.1: thirteen node classes and five model files confirmed present before any GPU time was spent.

The run, submitted from the application:

| Field | Value |
|---|---|
| Window | 3.75 s, 90 frames (exactly on the 17k+5 grid: 5·17+5) |
| Resolution / steps | 640×384, 4 steps, seed 12345 |
| Submission | HTTP 202, prompt ID assigned, graph accepted by ComfyUI validation |
| Output | h264 640×384, 90 frames, 3.750 s, AAC 32 kHz, 850 KB |
| Wall clock | ~12 minutes, dominated by loading the ~31 GB model stack |
| VRAM | 32 GB free → 1.1 GB free during sampling |
| Job state | reconciled to `complete`; `latest_output` written; `approved_output` correctly empty |

Defects observed during the run:

1. **Running renders report `queued`.** Job refresh reads `/history/<prompt-id>`, which has no entry until a prompt finishes. The whole twelve-minute execution appeared pending. Fix by reconciling against `/queue` as well. This is a user-visible honesty problem in a product whose stated value is honest status.
2. **Mixed path separators in stored outputs.** `VHS_VideoCombine` returns a Windows subfolder with backslashes; the app joins it to the filename with a forward slash. ComfyUI's `/view` tolerates it and previews resolve, so this is cosmetic — but it will bite anything that parses those paths.
3. **`aligned_frames` is computed and unused.** `build_director_timeline()` produces `DirectorTimeline.aligned_frames`, but `build_h3_director_payload()` sends `requested_frames`. The verified window was deliberately on-grid, so off-grid behavior is unknown. Either send the aligned value or delete the field.

## Scale arithmetic — for planning

The verified shot was 3.75 s at 640×384 and 4 steps. A three-minute song at 4–15 s per shot is roughly 12–45 shots.

Unknowns that only a real song will answer:

- Whether the model stack stays resident between queued shots, or reloads each time. The twelve-minute wall clock was dominated by loading; if it reloads per shot, a song is impractical and warm-batching becomes a hard requirement rather than an optimization.
- Storage per production at delivery resolution, across multiple takes per shot.
- Whether the Queue workspace and manifest remain usable with dozens of jobs and takes.

This arithmetic should drive the first scale story, and the batching question should be answered by measurement before any optimization is designed.

## Deferred decision: finishing chain

LTX 2.5 enhancement, SeedVR2, FILM interpolation, and RTX VSR are designed but unproven. One real failure is already diagnosed and patched in an audited reference: SeedVR2 emitted 1250×720, and LTX's VAE rejected width 1250; a KJ resize with `divisible_by=16` normalizes to 1248×720.

The blocker is not that failure. It is that the combined exports regenerate H3 from creator-specific media instead of accepting an already-approved take. A standalone adapter that takes an approved take as input is the prerequisite for putting finishing back in scope.

Decision rule proposed in the brief: the first complete video ships without upscaling if that standalone adapter is not proven in time. Enhancement is a quality multiplier on a finished pipeline, not a precondition for one.

## Rejected framings

- **"A tool for other creators" as the primary audience** — rejected. Building for a general audience before producing a single video would optimize for imagined users over the real one.
- **Client / commercial work** — rejected for this version. It would pull in delivery, review, and licensing concerns that have nothing to do with getting one video finished.
- **Positioning against cloud AI video tools as a competitor** — not pursued. The honest position is a different category (owned production tool) rather than a better version of the same thing.
