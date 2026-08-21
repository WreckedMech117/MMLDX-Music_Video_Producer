# Director's rulings — Effects and Transitions, 2026-08-21

Recorded by Mary (Analyst) during pre-PRD elicitation. These are decisions, not proposals.
The PRD is built on them; a change here is a change to the PRD.

---

## R-1 — The v1 effect catalogue is four families, not three

**Grade/colour, Texture/film, Stylize/glitch, and Geometry/camera** are all in v1.

Geometry was initially left out and then reinstated in full — static *and* reactive — rather
than admitted only as a reactive scale punch. Punch-in, slow zoom, handheld shake, dutch tilt
and mirror are therefore first-class v1 effects.

**Consequence:** geometry stages must run **before** `scale` in the trim chain, so a punch-in
samples the take's own pixels rather than resampling an already-scaled frame. This is the one
ordering constraint the build cannot get wrong silently.

## R-2 — Effects can be tied to the music, and the binding is general

> "In our EQ Video producer we have a way to tie some of these effects to the music EQ,
> applicable here."

**Any effect parameter can be bound to any frequency band.** The binding carries: band centre,
band width, band softness/falloff, drive mode (punch or sustain), trigger floor, and depth.
This mirrors the Music Visualizer Studio's *Effected range* + *reactive drive* model rather
than reducing it to a curated list of pre-wired reactive effects or to one global band per shot.

**Consequence:** every effect in the catalogue grows a "react to" sub-panel, and the audio
analysis subsystem is a **prerequisite**, not a later phase (see R-5).

## R-3 — A transition is an overlap, and the overlap is visible

Transitions are authored by **dragging clip edges to overlap**, taking advantage of the margins
already being produced. The overlapping region is **highlighted blue on the timeline**.

Each shot carries a **transition out** and a **transition in**. When A overlaps B, setting A's
*out* to a type **automatically sets B's *in* to the matching type** — the pair describes one
blend and cannot disagree.

## R-4 — With no overlap, out and in are independent one-sided effects

> "If there is no overlap then Shot/clip A out would be blur and Shot B could be set to
> something else."

A transition-out with nothing to blend into treats the **last N frames of A's own window** —
"blur out" blurs toward nothing, "fade out" fades to black — then a hard cut to B, which plays
its own *in* if it has one.

**Consequence:** a one-sided transition consumes no timeline length and borrows no neighbour's
frames. The frame grid is untouched in both the overlap and the no-overlap case.

## R-5 — Beat, onset and band analysis ship as a Phase 1 prerequisite

The Music Visualizer Studio's dependency-free extractor is ported to Python **now**, not in a
later phase. It feeds three consumers at once: reactive effect bindings (R-2), beat markers on
the timeline, and beat-snapping for cut placement — which gives the standing "snap cuts to
phrase boundaries" ruling actual beats to snap to for the first time.

## R-6 — Preview is a looping clip through the real chain

> **Superseded the same day.** R-6 originally chose a still frame plus a peak frame, and the
> Director then reopened it: *"We dont necessarily have to be locked into still frame choice if
> FFMPEG gives us options for a more live preview."* It was measured, and the still frame's cost
> advantage turned out not to exist. The original wording is kept below the ruling for the record.

The Effects panel previews by rendering **the Shot itself** through the exact ffmpeg filter chain
the export will run — at reduced dimensions and encoder quality, differing in nothing else — and
looping it. A Transition previews as a window spanning the boundary. A reactive binding shows its
Drive envelope drawn beneath the loop.

**Measured on this machine, 2026-08-21**, 1056×608 source, 24 fps, nine-stage filter chain:

| Preview | Cost |
|---|---|
| Single still frame, full resolution | 170 ms |
| Whole 4.5 s shot, half dimensions, `ultrafast`/CRF 28 | **270 ms** |
| Whole 15 s shot (longest this pipeline produces) | 660 ms |
| 2 s window around an `xfade` transition | 150–190 ms |
| Reactive chain driven by 108 timed `sendcmd` commands | 90 ms |
| Same, at half dimensions, using NVENC instead | 400–530 ms — **slower** |

A still costs 170 ms and the entire shot costs 270 ms. The still was never meaningfully cheaper,
and it cannot show motion, Geometry, Drive, or a Transition. **Hardware encoding is slower than
software here** because encoder initialization dominates a sub-second job; preview uses the CPU
encoder deliberately.

**Consequences of the reversal:**

- The accepted gap — no transition preview in v1 — is **closed**. Transitions are previewable.
- The **Peak Frame is dropped**. A 24 fps loop shows the Drive's peak several times a second, and
  the envelope readout answers the question the peak frame was really for: *when* is this firing.
- Preview fidelity at reduced dimensions is now an open assumption — speed is measured, but
  whether it is faithful enough to grade by is a judgement the Director still owes.

### Superseded original wording

The Effects panel previews by extracting a frame through the **actual ffmpeg filter chain the
export will run** — one engine, never an approximation. For a reactive effect it renders a
second still at the moment in the shot where the **drive envelope peaks**, so the effect is
visible at rest and at full.

~~**Stated gap, accepted:** a transition has no still frame, so **transitions are not previewable
in v1**. They are judged at export. A short-clip proxy render was offered and not taken; it
remains the obvious v2 answer if the export loop proves too slow to iterate on.~~ — withdrawn;
the measurement above made the proxy render cheap enough to be the default rather than the
fallback.

## R-7 — Standing constraints these rulings inherit

Carried in from the existing architecture and not renegotiated:

- The **frame grid is inviolable**. Nothing here may change the assembled video's length
  relative to the song, in either the overlap or the no-overlap case.
- Effects are **non-destructive**. An approved take file is never rewritten; effects live on
  the `Shot` in the manifest and are re-derived at export.
- A **locked shot refuses** effect and transition edits, as it refuses every other sweep.
- **ffmpeg argv stays pure and pinned by test.** Generated `sendcmd` scripts are held to the
  same standard: a pure function of (envelope, binding), compared as text.
