# Project Context

Music Video Producer is a local-first studio that turns a song into an AI music video: a Python 3.11+/FastAPI backend serving a dependency-free ES-module frontend, with `data/projects/<id>/project.json` as the recoverable source of truth for every creative document, shot, job, and output, rendering through a portable ComfyUI that the user starts and stops and this application never touches.

This file exists so that agent activation resolves a `project-context.md` glob to something useful. It is a pointer document. It deliberately does not restate `AGENTS.md` or `docs/BUILD-HANDOFF.md`.

## Read these, in this order

1. **`AGENTS.md`** — operations and policy: what must never be started or killed, where the payload builders live, the verify commands, the conventions that differ from defaults, the known pitfalls. Read it first, always.
2. **`docs/BUILD-HANDOFF.md`** — planning state, measured caveats, process traps, and the standing design laws. Required before any Epic 8-17 work (Shot Effects and Transitions, Treatment Planning); it also names the build order, which is not the epic order.

Then, by need:

- `docs/ARCHITECTURE.md` — product boundary and the shape of the application.
- `docs/DATA-MODEL.md` — every manifest field and what it means; read before adding one.
- `docs/WORKFLOW-MAP.md` — each ComfyUI workflow's models, controls, and readiness; read before touching an adapter.
- `docs/OPERATIONS.md` — start order, isolated-server browser QA, live GPU smokes, recovery.
- `docs/LLM-DIRECTOR.md` — the Director's tools, schemas, and prompt construction.
- `docs/ROADMAP.md` — verified-vs-unverified feature status, and the authoritative current test count.
- `docs/DEVELOPMENT-LOG.md` — chronological record of what landed and why. Large; search it, do not read it.
- `docs/measurements/` — self-contained evidence bundles from live runs, each with an `index.html` and the stills it is arguing from.
- `README.md` — what the product does today and its honest-status section.

## Non-negotiables

Each of these points at the document that owns it. None of them is restated here.

1. Never start, stop, or interrupt ComfyUI, and never submit a video render without explicit confirmation (AGENTS.md).
2. Every new manifest field needs an `_adopt_*` guard in `replace_project` plus its test in the same commit (AGENTS.md; BUILD-HANDOFF §4).
3. Every new Director tool schema goes through `_promoted()` in `director.py`; an optional field and a field the model silently dropped are the same bytes (BUILD-HANDOFF §3).
4. H3 render windows land on the 17k+5 frame grid via `align_h3_frames()`, and the assembled video matches the song within one frame (AGENTS.md; BUILD-HANDOFF §7.1).
5. Derived state is computed at read time, never stored as a flag that can outlive its condition (BUILD-HANDOFF §7.5).
6. Stage explicit paths when committing — other agents work in this tree concurrently and blanket `git add` has swept in their work (BUILD-HANDOFF §4).
7. Re-run any constraint written more than about a week ago before planning around it; three Treatment premises were already stale at eight days (BUILD-HANDOFF §4).
8. Report only what was actually verified; write "not verified" rather than implying something works (AGENTS.md; README honest status).

## Facts established by measurement

Measured 2026-08-24. These are not in `AGENTS.md` and cannot be re-derived by reasoning.

**Song analysis is cheap enough to be synchronous.** A 3-minute song analyses in 185 ms through the shipped `audio.analyze_song` at 22.05 kHz mono, 30 Hz, 8 bands, N=2048, 5400 frames. That is cheaper than the `ffprobe` call already in `upload_song`. Consequence: no background job lane is needed for song analysis, and this repo has none by deliberate choice (`app.py:10748`).

**The Song Envelope is a few times the manifest.** A 3-minute Song Envelope measured through the shipped extractor is **405 KB** of JSON, against a 110-190 KB manifest that rides a 2-second poll — so the AD-20 sidecar decision stands on a 2-4x ratio. Two earlier figures were wrong in opposite directions and should not be requoted: AD-20 estimated 750 KB, and a synthetic probe on 2026-08-24 projected 1.13 MB by filling more arrays with random floats than the real envelope carries. 405 KB is the measured one.

**BPM from autocorrelation is lag-quantized.** At a 30 Hz analysis rate, autocorrelation BPM estimation is quantized by integer lag: measured 90.0 / 128.6 / 138.5 against true 90 / 128 / 140. Resolution near 140 BPM is about +/-1.2 BPM. Parabolic interpolation on the autocorrelation peak roughly halves the error but does not remove it — measured 90.0 / 128.4 / 139.0 against the same three tempos. Higher precision needs a higher analysis rate, not a better peak-picker.

**Director-context field classification is an import-time gate.** `app.py:761` runs it at IMPORT TIME: a field on `Song` or `Shot` in neither the `*_DIRECTOR_VISIBLE` nor the `*_DIRECTOR_WITHHELD` frozenset raises `RuntimeError` and the application refuses to start. Raw-measurement fields go withheld, per the `vocal_spans`/`lyric_words` precedent.

**Baseline as of 2026-08-24.** `uv run pytest -q` = 1916 passed in ~121 s. `uv run ruff check .` clean. `node --check` clean on both `web/assets/app.js` and `web/assets/api.js`.

---

This file is a pointer document. Where it overlaps `AGENTS.md` or `docs/BUILD-HANDOFF.md`, those two are authoritative. Any constraint recorded here that is older than about a week should be re-run before it is planned around.
