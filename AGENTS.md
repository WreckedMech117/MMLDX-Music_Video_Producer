<!-- bmad:context -->
<!-- Verified 2026-08-16 against 3325eee. Managed by bmad-project-context; edits inside this block are replaced on refresh. Keep anything you want preserved outside the markers. -->

## Music Video Producer

Local-first studio that turns a song into an AI music video through a user-managed portable ComfyUI. Python 3.11+/FastAPI backend, dependency-free ES modules frontend, JSON project manifests as the source of truth. Technical documentation lives in `docs/`; the original implementation plan in `.hermes/plans/`.

## Policy

- Never start, stop, restart, interrupt, or kill ComfyUI — it is user-managed. Check `/system_stats`; if it is down, report that and stop.
- Never submit an H3, LTX, or other video render without explicit confirmation — each costs real GPU minutes on the user's hardware.
- Never add Agent OS imports, routes, data paths, or runtime dependencies; this application is deliberately standalone.
- Never copy the GPL Director extension's frontend source — use its HTTP/node interfaces and data formats only.
- Never edit or directly submit files under `workflow_templates/reference_exports/` — they are immutable audited evidence.

## Where things are

- ComfyUI payload builders: `src/music_video_producer/workflows.py`
- Adding or changing an adapter? Read `docs/WORKFLOW-MAP.md` first — it records each workflow's models, controls, and readiness.
- Verified-vs-unverified feature status: `docs/ROADMAP.md`. Update it whenever a live run changes readiness.

## Running and verifying

- Prefix every Python command with `uv run`; bare `pytest` and `ruff` run outside the project environment.
- `node --check src/music_video_producer/web/assets/app.js` is a required gate and appears in no config file.
- Browser QA is a release gate, not optional; it needs the app on an isolated port with an empty data root — see `docs/OPERATIONS.md`.
- ComfyUI must already be running at `MVP_COMFY_URL` before any live render or `/object_info` check.

## Conventions that differ from defaults

- Shot timing is seconds against the master song; convert to frames only at the workflow boundary (`timeline.py`).
- H3 render windows must land on the 17k+5 frame grid — use `align_h3_frames()`, never a raw frame count.
- `latest_output` is the most recent take; `approved_output` is an explicit editorial decision. Never write approval from job completion.

## Known pitfalls

- Never submit saved ComfyUI editor JSON to `/prompt` — it is not API format. Build an explicit payload in `workflows.py` instead.
- Report only what was actually verified; write "not verified" rather than implying a feature works. The honest-status convention in `README.md` and `docs/ROADMAP.md` depends on it.
- Edit `.env`, never `.env.example` — changing the example alone has no runtime effect.
- Read `/object_info` combo options from `[1]["options"]`; ComfyUI 0.33.1 moved them from `[0]`, and the old shape silently reports every model as missing.
- Job refresh reads `/history` only, so an executing render reports `queued`, not `running`. Check `/queue` to tell them apart.

<!-- /bmad:context -->

## Start here for build work

Read **`docs/BUILD-HANDOFF.md`** before starting on Shot Effects and Transitions (Epics 8-11) or Treatment Planning (Epics 12-17). It carries the planning state, the measured facts that must not be re-derived by reasoning, and the process traps -- concurrent agents, the recurring `replace_project` guard hole, and the fact that this repo invalidates its own planning artifacts within about a week.

*(Outside the managed block above, so a `bmad-project-context` refresh does not remove it.)*

