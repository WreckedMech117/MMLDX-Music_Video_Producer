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

**Phase D — layout variance as a parameter.** Energy-biased window lengths per section, closing a standing complaint.

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

## Still open

~~**Snapping requires a contiguous tiling; the Director's real timelines are no longer contiguous by design.**~~ **Settled 2026-08-21 — a transition is a seam, and it moves as a unit.** The open question was which point of an overlap should land in the voiceless gap. **The answer is the overlap's midpoint** (`timeline.SEAM_POINT`), not the later clip's start: under R-3 an overlap *is* a transition, at B's start B is at zero opacity so nothing is visible there, and the pair A-out/B-in describes one blend whose two edges are the ends of one object — the point that belongs to the blend rather than to one of its edges is its centre, which is also where every NLE centres a transition dropped on a cut. A hard cut is the zero-length case of the same arithmetic. Both edges travel by the same delta, so a transition is **never resized** by a snap; moving B's start alone would silently shorten a dissolve the Director authored.

Contiguity was restated to turn on the **sign** of the disagreement rather than its magnitude: past half a frame, a hole raises and an overlap snaps — `assembly.tiling_refusals`' own notion, bound to it by a test on the real geometry. On the Director's live plan, 15 of the 16 out-of-tolerance seams are overlaps and now snap; the sixteenth is a genuine 22 ms hole that assembly refuses too, and it is still refused, now by name. See the 2026-08-21 entry *"A transition is a seam too"* in `docs/DEVELOPMENT-LOG.md` for the measured numbers.

Phase E's story layer (beats, characters as entities, scenes) remains deliberately unplanned until the app is aimed at narrative work.

## What this plan does not claim

Nothing here is measured against a live run of a split populate, because none exists. The evidence above is that the *current* single-call populate fails in ways whose causes belong to different steps — which argues for the split, and does not prove the split will produce better plans.
