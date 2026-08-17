---
title: "Music Video Producer — Visual Design"
status: final
created: 2026-08-16
updated: 2026-08-16
---

# DESIGN — Visual Identity and Tokens

Settled conversationally with the Director on 2026-08-16. The rule for all MVP work: **extend the existing visual language, never redesign it.** Every token below already exists in `src/music_video_producer/web/assets/styles.css`; new UI must draw from this set before inventing anything.

## Token set (existing, authoritative)

| Token | Value | Role |
|---|---|---|
| `--bg` | `#0c0d0d` | Editor ground |
| `--ink` | `#f0f2ed` | Primary text |
| `--muted` / `--dim` | `#929a95` / `#626966` | Secondary / tertiary text |
| `--line` / `--line-strong` | `#303434` / `#454a49` | Hairlines, borders |
| `--acid` | `#d4f75e` | Active, complete, primary action |
| `--acid-ink` | `#15190a` | Text on acid |
| `--amber` | `#ffb454` | In progress, caution |
| `--red` | `#ff6b61` | Error, destructive, flag |
| `--cyan` | `#6bd0cc` | Approved, informational |
| `--radius` | `7px` | Corner radius everywhere |
| `--rail` / `--topbar` | `94px` / `58px` | Chrome dimensions |

Typography: system UI at 14px base; **Consolas monospace micro-labels** (9px, letter-spaced, uppercase) for indices, IDs, and table headers — this is the interface's signature and carries into all new components.

## State color semantics (binding)

One state, one color, everywhere it appears — clip, queue row, rail step, chip:

| State | Treatment |
|---|---|
| Pending / draft | `--dim` dashed or 1px outline, muted text |
| Running | `--amber` border, subtle pulse (`prefers-reduced-motion`: static amber, no pulse) |
| Complete | solid `--acid` border |
| Error | `--red` border + exact error text available |
| Flagged for regen | `--red` corner chip `⚑` on an otherwise-complete clip |
| Approved | `--cyan` corner chip `✓` on a complete clip |
| Wizard step done | `--acid` tick beside the rail index |

Flag and approve chips may coexist with the complete border; error replaces complete. No new colors are introduced for states — if a state seems to need a new color, the state model is wrong.

## New components (all composed from the tokens above)

1. **Wizard guidance banner** — slim strip at the top of the workspace: one sentence of what this step needs, `[Skip to editor]` quiet button, `[→]` primary. Border-bottom `--line`, background `#0e1010` matching topbar/rail chrome.
2. **Clip state chips** — 14px square corner chips on timeline clips: `⚑` red (flag), `✓` cyan (approve). Consolas glyphs, `--radius` 3px.
3. **Clip hover actions** — two micro-buttons revealed on hover of a completed clip (flag, approve), matching `.icon-button` styling.
4. **Pre-flight modal** — the only modal in the application. Title `RENDER BATCH` in Consolas micro-label style; body rows for shot count + time estimate, LM Studio warning block (amber left border), free VRAM line; `[Cancel]` quiet, `[Render N]` primary acid. Backdrop `rgba(0,0,0,.6)`.
5. **Safety notice block** — Director chat notices (document rejected, empty shot list, window flags) render inside an amber-left-bordered block, visually distinct from assistant prose, with a `raw output` disclosure.
6. **Missing-media placeholder** — dashed `--red` outline with Consolas `MISSING` label; never an empty/blank tile.

## Anti-goals

- No landing pages, hero panels, or vanity metrics (standing Operate/Command-Inspect decision).
- No new accent colors, gradients, or shadows; depth comes from the existing surface steps (`#0c0d0d` → `#0e1010` → `#171919`).
- No progress-percentage displays for renders — ComfyUI's queue does not provide honest per-prompt progress, and implying precision we don't have violates the product's honesty rule.
