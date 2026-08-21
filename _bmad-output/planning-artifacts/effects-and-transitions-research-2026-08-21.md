# Research: Transitions and Effects for Music Video Producer

Analyst: Mary · 2026-08-21 · Status: findings, pre-PRD

---

## Bottom line

**The pipeline you already have is unusually well-shaped for this feature, and the reason is
`assembly.py`.** Two properties decide almost everything:

1. **Every clip is already re-encoded once** at the trim stage (`trim_args`) through a `-vf`
   chain. Per-shot VFX are close to free — they are extra stages in a filter chain that
   already runs on every frame. No new pass, no second generation loss.
2. **Every intermediate is already normalized** to identical resolution, pixel format
   (`yuv420p`), frame rate (24) and SAR. FFmpeg's `xfade` filter requires exactly that of both
   its inputs — *"Both inputs must be constant frame-rate and have the same resolution, pixel
   format, frame rate and timebase."* You satisfy its precondition by accident of a decision
   made for a different reason.

The hard part is not the effects. It is **not breaking the frame grid** — the cumulative
`clip_frames_on_grid` telescoping that keeps a three-minute video within one frame of its
song. Everything below is organised around that constraint.

---

## Finding 1 — Transitions already have a home in the data model: the overlap

`assembly_plan()` already resolves overlapping shots. Today it resolves them as a **hard cut**:

> "Overlaps resolve as layers, later-on-top — the Director's ruling (2026-08-20)… an overlaid
> head is cut, a nested overlay splits the clip around itself and the underneath RESUMES."

An overlap is a region where **both takes contain real frames for the same song seconds**.
That is the definition of a crossfade. The Director already produces these by dragging a clip.

**A transition is therefore not new timeline geometry — it is a different resolution rule for
geometry that already exists.** Overlap N seconds → N-second transition. Timeline length is
unchanged, the song grid is untouched, and the frames come from material both takes already
hold. No margin borrowing, no arithmetic drift.

This is the single most important finding in this memo. It means transitions can ship without
touching the invariant that `assembly.py`'s docstring exists to defend.

### Keeping the join a stream copy

Naive `xfade` chaining forces the whole timeline through one `filter_complex` and re-encodes
every frame — losing `-c:v copy`, adding a second generation, and multiplying export time.

The alternative, which preserves today's argv shape: **bake each transition into its own
intermediate.** For an overlap between A and B, produce a third normalized intermediate — the
blended segment — and emit the concat list as `[A-shortened, AB-transition, B-shortened]`.
The join stays `-c:v copy`, frame counts still telescope (the transition segment's frames are
exactly the overlap frames), and the argv stays pure and pinnable by test, which is the house
style in that module.

---

## Finding 2 — The transition inventory is already installed

Your ffmpeg is **7.0 full_build (gyan.dev), GPL, static**. Measured on this machine today:

| Capability | Status | Note |
|---|---|---|
| `xfade` | **present**, transitions 0–57 + `custom` (-1) | 58 built-in transitions: fades, wipes, slides, circle/rect crops, radial, smooth*, vert/horz open-close, dissolve, pixelize, diagonals |
| `lut3d` / `haldclut` | present | `.cube` LUT grading; free film-emulation packs exist under permissive terms |
| Colour: `eq` `curves` `colorbalance` `colorchannelmixer` `colortemperature` `colorcorrect` `colorcontrast` `exposure` `selectivecolor` `monochrome` `hue` `lutrgb/lutyuv` | present | full grading vocabulary |
| Texture: `noise` `gblur` `smartblur` `unsharp` `vignette` `deband` `gradfun` | present | grain, bloom source, halation, vignette |
| Stylize: `rgbashift` `chromashift` `shufflepixels` `edgedetect` `elbg` `pseudocolor` `convolution` `morpho` `geq` | present | RGB split, glitch, posterize, arbitrary per-pixel expressions |
| Geometry: `zoompan` `crop` `scale` `rotate` `shear` `perspective` `scroll` `displace` `remap` | present | punch-in, Ken Burns, handheld shake (expression-driven crop), dutch tilt |
| Temporal: `tmix` `tblend` `amplify` `setpts` `minterpolate` `framerate` | present | motion blur, frame echo, speed ramp |
| Compositing: `blend` `overlay` `maskedmerge` `despill` | present | screen/multiply glow, light leaks, overlays |
| `libplacebo` (Vulkan) with `custom_shader_path` | **present** | arbitrary GPU GLSL in mpv `.hook` format — a real Phase-3 escape hatch on a 5090 |
| NVENC (`h264_nvenc`, `hevc_nvenc`, `av1_nvenc`) | present | export currently uses CPU `libx264` only |
| `frei0r` filter | present, **plugins not installed** | would require shipping DLLs — recommend excluding from v1 |

**Nothing needs to be installed, built, or vendored for a strong v1.** That is a materially
different situation from most "add VFX" projects.

### Beyond the built-ins

- **[scriptituk/xfade-easing](https://github.com/scriptituk/xfade-easing)** — 70+ GL Transitions
  ported plus Penner/CSS easings. Two variants: a patched ffmpeg build (fast), and **custom
  expressions that run on stock ffmpeg** via `transition=custom:expr=…`. The expression variant
  needs `-filter_complex_threads 1` and is *substantially* slower (the project's own timings
  show HD transitions at 30–900 s versus 2–230 s native). Verdict: **a Phase-2/3 expansion pack,
  pre-generated as expression strings, not a v1 dependency** — and gated by a measured cost,
  because linear easing on 58 built-ins is already more transition vocabulary than one music
  video needs.
- **[gl-transitions](https://github.com/gl-transitions/gl-transitions)** (npm `gl-transitions` is
  a plain data array of GLSL + params) — the source of truth those ports come from, and a
  possible WebGL preview engine. **Caution:** previewing a gl-transition in WebGL while
  rendering an `xfade` built-in shows the Director a transition the export will not produce.
  Preview and render must name the *same* transition or the panel lies.

---

## Finding 3 — Per-shot VFX slot into the existing `-vf` chain, and order is the only design work

`trim_args` currently builds:

```
[trim=start_frame=N, setpts=PTS-STARTPTS,]
scale=W:H:force_original_aspect_ratio=decrease,
pad=W:H:(ow-iw)/2:(oh-ih)/2,
fps=24, setsar=1, format=yuv420p
```

Effects insert as named stages with a fixed, documented order. Proposed:

```
trim → [geometry FX] → scale → pad → [colour/grade FX] → [texture FX] → fps → setsar → format
```

Geometry before `scale` so a punch-in samples the take's real pixels rather than upscaling
already-scaled ones. Vignette and grain go *after* `pad` would darken and dirty the letterbox
bars, so their placement relative to the pad is a measured decision during build — it belongs
in the architecture doc, not the PRD, but it must be made explicitly rather than by accident.

The important product property: the chain is a **pure function of the shot's stored effect
list**, exactly like today's argv. It stays testable by string comparison, non-destructive
(the approved take file is never rewritten), and re-derivable from the manifest.

---

## Finding 4 — What the Music Visualizer Studio actually has to give

`J:\Hermes-Remote\music-visualizer-studio` (Node/Express, 3.3k lines). Assessed for reuse:

**Directly valuable — port this:**

- **`analyzeAudio()` in `src/render.js`** (~120 lines). Decodes the song via `ffmpeg … -f s16le
  pipe:1` and extracts, with **zero library dependencies**: RMS envelope, peaks, zero-crossing
  rate, a spectral-flux proxy, **onset markers, beat-like local maxima, and a median-of-inter-
  onset-intervals BPM estimate** (octave-folded into 70–180). MVP has *none* of this — its only
  audio analysis is `faster-whisper` transcription. This is a clean, dependency-free port to
  Python that would serve three things at once: audio-reactive effects, **beat-snapping for cut
  placement** (the 2026-08-20 ruling "snap cuts to phrase boundaries" currently has only lyric
  boundaries to snap to), and timeline beat markers.
- **The layer-effect vocabulary and its ffmpeg encodings** — `layerEffectsFilter()`,
  `backgroundFilter()`, `visualLayerFilter()` are a worked catalogue of glow-via-
  `gblur`+`blend=screen`, rotation with transparent canvas expansion, `colorkey`/`colorchannelmixer`
  alpha compositing, gradient-multiply tinting. These are proven ffmpeg idioms, already
  debugged, in the exact style the `-vf` chain wants.
- **The "punch + DB-sustain envelope with a trigger floor"** concept from its reactive image
  layers — an effect that *flashes on hits* rather than pinning on a loud track. That is the
  hard-won part of audio-reactive design and it is already solved there in principle.

**Valuable as a cautionary precedent — read this before choosing a preview strategy:**

That project shipped an ffmpeg-filter render engine, found it drifted from the browser preview,
and **replaced it with a canvas engine that renders frame-by-frame using the identical
`draw-engine.js` the preview uses**, keeping ffmpeg only for background, audio and encoding.
The old engine survives only as a fallback and each render records which engine produced it.

The lesson generalises: **two engines drift, and the drift is discovered by the user, not by
tests.** MVP must not end up with a JS effect engine for preview and an ffmpeg effect engine
for export, describing the same effect differently.

**Not reusable:** the engine itself is Node + `@napi-rs/canvas`, and its subject is
audio-reactive *overlay graphics* on a static backdrop, not treatment of generated footage.
The architecture does not transfer; the algorithms and the lesson do.

---

## Finding 5 — Preview is the real product risk, not rendering

Applying an effect is a solved problem. **Showing the Director what it looks like before a
multi-minute export is not.** Four candidate strategies, with the honest trade:

| Strategy | Fidelity | Latency | Drift risk | Cost |
|---|---|---|---|---|
| **A. CSS filters on the `<video>` element** | Approximate — CSS has blur/brightness/contrast/saturate/hue-rotate/sepia and nothing else. No LUT, no grain, no glitch, no transition. | Instant, live during playback | **High** — a second, weaker vocabulary that silently disagrees with the export | Lowest |
| **B. ffmpeg single-frame proxy** — extract one frame at the playhead through the *real* filter chain | **Exact** for colour/texture/stylize; shows nothing about motion | ~150–400 ms per parameter change | **None** — one engine, and it is the export's | Low; fits the house idiom (pure argv, pinned by test) |
| **C. ffmpeg short-clip proxy** — render 2–4 s at reduced resolution through the real chain | **Exact**, including motion and transitions | seconds; needs a job, cancellation, staleness handling | None | Medium — a new job class in the Queue |
| **D. WebGL preview engine** (gl-transitions + shader ports) | Exact only for effects authored as shaders on both sides | Instant | **Highest** — precisely the trap the Visualizer Studio walked into and reversed out of | Highest |

### Measured, 2026-08-21 — and the recommendation changed

The table above priced C as materially more expensive than B. **It is not.** Timed on the
Director's machine, 1056×608 source, 24 fps, nine-stage filter chain:

| Job | Cost |
|---|---|
| B — single still frame, full chain, full resolution | 170 ms |
| **C — whole 4.5 s shot, half dimensions, `ultrafast`/CRF 28** | **270 ms** |
| C — whole 15 s shot (longest this pipeline produces) | 660 ms |
| C — 2 s window around an `xfade` transition | 150–190 ms |
| C — 4.5 s driven by 108 timed `sendcmd` commands | 90 ms |
| C — 4.5 s at half dimensions via NVENC | 400–530 ms — *slower* |

A still costs 170 ms; the **entire shot** costs 270 ms. The 100 ms difference buys nothing and
costs motion, geometry, reactive drive, and transitions — all four of which are invisible in a
still. NVENC loses at these lengths because encoder initialization dominates a sub-second job.

**Revised recommendation: C for everything.** A looping clip through the real chain, at reduced
dimensions and encoder quality and differing in nothing else. It keeps B's decisive property —
*there is exactly one description of what an effect is, and it is the one that renders* — and it
closes the transition gap that made B incomplete. B survives only as the fallback if a future
effect proves too slow to render in motion.

The plumbing already exists either way: the app shells out to ffmpeg for a contact sheet
(`app.py:4495` builds a `tile=2x2` chain), so this is a new argv, not a new capability.

---

## Finding 6 — The scope boundary this feature crosses

PRD §5 currently states: *"Not a general video editor. It edits this pipeline's output, not
imported footage."*

That line has already softened — external clips have their own Assets subtab and their own
assembly path (`app.py:7566`: *"External clip: no over-render margin exists in it"*). Adding
transitions and per-shot grading moves further across it.

**This is a deliberate product decision for the Director, not a technicality.** The defensible
position is a narrow one: *effects and transitions exist to make cuts land on the music and to
give a song one coherent look — not to make this an NLE.* Everything in the v1 catalogue should
be justifiable by that sentence, or it does not go in. Stated up front, it keeps the feature
from growing a keyframe editor.

Two related notes:

- **External clips carry no over-render margin.** Overlap-driven transitions still work on them
  (the overlap is real footage), but any design that borrows the margin does not. Prefer the
  overlap.
- **Export cost will rise.** Master preset is `x264 slow / CRF 16` on CPU today. Effects add
  filter cost per frame; transitions add segments. NVENC is available and unused — worth a
  measured comparison during build, though quality parity with `slow`/CRF 16 is not automatic.

---

## Proposed shape (for the PRD to firm up)

> **Superseded by the Director's rulings of 2026-08-21.** The phasing below was the analyst's
> proposal; the rulings widened it — four effect families rather than three, general audio-reactive
> binding, and audio analysis promoted to a Phase 1 prerequisite. See
> `effects-director-rulings-2026-08-21.md` and the PRD at
> `prds/prd-MusicVideoProducer-effects-2026-08-21/`. Kept for the record.

**Phase 1 — Look.** Effects tab in the shot inspector. A curated catalogue of per-shot effects
in three families (grade, texture, geometry), each a named preset with 1–3 exposed parameters,
composed into the existing `-vf` chain. Still-frame preview through the real chain. Effects
stored on `Shot`, non-destructive, re-derivable, refused on a locked shot.

**Phase 2 — Cuts.** Transitions as a resolution rule on clip overlap, baked into their own
intermediate so the join stays a stream copy. The 58 built-in `xfade` transitions, curated down
to a music-video-sized set. Short-clip preview.

**Phase 3 — Music.** Port the beat/onset/RMS extractor. Beat markers on the timeline, beat-
snapping for cut placement, and audio-reactive effect parameters (punch on the hit, not pinned
on the loud track).

**Deliberately out:** frei0r, keyframed parameter curves, masks/rotoscoping, a node graph,
per-effect GPU shader authoring, and any second effect engine that exists only for preview.

---

## Rejected alternatives

- **MLT Framework (Shotcut/Kdenlive's engine)** — a complete multitrack engine with XML
  authoring, filters, transitions and Python bindings. Rejected: it would replace `assembly.py`
  wholesale, discarding the frame-grid telescoping, the over-render trim rule, the pinned argv
  and the refusal reporting that took this project months to get right — to gain capabilities
  reachable through filters already installed. It is also a heavyweight non-Python native
  dependency on Windows, against the project's standalone/local-first constraint.
- **MoviePy / ffmpeg-python wrappers** — the project builds argv directly and pins it by test.
  A wrapper would hide the one artefact the tests assert on.
- **frei0r plugin suite** — plugins are not installed on this machine; shipping DLLs adds a
  GPL install dependency for effects the native filter set already covers.

---

## Sources

- [FFmpeg Filters Documentation](https://ffmpeg.org/ffmpeg-filters.html)
- [xfade filter reference](https://ayosec.github.io/ffmpeg-filters-docs/7.1/Filters/Video/xfade.html)
- [OTTVerse — CrossFade, Dissolve and other effects using xfade](https://ottverse.com/crossfade-between-videos-ffmpeg-xfade-filter/)
- [FFmpegLab — xfade transitions guide](https://www.ffmpeglab.com/articles/ffmpeg-xfade-transitions-guide.html)
- [scriptituk/xfade-easing](https://github.com/scriptituk/xfade-easing)
- [gl-transitions/gl-transitions](https://github.com/gl-transitions/gl-transitions)
- [gre/gl-transition-libs](https://github.com/gre/gl-transition-libs)
- [transitive-bullshit/ffmpeg-gl-transition](https://github.com/transitive-bullshit/ffmpeg-gl-transition)
- [DocM88/Free-Lut-Pack](https://github.com/DocM88/Free-Lut-Pack)
- [FFmpeg: Ultimate film grain (gist)](https://gist.github.com/logiclrd/287140934c12bed1fd4be75e8624c118)
- [Creating Vintage Video Filters with FFmpeg](https://zayne.io/articles/vintage-camera-filters-with-ffmpeg)
- [Simulating CRT Monitors with FFmpeg](https://int10h.org/blog/2021/01/simulating-crt-monitors-ffmpeg-pt-1-color/)
- [MLT Framework](https://www.mltframework.org/) — evaluated and rejected above
- Local measurement: `ffmpeg -version`, `-filters`, `-h filter=xfade`, `-encoders` on this machine, 2026-08-21
- Codebase: `src/music_video_producer/assembly.py`, `timeline.py`, `models.py`, `app.py`;
  `J:\Hermes-Remote\music-visualizer-studio\src\render.js`, `src/canvas-engine.js`
