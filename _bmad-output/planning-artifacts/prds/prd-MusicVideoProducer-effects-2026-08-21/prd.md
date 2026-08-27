---
title: Shot Effects and Transitions
status: final
created: 2026-08-21
updated: 2026-08-21
---

# PRD: Shot Effects and Transitions

## 0. Document Purpose

This PRD is for the Director (sole builder and operator) and for the downstream BMad workflows — architecture, UX, and epic/story creation — that consume it. It describes one significant addition to a working application: the ability to give a rendered Shot a look, and to make one Shot become the next in a way the Director chose.

It builds on, and does not duplicate, the product PRD at `_bmad-output/planning-artifacts/prds/prd-MusicVideoProducer-2026-08-16/prd.md`. That document remains authoritative for everything it covers; this one covers only what it adds. Where the two contradict, the contradiction is called out explicitly rather than left for a reader to discover — see §5, which amends the product PRD's "not a general video editor" non-goal.

Two upstream inputs are load-bearing and should be read alongside it:

- `_bmad-output/planning-artifacts/effects-and-transitions-research-2026-08-21.md` — the research findings, including the measured inventory of what this machine's ffmpeg can already do, the reuse assessment of the Music Visualizer Studio, and the rejected alternatives.
- `_bmad-output/planning-artifacts/effects-director-rulings-2026-08-21.md` — the Director's binding decisions **R-1** through **R-7**. Requirements below cite them. A change to a ruling is a change to this PRD.

**Requirement IDs are prefixed `FX-`** so they cannot be confused with the product PRD's `FR-1`–`FR-26`, which are still live. Cross-cutting quality requirements are `FX-NFR-n`. Vocabulary is anchored in §3 and used verbatim. Inferences are tagged `[ASSUMPTION]` inline and indexed in §9. Mechanism — how a requirement is realized in ffmpeg — lives in `addendum.md`, not here.

## 1. Vision

An AI-generated music video has a specific failure mode, and it is not bad clips. It is forty good clips that plainly do not belong to the same film. Every Shot arrives from the model with its own contrast, its own colour temperature, its own idea of how much the camera moves — and cut together against a song, the result reads as a playlist rather than a video.

**Effects exist to make cuts land on the music and to give a song one coherent look.** That sentence is the whole scope test. A capability that cannot be justified by it does not go in, no matter how standard it is in an editing suite.

This is deliberately not a general-purpose effects system, and the difference is visible in what is offered. There is no keyframe editor, because a music video's timing authority is the song, not a curve the Director draws. Instead, a parameter can be tied to the music itself — bound to a frequency band, driven by what that band actually does across the song, so grain surges on the kick and the frame breathes with the bass without anyone animating anything (R-2). The song is the automation.

Transitions follow the same instinct. They are not a property buried in a dialog; they are what happens when the Director drags two clips until they overlap (R-3). The overlap is the transition — visible on the timeline, its length set by hand, made of frames both takes genuinely hold.

## 2. Target User

The Director, as established in the product PRD §2. This feature adds jobs rather than users.

### 2.1 Jobs To Be Done

- **Functional:** make forty independently generated Shots look like one film.
- **Functional:** make a cut land on the beat instead of near it.
- **Functional:** give a section of the song its own look — the choruses hotter than the verses — without re-rendering a single Shot.
- **Functional:** hide the tell-tale plasticky sheen of generated footage under grain and grade.
- **Functional:** tie what the picture does to what the music does, without animating anything by hand.
- **Emotional:** stop feeling like an operator of a generator and start feeling like someone cutting a video.

### 2.2 Non-Users (v1)

Unchanged from the product PRD §2.2. One addition:

- Anyone wanting a compositing or motion-graphics tool. There are no masks, no rotoscoping, no layers beyond the Shot, no titling, and no node graph.

### 2.3 Key User Journeys

- **UJ-4. The Director gives a finished cut one look.**
  - **Persona + context:** every Shot is approved and the video assembles cleanly, but it reads as forty clips rather than one film.
  - **Entry state:** a complete Shot Plan with Approved Outputs, a song whose Song Envelope has been computed.
  - **Path:** selects the first Shot → opens the **Effects** tab beside Shot Info → adds a Grade effect and picks a LUT → the Preview Clip re-renders and loops in place through the real render chain → adjusts contrast until the Shot looks right → copies the Effect Stack to every other Shot → walks the timeline spot-checking Shots whose source footage was brighter or flatter, adjusting those individually.
  - **Climax:** the Director plays the timeline and the Shots read as one piece.
  - **Resolution:** the export carries the grade; no take file was rewritten, and removing every effect returns the video to exactly what it was.
  - **Edge case:** a Shot is locked. The Effects tab shows its stack read-only and says why, exactly as every other sweep refuses a locked Shot.

- **UJ-5. The Director makes the video move with the song.**
  - **Persona + context:** the track has a hard kick and the video is visually static under it.
  - **Entry state:** a Song Envelope exists; the Director has a Shot selected with a Texture effect already on it.
  - **Path:** opens the effect's **react to** panel → drags the band selector down to the bass end and narrows it → picks **punch** drive, sets a trigger floor so quiet passages do nothing → sets depth → the preview loops with the Drive envelope drawn beneath it, so the hits are visible as they land → raises depth until the pulse reads as a hit rather than a flicker.
  - **Climax:** on export, the grain surges on the kick and settles between hits.
  - **Resolution:** the binding is stored on the Shot and travels with the manifest; changing the song invalidates the Song Envelope and says so rather than driving effects from a stale one.
  - **Edge case:** no Song Envelope exists yet. The react-to panel refuses by name and offers to analyze the song, rather than presenting a band selector that would silently do nothing.

- **UJ-6. The Director dissolves one Shot into the next.**
  - **Persona + context:** two Shots of the same location cut harshly at a moment the song does not accent.
  - **Entry state:** two adjacent Shots with Approved Outputs.
  - **Path:** drags the later Shot's left edge back over the earlier Shot → the overlapping region highlights blue on the timeline → opens the earlier Shot's Effects tab and sets **transition out** to a dissolve → the later Shot's **transition in** is set to match automatically, and says so → drags the edge further to lengthen the dissolve, watching the blue region grow.
  - **Climax:** the export plays a dissolve exactly as long as the blue region, and the assembled video is the same length it was before, to the frame.
  - **Resolution:** the transition is a property of the overlap; removing the overlap removes the transition.
  - **Edge case:** the Director sets a transition out on a Shot with no overlap. It applies as a one-sided effect on that Shot's own final frames, followed by a hard cut — and the panel says that is what will happen, rather than implying a blend that has nothing to blend with.

## 3. Glossary

Terms from the product PRD §3 are unchanged and used as defined there. New vocabulary:

- **Effect** — one named, parameterized treatment applied to a Shot's picture at export. Non-destructive: it never modifies the Approved Output file.
- **Effect Family** — one of four groupings the catalogue is organized by: **Grade** (colour), **Texture** (film), **Stylize** (glitch), **Geometry** (camera).
- **Effect Stack** — the ordered list of Effects on one Shot. Order is the Director's, within the fixed Family ordering the render chain imposes.
- **Effect Parameter** — one named, bounded, numeric or enumerated control on an Effect. Every parameter has a default that is a visual no-op where the Family allows one.
- **Song Envelope** — the analysis of a Project's Song: RMS, peak, spectral-flux proxy, onset markers, beat markers, an estimated BPM, and per-band level envelopes across the song's duration. Computed from the Song, cached on the Project, and invalidated when the Song changes.
- **Band** — a region of the frequency spectrum selected by centre, width, and softness. The unit a reactive Effect Parameter listens to.
- **Drive** — the 0–1 signal a Band produces over time, in one of two modes. **Punch** is a fast-attack, slow-release envelope of the Band's level above its own running average — it responds to transients, so it flashes on hits instead of pinning high on a loud master. **Sustain** is a section gate: it engages only after the Band holds above a level for a hold time, and survives dips for a sustain time.
- **Parameter Binding** — the attachment of one Effect Parameter to one Band, carrying drive mode, trigger floor, and depth. Any parameter may carry one (R-2).
- **Trigger Floor** — the level below which a Drive produces nothing, so quiet passages leave the picture alone.
- **Depth** — how far a Parameter Binding moves its parameter between the parameter's resting value and its driven extreme.
- **Overlap** — a region of the timeline where two Shots' windows both cover the same Song seconds. Already resolvable by Assembly; now also the place a Transition lives.
- **Transition** — a named treatment of the boundary between two Shots. Authored as an Overlap (R-3).
- **Transition Pair** — a Shot's **transition out** and the next Shot's **transition in**, which describe one blend across an Overlap and are kept matching automatically.
- **One-Sided Transition** — a transition out or transition in on a boundary with no Overlap. It treats that Shot's own final or opening frames and is followed or preceded by a hard cut (R-4).
- **Preview Clip** — the selected Shot, or a window spanning a Transition, rendered through the exact ffmpeg filter chain the export will run — at reduced dimensions and encoder quality, and differing in nothing else — and looped in the Effects tab.
- **Drive Readout** — the Drive envelope drawn across a Shot's window beneath its Preview Clip, showing where a Parameter Binding fires and where its Trigger Floor silences it.

## 4. Features

### 4.1 Song Analysis

**Description:** the Project's Song becomes measurable — beats, onsets, levels, and per-band energy over time. A prerequisite for reactive Effects (R-5), and independently valuable to cut placement. Realizes part of UJ-5.

#### FX-1: Analyze a Song into a Song Envelope

The Director can produce a Song Envelope from the Project's Song, and the application produces one automatically when a Song is first imported or generated.

**Consequences (testable):**
- The Song Envelope contains, at a fixed analysis rate: RMS, peak, a spectral-flux proxy, per-band level envelopes, onset markers, beat markers, and one estimated BPM for the Song.
- Analysis introduces no new runtime dependency — it decodes through the ffmpeg binary the application already requires and computes in the application's own language.
- Analysis never blocks the interface, and never blocks or delays a render, a Batch, or an Assembly.
- The Song Envelope is cached on the Project and survives reload without recomputation.
- Changing the Project's Song invalidates the Song Envelope. An invalidated envelope is reported as absent, never served as current.
- A failed analysis is reported with its reason and leaves the Project otherwise unchanged. Nothing downstream treats a failure as an envelope of zeros.
- The estimated BPM is presented as an estimate and nothing refuses on its value.

**Notes:** the algorithm is a port of `analyzeAudio()` from `J:\Hermes-Remote\music-visualizer-studio\src\render.js` — roughly 120 dependency-free lines that decode via an ffmpeg `s16le` pipe. This is a port, not an integration: no Node runtime, no shared code, no coupling between the two applications.

#### FX-2: Show beats on the timeline

The Director can see the Song's beat and onset markers against the waveform.

**Consequences (testable):**
- Markers are display only. Nothing about a Shot changes when they are shown or hidden.
- Markers can be turned off, and the setting persists.
- A Project with no Song Envelope shows no markers and no error.

#### FX-3: Snap cut placement to beats

Beat markers join lyric and phrase boundaries as snap targets when the Director moves a Shot boundary.

**Consequences (testable):**
- Snapping remains an assist. A Shot boundary can always be placed off a beat, and nothing refuses it.
- Beat snapping can be disabled independently of existing snap targets.
- With no Song Envelope, boundary editing behaves exactly as it does today.

**Notes:** this gives the standing ruling *"snap cuts to phrase boundaries"* actual beats to snap to for the first time. It inherits the trim-nudge ruling's posture — a positioning aid that warns and never constrains.

### 4.2 The Effects Tab

**Description:** where a Shot's look is edited. Realizes UJ-4.

#### FX-4: Two tabs in the shot inspector

The shot inspector presents two tabs: **Shot Info** and **Effects**.

**Consequences (testable):**
- Shot Info contains exactly what the inspector contains today, unchanged in content, order, and behaviour.
- Switching tabs never discards an in-progress edit in the other tab.
- The selected tab persists across inspector rebuilds — including rebuilds triggered by a background reload — for the same Shot.
- Selecting a different Shot returns to Shot Info. `[ASSUMPTION: returning to the default tab on selection change is less surprising than carrying the Effects tab to a Shot the Director selected for another reason.]`
- The Effects tab indicates at a glance, from the tab itself, whether the selected Shot carries any Effects or Transitions.

#### FX-5: Build a Shot's Effect Stack

The Director can add, remove, reorder, and individually disable Effects on a Shot.

**Consequences (testable):**
- A disabled Effect is retained with its parameters and contributes nothing to preview or export.
- An empty Effect Stack produces the byte-identical ffmpeg **command** today's export produces for that Shot. *(Amended 2026-08-25, ruling R-20: the guarantee is about the command and the filter graph's frames, never the encoded file. An mp4 out of this pipeline is not byte-reproducible at all — eight renders of one identical chain produced two distinct pictures, and pinning the encoder to a single thread collapsed them to one. Multi-threaded libx264 is not bit-exact on high-entropy input, so the original wording was untestable as written.)*
- Removing every Effect from a Shot returns it to the empty state, not to a residual one.
- The Effect Stack is stored on the Shot in the Project manifest and is fully re-derivable from it.
- Reordering within the constraints the render chain imposes is the Director's; reordering that the chain forbids is not offered rather than silently ignored.

#### FX-6: Copy an Effect Stack across Shots

The Director can apply one Shot's Effect Stack to other Shots.

**Consequences (testable):**
- The target set is explicit — the Director names it. Nothing applies to "all Shots" without the Director choosing that.
- Copying reports what it did, by Shot count, and names any Shot it refused.
- A locked Shot is refused by name and left unchanged.
- Copying replaces the target's stack rather than merging, and says so before it runs.

#### FX-7: Refuse Effect edits on a locked Shot

A locked Shot's Effects tab is read-only.

**Consequences (testable):**
- Every control that would write is disabled, and the panel states the lock as the reason.
- Sweeps, fills, and copies skip locked Shots and name them in their report.
- Unlocking restores editing with the stack intact.

### 4.3 The Effect Catalogue

**Description:** the four Families and what is in them (R-1). Realizes UJ-4.

#### FX-8: Grade — give a Shot a colour

**Consequences (testable):**
- The Family provides at minimum: a LUT look drawn from a bundled set, exposure, contrast, saturation, colour temperature, tint, lift/gamma/gain in some exposed form, and monochrome.
- Every parameter is bounded, and every parameter's default is a visual no-op.
- The same parameters on the same source produce the same output frame on every run.
- A LUT look names the LUT it applied, and a missing LUT file is reported by name rather than silently skipped.

#### FX-9: Texture — give a Shot a surface

**Consequences (testable):**
- The Family provides at minimum: grain, vignette, bloom/halation, soft-focus diffusion, and banding suppression.
- Vignette and grain treat the picture, not the letterbox padding Assembly adds. This is asserted, not assumed. `[ASSUMPTION: the Director never wants a vignette darkening the pillarbox bars; if that is wrong it is a one-line change of stage order.]`

#### FX-10: Stylize — give a Shot an artefact

**Consequences (testable):**
- The Family provides at minimum: RGB/chroma split, pixel shuffle or sort, posterize, edge treatment, and a scanline/CRT look.
- Every Stylize Effect is off by default and none is applied implicitly by any other Family.

#### FX-11: Geometry — give a Shot a camera

**Consequences (testable):**
- The Family provides at minimum: punch-in, slow zoom, handheld shake, dutch tilt, and mirror/flip.
- **Geometry is applied before the Shot is scaled to the export's target dimensions**, so a punch-in samples the take's own pixels rather than resampling an already-scaled frame.
- A Geometry Effect never changes the Shot's frame count, its window, or its position on the timeline. It changes what is inside the frame and nothing else.
- Geometry that would sample outside the source frame is bounded so it cannot expose an undefined edge.

**Notes:** Geometry was reinstated in full by R-1 after initially being excluded, on the grounds that a scale punch is the most legible beat cue available and excluding the Family to gain one reactive primitive was the wrong trade. The before-scale ordering above is the single constraint the build cannot get wrong silently — it is invisible in a still and obvious in motion.

### 4.4 Audio-Reactive Binding

**Description:** any Effect Parameter can be driven by the music (R-2). Realizes UJ-5.

#### FX-12: Bind a parameter to a band

The Director can bind any Effect Parameter to a Band.

**Consequences (testable):**
- Any parameter of any Effect in any Family can carry a Parameter Binding. ~~No parameter is specially privileged and none is excluded by category.~~ *(amended 2026-08-27, R-25)* **Not literally true and never could have been:** drivability is a property of the (parameter -> filter option) pair. Measured on ffmpeg 7.0, `noise`, `vignette`, `unsharp`, `shufflepixels` and `edgedetect` expose no runtime-settable option at all, and a `sendcmd` aimed at one is silently ignored at rc 0. The genuinely non-drivable parameters are `grain.strength`, `grain.seed`, `vignette.angle`, `sharpen.amount`, `edge_treatment.low`, `edge_treatment.high`, `pixel_shuffle.block`, `pixel_shuffle.seed` and `lut_look.lut` -- the last because a `.cube`'s `file` has no timeline flag. `edge_treatment.strength` and `pixel_shuffle.amount` **are** drivable, because both effects compose as a branch and their dial is written into `blend`'s `all_opacity`. What survives of this clause, and is the half worth keeping: **no parameter is excluded by *family or category*** -- Geometry, Texture, Grade and Stylize are all bindable, and exclusion is per-parameter, measured, and refused by name.
- A parameter carries at most one binding.
- A bound parameter's manual value becomes its resting value; the binding moves it from there by Depth.
- Removing a binding restores the parameter to its resting value with no residue.
- A Shot with no bindings exports identically to one where the binding feature does not exist.

#### FX-13: Choose the band

**Consequences (testable):**
- A Band is selected by centre, width, and softness across the spectrum, and the selection is shown against the Song's own spectrum rather than as bare numbers.
- Two Effects on the same Shot may listen to different Bands independently.

#### FX-14: Choose how the band drives

**Consequences (testable):**
- Drive is either **punch** or **sustain**, chosen explicitly. Neither is inferred.
- Punch responds to transients rather than absolute level: on a heavily limited master it still flashes on hits rather than sitting pinned high. This is the property the mode exists for and it is asserted.
- Sustain engages only after its Band holds above a level for a hold time, and survives dips for a sustain time.
- A Trigger Floor is settable, and below it the Drive contributes nothing.
- Depth is settable and bounded so a binding cannot drive a parameter outside its own declared range.

#### FX-15: Refuse binding without an envelope

**Consequences (testable):**
- With no Song Envelope, the react-to panel refuses by name and offers to analyze the Song. It does not present a Band selector that would silently do nothing.
- An invalidated Song Envelope is treated as absent. Bindings stored on Shots are retained, reported as unresolvable, and become live again when the Song is re-analyzed.
- A binding is never silently dropped because its envelope went missing.

**Notes:** the whole audio-reactive layer is a **pure function of (Song Envelope, Parameter Binding)**. It is deterministic, it is re-derivable from the manifest, and it is comparable as text — see `addendum.md` for the mechanism, which is a generated command script rather than a runtime feedback loop.

### 4.5 Transitions

**Description:** what happens at the boundary between two Shots (R-3, R-4). Realizes UJ-6.

#### FX-16: Author a transition by overlapping clips

The Director creates a Transition by dragging Shot edges until two Shots overlap.

**Consequences (testable):**
- The Overlap's length is the Transition's length. There is no separate duration control that could disagree with it.
- The overlapping region is **highlighted blue** on the timeline while it exists.
- Removing the Overlap removes the Transition; the Transition Pair's stored types are retained and become One-Sided Transitions (FX-18).
- An Overlap is only a Transition when a Transition type is set on it. An Overlap with no type set resolves exactly as it does today — a hard cut, later Shot on top.
- Overlapping more than two Shots at one point is either supported with a stated resolution rule or refused with a stated reason. It is not left undefined. `[ASSUMPTION: three-way overlap is rare enough that refusing it with a clear message is acceptable for v1.]`

#### FX-17: Set a Transition Pair

Each Shot carries a **transition out** and a **transition in**.

**Consequences (testable):**
- Setting a Shot's transition out, where it overlaps a following Shot, sets that Shot's transition in to match — and the interface says it did so rather than changing a value silently.
- The reverse holds: setting the following Shot's transition in sets the preceding Shot's transition out.
- A Transition Pair across an Overlap can never hold two different types.
- Breaking the Overlap leaves both stored values in place and independently editable.

#### FX-18: One-sided transitions

A transition out or in on a boundary with no Overlap treats that Shot's own frames.

**Consequences (testable):**
- A one-sided transition out treats the Shot's final frames and is followed by a hard cut. A one-sided transition in treats the Shot's opening frames.
- Its type is honoured: a blur out blurs, a fade out fades. The named type is never quietly substituted.
- A one-sided transition consumes no timeline length, borrows no frames from a neighbour, and changes no Shot's window.
- The interface states, on the control, that this boundary has no Overlap and what will therefore happen.
- Its length is bounded by the Shot's own duration and by nothing invisible.

#### FX-19: The transition catalogue

**Consequences (testable):**
- The catalogue is a deliberately curated subset, not an exhaustive dump of everything available. Each entry is named in the Director's language.
- At minimum it covers: dissolve, fade through black, fade through white, blur, and directional wipes and slides.
- Every catalogue entry works both as a paired Transition and as a One-Sided Transition, or is marked as pair-only and refuses one-sided use with a reason.

### 4.6 Preview

**Description:** seeing an Effect before committing to an export. Supports UJ-4, UJ-5, and UJ-6.

> **R-6 was superseded by measurement on 2026-08-21.** The ruling chose a still frame, and a Peak Frame beside it for reactive Effects, on the assumption that rendering motion was substantially more expensive. It is not. Measured on this machine at the house resolution through a nine-stage chain: one still costs **170 ms**, and the **whole 4.5-second shot** at half dimensions costs **270 ms**. A fifteen-second shot — the longest this pipeline produces — costs 660 ms, and a two-second window around a transition costs 150–190 ms. The still frame is not meaningfully cheaper and it cannot show motion, Geometry, Drive, or a Transition. Preview is therefore a **looping clip**, and the accepted gap the ruling carried — no transition preview in v1 — is closed. The full timing table is in `addendum.md`.

#### FX-20: Preview a Shot as a looping clip

The Effects tab plays the selected Shot, with its Effect Stack applied, as a looping video.

**Consequences (testable):**
- The Preview Clip is produced by the **same filter chain the export will run**, at reduced dimensions and encoder quality and by nothing else. There is no second description of what an Effect is anywhere in the application.
- The Preview Clip covers the Shot's exposed window — what the export will contain — not the whole over-rendered take.
- It updates when any Effect, parameter, ordering, or enablement changes. The clip playing always corresponds to the current stack; a stale clip is never presented as current.
- Rapid parameter changes do not queue a preview render per change. Superseded renders are abandoned rather than played late.
- A preview failure is reported, names its reason, and does not disturb the stack.
- Preview never writes to, replaces, or modifies the Shot's Approved Output.
- Preview never runs against ComfyUI and consumes no GPU budget the render path is coordinating.

#### FX-21: Preview a Transition

Where a Shot has a Transition — paired across an Overlap or one-sided — the Director can preview the boundary.

**Consequences (testable):**
- The Preview Clip covers a window spanning the boundary, so what is shown is the outgoing Shot, the Transition, and the incoming Shot in one continuous piece.
- The Transition previewed is the same one the export will build, by name and by duration.
- A one-sided Transition previews as what it actually is: the treated frames followed by a hard cut.
- Changing the Overlap by dragging updates the preview's Transition length to match.

#### FX-22: Show what is driving a reactive parameter

For a Shot carrying a Parameter Binding, the Effects tab shows the Drive that is moving it.

**Consequences (testable):**
- The Drive envelope is drawn across the Shot's window, aligned with the preview, so the Director can see where it peaks and where the Trigger Floor silences it.
- The envelope shown is the same signal the export will use, not an illustration of one.
- With no Song Envelope, no drive readout is offered and the absence is explained.

**Notes:** the Peak Frame from R-6 is deliberately not carried forward. A twenty-four-frames-per-second loop shows the Drive's peak several times a second; a second still of that same peak would be redundant, and the envelope readout answers the question the Peak Frame was really for — *when* is this firing, and is it firing at all.

### 4.7 Export Integrity

**Description:** what must remain true of the exported video once Effects and Transitions exist.

#### FX-23: Effects are non-destructive and re-derivable

**Consequences (testable):**
- No Effect or Transition ever rewrites, replaces, or modifies a Shot's Approved Output file.
- Removing every Effect and Transition from a Project produces the byte-identical ffmpeg **command** that Project's export produces today. *(Amended 2026-08-25, ruling R-20 — the encoded file is not byte-reproducible even between two runs of one unchanged export, so the assertion is on the command and the filter graph.)*
- A Project manifest carries everything needed to reproduce its export's look. Nothing about an Effect lives only in the interface.

#### FX-24: Export refusals name what is wrong

**Consequences (testable):**
- An Effect that cannot be applied — a missing LUT, an unresolvable binding, a transition type not valid for its boundary — refuses the export and names the Shot and the reason, in one report covering every such problem rather than one at a time.
- An export never silently drops an Effect the Director configured.

#### FX-25: Provenance records the look

**Consequences (testable):**
- A completed export records the Effect and Transition state it was built from.
- An export made before this feature existed remains readable and is reported as carrying no Effects, not as carrying unknown ones.

## 4A. Cross-Cutting NFRs

### FX-NFR-1: The frame grid is inviolable

The assembled video's duration matches the Song within one frame, for every combination of Effects and Transitions, in both the Overlap and the no-Overlap case.

**Consequences (testable):**
- A Transition changes no clip's position on the timeline and no clip's contribution to the cumulative frame grid.
- A Geometry Effect changes no Shot's frame count.
- The existing grid assertions continue to pass unchanged, and gain cases covering Overlaps with Transitions and Shots carrying full Effect Stacks.

**Notes:** this is the constraint every design decision in this PRD was organized around. An Overlap-authored Transition satisfies it structurally rather than by arithmetic care: the frames come from material both takes already hold, at timeline positions neither Shot moved.

### FX-NFR-2: The export stays a stream-copy join

Adding Effects and Transitions does not convert the export into a whole-timeline re-encode.

**Consequences (testable):**
- Clips carrying no Effects are not re-encoded a second time on account of a Transition elsewhere in the timeline.
- Export wall-clock for a Project with no Effects and no Transitions is unchanged from today's, measurably.
- Export wall-clock growth is attributable to specific Effects and Transitions, and is measured rather than asserted.

### FX-NFR-3: One engine describes an Effect

There is exactly one description in the application of what any Effect does, and it is the one the export runs.

**Consequences (testable):**
- No Effect is approximated in the interface by a different mechanism than the one that renders it.
- A parameter that cannot be shown faithfully is shown as unpreviewable rather than shown wrongly.

**Notes:** stated as a requirement because the prior art warns about exactly this. The Music Visualizer Studio shipped a filter-based renderer alongside a browser preview, found they disagreed, and rewrote the renderer to use the preview's own code — the drift was found by a user, not by tests.

### FX-NFR-4: No new runtime dependency

**Consequences (testable):**
- Song analysis, Effects, reactive bindings, and Transitions add no package to the application's runtime dependency set.
- Everything is realized through the ffmpeg binary the application already requires and the language it is already written in.
- Bundled data files — LUTs and similar — are inert assets, carry compatible licences, and their absence is reported rather than crashed on.

### FX-NFR-5: Preview stays inside a measured budget

Preview is fast enough that the Director changes a parameter and looks, rather than changing a parameter and waiting.

**Consequences (testable):**
- A Preview Clip for a Shot of typical length is ready in under one second from the change that invalidated it, measured rather than asserted.
- The preview budget is met by reducing dimensions and encoder quality, and by nothing that changes what the filter chain does. A preview that is fast because it applies a different Effect is a defect, not an optimization.
- Preview render never competes with an export, a Batch, or a ComfyUI render for the same resource.

**Notes:** the whole preview design rests on measurement rather than on estimate, and the numbers are recorded in `addendum.md` so a future regression is visible against them. The one counter-intuitive result is worth stating here: **hardware encoding is slower than software at these clip lengths**, because encoder initialization dominates a sub-second job. Preview uses the CPU encoder deliberately.

### FX-NFR-6: Generated render inputs are pure and comparable

Any file the application generates to drive a render — a command script for a reactive binding, a concat list, a filter chain — is a pure function of the manifest and is comparable as text.

**Consequences (testable):**
- The same Project produces the same generated inputs on every run, byte for byte.
- Generated inputs are asserted by comparison in tests, in the same way ffmpeg argv already is.

## 5. Non-Goals (Explicit)

Inherited from the product PRD §5, with one amendment and several additions.

**Amendment to "Not a general video editor."** The product PRD states: *"It edits this pipeline's output, not imported footage."* That boundary has already softened — external clips have their own Assets subtab and their own Assembly path — and this feature moves further across it. The line is redrawn, narrower and defensible: **this application edits the look and the cuts of a song's own video. It does not become a general non-linear editor.** Everything below is what that redrawn line excludes.

- **No keyframe editor.** A parameter is constant, or it is bound to the music. There is no third option and no curve to draw. The song is the timing authority.
- **No masks, rotoscoping, tracking, or compositing.** An Effect treats the whole frame.
- **No titles, text, or motion graphics.**
- **No node graph or effect scripting surface.** The catalogue is curated; extending it is a code change, deliberately.
- **No audio effects.** The Song is the Director's master and is never re-mastered — the standing ruling of 2026-08-20, unchanged. Effects here treat picture only.
- **No multi-track video.** Overlaps resolve as Transitions or as layered cuts; they do not become a compositing stack.
- **No frei0r plugin suite.** The plugins are not installed on this machine and shipping them would add a GPL install dependency for effects the native filter set already covers.

## 6. Scope

### 6.1 In Scope

- Song analysis into a Song Envelope, with beat markers and beat-snapping (FX-1 – FX-3).
- The Effects tab beside Shot Info, with stack editing, copying, and locked-Shot refusal (FX-4 – FX-7).
- All four Effect Families (FX-8 – FX-11).
- Audio-reactive binding of any parameter to any Band (FX-12 – FX-15).
- Overlap-authored Transitions with blue highlighting, paired out/in, and one-sided behaviour (FX-16 – FX-19).
- Looping Preview Clips for Shots and Transitions through the real render chain, with a Drive readout (FX-20 – FX-22).
- Export integrity, refusals, and provenance (FX-23 – FX-25).

### 6.2 Out of Scope

- GLSL transition packs beyond the natively available set.
- Full-resolution preview. The Preview Clip is deliberately reduced; judging final encode quality is what the draft export is for.
- Preview of more than one Shot at a time, and preview playback locked to the master song.
- GPU shader authoring, and GPU-accelerated export encoding. Both are available on this machine and neither is needed to ship; both are live v2 candidates.
- Section-level or Project-level Effect Stacks. Effects are per-Shot in v1; copying (FX-6) is the substitute.
- Effect presets saved across Projects.
- Any Effect applied at generation time rather than at export. Effects are a treatment of an approved take, never an instruction to the model.

## 7. Success Metrics

- **SM-E1 — One look holds.** A complete video exported with one Effect Stack copied across every Shot reads as a single piece rather than as a playlist. Judged by the Director on a finished video, not measured.
- **SM-E2 — The grid never moves.** Across a full test matrix of Effects and Transitions, every exported video's duration matches its Song within one frame. Binary; gates release.
- **SM-E3 — A reactive binding is legible.** On a finished export, a viewer who was not told about the binding can identify that the picture is responding to the music. Judged, not measured.
- **SM-E4 — The iteration loop is usable.** The Director can change an Effect parameter and see the faithful result in motion without losing their place or their patience. Target: under one second from change to a looping Preview Clip, for a Shot of typical length.
- **SM-E5 — Nothing was lost.** A Project exported before this feature existed builds the byte-identical ffmpeg **command** after it, with no Effects applied. *(Amended 2026-08-25, ruling R-20. Comparing the two mp4s cannot show this: multi-threaded libx264 is not bit-exact, so two runs of the same unchanged export already differ. The command and the filter graph's raw frames are both reproducible, and are what this metric is read against.)*

**Counter-metrics** — signals that this feature has damaged the product:

- **CM-E1 — Export time.** A no-Effects export must not get slower. Any measurable regression is a defect, not a cost of the feature.
- **CM-E2 — Panel weight.** If the Effects tab becomes slow enough to make Shot selection feel heavy, the catalogue is too large or the preview too eager.
- **CM-E3 — Scope drift.** If a v2 request cannot be refused by the sentence in §1, the vision has stopped constraining the product and needs restating rather than extending.
- **CM-E4 — Reactive noise.** If bindings are used once and then removed on most Projects, the drive model is producing flicker rather than musicality and the punch/sustain design needs revisiting rather than more parameters.

## 8. Open Questions

1. **Vignette and grain relative to the letterbox padding.** FX-9 asserts they treat the picture and not the pad, but the padding is added mid-chain and the correct stage order is a measured decision during build. **Blocking for architecture, not for this PRD.**
2. **Three-way overlaps.** FX-16 assumes refusal is acceptable. Confirm before the timeline work begins, since the alternative is a resolution rule that affects the frame grid.
3. **How large is "curated"?** FX-19 and §4.3 both say the catalogue is deliberately smaller than what is available, without fixing a number. A concrete list should be settled with the Director during UX rather than chosen by the build.
4. **LUT sourcing and licence.** FX-8 bundles a LUT set; which one, and under what licence, is unresolved. Candidates were identified during research but none audited. **Blocking for build.**
5. **Where reactive Effects sit relative to *export* cost.** Preview cost is now measured; export cost is not. A binding drives a filter across every frame of a Shot at full resolution, and CM-E1 makes any regression a release concern. The preview timings suggest the cost is small — a timed-command chain measured *faster* than the static chain it replaced — but a full-resolution, full-song measurement is still owed.
6. **Is a reduced-dimension preview faithful enough to grade by?** Indexed as an assumption in §9. Speed is measured; fidelity is a judgement only the Director can make, and it should be made early, because the answer changes the preview budget rather than the design.
7. **Does an Effect Stack survive a re-render of its Shot?** The stack is a property of the Shot, not of the take, so it should — but the Director should confirm that re-rendering a Shot keeps its look rather than resetting it. `[NOTE FOR PM: this is a one-line answer that prevents a whole class of surprise.]`

## 9. Assumptions Index

- `[ASSUMPTION]` (FX-4) Returning to the Shot Info tab when a different Shot is selected is less surprising than carrying the Effects tab across a selection change.
- `[ASSUMPTION]` (FX-9) The Director never wants a vignette darkening the letterbox padding. If wrong, it is a one-line change of stage order.
- `[ASSUMPTION]` (FX-16) Three-way overlaps are rare enough that refusing them with a clear message is acceptable for v1.
- `[ASSUMPTION]` (FX-20) Half the export's dimensions at `ultrafast`/CRF 28 is a faithful enough preview to judge a grade by. Measured for speed, not yet judged for fidelity by the Director.
- `[ASSUMPTION]` (FX-21) A two-second window centred on the boundary is enough context to judge a Transition by.
