# Three-Step Populate — Plan

**Date:** 2026-08-21
**Analyst:** Mary (Business Analyst)
**Origin:** the Director's framing — *"Populating the timeline is definitely a multi-step process.. Laying it out, lining it up, filling it in. Each of those 3 steps has its own sub processes for figuring out where the lyrics are, the good cut boundaries, what assets to use, camera flow through the shots, etc. so we have to ensure we are getting them right and each step helps build the next."*

---

## Governing thought

`populate_timeline` is one model call plus arithmetic repair doing three different jobs. **That is why its weaknesses have been hard to attribute** — a bad layout and a bad asset choice look identical from outside, and this session spent real GPU and real hours chasing symptoms that belonged to different steps.

Splitting it makes each step **verifiable on its own terms**, and lets a later step *consume* an earlier one's output instead of re-deriving it. The split is not a refactor for tidiness; it is the only way to answer "which step got this wrong".

## The evidence for splitting, measured this session

| Finding | Which step it actually belonged to |
|---|---|
| The combined ask delivered both `shots` **and** `sections` in **0 of 9 rolls** across two runs — the model reliably drops one half | Asking **lay-out** and part of **line-up** in one call |
| Count enforcement measured **no effect** (control 0/12/12, treatment 0/12/12) | Wrong lever: the failure was `shots: []`, not a short count |
| The real cause was a **schema** that never listed `shots` as required | Not a prompt problem at all — a contract problem |
| Pallet racking in 11 of 33 shots | **Fill-in**, inherited from a treatment phrase ("warehouse aisles") — no shot prompt named it |
| A wedding dress instead of the performer | **Fill-in**, a missing citation — the layout and timing were correct |
| Shot lengths "all look the exact same" | **Lay-out** only |
| Cuts landing mid-word | **Line-up** only |

Every one of these was initially investigated as "populate is bad". Under the three-step model each has an obvious owner.

---

## Step 1 — Lay it out

**Owns:** the shot *structure*. How many shots, where their boundaries fall, how long each runs, how they group.

**Sub-processes:** section structure (from the lyric sheet's `[Tag]` blocks); required shot count; window tiling within H3's band; **length variance** — the Director's standing complaint that populate produces near-uniform windows.

**Already built:** `populate_windows` (contiguous 0→song-end tiling, band-clamped, water-filled), `populate_required_shots`, `lyric_blocks`, `proposed_sections_from_alignment`, `repair_sections`, `POPULATE_TARGET_WINDOW_SECONDS = 5.2` (chosen on measured render cost, not the band midpoint).

**Missing / weak:**
- **Variance is not a first-class output.** It is a hoped-for property of the model's proposal, repaired away by the tiling. It should be a *parameter of the layout* — energy-biased per section.
- The model is asked for a layout **and** section structure in one call. Measured to fail.
- Nothing states a layout's *shape* independently of its numbers, so nothing can judge it.

**Produces for the next step:** an ordered, contiguous set of windows with section membership. Nothing about content.

---

## Step 2 — Line it up

**Owns:** aligning that structure to the music. This is the step with the most machinery already built and the least of it wired into populate.

**Sub-processes:** where the lyrics fall; where the voice actually is; good cut boundaries; which lines a shot covers; **which character sings them** (the multi-character work now in pass 1).

**Already built and strong:** Whisper `lyric_words` (word-level times), `vocal_spans`, `align_lyric_blocks` (LCS of sheet tokens against heard words), `vocal_gaps` (now word-level, 3→7 interior gaps on the real song), `snap_cut_plan` (report-then-confirm, clearance-clamped, protections honoured), `shot_vocal_overlap` (the guard that outranks a singing mark).

**Missing / weak:**
- **Populate never calls any of it.** Snapping is a separate manual action taken afterwards; the layout is produced blind to phrase boundaries and then optionally repaired.
- Nothing hands a shot *the lines it covers*. `clip_position` gives a within-block estimate; the alignment could give the actual lines.
- The Director's ruling for multi-character — **prefer snapping to one singer's lines; cite everyone when unavoidable** — is a line-up decision that currently has nowhere to live.

**Produces for the next step:** windows that fall on musical boundaries, plus per-shot facts — the lines sung, the voice present, the singer(s).

---

## Step 3 — Fill it in

**Owns:** what each shot *contains*.

**Sub-processes:** assets to cite; camera flow across shots; **narrative** (the Director named this explicitly); section look; per-shot creative intent.

**Already built:** `prompt_citations` + the structural `PlannedShot.assets` field, `prefer_identity_sheets`, `with_default_setting`, section looks from the treatment, the DP pass (`dp_prompt.py`) for camera variety, `song_audio_prose` / the H3 expansion specialist.

**Missing / weak:**
- **The DP pass is not part of populate.** Camera flow is composed after the fact, if at all.
- **Narrative has no representation.** There is no beat, arc, or through-line — the treatment is prose and each shot is written independently against it. This is the Calliope-plan story layer arriving through the side door.
- Asset choice is per shot with no view of the whole; nothing balances b-roll against performance, or notices that one asset carries 24 shots and another 3.

**Produces:** a plan ready for expansion and render.

---

## The interfaces — the actual deliverable

The value of the split is that each step's **output is inspectable and re-runnable**. Proposed contracts:

1. **Layout →** ordered windows + section membership. *Re-runnable without touching content.*
2. **Line-up →** the same windows, moved; plus per-shot musical facts (lines, voice seconds, singer). *Re-runnable without re-laying-out.*
3. **Fill-in →** citations, intents, camera, narrative role. *Re-runnable without moving a window.*

**This is the property that matters most.** Today a Director unhappy with asset choice must re-populate and lose their hand-tuned timing. Under the split, fill-in re-runs against the timing they already approved. The Director's own broad-then-detail principle depends on it: *"we start broad and then do detail passes more individually."*

## Sequencing

**Phase A — make the steps separable without changing behaviour.** Extract the three concerns inside the existing route so each produces a named intermediate. No new model calls, no new UI, byte-identical output pinned. This is the enabling step and it is testable to the byte. **— SHIPPED 2026-08-21, see below.**

**Phase B — line-up consumes the alignment.** Wire phrase-boundary awareness into the layout instead of leaving it to a manual snap afterwards, and hand each shot the lines it covers. Highest ratio of existing machinery to new code. **— SHIPPED 2026-08-21, see below.**

**Phase C — re-runnable fill-in.** A route that redoes content against fixed windows. Directly serves the broad-then-detail workflow and preserves hand-tuned timing.

**Phase D — layout variance as a parameter.** Energy-biased window lengths per section, closing a standing complaint. **— SHIPPED 2026-08-23, see below; the single-step reshaper was replaced by a constrained redistribution the same day, so one saturated window no longer freezes its span. It narrows the complaint rather than closing it: within-section uniformity is gone, and the whole-song spread is limited by the window *counts* and the render ceiling, neither of which is a variance dial.**

**Phase E — narrative.** The story layer, planned separately. This is where the Calliope Phase 5 work belongs, and it should not be smuggled in earlier.

## Director's ruling — settled 2026-08-21

**Three separate routes, chained for the first pass.**

Each step is independently callable and re-runnable; a first-run "Populate Timeline" chains all three so the common path stays one gesture. This is the answer that makes the interfaces real rather than notional — a stage that can only run as part of a chain is an internal function with extra ceremony, not a step.

Consequences that follow, and should be treated as requirements rather than options:

- **Each route is report-then-confirm in its own right**, matching `snap-cuts`, `replace-citations` and `fill-looks`. The chain therefore needs a decision about confirmation: one confirmation for the whole first pass is almost certainly right (three modal confirmations to lay out one timeline would be worse than today), but each route keeps its own when called alone.
- **Each route's output must be inspectable without running the next.** That is what lets the Director re-run fill-in against approved timing.
- **The chain is a caller, not a fourth implementation.** It must call the same three routes, not a parallel path that can drift — this codebase has been bitten repeatedly by two implementations of one rule (the reference-map numbering, the readiness/submit staleness test).
- **Protections are asked once, at the step that would violate them.** Lay-out is destructive to windows and must keep `confirm_replace` and the locked/approved refusals; fill-in touches no window and should not inherit lay-out's refusals.

### Rendered shots under a fill-in re-run — ruled 2026-08-21

**Rewrite, report, and auto-flag for re-render.** The plan updates, the take is untouched and still playable, and every rendered shot whose plan changed is marked `flagged` — so the existing flagged batch scope becomes the re-render list.

This extends the replace-citations ruling rather than departing from it: blocking a plan change does not protect a take, and the take's file, its job record and the takes strip are all unaffected. What is new is that the follow-up stops being something the Director has to remember. `flagged` and `scope: "flagged"` already exist and were built for exactly this shape.

Requirements that follow:
- Only shots whose plan **actually changed** are flagged. A fill-in that reproduces a shot's existing citations and intent must not flag it, or the flagged set becomes meaningless.
- Flagging is not a render. Nothing queues; the Director chooses when to spend the GPU.
- The report must distinguish **changed-and-rendered** (now flagged) from **changed-and-unrendered** (nothing to re-render) — they are different situations and the counts should not be pooled.
- An **approved** shot is not exempt. Approval is a judgement about a take, and the take survives; but it should be named separately in the report, because a Director re-rendering a flagged approved shot is displacing something they explicitly blessed.

### Narrative — ruled 2026-08-21

**A per-shot narrative role, no new entity.** One defaulted field on `Shot` carrying its place in the arc — establish / build / escalate / turn / release / resolve — assigned by the fill-in step and read by the expansion specialist and the DP pass.

Chosen over a Beat entity because sections are *musical* and beats would be *dramatic*, and inventing a second grouping to solve a music-video problem is the wrong order. A role gives a shot its through-line without a story schema, and it composes with what exists: the DP pass can escalate movement across a build, and expansion can write a resolve differently from a turn.

**Beats remain the right shape for short films** and belong to the story-layer work (Calliope Phase 5), not here. Deliberately not built now.

Requirements that follow:
- Nothing infers a role from prose. It is assigned by fill-in, visible in the inspector, and editable — the same stance as `singing` and the vocal type.
- A shot with **no** role is *unassigned*, not "establish". An unstated value must never be read as a stated one; this codebase refuses fabricated defaults.
- The role is fill-in output, so it is re-runnable and must not survive as a stale claim if fill-in runs again.

## Phase A as shipped — 2026-08-21

Built against `master` at 9c3db45. Backend only; no UI, no prompt wording changed, no model call split.

**The three steps** are module-level functions in `app.py` — `lay_out_shots` (async, owns the model call), `line_up_shots` (pure), `fill_in_shots` (pure) — with three routes over them (`POST /timeline/lay-out`, `/timeline/line-up`, `/timeline/fill-in`) and `POST /timeline/populate` as the chain that calls the same three functions. Module-level rather than closures precisely so a test can monkeypatch a step and assert the chain observed it; the chain holds no copy of any step.

**The intermediates, named:**

| Step | Intermediate | Fields |
|---|---|---|
| lay it out | `ShotLayout` | `project` (re-read, carries the assigned section layer), `duration`, `required`, `proposals` (`ShotProposal(start, duration, prompt, performance, assets)`), `windows`, `sections`, `sections_origin`, `message` |
| line it up | `ShotAlignment` | `layout`, `placements` (`ShotPlacement(index, start, duration, section, vocal_seconds, voiceless)`), `measured`, `moved` (always 0 in this phase) |
| fill it in | `list[Shot]` | prompt, citations, `singing`, `use_song_audio`, seed |

On the wire: `LayOutResponse` → `LineUpResponse` → `FillInResponse`, each step's report being the next step's `plan`, digest-checked and revision-checked.

**The model call lives in lay-out**, once. The combined ask is deliberately unsplit — that is a later phase and the 0-of-9 measurement is the evidence for it — so fill-in receives the content half as data on `ShotLayout.proposals`, which lay-out reads only the length of.

**One confirmation for the first pass.** `confirm_replace` on the chain is lay-out's consent: lay-out is the destructive step, line-up writes nothing, fill-in writes content into windows that consent created. Each route keeps its own report-then-confirm when called alone. The chain requires the consent up front rather than reporting first, because a chained populate has never had a report step and adding one would spend the 300 s model call twice.

**Protections placed as ruled.** `lay_out_protections` (no song / renders in flight / locked or approved shots) is one function shared by the `lay-out` route and the chain. **Fill-in inherits none of them**, guaranteed by a window fingerprint across the write, a per-row window match against the live timeline, and a check that the produced shots sit in the windows the step was handed.

**Line-up is a pass-through that attaches musical facts**, stated rather than dressed up: it moves nothing, `moved` reads 0, and `LineUpResponse` has no applied form. What it contributes is `shot_vocal_overlap` per window — the fact that downgrades `singing`, which populate used to compute inline — and the section each window falls in. Phase B is where it consumes the alignment for real.

**Byte identity, proved.** Digests over every field of every shot and section a populate writes (only the freshly-minted shot ids dropped) were measured on a `git stash`ed `master` tree before any code was written, and reproduce exactly after: `8b101523…cab0b8` (sections marked) and `085917f1…3c88c5` (unmarked), pinned in `tests/test_populate_steps.py`. A second run against a *copy* of the Director's live project agreed on both trees; the live manifest was read only and its SHA-256 is unchanged.

**No new persisted field**, so no write-path enumeration was needed and old manifests load untouched.

**Verification.** 22 new cases; suite 1558 passing; ruff and both `node --check` gates clean; a 34-mutant sweep in a `git worktree` with a failing sentinel killed 33, the survivor being a documented defence-in-depth re-check that the revision check above it makes unreachable.

## Phase B as shipped — 2026-08-21

Built on Phase A. Backend only; no UI, no prompt wording changed, no model call split, no citation chosen.

**The prediction held.** Line-up was the step with the most machinery built and the least wired in, and Phase B is almost entirely composition: `snap_cut_plan`, `vocal_gaps`, `align_lyric_blocks` and `lyric_line_tags` all existed; populate called none of them.

**One snapping implementation, two doors.** The decision was lifted out of `snap_cut_plan` whole into `timeline.snap_window_plan(windows, song, *, tolerance, minimum, maximum)`, which knows nothing about `Shot`. Windows arrive as `SnapWindow(id, start, duration, label, refusal)` — the protections already worded, from `window_move_refusal`, the one reader of them. `shot_snap_windows` is the one place a Shot becomes one. `snap_cut_plan` is now a caller; `app.line_up_shots` is the other. Proved rather than claimed: the two doors are compared cut for cut on the same input, and a single substitution of `_gap_snap_target` is observed by both.

**Line-up moves windows and hands each one its lines.** `ShotPlacement` gained `lines` (`timeline.LyricLineSpan(index, text, slots, start, end)` — the sheet's own line numbers) and `singers`, a derived property rather than a stored field. `ShotAlignment` gained `status`, `tolerance`, `moves`, `skips`, and `moved` now means something. **Nothing consumes the cast facts** — that is fill-in's, and Phase B produces them so pass 2 can be written against a real tagged song rather than a guess.

**Line times are composed, not re-derived.** `_align_blocks` places the `[Tag]` blocks with the repeat-defeating machinery that already exists; `align_lyric_lines` only splits one block's span among its own lines. A second answer to the refrain problem is how a line gets timed to the wrong verse.

**Confirmation, as ruled.** The chain asks once, at lay-out. Standalone line-up over an existing timeline is report-then-confirm in `snap-cuts`' shape, with the locked / approved / in-flight protections in their existing wordings. `snap_tolerance` reaches the chain; **0 is a genuine no-op**, and Phase A's two byte digests are still pinned through it as the control arm.

**Live, read-only, on the Director's project.** 6 of 29 cuts move on their real song (mean 0.229 s, max 0.430 s), 39 lyric lines time cleanly, contiguity is exact. **But their actual 34-shot timeline cannot be lined up at all**: 16 of its 33 seams are outside assembly's tolerance because overlapping shots are now a deliberate editing gesture, and the snapper models a cut as a boundary two shots *share*. `snap-cuts` has always refused such a plan; Phase B inherits the gap rather than creating it.

**Verification.** 22 new cases; suite 1580 passing; ruff and both `node --check` gates clean; a 42-mutant sweep in a `git worktree` with a failing sentinel per file killed all 42.

## Phase D as shipped — 2026-08-23

Built on Phase C's absence — this is lay-out only. Backend only; no UI, no prompt wording changed,
no model call split, no window count changed, no GPU spent.

**The complaint was within-section, and the measurement says so.** The live 30-shot plan runs mean
5.155 s, stdev 0.507, min 4.308, max 5.986 — but the Verse is **five identical 4.308 s windows**
and the Chorus **three identical 5.986 s**. The model varies between sections and never inside
one. The old hand plan's stdev 1.78 is not a target and could not be one: with this mean, the
largest stdev reachable with every window inside 4–6 s is **0.988** and inside 4–6.8 s is **1.37**.

**What drives variance: the word-onset rate** (`timeline.vocal_density`) — how many words start in
a window against the most any window of the same length holds anywhere in the song. Measured, not
declared, and it is this plan's "energy-biased per section" with the energy read off Whisper
rather than inferred from a section's name. Rejected, with reasons recorded on the constant:
**section identity** (a label is free text, and it cannot vary *within* a section, which is where
the complaint lives — making one section cut faster than another is a change to its window count,
i.e. to GPU spend); **the gap structure** (12 gaps against 29 cuts cannot decide 29 lengths, and a
layout placing cuts at gaps would be a second snapper); **a declared per-section intent** (a new
persisted field and a new thing to fill in before the button works — Phase C/E).

**The mechanism cannot break the tiling, by construction rather than by repair.**
`_varied_durations` moves each of a span's windows by `step × (its density − the span's mean)`.
The deviations sum to zero, so the span's total is the number it was — no water-fill afterwards,
no residual, no hole. The single `step` is capped by the tightest headroom any window in the span
has, so no window can leave the band; and it is also capped by the band's own width, so a section
whose density barely varies gets barely any variance rather than the same spread as one that
swings. It runs **per span**, so a section boundary is untouched and Phase B's protections and
Part 1's work stand.

**The parameter: `variance`, 0 to 1, default 1.0.** A *fraction of the room the band leaves*,
never seconds: at 1.0 the tightest window in each section sits exactly on a band end, so 1.0 is
all the room there is rather than merely a lot. 0 is the feature off and a genuine no-op; past 1.0
is refused at the route and in the tiler rather than clamped. It reaches `POST /timeline/lay-out`
and `POST /timeline/populate`, rides `LayOutResponse.variance` so the report can be accounted for
from itself, and is inside `plan_fingerprint` so a confirm cannot claim a number the Director did
not read.

**The default was 0.5 in the first draft and the measurement moved it.** Half was insurance
against a bad Whisper timing putting a window on a hard limit — but the guarantee here is
*structural*, not statistical: the reshaping cannot leave the band whatever the density says, so a
mis-timed line makes a differently shaped **legal** layout, and the worst case is a length the
Director drags. Meanwhile half of the achievable effect is 0.522 against a baseline of 0.507 — a
3% move, which is a rounding error with a parameter attached. The band is where the costs were
decided; a second conservatism on top of it is one nobody argued for.

**How much variance is reachable at all — the number that reframes this phase.** On the
Director's song (7 spans, 30 windows, mean 5.155 s), the largest standard deviation *any legal
tiling* can reach is **0.919** at a 6.0 s ceiling and **1.280** at 6.8. Populate lays 0.507 today;
this mechanism at 1.0 reaches **0.589**, i.e. 64% of what is available against today's 55%. **So
the band is not the binding constraint at 6.0** — but the statistic is badly chosen: split into
its parts, **0.472 of the 0.507 is between-section spread**, fixed by how many windows each
section gets and untouchable by any reshaping. The within-section spread — the thing the Director
described as *"five identical 4.308 s windows in a row"* — goes **0.185 → 0.352**, and the Verse
comes out 4.001/4.405/4.324/4.163/4.647 where it was 4.308 five times.

**One section of seven is genuinely band-bound, and that locates the ceiling argument exactly.**
The Chorus averages 5.955 s (4 windows over 23.82 s), 45 ms under a 6.0 s cap; the largest stdev
any legal tiling of *that section* can reach is **0.078** at 6.0 and **1.153** at 6.8.

**Measured on the Director's real song, read-only.** At the default: laid stdev 0.589, 0.567 after
line-up, min 4.001, max 5.999, **0 window warnings, 0 tiling refusals**, coverage 0 → 154.640
unchanged. Line-up's book shifts by one cut (13 no-gap refusals become 14, plus one more band
refusal) because the windows moved; nothing became illegal.

**`POPULATE_MAX_WINDOW_SECONDS` should be 6.8 — and it is worth more than the variance dial.**
**— RULED AND SHIPPED 2026-08-23, see below.** Per-frame sampling cost is flat to 6.79 s (175
frames, 0.977 s/frame — *cheaper* per frame than 6.08 s) and then climbs 48% at 7.50 s and 184% at
8.21 s, so the top of the flat region is free. It lifts the reachable ceiling 0.919 → 1.280 and the
delivered figure 0.567 → 0.680. The floor should *not* move: `over_render_frames` floors at 107
frames, so a window under ~3.271 s costs exactly what a 3.271 s one costs.

## The ceiling as ruled — 2026-08-23

`POPULATE_MAX_WINDOW_SECONDS = 6.8`. The 6.8 rows of the Phase D table are no longer hypothetical
and were re-measured read-only against `data/projects/project_59f14d19ff10` (manifest SHA-256
`93301f3e…dd30f0`, unchanged): **30 windows** (the count does not move on this song), min 4.001,
max 6.595, mean 5.155, stdev **0.692** laid and **0.680** after line-up, within-section **0.507**
against 0.353 at 6.0, between-section 0.472 unchanged, **0** window warnings, **0**
`assembly.tiling_refusals`, coverage 0 → 154.640 unchanged. **The Chorus** — the section the ruling
was made for, 4 windows over 23.82 s averaging 5.955 s and so 45 ms under the old cap — goes from
`5.874 / 5.974 / 5.974 / 5.999` (section stdev 0.055 against a reachable 0.078) to
`5.386 / 5.920 / 5.920 / 6.595` (0.495 against a reachable 1.153).

Thirteen tests moved with it and the arithmetic for each is in `docs/DEVELOPMENT-LOG.md`. Two
did not, and they are the finding:
**`test_the_default_variance_reshapes_a_word_timed_song_and_stays_legal` is left failing.** At 6.8
the populate fixture's proposals (cycling 4–8 s) let the water-fill saturate *both* band ends in
all seven spans, and `_varied_durations` freezes a span whenever one window is pinned at the end
its density wants to push it past — so the default variance is a **byte-for-byte no-op** there.
Three of seven spans were already frozen at 6.0; the wider band makes it universal. Everything else
that test asserts (contiguity, band, warnings, no straddle, coverage) still holds. The Director's
real song does not saturate, which is why it gains rather than loses. **Open ruling:** whether
`_varied_durations` should spend the room its unsaturated windows have instead of freezing the
span. That would move every Phase D digest, so it is a decision, not a fix.

### The ruling, taken — 2026-08-23, later the same day

**One saturated window must not veto its neighbours**, and the scalar step could not express
that. `_varied_durations` is now a **constrained redistribution**: the windows busier than their
span's mean *give* seconds, the quieter ones *take* them, one figure is given and taken so the
span's total is exactly preserved, and each side spreads its half over its own windows in
proportion to their distance from the mean — water-filled, so a window that meets its band end
freezes at it and the rest carry on (`timeline._spend_room`). The four invariants are structural:
the band is inviolable (a window only moves by the room it has), deviations sum to zero (both
sides are allocated the same figure and a mismatch refuses the reshape), direction follows density
(givers only shrink, takers only grow, whatever the clamping does), and it terminates (one pass
per window, no outer loop).

**Freezing survives as the last resort, not the first.** A span whose *every* giver is on the
floor has nothing to hand over, and since the total is preserved nobody can take either; the
mirror holds at the ceiling. That is arithmetic, not caution.

| | fixture spans frozen at 6.0 | at 6.8 |
|---|---|---|
| before | 3 of 7 | 6 of 7 |
| after | **0 of 7** | **2 of 7** (both one-sided saturation) |

**On the Director's real song it barely matters, and that is the honest claim.** Replayed
read-only: **0** of its 6 signal-carrying spans were frozen before, and 5 of those 6 were already
at the band-width cap rather than the tightest-window one. Laid stdev 0.692 → **0.697**, after
line-up 0.680 → **0.685**, within-section 0.507 → **0.513**, between-section 0.472 unchanged,
30 windows / 4.001–6.595 / 0 warnings / 0 refusals unchanged, and the **Chorus is byte-identical**
(`5.386 / 5.920 / 5.920 / 6.595` laid, `5.130 / 6.540 / 5.555 / 6.595` lined, section stdev 0.495).
Exactly one of seven sections — the Verse — reshapes, from `4.001 / 4.405 / 4.324 / 4.163 / 4.647`
to `4.001 / 4.440 / 4.330 / 4.001 / 4.769`. **The fix is for other material**, and the fixture is
that material.

Both Phase D digests moved a third time, with their previous values recorded beside them in
`tests/test_populate_steps.py` and the delta proved rather than asserted: coverage 0 → 154.640
unchanged, worst seam 1 ms against a 20.8 ms tolerance unchanged, band 4.000–6.800 unchanged,
0 window warnings and 0 tiling refusals unchanged, shot count 27 unchanged — the reshape moves
seconds between windows that already exist and cannot change how many there are.

**What Phase D does not claim.** Variance does not always widen a spread — the ordinary populate
fixture proposes 4–8 s windows and aligning those to the singing pulls some together (0.719 →
0.695); the parameter makes length follow the music, and a wider spread is what that produces when
the music has more to say than the model did. **And it does not claim the Director will call the
complaint closed.** Within sections the plan is visibly no longer uniform; as one number it moves
0.507 → 0.567. The two levers that would move it further are the **ceiling** (worth 0.113 on its
own) and the **per-section window counts**, and neither is a variance dial. If the Director wants
a plan that reads as varied at a glance, the count per section is the next thing to look at — and
it is a change to GPU spend, so it is a ruling rather than a default.

**Verification.** 36 new cases; suite 1757 passing; ruff and both `node --check` gates clean. The
neutral pin is a byte digest in four arms — sections marked or not, word times present or not —
and `variance=0` reproduces the pre-Phase-D values exactly; the word-timed arm is what makes that
a claim rather than an accident. A 45-mutant sweep in a `git worktree` killed all 45. The sweep's
first pass is recorded in the development log: both sentinels were additive edits that could not
die, and six survivors named real gaps — two provably redundant guards deleted rather than tested,
and four branches (the band margin's shrinking side, the empty-input guard, the "off means
nothing was measured" short-circuit, and the section-free tiling branch) given tests that reach
them.

## Still open

~~**Snapping requires a contiguous tiling; the Director's real timelines are no longer contiguous by design.**~~ **Settled 2026-08-21 — a transition is a seam, and it moves as a unit.** The open question was which point of an overlap should land in the voiceless gap. **The answer is the overlap's midpoint** (`timeline.SEAM_POINT`), not the later clip's start: under R-3 an overlap *is* a transition, at B's start B is at zero opacity so nothing is visible there, and the pair A-out/B-in describes one blend whose two edges are the ends of one object — the point that belongs to the blend rather than to one of its edges is its centre, which is also where every NLE centres a transition dropped on a cut. A hard cut is the zero-length case of the same arithmetic. Both edges travel by the same delta, so a transition is **never resized** by a snap; moving B's start alone would silently shorten a dissolve the Director authored.

Contiguity was restated to turn on the **sign** of the disagreement rather than its magnitude: past half a frame, a hole raises and an overlap snaps — `assembly.tiling_refusals`' own notion, bound to it by a test on the real geometry. On the Director's live plan, 15 of the 16 out-of-tolerance seams are overlaps and now snap; the sixteenth is a genuine 22 ms hole that assembly refuses too, and it is still refused, now by name. See the 2026-08-21 entry *"A transition is a seam too"* in `docs/DEVELOPMENT-LOG.md` for the measured numbers.

**The mid-word cuts are not a snapper defect — investigated and closed 2026-08-23.** On the live
30-shot plan, 13 cuts refuse with "no voiceless gap within 0.75 s". **0 of 13 have one.** The
suspicion was that the 0.75 s *display* merge and the 0.75 s snap tolerance sharing a number meant
the snapper was reading merged spans; it is not — `vocal_gaps` has read `Song.lyric_words` since
Phase B, and the word-level view finds 12 gaps where the merged view finds 5. The nearest gap to a
refused cut is **0.856 s** away and the next is 1.350 s; the rest are 1.9 s or further. Snapping
the same plan against the merged view moves the same 0 cuts. Separately, 21 of the 33 word-level
voiceless stretches are under `2 × SNAP_CLEARANCE_SECONDS` — inter-syllable pauses, correctly
refused, and only one refused cut is within tolerance of one. **The tolerance is the only lever on
those 13 cuts, and it is the Director's.** What this settles for the plan is that cut placement
cannot rescue a layout whose boundaries are in the wrong seconds; the layout is where the seconds
are decided. Recorded in full in the 2026-08-23 entry of `docs/DEVELOPMENT-LOG.md`.

Phase E's story layer (beats, characters as entities, scenes) remains deliberately unplanned until the app is aimed at narrative work.

## What this plan does not claim

Nothing here is measured against a live run of a split populate, because none exists. The evidence above is that the *current* single-call populate fails in ways whose causes belong to different steps — which argues for the split, and does not prove the split will produce better plans.
