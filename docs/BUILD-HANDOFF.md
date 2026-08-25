# Build Handoff — start here

**Written 2026-08-23.** Point a fresh development session at this file. It covers what is planned, what is built, and — the part that matters most — **the things a fresh session will otherwise get wrong**, because they were established by measurement rather than by reasoning and cannot be re-derived by thinking harder.

**Read `AGENTS.md` first for operations** — policy, verify commands, conventions, ComfyUI rules. This document does not repeat it. What follows is the planning state and its caveats.

---

## 1. Where things stand

Three features, one story-numbering space, three epic files that cross-link each other.

| Feature | Epics | Stories | State |
|---|---|---|---|
| Base product | 1–7 | 24 | Largely built; see `docs/ROADMAP.md` for verified-vs-unverified |
| **Shot Effects and Transitions** | 8–11 | 18 | **Fully planned, nothing built** |
| **Treatment Planning** | 12–17 | 17 | **Fully planned, nothing built** |

Sprint tracking: `_bmad-output/implementation-artifacts/sprint-status.yaml` — 17 epics, 58 stories. **It is gitignored** (`.gitignore` excepts only `deferred-work.md` under `implementation-artifacts/`), so it is local-only by design. Regenerate it with `bmad-sprint-planning` after any epic change.

### Artifact map

| Feature | Artifacts |
|---|---|
| Effects | `planning-artifacts/effects-and-transitions-research-2026-08-21.md` · `effects-director-rulings-2026-08-21.md` (R-1…R-7) · `prds/prd-MusicVideoProducer-effects-2026-08-21/` · `ux-designs/ux-effects-2026-08-21/` · `architecture/architecture-MusicVideoProducer-effects-2026-08-21/` (AD-16…AD-31 + `BUILD-ORDER.md`) · `epics-effects.md` |
| Treatment | `planning-artifacts/treatment-planning-findings-and-rulings-2026-08-22.md` (F-1…F-6, R-1…R-17) · `prds/prd-MusicVideoProducer-treatment-2026-08-22/` · `ux-designs/ux-treatment-2026-08-22/` · `architecture/architecture-MusicVideoProducer-treatment-2026-08-22/` (AD-32…AD-47 + `BUILD-ORDER.md`) · `epics-treatment.md` |

All paths relative to `_bmad-output/`. Requirement prefixes never collide: base `FR-`, effects `FX-`, treatment `TP-`. Architecture decisions are one sequence, `AD-1`…`AD-47`.

---

## 2. Where to start

**Effects Story 8.1 — Analyze the Song into an Envelope.** No dependencies, no blockers, and it ships beat markers and beat-snapping on its own even if nothing else in that feature ever lands. This ordering is the Director's decision, and it has a consequence: see §5.

**Build order is `BUILD-ORDER.md`'s slices, not epic order.** Both features have one. Effects Epic 9 deliberately consolidates three slices; Treatment Epic 14 consolidates three more. Reading the epics alone will give you the wrong sequence.

**Independent quick wins**, each justified on its own and blocking nothing:

- Treatment **Story 12.1/12.2** — Brief lock, recovery slot, restore, contract line. Closes FR-16's unstated exception.
- Treatment **Epic 17** — the Song Planner. Fully independent, sits at the true start of the workflow.
- Treatment **Story 16.1** — plain proceed-to-next-step buttons, without their offers.
- Effects **Epic 8** — song analysis, beat markers, beat-snapping.

**Merge alone:** effects **Epic 11** (transitions). It is the only work touching `assembly_plan` and the cumulative frame grid, and it needs its own verification pass against `FX-NFR-1` before it merges.

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
- **`sendcmd=f=` breaks on an absolute Windows path.** The drive-letter colon parses as a filter option separator and fails with `No option name near 'frame'` — naming a filter that is not the problem. Run ffmpeg with cwd set to the script's directory and pass a bare relative filename.
- **Listing the looks folder reads every `.cube` to its end, and that is not free.** A truncated LUT carries its `LUT_3D_SIZE` header on line 1, so no header or size heuristic can tell a half-copied 33-cube from a whole one — only counting the data lines against `N³` can, and that means reading the file. Measured 2026-08-25 on the 48-file, 44.2 MB pack: **221 ms cold, 23 ms warm** (a header-only sniff was ~1 ms). The cost buys a refusal by name instead of `Error initializing filters` at export. **Do not call `discover_luts` per request** — read it once and hold it, or the folder is re-read on every keystroke that opens a picker.

### Manifest sizing

`project.json` is **110–190 KB** today and rides a **2-second poll**. A 3-minute Song Envelope at 30 Hz with 8 bands is **~750 KB** — four to seven times the whole manifest. It is a sidecar file (AD-20), never a manifest field. Apply the same test to anything else large.

---

## 4. Caveats — process

### This repo moves fast enough to invalidate its own artifacts

**Three premises in the Treatment planning pass turned out to be wrong**, each caught only by reading current code:

1. The Brief was described as needing promotion into the pipeline. It was **already in it** — `timeline.py` puts `creative_brief` in the project dump at three call sites.
2. The multiview gate was described as refusing non-character assets. It had **already been widened** to `character`/`prop`/`setting`.
3. Structure analysis was described as Timeline-only. **`Analyze structure` is already on the Song page**, beside `Build treatment →`, and `align-lyrics` already proposes section boxes from timed `[Tag]` blocks.

An eight-day-old constraint was stale in all three cases. **Re-run any constraint before planning around it.** The `Deferred` sections of both architecture spines name several that still need re-verifying — in particular whether the Krea reference sheet still runs one of three sampling stages.

### Other agents work in this repo concurrently

Blanket `git add` has swept in-flight planning work into unrelated commits — the effects PRD, architecture spine and UX spines were all committed by `9c3db45` *"Vocal type, per-line singer marks, and character slots"*. **Stage explicit paths, not `-A`.** Check `git status` for someone else's modified files before committing.

### The generic project PUT is this codebase's recurring guard hole

`replace_project` in `app.py` carries a comment counting the **sixth** finding of the same hole. The established remedy is an `_adopt_*` helper that takes the field off the **stored** project before the body is trusted — `_adopt_song_recovery_slots`, `_adopt_song_vocal_type`, `_adopt_expansion_maps`.

**Every new manifest field in either feature must be adopted this way**, and the adopt test belongs in the same commit as the field, not after. AD-16, AD-36 and AD-41 all say so. This is the pattern most likely to be skipped and most expensive to skip.

### Test and tooling traps

- Nested `uv run` deadlocks on the environment lock. Call `.venv` python directly inside harnesses.
- Mutation testing under concurrency: mutate in a worktree, and set `PYTHONPATH` or the editable `.pth` imports the live checkout and everything "survives".
- Restore mutated files with `write_bytes`, not `write_text` (CRLF), and purge `__pycache__` before same-length mutations.
- Parallel agents sharing a scratchpad have clobbered each other's harnesses; a poisoned baseline validates itself.

---

## 5. The cross-feature obligation

**Effects Story 8.1 ships first**, so **Treatment Planning owns the shared analysis trigger** (R-17).

Two different analyses want the same song and the same moment — the one point where the Director is plainly willing to wait:

- Effects `FX-1` — RMS, onsets, beats, BPM, per-band envelopes → a sidecar.
- Treatment `TP-18` — Whisper transcription, `[Tag]` block alignment, proposed section boxes.

Treatment Story 16.2 must present **one moment and one indicator** covering both, **without merging the computations** — they stay separate functions in separate modules (AD-40). Each half is **skipped when already current** (AD-47), and since `FX-1` analyses automatically on song import, the common case is that one half is already done and the job finishes fast. **That fast finish must read as "already done", not as a failure to run.**

Nothing in the effects work changes because of this. Story 16.2 cannot start before 8.1 ships.

---

## 6. Owed measurements and open items

None blocks starting. All are recorded in the spines' `Deferred` sections.

**Blocking a specific slice:**

- **LUT source and licence** — blocks the Grade family shipping (effects Story 9.1/9.3). `effects.py`'s structure does not depend on it; build against a placeholder set.

**Owed measurements:**

- **Suggest Video's timeout value** — set from live runs; raising the director timeout is authorised (R-11).
- **Full-resolution export cost** of a reactive binding and of transition segments. `CM-E1` makes an export regression a defect, not a cost.
- **NVENC at export length** — unmeasured; the preview result does not transfer.
- **`populate` after Treatment ships.** Richer Briefs make richer Treatments, and a Treatment is populate's input — **planning succeeding makes populate's job bigger.** Note that `91c7120` recorded populate's first live run and fixed four defects, so some of this may already be answered.
- **Krea reference sheet sampling stages** — the "one of three" finding is stale; `res_multistep` now appears in the builders. Re-measure before treating reference fidelity as a lever.

**Open questions with owners:**

- Effects: attribution-free — 3 open, 1 UX (transition catalogue size), 2 architecture.
- Treatment: 3 open — 1 UX (planning-turn indicator), 2 architecture (undo depth, bounded thread per turn).

---

## 7. Standing design laws

Violating one of these is a defect even when the code works.

1. **The frame grid is inviolable.** The assembled video matches the song within one frame, for every combination of effects and transitions. Effects `FX-NFR-1`.
2. **One engine describes an effect.** Preview is the export's own filter chain at smaller dimensions — never an approximation. Effects `FX-NFR-3`.
3. **Nothing renders without confirmation.** Every GPU spend passes an explicit confirmation naming what will run.
4. **Never silently destroy a creative document.** FR-16, and after Treatment Story 12.1 it holds for all three documents with no exception.
5. **Derived beats stored.** Media presence, envelope validity, preview staleness, proposal staleness, effects presence — all computed at read time, never a stored flag that can outlive its condition. AD-11 and everything that cites it.
6. **Consent is explicit on the wire, never ambient.** Planning Mode's session consent is a *client* affordance; every request still carries consent. AD-35.
7. **The palette is closed at six accents.** `--acid` complete/action · `--amber` running/caution · `--red` error · `--cyan` approved · `--blue` transitions and reactive bindings · `--dim`/`--muted` inert. A seventh needs the argument made from scratch.
8. **No progress percentage for work whose progress cannot be measured.** Renders and language-model passes both. Elapsed time only.
9. **Local-first.** No cloud model, no account, no telemetry — including for planning, however much the local model's limits show.
10. **Generated render inputs are pure and compared as text.** Filter chains, concat lists, `sendcmd` scripts — a pure function of the manifest, asserted by string comparison, exactly as ffmpeg argv already is.

---

## 8. If you only read one thing

Start on **effects Story 8.1**. Before writing any LLM-facing schema, read §3's model envelope and use `_promoted()`. Before adding any field to `Project` or `Shot`, write its `_adopt_*` guard and its test in the same commit. Before planning around any constraint written more than a few days ago, re-run it.
