# Build Handoff — start here

**Written 2026-08-23.** Point a fresh development session at this file. It covers what is planned, what is built, and — the part that matters most — **the things a fresh session will otherwise get wrong**, because they were established by measurement rather than by reasoning and cannot be re-derived by thinking harder.

**Read `AGENTS.md` first for operations** — policy, verify commands, conventions, ComfyUI rules. This document does not repeat it. What follows is the planning state and its caveats.

---

## 1. Where things stand

Three features, one story-numbering space, three epic files that cross-link each other.

| Feature | Epics | Stories | State |
|---|---|---|---|
| Base product | 1–7 | 23 | Largely built; see `docs/ROADMAP.md` for verified-vs-unverified |
| **Shot Effects and Transitions** | 8–11 | 19 | **All four epics shipped (8, 9, 10, 11).** See §2 for what is open. |
| **Treatment Planning** | 12–17 | 17 | **Slice A shipped 2026-09-03** (Stories 12.1, 12.2 — the Brief's lock, slot, restore and contract). Everything else planned, not built |

*Corrected 2026-08-27.* The effects row read ~~**Fully planned, nothing built**~~ and the story counts read ~~24 / 18~~ for the first two features. The state claim was true on 2026-08-23 and is two epics stale; the treatment count of 17 was right and is untouched. `sprint-status.yaml` carries **`epic-8: done`** (8.1–8.3) and **`epic-9: done`** (9.1–9.7), across roughly fifteen commits ending at `0b0bb96`; `audio.py` and `effects.py` both ship, with the envelope sidecar, beat markers and snapping, the Effects tab, the preview cache and an effects-aware export behind them. Counted today against `epics.md`, `epics-effects.md` and `epics-treatment.md`: **23 / 19 / 17**. The effects figure moved because Epic 9 gained Story 9.7 during the build, and the base figure was never 24. This is the row `AGENTS.md` sends every new session to read first, and it was two epics stale — the same failure Epic 9's retrospective found in the tracker.

*Corrected 2026-08-30.* The effects row then read ~~**Epics 8 and 9 shipped. Epics 10 and 11 planned, not built**~~, which was **the third time this one cell has gone stale**, each time in the commit that made it stale. Epic 10 shipped 2026-08-27 (`ad67a14`…`4fd9b41`, four stories) and Epic 11 on 2026-08-29 (`0929538`…`7ef2b82`, five stories); `sprint-status.yaml` carries `epic-10: done` and `epic-11: done`. A cell that has been wrong three times is not a writing problem — it is the one claim in this document that no guard reads, and it is worth one. The story counts are unchanged and were re-counted: **23 / 19 / 17**.

Sprint tracking: `_bmad-output/implementation-artifacts/sprint-status.yaml` — 17 epics, 59 stories. **It is gitignored** (`.gitignore` excepts only `deferred-work.md` under `implementation-artifacts/`), so it is local-only by design. Regenerate it with `bmad-sprint-planning` after any epic change.

### Artifact map

| Feature | Artifacts |
|---|---|
| Effects | `effects-and-transitions-research-2026-08-21.md` · `effects-director-rulings-2026-08-21.md` (R-1…R-7) · **`effects-director-rulings-2026-08-24.md` (~~R-8…R-28~~ ~~R-8…R-32~~ ~~R-8…R-41~~ R-8…R-46)** *(corrected 2026-08-28 twice, and again on 2026-08-30 by the commit that added R-42 — the third time, and the first where the range moved in the same pass as the ruling rather than one pass later. The first range was written by `ad67a14`, the commit that added R-29 and R-30. The correction to R-32 was written by `1933c2e`, **the commit that added R-33** — the same defect, in the pass that was fixing it. Found by `tests/test_stale_claims.py` on its first run, which is why that guard exists.)* · `prds/prd-MusicVideoProducer-effects-2026-08-21/` · `ux-designs/ux-effects-2026-08-21/` · `architecture/architecture-MusicVideoProducer-effects-2026-08-21/` (AD-16…AD-31 + `BUILD-ORDER.md`) · `epics-effects.md` |
| Treatment | `treatment-planning-findings-and-rulings-2026-08-22.md` (F-1…F-6, R-1…R-18) · `prds/prd-MusicVideoProducer-treatment-2026-08-22/` · `ux-designs/ux-treatment-2026-08-22/` · `architecture/architecture-MusicVideoProducer-treatment-2026-08-22/` (AD-32…AD-47 + `BUILD-ORDER.md`) · `epics-treatment.md` |

All paths relative to `_bmad-output/planning-artifacts/` — *corrected 2026-08-27, this said ~~`_bmad-output/`~~ and there is no `_bmad-output/prds/`, `ux-designs/` or `architecture/`; every artifact above is one level further down.* Requirement prefixes never collide: base `FR-`, effects `FX-`, treatment `TP-`. Architecture decisions are one sequence, `AD-1`…`AD-47`.

---

## 2. Where to start

**Treatment `BUILD-ORDER.md`'s Slice B, or Epic 17 — the effects `BUILD-ORDER.md` has no unbuilt slice left, and treatment Slice A shipped 2026-09-03.** Slices A (song analysis), B (the chain builder), C (the Effects tab), D (preview), E (reactive binding) and F (transitions) are all built. Pick from the independent quick wins below, or from §6's open items — which is where the effects work that is genuinely still owed now lives.

*Corrected 2026-08-30.* This said ~~**Effects Epic 10 — Reactive Binding, which is `BUILD-ORDER.md`'s Slice E**~~ and went on describing E's foundation as though nothing had been built on it. Epic 10 shipped 2026-08-27 and Epic 11 on 2026-08-29. This is §1's stale cell met a second time in the same document: the *state* was corrected on 2026-08-27 and the *instruction that reads it* was not, so a session that skipped the table and read this heading — which is what §8 tells it to do — would have started an epic that was already in `git log`.

*Corrected 2026-08-27.* This said ~~**Effects Story 8.1 — Analyze the Song into an Envelope.** No dependencies, no blockers, and it ships beat markers and beat-snapping on its own even if nothing else in that feature ever lands.~~ Story 8.1 shipped on 2026-08-24 and the whole of Epics 8 and 9 with it. The ordering *was* the Director's decision and it still has the consequence §5 records, which is why that section is amended rather than deleted.

**Build order is `BUILD-ORDER.md`'s slices, not epic order.** Both features have one. Effects Epic 9 deliberately consolidates three slices; Treatment Epic 14 consolidates three more. Reading the epics alone will give you the wrong sequence.

**Independent quick wins**, each justified on its own and blocking nothing:

- ~~Treatment **Story 12.1/12.2** — Brief lock, recovery slot, restore, contract line. Closes FR-16's unstated exception.~~ **Shipped 2026-09-03.** The Brief now has the whole apparatus and is the *best* protected of the three. The one thing it did not do as planned is capture: the Brief's slot is filled by the Director's own save, not by an applied reply, because no reply can write the Brief — see AD-41's amendment, and `DIRECTOR_REPLACEABLE_DOCUMENTS`, which is the mapping split that made the difference expressible. **It did not close FR-16 completely**, and the residue is on the other two documents rather than on the Brief — see §6.
- Treatment **Epic 17** — the Song Planner. Fully independent, sits at the true start of the workflow.
- Treatment **Story 16.1** — plain proceed-to-next-step buttons, without their offers.
- ~~Effects **Epic 8** — song analysis, beat markers, beat-snapping.~~ **Shipped 2026-08-24.**

~~**Merge alone:** effects **Epic 11** (transitions).~~ **Shipped 2026-08-29.** It was the only work touching `assembly_plan` and the cumulative frame grid, it did get its own verification pass against `FX-NFR-1`, and **that pass is the reason this instruction was worth following**: the frame rule held on every plan while three geometries shipped a non-positive frame count, because a window that runs backwards cancels against itself. See AD-18's 2026-08-30 amendment. Anything else that touches the frame grid still merges alone — **and is reviewed, which that instruction did not say and now does. See §4.**

---

## 3. Caveats — measured facts, not opinions

**These were established by running things. Do not re-derive them by argument; the argument gets them wrong.**

### The local model's reliability envelope

Everything LLM-shaped in both features lives inside this:

- `populate` has **exceeded its 300 s timeout**. The director timeout was raised to 300 s for that reason and may be raised further — the Director has authorised it (R-11).
- Roughly **90 % of a reply is reasoning**, and reasoning length swings **26× across identical rolls**. A pass that succeeds once may time out on the next identical attempt. **This is why a retry matters as much as a longer timeout: a longer timeout buys the tail of the distribution, a retry re-rolls it.**
- The model **silently drops boolean and object fields**. `fill_shots` applied modes and citations while omitting `use_song_audio` and `singing` twice, and the narration claimed otherwise.
- `DirectorResult` never required `shots`, and that — not prompt wording — was **the root cause of every empty-shots failure**. `_promoted()` in `director.py` exists because of it and **raises on an unknown field name**. Use it for every new tool schema.
- `enable_thinking: false` stopped working 2026-08-19; the model reasons and then answers.
- **A `ReadTimeout` stringifies to `""`.** A failure reported by `str(exc)` surfaces as a blank and reads as a bug in the application. Report by exception class and elapsed time.

**The design consequence, already encoded as AD-38:** asking and writing are *separate tools*, never one tool with an optional field. On a model that drops fields silently, **an optional field and a dropped field are the same bytes**.

### ffmpeg and encoding, measured 2026-08-21

On this machine, 1056×608 source, 24 fps, nine-stage filter chain:

| Job | Cost |
|---|---|
| Single still frame, full chain | 170 ms |
| **Whole 4.5 s shot, half dimensions, `ultrafast` CRF 28** | **270 ms** |
| Whole 15 s shot (longest this pipeline makes) | 660 ms |
| 2 s window around an `xfade` | 143–187 ms |
| 4.5 s driven by 108 timed `sendcmd` commands | 90 ms |
| Same, via **h264_nvenc** | **403–527 ms — slower** |

- **A still frame is not meaningfully cheaper than the whole clip.** Preview is a looping clip for that reason.
- **NVENC loses on sub-second jobs** because encoder init dominates. This may invert at export length; it has not been measured there and the preview result does not transfer.
- **Texture filters go before `pad`.** Measured on a 4:3 source into a 16:9 target: after `pad` the letterbox bar samples RGB `(1,1,5)`; before `pad`, `(0,0,0)`. Grain and vignette after the pad dirty the bars.
- **`lut3d`'s `file=` breaks on an absolute Windows path too, and the fix is not the one `sendcmd` needs.** Measured 2026-08-25 on ffmpeg 7.0. Unescaped it fails with `Error applying option 'clut' to filter 'lut3d': Invalid argument`, naming neither the path nor the problem. The unquoted escape `C\\\:/x/y.cube` *and* the cwd-relative form both work — until the path contains a `,` or a `;`, which the filtergraph splits on. **The single-quoted form with the colon escaped, `'C\:/x/y.cube'`, survives spaces, commas, semicolons, brackets, percent signs, ampersands and equals signs**, and is what `effects.py` writes. Nothing survives an apostrophe in the path; that is refused by name. `lut3d`'s `file` also has **no timeline flag** — only `interp` and `clut` are runtime-settable, so a LUT cannot be swapped by `sendcmd`. That is an Epic 10 constraint: a binding can drive a grade's parameters but never the LUT itself.
- **An export is not byte-reproducible, and the encoder is why.** Measured 2026-08-25: eight renders of one identical grained filter chain through this project's own `libx264 -preset veryfast` produced **two distinct pictures**; forcing the encoder to a single thread collapsed them to one, and the filter graph's own output was bit-identical across ten runs either way. Multi-threaded libx264 is not bit-exact on high-entropy input, and grain is what makes an export entropic enough to show it. **A determinism claim must be asserted on the filter graph's frames, never on the encoded file** — and "an empty stack exports byte-identically to today" is a claim about the argv and the chain, not about the bytes of the mp4.
- ~~**`sendcmd=f=` breaks on an absolute Windows path.** The drive-letter colon parses as a filter option separator and fails with `No option name near 'frame'` — naming a filter that is not the problem.~~ **Re-measured 2026-08-27 and the cause was wrong.** A plain forward-slash absolute path renders fine, drive-letter colon and all, spaces included. A **comma, `=` or `&`** in the path is what breaks it, and `lut3d`'s single-quoted colon-escaped form survives those for `sendcmd` too — the two filters do not need different remedies after all. Running ffmpeg with cwd set to the script's directory and passing a bare relative filename still works and is what ships, because a generated name needs no escaping and keeps an absolute path out of the composed chain, which is the preview cache key. It is a choice, not the only escape from a parser bug that does not exist.
- **Driving both `crop` or `scale` dimensions aborts ffmpeg.** Measured 2026-08-27: a `sendcmd` moving `w` *and* `h` gives `Assertion best_input >= 0 failed at fftools/ffmpeg_filter.c:1923`, **rc 3 and a 48-byte truncated file**, at any pair of timestamps. One dimension alone is fine, and a zoom is never one dimension alone. **ffmpeg's `T` (runtime-settable) flag is therefore not the test for drivability** — `crop` and `scale` both carry it on `w` and `h`. A drive table built from the flag would have shipped a punch-in that crashed every export it appeared in.
- **Listing the looks folder reads every `.cube` to its end, and that is not free.** A truncated LUT carries its `LUT_3D_SIZE` header on line 1, so no header or size heuristic can tell a half-copied 33-cube from a whole one — only counting the data lines against `N³` can, and that means reading the file. Measured 2026-08-25 on the 48-file, 44.2 MB pack: **221 ms cold, 23 ms warm** (a header-only sniff was ~1 ms). The cost buys a refusal by name instead of `Error initializing filters` at export. **Do not call `discover_luts` per request** — read it once and hold it, or the folder is re-read on every keystroke that opens a picker.

### Effects, measured during Epic 9

Both figures below existed only in commit messages until 2026-08-26 and could not be found by a
`grep` over the planning artifacts. That is Epic 8's retro item R1 recurring: **a number measured
and then left in a commit subject is a number nobody will find.**

- **A preview is 78.7 ms, not a second.** Median from the change that invalidates a preview to a
  playable clip, against FX-NFR-6's one-second budget — **2.0 ms on a cache hit**, and the join
  that lets a duplicate request wait on the render already in flight costs **0.74 µs** on the
  ordinary path. Measured in `23a00c8`. **Take dimensions must stay memoised:** AD-29 makes preview
  geometry a fact about the whole project, and without the memo every preview on a twenty-shot
  project paid **538 ms** re-probing takes. This is the number that justifies the preview route
  taking no busy check — see AD-24.
- **A boundary preview is 144.6 ms at the house delivery size, and the blend's length is free.**
  Measured 2026-08-29 by story 11.5 through the shipped route, cold, median of five: a 1.5 s window
  spanning a cut — twelve frames of the outgoing Shot, a twelve-frame blend, twelve frames of the
  incoming Shot — at 1056×608 delivery, so 528×304 on screen. **2.8 ms on a cache hit.** On the
  128×72 test fixtures the same window is **88.4 ms**, and *doubling the blend to 24 frames costs
  0.7 ms* (89.1 ms) — the two-input graph's setup dominates, not its length, which is why the
  margin is a fixed frame count rather than something tuned. All of it is inside FX-NFR-6's
  one-second budget and consistent with the 143–187 ms this document already records for a 2 s
  window around an `xfade`.
- **A one-sided transition costs a Shot's own preview +3.2 ms, and a blur ramp +26.5 ms.** Measured
  the same day on the same fixtures: 73.0 ms untreated, 76.2 ms with `dip_black` (one `fade`
  filter), 99.5 ms with `blur_ramp` (a `gblur` on every frame plus a compiled `sendcmd`). The gap
  between the two forms is worth knowing before anyone tunes `ONE_SIDED_TRANSITION_FRAMES`: the
  fade's cost is flat and the blur's is per-frame.
- **The band panel is 516.6px in a 626px rail, with 225px below the fold — the spectrum strip is
  36px of that, and it cost 13.** Measured in a real browser 2026-08-27, with seven numeric inputs,
  the drive control, the canvas and the panel's own notes. It was **553px** before a four-line gate
  note was split so its second clause is drawn only when a gated setting actually holds a tuned
  value (−49px), and **503.6px** immediately before story 10.2. The strip's 36px canvas and its 7px
  margin are 43 of those, and 30 came back because the sentence it replaced — which named *two*
  missing canvases — became one naming the drive readout alone. **Prose was 37% of this panel
  once**, which is the whole argument: a canvas that *replaces* an explanation is cheaper than one
  that joins it. The bound panel, which draws no "what is still needed" block, is 454px with 46px
  below the fold. The first build of the panel overflowed so far that Selenium reported `element
  not interactable` — a Director meets that as a control they cannot reach, and it reads like a
  layout fault when it is a scroll distance. `panelBelowFold` and `railScrollHeight` are recorded
  by `tests/e2e_band_panel.py` so the density is a number to argue with rather than a screenshot to
  notice.
- **The Drive readout is 63px under the Monitor — 34px of canvas, 14px of caption — and it costs
  the Monitor 28px and the tracks 35px.** Measured in a real browser 2026-08-27 at 1280x1024, by
  hiding the figure and re-measuring at the same instant: the Monitor is 218.2px with it and
  246.2px without, the tracks 272.8px and 307.8px. **Comparing against another Shot, or against an
  earlier moment on the same one, is not a measurement of this row** — it answered a 98px
  difference, because the inspector's own height moves with it.
- **A `hidden` grid child does not hold its track, and that collapsed the Monitor on every unbound
  Shot.** `.timeline-main`'s four rows were auto-placed, so hiding the readout let the tracks
  viewport fall into the readout's own `auto` track while the `minmax(140px, 1.25fr)` track meant
  for it sat empty, and the Monitor dropped to its 120px floor — on every Shot in every project
  that carries no binding, which is nearly all of them. Every automated gate passed over it. Each
  child of `.timeline-main` now names its own `grid-row`.
- **A canvas hairline drawn before a filled envelope is a hairline that is not there.** The
  readout's rest line was drawn first and the envelope's fill, whose bottom edge *is* that line,
  painted it out along its whole width: 10 `--dim` pixels where 1,119 belong. The spectrum strip
  made the same mistake one slice earlier with a 1px bar and a curve's baseline. Draw the datum
  last, and **count the pixels** — the census is what saw it.
- **Below the Trigger Floor a `punch` drive is exactly zero, so colour alone cannot mark a silenced
  passage.** Its own line lies *on* the rest hairline in the same token, so "the floor shut this"
  and "nobody measured here" draw identically. The readout lays a 4px `--dim` ground bar under
  every silenced run instead — a state with width, which grows as the Director raises the Floor.
  Removing the bar left every assertion in the QA script passing until the census learned to count
  the bar's own row separately.
- **The Drive readout's read is 16.3 ms on a 202-second master, and 1.8 ms for a Shot with no
  binding.** Measured 2026-08-27 through `GET .../shots/{id}/drive` in-process, medians of 20:
  **1.79 ms unbound, 16.25 ms bound**, with a 4.8 KB payload for a 4-second Shot (4.2 KB at 8
  seconds of song, 8.4 KB at an 8-second Shot). The cost is the same SHA-256 of the song plus the
  sidecar read the preview pays, and it is gated on `stack_is_driven` for the same reason — an
  unbound Shot must not pay it merely by being selected. Two orders inside FX-NFR-6's one second.
- **`band_width`'s minimum draws a region 3.2px wide, not 4.4.** The strip's canvas is **183px** on
  this rail — not the ~220px story 10.2's spec estimated — so the minimum band is under four pixels
  and has no interior at all. Both edge handles keep about 8.6px of grip there, because each is
  capped inward at half the region and keeps its full reach outward (R-16), and the body drag owns
  every pixel the handles do not, the ground outside the region included. At the minimum width
  *and* zero softness the softness handle has three pixels left, which is below the floor, so it is
  withdrawn and the panel names the box that still sets it.
- **Never offset a `beaty_wav_bytes` fixture by a whole number of half-seconds.** That waveform is
  **exactly periodic at 0.5 s**, so two Shots whose song offsets differ by a multiple of it are
  driven by an identical signal and no assertion can tell them apart. Measured 2026-08-28, and it
  already had a victim: `test_two_shots_with_one_binding_are_driven_by_their_own_stretches_of_the_song`
  places its Shots **4.0 s apart — eight whole periods** — and passes only because `punch`'s running
  average starts cold at the song's first tick, so the two scripts differ over roughly the first
  second and are equal after it. Its real margin is one second of song, not the whole clip, and a
  mutation shifting a Shot by any multiple of 0.5 s is invisible to it. Use an offset that is not a
  multiple — 2.75 s and 3.95 s are the ones the newer fixtures use, and both say so in their
  docstrings.
- **A driven clip can outlive the analysis, and it is one project in ten.** An envelope holds
  `ceil(seconds × 30)` rows while the export's last clip ends on the 24 fps cumulative grid, up to
  half a video frame past the song. Swept over every song length from 1 to 600 s at 1 ms resolution:
  **61,018 of 599,001 lengths** put the final clip's last tick past the final analysed row. Which
  side a project lands on is a rounding accident of its song's duration, which is why
  `held = min(tick, len(drive) - 1)` holds the last measured value rather than falling to nothing.
- **Do not give the shared stub DOM a `getContext`.** It is tempting — no pytest test can execute a
  canvas drawing without one — and it is a trap: it would silently switch `drawEffectBandStrip` on
  inside dozens of existing workspace tests, which then die on the missing `window.getComputedStyle`.
  The answer that shipped 2026-08-28 is a **separate, opt-in recording context**
  (`recorded_drawing` in `tests/test_frontend_contract.py`): the drawing function is pasted into a
  node module and called with a Proxy that appends every method call and property write to a log. It
  **records and simulates nothing** — no paths, no state machine, no pixels — and it is fed
  `api.js`'s own plan, so the geometry under test is the geometry that ships.
- **A `sendcmd` at a bare instance name is discarded at rc 0 — the fifth of these.** `xo sigma 20`
  where `gblur@xo sigma 20` belongs prints *"ret:Function not implemented"* at `-v verbose` and
  **nothing at `-v error`**, with the right frame count and a byte-identical picture.
  `avfilter_graph_send_command` matches against the **filter's** name, not the label alone. Found
  2026-08-29 while writing the one-sided blur ramp.
- **A blur cannot be measured on a flat colour field, and every fixture take is one.**
  `synthesize_take` writes `color=c=red`; a Gaussian blur of a uniform field *is* that field, so a
  working ramp reads as a discarded one. `synthesize_detailed_take` (`testsrc2`) exists for the one
  test that needs detail. The general rule: **a fixture must contain the thing the filter acts on**,
  which is the same failure as a fixture that makes its own defect impossible, one layer down.
- **Compare filter-graph frames, not encoded frames, when asking what a filter changed.** Measured
  2026-08-29: a treatment that moves 11 frames reads as **21** moved frames once encoded, because
  libx264's lookahead spreads a change backwards across the GOP. Replace the encoder with
  `-f framemd5` and the count is exact. This is why R-20 says a determinism claim is made about the
  graph and never about the mp4 — the same fact, met from the other side.
- **`xfade` emits `yuv444p`, and `concat -c copy` joins it to `yuv420p` without a word.** Measured
  2026-08-29 and reproduced independently. Two legs each ending `format=yuv420p`, blended by
  `xfade` with nothing after it, encode as **`yuv444p` / High 4:4:4 Predictive** — rc 0, correct
  frame count, correct geometry, and a pixel format no other intermediate in the export uses. Then
  `ffmpeg -f concat -c copy` **accepts the mismatch at rc 0 with no warning at `-v warning`** and
  writes a container **declaring `yuv420p` / High** over frames that are not. The header lies about
  a third of the file.

  The fix is one stage: a transition segment closes with `setsar=1,format=yuv420p`. It deliberately
  carries **no `fps`** — a rate filter downstream of a framesync filter is the exact shape
  `BRANCH_FRAME_GUARD` exists to compensate for.

  **This is the fourth distinct wrong-output-at-exit-code-0 this project has met**, after a branched
  chain losing one frame (Epic 9), a `sendcmd` at a missing label changing nothing (Epic 10), and
  `xfade` truncating to its shorter leg (Epic 11). The pattern is now established well enough to
  state as a rule: **in this pipeline, ffmpeg's exit code is evidence of nothing.** Assert the
  rendered artefact — frame count, pixel format, checksum against a control — never the return code.
- **A `sendcmd` at a bare instance name is discarded at rc 0 — the same failure as a missing
  label, from a different mistake.** Measured 2026-08-29 while story 11.4's blur ramp was written,
  and it is the fifth wrong-output-at-exit-code-0 this project has met. A command written
  `xo sigma 20` where `gblur@xo sigma 20` belongs reports **`Command reply for command #0:
  ret:Function not implemented`** at `-v verbose`, **nothing at all at `-v error`**, rc 0, the
  right frame count, and a picture **byte-identical** to the undriven chain.
  `avfilter_graph_send_command` matches a target against the *filter's own name* and returns
  `ENOSYS` when nothing matched — so an instance label alone reaches nothing, exactly as
  `StageContext.named`'s docstring recorded for `b0` in Epic 10. **The target must carry the class
  and the `@`**, and the discipline that catches it is unchanged: assert the target string appears
  in the chain the same call composed.

- **A blur cannot be measured on a flat colour field, and the test fixtures are flat colour
  fields.** `synthesize_take` writes `color=c=red`, which is right for every other treatment in
  this suite: a fade, a grade and a `sendcmd` ramp all move a flat picture. A Gaussian blur of a
  uniform field is that uniform field, at any sigma — so the first run of
  `test_a_one_sided_blur_ramp_…` read a working ramp as *"the ramp was discarded"*. This is the
  fixture-makes-its-own-defect-impossible shape running the other way: the fixture made a **pass**
  impossible, and a control that also did nothing would have hidden a real failure just as well.
  `synthesize_detailed_take` (`testsrc2`) exists for that one test.

- **A treatment's "bit-identical outside the ramp" claim must be made on the graph's frames, not
  on the encoded intermediate.** Measured 2026-08-29 on a 96-frame clip with a 12-frame ramp on
  its tail: comparing `framemd5` of the two **encoded** files reports **21** frames moved;
  comparing the two **filter graphs** (`-f framemd5` in place of the encoder, everything before
  `-c:v` untouched) reports **11**. libx264's lookahead spreads a change backwards, so the extra
  ten are the encoder and not the filter. This is R-20's rule met in a new place — a determinism
  claim belongs on the filter graph — and the eleven rather than twelve is right too: the ramp's
  first frame is the identity by construction.

- **A one-sided transition costs the export nothing new.** It is one more single-input filter in a
  chain that was already being encoded and opens no second input, which is where the +39.4 ms
  fixed cost of a paired segment goes (below). It adds no entry to `plan.clips`, no frame to any
  count and no timeline length: `assembly_plan` is not consulted and `assembly.py` is unchanged by
  the whole of story 11.4.

- **A transition segment costs ~40 ms per overlap, and almost nothing per second of dissolve.**
  The debt the spine's *Deferred* section and §6 both recorded — *"full-resolution export cost of a
  reactive binding and of transition segments, measure before E and F merge"* — is now discharged on
  both halves. Epic 10 measured the binding half (+1.1 ms, +0.4 %). This is the transition half,
  measured 2026-08-28 on **real H3 takes** (1056×608, 24 fps, yuv420p) at the export's own draft
  encoder (`libx264 veryfast crf18`), arms **alternated within each pair** and pooled, n=30 per arm
  per length:

  | segment | plain trim | `xfade` segment | delta |
  |---|---|---|---|
  | 12 frames (0.5 s) | 77.6 ms | 120.6 ms | **+43.0 ms** (+55.4 %) |
  | 48 frames (2.0 s) | 114.0 ms | 167.6 ms | **+53.6 ms** (+47.0 %) |

  Fitted across the two: **+39.4 ms fixed per segment, +0.30 ms per extra frame.** Within-arm spread
  was 8.4/21.1 ms at 12 frames and 14.2/37.7 ms at 48, so the delta clears the noise at both lengths
  — but only just at 48, which is why the fit uses both rather than either alone.

  **Two consequences for Slice F.** The cost is the *second input's open and decode*, not the blend:
  quadrupling a dissolve's length costs ten more milliseconds, so **transition length needs no
  ceiling on cost grounds** and a Director can use long dissolves freely. And the per-export figure
  is per *overlap*, not per second — twenty overlaps is under a second added to a whole assemble.

  **Do not quote the percentage on its own.** It is 55 % of a 77 ms segment, not of an export, and
  this repository has already had one export measurement lie in the other direction: a single 25-run
  round manufactured a 30 % regression out of nothing. Both numbers here come from alternated,
  pooled rounds for that reason.
- **The browser harnesses are not in `pytest`, and one of them was broken for four slices before
  anyone noticed.** `pyproject.toml` sets `testpaths = ["tests"]` and pytest's default
  `python_files = test_*.py`, so every `tests/e2e_*.py` harness is **outside the suite** — a green
  `uv run pytest -q` says nothing about any of them. `e2e_effects_tab.py` failed from Epic 10's
  first slice (an exact-equality predicate on a stack entry the wire now carries `bindings: []`
  on) and four consecutive slices reported green gates over it. **Run the harnesses that touch what
  you changed, and say which you ran** — the list is in `docs/OPERATIONS.md`, and a harness missing
  from that list is a gate nobody can find. This is Epic 9's flaky-release-gate lesson in its other
  form: there, a gate that failed under load and passed alone; here, a gate that was never in the
  gate.
- **A bound Shot's preview cache hit is 20.5 ms, not 2.1 ms — and only a bound Shot pays it.**
  Measured 2026-08-27 on a 3-minute master (7.9 MB) with a 326 KB envelope sidecar: **2.08 ms
  unbound, 20.46 ms bound** on a cache hit, and 74 ms for a bound first render. The cost is the
  SHA-256 of the song plus the sidecar read that `preview_fingerprint` now needs, because it
  composes the chain itself and cannot name a bound Shot without the envelope. Both render paths
  gate on `stack_is_driven` — an *enabled* card carrying a binding — so a project with no bindings
  reads no sidecar and pays nothing. Still two orders of magnitude inside FX-NFR-6's one-second
  budget, and recorded here rather than left in a commit subject, which is Epic 8's retro item R1.
- **Effects cost the export nothing measurable.** Wall-clock for a no-effects export, the effects
  build against a `git worktree` at the previous commit with its own `PYTHONPATH`, arms alternated
  and pooled: **median 0.2556 s against 0.2545 s — +1.1 ms, +0.4 %**, p25 identical to four decimal
  places, **n=160 per arm**. Measured in `0996128`. **Pooling was necessary rather than tidy:**
  single 25-run rounds gave the *same* arm medians of 0.343 s and 0.266 s, so a short round on this
  machine can manufacture a 30 % "regression" out of nothing. Alternate arms and pool before
  believing an export timing here.

### Manifest sizing

`project.json` is **110–190 KB** today and rides a **2-second poll**. A 3-minute Song Envelope at 30 Hz with 8 bands is **405 KB**, measured through the shipped extractor — two to four times the whole manifest, and 469 KB on a real 202-second master. It is a sidecar file (AD-20), never a manifest field. Apply the same test to anything else large.

*Corrected 2026-08-26.* This line said **~750 KB**, "four to seven times the whole manifest". **Two earlier figures were wrong in opposite directions and neither should be requoted as live:** ~750 KB was AD-20's original estimate, and 1.13 MB came from a synthetic probe that filled more arrays with random floats than a real envelope carries. **405 KB is the measured one** (2026-08-24, ruling R-8). The sidecar conclusion is unchanged by the correction — the ratio moved, the decision did not. Since 2026-08-24 the browser is served only the part it reads (`beats`, `onsets`, `band_average`, `band_edges`); the per-frame series, 98 % of the file, never leaves disk. Epic 8's action item 18 said to correct this figure *everywhere* and reached `docs/project-context.md`, `docs/ROADMAP.md` and the effects `ARCHITECTURE-SPINE.md` — this file, the one a new session opens first, is the instance it missed.

---

## 4. Caveats — process

### This repo moves fast enough to invalidate its own artifacts

**Three premises in the Treatment planning pass turned out to be wrong**, each caught only by reading current code:

1. The Brief was described as needing promotion into the pipeline. It was **already in it** — `timeline.py` puts `creative_brief` in the project dump at three call sites.
2. The multiview gate was described as refusing non-character assets. It had **already been widened** to `character`/`prop`/`setting`.
3. Structure analysis was described as Timeline-only. **`Analyze structure` is already on the Song page**, beside `Build treatment →`, and `align-lyrics` already proposes section boxes from timed `[Tag]` blocks.

An eight-day-old constraint was stale in all three cases. **Re-run any constraint before planning around it.** The `Deferred` sections of both architecture spines name several that still need re-verifying — in particular whether the Krea reference sheet still runs one of three sampling stages.

### A repair to the frame grid is reviewed, not just merged alone

*Item 80, from Epic 11's retrospective.* §2 has always said that work touching `assembly_plan` and
the cumulative frame grid merges alone. It said nothing about reviewing it, and I read "merges
alone" as sufficient.

**Epic 11's story diff got four review lenses, which is what found a defect that shipped the wrong
Shot in a delivered video at HTTP 200 with every gate green** — 2761 tests, ruff, both JS
parsers, six mutation passes, four browser harnesses, and real ffmpeg on the artefact in three
separate slices. **The repair for that defect got no review at all**, and introduced two
regressions plus six mutations that survived the entire suite: a blend that composed the day before
silently refused, a lock that made every Shot's predecessor un-fadeable, and two guards mutually
redundant across every fixture so the suite could not say which did the work.

**The rule:** a change to code a review pass just found a defect in gets its own review pass before
it merges, scoped to the *fix* commits rather than to the story diff again — re-reviewing
the story diff only re-derives known findings. Run the lenses blind to each other; in Epic 11 the
adversarial and edge-case lenses independently reached the same boundary-preview defect and that
agreement was worth more than either report.

**And verify every subagent finding against the primary source before acting on it.** Two of ten
edge-case findings changed under checking — one was pre-existing rather than a regression
(proved by running the same geometry against a worktree at the pre-fix commit), one could not be
reproduced at all.

### A claim in a spec that was not executed is a hypothesis, and must say so

*Item 81.* Across Epics 9, 10 and 11 **every wrong claim was reasoned and every right one was
run.** Diagnoses come from opening the code and none has been wrong. Remedies, citations and counts
come from a mental model: roughly twenty wrong remedies, `AD-19` cited where `AD-18` was meant at
both of its occurrences in one spec, and **five consecutive baseline suite counts miscounted**
(2698 for 2699, 2728 for 2729, 2761 for 2762, 2780 for 2783, and 2805-passing quoted as
2806-collected), two of them written *inside the paragraph correcting the one before*.

Three epics of "prescribe less" moved the errors rather than stopping them: Epic 9 said state
defects and decline to prescribe, Epic 10 found that relocated them, Epic 11's F5 spec named only
the invariant and the implementer corrected three of that spec's own **evidence** claims.

**So: run the command and paste its output, or write "not re-derived" beside the number.** Applies
to a baseline count, a complexity figure, a line count, a range of ruling ids, and any citation of
an AD or a ruling by number.

**Running it is necessary and not sufficient, and this is the sharper half.** For Epic 11's `api.js`
port I built a prototype, swept it against the real engine over 5,675 boundaries, found zero
disagreements, and put that in the spec as measured fact. The prototype was wrong:
`_paired_transitions` mutates its entry list as it goes, so a blended boundary changes what the
next one is measured against, and **both harnesses set exactly one transition per plan** —
structurally incapable of producing the disagreement. That is a fixture that makes its own defect
impossible, in the evidence section. **A measured claim is worse than a reasoned one in one
specific way: reasoning announces itself and a number does not.** Before quoting a sweep, state
what the harness could not vary and check that list against the mechanism under test. A sweep that
has never produced a single disagreement has not been shown to be capable of one.

### When you amend a record, grep for the sentence's siblings

*Item 82.* Three instances in Epic 11, and one of them was committed inside the pass whose subject
was this failure:

- **Story 11.2's `Then` clause** about the Overlap band was amended and the **`And` clause one line
  below it** was left saying the opposite — in the fix pass whose subject was sibling
  copies. Found by the retrospective.
- **AD-28's 2026-08-29 amendment** announced a change to its own title and Rule and made neither,
  so the heading and the first sentence stated the opposite of what shipped for a day.
- **`api.js`'s `overlapRemovalToasts` comment** contradicted the code eight lines below it, with a
  test in the same commit asserting the code — so the commit contained its own disproof.

In every case the amendment was written; what was missed was the copy beside it. `grep` the phrase,
not the file.

### Other agents work in this repo concurrently

Blanket `git add` has swept in-flight planning work into unrelated commits — the effects PRD, architecture spine and UX spines were all committed by `9c3db45` *"Vocal type, per-line singer marks, and character slots"*. **Stage explicit paths, not `-A`.** Check `git status` for someone else's modified files before committing.

### The routes are in `routes/`, and a new one almost certainly belongs there

**Added 2026-08-27.** `src/music_video_producer/routes/` is a package of eight modules holding **60 of this application's 76 routes**, split out of `create_app` on 2026-08-26 (`4a1a4f6`, `e6f6b23`). Nothing in this document, the effects spine or `BUILD-ORDER.md` knew that until now, and all three still named `app.py` as the route file.

**Read `routes/__init__.py` first** — its docstring is the authority on what is where and why, including why the modules use `register(app)` rather than `APIRouter`/`include_router` (on FastAPI 0.141.1 an included router stops `app.routes` being a flat list, and three of this repo's guards walk it off a live `create_app()`).

Where the effects work actually sits today, verified against the code on 2026-08-27:

| Route | Module |
|---|---|
| `GET`/`PUT .../shots/{id}/effects` | `routes/shots.py` |
| `GET /api/effects/catalogue` | `routes/unsorted.py` |
| `POST .../song/analyze`, `GET .../song/envelope` | `routes/song.py` |
| `GET .../timeline/snap-targets` | `routes/timeline.py` |
| `PUT /api/projects/{id}` (`replace_project`) | `routes/project.py` |
| `POST .../shots/{id}/preview` (`render_shot_preview`) | **`app.py`** — pinned |
| `POST .../assemble` (`assemble_project`) | **`app.py`** — pinned |

The sixteen still in `app.py` are not there by preference. Fifteen are held by tests that monkeypatch a module-level name in `music_video_producer.app`'s namespace — a route resolves such a name against the globals of the module it is *defined* in, so moving it makes the patch invisible — and the sixteenth is `index`. `render_shot_preview` is pinned by `build_effect_stages`, `trim_args` and `probe_take_args`; `assemble_project` by `trim_args` and `concat_args`. **Epic 10's implication:** a binding route hangs off a Shot and belongs in `routes/shots.py`; anything reading the envelope belongs in `routes/song.py`. The drive readout is the one judgement call — if it renders through `build_effect_stages` under a patched name it will be pinned in `app.py` beside the preview, and that is a reason to serve compiled values rather than to render.

Helpers are a separate question from routes: `_adopt_shot_effects`, `_adopt_job_measurements`, `_adopt_song_vocal_type` and the rest are all still defined in `app.py`, and the route modules import them back from it. That import direction is deliberate and explained in the package docstring.

### The generic project PUT is this codebase's recurring guard hole

`replace_project` — in **`routes/project.py`** since the split, not in `app.py` — carries comments counting the **sixteenth** finding of the same hole *(sixteenth as of 2026-09-03: the Brief's recovery slot and lock, which cost no new line because that adoption loop is driven off `DOCUMENT_LABELS`; fifteenth as of 2026-08-29 was the Transition pair, `_adopt_shot_transitions`, guarded in the same commit as the field. This said **fourteenth** until 2026-08-30 while §8 of this same document already said fifteen — one number, two places, and they disagreed for a day, which is why both were moved together this time.)*. The established remedy is an `_adopt_*` helper that takes the field off the **stored** project before the body is trusted — `_adopt_song_recovery_slots`, `_adopt_song_vocal_type`, `_adopt_song_analysis`, `_adopt_expansion_maps`, `_adopt_shot_effects`, `_adopt_job_measurements`.

*Corrected 2026-08-27.* This said ~~"`replace_project` in `app.py` carries a comment counting the **sixth** finding"~~, and both halves were wrong. The route moved; and the count reached **fourteen** — the envelope pointer was the twelfth, the Effect Stack the thirteenth (Epic 9, `_adopt_shot_effects`) and the record of what an export looked like the fourteenth. **Re-counted 2026-08-27 and it is fourteen, not thirteen:** the envelope-pointer guard (`_adopt_song_analysis`, 2026-08-24) had been numbered *seventh*, a number `character_slot` already held since 2026-08-21, so one instance was invisible in the route's own ledger. Dated from `git log -S` on each comment, it falls twelfth; the Effect Stack is the thirteenth and the export-look record the fourteenth. **The ordinals in that route are instance numbers, not a running total:** `routes/song.py`'s "the sixth time that route has had to be defended" is `Song.vocal_type`, instance six, and it is correct and stays six. Only a claim about *how many times in total* has to move.

**Every new manifest field in either feature must be adopted this way**, and the adopt test belongs in the same commit as the field, not after. AD-16, AD-36 and AD-41 all say so. This is the pattern most likely to be skipped and most expensive to skip.

### Test and tooling traps

- Nested `uv run` deadlocks on the environment lock. Call `.venv` python directly inside harnesses.
- Mutation testing under concurrency: mutate in a worktree, and set `PYTHONPATH` or the editable `.pth` imports the live checkout and everything "survives".
- Restore mutated files with `write_bytes`, not `write_text` (CRLF), and purge `__pycache__` before same-length mutations.
- Parallel agents sharing a scratchpad have clobbered each other's harnesses; a poisoned baseline validates itself.

---

## 5. The cross-feature obligation

**Effects Story 8.1 shipped first** — 2026-08-24 — so **Treatment Planning owns the shared analysis trigger** (R-17). *Amended 2026-08-27: the tense. This read ~~"ships first"~~ as a prediction; it is now a fact, and the obligation it creates is unchanged.*

Two different analyses want the same song and the same moment — the one point where the Director is plainly willing to wait:

- Effects `FX-1` — RMS, onsets, beats, BPM, per-band envelopes → a sidecar.
- Treatment `TP-18` — Whisper transcription, `[Tag]` block alignment, proposed section boxes.

Treatment Story 16.2 must present **one moment and one indicator** covering both, **without merging the computations** — they stay separate functions in separate modules (AD-40). Each half is **skipped when already current** (AD-47), and since `FX-1` analyses automatically on song import, the common case is that one half is already done and the job finishes fast. **That fast finish must read as "already done", not as a failure to run.**

Nothing in the effects work changes because of this. ~~Story 16.2 cannot start before 8.1 ships.~~ **Story 16.2's prerequisite is met** — 8.1 has shipped, and `POST /song/analyze` (R-12, `routes/song.py`) is the skippable-by-fingerprint entry point it calls.

---

## 6. Owed measurements and open items

None blocks starting. All are recorded in the spines' `Deferred` sections.

**Blocking a specific slice:**

- ~~**LUT source and licence** — blocks the Grade family shipping (effects Story 9.1/9.3). `effects.py`'s structure does not depend on it; build against a placeholder set.~~ **Unblocked 2026-08-27:** Stories 9.1 and 9.3 shipped, and nothing was licensed. `effects.py` **generates** its default pack — `write_default_luts` writes each `.cube` from `DEFAULT_LUTS`' own transforms via `cube_text`, into an empty folder, never overwriting a Director's file. The question returns only if a third-party pack is ever bundled.

**Owed measurements:**

- **Suggest Video's timeout value** — set from live runs; raising the director timeout is authorised (R-11).
- **Full-resolution export cost** of a reactive binding and of transition segments. `CM-E1` makes an export regression a defect, not a cost.
- **NVENC at export length** — unmeasured; the preview result does not transfer.
- **`populate` after Treatment ships.** Richer Briefs make richer Treatments, and a Treatment is populate's input — **planning succeeding makes populate's job bigger.** Note that `91c7120` recorded populate's first live run and fixed four defects, so some of this may already be answered.
- **Krea reference sheet sampling stages** — the "one of three" finding is stale; `res_multistep` now appears in the builders. Re-measure before treating reference fidelity as a lever.

**Found while building treatment Slice A (2026-09-03), needs a Director ruling before anyone builds it:**

- **FR-16 still has a hole, and it is not the one Slice A closed.** The Brief is now protected on every path.
  `treatment` and `style_bible` are not: `PUT /documents` assigns their text from the body with no capture and
  no confirmation, so a Director who clears the Treatment textarea and clicks **Save document** destroys it with
  nothing kept — the exact threat model the 2026-09-03 ruling identified for the Brief, on the two documents
  that were supposed to be the protected ones. Slice A deliberately did not close it, because the obvious fix is
  wrong: capturing on save for those two would let one click of Save spend the single slot that exists to protect
  the Director *from the model*. The candidate remedies are a clearing confirmation on the document save
  (`songContextClearing`'s shape, and the song context asks exactly this question already) or a second slot, and
  which one is a decision, not an implementation detail. **Measured, not reasoned:** a `PUT /documents`
  carrying `""` for all three documents left `creative_brief_previous` holding the Brief while both other slots
  stayed empty, and `POST .../documents/treatment/restore` answered 409. No browser run has been made against
  it, and no test in the suite asserts either half — the gap is recorded here rather than pinned in code,
  because pinning the current behaviour would be asserting the defect.

**Open questions with owners:**

- Effects: attribution-free — 3 open, 1 UX (transition catalogue size), 2 architecture.
- Treatment: 3 open — 1 UX (planning-turn indicator), 2 architecture (undo depth, bounded thread per turn).

---

## 7. Standing design laws

Violating one of these is a defect even when the code works.

1. **The frame grid is inviolable.** The assembled video matches the song within one frame, for every combination of effects and transitions. Effects `FX-NFR-1`.
2. **One engine describes an effect.** Preview is the export's own filter chain at smaller dimensions — never an approximation. Effects `FX-NFR-3`.
3. **Nothing renders without confirmation.** Every GPU spend passes an explicit confirmation naming what will run.
4. **Never silently destroy a creative document.** FR-16. Since Treatment Slice A shipped (2026-09-03) it holds against every *machine* write for all three documents, and the **Brief** holds against the human save too — its slot is filled by the Director's own save rather than by an applied reply, because no reply can write it (AD-41, amended). **It does not yet hold against the human save for `treatment` and `style_bible`**: measured 2026-09-03, a `PUT /documents` carrying empty text for all three left the Brief recoverable and both of the others gone with the restore answering 409. See §6 — that gap needs a ruling, not an implementation.
5. **Derived beats stored.** Media presence, envelope validity, preview staleness, proposal staleness, effects presence — all computed at read time, never a stored flag that can outlive its condition. AD-11 and everything that cites it.
6. **Consent is explicit on the wire, never ambient.** Planning Mode's session consent is a *client* affordance; every request still carries consent. AD-35.
7. **The palette is closed at six accents.** `--acid` complete/action · `--amber` running/caution · `--red` error · `--cyan` approved · `--blue` transitions and reactive bindings · `--dim`/`--muted` inert. A seventh needs the argument made from scratch.
8. **No progress percentage for work whose progress cannot be measured.** Renders and language-model passes both. Elapsed time only.
9. **Local-first.** No cloud model, no account, no telemetry — including for planning, however much the local model's limits show.
10. **Generated render inputs are pure and compared as text.** Filter chains, concat lists, `sendcmd` scripts — a pure function of the manifest, asserted by string comparison, exactly as ffmpeg argv already is.

---

## 8. If you only read one thing

Start on **treatment Slice B or Epic 17** *(corrected 2026-09-03, when treatment Slice A shipped; this said ~~Start on **treatment Epic 12 or Epic 17**~~, itself a 2026-08-30 correction of ~~Start on **effects Epic 10 — Slice E**~~ and before that of ~~Start on **effects Story 8.1**~~)*. **This one sentence has now been stale three times, and it is the sentence whose whole job is to be read alone** — which is why §1's table, §2's heading and this line are three copies of one fact and were corrected together rather than one at a time. Put a new route in `src/music_video_producer/routes/`, not in `app.py`. Before writing any LLM-facing schema, read §3's model envelope and use `_promoted()`. Before adding any field to `Project` or `Shot`, write its `_adopt_*` guard and its test in the same commit — that hole has now been found ~~fourteen~~ ~~fifteen~~ **sixteen** times in one route *(the Brief's slot and lock, 2026-09-03 — guarded in the same commit as the fields, as the Rule asks, and still counted; the transition pair, 2026-08-29, was the fifteenth)*. Before planning around any constraint written more than a few days ago, re-run it.
