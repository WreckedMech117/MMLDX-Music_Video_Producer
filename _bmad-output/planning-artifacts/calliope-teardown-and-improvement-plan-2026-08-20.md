# Calliope Teardown & Improvement Plan

**Date:** 2026-08-20
**Analyst:** Mary (Business Analyst)
**Subject:** [Calliope](https://github.com/benjiyaya/Calliope) by Benji ([Benji's AI Playground](https://www.youtube.com/@BenjisAIPlayground)), presented in ["MiniMax H3 and Krea 2 - I Created This AI App for Idea-To-Video"](https://www.youtube.com/watch?v=szJF38GzvWk)
**Inputs:** full-repo teardown of Calliope (agent report, file-path-cited), capability inventory of MusicVideoProducer (agent report), `docs/ROADMAP.md`, memory of measured H3/LM Studio findings.

---

## Final status (2026-08-20, end of session)

**Suite 545 → 1226 passing**, `ruff` clean, `node --check` clean, verified independently after every agent cleared. `.env` switched to `gemma-4-26b-a4b-it-heretic-ara-v2` (previous: `huihui-qwythos-9b-claude-mythos-5-1m-abliterated` — one line, one-line revert).

**The session's real finding was not on this plan.** `DirectorResult`'s strict `json_schema` never listed `shots` as `required`, because a Pydantic field with a default is never required — so LM Studio's constrained decoder was correct to omit it. Three live runs and a full day of theorising about prompt wording, count enforcement, two-stage splits and model overthinking were arguing with a constraint that did not exist. Fixing it took empty-`shots` from 2-of-3 to 0-of-3, and made the combined ask deliver both halves **15 of 15** where it had managed **0 of 17**.

An audit then found the same hole in four more places — `PlannedShot.performance`, `PlannedSection.prompt`, `stage_manager.assets`, and all five `vision_inspection` lists (that last a correctness bug: an omitted `risks` rendered as "Risks: None", an inspection that never looked shown as one that found nothing). The guard is a test that captures every schema **off the wire** and fails when a new defaulted field joins the optional set unexamined.

**Also shipped beyond the plan:** live render progress on asset cards and timeline clips (backend WebSocket → existing 2 s poll, progress never persisted); cut-snapping to phrase boundaries reading word-level Whisper timings; honest re-render surfaces; one reference-numbering rule; job supersession and startup healing; the narrow `PUT` gate; and a case-insensitivity fix to the H3 format checker (19 of 28 captured timecodes used the casing it could not see).

**Model choice, decided on evidence:** Gemma won populate (reasoning spread 2.0× vs 3.8×/5.3×; never mislabelled a section; 20/20 shots never straddled a boundary) and expansion (5/5 well-formed first attempt vs 3/5 and 1/5; zero fatal format problems; 1.3× reasoning spread). The earlier "Gemma unusable" verdict was withdrawn — a within-session control proved its 8-of-8 failure was the grammar, not the model.

**Everything remains offline- or LM-Studio-verified. No H3 render was submitted; nothing has been rendered against a snapped plan, an anchored asset, or the new checker.**

## Execution status (2026-08-20, same day)

Six stories landed. Suite went **545 → 1059 passing**, `ruff` clean, `node --check` clean on both modules, verified independently after all agents cleared. Every item is **offline-verified only** — no GPU time, no render, no ComfyUI submission, no live model call, no browser.

| Item | Status | Note |
|---|---|---|
| 1.1 JSON extraction ladder | **Shipped** | 4 rungs, 4 call sites (not 6 — grep corrected the estimate); `response_format` 400-retry ported unobserved on our provider |
| 1.2 Count-enforced populate | **Shipped** | Two-stage split built but **gated off** pending a live run; two fixtures changed because they encoded the under-delivery now refused |
| 1.3 Supersede + startup healing | **Shipped** | Reachable on one route only; ComfyUI jobs deliberately never healed at startup |
| 2.1 `consistency_prompt` | **Shipped** | One writer; generic PUT can neither blank nor invent |
| 2.2 Appearance anchors | **Shipped** | Wording is a first draft **no live model has seen** |
| 2.3 Reference-slot bounds | **Shipped** | Did **not** already exist — the roadmap line claiming it was a plan read as a status |
| 4.2 Export presets | **Shipped** | `draft` default is byte-identical to today; loudness target is a broadcast convention, not a music one |
| 3.1 H3 six-section A/B | **Open** | Needs a GPU session at production settings, Director present |
| 3.2 Deterministic expansion fallback | **Open** | Not started |
| 4.1 Video-only crossfade | **Open by decision** | Creative call on the vocal-transition lever — the Director's |
| 4.3 Dry-run placeholder takes | **Open** | Not started |
| 5.1 Story layer | **Open** | PRD conversation, not code |
| 5.2 Role-tagged Playground | **Open** | Not started |

**Findings surfaced by the work, none of them taken:**
1. `timeline.shot_expansion_input` numbers every citation into one `<Picture N>` series while the payload numbers per kind — latent until a shot cites a video. Own story; expansion inputs are pinned.
2. **Fourth appearance of the sibling-write-path pattern**: the generic `PUT` routes still write `Shot.status` from a body with no in-flight check. It is what makes supersession reachable at all.
3. Two frontend surfaces lie during a re-render: the Monitor plays the displaced take with no in-flight indication, and the takes strip labels it `Current`. Recommendation is to fix the surfaces and **leave `latest_output` alone** — blanking it puts an editorial decision inside a cleanup.
4. `app.js` Duplicate `structuredClone`s a shot and resets only `status`, so the copy inherits and displays the original's take as its own.
5. `loudnorm I=-16` is a broadcast/streaming target measured against a synthetic tone, never judged by ear on music.

## Governing thought

Calliope and MusicVideoProducer are convergent-evolution siblings — both local-first, FastAPI, ComfyUI-driven, LM-Studio-compatible, MiniMax-H3-targeting studios — but they optimized different halves of the problem. **We are 12–18 months ahead on music-video machinery** (timeline editing, lyric alignment, takes/approval, windowed song audio, assembly against a master track, VRAM coordination). **Calliope is ahead on three things worth taking**: local-LLM ergonomics (JSON robustness, count enforcement), prompt-level character consistency (appearance anchors + user-owned `consistency_prompt`), and workflow portability (role-tagged node discovery). Its story data model (beats → characters/locations → scenes with dialogue) is the proven shape of the narrative layer we'd need for story-based production — our stated next step.

## Who is Calliope

Local-first "story-to-video studio": idea → LLM story (beats, characters, locations) → deterministic reference-image generation → LLM script (scenes with action/dialog/duration) → per-scene ComfyUI video render → ffmpeg export with crossfades and loudness normalization. FastAPI + SvelteKit + SQLite + Electron portable shell. MIT, created 2026-07-27, 8 commits (squashed), 54 stars, single experienced author, real test suite (12 files). Honest scope limits: **no timeline editor, no music/audio generation, no lyrics/Whisper, no lip-sync, no takes/approval, no seed management** — scenes are a flat ordered list joined by fixed 0.5 s crossfades.

## Head-to-head

| Dimension | MusicVideoProducer | Calliope | Verdict |
|---|---|---|---|
| Planning unit | Timed window against a song | Scene in a story | Complementary — theirs is the narrative layer we lack |
| Character consistency | Krea multiview sheets, reference maps, continuity clauses; **not yet demonstrated** | Turnaround sheets + **prompt anchors** + `consistency_prompt` override | Take their prompt-level layer; it stacks with ours |
| H3 prompting | Prose recipe for song-audio (measured 0.90–0.95 envelope corr.); document format validated by `h3_prompt.py` | Six-section profile with `retention_analysis`, `<Subject N>` ↔ ref-slot binding, `non_diegetic_music: N/A` | A/B their sections on our shots — esp. retention & score-suppression |
| Local-LLM robustness | 3-retry expansion w/ corrective feedback; live LM Studio regressions logged | JSON ladder (fence-strip → balanced-brace `raw_decode` scan → retry w/ `json_object`), `response_format` off-by-default w/ retry-on-400, count math + HARD RULE + code verify + one guided lower-temp retry | Adopt their ladder + count pattern; direct fix for logged failures |
| Workflow coupling | Hand-built audited graphs, digest-pinned | Role-tagged node titles `(Input:prompt)`, alias normalization, smart-fill precedence chain | Keep our audited core; lift their contract for a Playground/import lane |
| Audio | Whisper alignment, vocal band, windowed song refs, audio restore, per-clip mix | H3's own synthesized audio + ffmpeg loudnorm only | We lead decisively |
| Editing | Full timeline, takes strip, trim nudge, Monitor, batch, assembly refusal reports | None (linear list + crossfade) | We lead decisively |
| Export | ffmpeg, cumulative 24 fps grid, master-song spine, ffprobe verify | ffmpeg xfade chain, `anullsrc` silence donors, `yuv420p` pinned after last xfade, `loudnorm I=-16:TP=-1.5:LRA=11`, `-progress pipe:1` | Take their loudnorm/progress/crossfade details |
| Ops hygiene | Orphan-heal at assemble, 3-tick settle, VRAM eject | Supersede-pending-on-regenerate, stale-`running` reset at startup, labeled dry-run placeholders, path rebasing | Take dry-run + supersede semantics |

## The plan

Ordered by leverage ÷ cost, phased so cheap certainties land before GPU experiments, and experiments before strategy.

### Phase 1 — Local-LLM ergonomics (days, no GPU)

**1.1 JSON robustness ladder in `director.py`.** Port Calliope's `extract_json` shape: raw parse → fenced-block strip → `json.JSONDecoder.raw_decode` scan for the first balanced object *ignoring leading/trailing chatter* → one retry requesting `{"type": "json_object"}`. Also their transport policy: `response_format` off by default, drop-and-retry on 400.
*Why this first:* it directly absorbs the 2026-08-19 LM Studio regression class (model reasons, then answers — the answer sits after chatter our parser may not survive) and their v1.1.1 fix commit shows the author hit the same wall. Testable offline against recorded degraded outputs.

**1.2 Count enforcement for populate.** Calliope computes required counts deterministically (~7 s/scene), states them as a numbered HARD CONSTRAINTS block plus a closing "FINAL CHECK before responding: scenes.length == N", verifies in code, and retries **once** at lower temperature with the failure named ("PREVIOUS ATTEMPT FAILED: it only had N"). Apply to `timeline/populate` and the planned two-stage populate (sections-first, then shots-within-sections — roadmap item, measured failure on run 2). The named-failure retry also generalizes our expansion retry loop.

**1.3 Supersede semantics + startup healing.** On render-again/re-populate, mark leftover pending jobs superseded and null the stale pointer so the UI shows "generating", not an old take; reset stale `running` jobs at app startup (we currently heal only at assemble).

### Phase 2 — Character consistency, prompt layer (days, minimal GPU)

**2.1 `consistency_prompt` on Asset.** One user-editable field on character/setting/prop assets that **wins over any generated description everywhere it is consumed** — expansion, fill, reference-map lines. Calliope's single most user-empowering lever; costs one model field and a few injection points.

**2.2 Appearance anchors in expansion.** Rule for the H3 expansion and fill personas: at each character's first mention per shot, inject a stored 3–8-word visual anchor ("MIA, a teenage girl with a chestnut ponytail and yellow rain jacket,"), sourced from `consistency_prompt`; "never invent new appearance details." Stacks under our image references; directly serves Epic 3's undemonstrated goal (recognizably the same person across a finished video). Checkable pre-render, like the rest of `h3_prompt.py`.

**2.3 Subject-roster cap.** Calliope caps prompt subjects at the wired ref-slot count so a prompt can never cite an unattached picture. Verify our `h3_prompt` undefined-reference check enforces the same invariant in both directions (prompt→refs and refs→prompt); close any gap.

### Phase 3 — H3 format experiments (GPU A/Bs, production settings only)

**3.1 A/B the six-section profile.** Calliope's rewrite emits `subject_definitions` / `summary` / `retention_analysis` ("<Subject N>… fully_preserved — …") / `detailed_description` / `overall_soundscape` / `non_diegetic_music`. Two hypotheses to test separately:
- **Non-singing b-roll shots:** does `retention_analysis` measurably improve identity retention vs our current expansion? (Candidate fix for the sheet-layout-bleeding-into-composition artifact.)
- **Song-audio shots:** does an explicit `non_diegetic_music: N/A` section suppress H3's self-synthesized score better than, or compatibly with, our prose recipe? Our measurements (prose 0.90–0.95 vs document ≤0.43) say the H3 document format *as we built it* loses — but Calliope's variant differs in exactly the section aimed at the score problem, so the comparison is not settled by prior data.
*Guardrail from our own roadmap:* every prior cheap-settings verdict was voided; run A/Bs at production resolution/steps, on a windowed lyric-bearing span, one variable changed.

**3.2 Deterministic fallback for expansions.** Calliope never blocks generation on the LLM: if the rewrite fails, a pure-template prompt assembles from stored fields. We already do this for song-audio (`song_audio_prose`); extend the principle to non-singing expansion failures (template from prompt + section prompt + anchors) instead of surfacing a refusal.

### Phase 4 — Assembly & delivery upgrades (days)

**4.1 Optional video-only crossfade at cuts.** Their xfade chain (offset = `sum(durations[:k]) − k·XFADE_SEC`, `format=yuv420p` pinned *after* the last xfade — "players choke" otherwise) — applied to **video only**, master song untouched — is a cheap first lever for the open "vocal transition points" roadmap item: a 0.25–0.5 s dissolve masks uncoordinated mouth positions at cuts. Per-cut opt-in, defaulting off, so hard cuts stay the norm.

**4.2 Export presets + loudnorm.** Draft/master presets (open roadmap item): master pass adds `loudnorm=I=-16:TP=-1.5:LRA=11` (or music-mastering values the Director prefers), libx264 CRF + faststart; draft stays fast. Assembly progress from `-progress pipe:1` with concurrent stderr drain (our `post` jobs currently report nothing until done).

**4.3 Dry-run placeholder takes.** A labeled-placeholder mode (per-shot labeled MP4 stand-ins, no GPU) so full-timeline flows — populate → readiness → batch → takes → assembly — can be exercised end-to-end in browser QA without ComfyUI. Calliope ships this as a first-class mode; our scripted-double harness covers routes, not the full visual pipeline.

### Phase 5 — Strategic: the story layer & workflow portability (weeks; PRD-worthy)

**5.1 Narrative entities.** Calliope's proven schema — beats (ordered, titled), characters (name, appearance, personality, sheet, `consistency_prompt`), locations, scenes (heading, action-as-prompt, dialog with speaker cues, duration, character_ids, location_id) — is the minimal viable story layer. For us: a *beats lane* over the treatment that maps beats → shot ranges would structure music-video storytelling today, and is the load-bearing schema for full story-based production tomorrow. Dialogue is nearer than it looks: H3 `<d>[English]…</d>` tags are already validated by `h3_prompt.py` and merely forbidden on song-audio shots — a non-song story shot could speak now, no TTS required.
**Recommendation:** run this through `bmad-prd` as its own initiative rather than bolting entities on ad hoc.

**5.2 Role-tagged workflow import + Playground.** `(Input:role)` node-title tags, alias normalization, smart-fill precedence ("anything you type wins", blanks stripped so an empty field can't wipe context) — ~250 liftable lines. **Do not touch the audited, digest-pinned core paths.** Land it as a separate *Playground lane*: import any community workflow, analyze/preview roles, run free-form. This is how new models (the next H3, the next LTX) get tried without an adapter-audit cycle, and how users extend the app without us.

### Explicitly not recommended

- **Replacing our hand-built audited graphs with role-tag patching.** Byte-identical digest pinning is our evidence discipline; role-tags are for the exploratory lane only.
- **Adopting their crossfaded-audio export.** Our master-song spine with frame-exact trims is strictly stronger for music video.
- **SQLite migration.** Their WAL/deadlock care is solving a problem our atomic-JSON store doesn't have.
- **Electron packaging.** Distribution isn't the current bottleneck; revisit at productization.

## Suggested sequencing

Phases 1–2 are one short sprint of offline-testable work and should precede everything (they also de-risk Phase 3's A/Bs by making the LLM plumbing sturdier). Phase 3 wants dedicated GPU sessions with the production-settings guardrail. Phase 4 items are independent and can interleave. Phase 5 starts with a PRD conversation, not code.

## Sources

- [Calliope repository](https://github.com/benjiyaya/Calliope) — all file-path claims from the teardown agent's direct reads
- [benjiyaya on GitHub](https://github.com/benjiyaya)
- [Video: "MiniMax H3 and Krea 2 - I Created This AI App for Idea-To-Video"](https://www.youtube.com/watch?v=szJF38GzvWk), [Benji's AI Playground](https://www.youtube.com/@BenjisAIPlayground)
- Internal: `docs/ROADMAP.md`, `docs/DATA-MODEL.md`, capability-inventory agent report (2026-08-20)
