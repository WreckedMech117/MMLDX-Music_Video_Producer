---
title: "Music Video Producer — Experience Design"
status: final
created: 2026-08-16
updated: 2026-08-16
---

# EXPERIENCE — Information Architecture, Behavior, Interaction

Settled with the Director on 2026-08-16. Mirrors the PRD's UJ IDs (UJ-1..3); FR references are to the PRD's global IDs.

## Information architecture

Unchanged: topbar (project, transport, ComfyUI state, save) · left rail `01 Song / 02 Treatment / 03 Assets / 04 Timeline / 05 Queue` · one workspace. The Production Wizard adds **no new surface**.

## The wizard is the rail (UJ-1, FR-1..3)

- The existing rail doubles as the wizard's step indicator. Completed steps show an acid tick beside their Consolas index; the derived current step gets the active treatment; future steps are dimmed **but remain clickable** — the wizard guides, it never locks.
- The current step is a pure function of the project manifest (no Song → `01`, Song but no Shots → `02`, …). Nothing is stored; reload lands in the right place by construction.
- A **guidance banner** at the top of the workspace states what the current step needs in one sentence, with `[Skip to editor]` and `[→]` next.
- Wizard Cast maps onto the Assets workspace scoped to character generation and Reference Sheet promotion; wizard Render maps onto Queue scoped to the pre-flight.
- Once a project has a completed render, the banner never appears again for it (FR-3). Skipping hides the banner for the session but leaves the ticks — they're honest state, not wizard chrome.
- Each step shows the **real workspace** (FR-2); the banner is the only wizard-specific element on screen.
- Edge case: ComfyUI offline at the Render step — the banner states it plainly and the pre-flight refuses to open; no fake progress (UJ-1 edge case).

## Live batch review (UJ-2, FR-4..9)

**Timeline clip state language** — border + corner chips per `DESIGN.md`:

- pending: dim outline · running: amber pulse · complete: acid · error: red · `⚑` red chip = flagged · `✓` cyan chip = approved.
- The rendered-so-far region reads as the contiguous acid run from the start; the amber clip is the frontier. Playback works through the acid region while amber/pending continue (FR-7).
- A completed clip landing on the timeline never interrupts current playback (NFR-1).

**Review interaction — one gesture per shot:**

- Hover a completed clip → `⚑` and `✓` micro-actions appear.
- Keyboard on selected clip: `F` flag · `A` approve (toggle) · `Space` play/pause from clip start · `←`/`→` previous/next clip.
- Flagging during an active batch persists on the Shot and touches nothing else (FR-8). A `Re-render N flagged` action arms only when the batch has drained; pressed earlier it explains why it must wait (FR-9).
- Queue rows mirror clip states with the same colors; a failed row shows the exact ComfyUI error inline (FR-6).

## Pre-flight modal (FR-4, FR-10, FR-11, FR-26)

Opens from `Render all` (Queue or wizard Render step). Sections, top to bottom:

1. **Plan readiness** — `N shots ready` or the blocking list: empty-prompt Shots by ID (blocks), near-duplicate pairs (warns) (FR-26).
2. **Time estimate** — `~{N×288s + 150s}` from measured warm/cold figures, labelled "estimate".
3. **GPU** — loaded LM Studio model named with amber warning; confirm triggers automatic unload (visible skip control), then re-reads and shows observed free VRAM (FR-10). No model loaded → section absent (FR-11). VRAM is context, never a gate.
4. **Actions** — `[Cancel]` / `[Render N]`. One confirmation covers the batch (FR-4).

## Safety notices (FR-15..17)

Director-chat notices render as the amber notice block, visually separate from assistant prose, with raw rejected output behind a disclosure. A rejected document never silently disappears from view — the notice names what was kept and why.

## Approval and assembly (UJ-2/3, FR-21, FR-22, FR-25)

- `✓` on a clip sets Approved Output; approving is always the Director's act — nothing auto-approves.
- The Assemble action lists blocking Shots (unapproved, stale-window, gaps/overlaps) by ID before it will run.
- Missing media renders the `MISSING` placeholder, never a blank tile (FR-25).

## Accessibility

- All clip actions reachable by keyboard (selection + `F`/`A`/`Space`); hover-only affordances always have a keyboard/inspector equivalent.
- Amber pulse respects `prefers-reduced-motion`.
- State is never color-alone: chips carry glyphs (`⚑`, `✓`), running clips show `▶`, errors show text.
- The pre-flight modal traps focus, restores it on close, and is labelled via `aria-labelledby`.

## Out of scope for MVP UX

Review-strip culling mode, thumbnail filmstrips, undo/redo surfaces, BPM lanes — all deferred with their PRD scope items.
