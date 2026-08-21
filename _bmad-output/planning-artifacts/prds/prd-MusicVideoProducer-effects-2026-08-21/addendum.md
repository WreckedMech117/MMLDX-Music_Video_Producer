# Addendum — Shot Effects and Transitions

Depth that belongs downstream rather than in the PRD: mechanism, measurements, options considered, and the rationale behind rejected alternatives. Architecture and UX consume this; the PRD stays capability-level.

---

## A. Measurements taken 2026-08-21

All on the Director's machine. Source: 1056×608 (the house resolution), 24 fps, H.264. Filter chain of nine stages — crop, scale, `eq`, `colortemperature`, `gblur`, `noise`, `vignette`, `rgbashift`, format conform. Each figure is the median of two or three runs; variance was under 10 ms except where a range is given.

| Job | Encoder | Cost |
|---|---|---|
| Single still frame, full chain, full resolution | PNG | **170 ms** |
| Whole 4.5 s shot, full resolution, export-equivalent | libx264 `veryfast` CRF 18 | 610 ms |
| Whole 4.5 s shot, half dimensions (528×304) | libx264 `ultrafast` CRF 28 | **270 ms** |
| Whole 15 s shot, half dimensions | libx264 `ultrafast` CRF 28 | 660 ms |
| 2 s window around a 1 s `xfade=dissolve` | libx264 `ultrafast` CRF 28 | 187 ms |
| Same with `xfade=circleopen` | libx264 `ultrafast` CRF 28 | 143 ms |
| 4.5 s driven by 108 timed `sendcmd` commands | libx264 `ultrafast` CRF 28 | 90 ms |
| 4.5 s driven by an `eq=…:eval=frame` expression | libx264 `ultrafast` CRF 28 | 89 ms |
| Whole 4.5 s shot, half dimensions | **h264_nvenc `p1` CQ 30** | **403–527 ms** |

**Three conclusions the PRD rests on.**

1. **The still frame was never cheaper in any way that mattered.** 170 ms versus 270 ms for the entire shot. The 100 ms bought nothing and cost motion, Geometry, Drive, and Transitions.
2. **Hardware encoding is slower here.** NVENC took 400–530 ms against libx264's 270 ms, because encoder initialization dominates a sub-second job. This inverts at export length and should be re-measured there before any conclusion is drawn about the export path — the preview conclusion does not transfer.
3. **Timed commands are not a cost.** The `sendcmd`-driven chain measured *faster* than the static chain, because that test chain carried fewer heavy filters — but the headline is that per-frame command brokering did not register as overhead at all.

**Not yet measured, and owed:** full-resolution export cost of a reactive binding across a whole song; export cost of transition segments at delivery quality; whether reduced-dimension preview is *faithful* rather than merely fast.

---

## B. Mechanism — per-Shot Effects

Effects compose into the `-vf` chain that `assembly.py:trim_args` already builds for every clip. That chain is currently:

```
[trim=start_frame=N, setpts=PTS-STARTPTS,]      # over-render offset, when non-zero
scale=W:H:force_original_aspect_ratio=decrease
pad=W:H:(ow-iw)/2:(oh-ih)/2
fps=24, setsar=1, format=yuv420p
```

Proposed insertion points, with the reason for each:

```
trim                       # unchanged — selects the exposed slice of the take
[GEOMETRY]                 # before scale: a punch-in must sample the take's own pixels
scale                      # unchanged
pad                        # unchanged
[GRADE] [STYLIZE]          # after pad: operating in the export's own colour space
[TEXTURE]                  # last treatment: grain and vignette sit on the finished picture
fps, setsar, format        # unchanged — the concat-identity conform
```

**The one open ordering question** is whether Texture belongs before or after `pad`. After the pad, a vignette darkens the letterbox bars and grain dirties them; before the pad, a vignette's falloff is computed against the un-padded frame, which is geometrically correct but means the bars stay clean while the picture edge is darkened — arguably the right look. This needs one visual comparison during build. FX-9 states the requirement; this is the mechanism question behind it.

**Why this is nearly free.** The trim stage already re-encodes every frame of every clip — it must, because stream-copy trims cut on keyframes only and frame-accurate cuts are the entire point of the module. Effects add filter cost to a pass that already runs. There is no second pass and no second generation of encoding loss.

---

## C. Mechanism — audio-reactive bindings

Two native paths, both available in the installed ffmpeg 7.0. Neither adds a dependency.

### C.1 Timed commands (`sendcmd`) — the general case

Filters flagged `..C` in `ffmpeg -filters` accept runtime commands. Confirmed present and command-capable: `eq`, `gblur`, `hue`, `rgbashift`, `chromashift`, `vignette`, `colorchannelmixer`, `colorbalance`, `colortemperature`, `noise`, `unsharp`, `deband`.

`sendcmd` reads a commands file of the form:

```
0.0000 eq saturation 1.300;
0.0417 eq saturation 1.450;
0.0833 eq saturation 1.560;
```

One line per analysis tick, generated from the Song Envelope and the Parameter Binding. This is the general mechanism: it follows the actual performance rather than an idealized tempo, and it works for any command-capable filter.

**It satisfies FX-NFR-6 by construction.** The commands file is a pure function of `(Song Envelope, Parameter Binding, Shot window)` — deterministic, re-derivable from the manifest, and comparable as text in exactly the way the project already pins ffmpeg argv.

**Windows path trap, found during measurement.** An absolute path's drive-letter colon is parsed as a filter option separator, so `sendcmd=f=C:/…/cmds.txt` fails with `No option name near 'frame'` — a message that names the wrong filter and sends a reader in the wrong direction entirely. Reproduced and confirmed. Run the ffmpeg process with its working directory set to the script's directory and pass a bare relative filename. This is worth a comment in the code, because the failure mode is actively misleading.

### C.2 Expressions (`eval=frame`) — the tempo-locked case

`eq` and several others evaluate parameter expressions per frame against `t`. A pulse locked to a steady tempo is then a one-line expression with no generated file at all:

```
eq=saturation='1+D*exp(-K*mod(t-phase, 60/bpm))':eval=frame
```

Compact, needs only BPM and downbeat phase from the Song Envelope, and it measured identically to the commands file (89 ms versus 90 ms). Attractive for a steady electronic track; wrong for anything with rubato or a tempo change, where it drifts against the performance while the commands file does not.

**Recommendation:** build C.1 and treat C.2 as an optional optimization that must produce the same visible result or not exist. Two mechanisms that disagree is exactly what FX-NFR-3 forbids.

### C.3 The drive model, ported

From `J:\Hermes-Remote\music-visualizer-studio\public\draw-engine.js`:

- `bandWeight(layer, pos)` — weights a spectrum position by centre, width, and a softness falloff.
- `applyBand(layer, data)` — reduces the weighted spectrum to one `bandLevel`.
- `energyLevel(...)` — **punch**: `punch = max(0, raw - slowRunningAverage - 0.015)`, fed into a fast-attack/slow-release envelope, combined with a floor-relative sustained term. This is the part that matters. A limited master keeps `raw` pinned near the top, so raw level cannot flash on hits; measuring level *above its own running average* can.
- `sustainDrive(...)` — the section gate: engages after the band holds above `dbLevel` for `dbHold` seconds, survives dips up to `dbSustainTime`.

These are algorithms to port, not code to share. No Node runtime, no shared files, no coupling between the two applications.

### C.4 What was rejected

**`zmq`/`azmq` live command brokering.** The filter is present in this build (`--enable-libzmq`), and it would allow a persistent ffmpeg process streaming a live preview while parameter changes are pushed into the running graph — a genuinely live preview with no re-render. Rejected for v1: it requires a long-lived ffmpeg process, a streaming transport to the browser, and process lifecycle management, to improve on a 270 ms re-render that is already under the interaction threshold. It is the right answer only if the measured budget stops being met.

---

## D. Mechanism — transitions

### D.1 Why the overlap is the right authoring model

`assembly_plan()` already resolves overlapping Shots into layered segments, later-on-top, per the ruling of 2026-08-20. An Overlap is a region where both takes hold real frames for the same Song seconds. Today that resolves to a hard cut; a Transition is the same geometry resolved differently.

The consequence that makes this the right model rather than merely a convenient one: **no frame moves.** Clip positions are unchanged, the cumulative `clip_frames_on_grid` telescoping is unchanged, and the material is frames both takes already contain. The alternative — borrowing from the over-render margin — fails on external clips, which carry no margin, and caps transition length at something the Director cannot see.

### D.2 Keeping the join a stream copy

The naive approach chains `xfade` across the whole timeline in one `filter_complex`. That loses `-c:v copy` on the join, re-encodes every frame a second time, and adds a generation of loss to clips that carry no effects at all.

Instead, **bake each Transition into its own intermediate**. For an Overlap between A and B:

1. A renders as an intermediate that ends where the Overlap begins.
2. B renders as an intermediate that begins where the Overlap ends.
3. A third intermediate is rendered from A's and B's overlapping frames through `xfade`, at the same normalized parameters as every other intermediate.
4. The concat list becomes `[A-short, AB-transition, B-short]`.

The join stays `-c:v copy`. Frame counts still telescope — the transition segment contributes exactly the Overlap's frames. The argv stays pure and pinnable by test.

**`xfade`'s preconditions are already met.** It requires both inputs to be constant frame rate with identical resolution, pixel format, frame rate and timebase. Every intermediate `trim_args` produces is normalized to exactly that, for reasons that had nothing to do with transitions.

### D.3 One-sided transitions

A transition out with no Overlap is a filter applied to the tail of A's own intermediate — `fade=out`, a ramped `gblur`, whatever the named type maps to — with no second input and no change to frame count. It is strictly simpler than the paired case and shares no code path with `xfade`.

### D.4 The catalogue

`xfade` in this build offers transitions 0–57 plus `custom` (−1). The v1 catalogue should be a curated subset named in the Director's language, not a dump. Suggested starting point, to be settled during UX (§8 open question 3):

`fade`, `fadeblack`, `fadewhite`, `dissolve`, `wipeleft`/`wiperight`/`wipeup`/`wipedown`, `slideleft`/`slideright`, `circleopen`/`circleclose`, `radial`, `pixelize`. Blur is not an `xfade` type and is composed separately.

### D.5 Deferred: GLSL transition packs

[scriptituk/xfade-easing](https://github.com/scriptituk/xfade-easing) ports 70+ GL Transitions plus Penner and CSS easings. Its expression variant runs on stock ffmpeg through `transition=custom:expr=…`, requiring `-filter_complex_threads 1`. The project's own published timings show HD transitions at 30–900 s against 2–230 s for its patched native build — a cost this PRD has not independently measured and should not assume.

Deferred, not rejected. The pre-generated expression strings could be bundled as inert data with no build dependency, and the easing envelopes alone would improve the built-ins. It is a Phase 2/3 expansion gated on a measurement, and it needs 58 built-in transitions to prove insufficient first.

---

## E. Rejected alternatives

**MLT Framework** (the engine behind Shotcut and Kdenlive). A complete multitrack engine with XML authoring, filters, transitions, and Python bindings — on paper, exactly this feature, already built.

Rejected. Adopting it means replacing `assembly.py` wholesale, discarding the cumulative frame-grid telescoping, the over-render trim rule, the pinned argv, and the refusal reporting — the parts of this project that took the longest to get right and that encode the most hard-won knowledge. The gain would be capabilities reachable through filters already installed on the machine. It is also a heavyweight native dependency on Windows, against the standalone/local-first constraint, and it would make the export's behaviour something the project no longer fully describes.

**MoviePy / ffmpeg-python.** The project builds argv directly and asserts on it in tests. A wrapper hides the one artefact the tests are pinned to, and the tests are the reason the export is trustworthy.

**frei0r plugin suite.** The `frei0r` filter is present, but no plugins are installed on this machine — verified. Shipping the DLLs adds a GPL install dependency to reach effects the native filter set already covers.

**WebGL / CSS preview engines.** Both create a second description of what an Effect is. CSS additionally cannot express most of the catalogue — no LUT, no grain, no glitch, no transition — so it would have been a weaker vocabulary silently disagreeing with the export. The Music Visualizer Studio's own history is the argument: it shipped an ffmpeg renderer beside a browser preview, they drifted, and the renderer was rewritten to use the preview's code. FX-NFR-3 exists to make that outcome unreachable here.

**`libplacebo` custom shaders.** Present in this build with `custom_shader_path`, Vulkan-accelerated, and genuinely powerful on the Director's hardware. Out of scope for v1: it takes mpv `.hook`-format shaders rather than plain GLSL, and nothing in the v1 catalogue needs it. A real Phase 3 escape hatch if the native filter set is ever exhausted.

---

## F. Reuse assessment — Music Visualizer Studio

**Port:** `analyzeAudio()` from `src/render.js` (~120 dependency-free lines, decodes through an ffmpeg `s16le` pipe) and the drive model from `public/draw-engine.js` (§C.3). Algorithms only.

**Do not port:** the render engine itself. It is Node plus `@napi-rs/canvas`, and its subject is audio-reactive overlay *graphics* on a static backdrop — a different problem from treating generated footage. The architecture does not transfer.

**Learn from:** its preview/render drift and the rewrite that resolved it. Recorded as FX-NFR-3 rather than as a note, because a lesson that only appears in an addendum is a lesson that gets relearned.

---

## G. Downstream notes

**For architecture.** The stage-order question in §B is the first thing to settle and the easiest to get silently wrong — a wrong Geometry position is invisible in a still and obvious in motion. The transition-baking design in §D.2 is the load-bearing decision for FX-NFR-1 and FX-NFR-2 together; if it proves unworkable, both NFRs need renegotiating before the build proceeds, not after.

**For UX.** Three surfaces need design that the PRD deliberately does not specify: the band selector shown against the Song's own spectrum (FX-13), the Drive readout beneath the preview (FX-22), and the blue Overlap treatment on the timeline (FX-16) — which must read as a transition rather than as an error state, given that an overlap is currently just a hard cut. The existing Assets subtab strip is the precedent for the tab pattern in FX-4.

**For epics.** Song Analysis (§4.1) is a genuine prerequisite for §4.4 and has no dependency on the rest — it can start immediately and in parallel. §4.5 Transitions is the only part that touches `assembly_plan()`, and it is where the frame-grid risk is concentrated; it deserves its own verification pass against FX-NFR-1 before it merges.
