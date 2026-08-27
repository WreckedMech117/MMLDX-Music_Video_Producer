# Build Handoff — start here

**Written 2026-08-23.** Point a fresh development session at this file. It covers what is planned, what is built, and — the part that matters most — **the things a fresh session will otherwise get wrong**, because they were established by measurement rather than by reasoning and cannot be re-derived by thinking harder.

**Read `AGENTS.md` first for operations** — policy, verify commands, conventions, ComfyUI rules. This document does not repeat it. What follows is the planning state and its caveats.

---

## 1. Where things stand

Three features, one story-numbering space, three epic files that cross-link each other.

| Feature | Epics | Stories | State |
|---|---|---|---|
| Base product | 1–7 | 23 | Largely built; see `docs/ROADMAP.md` for verified-vs-unverified |
| **Shot Effects and Transitions** | 8–11 | 19 | **Epics 8 and 9 shipped. Epics 10 and 11 planned, not built** |
| **Treatment Planning** | 12–17 | 17 | **Fully planned, nothing built** |

*Corrected 2026-08-27.* The effects row read ~~**Fully planned, nothing built**~~ and the story counts read ~~24 / 18~~ for the first two features. The state claim was true on 2026-08-23 and is two epics stale; the treatment count of 17 was right and is untouched. `sprint-status.yaml` carries **`epic-8: done`** (8.1–8.3) and **`epic-9: done`** (9.1–9.7), across roughly fifteen commits ending at `0b0bb96`; `audio.py` and `effects.py` both ship, with the envelope sidecar, beat markers and snapping, the Effects tab, the preview cache and an effects-aware export behind them. Counted today against `epics.md`, `epics-effects.md` and `epics-treatment.md`: **23 / 19 / 17**. The effects figure moved because Epic 9 gained Story 9.7 during the build, and the base figure was never 24. This is the row `AGENTS.md` sends every new session to read first, and it was two epics stale — the same failure Epic 9's retrospective found in the tracker.

Sprint tracking: `_bmad-output/implementation-artifacts/sprint-status.yaml` — 17 epics, 59 stories. **It is gitignored** (`.gitignore` excepts only `deferred-work.md` under `implementation-artifacts/`), so it is local-only by design. Regenerate it with `bmad-sprint-planning` after any epic change.

### Artifact map

| Feature | Artifacts |
|---|---|
| Effects | `effects-and-transitions-research-2026-08-21.md` · `effects-director-rulings-2026-08-21.md` (R-1…R-7) · **`effects-director-rulings-2026-08-24.md` (R-8…R-28)** · `prds/prd-MusicVideoProducer-effects-2026-08-21/` · `ux-designs/ux-effects-2026-08-21/` · `architecture/architecture-MusicVideoProducer-effects-2026-08-21/` (AD-16…AD-31 + `BUILD-ORDER.md`) · `epics-effects.md` |
| Treatment | `treatment-planning-findings-and-rulings-2026-08-22.md` (F-1…F-6, R-1…R-17) · `prds/prd-MusicVideoProducer-treatment-2026-08-22/` · `ux-designs/ux-treatment-2026-08-22/` · `architecture/architecture-MusicVideoProducer-treatment-2026-08-22/` (AD-32…AD-47 + `BUILD-ORDER.md`) · `epics-treatment.md` |

All paths relative to `_bmad-output/planning-artifacts/` — *corrected 2026-08-27, this said ~~`_bmad-output/`~~ and there is no `_bmad-output/prds/`, `ux-designs/` or `architecture/`; every artifact above is one level further down.* Requirement prefixes never collide: base `FR-`, effects `FX-`, treatment `TP-`. Architecture decisions are one sequence, `AD-1`…`AD-47`.

---

## 2. Where to start

**Effects Epic 10 — Reactive Binding, which is `BUILD-ORDER.md`'s Slice E.** Slices A (song analysis), B (the chain builder), C (the Effects tab) and D (preview) are all built, which is precisely the foundation E was sequenced onto: an envelope to read, parameter rows to bind, and a preview to judge a binding against. **Read Slice E's risk paragraph before you start** — it was rewritten on 2026-08-27 because the original named the wrong danger.

*Corrected 2026-08-27.* This said ~~**Effects Story 8.1 — Analyze the Song into an Envelope.** No dependencies, no blockers, and it ships beat markers and beat-snapping on its own even if nothing else in that feature ever lands.~~ Story 8.1 shipped on 2026-08-24 and the whole of Epics 8 and 9 with it. The ordering *was* the Director's decision and it still has the consequence §5 records, which is why that section is amended rather than deleted.

**Build order is `BUILD-ORDER.md`'s slices, not epic order.** Both features have one. Effects Epic 9 deliberately consolidates three slices; Treatment Epic 14 consolidates three more. Reading the epics alone will give you the wrong sequence.

**Independent quick wins**, each justified on its own and blocking nothing:

- Treatment **Story 12.1/12.2** — Brief lock, recovery slot, restore, contract line. Closes FR-16's unstated exception.
- Treatment **Epic 17** — the Song Planner. Fully independent, sits at the true start of the workflow.
- Treatment **Story 16.1** — plain proceed-to-next-step buttons, without their offers.
- ~~Effects **Epic 8** — song analysis, beat markers, beat-snapping.~~ **Shipped 2026-08-24.**

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

`replace_project` — in **`routes/project.py`** since the split, not in `app.py` — carries comments counting the **fourteenth** finding of the same hole. The established remedy is an `_adopt_*` helper that takes the field off the **stored** project before the body is trusted — `_adopt_song_recovery_slots`, `_adopt_song_vocal_type`, `_adopt_song_analysis`, `_adopt_expansion_maps`, `_adopt_shot_effects`, `_adopt_job_measurements`.

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

Start on **effects Epic 10 — Slice E of the effects `BUILD-ORDER.md`** *(corrected 2026-08-27; this said ~~Start on **effects Story 8.1**~~, which shipped on 2026-08-24 along with the rest of Epics 8 and 9)*. Put a new route in `src/music_video_producer/routes/`, not in `app.py`. Before writing any LLM-facing schema, read §3's model envelope and use `_promoted()`. Before adding any field to `Project` or `Shot`, write its `_adopt_*` guard and its test in the same commit — that hole has now been found fourteen times in one route. Before planning around any constraint written more than a few days ago, re-run it.
