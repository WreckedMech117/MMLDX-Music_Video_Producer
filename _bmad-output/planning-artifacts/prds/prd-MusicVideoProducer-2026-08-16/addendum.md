---
title: "Addendum: Music Video Producer PRD"
status: draft
created: 2026-08-16
updated: 2026-08-16
---

# Addendum

Technical depth for architecture and implementation. Not requirements — evidence and mechanism behind them.

## Director defect — full diagnosis

**Symptom reported:** the Director errors when used.

**What was actually found**, tested live against LM Studio on 2026-08-16:

1. The `DirectorClient` works. A direct `plan()` call with thin context returned a valid result, and `tests/smoke_director_lmstudio.py` passes.
2. The schema is not the cause. Three trials with the current Pydantic schema (which uses `$defs`/`$ref` and sets no `additionalProperties`) scored 3/3 clean; three trials with a flattened, strict-conformant schema also scored 3/3. The obvious suspect is innocent.
3. **With full Project context, `style_bible` comes back as serialized JSON rather than prose** — `[{"style":"moody","color_palette":[...` — reproduced on three consecutive requests through the running application. One earlier observation returned the truncated fragment `:[{`.
4. In the same failure mode, `shots` came back empty while the assistant prose described "a four-beat sequence." `apply_shots: true` therefore applied nothing, silently.

**The application-level defect**, which is separable and more serious than the model behaviour:

```python
project.treatment = result.treatment
project.style_bible = result.style_bible
```

Both assignments are unconditional and unvalidated. A single Director call overwrites existing creative documents with whatever came back, and there is no undo. This is why FR-15 and FR-16 are written as data-protection requirements rather than as LLM-tuning requirements — the application must be safe regardless of what the model returns.

**Not yet established:** why rich context degrades the output. Candidates are context length, the shape of the serialized Project payload (which includes Windows paths with backslashes and nested nulls), or the model. The application-level fix does not depend on answering this, and should not wait for it.

**Also observed:** the model proposed a single 20-second Shot for a 20-second request. `PlannedShot` permits up to 30 seconds while H3's reliable window is 4–15, so validation passed something the renderer handles poorly. Hence FR-17.

**Unicode note:** returned text contains characters such as U+2014 and U+2011. Anything printing model output to a Windows console under the default `charmap` codec raises `UnicodeEncodeError`. The application itself writes UTF-8 explicitly and is unaffected; scripts and tests need `PYTHONIOENCODING=utf-8`.

## VRAM contention — measured

| Moment | Free VRAM (of 34.2 GB total) |
|---|---|
| Idle, ComfyUI loaded but no prompt | ~32.3 GB |
| During verified H3 sampling | **1.1 GB** |

The H3 stack consumes essentially all available VRAM. LM Studio holds the Director and vision models in the same memory, outside ComfyUI's control and invisible to it. On this hardware the margin is roughly 1 GB — a resident 9B language model does not fit inside it.

Design implications for architecture:

- Unload before render, not after failure. An out-of-memory failure mid-batch wastes the whole queue.
- The application must not assume the unload worked. Read free VRAM back from ComfyUI's `/system_stats` and report what it actually sees.
- Vision inspection and rendering are mutually exclusive on this hardware. Sequencing them matters; running them concurrently is not an option.
- This is a coordination problem between two systems neither of which owns the other. It belongs in the application, because the application is the only component that knows both exist.

## SageAttention

`build_h3_reference_payload()` pins `PathchSageAttentionKJ` to `sage_attention: "disabled"`, and `docs/OPERATIONS.md` says to keep it bypassed "unless a compatible `sageattention` installation has been verified."

**Checked live on 2026-08-16.** In `ComfyUI_windows_portable/python_embeded`:

| Package | State |
|---|---|
| `sageattention` | **not installed** |
| `triton` | installed |
| `torch` | 2.7.0+cu128 |

So the pin is correct as it stands — enabling any non-`disabled` option today would fail at runtime, which is almost certainly the original error. The live node offers `disabled`, `auto`, four `sageattn_qk_int8_*` variants, and two `sageattn3` variants.

The remaining work is a spike, not a design question:

1. Find a `sageattention` build compatible with torch 2.7.0+cu128 on Blackwell (RTX 5090, sm_120). `sageattn3` variants specifically target Blackwell.
2. Install into the embedded Python — this modifies the user's ComfyUI installation and needs their consent.
3. Expose the mode as a setting rather than a hardcoded literal.
4. Measure one identical render with it `disabled` and enabled.

Only step 4 tells you whether any of this is worth keeping. Until then the pin stays.

## Song workflow variants

The Director has split the SongPlanner workflow. Present on disk:

- `SongPlanner + MiniMax Music 3 - Quality BF16.json` — lyrics invented by the model
- `SongPlanner + MiniMax Music 3 - Quality BF16-Known_Lyrics.json` — lyrics supplied, e.g. a cover
- `SongPlanner + MiniMax Music 3 - Balanced Official.json`, `... - Low VRAM INT8.json` — quality/VRAM tiers

The application's current `build_music3_payload()` implements a direct Music 3 payload and covers no SongPlanner variant. `docs/WORKFLOW-MAP.md` is accurate about this and does not overclaim. Two adapters are needed, and they should share everything except lyric handling.

The quality tiers are a separate axis from the lyric axis. Whether to expose them is an open product question, not a requirement.

## Live timeline population — mechanism notes

FR-7 and FR-8 are the highest-value interaction in the product and the most architecturally demanding, because the current job model is pull-based: the client asks about a job, the server reads ComfyUI's history, and history has no entry until a prompt finishes.

Constraints this creates:

- Per-Shot completion must be observable while the Batch is still running. ComfyUI exposes a WebSocket carrying execution events; `docs/ARCHITECTURE.md` already anticipates "REST/WebSocket-compatible polling."
- Whatever the transport, FR-6 (truthful running-vs-queued state) is a prerequisite. There is no live timeline without live state.
- Flagging during an active Batch must not mutate anything the Batch is reading. Flag state belongs on the Shot and must be independent of render state.

`[NOTE FOR PM]` Whether this is polling `/queue` and `/history` on an interval or subscribing to ComfyUI's WebSocket is an architecture decision, deliberately left open here.

## Brownfield defect register

Observed and reproduced during this documentation pass. Each is small; together they are the difference between a demo and a tool.

Observed and reproduced during this documentation pass, then fixed in the same session.

| # | Defect | Evidence | Severity | Status |
|---|---|---|---|---|
| 1 | Creative documents overwritten by unvalidated model output | Reproduced 3/3 | Data loss | **Fixed** — `document_rejection()`; guard verified firing 2/2 live |
| 2 | Executing renders report `queued` | Observed across a 12-minute render | User-facing honesty | **Fixed** — `ComfyClient.queue_state()`; verified live |
| 3 | `apply_shots` silently applies nothing when the model returns an empty list | Observed | Silent failure | **Fixed** — reported as a notice |
| 4 | `DirectorTimeline.aligned_frames` computed but never sent | Code inspection; only on-grid windows verified | Latent | **Fixed** — payload now receives aligned frames |
| 5 | Stored output paths mix `\` and `/` | Observed; ComfyUI `/view` tolerates it | Cosmetic | **Fixed** — separators normalised |
| 6 | Planned Shots may exceed H3's reliable window | Observed a 20-second proposal | Quality | **Fixed** — flagged against a 4–15 s bound |
| 7 | `ruff check .` failed on 39 errors in vendored agent tooling | Observed; gate documented in `README.md` | Broken gate | **Fixed** — `extend-exclude` in `pyproject.toml` |

### Root cause of defect 1 — worth recording

The corruption was **self-reinforcing**. The application sends the whole Project as context. Once a Style Bible had been written as JSON, the model saw JSON in that field and returned JSON again, so a single bad response poisoned every subsequent one. Live evidence: with a prose Style Bible in context, three consecutive calls returned clean prose; with a JSON Style Bible in context, two consecutive calls returned JSON and the new guard rejected both.

This is why the fix belongs at the persistence boundary rather than in prompt tuning. A better prompt reduces the chance of the first bad response; only a guard stops the first one from becoming permanent.

Note the guard prevents corruption but cannot repair it. A Style Bible already corrupted needs one manual edit to break the loop — as was required for the validation project during this session.
