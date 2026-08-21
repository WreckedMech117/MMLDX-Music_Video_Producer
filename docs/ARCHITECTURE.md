# Architecture

## Product boundary

Music Video Producer is a standalone application rooted at `F:\MusicVideoProducer`. Agent OS is unrelated and must not be imported, embedded, or used as a storage/runtime host.

## Runtime components

```text
Browser editor :8765
        │ REST
        ▼
FastAPI application
 ├─ ProjectStore ── data/projects/<id>/project.json
 ├─ DirectorClient ── configurable OpenAI-compatible LLM
 ├─ WorkflowCatalog ── saved creator/editor workflows
 ├─ Payload factories ── versioned API-format ComfyUI graphs
 └─ ComfyClient ── :8188 /prompt /history /queue /view /upload
                         │
                         ▼
                Portable ComfyUI + RTX 5090
```

## Frontend

The interface is an **Operate / Command-Inspect** editor, not a dashboard. This rejects *decorative* surfaces — landing-page heroes, vanity metrics — and it does not reject *sequencing*. A Production Wizard is therefore compatible with this decision, provided it composes the workspaces below rather than reimplementing them: each wizard step presents the real workspace, scoped to that step, and the current step is derived from project state rather than stored. See the PRD (§4.1) for the requirements built on this reconciliation.

The workspaces:

- Top bar: project, transport, ComfyUI state, save.
- Left rail: Song, Treatment, Assets, Timeline, Queue.
- Song: import and Music 3 creation share equal status; one persistent media element drives native, global, waveform, and timeline transport state.
- Treatment: natural-language direction plus editable structured documents.
- Assets: Flux generator, media library, asset inspector, Krea multiview promotion.
- Timeline: waveform, direct-manipulation shot clips, references, shot inspector.
- Queue: persisted generation jobs and finishing-route context.

The frontend uses native ES modules and the Web Audio/Canvas APIs. It has no CDN or cloud dependency.

## Backend modules

| Module | Responsibility |
|---|---|
| `config.py` | Environment-driven standalone paths and service URLs |
| `models.py` | Pydantic production manifest models |
| `store.py` | Atomic JSON persistence and media directories |
| `comfy.py` | Bounded REST client, uploads, prompt submission, queue/history parsing, live queue lookup; plus the in-memory render-progress listener (a minimal WebSocket client, a parser for ComfyUI's `progress_state`/`progress` messages, and a bounded `prompt_id → percent` map) |
| `workflows.py` | Explicit Flux, Music 3, Krea, and text-only H3 payloads; audited LTX boundary patch |
| `director.py` | OpenAI-compatible structured treatment planner; degraded-output rejection |
| `timeline.py` | Shot validation, H3 frame alignment, Director timeline conversion |
| `app.py` | FastAPI routes, orchestration, static UI |

## Decisions

1. **Explicit workflow adapters.** Saved editor JSON is not executable by `/prompt`. The app builds known API-format graphs instead of attempting a generic lossy conversion.
2. **Project manifests own provenance.** Output filenames alone do not define job state.
3. **LLM output remains editable.** It creates project records; it does not queue expensive video renders automatically.
4. **Full songs are shot sequences.** H3 generation remains within reliable 4–15 second windows.
5. **ComfyUI ownership is respected.** The application checks and uses the configured server but never launches, restarts, interrupts, or kills it.
6. **License boundary.** The GPL Director extension remains an installed ComfyUI component. This original UI uses compatible data and HTTP/node interfaces without copying its frontend source.
7. **Reference exports are immutable evidence.** Audited copies live under `workflow_templates/reference_exports`; runtime code never submits them blindly.
8. **Unvalidated model output never replaces creative work.** `director.document_rejection()` refuses a Treatment or Style Bible that parses as JSON, or that collapses below 40% of the document it would replace. Added after the failure was observed live: once a Style Bible had been corrupted into JSON, the model saw JSON in the project context and kept returning JSON, so the corruption was self-reinforcing.

9. **Refused model output is retained for inspection and withheld from the model.** A refusal is carried as structured data on the message — the sentence in `notices[].text`, the offending output in `notices[].raw` — and `DIRECTOR_CONTEXT_EXCLUDE` strips the whole `notices` list from the dump sent to the Director. The Director can see exactly what was refused; the model never sees it again. This is the direct consequence of invariant 8: a guard that persisted the degraded output into the thread would be the thing feeding the loop it exists to break.
9. **Render state is read from the queue, not only from history.** ComfyUI writes no history entry until a prompt finishes, so history alone cannot distinguish an executing render from a waiting one. `ComfyClient.queue_state()` resolves it.
10. **Frame counts cross the workflow boundary already aligned.** `timeline.align_h3_frames()` rounds to MiniMax H3's 17k+5 grid, and the payload receives the aligned count. A rendered clip may therefore be marginally longer than its shot window; trimming belongs to assembly.
11. **Live render progress is read on a backend socket, delivered on the existing poll, and never persisted.** AD-1 fixed the transport: the browser polls `GET /api/projects/{id}/render-status` every 2 s *while and only while* a render is open, and an idle project makes no request at all. Percentages ride that same answer rather than a route, a timer or a browser WebSocket of their own — `comfy.ComfyProgressListener` holds one socket to ComfyUI's `/ws` in the server process, `comfy.ProgressTracker` holds the resulting `prompt_id → percent` map in memory, and `batch.render_status_report` merges it into the report it already builds. **Nothing about a percentage is written to the manifest.** Two reasons, both load-bearing: `store.save` moves `Project.updated_at`, which `PUT /api/projects/{id}` *compares*, so a number that changes several times a second would collide with every optimistic-concurrency check the Director's own edits ride on; and a percentage is derived state that is stale the instant it is read, which this codebase computes rather than stores (see `batch.readiness_report`). `RenderJob.progress` stays what it was — the local ffmpeg export's own clock (AD-9), where the record is the only witness. The socket is a pure enhancement: it is never awaited by a submission and never consulted by the reconciler, so a refused, dropped or unrecognised socket costs the percentage and nothing else. Attribution is by `prompt_id` alone, and submissions deliberately send no `client_id`, which is what makes ComfyUI broadcast these messages to every listener — this application's and the Director's own ComfyUI tab alike.

## Security and recovery

- API keys live only in environment variables or ignored `.env`.
- Uploaded filenames are sanitized.
- Media reads are resolved beneath each project's media root to block path traversal.
- Project writes use temp-file + atomic replace.
- Every submitted render stores its prompt ID, seed, target and status.
