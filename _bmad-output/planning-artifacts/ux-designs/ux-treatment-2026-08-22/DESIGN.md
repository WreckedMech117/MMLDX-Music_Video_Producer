---
title: "Treatment Planning — Visual Design"
status: final
created: 2026-08-22
updated: 2026-08-22
sources:
  - ../../prds/prd-MusicVideoProducer-treatment-2026-08-22/prd.md
  - ../../treatment-planning-findings-and-rulings-2026-08-22.md
  - ../ux-mvp-2026-08-16/DESIGN.md
  - ../ux-effects-2026-08-21/DESIGN.md
---

# DESIGN — Visual Identity for Treatment Planning

Settled with the Director on 2026-08-22. An **extension** of `../ux-mvp-2026-08-16/DESIGN.md` and `../ux-effects-2026-08-21/DESIGN.md`, both of which remain authoritative for everything they cover. The standing rule holds: **extend the existing visual language, never redesign it.**

## 1. No new accent, and why that is settled rather than merely observed

The palette is closed. Six accents, each with one meaning:

| Token | Means | Spoken for by |
|---|---|---|
| `--acid` | complete · primary action | MVP |
| `--amber` | running · caution | MVP |
| `--red` / `--red-edge` | error · destructive · flagged | MVP |
| `--cyan` | approved | MVP |
| `--blue` | transitions · reactive bindings | Effects (declared the sixth and **final** accent) |
| `--dim` / `--muted` | inert · secondary | MVP |

This feature introduces **no seventh**. That is not restraint for its own sake — it is forced by the one-state-one-colour law. Every candidate meaning this feature needs (*the machine wrote this*, *planning is on*, *this proposal may be stale*) would have to either invent a colour or overload one that already means something, and overloading is how `--cyan` would come to mean both "approved" and "written by ProducerBot" on the same screen.

**Attribution therefore uses surface and rule, not hue** (§3). Where a state genuinely maps onto an existing meaning, it uses that colour honestly: a stale proposal is a **caution**, so it is `--amber`; planning mode is a **write happening without per-turn consent**, which is also a caution, so its indicator dot is `--amber`.

## 2. Inherited tokens, used unchanged

All MVP and Effects tokens. Typography unchanged: Bahnschrift/system UI at 14 px base, **Consolas micro-labels** (9 px, letter-spaced, uppercase) for mode names, proposal kinds, timestamps and origins.

## 3. Attribution — the one genuinely new visual idea

The Director chose **in-place, permanent attribution**: assistant-written text is marked where it sits and stays marked, rather than being summarised in the thread or listed in a side rail.

**The treatment is neutral, not coloured.**

| Element | Treatment |
|---|---|
| Assistant-written range | `--surface-1` wash behind the text, and a 2 px `--line-strong` rule down its left edge |
| Director-written text | no treatment at all — the default state is *yours*, and only the machine's contribution is marked |
| Range hover / focus | rule brightens to `--muted`; a Consolas micro-label names the turn that wrote it |
| A range the Director has since edited | treatment is **removed** — once you have touched it, it is yours |

Neutral is the right register. A coloured wash would read as a *state* — running, error, approved — and this is not a state, it is a **provenance**. Provenance is quieter than state, always.

**State is never colour-alone** (`EXPERIENCE.md` § Accessibility floor): the left rule is a shape, the micro-label is text, and the treatment is legible with colour removed entirely.

## 4. New components

All composed from existing tokens.

1. **Planning bar** — a slim strip across the top of the document column, present for as long as Planning Mode is on. **Reuses the wizard guidance-banner shape exactly**: `#0e1010` chrome background, `--line` bottom border, one sentence of what this mode means, controls right-aligned. Leading `--amber` dot marks it as a caution state. Carries `[Exit planning]` as a quiet button. It sits over the **documents**, not beside the chat, because the documents are what is at risk.

2. **Brief contract line** — a single `--muted` sentence under the Brief's existing "Creative brief" label, stating that this is the source document the Treatment and Style Bible are generated from. Not a tooltip; not a placeholder that vanishes when you type.

3. **Proposal card** — one per Asset Proposal in the Suggested Assets tab. `--surface-1` on `--radius`, 1 px `--line`. Consolas kind micro-label, name in `--ink`, prompt in `--muted` at 12 px. Two footer rows: the **origin** (`--dim`, the Brief passage that called for it, truncated with the full text on hover) and the actions.

4. **Staleness flag** — an `--amber` left edge on a proposal card plus a Consolas `MAY BE STALE` micro-label and one sentence naming what changed. Never `--red`: a stale proposal is not an error, and the bus depot may well survive the rewrite.

5. **Indeterminate pass indicator** — for Suggest Video and any long pass. An `--amber` pulsing dot and elapsed time in Consolas. **No percentage, no bar, no estimate** — the standing honesty rule that bars fake render progress applies identically, and this pass has 26× variance in how long it takes. Respects `prefers-reduced-motion` with a static dot.

6. **Proceed control** — a `--acid` primary button at the foot of the Song, Treatment and Assets workspaces, naming the next phase (`Build treatment →`, `Gather assets →`, `Lay out the timeline →`). Where the boundary carries an offer, the offer renders as an inline `--surface-1` block above the button — never a modal.

7. **Document tab strip, extended** — the existing `document-tabs` gains a fourth entry, `Assets`, in the same style. It carries a count chip when proposals exist and an `--amber` dot when any of them is flagged stale.

## 5. Do's and don'ts

**Do**

- Mark only what the machine wrote. The Director's own text is the unmarked default.
- Reuse the wizard guidance-banner shape for the Planning bar rather than inventing a second banner.
- Keep every Consolas micro-label uppercase and letter-spaced, including the new mode, kind and origin labels.
- Use `--amber` for staleness and for planning-mode, because both are honestly cautions.

**Don't**

- Introduce a seventh accent. See §1; this is closed.
- Colour the attribution wash. Provenance is not a state.
- Show a percentage, a bar, or a time estimate for any language-model pass.
- Put the Planning bar next to the chat. It belongs over the documents it puts at risk.
- Add a modal. The pre-flight remains the application's only one; every offer in this feature is inline.
- Mark a stale proposal in `--red`. It is a caution, not a failure, and nothing is broken.
