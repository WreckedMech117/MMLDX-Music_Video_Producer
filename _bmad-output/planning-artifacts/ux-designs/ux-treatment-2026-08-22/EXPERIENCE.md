---
title: "Treatment Planning — Experience Design"
status: final
created: 2026-08-22
updated: 2026-08-22
sources:
  - ../../prds/prd-MusicVideoProducer-treatment-2026-08-22/prd.md
  - ../../prds/prd-MusicVideoProducer-treatment-2026-08-22/addendum.md
  - ../../treatment-planning-findings-and-rulings-2026-08-22.md
  - ../ux-mvp-2026-08-16/EXPERIENCE.md
  - ../ux-effects-2026-08-21/EXPERIENCE.md
---

# EXPERIENCE — Information Architecture, Behavior, Interaction

Settled with the Director on 2026-08-22. Extends the MVP and Effects spines, which remain authoritative for everything they cover. Mirrors the PRD's `UJ-7..9` and `TP-` IDs verbatim. Visual tokens resolve against `DESIGN.md` in this folder.

## Foundation

Desktop web, single operator, one window. Dependency-free ES modules, no CDN, no build step, native Web Audio and Canvas only. No UI system — the house language in `styles.css` *is* the system. This feature adds **no workspace and no modal**; the pre-flight remains the application's only modal.

## Information architecture

Unchanged: topbar · left rail `01 Song / 02 Treatment / 03 Assets / 04 Timeline / 05 Queue` · one workspace.

Existing surfaces gain responsibility; **nothing new is created**:

| Surface | Gains |
|---|---|
| Treatment · document column | A **Planning bar** above it; a fourth tab, `Assets`; a contract line on the Brief; a lock/restore row for the Brief in the existing `data-doc-controls` pattern |
| Treatment · Brief editor | In-place attribution of assistant-written ranges, and session undo |
| Treatment · chat thread | Planning Turns, including question-only turns |
| Song workspace | A `Suggest video` entry point is **not** here — Suggest Video lives in Treatment; the Song page gains only the proceed control and its analysis offer |
| Song / Treatment / Assets | A proceed control at the foot, each naming its next phase |

**Already there, and reused rather than rebuilt:** the `document-tabs` strip, the `data-doc-controls` scoped row pattern, the chat thread and composer, the `Analyze structure` control, and the `Build treatment →` button.

## Voice and tone

The house voice, with three rules this feature leans on hardest:

- **Say what a mode means, not what it is called.** The Planning bar reads *"the Brief is being edited live"*, not *"planning mode active"*.
- **An automatic change announces itself in the past tense, naming what it touched.** *"Added two sentences about the brother."*
- **Never imply precision the system does not have.** A long pass reports elapsed time and nothing else — no percentage, no estimate, no "almost done".
- Refusals name the thing and the reason and offer the next action: *"The song has no lyrics yet — Suggest Video needs them. [Go to Song]"*

## Component patterns

### Planning Mode (TP-6)

- Entered and left explicitly. While on, the **Planning bar** sits above the document column stating what the mode means and offering `[Exit planning]` (`DESIGN.md` §4.1).
- Entering states the trade in one sentence: the Brief will be edited live, without the per-turn tick.
- The composer's `Apply document changes` checkbox is **disabled and visibly superseded** while planning is on — the control that has been suspended must not sit there looking operable.
- Leaving restores per-turn consent, unticked. Consent never survives leaving, a project change, or a reload.

### Attribution in the Brief (TP-8)

The Director chose **in-place and permanent**: assistant-written text is marked where it sits.

- An assistant-written range carries a `{colors.surface-1}` wash and a 2 px `{colors.line-strong}` left rule. Director-written text carries nothing — **the unmarked default is yours**.
- Hovering or focusing a range names the turn that wrote it in a Consolas micro-label.
- **Editing a range clears its mark.** Once the Director has touched it, it is theirs.
- Attribution survives reload; it is a property of the document, not of the session.

> **`[NOTE FOR ARCHITECTURE]` The mechanism is yours, not this spine's.** The Brief is a plain `<textarea>`, which cannot style a range of its own text. Two ways to deliver the behaviour above: a hand-rolled `contenteditable` editor, or a **mirror overlay** — a styled read-only div behind a transparent textarea sharing its font metrics, with ranges highlighted in the mirror. **The overlay is recommended**: it keeps native selection, paste, undo, IME and spellcheck, which a hand-rolled editor must all reimplement, and it is the smaller change by a wide margin. This spine specifies the behaviour and does not choose between them.

### Session undo (TP-9)

- Every Brief revision made during a Planning session can be stepped back through, in order, for the life of that session.
- The control names what it will undo before it does it — *"Step back: added two sentences about the brother"*.
- Stepping back **never removes anything from the chat thread**. What was said stands even when what was written is undone.
- The persisted recovery slot is the durable floor and survives reload; the session stack does not.

### Suggested Assets (TP-11 – TP-13)

A fourth tab in the existing `document-tabs` strip, carrying a count chip and an `{colors.amber}` dot when anything is flagged.

- Each **proposal card** shows kind, name, prompt, and its **origin** — the Brief passage that called for it (`DESIGN.md` §4.3).
- Actions per card: edit the prompt, delete, accept.
- A **stale** proposal carries an `{colors.amber}` left edge, a Consolas `MAY BE STALE` label, and one sentence naming what changed. It is never removed automatically and never blocks anything; accepting it is permitted.
- A proposal duplicating a library asset says so **before** acceptance, naming the asset.
- Accepting generates as one confirmed batch naming the count — the same confirmation TP-19's offer uses, not a second path.

### The long pass (TP-4, TP-NFR-4)

- An `{colors.amber}` pulsing dot and **elapsed time only**. No percentage, no bar, no estimate — this pass has 26× variance and any number would be a lie.
- The Director can abandon a pass in flight; abandoning leaves the Brief untouched and says so.
- A failure names its reason. **A timeout that stringifies to nothing must never surface as a blank** — it is reported as a timeout with its elapsed time.
- The rest of the application stays usable throughout.

### Proceed controls (TP-17 – TP-19)

One rule, both boundaries: **proceeding offers, it never does.**

- Each control names its next phase and states what is not yet ready rather than being silently disabled.
- Where a boundary carries an offer, the offer renders **inline above the button**, never as a modal.
- Declining always proceeds.

**Song → Treatment (TP-18).** Offers the song analysis when it has not been run, naming what it does and roughly what it costs. Declining proceeds normally; structure is never a precondition of proceeding or of Suggest Video.

> **One analysis moment, not two (R-17).** The effects work's Story 8.1 ships first and adds a second, different analysis of the same song — beats, onsets, per-band envelopes. This offer presents **one moment and one indicator** covering both computations. A Director must not sit through two passes back to back for a distinction they never asked about.

**Treatment → Assets (TP-19).** Offers to generate the accepted Suggested Assets, including character reference-sheet conversions.

### The Brief's contract (TP-2)

A single `{colors.muted}` sentence beneath the existing "Creative brief" label, stating that this is the source document Treatment and Style Bible are generated from. Persistent — not a placeholder that vanishes on first keystroke, and not a tooltip.

*(The PRD called the Brief unlabelled. It is labelled; what it lacked was the contract.)*

## State patterns

| State | Where | Treatment |
|---|---|---|
| Planning Mode on | Document column | Planning bar, amber dot, `[Exit planning]`; composer consent control disabled and superseded |
| Assistant-written text | Brief | Surface wash + left rule; micro-label on hover |
| Range edited by the Director | Brief | Mark cleared |
| Long pass running | Wherever it was started | Amber pulsing dot + elapsed time; abandon available |
| Long pass failed | Same | Reason named, document untouched; a timeout is named as a timeout |
| Long pass partial | Same | Reported as partial, never presented as finished |
| Proposal, current | Assets tab | Plain card with origin |
| Proposal, stale | Assets tab | Amber left edge, `MAY BE STALE`, reason; still acceptable |
| Proposal, duplicates an asset | Assets tab | Named before acceptance |
| Brief locked | Brief + Planning bar | Every automatic write refused by name; planning states it cannot write |
| Song not ready for Suggest Video | Treatment | Refusal names the missing field and offers where to fill it |

## Interaction primitives

- **Enter / exit Planning Mode** — explicit, from the Treatment workspace. Never entered by side effect.
- **Send a Planning Turn** — the existing composer, unchanged. A turn may return a question and write nothing.
- **Step back** — one control, naming what it will undo.
- **Accept / edit / delete a proposal** — per card; accept-all is an explicit batch with one confirmation.
- **Proceed** — one control per workspace; any offer is inline and declinable.
- **Abandon a pass** — available for the whole life of any long pass.

No new keyboard binding is added. Every control above is reachable by tab order and activated by `Enter`/`Space`.

## Accessibility floor

Inherits the MVP and Effects floors, and extends them:

- **State is never colour-alone.** Attribution is a rule plus a wash plus a hover label, legible with colour removed. Staleness carries a Consolas `MAY BE STALE` label beside its amber edge. The Planning bar carries a sentence, not just a dot.
- **The long pass is announced.** Its start, its failure and its completion are announced in a polite live region; elapsed time is *not* announced continuously, because a screen reader reading a ticking clock is noise rather than information.
- **The Planning bar is a status region**, associated with the document column it governs, so its state is available without hunting for it.
- **Attribution is reachable non-visually.** A screen reader can identify which passages were assistant-written; the mark is not a visual-only affordance.
- **Motion.** The pass indicator's pulse is the only new motion and respects `prefers-reduced-motion` with a static dot.
- **Focus survives the background reload.** The existing focus-preserving rebuild extends to the Planning bar, the fourth tab, and the Brief's editor including cursor position and selection.

## Key flows

### KF-4 — The Director turns a finished song into an idea (UJ-7)

1. Song page: presses `Build treatment →`. An inline offer appears — *analyse the song first?* — naming what it does.
2. Accepts. One indicator, one pass, covering structure **and** the effects envelope (R-17). Sections appear on the timeline row.
3. Treatment opens. The Brief is empty and its contract line says what belongs there.
4. Presses **Suggest Video**. Amber dot, elapsed time climbing, the rest of the application still usable.
5. **Climax:** a complete Brief arrives — premise, cast, locations, arc, look — entirely marked as assistant-written. Half of it is wrong in a way that tells the Director what right would be.
6. They enter Planning Mode to start correcting it.

**Edge case:** the pass times out at 4 minutes. The indicator reports a timeout with its elapsed time — not a blank — the Brief is untouched, and retry and write-by-hand are both offered.

### KF-5 — The Director refines the idea by talking about it (UJ-8)

1. Enters Planning Mode. The bar appears over the documents; the composer's consent checkbox greys out, visibly superseded.
2. *"she's a passenger, not the driver"* → the Brief updates in place, the changed range washed and ruled.
3. The assistant asks who is driving, and writes nothing. A question-only turn.
4. *"her brother, they don't speak"* → the Brief gains a second character; the new range is marked, the older one still is.
5. The Director rewrites one of the assistant's paragraphs by hand. **Its mark clears** — it is theirs now.
6. **Climax:** the Brief says something the Director recognises as their own, arrived at by being asked about it, and they can see exactly which half came from where.
7. They change their mind about the ending and step back twice, each step naming what it undoes. They exit Planning Mode.

**Edge case:** the Brief is locked. Entering Planning Mode states that it cannot write and offers to unlock; the conversation still runs and still asks questions.

### KF-6 — The Director finds out what the video needs before paying for it (UJ-9)

1. Opens the `Assets` tab. Proposals, each with its origin quoted from the Brief.
2. Two carry `MAY BE STALE` — their Brief passages changed when the ending did. One is now wrong and is deleted; one still applies and is kept.
3. A third is flagged as duplicating a library asset, by name, and is deleted.
4. Edits one prompt. Submits an existing photo for the singer; it is reported as a single view, not a multiview, and queued for conversion on confirmation.
5. Presses `Gather assets →`. An inline offer lists what will be generated and what it will cost.
6. **Climax:** one confirmation, and the batch covers exactly what the video needs and nothing it does not.

**Edge case:** the Director accepts nothing and proceeds. No GPU time is spent and the proposals stay in the list.

## Key-screen reference

[`mockups/treatment-planning-key-screen.html`](mockups/treatment-planning-key-screen.html) — the document column in Planning Mode with in-place attribution (assistant ranges washed and ruled, the Director's own paragraph unmarked), the fourth `Assets` tab with proposal origins and one stale flag, the long-pass indicator showing elapsed time only, and the Song → Treatment proceed offer rendered inline above its button.

Built from the live `styles.css` tokens with **no seventh accent introduced**. **The spines win on conflict with this or any other mock** — it is a reference for layout and density, not a specification.

## Open questions for the build

1. **How deep is session undo?** Carried from the PRD; a bound is architecture's to set. The control's wording does not depend on the answer.
2. **Does the attribution mark survive a Treatment/Style Bible regeneration?** Those documents are generated *from* the Brief, and generation does not write the Brief — so it should be untouched. Worth asserting rather than assuming.
3. **What does a Planning Turn show while it is running?** The long-pass indicator is specified for Suggest Video; a 20–40 s planning turn may want something lighter than the same treatment.

## Out of scope for this UX

Attribution on the Treatment or Style Bible · a revision history surface beyond session undo · proposal drag-ordering or grouping · any second chat surface · any modal.
