---
title: "Shot Effects and Transitions — Experience Design"
status: final
created: 2026-08-21
updated: 2026-08-21
sources:
  - ../../prds/prd-MusicVideoProducer-effects-2026-08-21/prd.md
  - ../../prds/prd-MusicVideoProducer-effects-2026-08-21/addendum.md
  - ../../effects-director-rulings-2026-08-21.md
  - ../ux-mvp-2026-08-16/EXPERIENCE.md
---

# EXPERIENCE — Information Architecture, Behavior, Interaction

Settled with the Director on 2026-08-21. Extends `../ux-mvp-2026-08-16/EXPERIENCE.md`, which remains authoritative for everything it covers. Mirrors the PRD's `UJ-4..6` and `FX-` IDs verbatim. Visual tokens referenced by name resolve against `DESIGN.md` in this folder.

## Foundation

Desktop web, single operator, one window. Dependency-free ES modules, no CDN, no build step, native Web Audio and Canvas only. No UI system — the house language in `styles.css` *is* the system.

The application is an **Operate / Command-Inspect editor**. This feature adds no workspace, no wizard step, and no modal. Everything below lands inside surfaces that already exist.

## Information architecture

Unchanged: topbar · left rail `01 Song / 02 Treatment / 03 Assets / 04 Timeline / 05 Queue` · one workspace.

Three existing surfaces gain responsibility, and **nothing new is created**:

| Surface | Gains |
|---|---|
| Shot inspector (right panel) | A tab strip. `Shot Info` holds today's contents unchanged; `Effects` holds the Stack, the transition pair, and the band panels. |
| Monitor (`#timeline-monitor`) | Plays the **effected** picture rather than the raw take, and carries the Drive readout beneath it. |
| Timeline | The Overlap band, and the `ƒ` chip on clips carrying Effects. |

Song analysis (FX-1) surfaces as beat markers on the existing waveform and as a snap target; it adds no panel of its own.

## Voice and tone

The house voice: **plain, specific, and never reassuring about something it cannot verify.** Microcopy rules for this feature:

- Name the mechanism, not the feeling. `Cut at 2.417s into the take` — not `Looking good`.
- A refusal names the thing and the reason in one sentence, and offers the next action where one exists. `No song analysis yet — bands need it. [Analyze song]`
- An automatic change announces itself in the past tense at the moment it happens. ~~`Shot 05's transition in set to Dissolve to match.`~~ `Shot 05's transition in set to Dissolve to match Shot 04's transition out.` *(amended 2026-08-29 by story 11.3: story 11.3's own acceptance criterion says the toast names **both** Shots, and this example named one. Two documents could not both be right; the epic's criterion won, and the sentence is now the one that ships.)*
- Never imply precision the system does not have. The BPM estimate reads `~124 BPM (estimated)`, and nothing refuses on it.
- Stale is a state with a name, not a silence. `Preview is stale — re-rendering` and never a frozen picture presented as current.

## Component patterns

### The inspector tab strip (FX-4)

Follows the existing Assets subtab pattern in `api.js` (`ASSET_TABS`) — a strip built from a data array rather than written into markup, so tabs stay declarative.

- Two tabs: `Shot Info`, `Effects`. `Shot Info` is the default and contains today's inspector unchanged in content, order, and behaviour.
- The `Effects` tab carries a trailing count when the Shot has anything: `Effects · 3`. ~~An Overlap transition counts toward it.~~ **A transition set on either of the Shot's two boundaries counts toward it, whether or not an Overlap is under it** *(decided 2026-08-29 by Epic 11, which owns transitions. `api.shotTabStrip` counted effect cards only and its docstring said so, so the two could not both be right. The count is the document's way, because the chip's stated job is "the Shot has anything" and a Shot whose only Director work is a Dissolve would otherwise read as untouched in the one place the strip could say otherwise. The narrowing from "an Overlap transition" is the other half: a count that fell as a Director dragged two clips apart would say the row went empty while the row goes on showing the type it holds. The cost is that `clipEffectsChip` on the timeline counts effects alone and says "Carries 3 effects" in words, so the two numbers can differ — which is why the chip now carries its own sentence, `2 effects and 1 transition on this shot.`, rather than being a digit whose unit has to be guessed at.)*
- **Tab selection persists across inspector rebuilds for the same Shot** — including the background reload that already rebuilds this panel every two seconds. Selecting a *different* Shot returns to `Shot Info`.
- The existing focus-preserving rebuild (`captureInspectorEdit` / `restoreInspectorEdit`) extends to cover the active tab and any open band panel. An in-progress edit in either tab survives a rebuild and survives a tab switch.

### The Effect Stack (FX-5, FX-6)

- `[+ Effect]` opens a grouped picker: four Consolas family headers — `GRADE`, `TEXTURE`, `STYLIZE`, `GEOMETRY` — with their effects beneath. The picker is a list, not a gallery; there are no thumbnails, because a thumbnail of a grade on an unknown frame teaches nothing.
- Cards reorder by drag on the handle, and by `Alt+↑`/`Alt+↓` when a card is focused. **Reorder that the render chain forbids is not offered** — the picker and the drop targets both respect family ordering, so an illegal order cannot be expressed rather than being expressed and then rejected (FX-5).
- Each card: enable toggle, `✕` remove, and its parameter rows.
- `[Copy stack to…]` opens an explicit target chooser — named Shots, or the current Section, never a bare "all". It states what will happen before it runs (`Replaces the stack on 12 shots`), and its report names refusals: `Applied to 11. Shot 07 is locked.` (FX-6, FX-7).

### The parameter row and its band panel (FX-12, FX-13, FX-14)

The density decision: **per-parameter, collapsed.**

- Every parameter row ends with a `〜` glyph — `{colors.dim}` when inert, `{colors.blue}` when the parameter is bound. The glyph is the whole affordance; there is no separate "make reactive" mode.
- Clicking `〜` opens that parameter's **band panel** inline beneath the row. Only one band panel is open at a time; opening another closes the first.
- The band panel contains, in order:
  1. **Spectrum strip** — the song's own average spectrum with the Band drawn over it. Drag the region to move centre, drag its edges for width, drag the softness handle for falloff. The Band is a thing you see, not three numbers you guess.
  2. **`punch | sustain`** segmented control. Neither is preselected on a fresh binding — the Director chooses, because nothing infers a drive mode (FX-14).
  3. **Floor** and **Depth** sliders, with live Consolas readouts.
- Closing the panel keeps the binding. Removing it is an explicit `Remove binding` inside the panel, and the parameter returns to its resting value with no residue (FX-12).
- **With no Song Envelope**, `〜` is present but inert, and clicking it opens a one-line refusal with an action: `No song analysis yet — bands need it. [Analyze song]` (FX-15). The glyph is never hidden, because a hidden control teaches nothing about what the product can do.

### The transition pair (FX-16, FX-17, FX-18, FX-19)

Two rows in the Effects tab, `Transition in` and `Transition out`, each selecting from the curated catalogue.

**When an Overlap exists** — the row carries a `{colors.blue}` left edge and the Overlap's length in Consolas (`0.50s · from overlap`). Setting one side sets the other and says so:

> ~~`Shot 05's transition in set to Dissolve to match.`~~
> `Shot 05's transition in set to Dissolve to match Shot 04's transition out.`

announced in the existing toast idiom, in the past tense, naming both Shots. The pair can never hold two types (FX-17). *(Amended 2026-08-29 by story 11.3: the example named one Shot while the sentence under it — and story 11.3's own acceptance criterion — asks for both. Clearing a side says the same thing in the same shape: `Shot 05's transition in cleared to match Shot 04's transition out.` And **nothing is said where no mirror could have fired** — the last Shot's `Transition out` and the first Shot's `Transition in` have nothing on the other side of them to write, and announcing a change that did not happen is what this idiom exists to prevent.)*

**When there is no boundary at all** — a `Transition in` on the **first** Shot in song order. *(Added 2026-08-29 by story 11.3; no artifact in this epic described this state, and it is not the one below.)* Nothing precedes it, so the write route has nothing to mirror onto, and the export reads `transition_out` and only that — `app._compose_one_sided_transitions`' own docstring names this as the one boundary *"where an incoming field has no pair to mirror"* and leaves it to a later story. The field stores and nothing renders from it, so the row says exactly that:

> `Nothing plays before shot 01 — this transition in has no frames to treat, and the export renders nothing from it.`

The control stays live, on the same rule that keeps the one-sided row live: a greyed control states that something is impossible without stating why, and this one becomes meaningful the moment a Shot is added ahead of it. What is refused is the silence, not the gesture.

**When no Overlap exists** — the row carries a `{colors.dim}` left edge and states what will actually happen:

> `No overlap — this treats shot 04's last frames, then cuts.`

The setting is live, not disabled: a one-sided transition is a real editorial choice, not a broken pair (FX-18). Its length control is bounded by the Shot's own duration and by nothing invisible.

**Catalogue entries that are pair-only** appear in the list and refuse one-sided use with their reason, rather than being silently absent from a list the Director is trying to learn (FX-19).

### The Monitor becomes effect-aware (FX-20, FX-21)

The existing Monitor plays the **effected** picture. There is no second preview surface.

- It continues to follow the master clock and continues to show the take slice for the playhead. What changes is which file it plays.
- When the Effect Stack changes, the Monitor's picture is **marked stale and re-rendered**. During the re-render it keeps playing the previous picture with a Consolas `STALE` micro-label in the corner — never a frozen frame, never a spinner over black, never a percentage (`DESIGN.md` §5).
- Rapid parameter dragging does not queue a render per change. A render superseded before it finishes is abandoned and never played (FX-20).
- **A Transition previews in place**: when the playhead is inside or approaching an Overlap, the Monitor's window spans the boundary, so the outgoing Shot, the Transition, and the incoming Shot play as one continuous piece (FX-21).
- Preview never touches the Approved Output and never reaches ComfyUI.

`[NOTE FOR UX]` **A/B compare was raised and not adopted.** Grading is comparative and a grade you have been staring at is hard to judge; a hold-to-compare control on the Monitor would answer it in one key. It is not in this spec because it was not chosen, and it is recorded here so it is a deferred idea rather than a forgotten one.

### The Drive readout (FX-22)

A canvas strip immediately beneath the Monitor, spanning the selected Shot's window.

- The Drive envelope in `{colors.blue}`; ~~the Trigger Floor as a `{colors.dim}` hairline~~; the existing `{colors.acid}` playhead line drawn through it, so envelope and picture read against the same time axis. *(amended 2026-08-27, R-31: the floor is compared against the **band level** while the readout draws the **compiled parameter value** — different units, so a hairline at the floor's number names a value it has nothing to say about. The `--dim` hairline is the **rest line**, and the floor is drawn as ground under the silenced runs. Below the floor a `punch` drive is exactly zero, so colour alone could not have marked it: the silenced line lies on the rest line in the same token, and the state needs width.)*
- Below the floor the envelope draws `{colors.dim}` — a silenced passage looks silenced, not merely low. This is the readout's whole reason for existing: the question is not "how loud" but "is this firing, and where".
- Visible only when the selected Shot carries at least one binding. Absent, not empty, otherwise.
- With no Song Envelope it is absent and the band panel's refusal explains why (FX-15).

### The Overlap on the timeline (FX-16)

The Director's requirement was blue. The design requirement is that it **reads as a transition rather than as an error**, because an overlap today is just a hard cut and every other coloured region on this timeline means something is wrong.

Three things carry that meaning together:

1. **Fill, not outline.** A `{colors.blue}` band at 22 % behind the clip content, with 1px `{colors.blue}` top and bottom edges. Error states in this application are *outlines* (`--red-edge` borders, dashed `MISSING` boxes); a soft filled region is structurally unlike them.
2. **A name.** The transition type as a Consolas micro-label — `DISSOLVE`, `FADE`, `WIPE →`. A region with a name is a decision; a region without one is a warning. *(~~centred~~ amended 2026-08-29 by story 11.2: centred, the label lands on the clip text under it and both go illegible — see `DESIGN.md` §3. It is drawn along the band's bottom edge, where the clip paints nothing.)*
3. **An untyped overlap looks different.** `{colors.line-strong}` hatch and a `CUT` label, no blue. An overlap with no transition set *is* a hard cut, so it must not borrow the transition's treatment.

~~The band draws **behind** clip content, so state borders and the `✓ ƒ ⚑` chips stay fully legible on top of it.~~ **The band draws above the clip content at 22 % alpha with `pointer-events: none`** *(amended 2026-08-29 by story 11.2, on R-40: behind is unbuildable — `.shot-clip` is opaque with `overflow: hidden`, so two clips cover the overlap region completely and a band behind them paints nothing. Above at 22 % keeps both properties this sentence was protecting, and `DESIGN.md` §3 carries the measurement.)* The band itself is not a drag target — the existing clip edges remain the only handles, so nothing new competes with edge dragging, and its layer sits below the resize handles' own so an overlay cannot bury a handle a neighbour was already covering.

**The name is drawn where it fits, and always said where it does not.** *(Added 2026-08-29 by story 11.2, measured.)* At the default 16.6 px/s a 0.50 s Overlap is 8.3 px wide, and `DISSOLVE` needs roughly 3.1 s of Overlap before it letters at that zoom. Below its own label's measured width the band draws no label rather than a clipped fragment of one, and the whole sentence stays on its `title` and its accessible name at every width — so the type is text at every zoom, even where it is not drawn as one.

### The `ƒ` chip (timeline signal)

A completed clip carrying Effects shows a Consolas `ƒ` corner chip in the existing 14 px idiom, reading order `✓ ƒ ⚑`. It answers "which shots have I graded" from the timeline, without selecting anything.

Three chips is the corner's maximum. A fourth requires removing one.

## State patterns

| State | Where it shows | Treatment |
|---|---|---|
| No Song Envelope | Band panel, Drive readout, beat markers | Named refusal with `[Analyze song]`; markers absent, no error |
| Song Envelope computing | Song workspace, band panels | `Analyzing song…`; the rest of the application is unaffected and unblocked (FX-1) |
| Song Envelope invalidated by a song change | Everywhere it is consumed | Reported as **absent**, never served as current. Stored bindings are retained and reported unresolvable, never dropped (FX-15) |
| Preview stale | Monitor | Previous picture continues, Consolas `STALE` corner label |
| Preview failed | Monitor + Effects tab | Named reason inline; the Stack is untouched (FX-20) |
| Effect unresolvable (missing LUT, unresolvable binding) | Effect card + export refusal | `--red-edge` on the card, exact reason inline; export refuses naming every such problem in one report (FX-24) |
| Shot locked | Effects tab | Every writing control disabled, lock stated as the reason, Stack readable (FX-7) |
| Parameter bound | Parameter row | `〜` glyph in `{colors.blue}` |
| Overlap, transition set | Timeline | Blue band + type label |
| Overlap, no transition | Timeline | Hatch + `CUT` label |

## Interaction primitives

- **Drag a clip edge** — existing gesture, unchanged. Overlapping a neighbour now produces a visible Overlap band. Beat markers join lyric and phrase boundaries as snap targets; snapping stays an assist that warns and never constrains (FX-3, inheriting the trim-nudge posture).
- **Drag a slider** — coalesced. The preview re-renders on settle, not per pixel.
- **Drag on the spectrum strip** — region body moves centre, edges set width, handle sets softness.
- **Click `〜`** — open/close that parameter's band panel.
- **`Alt+↑` / `Alt+↓`** on a focused effect card — reorder within legal positions.
- **Keyboard on a focused clip** — existing `F` / `A` / `Space` / `←` `→` unchanged. No new single-key binding is added to the timeline; the corner is already crowded and the Effects work happens in the panel.

## Accessibility floor

Inherits the MVP floor and extends it:

- **State is never colour-alone.** The Overlap always carries its type as text; the `ƒ` chip is a glyph, not a tint; a bound parameter shows a `〜` glyph as well as a colour; the Drive readout's silenced region differs in colour *and* ~~is annotated by the floor hairline~~ carries a `--dim` ground bar beneath it *(corrected 2026-08-28 by audit: R-31 struck the floor hairline as unmeasurable on this axis, and this rule's own evidence was still citing it -- an accessibility argument resting on a drawing that does not exist. The correction **strengthens** the rule rather than weakening it: below the floor a `punch` drive is exactly zero, so the silenced line lies **on** the rest line in the same token, and colour genuinely carries nothing here. The ground bar has width, and grows as the Floor rises.)*.
- **Every canvas has a non-canvas equivalent.** The spectrum strip's Band is also expressed as three labelled numeric inputs, reachable by keyboard and readable by a screen reader. The Drive readout is decorative-by-derivation and is marked `aria-hidden`; the facts it shows (peak time, whether the binding fires at all) are also stated in text ~~on the band panel~~ in a caption inside the readout's own `<figure>`. *(amended 2026-08-27, R-32: the band panel is closed most of the time, so a screen reader meeting the canvas with no panel open would get nothing — which is the case this rule exists to serve. The facts are stated in a caption inside the readout's own `<figure>`; the band panel points at it without restating them.)*
- **Every drag has a keyboard path.** Band centre, width, and softness are arrow-key adjustable when their numeric inputs are focused. Effect reordering has `Alt+↑`/`Alt+↓`. Clip-edge dragging keeps its existing numeric start/duration inputs in `Shot Info`.
- **The tab strip is a real tablist** — `role="tablist"`, arrow-key movement between tabs, `aria-selected`, and panels associated by `aria-controls`.
- **No motion is introduced.** Nothing here pulses or animates; the existing amber render pulse remains the only animation and keeps its `prefers-reduced-motion` behaviour.
- Focus is never lost to a background rebuild — the existing focus-preserving mechanism extends to the tab strip and band panels.

## Key flows

### KF-1 — The Director gives a finished cut one look (UJ-4)

1. Selects shot 01 on the timeline; the inspector opens on `Shot Info`.
2. Clicks `Effects`. Empty stack, `[+ Effect]`.
3. Picks `GRADE / Film LUT`. A card appears; the Monitor marks `STALE` and, within about a second, plays the graded picture against the song.
4. Drags `Contrast`; the Monitor re-renders on settle.
5. `[Copy stack to…]` → chooses all shots in the Chorus section. Report: `Applied to 9. Shot 07 is locked.`
6. **Climax:** plays the timeline from the top. Nine shots that were nine clips read as one piece — and the `ƒ` chips make the two ungraded shots visible without opening anything.
7. Unlocks shot 07, copies again, plays it back.

### KF-2 — The Director makes the video move with the song (UJ-5)

1. Selects a shot carrying `TEXTURE / Film grain`.
2. Clicks `〜` on the `Amount` row. The band panel opens; the spectrum strip shows the song's own spectrum.
3. Drags the Band region to the bass end, narrows it. Picks `punch`. Raises `Floor` until the quiet verse passage in the Drive readout drops to `--dim`.
4. Raises `Depth`, watching the readout and the Monitor together.
5. **Climax:** the grain surges on the kick and settles between hits, and the readout shows exactly which hits it is answering.
6. Closes the panel. The `〜` stays lit blue; the binding travels with the manifest.

**Edge case:** the Director had not analyzed the song. Step 2 opens `No song analysis yet — bands need it. [Analyze song]` instead of a Band selector that would silently do nothing.

### KF-3 — The Director dissolves one Shot into the next (UJ-6)

1. Drags shot 05's left edge back over shot 04. A hatched region with a `CUT` label appears — honest: an untyped overlap is still a hard cut.
2. Opens shot 04's `Effects` tab, sets `Transition out` to `Dissolve`. Toast: `Shot 05's transition in set to Dissolve to match Shot 04's transition out.` The region turns `--blue` and its label becomes `DISSOLVE` — at a zoom wide enough to hold it.
3. Drags the edge further; the band grows and the row's length readout follows.
4. **Climax:** scrubs the playhead through the boundary. The Monitor plays shot 04, the dissolve, and shot 05 as one continuous piece — and the timeline's total length has not moved by a frame.
5. Drags shot 05 back off the overlap. The band disappears; both rows keep their stored types and now read `No overlap — this treats shot 04's last frames, then cuts.` And the change is announced, because it changes the rendered picture with no gesture of its own (R-36): `Shot 04 and Shot 05 no longer overlap — Shot 04's transition now treats its own last frames, then cuts.`

## Key-screen reference

[`mockups/effects-tab-key-screen.html`](mockups/effects-tab-key-screen.html) — shot 05 selected with two live effects and one disabled, `Film grain → Size` bound to a bass band with its panel open, the Monitor mid-restage showing `STALE`, the Drive readout beneath it, a typed `DISSOLVE` overlap and an untyped `CUT` overlap on the timeline, and the transition pair in both its paired and one-sided states.

Built from the live `styles.css` tokens plus `--blue`. **The spines win on conflict with this or any other mock** — it is a reference for layout and density, not a specification.

## Open questions for the build

1. **Spectrum strip source data.** The strip needs a per-band average across the whole song. The PRD's Song Envelope (FX-1) specifies per-band level envelopes over time; whether the strip draws a whole-song average, a windowed average around the playhead, or the selected Shot's own average is undecided and affects what the analysis must store. **Blocking for architecture.**
2. **Preview render location and lifetime.** Where preview files are written, when they are cleaned up, and whether they survive a reload is undecided. It affects the `STALE` behaviour directly.
3. **Section-scoped copy.** KF-1 step 5 assumes `[Copy stack to…]` can target a Section. The PRD scopes Effects per-Shot with copying as the substitute (§6.2), and a Section target is the natural affordance — confirm it is a target chooser convenience and not Section-level storage, which is explicitly out of scope.
4. **A/B compare on the Monitor.** Recorded above as deferred, not forgotten.

## Out of scope for this UX

Effect thumbnails or a preview gallery in the picker · keyframe curves of any kind · a transition preview gallery · per-effect presets saved across projects · any new modal (the pre-flight remains the application's only one) · any new workspace or rail step.
