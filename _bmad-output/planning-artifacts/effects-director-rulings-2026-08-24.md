# Director's rulings — Effects and Transitions, 2026-08-24

Recorded by Amelia (Dev) during the build of Epic 8 and its retrospective. These are decisions,
not proposals. They continue the sequence begun in `effects-director-rulings-2026-08-21.md`
(R-1…R-7); a change here is a change to the PRD and the architecture spine.

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
  1.13 MB in a synthetic probe, and **405 KB** measured through the shipped extractor (0.51 MB on
  a real 3-minute master). The spine and `docs/ROADMAP.md` still carry the oldest figure.
