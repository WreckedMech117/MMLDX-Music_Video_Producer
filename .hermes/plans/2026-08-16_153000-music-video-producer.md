# Music Video Producer Implementation Plan

> **For Hermes:** Implement this plan task-by-task with test-first vertical slices. Do not couple this application to Agent OS.

**Goal:** Build a standalone local-first music-video production studio in `F:\MusicVideoProducer` that creates or imports songs, develops treatments conversationally, generates character/setting assets through ComfyUI, turns characters into multiview sheets, plans Director-compatible shots, queues renders, and manages the resulting project media.

**Architecture:** A standalone FastAPI service owns project manifests, uploads, workflow adapters, LLM planning, and ComfyUI job orchestration. A purpose-built browser UI provides Song, Treatment, Assets, Timeline, and Render surfaces. ComfyUI remains the rendering backend at a configurable URL (default `http://127.0.0.1:8188`); no Agent OS imports, data, routes, or runtime dependencies are allowed.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, httpx, pytest, vanilla TypeScript-free ES modules, semantic HTML, modern CSS, Canvas/Web Audio API, ComfyUI REST/WebSocket-compatible polling, JSON project manifests.

---

## Product boundaries

- Standalone root: `F:\MusicVideoProducer`.
- Song workspace supports both upload and MiniMax Music 3 generation.
- Asset library supports uploads, Flux image generation, character/setting classification, and Krea multiview promotion.
- Timeline stores song-level shots and converts selected windows into MiniMax H3 Director timeline JSON.
- Full songs are orchestrated as multiple 4–15 second shots; do not use the withdrawn H3 Director Chain node.
- LLM interaction uses a configurable OpenAI-compatible endpoint. Missing LLM configuration must be reported honestly, never replaced by fake AI output.
- Original ComfyUI workflow JSON files stay in their existing locations and are treated as source references.
- The UI is an **Operate / Command-Inspect** surface: waveform, timeline, library, inspector, and queue dominate. No landing-page hero or decorative dashboard metrics.

## Phase 1 — Foundation and persistence

### Task 1: Create package and test skeleton

**Files:**
- Create: `pyproject.toml`
- Create: `src/music_video_producer/__init__.py`
- Create: `src/music_video_producer/config.py`
- Create: `tests/test_config.py`

**TDD:**
1. Test default ComfyUI URL, data root, and workflow paths.
2. Run the test and confirm import failure.
3. Implement immutable settings with environment overrides.
4. Run the focused test, then the complete suite.

### Task 2: Define project manifest models

**Files:**
- Create: `src/music_video_producer/models.py`
- Create: `tests/test_models.py`

**TDD:**
1. Test project creation, song source modes, typed assets, shot validation, and serialisation.
2. Confirm failures before implementation.
3. Implement Pydantic models for Project, Song, Asset, Shot, RenderJob, TreatmentMessage, and WorkflowRef.
4. Verify invalid negative durations and unsupported asset kinds are rejected.

### Task 3: Implement filesystem project store

**Files:**
- Create: `src/music_video_producer/store.py`
- Create: `tests/test_store.py`

**TDD:**
1. Test create/list/get/update persistence in a temporary root.
2. Test duplicate-safe IDs and missing-project errors.
3. Implement atomic JSON writes and per-project media directories.
4. Verify manifests survive a new store instance.

## Phase 2 — ComfyUI adapters

### Task 4: Implement ComfyUI client

**Files:**
- Create: `src/music_video_producer/comfy.py`
- Create: `tests/test_comfy.py`

**TDD:**
1. Test health, prompt submission, history lookup, queue lookup, upload, output URL construction, and error translation with `httpx.MockTransport`.
2. Confirm each test fails for the missing client behavior.
3. Implement an async client with bounded timeouts.
4. Verify no user-owned ComfyUI process is started or stopped by the app.

### Task 5: Build workflow catalog and payload factories

**Files:**
- Create: `src/music_video_producer/workflows.py`
- Create: `tests/test_workflows.py`
- Create: `docs/WORKFLOW-MAP.md`

**TDD:**
1. Test discovery of the saved `Flux-Image-Gen.json`, Music 3 workflows, Krea multiview workflow, H3 Director workflow, and LTX enhancement workflows.
2. Test API-format payload factories for Flux image generation, MiniMax Music 3, Krea multiview, and Director prompt compilation.
3. Confirm failures before implementation.
4. Implement explicit, versioned payload factories instead of a lossy general editor-JSON converter.
5. Document node IDs, user-facing controls, model selections, output prefixes, and source workflow paths.

### Task 6: Add generation/job service

**Files:**
- Create: `src/music_video_producer/jobs.py`
- Create: `tests/test_jobs.py`

**TDD:**
1. Test Flux, Music 3, multiview, H3 shot, and LTX job submission against a fake Comfy client.
2. Test that prompt IDs, seeds, workflow version, and project IDs are persisted.
3. Implement service methods and job reconciliation.

## Phase 3 — LLM treatment and Director timeline

### Task 7: Implement configurable LLM director

**Files:**
- Create: `src/music_video_producer/director.py`
- Create: `tests/test_director.py`
- Create: `docs/LLM-DIRECTOR.md`

**TDD:**
1. Test OpenAI-compatible request construction and structured response validation.
2. Test explicit 503-style unavailable state when no endpoint/model is configured.
3. Test conversion of a treatment into editable shots without overwriting user-locked fields.
4. Implement treatment chat and schema-constrained plan generation.

### Task 8: Implement Director timeline conversion

**Files:**
- Create: `src/music_video_producer/timeline.py`
- Create: `tests/test_timeline.py`

**TDD:**
1. Test shot sorting, overlap detection, frame conversion at 24 fps, H3 17k+5 alignment, 4–15 second warnings, and `timeline_data` JSON generation.
2. Test song-level timelines split into independent shot windows.
3. Implement pure conversion functions.
4. Verify against the installed `/minimax_director/compile_prompt` endpoint.

## Phase 4 — FastAPI application

### Task 9: Expose project, media, workflow, generation, timeline, and job APIs

**Files:**
- Create: `src/music_video_producer/app.py`
- Create: `tests/test_api.py`

**TDD:**
1. Add one route at a time: health, projects, uploads, workflow catalog, Flux generation, Music generation, multiview promotion, treatment chat, timeline compile, job status.
2. Confirm each route fails before implementation.
3. Implement with dependency injection for store and Comfy client.
4. Test validation and downstream failures.

### Task 10: Add local launcher

**Files:**
- Create: `run.py`
- Create: `start-music-video-producer.bat`
- Create: `.env.example`
- Create: `.gitignore`

**Validation:**
- Start on `127.0.0.1:8765`.
- Verify `/api/health` and `/docs`.
- Do not alter port 8188 ownership.

## Phase 5 — Production UI

### Task 11: Build application shell and project navigation

**Files:**
- Create: `src/music_video_producer/web/index.html`
- Create: `src/music_video_producer/web/assets/styles.css`
- Create: `src/music_video_producer/web/assets/app.js`
- Create: `src/music_video_producer/web/assets/api.js`
- Create: `src/music_video_producer/web/assets/state.js`

**Acceptance:**
- Original cinematic dark design.
- Persistent sidebar, top transport/status bar, central workspace, contextual inspector.
- Keyboard-visible focus states and 44px primary hit targets.
- Responsive fallback without pretending mobile is a full editing workstation.

### Task 12: Build Song workspace

**Files:**
- Modify: `index.html`, `styles.css`, `app.js`

**Acceptance:**
- Import WAV/FLAC/MP3.
- Create songs through Music 3 fields for concept, caption/style, lyrics, duration, and seed.
- Real waveform rendered with Web Audio API after local upload.
- Song duration drives the timeline scale.

### Task 13: Build conversational Treatment workspace

**Acceptance:**
- Chat thread, creative brief, treatment preview, style bible, narrative arc, constraints, and “Apply as editable shot plan.”
- Honest unavailable state if no LLM endpoint is configured.
- Never silently invent a successful LLM call.

### Task 14: Build Asset Library

**Acceptance:**
- Upload and classify character, setting, prop, style, image, audio, and video assets.
- Flux prompt form with aspect ratio, seed, steps, guidance, and count.
- Generated settings/characters flow into the same library.
- Character cards include “Create multiview sheet,” which uses the installed Krea workflow and links the result back to the source character.
- Assets can be attached to shots by drag/drop or inspector controls.

### Task 15: Build music-aware timeline and shot inspector

**Acceptance:**
- Waveform lane, song-section lane, shot lane, references lane, lyrics/notes lane, and render status.
- Add, select, drag, resize, split, duplicate, delete, and lock shots.
- Conventional transport controls and zoom.
- Direct numeric start/duration controls in the inspector.
- Prompt, workflow mode, references, seed, render variant, and status controls.

### Task 16: Build render queue and output review

**Acceptance:**
- Queue individual shots, selected shots, or all stale shots.
- Poll actual Comfy history.
- Show queued/running/completed/error states and exact error messages.
- Review variants and mark one approved.
- Provide H3 → LTX 2.5 → SeedVR2/RTX/FILM stage selection without forcing every stage.

## Phase 6 — Verification and documentation

### Task 17: End-to-end vertical-slice verification

1. Start the app.
2. Create a project.
3. Import a real song and verify waveform/duration.
4. Generate one real Flux setting image.
5. Add it to the asset library.
6. Create one character image and promote it to a Krea multiview sheet.
7. Create a three-shot timeline.
8. Compile one shot through the real Director endpoint.
9. Queue a low-cost H3 test shot.
10. Verify output metadata and UI status.

### Task 18: Final documentation

**Files:**
- Create: `README.md`
- Create: `docs/ARCHITECTURE.md`
- Create: `docs/PROJECT-MANIFEST.md`
- Create: `docs/OPERATIONS.md`
- Create: `docs/ROADMAP.md`

Document setup, launcher, project storage, ComfyUI expectations, workflow mappings, LLM configuration, recovery, backups, license boundaries, current capabilities, and later features.

## Risks and tradeoffs

- ComfyUI editor workflows are not directly executable API payloads; adapters must be explicit and tested.
- The H3 Director frontend is GPL-3.0. Use its installed HTTP/node interface and compatible data schema; do not copy its frontend source into this application.
- Full-song generation must remain shot-based because H3 quality degrades outside the trained 4–15 second range.
- Audio analysis beyond duration/waveform (BPM, sections, lyrics alignment) will be implemented as a later isolated analysis service unless a dependable local library is added and verified.
- Large media stays local. Cloudflare/Supabase can be optional future collaboration layers, not a prerequisite.
- The application must never kill or restart a user-owned ComfyUI process automatically.

## Verification commands

```bash
cd F:\MusicVideoProducer
uv sync
uv run pytest -q
uv run ruff check .
uv run python run.py
curl http://127.0.0.1:8765/api/health
```

Browser verification:
- Open `http://127.0.0.1:8765`.
- Check console for errors.
- Exercise Song, Treatment, Assets, Timeline, and Queue paths.
- Verify at 1440×900 and 1920×1080.
- Run a slop audit: target 0/10, with no wrong-surface, hero, feature-grid, generic indigo, glassmorphism, or fake metrics tells.
