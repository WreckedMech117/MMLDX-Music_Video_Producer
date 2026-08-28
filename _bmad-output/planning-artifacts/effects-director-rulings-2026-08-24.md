# Director's rulings — Effects and Transitions, 2026-08-24

Recorded by Amelia (Dev) during the build of Epic 8 and its retrospective, continued through
Epic 9's first slice, and — since 2026-08-27, R-25 onward — through the rulings that settle Epic 10
before it starts. The file keeps its 2026-08-24 name because the sequence, not the date, is what a
reader follows. These are decisions, not proposals. They continue the sequence begun in
`effects-director-rulings-2026-08-21.md` (R-1…R-7); a change here is a change to the PRD and the
architecture spine.

**Why this file exists.** Every ruling below was made during implementation and, until now, lived
only in three places: the story specs under `_bmad-output/implementation-artifacts/`, which
`.gitignore:20` excludes from version control; code comments; and commit messages. A search of
`planning-artifacts/` for "numpy", "Snap to" or this date returned nothing, while the spine
contradicted the shipped code in roughly a dozen places with no record of why. This is
retrospective action item **R1** — see `epic-8-retro-2026-08-24.md`, process lesson **P1**.

Each ruling names the alternatives that were rejected, because a decision without its discarded
options reads later as an arbitrary preference.

---

## R-8 — numpy is a declared dependency, and AD-25's "no new dependency" is amended

`pyproject.toml` declares `numpy`. AD-25 and FX-NFR-4 say *"no package is added to the runtime
dependency set"*, and the literal reading is now false.

**The evidence that made it acceptable:** `git show cab8038 -- uv.lock` adds **no new
`[[package]]` block**. numpy was already locked transitively through `faster-whisper`
(via `ctranslate2` and `onnxruntime`), so nothing new installs; a declaration was added to make an
existing fact honest. *Rejected:* a hand-rolled radix-2 FFT in pure Python (~25–40 s for a
3-minute song against 185 ms), and pushing the DSP into ffmpeg's own filters, which would have
moved the computation out of the application's language entirely.

**Consequence:** SPINE:135, SPINE:188, SPINE:193, `BUILD-ORDER.md:40` and `epics-effects.md:63`,
`:80`, `:176` all still state the un-amended rule and need reconciling (retro item **R2**).

## R-9 — `effects.py` is created by slice A, not slice B

AD-28 mandates one fingerprint function living in `effects.py`; `BUILD-ORDER.md:56` puts
`effects.py` in slice B; story 8.1 is slice A and needs the fingerprint to decide envelope
validity. Story 8.1 creates the file, minimally, holding fingerprinting only.

*Rejected:* defining the function in `audio.py` and amending AD-28, and a third shared module.

**Consequence:** `BUILD-ORDER.md:27`'s claim that slices A and B *"share no files"* is false as
shipped. The spine predicted the collision its own build order denies — AD-28's `Binds:` line
names FX-1, a slice-A requirement (retro item **R3**).

## R-10 — A generated song is analysed on a path change, never on a hash

A song that arrives from a render is measured when `apply_job_history` fills `Song.path`,
detected by comparing the path in memory before and after, at both async call sites that own it.

*Rejected:* analysing lazily on the first envelope read, which would have made a read-only
endpoint write; and accepting the gap.

**Consequence:** the two-second render-status poll must never hash the song file. The trigger is
an in-memory string comparison precisely so the poll stays free.

## R-11 — A failure's reason is recomputed at read time, never stored

The reasons an analysis is absent — no song, audio pending, ffmpeg missing, undecodable, changed,
unreadable sidecar, record disagreement — are derived when asked and never written to the
manifest.

*Rejected:* a `last_failure_reason` field on `SongAnalysis`.

**Consequence:** a stored string describing a past attempt is the flag AD-21 and standing law 5
forbid; it would also have needed its own adopt guard and staleness rules. The reasons are
computed by `analysis_absence_reason` and `song_envelope_report` in `app.py`.

## R-12 — A measurement can be re-taken, and `POST /song/analyze` is how

Without it, `SONG_CHANGED`, `WRITE_FAILED` and `FFMPEG_MISSING` are states a Director cannot
clear except by re-importing the song. The route is one request, one measurement, one answer — not
a poll and not a job lane, so the frozen "no polling endpoint" clause stands.

*Rejected:* deferring the route to a later story, and deleting the unused `force` parameter.

**Consequence:** `force` gains its production caller, and Treatment story 16.2 can call the same
entry point skippably-by-fingerprint from outside the import route.

## R-13 — Beat markers are drawn on the Timeline waveform only

The Song page's waveform is unchanged. *Rejected:* both waveforms, which would have meant two
placement expressions (percent-of-duration versus `t × pixelsPerSecond`) for one feature.

**Consequence:** markers appear where cutting happens, which is also where R-15's snapping lands.

## R-14 — View settings live in the browser session, not in machine preferences or the manifest

The beat-marker toggle and the snap-target selection persist in the existing `mvp-session`
`localStorage` store beside panel, zoom, volume and the playhead magnet.

*Rejected:* `preferences.py`, which holds one key and it is a GPU policy; and the project
manifest.

**Consequence:** no route, no model change, no `_adopt_*` guard. How a Director likes to look at
and drag on their timeline is not part of the video. The stored selection carries **three**
states: absent means every kind, an empty array means the Director turned everything off, and a
populated array is their subset — conflating absent with empty is the bug this clause exists to
prevent.

## R-15 — A dragged edge snaps to phrase gaps as well as beats, and one snapper decides both

Story 8.3's acceptance criterion promised snapping *"alongside the existing lyric and phrase
boundary targets"*. **Verified against `93f4c77`: no such targets existed on a boundary drag.**
The only drag-time target was the playhead magnet; lyric and phrase snapping lived solely in the
server-side batch "Snap cuts" button. The AC described behaviour the application never had.

The ruling was to make the AC true rather than narrow the story: gap targets join the drag, served
by a read-only route computing them with **the same `timeline.py` functions the batch button
uses**.

*Rejected:* porting `vocal_gaps` and `_gap_snap_target` into JavaScript — the second snapper
`timeline.py:2152` names as this codebase's defect; and snapping to raw vocal-span edges, which
would offer a target `SNAP_CLEARANCE_SECONDS` exists to avoid.

**Consequence:** a dragged cut and the batch button choose the same second by construction, not by
agreement. FX-3 and story 8.3's AC still promise a *lyric* target that does not exist and needs
amending (retro item **R5**). And the AC's *"with no Song Envelope, boundary editing behaves
exactly as it does today"* cannot hold literally — a transcribed but unanalysed song now gains gap
snapping it did not have.

## R-16 — Snap tolerance is capped by local target spacing, and no kind is exempt

Every target's pull is capped at a fraction of the distance to its own neighbours, so a dead zone
always survives between targets and a cut can be placed deliberately off every one of them.

**Measured, and the reason this is a ruling rather than a detail:** beats sit ~6.5 px apart at the
16 px/s default. Sweeping the span between two beats, 33% of it is free with beats alone and
**0% free** once a playhead is parked mid-gap at its flat 8 px. Exempting the playhead broke the
story's central promise in the *default* configuration, where every kind is active and the
Director parks the playhead exactly where they are working.

*Rejected:* a flat pixel radius, and a Director-tunable seconds tolerance, which is not
zoom-invariant.

**Consequence:** when Epic 10 or 11 adds a snap target, it becomes a point in the same plan rather
than a second magnet with its own radius.

## R-17 — One "Snap to" selector says what a drag will land on

Issued mid-implementation, amending frozen intent:

> "In regards to all the different snap points there should definitely be a dropdown selector or
> something to help isolate what set of snap points dragging snaps to."

A single multi-select lists every target kind; any subset may be active, including none. It
replaces the playhead magnet's icon so there is one place that answers *what does dragging snap
to*; the magnet's behaviour is unchanged.

**Consequence:** the magnet receives the active kinds as a **set**, so the pure function never
counts them and a new kind needs no signature change. Story 8.2's beat-marker *visibility* toggle
is deliberately **not** folded in — markers are drawing, snap targets are dragging, and a Director
may want to see beats without snapping to them or snap to beats they have hidden. Folding them is
an Ask First.

## R-18 — The Snap to rows say what is missing and offer the measurement

Rows derive from the `measured` and `analysed` flags the targets route already serves. A kind
whose input is missing says so plainly; where this application can supply that input, the row
offers the action. This is the answer the UX spine specified from the start — *"the refusal names
the thing and the reason and offers the action"*.

*Rejected:* a silent backfill on project load, which does work without asking and leaves the
computed reasons reaching nobody; and a plain toolbar button, which sits where the Director is
looking at the song rather than where they discover the envelope is missing.

**Consequence:** the action is offered **only while the row cannot pull**, and disappears once it
can. A Director with a working analysis therefore has no way to force a re-measurement from the
interface — acceptable today, and a one-line change should SPINE:242's unresolved 30 Hz / 8-band
assumption ever be tuned.

## R-19 — Onsets are reference marks, and the band says so

The waveform draws beats and onsets; only beats are snap targets. Rather than promote onsets to a
fourth snap kind, the marker control states what each kind is for.

**Measured:** onsets run 2.9–4.1 a second against 2.0–2.1 beats on this project's own masters, so
at the default zoom they sit ~5.6 px apart and R-16's cap would give them a ~1.9 px pull — a target
in name only, bought with the eight lookup tables a new kind costs.

*Rejected:* adding an onset snap kind, and no longer drawing onsets, which would lose sight of
transients that are not on the beat grid.

**Consequence:** a tick a Director cannot land on is fine; a tick they cannot land on and were
never told about is the contradiction. A test asserts the relationship — every kind the band draws
is either offered by the selector or named in the help as not being somewhere a cut lands.

## R-20 — The empty-stack identity guarantee is an argv guarantee, not an mp4 guarantee

Story 9.1's acceptance criterion — *"a Project with an empty stack everywhere exports
byte-identically to the file it produces today"* (`epics-effects.md:286`, stated as FX-5 at `:36`
and repeated for story 9.4 at `:388`) — is untestable read literally. An mp4 out of this pipeline
is not byte-reproducible at all, so two runs of the **same** export differ with no effects
involved anywhere.

**Measured 2026-08-25, during slice B:** eight renders of one identical grained chain through this
project's own `libx264 -preset veryfast` produced **two distinct pictures**; the same chain's
output *before* the encoder was bit-identical across ten runs, and pinning the encoder to a single
thread collapsed the encoded results to one as well. Multi-threaded libx264 is not bit-exact on
high-entropy input, and grain is what makes a render entropic enough to expose it — an effect-free
export is stable only because its input is not.

The criterion is satisfiable, and satisfied, one level down: with no effects, `trim_args` builds
the byte-identical command it always built.
`test_the_default_preset_is_draft_and_draft_is_what_this_application_already_built`
(in `tests/test_assembly.py`) pins that argv against the written-out `TODAYS_TRIM_ARGV` constant;
it predates the epic (`611594b`, 2026-08-20) and `d8b8afb` did not touch it — that commit changed
five files and `tests/test_assembly.py` is none of them. Slice B added its own
`test_a_shot_with_no_effects_builds_exactly_what_this_application_builds_today`
(in `tests/test_effects.py`), which passes empty stage groups and asserts the same argv written
out rather than derived.

> **Citations corrected 2026-08-26, and the *form* corrected with them.** This paragraph used to
> cite `tests/test_effects.py:215`. **The test has never been at line 215.** It was at 265, then
> 267, was at 271 when Epic 9's retrospective checked it on 2026-08-26, and is at **272** by the
> end of that same day — it moved again between the finding and the fix. The companion citation
> was checked rather than assumed and *was* right: `test_the_default_preset_is_draft…` sat at
> `tests/test_assembly.py:515` exactly.
>
> Being right was luck of timing, not accuracy of method, and both line numbers are now gone.
> **In a tree that moves this fast a line number is the wrong citation:** it decays silently, it
> cannot be verified by `grep`, and a stale one sends a reader to an unrelated assertion that
> looks close enough to believe. A test's full name is unique, stable across every edit above it,
> and locatable with one search. Cite the file and the name; cite a line only for something that
> has no name.
>
> The mechanism this ruling states is unaffected — the tests exist, they pin what R-20 says they
> pin, and both were re-run. What was invented was the instance, which is the failure this
> project's own memory records under *"verify named instances in reviews"*.

**The ruling:** slice C asserts argv identity for the empty stack and never compares exported mp4
bytes. A determinism claim about rendered output is asserted on raw frames off the filter graph —
which *is* reproducible — never on the encoded container.

*Rejected:* pinning `-threads 1` on the export so the file becomes byte-reproducible, which buys a
test property with every Director's export time and lies outside slice B's two splice points; and
narrowing the criterion to "an effect-free export looks the same", which no test can assert at
all.

**Consequence:** FX-5, story 9.1's closing AC and story 9.4's all still say *byte-identically*
about a file when they mean the argv, and want amending to say so. The measurement is also carried
in `docs/BUILD-HANDOFF.md` under the ffmpeg-and-encoding caveats, where slice C will meet it.

## R-21 — A new Shot may arrive carrying a validated stack, and the one-writer claim was never the guard that mattered

Slice C1 shipped `_adopt_shot_effects` giving any Shot the stored project does not hold an
**empty** stack, and its docstring justified that with a security property: *"`PUT
.../shots/{shot_id}/effects` is the one writer, and it is what keeps this field out of reach of
anything a model can call."* Two reviewers then found, independently, that the same rule loses the
look on **Split and Duplicate** — both are client gestures that mint a new id and save through
`PUT .../shots`, so the second half of a split came back ungraded. `models.py:840` had already
called that outcome a defect in the same commit that shipped it.

The fix validates a new Shot's arriving stack through `validate_stack` and keeps it, refusing the
whole write when it does not compose. **This widens the write surface**, and the one-writer
sentence is no longer true: a client can now land a stack through `PUT .../shots` and
`PUT /api/projects/{id}` by inventing a shot id.

**Checked before accepting it, because the docstring staked a security claim on it.** The model
cannot reach this. `director/chat`'s `apply_shots` constructs a new Shot from three named fields —
`Shot(start=item.start, duration=item.duration, prompt=item.prompt)` at `app.py:13885` — and
mutates only those same three on an existing one. It never accepts a client body, and no tool
schema declares `effects`. So what actually keeps filter configuration away from a model is the
tool schema and that explicit construction, **not** the adopt guard. The guard's stated reason was
wrong even while its conclusion was safe.

The two generic routes are reachable by any HTTP client — and any client that can call them can
equally call `PUT .../effects`. The widening therefore grants no capability to anyone who did not
already have it, and AD-27 still holds absolutely: nothing reaches a filter string unvalidated.

**The ruling:** an arriving stack on a Shot the store does not hold is kept when it validates and
refuses the write when it does not. `PUT .../effects` is the one route that changes an **existing**
Shot's stack, and that narrower sentence is the one to state. A guard's docstring may not claim a
security property that a different mechanism is actually providing — the claim outlives the
reading, and the next person to weaken the guard will believe they are weakening a defence.

*Rejected:* a client-side follow-up write after each split, which is not atomic — the split would
land and its look could fail separately, leaving exactly the ungraded half this fixes.

## R-22 — Supersede means stale, so an identical preview request joins rather than restarts

Slice D1's frozen matrix said *"Request while one is in flight → the in-flight one is cancelled"*,
without qualifying which requests. Implemented literally, a request whose fingerprint **matches**
the render already in flight also cancels it and starts an identical one. The implementer flagged
it rather than quietly carving out an exception, which was right — it was frozen intent.

It is wrong, and the matrix row is the thing at fault. AD-24 exists so that a Director dragging a
slider is not shown five outdated pictures in sequence: it discards **stale** work. A request for
the fingerprint already rendering is not stale work — it is asking for exactly what is underway.
Restarting throws away completed effort to produce a byte-identical answer, and it pushes onto the
client a guarantee no client can honestly make, since a retry, a poll, or a re-render on window
focus each produce a duplicate request. Under identical requests arriving faster than a render
completes — measured at ~116 ms — nothing would land at all.

**The ruling:** supersede applies when the arriving fingerprint **differs**. An identical request
joins the render in flight and receives its result. A differing request cancels as before, and the
publish gate is untouched: a superseded render still can never land its output and be served as
current, which is the property that actually matters and which the fingerprint comparison must not
weaken.

*Rejected:* requiring the client to fire exactly once per change. That is the same class of demand
as "do not double-submit a form" — true of a careful client on a good day, and false of every real
one.

## R-23 — A preview shows the Shot's own window, not the fragment the export ships

Story 9.2 says the Preview Clip *"covers the Shot's exposed window, not the whole over-rendered
take"*. Under an overlap that is ambiguous, because `assembly_plan` subtracts a later Shot's window
from an earlier one — so a half-covered Shot ships only its uncovered fragment, and "exposed" could
mean either the Shot's window or that fragment.

**The ruling: the Shot's own window.** The contrast the sentence draws is window against *take* —
the failure it names is previewing over-rendered footage that will never ship — and the shipped
fragment reading would show a Director a clip that begins mid-look, which is the harder thing to
judge a grade against. Grading is looking, not shipping.

Two consequences follow and are accepted. A preview can show frames the export will not include,
where a later Shot covers them. And the preview deliberately does **not** honour the export's own
staleness refusal: a Shot whose window moved since approval refuses at export and previews fine, at
its current window, because the point of looking is to decide what to ship rather than to be told
you cannot yet.

This wants revisiting when Epic 11 gives an overlap a transition, since a transition is precisely a
thing that happens in the covered region.

## R-24 — A preview shows the take the export will ship, so it can change footage as well as grade

Slice D2 surfaced an edge nobody chose: the preview renders `approved_output`, because that is what
an export ships, while the Monitor plays `latest_output`. Where a Shot has a newer take that has not
been approved, turning on a look therefore changes **which footage is on screen** as well as how it
is graded, and nothing says so.

**The ruling: the preview stays on the approved take.** A preview whose only job is to predict the
export must be of the footage the export will use; previewing the latest take would grade a picture
that is not going to ship, which is a worse lie than a surprising cut. This is the same reasoning as
R-23 — a preview answers *what will this look like*, and the answer has to be about the thing that
goes out.

Two consequences, both accepted and both now handled rather than silent:

- A Shot carrying a stack and a take that has **never been approved** would have shown an ungraded
  picture and said nothing at all — the "control that appears to do nothing" failure this repo
  rejects. It now carries a named note and makes no request, since the route would refuse by name
  anyway.
- Where latest and approved differ, the footage changes when the look goes on. Left as it is,
  because the alternative is worse, but it is the kind of surprise that earns a sentence in the
  interface the first time a Director reports it.

**Also settled here, because D2's spec did not flag it:** the Preview Clip **free-runs and loops
while the transport is stopped**, and follows the playhead only while it is playing. Story 9.2 asks
for a looping preview and forbids a frozen frame, and the Monitor's video is otherwise paused
whenever the transport is — so honouring both meant changing what a Shot **with** effects shows
while stopped. A Shot with no effects is untouched, which is what the Ask First boundary protected.

**A correction of fact for anyone reading D2's spec:** it says "when the Shot is selected"
throughout. The Monitor shows the Shot under the **playhead**, and selecting a clip does not move
the playhead. The implementation is keyed to the playhead, which is the only thing "the Monitor
plays" can mean.

---

## Rulings of 2026-08-27 — Epic 10's shape, settled before it starts

Made after Epic 9 closed at `0b0bb96` and before Slice E began. They continue this file's sequence
rather than opening a new one: it is the same feature, and someone asking *what has the Director
decided about effects* should find one answer in one place. Same rules — each names what was
rejected, and a change here is a change to the PRD and the architecture spine.

## R-25 — Grain cannot be driven, and Epic 10 ships the drivable subset

A `sendcmd` can only move an ffmpeg option the filter declares as runtime-settable — the `T` flag
in `ffmpeg -h filter=<name>`. **Measured 2026-08-27 against this project's own ffmpeg 7.0**, and the
split is not where the epic assumed it was:

| Filter | Runtime-settable options |
|---|---|
| `noise` (Grain) | **0 of 25** |
| `vignette` (Vignette) | **0 of 8** |
| `unsharp` (Sharpen) | **0 of 18** |
| `shufflepixels` (Pixel Shuffle's leg) | **0 of 10** |
| `edgedetect` (Edge Treatment's leg) | **0 of 4** |
| `eq` | 8 of 9 |
| `colorbalance` · `hue` · `gblur` · `deband` · `chromashift` · `pixelize` · `lutyuv` · `drawgrid` | every option |
| `crop` | 6 of 8 — `w`, `h`, `x`, `y` among them |
| `rotate` | `angle`/`a` |
| `scale` | `w`, `h` among 36 |
| `blend` | 15 of 19 — **including `all_opacity`** |

**A `sendcmd` aimed at a filter that takes no commands does nothing and says nothing.** Frames come
back byte-identical, rc 0, no warning even at `-v warning`; a command addressed to a target that is
not in the graph at all is discarded the same silent way. There is no error to catch and no output
to compare against, which is why this is a ruling and not a detail.

**The ruling:** Epic 10 ships the drivable subset. A bind glyph on a parameter that cannot be driven
stays `--dim` and **refuses by name**, saying which ffmpeg filter takes no runtime commands — the
refusal shape this feature already uses everywhere else. A dial that binds and then does nothing is
the "control that appears to do nothing" failure R-24 rejects by name, and here it would be
invisible from the outside: the export succeeds, the picture is simply un-driven.

**Two acceptance criteria are amended by this, and they are amended rather than quietly narrowed.**
Story 10.1's *"no parameter is specially privileged and none is excluded by category (FX-12)"*
(`epics-effects.md:540`, stated as FX-12 at `prd.md:228`) cannot hold: nothing about the *category*
excludes anything, but five filters this application already composes take no commands, and that is
a fact about ffmpeg rather than a design choice available to be made differently. Epic 10's own
headline — *"grain surging on the kick"* (`epics-effects.md:152`) — names as its example the one
effect that cannot do it. Both want rewording: the privilege clause to say *no category is
privileged and the drivable set is a measured property of the filters*, and the headline to reach
for a look that is actually drivable. **Grain surging on the kick is still the right thing to want**,
which is why it becomes a story rather than a deletion.

*Rejected:* **re-composing Grain as a driven `blend` of a `noise` leg inside this epic.** It would
work — `blend`'s `all_opacity` is settable, which is exactly how Edge Treatment and Pixel Shuffle
already earn their dials — and it is deliberately not done here. It changes a composer, so it
changes the composed chain, so it changes every affected preview's name and every affected export's
argv; and Epic 9 shipped a 26-pixel black bar down the left edge of Scanlines the last time a
composer changed quietly (`e4aec46`). That belongs in its own story **with a measurement attached**
— the frame cost of the branch, and the frame-guard interaction AD-17's amendment describes — and
not inside a UI epic. *Also rejected:* letting the glyph appear and the binding store, resolving to
nothing at export, which is the silent failure above wearing a checkbox.

> **A premise of this ruling was verified and half of it did not survive, so it is recorded here
> rather than discovered in the build.** The five filters above are correct. **The inference from
> filter to effect is not**, for two of them: `edge_treatment` and `pixel_shuffle` do not hand the
> Director's dial to `edgedetect` or `shufflepixels` at all. Both compose as a **branch** — the
> treated copy crossfaded back over the untouched one — so `strength` and `amount` are written into
> `blend=all_mode=normal:all_opacity=…`, and `all_opacity` **is** runtime-settable. Their headline
> dials are drivable today, with no recomposition, exactly as the rejected Grain remedy would have
> made Grain's.
>
> **Drivability is a property of the (parameter → filter option) pair, not of the effect and not of
> the family**, and that is the granularity the bind glyph must be decided at. Read off the
> composers on 2026-08-27, the parameters that **cannot** be driven are exactly:
>
> * `grain.strength`, `grain.seed` → `noise`
> * `vignette.angle` → `vignette`
> * `sharpen.amount` → `unsharp`
> * `edge_treatment.low`, `edge_treatment.high` → `edgedetect` — but **`strength` → `blend`, drivable**
> * `pixel_shuffle.block`, `pixel_shuffle.seed` → `shufflepixels` — but **`amount` → `blend`, drivable**
> * `lut_look.lut` → `lut3d`'s `file=`, which has no timeline flag (spine, *How a filename reaches a
>   filter*). `interp` is settable, and swapping interpolation mid-clip is not a look anybody asked
>   for.
>
> Everything else in the catalogue lands on `crop`, `scale`, `overlay`, `rotate`, `eq`,
> `colorbalance`, `hue`, `gblur`, `deband`, `lutyuv`, `chromashift`, `pixelize`, `drawgrid` or
> `blend`, and is drivable. **The ruling stands as stated** — ship the drivable subset, refuse the
> rest by name — and it excludes far less than it appeared to.

## R-26 — A `ParameterBinding` lives on `EffectSpec`, keyed by parameter name

Three placements were available and only one survives contact with what Epic 9 shipped.

**`(effect id, parameter)` is ambiguous.** `EffectSpec` is `{effect, enabled, parameters}` and
carries **no id** (`models.py`); stack entries are positional, and duplicates of one effect are
legal and composable — verified 2026-08-27, two `grain` entries validate and compose to two `noise`
stages at different strengths. There is nothing there for a binding to name.

**`(stack index, parameter)` breaks under Story 9.4**, which shipped stack reordering. An index is
correct until the Director drags a card, and then it is silently pointing at a different effect —
the worst available failure, because the binding still resolves.

**The ruling: the binding lives on its own entry, keyed by parameter name.** A binding then travels
with the card it belongs to, so it survives reorder, copy, split and duplicate **for free**:
`effects` is already in `SHOT_PLAN_CONTENT_FIELDS` (`models.py`), which is the one classification
Split, Duplicate and copy-a-look all read. It is also what makes copy-a-stack (FX-6) carry its
bindings correctly with no new code, which is the property **AD-26** was written to make meaningful
— the whole-song band average is identical in every Shot's panel, so the band the Director chose
against the reference is the same band on the target.

**And it stays clear of the import-time classification gate.** A new field on `Shot` is not a schema
decision in this codebase, it is a startup failure: `_withheld_fields` (`app.py`) proves
`visible | withheld` is exactly the model's declared surface and raises `RuntimeError` **at import**
for anything unclassified, so the application refuses to start until somebody decides whether the
Director's prompt should carry it. A binding inside `EffectSpec` adds no `Shot` field at all, and it
inherits `_adopt_shot_effects` — the twelfth of that route's thirteen recorded guard holes — rather
than needing a fourteenth adopt helper of its own.

*Rejected:* a `bindings` list on `Shot` parallel to `effects`, which is two structures describing one
card, plus that fourteenth `_adopt_*`; and adding an `id` to `EffectSpec` so `(effect id, parameter)`
would work, which changes the three-key shape `effects.validate_stack`, the wire and every stored
manifest already share, for a key needed only because the binding was put somewhere else.

## R-27 — The Drive readout draws the compiled `sendcmd` values themselves

Story 10.3's acceptance criterion is that *"the signal drawn is the same one the export will use,
not an illustration of one"* (`epics-effects.md:600`; FX-22, FX-NFR-3). Serving the **compiled
values** is the strongest available form of that guarantee: it reuses the pure compiler rather than
deriving the same curve a second way, and it makes the Drive readout a **test artifact** — what the
canvas draws is the text that will be handed to ffmpeg — instead of a second renderer that can drift
from the first. FX-NFR-3's *one engine describes an effect*, applied to the drive signal.

**The per-frame `bands` array stays on disk.** It is about 98 % of a 469 KB sidecar, against
manifests of 110–190 KB, and AD-20 exists because of that ratio. Compiling on the server and serving
the result sends the readout exactly what it draws and nothing else.

**`SERVED_ENVELOPE_KEYS`' standing rule holds:** *a consumer is necessary and not sufficient*
(`app.py`). A key does not join that tuple because something on the page would read it; it joins
because nothing else can answer the question. The Drive readout has a compiler that can.

*Rejected:* shipping the raw band series to the browser and drawing the drive model in JavaScript —
the second-implementation shape R-15 already refused for snapping, and worse here, because it is a
second *renderer*: the picture and the export could disagree while every automated gate passed.

## R-28 — A bound parameter composes its stage even at the identity value

Every composer that has an identity value returns `()` at it — `punch_in` at zoom 1.0, `grain` at
strength 0, `vignette` at angle 0, `exposure` at amount 0. That is deliberate, and it is what makes
an empty stack cost nothing. **It also means binding a parameter that currently sits at rest would
produce no filter instance for `sendcmd` to address**, and a command aimed at a filter that is not
in the graph is discarded silently at rc 0 (R-25). The binding would be **inert, and inert with no
symptom**: the export succeeds and the picture never moves.

**The ruling: a parameter carrying a binding composes its stage, at the identity value or not.** The
Director asked for the picture to move; the resting value is where it moves *from*, not a statement
that there is nothing to do.

**It costs the empty-stack argv guarantee nothing.** R-20's guarantee is about a Shot with no
effects, and this rule fires only on a Shot that carries a binding — a Shot with none composes
exactly what it composes today, character for character.

> **Verified 2026-08-27 before recording, and the wording is narrowed by one clause.** *Every*
> composer does not return `()` at its identity: **23 of the 25 do**, and the two that do not have
> no identity to return at. `mirror` takes a choice of axis and its own docstring says *"a mirror
> that mirrors nothing is not a look anybody asked for"*; `lut_look` always names a file. Neither
> declares a number, so neither is bindable in the first place and neither is touched by this
> ruling. Every composer that declares a drivable number does return `()` at rest, which is the
> clause the ruling actually rests on.

---

## R-29 — Geometry cannot be driven either, and the recompose is a follow-up story

**Driving both dimensions of `crop` or `scale` aborts ffmpeg.** Measured 2026-08-27 on ffmpeg 7.0
and reproduced independently: a `sendcmd` moving `w` *and* `h` gives
`Assertion best_input >= 0 failed at fftools/ffmpeg_filter.c:1923`, **rc 3 and a 48-byte truncated
file**, at any pair of timestamps, with or without `-frames:v`. Moving one dimension alone is fine.
**A zoom is never one dimension alone.**

So `punch_in.zoom`, `slow_zoom.zoom`, `handheld_shake.amplitude` and `dutch_tilt.angle` are
undrivable as composed today, and the whole Geometry family is out of Epic 10's drivable subset
alongside grain (R-25).

**The consequence worth stating plainly: ffmpeg's `T` flag is not the test for drivability.** Both
`crop` and `scale` carry `T` on `w` and `h`. A drive table built from the flag — which is exactly
what R-25's first draft was built from — would have shipped a punch-in that **crashed every export
it appeared in**, and crashed it with a written output file rather than a clean failure. Drivability
is a property of `(parameter -> filter option)` **verified by running it**, not by reading a flag.

**The ruling: Epic 10 drives Texture, Grade and Stylize.** A Geometry parameter's bind glyph stays
dim and **refuses by name**, saying that ffmpeg aborts when both `crop` dimensions move. Story
10.1's AC and the epic's headline are amended a second time — the headline has now lost *both* its
examples, *grain surging on the kick* and *the frame breathing with the bass*, and the honest
reading is that the epic was written from what the filters looked like they could do rather than
from what they do.

**The way back is a composer change, not a compiler one, and it is a follow-up story with a
measurement attached** — the same shape as R-25's grain recompose, and for the same reason: a
composer change landing mid-UI-epic is how Epic 9 shipped a 26-pixel black bar. A bound geometry
stage would compose its companion `crop` sized for the binding's **whole reach** rather than its
resting value, so only one dimension moves at render time. `dutch_tilt.angle` already drives
`rotate`'s `a` cleanly on its own and is the cheapest first case.

---

## R-30 — The script stays cwd-relative, and AD-22's stated cause was false

AD-22 said `sendcmd=f=` cannot take an absolute Windows path because **the drive-letter colon
parses as a filter option separator**, failing with `No option name near 'frame'`. Re-measured
2026-08-27 on ffmpeg 7.0, and reproduced independently: **that is not true.**
`sendcmd=f=C:/dir/name.cmds` — forward slashes, plain, unquoted — renders correctly, drive-letter
colon and all, and keeps working when the directory name holds a space. What actually breaks it is
a character the **filtergraph** splits on: a comma gives `No such filter: 'comma/t1.cmds'`, and a
path holding `=` or `&` gives `No option name near '...'`. That last message is the one AD-22
quoted, so the original measurement was almost certainly taken on such a path and the cause
misattributed to the drive letter.

**And the two remedies were never different.** `lut3d`'s single-quoted colon-escaped form
`'C\:/dir/name.cmds'` survives a directory named `hard, dir; [x] =y & 100%` for `sendcmd` too.
The spine's amendment of 2026-08-26 — which corrected a blanket rule into two per-filter rules —
was right that the blanket rule was wrong and wrong about why they differ.

**The ruling: the script stays a bare relative filename with the process cwd set to its directory,
and `run_tool` gains a `cwd` in the next slice.** Not because the alternative fails, but because a
generated script name is `[a-z0-9_.-]` only and needs no escaping, and because **an absolute path
inside the composed chain is an absolute path inside the preview cache key** — the chain became
`preview_fingerprint`'s fourth input on 2026-08-26, so a project that moved on disk would
invalidate every preview it owns. AD-22's stated cause is struck in the spine and in
`docs/BUILD-HANDOFF.md`.

**The lesson, which is the reason this is a ruling and not a footnote: a remedy can be correct
while the reason given for it is false, and the false reason is what the next reader generalises
from.** Two of Epic 10's first three days were spent on measurements that contradicted recorded
causes — this one, and the `T` flag in R-29.

---

## R-31 — The Trigger Floor is not a hairline on the Drive readout, because it is not on that axis

Story 10.3's acceptance criterion, UX-DR7 and `DESIGN.md` §6 all say the same thing: the readout
draws *"the Trigger Floor as a `--dim` hairline"* through the envelope. **It cannot, and this is a
consequence of R-27 rather than a disagreement with it.**

The floor is compared against the **band level** — `_punch_series` computes
`0.0 if raw < floor else envelope`, and `_sustain_series` gates on `raw >= floor`, where `raw` is
the measured band. R-27 makes the readout draw the **compiled parameter value**. Those are
different units. A horizontal line at the floor's number, drawn on a value axis, names a value the
floor has nothing to say about — a picture that reads as information and is not. The only drawing
where the floor and the signal share an axis is one of the *band series*, and serving that is
exactly what R-27 rejected, because it puts a second renderer between the picture and the export.

**The ruling: the readout draws a `--dim` rest line — where the parameter sits when nothing fires
— and expresses the floor as ground beneath the silenced runs.** That satisfies the clause of the
same AC that carries the actual requirement, *"where the envelope falls below the floor it draws
`--dim`, so a silenced passage looks silenced rather than merely low"*, and it is what the UX flow
describes a Director doing: *raising the Floor until the quiet verse passage drops to `--dim`.*

**And colour alone could not have carried it anyway, which is the measured half.** Below the floor
a `punch` drive is exactly zero, so a silenced run's line lies *on* the rest line, in the same
token — "the floor shut this" and "nothing is happening here" draw identically. The state needs
width, not hue: a 4px ground bar under the silenced runs, which grows as the Floor rises. That is
also the honest answer to why the epic could specify a hairline in good faith — on a band-level
axis it would have worked.

Two amendments follow and are made: the AC and UX-DR7 in `epics-effects.md`, and `DESIGN.md` §6 and
`EXPERIENCE.md`'s readout section.

---

## R-32 — The readout's spoken equivalent lives under the readout, not in the band panel

UX-DR7 says the Drive readout is `aria-hidden` and *"the facts it shows (peak time, whether the
binding fires at all) are also stated in text on the band panel."*

**The band panel is closed most of the time.** A screen reader meeting the canvas with no panel open
gets nothing at all — which is the case the requirement exists to serve. Stating the equivalent in a
different panel, for one parameter, that is usually not on screen, satisfies the sentence and not
the rule.

**The ruling: the facts are stated in a caption immediately beneath the canvas, inside the same
`<figure>`** — which binding is drawn, where its drive peaks, and whether it fires at all. The band
panel points at the readout without restating the numbers: repeating them is the doubled-sentence
defect Story 10.2's browser QA found on the unresolvable panel, where one absence was explained
twice on one screen.

The standing rule is unchanged and is what this ruling serves: **every canvas in this application
has a non-canvas equivalent**, and the equivalent has to be reachable wherever the canvas is.

---

## R-33 — `EffectSpec` gets a stable id, and bindings are adopted rather than refused

Epic 10's retrospective reproduced three defects and an investigation established they are not
three bugs. **A1** (a deleted `.cube` empties the binding census for the whole project and refuses
every Split and Duplicate of every bound Shot, and on the narrow route makes deleting the binding
the only accepted write), **A3** (a generic write relocates a binding between two cards of one
effect, changing the rendered picture at 200) and **A4** (one held binding multiplies onto
arbitrarily many new Shot ids in a single write) are three consequences of one contract's shape:
**"carry, never mint"**, which compares a multiset of validated `ParameterBinding`s because an
`EffectSpec` has no identity to compare instead.

**The ruling: give `EffectSpec` a stable id and adopt a card's bindings from the stored card,
retiring `binding_census` and `carried_bindings_refusal`.**

**The reasoning, because it is the part worth keeping.** Epic 10's stated obstacle was that *an
`EffectSpec` has no id, so adopting means matching stack entries positionally, and positional
matching is what R-26 rejected.* That is a reason not to **match** positionally. It was never a
reason not to **have** an id. R-26 rejected `(effect, parameter)` and `(index, parameter)` as
*addressing* schemes precisely **because** there was no id, and nobody then asked whether there
should be one. R-26 decided **where a binding is stored** — keyed by parameter name on its own card
— and that stands untouched; this changes the **addressing**, which is the question R-26 was never
asked.

**AD-16 already prescribed this.** Its Rule reads: *"adopts them from the stored Shot, via the
established `_adopt_*` idiom… a body that omits them, or invents them, does not change them."* Epic
10 substituted a refusal mechanism, argued it at length in `models.EffectSpec`'s docstring, and it
produced three holes the idiom would not have. The substitution is the finding, not the argument.

**A3 has no fix inside the old contract, and that decided it.** The census key can only be content
or position. Content-keyed refuses the slider drag the route exists for — *measured: that same drag
changes a bound card's resting value at 200 today*. Ordinal-keyed is `(index, parameter)` in a thin
disguise. Patching would have dissolved A1 and A4 and left A3 permanently open.

**What was measured before ruling, so nobody re-derives it:**

- **Zero effect cards and zero bindings exist in stored data** — 5 projects, 91 shots, no `effects`
  key on any stored shot. The migration cost is **zero**, and it will not stay zero.
- Both shapes were prototyped end to end in a scratch copy. The id plus adoption costs **14 test
  edits, every one mechanical** — ten wire-shape equality literals, two asserting a key list, two
  asserting the refusal that stops existing.
- **No fingerprint moves and no cached preview is invalidated**: `preview_fingerprint` hashes the
  composed chain plus the stored `bindings`, and `effects` reaches neither the ComfyUI payload nor
  `expansion_input`.
- All four adversarial writes come back correct with adoption in place, in a running application.

**AD-21 is not in play.** It forbids a stored *verdict* that can outlive its condition — "a second
truth that can disagree with the first". An id has no condition and nothing to disagree with. Six
models in `models.py` already carry `new_id(...)`, including `SongSection`, a repeated element
nested inside another model — exactly `EffectSpec`'s shape. `EffectSpec` is the only repeated nested
element in this schema without one.

**Two costs, accepted rather than discovered:**

1. **A wire-contract change.** Any non-browser client writing a bound Shot's stack must echo card
   ids or be refused by name. Losing a binding is otherwise indistinguishable from removing its
   card, which is where this whole thread began.
2. **New machinery: card ids clone.** Split, Duplicate and copy-a-stack all copy a card, so two
   Shots can hold one card id until something re-mints on collision. This is the piece no
   measurement covers yet and the piece most likely to be got wrong — it is to be designed against
   the shipped panel, not inherited from the prototype's sketch.

**How the Director could have ruled otherwise, recorded because it was close.** The feature has no
users — zero stacks exist anywhere. A1 alone refuses real work and is six lines and 0.03 ms. A3's
picture consequence is already reachable through an intended slider drag and needs two cards of one
effect to exist at all; A4's end state is what `POST .../effects/copy` produces on purpose, with an
announcement. Patching A1 and writing the other two down would have been defensible. It was not
chosen because A3 would then never close, and because the id is free today in a way it stops being
the moment a Director saves a stack.

---

## Delegated decisions

Not the Director's calls. Recorded here because they were made *for* the Director on 2026-08-27,
under rulings R-25 to R-28, and a later reader has no way to tell a delegated decision from a ruling
except by being told which it was.

- **Label only the stages that carry a binding.** An `@label` is part of the composed filter text,
  and the composed chain has been the fourth of `preview_fingerprint`'s eight inputs since
  2026-08-26 (AD-28's amendment) — so labelling every stage would rename every cached preview in
  every project on the day Epic 10 merges, for looks that did not change, and would move the
  export's argv on Shots that carry no binding at all. Labelling only bound stages keeps an unbound
  Shot **byte-identical in argv and valid in cache**, which is R-20's guarantee still holding after
  this epic.
  **The test this obliges is not optional:** every `sendcmd` target string must appear as an
  `@label` in the composed chain **produced by the same call**, asserted by string comparison the
  way every generated render input in this project already is. A mistargeted command is ignored in
  silence, so that assertion is the only thing standing between a typo and an export that is quietly
  un-driven.
- **Ship on 8 bands, and leave the band count unjudged.** The spine's *Deferred* has said since
  2026-08-24 that 30 Hz and 8 bands are an assumption Epic 8 shipped on and that nothing has judged
  them; Epic 10 gives the band selector its first consumer and still does not judge them. What Epic
  10 must not do is **bake either in**: the spectrum strip reads the band count off the measurement
  it is drawing — `band_count` on the envelope, or equivalently the length of `band_average` on the
  served subset — never a literal 8. Both the count and the rate are recorded fields on every
  envelope rather than constants (AD-20, `models.SongAnalysis`, `audio.py`), so re-judging them
  later stays a **re-measurement rather than a migration**, which is the property that made shipping
  on an unjudged assumption acceptable in the first place.

---

## Process rulings

Recorded because they governed how the work was done, not what was built.

- **Amend and patch rather than revert.** Three times a review finding's root cause was the spec's
  own non-frozen sections, which the build workflow routes as `bad_spec` — a full code revert and
  re-derivation. Each time the design was sound and only wiring or test depth was wrong, and the
  ruling was to amend the spec, record the change, and patch. No code was reverted in Epic 8.
- **Browser QA before committing anything that draws or is operated.** Made standing after Story
  8.2 shipped its first pass with the manual check unperformed and markers that buried the
  waveform. Every automated gate passed. A story that changes a control is not verified until the
  control has been operated.
- **Retrospective completeness gate overridden, on the record.** All three Epic 8 stories sat at
  `review` rather than `done`, which forces a machine verdict of `rejected`. The Director
  overrode it on the grounds that `review` is where this repo's workflow leaves a story after
  development, and the retrospective records both the machine verdict and the override.
- **Epic 8 verdict: `accepted-with-open-items`**, with 22 routed action items.

---

## What these rulings do not cover

- **SPINE:242's `[ASSUMPTION: 30 Hz and 8 bands — not yet judged against a real song]`** shipped
  unresolved. Epic 8 ships exactly those values, recorded as fields rather than constants so
  tuning them is not a migration, but nothing has judged them.
- **The envelope's size** was estimated three times and measured once: ~750 KB in the spine,
  1.13 MB in a synthetic probe, and **405 KB** measured through the shipped extractor (469 KB on a
  real 202-second master). ~~The spine and `docs/ROADMAP.md` still carry the oldest figure.~~
  **Corrected 2026-08-27:** they no longer do. Epic 8's action item 18 reached AD-20, `ROADMAP.md`
  and `docs/project-context.md`; `docs/BUILD-HANDOFF.md` was the instance it missed and was corrected
  on 2026-08-26. All four now carry 405 KB and name the two wrong figures as wrong. *(This bullet said ~~0.51 MB on a real
  3-minute master~~. Every other artifact — AD-20, `BUILD-HANDOFF.md`, `ROADMAP.md` — carries
  **469 KB on a real 202-second master**, and nothing found on 2026-08-27 supports 0.51 MB or the
  3-minute framing. Restated in the figure the rest of the record agrees on; if 0.51 MB was a
  separate reading it has no surviving source.)*
