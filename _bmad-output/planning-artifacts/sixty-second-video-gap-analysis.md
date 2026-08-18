# The Sixty-Second Video: Pipeline Walk and Gap Analysis

**Status:** analysis, written 2026-08-18. No code, tests, or docs were changed to produce it.
**Organizing use case, the Director's words verbatim:**

> "ill eventually want you to work with the system to have it go through and set up a 60 second music video all the way up to being ready to run the video generation chain. Its own generated track, characters, scene, b-roll, and shots linked together appropriately with the music. It should utilize the lypsinc when the shot calls for it and have good video composition and assembly for a music video."

Everything below is graded against that sentence. Section 2 walks what exists; section 3 is the adapter truth table (the direct answer to *"allot of our 'Generation Mode' settings are 'no adapter yet' marked and i cant tell when the Minimax music video workflows are employed"*); section 4 names every gap; section 5 sequences them. A concurrent analysis covers layout/UI; where a gap is a layout problem this document names it and moves on.

---

## 1. The short answer

**The generation side of the milestone is nearly whole; the finishing side is nearly absent.** From an empty project, the shipped routes can today produce a 60 s generated track, Flux characters/settings/props, promoted reference sheets (characters *and* objects), a Director-planned 15-shot timeline, two-pass prompts in H3's documented format, and per-shot lip-synced renders windowed to the right seconds of the song. What cannot happen today: approving a take (FR-21 — no route writes `approved_output`), assembling approved takes into one video (FR-22 — no assembly code exists anywhere in `src/`), arming a plan in one act, or rendering any of the four keyframe/extend modes. The two biggest unlocks are **take approval + assembly** (they *are* the milestone's last mile) and **mode-aware readiness + a real batch path** (they make firing 15 shots one uneventful act instead of 15 hand-clicked ones).

---

## 2. The pipeline as it exists today, empty project → rendered clips

Every step below is a shipped route in `src/music_video_producer/app.py` unless marked otherwise. File references: `models.py` (data model), `workflows.py` (ComfyUI adapters), `batch.py` (readiness), `timeline.py` (window math and LLM payloads), `director.py` (LLM client), `h3_prompt.py`/`h3_expansion_prompt.py` (pass-two format + checker), `web/assets/app.js`/`api.js` (frontend).

### 2.1 The track

- **Import**: `POST /api/projects/{id}/songs/upload` with optional `lyrics`/`caption` form fields; `PUT .../song/context` corrects them later. ffprobe fallback for duration; the duration is the timing spine (`docs/DATA-MODEL.md`).
- **Generated, the milestone's own path**: `POST .../generate/songplanner` → `build_songplanner_invented_payload` / `build_songplanner_known_lyrics_payload` (`workflows.py`), both live-verified 2026-08-17. Duration 30–300 s — **a 60 s track is squarely inside the range** (the 30 s schema floor on `M3SongPlanner.duration_seconds` is the only reason a shorter one is impossible). The **duration-headroom field** shipped: `SongPlannerRequest.duration_headroom` (default 1.5, range 1.0–12.0) multiplies `MiniMaxMusic3TextEncode.max_duration` above the planner's target so lyrics that run long keep their ending; the Song form sends it explicitly and refuses a duration×headroom product over the encoder's 360 s ceiling locally (`docs/WORKFLOW-MAP.md`, "Duration headroom"). The 1.5-vs-1.0 question is still unsettled by ear — both live runs sat at the 30 s floor where it cannot show.
- Song replacement/removal is gated on all five Song-changing routes (`_require_song_replacement_confirmation`); the known residual hole is the generic full-project `PUT` wiping *Shots* while carrying an unchanged Song (`docs/ROADMAP.md`).

### 2.2 Assets

- **Flux**: `POST .../generate/flux` → `build_flux_payload`, `kind` ∈ character/setting/prop/style/…. Live. Note it is **library-scoped, not shot-scoped** — see gap G11.
- **Krea multiview promotion**: `POST .../assets/{asset_id}/multiview` → `build_multiview_payload`. `MULTIVIEW_SUBJECTS = {"character": "character", "prop": "object", "setting": "object"}` (`app.py` ~line 141) — **objects are promotable**, live-verified with a cargo ship 2026-08-18; the child inherits the source's kind. Caveat that bites automation: the child Asset is created with `path=""` and only job refresh (`read_job`) populates it — *a promotion is finished when the job has been refreshed, not when it completes* (`docs/WORKFLOW-MAP.md`). Known quality lever left on the table: the adapter runs only the first of the creator's three sampling stages (layout pass without refine/finish) — recorded in `shot-modes-and-pre-generation-planning.md`, with the note that `Music-Video.md` is wrong about that graph in five ways and the two extra passes were deliberately reverted.
- **Vision inspection** (`.../assets/{id}/analyze`, `.../shots/{id}/analyze-latest`): advisory continuity records; never touch approval.

### 2.3 Planning: Director chat → treatment/style bible → shots

- `POST .../director/chat` (`DirectorRequest`): one JSON contract (`docs/LLM-DIRECTOR.md`) returning message, treatment, style bible, and timed shot suggestions. Writes are opt-in per turn (`apply_shots`, `apply_documents`), documents have single-slot recovery + locks (FR-16), degraded output is refused (`document_rejection`) and carried as excluded `notices`. **This is where the 15 shots come from**: an applied shot plan creates timed `Shot` windows with short intents.
- **Pass one — `POST .../director/expand`**: one whole-plan call, one intent per unlocked Shot, merged by Shot id, never queued, never retimed. Input is the trimmed `timeline.expansion_input` — carries song `lyrics`/`caption` when present, `song_fraction` per shot, and **no** `song_section` (no analyser exists; the key is omitted).
- **ProducerBot — `POST .../assistant/fill`**: the Director's model given `fill_shots` (mode/prompt/citations/singing, schema generated from `models.py` enums) and `expand_prompts` as tools, scoped to a required `shot_ids` selection. Nothing infers `singing`. All refusals shared with expansion. Persona written, **not yet iterated against real output** (`deferred-work.md`).
- **Pass two — H3 expansion**: `POST .../shots/{id}/expand-prompt` (per shot), `POST .../shots/expand-prompts` (whole-plan sweep, N sequential calls, single terminal save), and ProducerBot's tool. Writes `Shot.h3_prompt` — the MiniMax three-field format (`h3_expansion_prompt.py` carries the guide-derived rules incl. `SINGING_RULES`; `h3_prompt.check` refuses malformed output before any write). `h3_prompt` is withheld from the Director's chat context. Known live behaviour: the 4000-token budget exhausts on reasoning ~1 call in 6 on this machine's model — a `failed` line in the sweep, re-runnable.

### 2.4 Arming and readiness

- `POST .../shots/{id}/mark-ready` / `mark-draft`: one shot at a time, never automatic, prompt-gated via `batch.prompt_rejection`. **Marking ready takes the shot out of every automated writer's reach** (expansion, ProducerBot) — so the correct order is: fill → expand → mark ready.
- `batch.readiness_report(project)` (`GET .../readiness`): the single "fit to submit" implementation (AD-5). **It reads prompts only** — emptiness/placeholder blocks, sameness warns. It is **not mode-aware**: `models.mode_specification_problems` (missing middle frame, song audio on a mode with no slot) and adapter absence are checked only at the render route, not in the report. See gap G4.
- **Arm-a-plan** (`spec-arm-a-plan.md`): specced, ruled buildable (bulk *arming* is FR-neutral; queuing stays Generate All's), **no route exists**.

### 2.5 Rendering

- `POST .../shots/{id}/generate/h3` (`H3Request`): refusal ladder is readiness → `status == "ready"` → mode has an adapter (`mode_without_adapter_refusal`) → `mode_specification_problems` → payload. Two branches:
  - **Text-only** (`h3-director`): `build_h3_director_payload`, `MiniMaxH3DirectorCS`, sends `timeline.aligned_frames` (17k+5 grid), defaults **1344×768 / 20 steps** (`H3_DIRECTOR_DEFAULT_*`), refuses sampling profiles and the resolution selector with a 422 (no evidence at those settings). Live-verified 2026-08-16.
  - **Reference** (`h3-reference`): `build_h3_reference_payload`, MiniMax H3 Ultra References-to-Video, 18 nodes (19 with a LoRA profile). Sampling profiles `default` (20-step no-LoRA, byte-pinned to the audited export), `turbo` (ref2v 4-step LoRA @0.7, rendered live once), `turbo-references2v` (canonical turbo export's bundle, **never rendered**). Default frame is the Director's own selection: **0.6 MP / 16:9 / multiple 32 → 1056×608** via `select_resolution`. Up to 9 pictures / 3 videos / 3 audios per the node's arity.
  - **Windowed song audio — built and correct in code.** `use_song_audio` appends the master song as a `ref_audios` reference **with a `trim` of the shot's own window** (`song_audio_window` in `workflows.py`; both keys validated; a window past the song's end is a 422 before GPU). *Note the doc/code disagreement:* `docs/ROADMAP.md` still lists this as "Open defect … specced" and "[ ] Audio range extraction per shot" — the code has it wired into both `generate_h3` and `restore_song_audio`. The code is ahead of the doc.
  - H3 **generates its own output audio by design** — the song reference drives lip movement only (measured ≈0.01 correlation with the master).
- `POST .../shots/{id}/render-again`: re-opens a settled shot (no body, one field). The app does **not** track takes; ComfyUI numbering keeps old files on disk unrecorded.
- **Batch**: the `#queue-ready` button (`app.js` ~1695) is a **client loop** — one server-side readiness check, one confirm, then `api.generateH3` per ready shot **in manifest order with no kind grouping** and empty bodies (so all defaults). No server batch endpoint, no `batch_id` (AD-7), no FR-9 same-kind ordering. See gaps G5/G6.
- **VRAM eject** (FR-10): every ComfyUI submission runs the `before_submit` hook (`comfy.py`) which asks LM Studio to release resident models and confirms by re-reading the listing (`vram.py`); toggleable in the topbar, stored in `data/machine-preferences.json`. Live-verified. This handles the *eject* half of the memory rule; the *front-load-text-work* half is procedural today (do all LLM passes before any render), not enforced by anything.

### 2.6 After the render

- **LTX 2.5 enhancement**: `POST .../shots/{id}/enhance/ltx25` → `build_ltx25_enhance_payload` (reachable subgraph of the audited export; four models; `ManualSigmas` starting at 0.909375). Writes nothing to the Shot; output lands on the job only. **Measured: it is a re-generation, not an upscale — it moves lips and must not run on singing shots** (`docs/WORKFLOW-MAP.md`). **The route does not check `Shot.singing`** — no gate exists in `enhance_with_ltx25`. `singing` today affects only the pass-two prompt. See gap G8. No UI control exists (grep of `app.js`/`api.js`: no enhance call sites).
- **Restore song audio**: `POST .../shots/{id}/restore-song-audio` → `build_audio_replace_payload` (zero model files; `VHS_LoadAudio` `seek_seconds`/`duration` from the *same* `song_audio_window`, so conditioning window and restored window agree by construction; loader `format: "None"` so the 17k+5 take is not conformed to 8k+1). Live-verified at +0.9945 correlation, lag 0.000 s. Sibling file; the Shot is untouched. **No UI.**
- **Job reconciliation** (`GET .../jobs/{id}`): moves `latest_output` for `kind="h3"`, asset paths for flux/multiview, song path for music. Deliberately no branch for `ltx`/`post` — enhanced/restored files never displace the take.
- **Approval and assembly: nothing.** No route writes `approved_output` (FR-21, AD-13). No ffmpeg/concat/assembly code exists in `src/` (FR-22, AD-9). This is where the pipeline ends today.

### 2.7 The milestone run as it would go today — where it stops

Track (SongPlanner, 60 s, headroom 1.5) → Flux character + setting + props → multiview sheets (character and objects) → chat until treatment/style bible/15 timed shots exist (`apply_shots`) → `director/expand` for intents → `assistant/fill` for modes/citations/singing (references+song-audio for performance shots, text_to_video for B-roll) → `shots/expand-prompts` sweep → **15 individual mark-ready clicks** (no bulk arm) → `#queue-ready` (works, but unordered and only prompt-checked) → 15 renders with correct lip-sync windows → per-take `restore-song-audio` **by API client only** → **stop.** No approval, no assembly, no master file. "Ready to run the video generation chain" is reachable; "good video composition and assembly" is not.

---

## 3. The adapter truth table — when the MiniMax workflows are employed

`models.SHOT_MODE_SPECS` is the routing table; `resolve_shot_mode` supplies the fallback for undeclared shots (citations or `use_song_audio` present → `references`, else `text_to_video`). `generate_h3` refuses `adapter == ""` with `mode_without_adapter_refusal` before building anything. The UI labels those rows "— no adapter yet" (`shotModeOptions`, `app.js` ~790).

| `ShotMode` | Adapter today | ComfyUI graph actually submitted | Evidence waiting if unrenderable | `use_song_audio` | `singing` |
|---|---|---|---|---|---|
| `text_to_video` | `h3-director` | **MiniMax H3 Director** — `build_h3_director_payload`, 15 nodes around `MiniMaxH3DirectorCS`; 1344×768/20 steps; aligned 17k+5 frames; live since 2026-08-16 | — | **Refused** (`song_audio=False` → `mode_specification_problems`: "no slot for the master song"). No audio conditioning; H3 still generates output audio | No render effect; steers pass-two wording (`SINGING_RULES`) |
| `references` | `h3-reference` | **MiniMax H3 Ultra References-to-Video** — `build_h3_reference_payload`, `MiniMaxH3MediaLoader`→`Splitter`→`ReferenceToVideo`; profiles `default`/`turbo`/`turbo-references2v`; 1056×608 default; live since 2026-08-18 | — | **The lip-sync path.** Master song appended as `ref_audios` with `trim` = shot window; drives mouth movement; never the output track (restore-song-audio or assembly puts the real track back) | Same as above — plus it is the flag the LTX enhancer *should* consult and does not (G8) |
| `image_to_video` | **none** (`""`) | — | `LTS2.5 I2V` export (LTX 2.5, `LTXVImgToVideoInplace` applied twice, strength 1.0 then 0.7) — on `J:\…\API-Workflows`, **not yet imported/hashed into `workflow_templates/reference_exports/`** | Declared `song_audio=False`; note an LTX render has no H3-style audio conditioning at all — a singing close-up cannot be this mode | Plannable; meaningless at render until an adapter exists |
| `first_last` | **none** | — | `MiniMaxH3 I2V-FLframe` export (H3's own first/last control) — same J:-drive status. Being H3-family, it is the one keyframe mode that could plausibly keep song-audio conditioning; whether the export has a `ref_audios` slot must be read from the file, not assumed | Declared `song_audio=False` today | Same |
| `first_middle_last` | **none** | — | `LTX2.5 FML` export (`LTXVFirstLastFrameControl_TTP` at 1.0 then 0.5 + `LTXVMiddleFrame_TTP`, position 0.51, strength 0.35 — position is a parameter, not a fixed centre) | Declared `song_audio=False`; LTX — no lip-sync | Same |
| `extend` | **none** | — | `LTX2.5 VideoExtender` (+`NoAudio` variant; `LTXVAudioVideoMask`, `max_length: "pad"`) — the escape hatch for a section above a generator's ceiling | Declared `song_audio=False` | Same |

**Not modes, by decision** (`models.py` docstring): image editing is a `length: 5` reference render (parameter of `references`); enhance and audio-replace are operations on takes with their own routes; slicing is an unadapted utility (`LTX2.5 VideoSlicer`, orphan-heavy, unimported).

**So, "when are the MiniMax music video workflows employed":**

- **MiniMax Music 3 / SongPlanner** — every generated song (`/generate/music`, `/generate/songplanner`).
- **MiniMax H3 Director** — every render of a shot that *is or resolves to* `text_to_video` (declared, or undeclared with no citations and no song audio).
- **MiniMax H3 Ultra References-to-Video** — every render of a shot that *is or resolves to* `references` (declared, or undeclared with citations or `use_song_audio`). This is the only lip-sync path.
- **The combined "04 - H3 Music Video - LTX 2.5 READY" pipeline** (H3→SeedVR2→LTX→FILM→VSR) is **never submitted by the application** — it regenerates H3 from creator media and is audited evidence only (`workflow_templates/reference_exports/h3-ltx25-user-export.json`, immutable, pre-fix by design).
- Everything labelled "no adapter yet" currently renders **nothing** and is refused with a 422 naming the modes that do render.

**The canonical turbo question (open):** two evidenced turbo bundles ship side by side — `turbo` (from the LTX music-video pipeline: `minimax_h3_ref2v_turbo_4step` @0.7, `beta`/`euler`/4; the only one with a live frame) and `turbo-references2v` (from the Director's canonical `MiniMaxH3Turbo References2V` export: `turbo_v4_step600_ema` @1.0, `simple`/`euler`/8; never rendered). One A/B render, one variable changed, then the Director rules which is canonical (`docs/WORKFLOW-MAP.md` open item; `MANIFEST.md`).

---

## 4. The gaps, each with what breaks, size, and dependencies

Sizes: **S** ≤ a day's story, **M** a few stories, **L** an epic-shaped effort. "Milestone-blocking" means the 60-second video cannot be called done without it.

### G1 — Take approval (FR-21, AD-13). **Milestone-blocking. S.**
No route writes `Shot.approved_output`; `render_again` and the enhancer both *read* approval but nothing can create one. Without it, assembly has no input set and "good composition" has no editorial act behind it. Depends on: nothing. Unlocks: G2, G9. The epics already specify: reversible, never automatic, per AD-13; the F/A hover keys are the layout lane's half.

### G2 — Assembly (FR-22, AD-9). **Milestone-blocking. M.**
No ffmpeg, no concat, no export path anywhere in `src/`. AD-9 is fully decided: local ffmpeg subprocess (extending the existing ffprobe pattern in `app.py` ~1996), **trim each approved take to its Shot window** — this is where the documented grid math lands: H3 renders long on the 17k+5 grid (measured 4.0 s → 4.458 s, ~11% overrun) and any LTX-processed clip is on the 8k+1 grid (192→185, and the enhancer's 90→89) — concat in shot order, **mux the master song as the sole audio track** (Director's ruling: shot audio dropped at assembly), output under `media/exports/`, ffprobe-verified within one frame of the song, recorded as `kind="post"` reconciled locally. Refuses on unapproved shots, stale takes, gaps/overlaps. Depends on: G1. Note a useful consequence: because assembly muxes the master song itself, `restore-song-audio` remains a *preview* convenience, not an assembly prerequisite. Also note assembly must choose *which file* per shot — the take, the enhanced sibling, the restored sibling — which drags in a minimal form of G9.

### G3 — Arm-a-plan (bulk ready). **Milestone-quality. S.**
Specced (`spec-arm-a-plan.md`), tension resolved (arming ≠ queuing), unbuilt. Without it the 15-shot plan is 15 inspector clicks. Should report "N armed, M skipped and why" and — now that `mode_specification_problems` exists — name a first/middle/last shot missing its middle image as unarmable *for that reason*. Depends on: G4 for honest skip reasons (buildable before it with prompt-only reasons).

### G4 — Mode-aware readiness. **Milestone-quality, and a real defect. S–M.**
`batch.readiness_report` checks prompts only. Consequences today: (a) a `ready` shot in a no-adapter mode, or a first/middle/last shot missing its middle image, **passes the client's pre-batch check and then 422s mid-loop** — producing exactly the half-submitted batch the pre-check exists to prevent (`app.js` queue-ready loop; `generate_h3` refusal ladder); (b) `mark-ready` will arm a shot its own render route will refuse. Fold `mode_without_adapter_refusal` + `mode_specification_problems` + `dangling_citations` into the report (or a parallel section of it), keeping AD-5's one-implementation rule. The shot-modes artifact already states this direction ("a first/middle/last shot missing its middle image is not ready, and no prompt check would notice"). Depends on: nothing.

### G5 — Server-side batch submission with same-kind ordering (FR-4, FR-9, AD-7). **Milestone-quality. M.**
Today's batch is a client loop in manifest order with per-shot submissions: no `batch_id`, no grouping of `h3-director` vs `h3-reference` shots (different UNET stacks; measured ~150 s eviction per interleave), no single server-side atomicity, and FR-9's "never interleave workflow kinds" is unenforced. Epic 4 Story 4.2 owns this; AD-7 (derived batch, `batch_id` on RenderJob, `flagged` on Shot) and the pre-flight modal (Story 4.2, layout lane) are already decided. Depends on: G4 (the pre-flight should tell the whole truth). The Draft/Master preset decision (640×384/8 vs 1344×768/20; backlog note in `ARCHITECTURE-SPINE.md`) lands in this modal — note the reference path's measured default is 1056×608, so "Master" needs a per-path definition when it is built.

### G6 — Adapters for the four unrenderable modes. **Not milestone-blocking; Director-priority-dependent. M each.**
Per mode: import + SHA-256 the export into `workflow_templates/reference_exports/` (none of the four is there yet — the evidence lives on `J:\…\API-Workflows`), derive the reachable subgraph (the MANIFEST's orphan caution is proven twice over), build the adapter, extend the pre-flight, add the route branch + `H3_ADAPTERS` entry (the import-time check forces this), live smoke. Honest assessment against the milestone: **the 60-second video does not require any of them** — `references` covers performance and identity-driven shots, `text_to_video` covers B-roll. They matter for the Director's step-5 flow (an image generated *for* a shot used as its first frame) and for sections above a ceiling (`extend`). Two rulings needed (§6): which mode first, and whether first-frame work goes H3-family (`first_last`, possibly audio-conditionable) or LTX-family (`image_to_video`, sharper but lip-sync-free and on the other frame grid). Depends on: nothing technically; sequencing only.

### G7 — Lyric-to-time alignment. **Not milestone-blocking. M–L.**
Nothing aligns words to windows: `timeline.song_section` is an explicitly empty branch, the pass-two payload sends the whole sheet with `song_fraction` as an honest hint, and the specialist's prompt states the sheet is unaligned (`docs/LLM-DIRECTOR.md`, "One claim it refuses to make"). Important nuance: **lip-sync itself does not depend on this** — the windowed `ref_audios` conditioning carries the actual sung sounds. What alignment buys is prompt accuracy (`<d>` lyric lines in singing shots, verse/chorus-aware planning) and the FR-26 `song_section` slot. A forced-alignment pass (e.g. whisper-class) over the generated FLAC is the obvious shape; it is new-capability work with its own model residency question. Depends on: nothing; benefits G-none critically.

### G8 — Enhancer singing gate. **Small safety hole. S.**
`enhance_with_ltx25` has no `singing` check, while the same repo documents "it breaks lip-sync and must not be used on singing shots" (`docs/WORKFLOW-MAP.md`). Today the only protections are the absence of a UI button and operator memory. A refusal for `singing == "singing"` is mechanical; **what `unknown` should do is an explicitly open Director question** (`models.py` docstring: nothing may infer it). Depends on: ruling R3.

### G9 — Take comparison / take identity. **Milestone-quality. M.**
The app tracks one pointer (`latest_output`); prior takes, enhanced siblings (`-ltx25` prefix) and restored siblings (`-song-audio` prefix) exist only as job `output_files` and disk numbering. `render_again` explicitly does not start take management. Assembly (G2) needs at minimum "which file is *the* approved take"; a real compare view is the layout lane's half. Depends on: G1 for the approval anchor.

### G10 — UI for enhance and restore-song-audio. **S. Layout-lane adjacent.**
Both routes are API-only (no call sites in `api.js`/`app.js`). Once G8's gate exists, surfacing them is small. Noted and left to the layout analysis.

### G11 — Shot-scoped image generation (the Director's step 5). **S–M.**
The Director described: open a shot, generate an image *for* it, in a role they assign. Today: `generate/flux` is library-scoped; the path Flux render → library asset → attach to shot → role select (`citation-role` in the inspector offers **all** roles including `first`) exists and works — so **yes, there is a path from a Flux render to a `first`-role citation**, but it is four manual steps, and a `first` citation is consumed by no adapter (G6). As a *reference*-role citation it is fully renderable today. The missing piece is the affordance (pre-filled prompt from the shot, auto-citation in a chosen role on completion), plus the job-refresh timing caveat from §2.2. Depends on: G6 for `first` to mean anything at render.

### G12 — ProducerBot + specialist iteration against real output. **Ongoing quality work, not a code gap.**
The two-pass structure is built and correct; what is unproven is one-shot quality: the persona is written-not-iterated (`deferred-work.md`), the ~1-in-6 reasoning-budget exhaustion is measured, and the chat thread is the one unbounded prompt term (8k-token window crossed at turn 8 with a max lyric sheet — the prompt-budget story is Ask First). The 15-linked-shots question resolves to: **the mechanism exists end to end; the quality is unvalidated at plan scale.** The cheapest evidence is exactly the milestone dry run of §2.7 up to mark-ready — no GPU beyond the track and stills.

### G13 — Residual pipeline items, listed for completeness.
Epic 4's live-batch UX (FR-6/7/8, NFR-1), FR-25 missing-media reporting (AD-11), FR-11 naming the loaded model at confirmation, the browser-QA defects (toast overlaying inspector buttons; controls hidden below ~1180/~860 px — **layout lane**), the `PUT /projects` shot-wipe hole (memory note: enumerate sibling write paths in specs), SeedVR2/FILM/RTX-VSR standalone adapters (Epic 7, drop-conditioned), and Krea sheet refine passes (quality lever on all reference fidelity).

---

## 5. The sequence

Dependency-ordered; each step leaves the milestone strictly closer.

1. **G4 Mode-aware readiness** (S–M) — first because it is a live defect (half-submitted batches) and because G3 and G5 both consume its answers. Pure `batch.py`/`models.py` work, no GPU.
2. **G1 Take approval** (S) — the smallest milestone-blocking piece; AD-13 already rules its shape.
3. **G3 Arm-a-plan** (S) — with G4's reasons, the report step is honest on day one. The plan now arms in one act.
4. **G2 Assembly** (M) — **the single biggest unlock**: with G1+G2 the pipeline produces the actual deliverable — one song-synchronized file — from shots the existing two adapters can already render. Trim math (17k+5 overrun, 8k+1 siblings) is fully documented; the audio ruling (song-only) is made. Minimal take selection (approved file per shot) comes with it (G9 seed).
5. **G5 Server batch + FR-9 ordering + pre-flight modal** (M) — turns "fire the plan" into one uneventful act with kind-grouped submission and the Draft/Master preset choice. (Modal surface is layout-lane.)
6. **G8 Enhancer singing gate** (S) + **G10 route UI** (S) — after ruling R3; cheap.
7. **Milestone dry run** (G12) — the full §2.7 sequence on a real 60 s track, now ending in an assembled file. This is also the forcing function for persona iteration and the turbo A/B.
8. **G6 first mode adapter** (M) — per rulings R1/R2, likely after the first assembled video proves the two-adapter milestone, since no mode is milestone-blocking.
9. **G7 lyric alignment, G11 shot-scoped images** — quality ladder, in whichever order the first assembled video makes urgent.

**The two-or-three that unlock the most, plainly:** (1) **approval + assembly** — everything before them already works well enough to feed them, and they are the definition of "a music video" rather than "a folder of clips"; (2) **mode-aware readiness → arm-a-plan → batch** — the difference between a plan that fires and a plan that is hand-pumped; (3) **the milestone dry run itself** — it is cheap, it exercises every seam named here, and this project's history says live runs find what suites cannot (the audio-window defect was found *by ear*).

---

## 6. Rulings needed from the Director (not buildable judgement)

- **R1 — Which unrenderable mode first**, if any before the milestone: `first_last` (H3-family), `image_to_video` (LTX-family), `first_middle_last`, or `extend`. The milestone needs none; the step-5 image-as-first-frame flow needs one of the first two.
- **R2 — H3 vs LTX for keyframe work**: LTX modes cannot lip-sync and live on the 8k+1 grid; whether `MiniMaxH3 I2V-FLframe` carries audio conditioning must be read from the export. This decides which family "a singing shot with a chosen first frame" belongs to.
- **R3 — Enhancer policy for `singing == "unknown"`**: refuse, warn-and-allow, or allow. (`singing == "singing"` refusal is assumed; even that is formally Ask First since it touches a working route's behaviour.)
- **R4 — Canonical turbo profile**: render `turbo-references2v` against `turbo`, one variable changed, then pick. Until then batch renders should pin one profile explicitly.
- **R5 — Enhancer starting-sigma tuning** (the lever that could make enhancement lip-safe): explicitly Ask First in the spec.
- **R6 — Prompt-budget story** (window/summarise the chat thread; pin the lyric sheet once): marked Ask First in `deferred-work.md`; becomes acute during the long planning conversation the milestone requires on an 8k-context model.
- **R7 — Headroom by ear**: the first long, lyric-dense song (≥ ~120 s at 1.0 vs 1.5) settles the 1.5-vs-1.0 question; the 60 s milestone track is itself a partial probe.
- **R8 — `shift_audio` 4 (Director graph) vs 3 (reference graph)**: standing recorded discrepancy; both in range, neither provably wrong.

---

## 7. Where docs and code disagree (checked while walking)

- `docs/ROADMAP.md` records the song-audio-window bug as an **open defect** and "Audio range extraction per shot" as unbuilt; `workflows.song_audio_window` + the `trim` in `generate_h3` and `build_audio_replace_payload` show it **built and wired**. Code is ahead of the doc.
- `docs/ROADMAP.md` "Rendering and delivery" still shows "[ ] LTX 2.3/2.5 enhancement" while the same file's 2026-08-18 section records the enhancer **built and verified live**; internal inconsistency.
- `docs/WORKFLOW-MAP.md` (MiniMax H3 Director section) says `DirectorTimeline.aligned_frames` is "computed but unused by the payload"; `app.py` line ~3294 passes `requested_frames=timeline.aligned_frames`. Stale — superseded by the 2026-08-16 off-grid verification recorded elsewhere in ROADMAP.
- `epics.md` FR-20 annotation "adapter built; never rendered live" is superseded by the 2026-08-18 live reference render.
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (`last_updated: 08-17`) predates the entire shot-mode/ProducerBot/pass-two/enhancer/restore wave; it tracks the old epic list only.
- `shot-modes-and-pre-generation-planning.md` calls for mode-aware readiness and per-mode duration bounds enforced at the timeline; neither is in `batch.py`/`timeline.py` yet (readiness is prompt-only; the timeline snaps drags to 0.25 s, not to a mode's frame grid). Direction accepted, not yet decomposed — consistent with its own status line.
