# Timeline-Centric Layout Analysis

**Status:** analysis for decision, 2026-08-18. No code was changed; this document is the deliverable.
**Brief:** the Director's direction to pull back from the proposed wizard and move toward a Premiere-Pro-style, timeline-first layout — "everything should focus around the timeline assembly," with the workflow described as: click a section of the timeline and see the assets that build it; a prefill button beside an assistant chat targeting the selected shot; scroll shot-for-shot with the music, replacing and editing, lining shot edges up with the music's flow; per-clip "Generate Video" buttons and a "Generate All" with a Replace Existing toggle — "a really solid polished pre-gen editor feel."
**Lane:** layout and interaction only. A parallel production-pipeline gap analysis covers adapters, batch routes, and render machinery; where this document touches those (per-clip Generate needs adapters that do not exist), the dependency is named and left to that lane.

Sources read in full: `src/music_video_producer/web/index.html`, `src/music_video_producer/web/assets/app.js` (1,783 lines), `assets/api.js` (selected), `assets/state.js`, `assets/styles.css` (397 lines); `docs/ROADMAP.md`, `docs/OPERATIONS.md`, `docs/ARCHITECTURE.md`; `_bmad-output/planning-artifacts/epics.md`, `shot-modes-and-pre-generation-planning.md`, `ux-designs/ux-mvp-2026-08-16/{DESIGN,EXPERIENCE}.md`, `architecture/.../ARCHITECTURE-SPINE.md`, `_bmad-output/implementation-artifacts/sprint-status.yaml`.

One path correction up front: the frontend is **not** `web/app.js` / `web/styles.css` — everything lives under `src/music_video_producer/web/assets/` (`app.js`, `api.js`, `state.js`, `styles.css`), with only `index.html` at `web/`. Every selector cited below is from those files as they exist today.

---

## 1. Inventory — what exists, and where it lives

### 1.1 The shell

`index.html` builds a fixed shell: a grid `.app-shell { grid-template: var(--topbar) 1fr / var(--rail) 1fr }` (`styles.css:35`) with three permanent regions and one swapped one:

- **Topbar** (`header.topbar`, 58 px): brand · project switcher (`#project-select`, `#new-project`) · mini transport (`#jump-start`, `#global-play`, `#global-time`) · system state (`#comfy-dot`/`#comfy-label`, the VRAM eject `#vram-eject-toggle` + `#vram-eject-note`, `#save-project`).
- **Rail** (`nav.rail`, 94 px): five workspace buttons with Consolas indices `01 Song / 02 Treatment / 03 Assets / 04 Timeline / 05 Queue`. The rail is a plain panel switcher; no wizard ticks, banner, or derived step exist anywhere in code (`grep -i wizard src/` returns nothing; there is no `wizard.js`).
- **Workspace** (`main.workspace`): five `section.panel` elements; exactly one carries `.active` (`display:block`; the rest `display:none`, `styles.css:68-69`). Switching is `bindEvents`' rail handler (`app.js:1338-1343`), which also fires `requestAnimationFrame(renderTimeline)` when entering the timeline — necessary because canvases measured while `display:none` report zero size.
- **Toast region** (`#toast-region`, fixed bottom-right, `pointer-events:none` after the 2026-08-18 browser-QA finding that a toast swallowed a click aimed at `#compile-shot`).

### 1.2 The five workspaces, control by control

**01 Song (`#panel-song`)** — two columns (`.song-layout`, 365px + flexible):
- Import block: drop zone `#song-drop`/`#song-file`, `#import-title`, `#import-lyrics`/`#import-style` (with `.field-count` counters), `#import-song`.
- MiniMax Music 3 form `#music-form`: title, caption, lyrics, duration/seed/preset thirds row, the SongPlanner-only headroom field `#music-headroom-field` with its live encoder-ceiling readout `#music-ceiling`.
- Stage: source badge `#song-source`, actions (`#analyze-song` — a **permanently disabled stub**, `#remove-song`, `#send-treatment`), the single persistent `<audio id="master-audio">` that drives every transport in the application, the big waveform canvas `#waveform` + `#song-playhead`, a time ruler, the analysis strip (`#bpm-value` and `#sections-value` are **hardcoded "Not analyzed"** — no analyser exists anywhere; `docs/ROADMAP.md:68` is explicit that nothing can name the section a shot sits in), the song-context editors `#song-lyrics`/`#song-style` with per-field restores, and a static production note.

**02 Treatment (`#panel-treatment`)** — chat column + document column:
- Chat: thread `#chat-thread` (rendered whole via `threadHtml`, including the notice blocks Story 2.4 shipped), composer `#chat-form` with the per-turn consent `#apply-documents`, and — load-bearing for this analysis — the **assistant row**: `#prefill-shot`, `#assistant-fill-all`, `#assistant-fill` (`api.js:1479-1481`). These three are enabled/disabled off the **timeline's shot selection**, repainted by `syncAssistantControls()` which is called from `renderTimeline()` because, in the code's own comment, the controls "live in the composer, **two panels away**, and their state is decided by the shot selection this function owns" (`app.js:671-675`).
- Documents: tabs Brief/Treatment/Style with per-document lock + restore, `#save-treatment`, `#expand-shot-prompts` (pass one, whole plan), `#expand-h3-prompts` (pass two sweep, `EXPAND_ALL_PROMPTS_CONTROL`).

**03 Assets (`#panel-assets`)** — three columns (`.asset-layout`: 310px generator, flexible library, 270px inspector):
- Flux generator `#flux-form`; upload `#upload-asset-button`/`#asset-file`.
- Library: filter segments `#asset-filters`, search `#asset-search`, grid `#asset-grid` of draggable `.asset-card`s (dragstart sets `text/asset-id`; timeline clips accept the drop and add a citation with role `reference`).
- Asset inspector `#asset-inspector` (`renderAssetInspector`, exported for the contract tests): preview, provenance, vision summary, `#analyze-asset`, `#create-multiview` (gated by `multiviewPlan`), `#attach-asset` (enabled only when a shot is selected — another cross-panel dependency).

**04 Timeline (`#panel-timeline`)** — the panel the Director wants the whole application to become:
- Tools row: `#add-shot`, `#split-shot`, `#duplicate-shot`, `#delete-shot`, zoom `#zoom-out`/`#zoom-label`/`#zoom-in`.
- Main: transport (`#timeline-start`, `#timeline-play`, `#timeline-time`, `#timeline-duration`), scrolling canvas with four tracks — `#section-track` (**never populated**; waiting on the analyser that does not exist), MASTER `#timeline-waveform` (a second drawing of the same `state.audioBuffer` the Song stage draws), SHOTS `#shots-track` (clips with drag/resize handles, `NO PROMPT` flag state via `shotPromptCell`, selection), REFERENCES `#refs-track` (read-only pills) — plus playhead `#timeline-playhead`.
- **Shot inspector** (`aside#shot-inspector`, 280 px, `renderShotInspector` — the richest surface in the application): readiness block (`shotInspectorReadiness` — blocking flag + sameness lines), start/duration, **mode select** `#shot-mode` (`shotModeOptions`: "Not declared — renders as …" plus every `SHOT_MODES` entry, unadaptered modes labelled "— no adapter yet"), mode-specification problems (`shotSpecificationProblems`, mirroring `models.mode_specification_problems`), **singing select** `#shot-singing` (`SINGING_STATES`), creative intent `#shot-prompt`, the **H3 expansion box** (`#shot-h3-prompt` drawn only when `shot.h3_prompt` is non-empty; `#expand-prompt` per-shot pass-two button; `#expansion-report` for a refused expansion), seed, **citations with roles** (`#shot-asset-select` attach, `.citation-row` per citation with a role `<select>` from `ASSET_ROLE_LABELS` and a remove button; missing assets render as `citation-missing` rows, never as nothing), `#shot-song-audio`, `#analyze-take`, the **mark-ready / render-again pair** (`markReadyControl` / `renderAgainControl` — complementary, exactly one drawn per shot, disabled-with-reason via `.control-reason`), and `#compile-shot`.

**05 Queue (`#panel-queue`)**:
- Heading actions: `#refresh-jobs`, `#queue-ready` (state from `queueButtonState`, which folds in `batchReadinessBlock`).
- **Plan readiness region** `#plan-readiness` (aria-live; `readinessLines` — blocking + sameness in the server's own sentences), fetched on every project load (`loadReadiness`) and after every shot save.
- Job table `#job-list`; a static "Planned finishing route" aside (`.finish-stack`) that is pure copy — five numbered stages, no live state.

### 1.3 How rendering works (matters for costing)

- All DOM is built by `innerHTML` template strings inside render functions (`renderSong`, `renderTreatment`, `renderAssets`, `renderTimeline`, `renderShotInspector`, `renderJobs`, `renderReadiness`), orchestrated by `renderAll()`. Every element is addressed by **id or class selector**, never by position — which is what makes markup relocation cheap.
- Pure decision functions live in `api.js` and are executed (not grepped) by `tests/test_frontend_contract.py` against a stub DOM: `renderSong`, `renderShotInspector`, `renderAssetInspector`, `syncAssistantControls`, `syncExpansionControls` are exported specifically so the tests can run them.
- The inspector is rebuilt by replies nobody awaited (readiness GETs after shot saves); `captureInspectorEdit`/`restoreInspectorEdit` carry the focused control's value and caret across the rebuild (`app.js:762-776`). This was a live browser-QA defect (`docs/OPERATIONS.md`, "The app re-rendered after replies it never awaited") and its mitigation is load-bearing.
- Shot writes serialize through `saveShotsSilently()`'s promise chain, guarded by `shotWriteInFlight` so an expansion or assistant fill can't be reverted by a queued whole-list save.
- The single `#master-audio` element is the one playhead source for the topbar transport, the Song waveform, and the timeline (`timeupdate → updateTimelinePlayhead`). `docs/ARCHITECTURE.md:32` names this deliberately.

### 1.4 Responsive behaviour a redesign inherits

`styles.css` carries exactly two breakpoints, both written *after* browser QA found controls vanishing (`docs/OPERATIONS.md`, "Three controls vanished at narrow widths"):

- **≤1180 px** (`styles.css:348-363`): topbar loses the mini transport; the **asset inspector reflows** to a full-width row under the library (it is the only surface carrying `#create-multiview`/`#analyze-asset`/`#attach-asset`, so hiding it removed three actions outright — the comment records this).
- **≤860 px** (`styles.css:364-393`): rail narrows to 68 px; the VRAM eject toggle hides *with* its note (the topbar has no room to reflow, and "hiding half a control is worse than hiding all of it"); every layout collapses to one column; the **shot inspector stacks under the timeline** (it holds the only mark-ready/render-again/compile controls and the only prompt editor, so it must not disappear); the workspace becomes scrollable.

`tests/e2e_shot_controls.py` asserts control reachability at 1600/1280/1024/820. Any new layout must re-earn those assertions — they encode the rule "no control exists only at one width."

### 1.5 The problem, stated from the code

The workflow the Director described is **already one workflow spread across four panels**, and the code visibly strains against the panel structure:

1. The assistant controls sit in Treatment but are driven by the Timeline's selection — `renderTimeline()` reaches "two panels away" to repaint them, and the Director's actual gesture (click a shot → click prefill → type → send → see the shot change) requires a rail round-trip per shot.
2. Attaching an asset to a shot requires the Assets panel's inspector plus a shot selected in the Timeline panel — `#attach-asset` is disabled unless `selectedShot()` is truthy, a dependency on a panel that is not on screen.
3. Plan readiness is computed for the timeline's plan but rendered in the Queue panel (`#plan-readiness`), and mark-ready happens in the Timeline's inspector while the button it feeds (`#queue-ready`) is in the Queue.
4. The waveform is drawn twice (`#waveform`, `#timeline-waveform`) because the song and the timeline are two rooms that both need to see the music.

Cross-panel wiring like `#send-treatment` literally clicking the rail button (`app.js:1642`) is the tell: the panels were never independent; they are one editor with walls.

---

## 2. Proposed layout — Premiere-style, timeline as the permanent anchor

### 2.1 Region map

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ TOPBAR  project ▾ ＋ │            │ ComfyUI ● · Eject LLM ☑ note · Save          │
├──────────────┬───────────────────────────────────────────────┬───────────────────┤
│ BIN          │ STAGE   [Monitor] [Documents] [Song] [Queue]  │ INSPECTOR         │
│ ────────────  │                                               │ ────────────────  │
│ All Char Set │   Monitor: selected shot's latest take, or    │ Shot (selected    │
│ Prop Style   │   master audio + large waveform when no take  │ clip) or Asset    │
│ [search]     │   Documents: brief / treatment / style bible  │ (selected bin     │
│ ┌──┐┌──┐┌──┐ │     + locks/restores + expand pass 1 & 2      │ item) — the       │
│ │  ││  ││  │ │   Song: import · Music 3 · song context       │ existing panels,  │
│ └──┘└──┘└──┘ │   Queue: job table + refresh                  │ verbatim: mode,   │
│ [Upload][Flux]│                                              │ singing, intent,  │
├──────────────┤                                               │ H3 box, citations │
│ ASSISTANT    │                                               │ +roles, readiness,│
│ thread       │                                               │ mark-ready /      │
│ [composer…]  │                                               │ render-again,     │
│ Prefill·Fill │                                               │ Generate ▸        │
├──────────────┴───────────────────────────────────────────────┴───────────────────┤
│ READINESS  22 of 25 prompted · 1 blocking · 2 near-duplicate   [Generate all ▸]  │
│                                                     Replace existing ☐           │
│ TIMELINE  │◀ ▶ 00:00:00 ── 24 FPS ── zoom − 100% ＋ │ ＋Shot Split Dup Delete    │
│ SECTIONS  (empty lane, reserved for the analyser that does not yet exist)        │
│ MASTER    ▁▂▅▇▆▃▁▂▄▇▇▅▂▁▃▅▇▆▄▂▁  ← the one waveform; #waveform in Song dies     │
│ SHOTS     [S01 ▸][S02 ▸][S03 NO PROMPT][S04 ⚑][S05 ✓]…  per-clip state + ▸ Gen  │
│ REFS      [Lucy sheet][forest]…                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘
```

Premiere role mapping: **Project bin** = asset library (left, permanent — it already supports drag-to-timeline). **Program monitor** = the Monitor stage tab (see open question 1 — no video preview exists today). **Source monitor** has no equivalent and none is proposed; the Asset inspector's preview covers "look at a source before using it." **Effect Controls / Essential panels** = the right-hand Inspector. **Timeline** = the timeline, permanent, full width, bottom.

### 2.2 Always visible

- **Timeline** (all four tracks, transport, tools, zoom). Being permanently laid-out removes today's `requestAnimationFrame(renderTimeline)` hack on rail entry — canvases always have real dimensions.
- **Readiness strip**: the current `#plan-readiness` region relocated to sit directly above the plan it describes, with the batch action beside it. `readinessLines`/`readinessSummary`/`queueButtonState` are already pure and placement-free.
- **Inspector** (right): the existing `#shot-inspector` markup and behaviour unchanged — every load-bearing control it carries today (mode select with "no adapter yet" labels, singing state, creative intent, the H3 expansion box and its refusal report, citation rows with roles, the mark-ready/render-again pair with `.control-reason` sentences) survives verbatim. The **asset inspector shares this dock** (see §2.4 and risk 2).
- **Assistant chat dock** (left, below the bin): thread + composer + the assistant row (`#prefill-shot`, `#assistant-fill`, `#assistant-fill-all`). This is the geometry the Director described — prefill *beside* the chat, both beside the timeline whose selection they act on. `syncAssistantControls` already runs from `renderTimeline`, so no wiring changes; the controls just stop being two panels away.
- **Topbar** as-is, including the VRAM eject pair (a machine-wide setting that belongs beside ComfyUI status, per the long comment at `index.html:31-43` — do not move it into any workspace). The mini transport (`#global-play`/`#jump-start`/`#global-time`) **dies**: it exists because the timeline transport was usually off-screen, and it already hides below 1180 px.

### 2.3 Summoned (stage tabs or drawers)

- **Documents** stage tab: the three editors, locks, restores, `#expand-shot-prompts`, `#expand-h3-prompts`. This work is front-loaded, per-session, and text-heavy — exactly the "do all text work in one pass so the LLM loads once" discipline `docs/OPERATIONS.md:51` prescribes — so it does not need to be permanent, only one click away without losing the timeline.
- **Song** stage tab (or drawer): import block, Music 3/SongPlanner form, song-context editors with their restores and counters. These are start-of-project acts; after the song exists they are visited rarely. The stage's Monitor tab shows the master waveform large when no take is selected, so the Song tab keeps only the *forms*.
- **Queue** stage tab: the job table and `#refresh-jobs`. Job *state* migrates onto clips (border/chip language already specified in `DESIGN.md` — pending/running/complete/error/flag/approve — none of it built yet); the table remains the inspect-on-demand truth with exact ComfyUI errors (FR-6's unshipped UI half).

### 2.4 Merges and deaths

| Element today | Fate |
|---|---|
| `#waveform` (Song stage canvas) + `#timeline-waveform` | **Merge** — the timeline MASTER track is the one waveform; the Song tab keeps only `#master-audio`'s native controls if anything |
| `.transport-mini` (topbar) | **Dies** — the always-visible timeline transport is the transport |
| `.rail` (five-button switcher) | **Dies**, replaced by the stage tab strip (Monitor/Documents/Song/Queue). The rail-as-wizard concept dies with it (§3) |
| `.finish-stack` ("Planned finishing route" static aside) | **Dies** — static decorative copy; the Operate/Command-Inspect decision (`docs/ARCHITECTURE.md:26`) already rejects decoration |
| `#asset-inspector` (third column of Assets) | **Merges** into the right Inspector dock as the asset-selection face; `#attach-asset` finally sits next to the shot it attaches to |
| `#plan-readiness` (Queue panel) | **Moves** above the timeline |
| `#queue-ready` ("Queue ready H3 shots") | **Becomes** "Generate all" beside the readiness strip, gaining the Replace Existing toggle (backend semantics: pipeline lane) |
| `#send-treatment` ("Build treatment →" that clicks the rail) | **Dies** — replaced by the Documents tab being one click away |
| `#section-track` | **Stays** as a reserved lane. It is honest scaffolding: the analyser gap is documented (`ROADMAP.md:68`), and the lane is where "line shot edges up with the music's flow" eventually gets beat/section snapping |

### 2.5 Per-clip Generate and Generate All

- **Per-clip:** on-clip hover micro-actions were already contracted in `DESIGN.md` §3 (flag/approve with `F`/`A` keys); a **▸ Generate** action joins them, mirrored by a button in the inspector (the inspector copy is the accessible/keyboard path, per the existing "hover-only affordances always have a keyboard/inspector equivalent" rule in `EXPERIENCE.md`). Its enabled state must come from one pure decision function in `api.js` (the `markReadyControl`/`renderAgainControl` pattern), folding in: prompt gate, mode adapter existence (`SHOT_MODES` today ships `adapter: ""` for image_to_video, first_last, first_middle_last, and extend — **only** text_to_video→`h3-director` and references→`h3-reference` exist, `api.js:1280-1287`), lock, approval, in-flight. A clip whose mode has no adapter shows a disabled Generate with the reason — never a live button that 422s. *Adapter existence itself is the pipeline lane's problem; the layout's obligation is only to render its absence honestly.*
- **Generate All + Replace Existing:** today's `#queue-ready` handler loops `api.generateH3` per shot client-side after one readiness check (`app.js:1695-1740`); AD-5's server-side batch endpoint is unbuilt, and Replace Existing is decided ("Generate All skips shots that already have a take, with an explicit Replace Existing toggle" — `shot-modes-and-pre-generation-planning.md`, decisions of 2026-08-18) but expressed nowhere in FR-4/FR-5 or code. The layout gives it a home — the readiness strip — and the confirmation flows through the pre-flight modal the UX contract already specifies (`DESIGN.md` §4: readiness list, time estimate, LM Studio warning, one confirmation). The toggle's semantics (what "existing" means against `latest_output` vs `approved_output`) belong to the pipeline lane.
- **Mark-ready survives.** The readiness gate is deliberately not keyed to status (`ROADMAP.md`, readiness entry), but `ready` is what the batch submits, and the mark-ready/render-again pair is the only commitment step the primary journey has (`docs/OPERATIONS.md` §Mark a shot ready). The timeline-first layout keeps the pair in the inspector; whether per-clip Generate *implies* arming is open question 3.

---

## 3. Reckoning with the wizard

**What the wizard is on paper.** FR-1 (progress derived purely from project state, never stored), FR-2 (each step presents the real workspace), FR-3 (escapable; never reappears past first completed render) — Epic 6, stories 6.1–6.3. The architecture ratified it as AD-3 (frontend-only pure `deriveStep(project)` in a new `wizard.js`) and AD-4 (a fixed derivation chain: no song → 01, song no shots → 02, no reference sheet → 03 Cast, any draft/empty-prompt shot → 04, else → 05 Render; any complete h3 job → wizard permanently off). The UX contract's first load-bearing decision was "the rail doubles as the wizard" with a guidance banner (`EXPERIENCE.md` §"The wizard is the rail"; `epics.md` §UX Design Requirements).

**What the wizard is in code: nothing.** No `wizard.js`, no banner, no ticks, no derived step — `grep -i wizard src/` is empty, and `sprint-status.yaml:71-75` shows epic-6 and all three stories at `backlog`. Pulling back from the wizard **abandons zero built code**. That is the single most important cost fact in this analysis.

**What is genuinely superseded:**
- FR-2 and FR-3 as stated. They are wizard-chrome requirements ("each Wizard step", "Wizard escapable") and have no referent once there is no wizard. Story 6.3's browser QA journey is likewise void as written.
- The rail-as-wizard UX decision, and with it the guidance banner as a *step* banner. The rail itself dies in the proposed layout, so the surface the wizard was going to decorate no longer exists.
- AD-4's chain as a *step* selector. (Its predicates remain useful — see below.)
- `EXPERIENCE.md`'s "Information architecture — Unchanged … The Production Wizard adds no new surface" is now false in both halves: the IA changes, and there is no wizard. That document is marked `status: final`; it needs a superseding note, not silent contradiction.

**What survives in spirit, and should be said out loud when FRs are rewritten:**
1. **FR-1's rule survives intact: derived, never stored.** It is already the codebase's strongest habit — readiness is derived on every call (AD-5, "never stored"), batch state is derived (AD-7), the manifest is the sole truth (Design Paradigm: "Derived state beats stored state everywhere"). Whatever replaces the wizard must compute "what does this project need next" from the manifest every time, and persist nothing.
2. **AD-4's derivation chain survives as an empty-state and next-action oracle.** The same pure function that would have picked a wizard step instead drives: the empty timeline's message ("No song yet — open the Song tab and import or generate one"), the empty bin's message, the readiness strip's summary when there are no shots, and optionally a single quiet "next:" hint in the topbar or readiness strip. First-run guidance in a timeline-first world is **the empty states of the permanent regions**, each stating the one thing that unblocks it — which is arguably more honest than a wizard, because the guidance appears exactly where the missing thing will land. FR-3's "never reappears past first completed render" becomes trivially true: a project with content has no empty states.
3. **FR-2's real principle — never build a copy of a workspace — survives** as: the stage tabs *are* the workspaces, not scoped duplicates. The proposed layout satisfies it more strictly than the wizard would have, since there is only ever one instance of each surface.

**Recommended paper actions** (for the PM/architecture pass, not this analysis): mark FR-2/FR-3 superseded with a pointer to this document; restate FR-1 as a layout-independent invariant ("any 'what next' guidance is a pure function of the manifest"); retire Epic 6 stories 6.1–6.3 and AD-3 (AD-4's chain re-homes into whatever module owns empty states); add a superseding note to `EXPERIENCE.md`/`DESIGN.md` §1 (guidance banner). The other five UX-contract components (clip chips, hover actions, pre-flight modal, safety notices, missing-media placeholder) are wizard-independent and stand.

---

## 4. Cost and sequencing

### 4.1 What is markup/CSS versus real `app.js` work

**Cheap (markup + CSS, near-zero JS):** relocating elements. Every render function targets ids (`$("#shots-track")`, `$("#plan-readiness")`, `$(ASSISTANT_PREFILL_CONTROL)`…) and never assumes ancestry, so moving `#plan-readiness` above the timeline, the assistant column into a left dock, or `#shot-inspector` into a right dock is `index.html` surgery plus a new grid in `styles.css`. The contract tests (`tests/test_frontend_contract.py`) execute exported functions against a stub DOM keyed to the same ids — they are **layout-blind** and survive any relocation that preserves ids.

**Moderate JS:**
- Replacing the rail handler (`app.js:1338-1343`) with a stage-tab handler; deleting `state.activePanel` semantics or narrowing them to the stage.
- Deleting `.transport-mini`, `#send-treatment`, the Song-stage waveform draw (one branch of `renderSong`), `.finish-stack`.
- The inspector focus model (shot vs asset selection — see risk 2): new but small, one function deciding which face the dock shows.
- Empty-state guidance: one pure `nextAction(project)` in `api.js` (AD-4's chain), rendered into region empty states — the same pattern as every existing control-decision function, testable the same way.

**Real work (and partly out of lane):**
- **Per-clip Generate**: new pure control-decision function + on-clip hover actions (the `DESIGN.md` §3 component, unbuilt) + keyboard path. Frontend-substantial; backend adapters are the pipeline lane.
- **Generate All + Replace Existing + pre-flight modal**: the modal is the UX contract's only modal and is unbuilt; the batch endpoint (AD-5) and Replace semantics are pipeline lane.
- **Monitor tab with video playback**: entirely new — nothing in the application plays video today; takes are file paths in job rows. ComfyUI `/view` URLs play fine in a media element cross-origin, but anything that *reads* the bytes (waveform-style analysis, thumbnails via canvas) hits CORS — the known constraint that already blocks browser QA of generated-song playback (`ROADMAP.md:109`, "needs ComfyUI started with `--enable-cors-header`"). A proxy route through the app is the clean fix and is backend (pipeline lane).
- **Live clip states during a batch**: needs AD-1's `render-status` polling endpoint (unbuilt, pipeline lane). The layout should reserve the clip-state visual language now (it is fully specified in `DESIGN.md`) and render only the states derivable today (draft/no-prompt/complete-by-latest_output).

### 4.2 Sequence that keeps every existing browser test meaningful

The e2e suite (`e2e_first_run.py`, `e2e_audio_playback.py`, `e2e_epic2_surfaces.py`, `e2e_shot_controls.py`, `e2e_song_context.py`) asserts *presence, hit-testability, and behaviour of controls by id* — plus the width matrix 1600/1280/1024/820. The sequence below never removes a control's id before the step that re-homes it, so each script needs only its navigation preamble updated per phase, never its assertions rewritten wholesale.

1. **Phase A — shell regrid.** `.app-shell` becomes topbar / main / timeline-row. The timeline panel's `.timeline-main` (tracks + transport + tools) moves to the permanent bottom row; the five workspaces become stage tabs in the main row's centre, *initially unchanged inside*. `#shot-inspector` stays where it is (inside what was the timeline panel, now docked right of the timeline or right of the stage — pick one and keep it through Phase B). Delete `requestAnimationFrame(renderTimeline)` on tab entry (no longer needed). Re-baseline the width assertions. Everything still renders through `renderAll()` untouched.
2. **Phase B — inspector dock.** Move `#shot-inspector` and `#asset-inspector` into the shared right dock with the focus model. `renderShotInspector`/`renderAssetInspector` bodies unchanged; only the container and a small "which face" decision are new. `e2e_shot_controls.py` (mark-ready, render-again, compile) and the multiview/attach assertions keep passing against the same ids.
3. **Phase C — assistant dock + stage tabs for Documents/Song/Queue.** Move `.chat-column` to the left dock; `.document-column` becomes the Documents tab; Song forms and Queue table become tabs; `#plan-readiness` moves above the timeline; kill rail, mini transport, `#send-treatment`, second waveform, finish-stack. `e2e_song_context.py` and `e2e_epic2_surfaces.py` update navigation (open the Song/Documents tab instead of clicking rail items) but assert the same controls.
4. **Phase D — per-clip Generate, Generate All + Replace Existing, pre-flight modal, clip-state chips.** Gated on the pipeline lane's adapters/batch endpoint; the frontend halves (control decisions, hover actions, modal) can land behind honest disabled states earlier.
5. **Phase E — empty-state guidance + paper cleanup.** `nextAction(project)`, region empty states, FR/Epic/UX-doc supersession notes (§3).

Phases A–C are almost entirely `index.html`/`styles.css` plus deletions; they deliver the Director's described interaction loop (click clip → prefill beside chat → fill → see the clip change, without ever leaving the timeline) **before** any pipeline-lane work lands.

### 4.3 The riskiest moves

1. **The always-visible timeline meets the un-awaited-reply rebuild pattern.** Today `renderTimeline()` rebuilds every clip via `innerHTML` on every selection, drag frame, zoom, project load, and readiness reply — tolerable partly because the panel is usually hidden. Permanent visibility makes every rebuild visible and makes the documented stale-element/caret hazard (`OPERATIONS.md`: "the app rebuilds the inspector after replies it does not await") a constant condition rather than a timeline-panel one. NFR-1's 40-shot bar applies. Mitigations exist in-idiom (the `captureInspectorEdit` pattern; comparing against `original` before saving), but this is the place a regression will hide, and the browser resource-timing assertions ("a selection issues zero writes and zero readiness reads") must be preserved and extended.
2. **Merging the two inspectors needs a focus model that does not exist.** `state.selectedAssetId` and `state.selectedShotId` are independent and both persist; today the panels resolve the ambiguity by being in different rooms. One dock must decide which selection it shows (last interaction wins? explicit tabs?) without breaking `#attach-asset`'s "asset selected *and* shot selected" case — which is precisely the case the merge exists to serve. Get this wrong and the merge reintroduces the vanished-control class of defect the 1180 px fix just closed.
3. **The composer serves two masters, and moving it stresses the consent design.** One textarea feeds the Director send (with `#apply-documents` per-turn consent, `confirmDiscardingDocumentEdits`, and document re-render side effects) *and* the three assistant controls (deliberately `type="button"`, "a different button, a different route" — `styles.css:192-196` comment). Docking the composer beside the timeline while the documents it can rewrite live in a sometimes-hidden stage tab means a consented Director turn can replace a document that is not on screen. The gate wording survives (it names the documents), but the geometry weakens the "editable structured output stays visible" assumption. Splitting into two composers doubles state (two drafts, two unsaved-work answers); keeping one demands the Documents tab auto-open on a consented turn or an equivalent. This is open question 2 — it changes markup, `unsavedWorkPending`, and two e2e scripts, so it must be decided before Phase C.

---

## 5. Open questions for the Director

1. **What is the Monitor for v1?** No video playback exists anywhere in the application; building a real program monitor needs take-serving decisions (direct ComfyUI `/view` vs an app proxy for CORS) that sit in the pipeline lane. Options: (a) Monitor tab ships audio-first — master audio + large waveform — with video takes following; (b) the first layout pass has no Monitor tab at all and the center stage opens on Documents; (c) video playback is a launch requirement and Phase A waits on the proxy. This decides the stage's default tab and whether the layout ships this sprint or next.
2. **One composer or two?** Does the assistant keep sharing the Director chat's textarea (today's design), or get its own input docked with the timeline while Director chat stays with the documents? Determines the chat dock's contents, the consent geometry for `#apply-documents`, and how `unsavedWorkPending` counts drafts. (Risk 3 above.)
3. **Does per-clip Generate imply arming?** Today the commitment step is explicit: mark-ready, then the batch submits `ready` shots, and `spec-arm-a-plan.md` is on record as needing reconciliation with the pre-planning premise. Is clicking Generate on a `draft` clip (that passes the prompt/mode gates) a one-click render, or does it first require mark-ready — keeping "arming" and "spending GPU" as two acts everywhere? This decides the per-clip control's decision function and whether `markReadyControl` stays a visible pair or becomes an internal transition.
4. **What is the width floor?** Bin + chat + stage + inspector + timeline is realistically a ≥1440 px layout. The current 1180/860 reflows get replaced wholesale — so: what is the smallest window this must be *usable* (not merely not-broken) at, and which docks collapse first below it? The answer is needed before Phase A's CSS is written, because the e2e width matrix (1600/1280/1024/820) is re-baselined against it and the "no control exists at only one width" rule must be re-proven.
5. **Where does song setup live once the rail dies?** Import, Music 3/SongPlanner generation, and the song-context editors: a Song stage tab (always one click away), or a summoned dialog/drawer that appears only when the project has no song (the AD-4 predicate)? This also decides whether the section-track lane stays visibly empty as a promise or is hidden until an analyser exists — the honest-emptiness question the "Not analyzed" strip already answers one way.

---

## Appendix: lane boundaries (named, not analysed here)

Dependencies this layout has on the production-pipeline lane: per-mode adapters for `image_to_video`, `first_last`, `first_middle_last`, `extend` (all `adapter: ""` in `SHOT_MODES`); the AD-5 server-side batch endpoint and Replace Existing semantics; the AD-1 `render-status` polling endpoint for live clip states; take serving/proxying for the Monitor; the section/BPM analyser for the SECTIONS lane and snap-to-music editing; assembly (FR-22) for anything the Monitor plays as "the video so far." Where any of these is absent, the layout's obligation is to render the absence honestly (disabled with a reason, empty with a sentence), in the idiom the codebase already enforces everywhere else.
