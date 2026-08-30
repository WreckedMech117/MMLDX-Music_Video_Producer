---
title: "Shot Effects and Transitions — Visual Design"
status: final
created: 2026-08-21
updated: 2026-08-21
sources:
  - ../../prds/prd-MusicVideoProducer-effects-2026-08-21/prd.md
  - ../../effects-director-rulings-2026-08-21.md
  - ../ux-mvp-2026-08-16/DESIGN.md
---

# DESIGN — Visual Identity for Effects and Transitions

Settled with the Director on 2026-08-21. This is an **extension** of `../ux-mvp-2026-08-16/DESIGN.md`, which remains authoritative for everything it covers. The standing rule holds unchanged: **extend the existing visual language, never redesign it.** Every token below either already exists in `src/music_video_producer/web/assets/styles.css` or is the one deliberate addition recorded in §1.

## 1. The one new token, and why the anti-goal permits it

The MVP design contract carries an anti-goal: *"No new accent colors, gradients, or shadows."* FX-16 requires the Overlap to read as blue, and the palette has no blue. The nearest existing accent, `--cyan`, already means **approved** and appears as a chip on the very clips an Overlap runs between.

**Resolved by the Director: add one token, and only one.**

| Token | Value | Role |
|---|---|---|
| `--blue` | `#5b9bd5` | **Transitions, and nothing else.** The Overlap band, the transition-type label, the paired out/in controls when they are live. |

The anti-goal is **amended rather than broken**, and the amendment is narrow enough to state as a rule: *the no-new-colours rule bars decorative colour and colour invented for a state the model already has a colour for. A Transition is a new first-class concept in the product, and a concept with no vocabulary gets expressed by overloading someone else's — which is what "cyan means approved and also transition" would have been.*

`--blue` is now closed. A seventh accent requires the same argument to be made again from scratch, and "the effects feature got one" is not that argument.

**Contrast note.** `--blue` is used as a band fill, a border, and a control accent — never as body text on `--bg`. Where it labels, the label is `--ink` or `--muted` on a `--blue` edge. State is never colour-alone (see EXPERIENCE.md § Accessibility floor): the Overlap always carries its transition type as a Consolas micro-label.

## 2. Inherited tokens, used unchanged

All of `--bg`, `--surface-0..3`, `--line`, `--line-strong`, `--ink`, `--muted`, `--dim`, `--acid`, `--acid-ink`, `--amber`, `--red`, `--red-edge`, `--cyan`, `--radius`, `--rail`, `--topbar`.

Typography unchanged: Bahnschrift/system UI at 14 px base; **Consolas micro-labels** (9 px, letter-spaced, uppercase) for indices, IDs, table headers, and — new here — effect family names, transition type names, and band names. The micro-label is the interface's signature and it carries into every component below.

## 3. State colour semantics — additions

The MVP contract's rule stands: **one state, one colour, everywhere it appears.** Three additions, and no reassignments:

| State | Treatment |
|---|---|
| Clip carries Effects | Consolas `ƒ` corner chip, `--muted` on `--surface-2`, 14 px, `--radius` 3px |
| Overlap with a Transition set | `--blue` band at 22 % ~~behind~~ **above** the clip run, `--blue` 1px top and bottom edge, Consolas type label ~~centred~~ along the band's bottom edge, where it fits |
| Overlap with no Transition set | `--line-strong` hatch, no `--blue`, Consolas `CUT` label — an overlap is a hard cut until a type is chosen, and it must look like one |
| Parameter bound to a Band | `〜` glyph on the parameter row, `--dim` when inert, `--blue` when bound |

**Amended 2026-08-29 by story 11.2, on R-40, with the measurement.** Both rows above said the band draws *behind* the clip content (see also §5), so state borders and the corner chips would stay legible on top of it. **That is unbuildable at HEAD:** `.shot-clip` is `background: #232919` — opaque — with `overflow: hidden`, so two clips cover the overlap region completely and a band behind them paints nothing at all. It is drawn **above** at 22 % alpha with `pointer-events: none`, which preserves both properties the rule was protecting — everything underneath is still readable through the fill, and nothing new is a drag target — and is the technique `#beat-band` and `#vocal-band` already use one track down.

The layer sits at `z-index: 1`: above every clip body (`.shot-clip` carries no z-index) and **below every resize handle** (`.resize-handle` carries `z-index: 2`). That is load-bearing rather than tidy — a later clip already paints over the earlier one's right handle, and an overlay at 2 or above would take back the 2026-08-21 fix on all eighteen of the Director's overlapping pairs. `tests/e2e_overlap_band.py` hit-tests every handle lying under a band.

**And the label is not always drawn, which nothing here said.** Measured 2026-08-29: at the default 16.6 px/s a **0.50 s Overlap is 8.3 px wide**, and `DISSOLVE` needs about 3.1 s of Overlap before it letters at that zoom. Below its own label's measured width the band draws **no label**, because a clipped fragment of a word says something false where nothing says only that there is not room — and the whole sentence (`DISSOLVE across a 0.50s overlap between shot 01 and shot 02.`) stays on the band's `title` and its accessible name at **every** width, which is what keeps the type off the fill alone (UX-DR15). At a working zoom the label is there; at a whole-song zoom what carries the state is the treatment, and a `--blue` fill against a `--line-strong` hatch differ in texture and not only in hue.

One real consequence, recorded rather than implied: `BOUNDARY_TOLERANCE_SECONDS` (1/48 s) is what makes an overlap an overlap, and that is **0.35 px** at the default zoom. The band is floored at 2 px so a blend the assembler will really perform is never drawn as nothing.

**And the label is not centred vertically, which was found by looking at it.** *(Added 2026-08-29, same story, second pass.)* The first build centred it, as this section said. The screenshot showed `CUT` drawn straight through the word "dark." in the clip beneath — **both** words illegible — and every automated gate was green over it. The cause is structural rather than a near miss: a band spans a *boundary*, so the region under it is the later clip's own left edge, which is exactly where `.clip-id` and `.clip-prompt` are drawn. This is `#beat-band`'s lesson in the other direction, and its own comment already states the rule: a mark that buries what it annotates fails, however correctly it is placed.

So the label sits along the band's **bottom** edge, on a translucent `--bg` ground. A clip is 82 px and its prompt stops painting at 47.8 px (59.8 px with a RENDERING line), so the bottom of the band is the one strip where the clip draws nothing at all. The ground is not a shadow and not chrome (§5 bars both); it is the same device the existing `.job-status` and `.source-badge` chips already use.

**And then the chip column, which the same measurement caught on the next run.** `.clip-chips` is anchored `bottom: 4px` at the clip's **right** edge — and a band's right edge *is* the earlier clip's right edge, so a bottom-aligned label lands on the `ƒ` chip of every graded Shot with an Overlap after it. The ground made the label readable and hid the chip, which is one state signal drawn over another. The label gives that column its **33 px** back — `.clip-prompt`'s own inset around the same chips, the same number for the same reason — and `overlapBands` adds it to the width a band needs *before it letters at all*, so the inset can never squeeze a word into the column instead of withholding it.

The cost is stated rather than hidden: under an overlap the earlier clip's chip is usually already covered by the later clip's opaque body, so the inset often protects a chip nobody can see, and it costs 33 px of band before a graded Shot's label appears. `renderTimeline` draws clips in **manifest** order rather than song order, though, so which clip is on top is not a property of the plan — a Split appends the new half to the end of the manifest — and in that case the chip really is on top. `tests/e2e_overlap_band.py` measures the label against the **painted glyph rectangles** of every clip's text and chips — element boxes lie here, exactly as they did for the chip inset above — and fails on any intersection; its fixture carries a real effect stack, because one with no chip on it would make half that check impossible to fail.

Chips **stack in a column up the clip's right edge**, newest concern nearest the bottom, and are ordered only when there is more than one. The reading order when they coexist is `✓ ƒ ⚑`.

**Amended 2026-08-25 by Director ruling.** This section previously said the chips sat side by side in one corner and that *"three chips is the maximum the corner will ever carry"*. That cap was a **width** constraint wearing a design rule: a clip is `min-width: 40px` — a short Shot, or any Shot at low zoom — and a single 15px chip at its 14px offset already claims 29px of it, so three abreast simply cannot fit a narrow clip. Stacking spends height instead, and a clip is a fixed 82px tall, which holds four 15px chips with their gaps inside the existing box. **No cap is needed and none is stated**; the limit is the clip's own height, which is a real constraint rather than an invented one.

Row geometry does **not** become content-dependent. The clip stays 82px and the track keeps its `min-height`; nothing about a chip changes what the timeline's arithmetic assumes.

**The prompt is inset only at the counts that need it, and one chip does not.** The first draft of this amendment said a clip carrying chips insets its prompt, full stop. Measured 2026-08-25 against painted glyph rectangles rather than element boxes — a `-webkit-line-clamp` box has rectangles for lines it does not paint, and counting those invents collisions — the column's top sits at **62px** for one chip and rises 19px per chip after it, while the prompt stops painting at **47.8px**, or **59.8px** on a clip also carrying a RENDERING line. One chip therefore passes cleanly beneath the prompt, clearing even the worst case by 2.2px, and the collision begins at **two**. A flat inset cost a wide clip 16 of its 31 readable characters to prevent nothing. So the inset is a function of the column's height and starts at the second chip.

One honest limit: on a clip at its `min-width: 40px` the prompt's content box is already zero, so text overflows to the clip's edge whatever the padding says — the overlap there is pre-existing geometry and no inset changes it. And at **four** chips the top of the column reaches the `.clip-id` row, which has no inset of its own, so a short clip loses the tail of "SHOT 01 · 6.0s". The 82px budget genuinely holds four chips; the *readable* budget on a short clip is nearer three. Whoever adds the second chip inherits that.

At the time of writing only `ƒ` exists: approved and flagged are still a border colour and a top border rather than chips, so the reading order above is a rule waiting for a second chip to govern.

## 4. New components

All composed from the tokens above.

1. **Inspector tab strip** — two tabs, `Shot Info` and `Effects`, in the existing Assets-subtab idiom (`api.js` `ASSET_TABS`): Consolas micro-labels, 1px `--line` bottom border, active tab carries a 2px `--acid` underline and `--ink` text; inactive `--dim`. The Effects tab shows a trailing count chip when the Shot carries anything (`Effects · 3`).

2. **Effect card** — one per Effect in the Stack. `--surface-1` on `--radius`, 1px `--line`. Header row: drag handle, Consolas family name in `--dim`, effect name in `--ink`, an enable toggle, and a `✕` remove. Disabled cards drop to 45 % opacity and keep their controls readable — a disabled Effect is retained, not hidden (FX-5).

3. **Parameter row** — label in `--muted`, a horizontal slider on `--line` track with an `--acid` fill and `--ink` thumb, a numeric readout in Consolas, and the `〜` bind glyph at the right edge. Row height matches existing form rows; nothing about it is taller than the panel already is.

4. **Band panel** — opens inline beneath its parameter row, `--surface-2`, inset 1px `--line`. Contains the **spectrum strip** (below), a `punch | sustain` segmented control, and floor and depth sliders. Bordered left in `--blue` to mark it as reactive rather than static.

5. **Spectrum strip** — a canvas drawn from the Song Envelope: the song's own average spectrum as `--dim` bars, with the selected Band drawn over it as a `--blue` region whose edges fall off according to softness. Draggable centre, draggable edges for width, a softness handle. This is the interface's only data-driven chart and it earns its place by making a Band a thing you *see* rather than three numbers you guess at.

6. **Drive readout** — a canvas strip beneath the Monitor spanning the selected Shot's window: the Drive envelope in `--blue`, ~~the Trigger Floor as a horizontal `--dim` hairline~~, and the playhead as the existing `--acid` line. *(amended 2026-08-27, R-31: the floor is compared against the **band level** while the readout draws the **compiled parameter value** — different units, so a hairline at the floor's number names a value it has nothing to say about. The `--dim` hairline is the **rest line**, and the floor is drawn as ground under the silenced runs. Below the floor a `punch` drive is exactly zero, so colour alone could not have marked it: the silenced line lies on the rest line in the same token, and the state needs width.)* Where the envelope sits below the floor it draws `--dim` instead of `--blue`, so silenced passages are visible as silenced rather than merely low.

7. **Overlap band** — on the timeline, per §3. Drag targets are the existing clip edges; the band itself is not draggable, so nothing new competes with edge dragging.

8. **Transition pair control** — in the Effects tab, two rows: `Transition in` and `Transition out`, each a select of catalogue types. When a row is live because an Overlap exists, it carries a `--blue` left edge and ~~the Overlap's length~~ **the blend's length** in Consolas *(R-42, 2026-08-30: the Overlap is a float a Director dragged and the blend is that float in frames, and the readout states the one that renders — measured at 113 px of a 248 px row, one line, 35 px clear of its label)*. When it is one-sided it carries a `--dim` left edge and states so. *(Extended 2026-08-29 by story 11.5.)* A row naming a boundary with a blend also carries a **`Watch blend`** control and, under it, the clip itself — full width of the rail, looping, drawn only once a picture has decoded onto it. A row naming a boundary with **no** blend carries a sentence saying which absence it is instead, and only where the row is not already saying it: a one-sided row's own note and the preview's would be one fact stated twice.

   **Two measurements, both found by looking and neither predictable from the markup.** Beside the button in a 236 px rail the clip had about 100 px left for a two-word label, which wrapped `Watch blend` onto two lines and squeezed the picture into a 128 px strip — too small to judge a transition by, which is the one thing it is for. Given the whole rail it is legible, and then it lands **below the fold**: the Effects tab scrolls and the transition rows are the bottom of it, so the clip appears where a Director cannot see it. It is scrolled into view on `loadeddata` — not before, because the element has no height until it has decoded a frame — and `tests/e2e_transition_preview.py` measures its bottom edge against the rail's and fails if it is past it. Same class as the band panel's 225 px below the fold, and invisible to every gate but a browser.

## 5. Do's and don'ts

**Do**

- ~~Draw the Overlap band *behind* the clip content, so clip state borders and chips stay fully legible on top of it.~~ **Amended 2026-08-29 by story 11.2, on R-40 — see §3.** Draw it **above** the clip content at 22 % alpha with `pointer-events: none`, at a `z-index` below the resize handles. Behind is unbuildable: `.shot-clip` is opaque with `overflow: hidden`, so a band behind two clips paints nothing. Legibility and "not a drag target" both survive the move; drawing it behind did not survive contact with the stylesheet.
- Keep every Consolas micro-label uppercase and letter-spaced, including the new transition and band names.
- Let `--blue` mean transition or reactive-binding and nothing else, anywhere.

**Don't**

- Introduce a seventh accent. `--blue` is the last one; see §1.
- Use `--acid` for anything in the effects surface except a slider fill and the active tab underline — acid means *complete* and *primary action* everywhere else, and a grade control is neither.
- Add gradients, shadows, or a preview "frame" chrome. Depth comes from the existing surface steps (`#0c0d0d → #111313 → #171919 → #1d2020 → #252828`).
- Show a percentage for a preview render. The honesty rule that bars fake render progress bars applies here identically — a preview is either the current one or it is stale, and it says which.
- Animate an effect card's expansion beyond the existing collapse idiom, and never animate the spectrum strip's redraw.
