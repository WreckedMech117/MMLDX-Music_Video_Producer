# H3 attention backend: a specified experiment

**Status as first written (2026-08-21, §1–§5): SPECIFIED, NOT RUN, no adapter changed.**
**Status now (2026-08-21, §6 onward): BUILT AND RUNNING.** The capability landed, the default
is digest-proved unchanged, the pre-flight covers every profile, and the harness is
`tests/measure_h3_attention.py`. **§6 corrects the premise §1–§5 were written on**, so read §6
before acting on anything above it.
**Status (2026-08-23, §9): DIAGNOSIS, nothing run.** §9 answers the "unload models between
generations" question from ComfyUI's source and its own log: the cost variance is entirely in the
sampler, not in model loading, and it is VRAM oversubscription by other processes on the card.
`empty_cache()` is already called on the `/free` path and returns 25 MiB. **§9 supersedes the
model-load reading of §1's cliff**, and §9.9 specifies the unrun experiment.

This document exists because a render-cost cliff was measured on a pipeline whose attention
acceleration has never once been switched on, and because the one node that would switch it on is
set, in every H3 graph this application emits, to a value that makes it a literal no-op. That is
not a bug — it is faithful to the Director's audited export — but it means the cliff was measured
on the unaccelerated path, and attention cost is exactly the term that explains a cliff.

**Nothing here may be applied without measuring it.** An attention backend swap alters every
render this project makes. The LTX enhancer is the precedent: a graph change silently altered a
performance, and nobody knew until it was looked for. This project's whole premise is H3's
audio-driven lip sync, which is a *performance*, and a change to the attention implementation is
a change to the numerics that produce it.

---

## 1. What was actually measured, and how badly

### The claim that started this

`app.POPULATE_MAX_WINDOW_SECONDS = 6.0` was justified, in part, by a comment asserting that
221-frame windows "took 2.2 HOURS each on this card (2026-08-19)". That figure had **no primary
record anywhere in the repository**. It appeared only as a code comment in `app.py`, and was
quoted onward into `tests/test_api.py`, `tests/test_populate_steps.py` and one
`docs/DEVELOPMENT-LOG.md` line — each citing the comment rather than a measurement. The
application had never recorded how long a render takes.

### The table

Reconstructed on 2026-08-21 from the mtimes of the `.mp4` files in
`J:/Hermes-Remote/comfyui/ComfyUI_windows_portable/ComfyUI/output/music-video-producer/project_59f14d19ff10/shots`,
taking the gaps between consecutive `*h3-reference*_00001-audio.mp4` on the serial overnight
batch of 2026-08-19/20:

| frames | window | n | median | sec/frame |
|---|---|---|---|---|
| 107 | ≤3.3 s | 7 | 4.6 min | 2.58 |
| 124 | ~4.5 s | 4 | 5.4 min | 2.61 |
| 141 | ~5.1 s | 14 | 6.3 min | 2.68 |
| 158 | ~5.8 s | 5 | 7.6 min | 2.89 |
| 226 | 8.75 s | 1 | 30.2 min | 8.02 |
| 277 | 10.38 s | 1 | 39.1 min | 8.47 |

**A cliff is real; 2.2 hours is not.** The measured worst case is 39 minutes — the claim is wrong
by roughly 3.4×. Per-frame cost is flat at 2.6–2.9 s/frame out to 158 frames, then triples to
~8 s/frame at 226+.

### Three caveats, carried everywhere the table is

1. **n=1 at both cliff points.** A single sample is exactly what produced the 2.2-hour claim.
2. **The acceleration was never on.** See §2.
3. **These came from file mtimes, not instrumentation.** They are gaps between consecutive
   output files on a serial batch, so each includes whatever sat between two renders.

**Two outliers, named rather than smoothed away:** one 141-frame render at 26.7 min and one
158-frame at 9.5 min sit outside the pattern. Both are most likely machine contention. Neither
is evidence of anything and neither was averaged into the medians above as though it were.

### Why the cliff looks like spilling rather than compute

158 → 226 frames is a **1.43×** increase in sequence length. Attention's quadratic term alone
predicts about **2.05×**. The observed increase is **3.0×**. Something superlinear beyond
attention's own arithmetic is happening at that step — the shape of a working set no longer
fitting, not the shape of more matrix multiplies. That is a hypothesis, not a finding; §4 says
what would test it.

### What now records this without anyone reading mtimes

Since 2026-08-21 every job settle path writes `RenderJob.updated_at`, `render_seconds`,
`render_seconds_source` and `render_frames`. Where ComfyUI reports its own execution clock the
render is timed with the queue wait excluded; where it does not, the record's enqueue-to-settle
span is used and is labelled as an upper bound. **The next version of the table above is
measured, not reconstructed** — which is the precondition for every experiment below.

---

## 2. What the shipped graphs actually emit

`workflows.py` emits, at three sites, a `PathchSageAttentionKJ` node:

```python
"mvp:attention": {"class_type": "PathchSageAttentionKJ",
                  "inputs": {"model": ..., "sage_attention": "disabled", "allow_compile": False}}
```

**This node is a no-op as configured.** KJNodes' own implementation
(`ComfyUI-KJNodes/nodes/model_optimization_nodes.py:118-120`) is:

```python
def patch(self, model, sage_attention, allow_compile=False):
    if sage_attention == "disabled":
        return model,
```

It returns the model untouched. **Every H3 render this project has ever timed ran with
SageAttention disabled and compilation off.** That is deliberate and documented: the `#:` comment
at `workflows.py:513-514` says the sigma shift (12/3) "and disabled sage attention are what this
adapter already emits", faithful to the Director's 2026-08-17 export.

**A correction to the premise this task was given.** The three `mvp:attention` sites are *not*
the three video builders. Verified by reading the file:

| site | builder | model chain around it |
|---|---|---|
| `workflows.py:1001` | `build_h3_reference_payload` | `mvp:model` → (`mvp:lora`) → `mvp:shift` (12/3) → **`mvp:attention`** → `mvp:preview` → scheduler/guider |
| `workflows.py:1166` | `build_h3_keyframe_payload` | `mvp:model` → `mvp:shift` (12/3) → **`mvp:attention`** → `mvp:preview` → scheduler/guider |
| `workflows.py:1399` | `build_h3_image_edit_payload` | `mvp:model` or `mvp:lora` → **`mvp:attention`** → scheduler/guider (no shift node, no preview) |

`build_h3_director_payload` (the text-only path) emits **no `mvp:attention` node at all**: its
model runs `mvp:model` → `MiniMaxH3DirectorCS` (which carries its own `shift_video: 12,
shift_audio: 4`) → `ModelPreviewOverrideKJ` → scheduler/guider. So a change to the attention
backend would reach the **reference**, **keyframe** and **image-edit** graphs and would leave the
Director graph untouched unless a node were added to it.

The measured table in §1 is from `*h3-reference*` files, i.e. the reference builder — which is
one of the three that does route through `mvp:attention`. Good: the experiment's control and its
treatment are the same graph.

---

## 3. What Comfy Kitchen does differently

Source: `J:/Hermes-Remote/comfyui/workflowsbackup/ComfyKitchen/MiniMax-H3 TXT2VID IMG2VID (Full)- 20260818.json`,
dated 2026-08-18, from <https://www.patreon.com/aifuturetech/posts/minimax-h3-mode-167313564>.
Read as a saved **editor** graph (not API format); node modes are ComfyUI's own (`0` = active,
`4` = bypassed).

**It is one graph with two parallel, alternative sampling branches**, and the SageAttention
branch is the bypassed one:

| node | type | mode | widgets |
|---|---|---|---|
| `6095` | `ModelAttentionBackend` | **0 — active** | `["comfy kitchen attention"]` |
| `223` | `PathchSageAttentionKJ` | **4 — bypassed** | `["auto", false]` |

The shared model chain (`UNETLoader#215` → bypassed turbo LoRA → an empty rgthree Power Lora
Loader) fans out into both. The live branch is `#6095 → MiniMaxH3SigmaShift#6007 (12/3) →
BasicGuider#214 / BasicScheduler#212 → SamplerCustomAdvanced#213`. The bypassed branch is
`#223 → MiniMaxH3SigmaShift#6097 (13.92/3) → … → SamplerCustomAdvanced#6102`, with its own
`VHS_VideoCombine` writing the `minimax-h3/vid-sage` prefix.

So the Comfy Kitchen graph substitutes **a different attention backend in place of Sage**, not
alongside it. That is the whole of the change worth testing.

**Its other settings, reported honestly, because they are not free variables:**

* **30 steps.** Not the `BasicScheduler` widget's stale `8`: the `steps` widget is converted to
  an input, fed from `PrimitiveInt#6109 "Step" = [30, "fixed"]` through a Set/Get pair.
  This project's reference builder defaults to **20** on the `default` profile, **4** on `turbo`
  and **8** on `turbo-references2v`.
* **Sigma shift 12/3** on the live branch, **13.92/3** on the bypassed Sage branch. This
  project's reference and keyframe builders emit **12/3**, matching the live branch.
* **0.6 megapixels**, via `ResolutionSelector#115 = ["16:9 (Widescreen)", 0.6, 32]`. The graph's
  own note maps that to **1056×608**, which is exactly this project's `H3_DEFAULT_MEGAPIXELS`
  0.6 / `H3_DEFAULT_ASPECT_RATIO` "16:9 (Widescreen)" / `H3_DEFAULT_MULTIPLE` 32 and its recorded
  1056×608. **This one variable already matches.**
* `euler` sampler, `beta` scheduler, `BasicGuider` (so **no CFG value exists** — there is no
  `CFGGuider` or `KSampler` anywhere in the graph), `denoise 1`, noise seed 42.
* Checkpoint `minimax_h3_fl2va_int8_convrot.safetensors` — the **first/last** model, and *not*
  the pruned variant this project's keyframe builder loads. As saved, both keyframe feeders are
  bypassed, so **the graph runs T2V**.
* Frame count from `ComfyMathExpression#220`:
  `max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17` with `a = 10` — the same
  17k+5 grid this project's `align_h3_frames` implements, at 24 fps.
* Downstream post-processing (SeedVR2 upscaler, an LTX-2.5 upsampler subgraph, FILM frame
  interpolation, RTX super-resolution) that is **not part of this experiment** and must not be
  imported with it.

**The node is core ComfyUI, and availability is conditional.**
`ModelAttentionBackend` is defined in `comfy_extras/nodes_model_advanced.py:352-382`, not in a
custom-node pack. Its options are `["pytorch attention"]`, plus `"comfy kitchen attention"` **only
when** `comfy.ldm.modules.attention.COMFY_KITCHEN_INT8_ATTENTION_IS_AVAILABLE` — a **runtime**
check. Two consequences that shape the protocol:

* `VALIDATE_INPUTS` returns `True` unconditionally, so an API payload can pass any string and an
  unavailable backend **silently falls back to PyTorch attention with a log warning**. A run that
  "used comfy kitchen attention" and was actually PyTorch is indistinguishable from a real one in
  the output. This is the same failure this codebase already refuses elsewhere — a GPU job logged
  under a configuration that was never applied is worse than a refusal.
* Therefore **`/object_info` must be read for `ModelAttentionBackend`'s option list before any
  measurement**, and the option string must be confirmed present. This is a precondition to
  check, not an assumption to make.

**ComfyUI was updated 0.33.1 → 0.33.3 on 2026-08-21, and is back up.** Everything in §2 and §3
was read off the **pre-update** install (0.33.1, `comfy_kitchen-0.2.31` present in
`python_embeded`). The node and the backend may exist only on the newer build, which makes this
experiment *more* runnable after the update, not less — but nothing here has been re-read against
0.33.3, so **the `ModelAttentionBackend` option list must be confirmed on the live server before
anything is run** (§4, precondition 2). A ComfyUI update can also drift node schemas, and this
project pins its graphs hard against the Director's audited exports; **re-baselining any adapter
against the updated server is a separate, deliberate task and is not part of this experiment.**

What *has* been confirmed on 0.33.3: `/history` carries the per-prompt execution clock this
experiment's timings depend on. One 512×512 4-step Flux still returned
`['execution_start', 'execution_cached', 'execution_success']`, parsed to a 32.431 s span against
a 33.140 s stopwatch. So precondition 3 below is satisfied on the current build.

---

## 4. The experiment

### Preconditions (all must hold before a single render)

1. ComfyUI is back up after the Director's update and the GPU is free.
2. `/object_info` reports `ModelAttentionBackend`, and its `attention` option list **contains the
   exact string** `"comfy kitchen attention"`. If it does not, the experiment stops here and is
   reported as unavailable — never approximated.
3. The render-timing instrumentation shipped 2026-08-21 is in the build under test, so every run
   records its own `render_seconds`, `render_seconds_source` and `render_frames`. **No timing in
   this experiment is taken from a file mtime.** A run whose `render_seconds_source` is not
   `"comfy"` is a run whose number includes queue wait and is not comparable.
4. Renders are submitted **one at a time**, never as a batch, so the queue wait is zero and the
   two timing sources agree.
5. `docs/WORKFLOW-MAP.md` is read first, per this repo's standing rule for anything touching an
   adapter.

### What would change, and only this

A **branch on the reference builder alone**, selected by an explicit request field, defaulting to
today's behaviour. Concretely: where `build_h3_reference_payload` emits

```python
"mvp:attention": {"class_type": "PathchSageAttentionKJ",
                  "inputs": {"model": ["mvp:shift", 0],
                             "sage_attention": "disabled", "allow_compile": False}}
```

the treatment arm emits instead

```python
"mvp:attention": {"class_type": "ModelAttentionBackend",
                  "inputs": {"model": ["mvp:shift", 0],
                             "attention": "comfy kitchen attention"}}
```

with everything downstream (`mvp:preview` consuming `["mvp:attention", 0]`) unchanged, so the node
id and the wiring are identical and the graph diff is one node's class and inputs.

**Explicitly out of scope, and each for a reason:**

* The keyframe and image-edit builders. Same node, different checkpoints; one graph at a time.
* The Director (text-only) builder. It has no `mvp:attention` node; adding one is a second change.
* `allow_compile: True`. Compilation is a *separate* variable with its own first-run cost and its
  own failure modes. Two variables in one experiment measures neither.
* Comfy Kitchen's 30 steps, its 13.92 sigma shift, its `fl2va` checkpoint, and every downstream
  post-processing node in its graph. Step count changes both cost and picture; importing it would
  make any speed difference uninterpretable. **The control and the treatment differ in the
  attention node and nothing else.**
* Any change to `POPULATE_MAX_WINDOW_SECONDS`.

### What is measured

**Arm A (control):** today's shipped reference graph, `PathchSageAttentionKJ` / `"disabled"`.
**Arm B (treatment):** identical but `ModelAttentionBackend` / `"comfy kitchen attention"`.

Two frame counts, chosen to straddle the cliff:

* **141 frames** (~5.1 s window) — the flat region, and the count `POPULATE_TARGET_WINDOW_SECONDS`
  actually produces.
* **226 frames** (8.75 s window) — the first cliff point, and the point where n=1 today.

**n ≥ 5 per cell**, i.e. 20 renders total. Five because the whole reason this document exists is
that n=1 produced a claim wrong by 3.4×. Report **median and full range**, never a mean, and
report every individual run — an outlier is named, not averaged away. Same prompt, same
references, same seed within a cell (distinct seeds across cells).

**Primary measure:** seconds per frame at each count, per arm, from `render_seconds` with
`render_seconds_source == "comfy"`.

**Secondary measure — and it can veto the result on its own:**

* **Lip sync survival.** This project's entire premise is H3's audio-driven sync. Every arm-B
  take at both frame counts is inspected against its arm-A counterpart at the same seed. A speed
  win with degraded sync is not a win; it is a different, worse product. The comparison is
  visual and is the Director's call — the LTX enhancer taught this repo that a graph change can
  silently alter a performance, and that nobody notices unless they look.
* **Motion and picture.** Same subject, same prompt, same seed: is the motion the same motion?
  Numerics changes in attention can move a sample.
* **VRAM headroom** at 226 frames, read from `/system_stats` before and after, because the
  spilling hypothesis in §1 predicts the benefit is *larger* at 226 than at 141. If arm B speeds
  up 141 frames and 226 frames by the same proportion, the cliff is not what this change fixes.

### What each result would justify

| Result | What it justifies |
|---|---|
| Arm B is materially faster at 226 (say ≥1.5×) **and** sync/motion are indistinguishable at both counts | A proposal to make the accelerated graph the reference builder's default, as its own change, with its own audit against `docs/WORKFLOW-MAP.md` — and **then**, separately, a re-derivation of `POPULATE_MAX_WINDOW_SECONDS` from arm B's own table. Two changes, two decisions. |
| Arm B is faster at 226 but sync or motion moved | **No default change.** At most an opt-in profile beside `turbo`, documented as altering the performance, never applied silently. The premise outranks the minutes. |
| Arm B is faster by roughly the same factor at 141 and 226 | The acceleration is a flat win and **says nothing about the cliff**. The spilling hypothesis survives untested; the ceiling stays where it is. |
| Arm B is no faster, or slower | The cliff is not an attention-implementation artefact. Record it, keep the shipped graph, and the ceiling stays. This is a real result and must be written up, not discarded. |
| `"comfy kitchen attention"` is absent from `/object_info` | Unavailable on this build. Report it; run nothing. |

**In every row: `POPULATE_MAX_WINDOW_SECONDS` does not move as part of this experiment.** Its
second justification — that guidance alone failed, because on the first 5.2 s-target run the local
model echoed the previous plan's 9 s windows out of its own context — stands on its own and is
untouched by any of this.

### Cost

20 renders. At the arm-A medians in §1 that is 5 × 6.3 min + 5 × 30.2 min ≈ 3.1 hours for the
control arm alone, plus whatever arm B costs. **This is hours of the Director's GPU and must be
scheduled by them, not assumed.**

---

## 5. Provenance

* Render costs: mtimes under
  `…/output/music-video-producer/project_59f14d19ff10/shots`, batch of 2026-08-19/20, read
  2026-08-21. The project directory was read and **never written**.
* Shipped graph: `src/music_video_producer/workflows.py` lines 513-514, 1001, 1166, 1399, read
  2026-08-21.
* KJNodes no-op: `ComfyUI-KJNodes/nodes/model_optimization_nodes.py:118-120`.
* `ModelAttentionBackend`: `comfy_extras/nodes_model_advanced.py:352-382` and
  `comfy/ldm/modules/attention.py:13, 54, 887-888`, ComfyUI 0.33.1.
* Comfy Kitchen graph: `…/workflowsbackup/ComfyKitchen/MiniMax-H3 TXT2VID IMG2VID (Full)- 20260818.json`,
  <https://www.patreon.com/aifuturetech/posts/minimax-h3-mode-167313564>.
* All of the above were read from disk. **No ComfyUI endpoint was queried and no render was
  submitted in the writing of §1–§5 of this document.**

---

## 6. The premise correction, and what was built (added 2026-08-21, live server)

Everything in §1–§5 was written from disk against ComfyUI 0.33.1. This section was written
after reading live `/object_info` and `/system_stats` on 0.33.3 and the installed libraries.
It **contradicts §1 and §2 on their central claim**, and the contradiction is the most
important thing in this file.

### 6.1 The acceleration was never off. It was on the whole time.

§1's caveat 2 and §2's heading both say the cliff was measured on the unaccelerated path.
That is wrong, and the argument for it — "the node is a no-op at `disabled`" — is correct
but does not support the conclusion.

`PathchSageAttentionKJ` at `disabled` is `return model,`: no clone, **no
`optimized_attention_override` written**. A node that writes no override does not disable
anything; it declines to override whatever ComfyUI already selected globally at startup.
And this ComfyUI selects SageAttention:

* `/system_stats` reports `argv` as
  `['…/main.py', '--use-sage-attention', '--fast']`.
* `ComfyUI/user/comfyui.log` prints `Using sage attention` at every start it still holds,
  including **`2026-08-20 09:00:34`** — inside the serial overnight batch §1's table was
  reconstructed from. (`comfy/ldm/modules/attention.py:855` — `sage_attention_enabled()` sets
  the module-level `optimized_attention`.)
* `python_embeded/Lib/site-packages` holds `sageattention 2.2.0+cu128torch2.7.1`. The
  launcher `run_nvidia_gpu.bat` carries the flag and was last modified **2026-08-19 12:08**,
  which is when this became true. `docs/ROADMAP.md` and `docs/OPERATIONS.md` still said
  "not installed" until today; both are corrected.

**So the cliff was measured on SageAttention, and the arm nobody here has ever rendered is
plain PyTorch.** Every conclusion in §4's result table that turns on "arm A is the
unaccelerated path" needs re-reading with that in mind: arm A is *an* accelerated path, and a
finding that a candidate is no faster than it is a finding about two accelerators, not about
acceleration.

### 6.2 The chain position, chain-walked rather than assumed

§4's proposed treatment emits `ModelAttentionBackend` at `["mvp:shift", 0]` — *after* the
sigma shift. §3's own chain walk of the Kitchen graph says the opposite, and §3 is right:
`6095 ModelAttentionBackend → 6007 MiniMaxH3SigmaShift → 214 BasicGuider`. The implementation
follows §3. Each node class carries its evidenced position (`H3_ATTENTION_CHAIN_POSITION`), so
`ModelAttentionBackend` lands **before** the shift and `PathchSageAttentionKJ` stays **after**
it, which is also what keeps the default byte-identical.

Whether that ordering is load-bearing was checked in the implementations rather than inferred
from the picture. It is not — **but only because exactly one attention node is ever emitted.**
Both classes write the same
`model_options["transformer_options"]["optimized_attention_override"]`
(`comfy/model_patcher.py:688` and `model_optimization_nodes.py:134`). Two in one chain do not
compose: the later one wins, silently. That is the mechanical reason the Kitchen graph
*branches* rather than stacking, and the reason a profile is a whole node.

### 6.3 Which sage kernels are reachable, and why the other five are not

Read from the installed library rather than from the option list:

* **`sageattn3`, `sageattn3_per_block_mean`** import `sageattn3` — a **separate package**
  (`model_optimization_nodes.py:51`) that is not installed. They raise at sampling time,
  after the checkpoint is loaded. Not offered.
* **`sageattn_qk_int8_pv_fp16_triton`** — `sageattention/core.py:153` says the triton kernel
  "is currently not usable on sm120", which is this card. Not offered.
* **`auto`** → offered as `sage-auto`. `core.py:143-153` dispatches on the live CUDA arch and
  routes **sm120 to `sageattn_qk_int8_pv_fp8_cuda(pv_accum_dtype="fp32+fp16",
  qk_quant_gran="per_warp")`**. This is the vendor's own choice for Blackwell, and on a
  `--use-sage-attention` server it should reproduce `default`'s timing — which makes it the
  experiment's **internal control**, not a fifth guess.
* **`sageattn_qk_int8_pv_fp8_cuda++`** → offered as `sage-fp8-cuda++`. KJ's `++` mode calls
  the same kernel with the same accumulator but leaves `qk_quant_gran` at the library default
  `per_thread` (`model_optimization_nodes.py:46-49`), so it differs from `sage-auto` in
  exactly one term. If the two diverge, granularity is what diverged.
* The two remaining lower-accumulator CUDA kernels are not what the vendor selects for sm120.
  Adding one is a one-line edit plus a reason; the registry is closed on purpose.

`allow_compile` stays `False` everywhere, for §4's own reason.

### 6.4 What was actually built

* `workflows.H3AttentionProfile` + `H3_ATTENTION_PROFILES` + `resolve_h3_attention`, following
  the `H3SamplingProfile` bundle pattern. Five profiles: `default`, `pytorch`,
  `comfy-kitchen`, `sage-auto`, `sage-fp8-cuda++`.
* `attention=` on the **reference**, **keyframe** and **image-edit** builders. The
  text-only Director builder takes none and grows no node, per §4.
* **The default is proved unchanged by digest, which is the acceptance criterion.** The two
  pre-existing reference digests and the text-only digest still pass untouched; two new
  digests (`H3_KEYFRAME_PRE_ATTENTION_DIGEST`, `H3_IMAGE_EDIT_PRE_ATTENTION_DIGEST`) were
  taken at commit `c4c0722` — the commit before this work — so every builder that emits an
  attention node now has its default pinned. Every sampling profile × builder combination was
  additionally hashed at `c4c0722` and re-hashed after, byte-for-byte identical.
* `tests/preflight_h3_ultra.py` grew `check_attention_profiles` and five payload variants; it
  now audits **21 variants / 373 nodes / 25 classes** clean against live 0.33.3.
* `tests/measure_h3_attention.py` — the harness, gated on `--confirm-gpu`.

### 6.5 The false-null trap, and how the harness refuses to fall into it

`ModelAttentionBackend.VALIDATE_INPUTS` returns `True` unconditionally and `patch` falls back
to PyTorch with a log warning when the named backend is not registered. A run that "used comfy
kitchen attention" and was actually PyTorch is **indistinguishable from a real one in the
output**, and reads as "no difference" — the one result this experiment cannot afford to
publish by accident. The harness:

1. reads live `/object_info` and refuses to run `comfy-kitchen` unless
   `"comfy kitchen attention"` is in the published list (it is, on 0.33.3), recording the list
   in the report;
2. opens a **per-render window on `ComfyUI/user/comfyui.log`**, saves it beside the output,
   and classifies each arm: `fell-back` (the warning appeared — **inconclusive, never "no
   difference"**), `confirmed` (a sage arm whose selected mode ComfyUI named, matching what was
   asked), `wrong-mode`, `unconfirmed`, `registered` (a `ModelAttentionBackend` arm, which
   emits no positive line), or `inherited` (the default, which patches nothing);
3. treats `registered` as weaker evidence than `confirmed` and says so. The chain behind it is
   sound — one boolean, `COMFY_KITCHEN_INT8_ATTENTION_IS_AVAILABLE`, evaluated once at import,
   gates *both* the option's appearance in `INPUT_TYPES` *and*
   `register_attention_function("comfy_kitchen_int8", …)` (`attention.py:54, 886-888`), so an
   option published by the running server means the function is registered in that same
   process and the fallback branch is unreachable — but the `pytorch` arm exists partly as the
   discriminator: if `comfy-kitchen` and `pytorch` time identically while the sage arms differ,
   that has to be explained before it is believed.

### 6.6 How the run differs from §4's design, and why

| §4 | as run | why |
|---|---|---|
| 2 arms (control, kitchen) | **5 arms** | §6.1: the control is SageAttention, so `pytorch` is a *necessary* arm and `sage-auto` is an internal control that should reproduce `default`. |
| 141 and 226 frames | **226 only** | The cliff point is the question; 141 doubles the cost for the flat region the table already samples at n=14. |
| n ≥ 5 per cell (20 renders) | **n = 1 per arm**, `--repeats N` available | 5 arms × 5 = 25 renders ≈ 12 h. n=1 is explicitly **not** enough to settle the cliff and is reported as such; it is enough to see a large effect or an inert arm, and `--repeats` is one flag away. |
| — | **a 107-frame warmup first** | Model residency is worth ~150 s (`docs/ROADMAP.md`); without it the first arm pays the load and looks slower for a reason unrelated to attention. |
| — | **round-robin across repeats** | Thermal drift over a multi-hour run would otherwise land on whichever arm ran last. |
| timing from `render_seconds` | ComfyUI's `/history` execution span, wall clock recorded beside it and labelled | Same source, read directly; no mtimes. |
| lip sync "inspected" | frames **20, 44, 70, 113, 180, 225** extracted by frame index (`select=eq(n,N)`, never by timestamp) and hstacked into one labelled sheet per index | The LTX enhancer investigation's own indices and comparison shape, so the two are comparable; 113/180/225 cover the back half its 107-frame clip did not have. |

**Speed and lip sync are two verdicts and are never averaged.** A backend that is faster and
moves the mouth is a refusal, not a trade.

### 6.7 The bigger variable was never the backend: the whole table is a 20-step measurement

Found while the arms were rendering, and it reframes what any of these numbers mean.

**Every figure in §1's table was taken on the `default` sampling bundle — 20 steps,
`res_multistep`, no LoRA.** Both `api.generateBatch` call sites (`app.js:4157`, `4173`) send
no `profile`, so `BatchRequest.profile` falls through to its `"default"`. Meanwhile
single-shot "Render Again" (`app.js:2575`) hardcodes `profile: "turbo"` — 4 steps, the
`ref2v` turbo LoRA at 0.7, `beta`/`euler`. The overnight batch and one-off re-renders have
been running **different bundles**, and the cost table — and therefore the 6-second window
ceiling it justifies — describes only the slower of the two.

The Director's reading, and it fits the first measured arm: at matched settings this machine
is not slow. Their own workflow does a 10 s reference-to-video at 20 steps in about half an
hour, and `default+default` came in at 1753 s for 226 frames — 29 minutes. **The headline
cost problem is bundle selection, not attention and not implementation.**

So two more arms follow the backend comparison, at the same 226 frames, seed, prompt,
references and window, on whichever attention configuration wins (or `default` if the five
tie):

* **`turbo-references2v`** — 8 steps, `minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors`
  at 1.0, `simple`/`euler`. The Director's audited canonical turbo reference-to-video graph.
* **`turbo`** — 4 steps, the `ref2v` turbo LoRA at 0.7, `beta`/`euler`. What "Render Again"
  already ships.

These are the arms that actually decide the ceiling, because they are what production could
ship. An arm is now a `(bundle, backend)` pair; every arm records its own step count, LoRA,
scheduler and sampler beside its cost, so a five-fold step difference can never be read as an
attention result.

**Caution, and it is not a formality.** Fewer steps is a **quality** trade as well as a speed
one. `docs/WORKFLOW-MAP.md` already records that at 107 frames turbo saved only 23% despite a
5x cut in steps, because sampling is not what dominates this graph — so the speed case for
turbo may be much weaker at 226 frames than the step ratio suggests, while the quality cost is
paid in full. **Do not recommend turbo on timing alone.**

### 6.8 Audio fidelity is a third verdict

The Director heard a test render's audio as "a bit of a mutation of what I assume was the
input audio". Treated as a data point rather than noise.

H3 generates its audio conditioned on the reference rather than copying it, so *some*
departure is the model working as designed. The open question is whether it departs further
under fewer sampling steps or a quantised attention backend — which is exactly the trade
§6.7 is about, and the place it would land first on a project whose premise is lip-sync. Each
arm's take is now decoded to mono 16 kHz alongside the same master-song window and reported
as a normalised cross-correlation plus the lag in milliseconds at its peak.

Three honesty constraints on reading it:

* It is **comparative, not a fidelity score**. An absolute correlation well below 1 is
  expected. A correlation markedly lower *than another arm's*, at the same seed and window,
  is the signal.
* **The lag aliases on periodic material.** Correlation peaks at every multiple of a repeating
  period, so a sustained note's reported offset may be an alias — a 220 Hz tone delayed
  100 ms reports 50 ms. It is usable where the consonants are, which is where sync is judged
  anyway, and a hint elsewhere.
* **Silence is reported as absent, never scored.** A failed decode correlating at exactly 0.0
  would read as "this arm's audio bears no resemblance to the song", which is the most
  alarming thing the table can say and would send someone hunting a defect that is not there.

**Speed, audio and lip-sync are three verdicts. None of them is averaged into another, and the
Director decides the trade.**

### 6.9 MEASURED: the cliff is a memory wall, not a compute one

**This is the most useful thing the experiment has produced, and it came from an arm that
never finished.**

§1 offered a hypothesis: 158 → 226 frames is a 1.43x increase in sequence length, attention's
quadratic term alone predicts ~2.05x, and the observed 3.0x meant "something superlinear beyond
attention's own arithmetic — the shape of a working set no longer fitting". It said what would
test it. The `pytorch` arm tested it by failing.

**Measured 2026-08-21/22, `default` bundle, 226 frames, RTX 5090 / 32 GB:**

| arm | result |
|---|---|
| `default+default` (inherits SageAttention) | **1753.3 s execution, 7.758 s/frame**, completed |
| `default+pytorch` (`ModelAttentionBackend`, `pytorch attention`) | **did not complete** — cancelled at 97 min with zero sampling steps |

The `pytorch` arm's four signals, each independently checked:

* submitted 23:54:47, cancelled 01:32 — **97 minutes against the completed arm's 29:13**
  (>3.2x) with **no sampling steps at all**;
* ComfyUI's log printed one line after submission (`Requested to load MiniMaxH3`) and
  **never reached `loaded completely`**, which the `default` arm reached before sampling;
* a raw ComfyUI websocket listened for a full 3 minutes saw **zero progress frames** — the
  `default` arm averaged ~88 s per step, so a listen that long crosses a boundary on anything
  genuinely running;
* GPU pinned at **100% utilisation drawing 159–232 W** on a card that pulls 400–575 W under
  compute — high utilisation at low power is memory traffic, not arithmetic — with VRAM at
  31743/32607 MiB and **host RAM free falling from 30.5 GiB to 14.3 GiB**, i.e. spilling.

**Conclusion: plain PyTorch attention cannot fit a 226-frame H3 sequence on a 32 GB card.**
Attention memory is quadratic in sequence length; SageAttention's fused kernel never
materialises the full matrix and SDPA does. The 158 → 226 cliff is therefore **a memory wall
rather than a compute one**, and §1's hypothesis is supported by direct measurement rather than
by arithmetic.

**What follows for the Director, and it is more useful than any arm's timing:**

* **Frame count and resolution are the real levers.** They set the working-set size, which is
  what decides whether a render is on the cheap side of the wall or the expensive one. The
  6-second window ceiling is defensible on exactly this basis — not on "renders are slow".
* **No backend can do more than move where the wall sits.** A better kernel shifts the frame
  count at which the working set stops fitting; none of them removes the wall. So the backend
  question, which prompted this whole experiment, has largely answered itself — and it
  answered *against* the premise: acceleration was never off, so there was no switch to flip,
  and flipping it the other way is catastrophic.
* **A failed arm with evidence beat a number.** Had the `pytorch` arm merely been slow and
  finished, it would have produced one more row in a timing table. Because it failed, it
  identified the mechanism.

**Standing rule adopted from this.** An arm is cut automatically — no re-confirmation, no GPU
hours — when *all four* hold: no `loaded completely` within 1.5x a completed arm's execution
time, zero websocket progress frames across a 3-minute listen, and power draw under 200 W at
high reported utilisation. `should_cut` implements exactly that conjunction and is tested
against every single-signal case, because each alone has an innocent reading. Anything not
matching the pattern — an error, a crash, an arm merely slow but genuinely sampling — is left
for a person.

**And the sequencing changed.** Backends are now screened at **107 frames** (~5 min) before any
is promoted to 226. A backend that cannot load, cannot engage, or thrashes shows it there for
minutes instead of an hour, and `sage-auto` doubles as the harness's own control at that
length: if it does not reproduce `default`, the harness is suspect and nothing downstream can
be trusted.

### 6.10 Three measurement defects, what they voided, and what survives

**All three were in the harness, and all three were found by reading code rather than by a
test failing.** They are recorded in full because the run's credibility depends on which
numbers came from a sound fixture and which did not.

**Defect 1 — cost included model load.** The 107-frame screen ran with the checkpoint cold for
its first arm and resident for the rest, so that arm paid a 62 s load (CLIP 25.9 GB + UNET
20.0 GB) the others did not. A table built on *total execution* reported it as the attention
backend being **24% slower** — a headline figure that was reported twice before being checked.
ComfyUI's own per-step rates were 9.08 / 9.18 / 9.22 s/it: indistinguishable. **Fixed** by
measuring sampling time, parsed from ComfyUI's tqdm summary, so residency cannot move the
number; the load is reported beside it in a `non_sampling` column rather than inside it.

**Defect 2 — the harness never used `over_render_window`.** A take does not begin at
`shot.start`; it begins `lead` seconds earlier, and take second `t` is song second
`start − lead + t`. The route conditions H3 on exactly that window (`app.py:8215`). The
harness sent the bare exposed slice `(start, start + duration)` — not lead-shifted, and
shorter than the take — and then compared the output against a *third* window again. The
resulting "systematic −513 ms offset across all backends" was the harness measuring its own
inconsistency, and it was nearly written up as a finding about render geometry.

**This is the same defect `over_render_window` was extracted to prevent**, met from a new
direction: its own docstring records `restore_song_audio` having windowed "by the bare
`start`/`duration`, which is the *exposed slice* rather than the take". **The invariant had a
home and the harness did not use it.** That is the lesson worth keeping — a helper that exists
to stop arithmetic being written twice only works if the third caller finds it.

**Defect 3 — the 107-frame fixture was 42 milliseconds of song.** `duration_for_frames` walked
*up* from one frame and returned the first duration matching the count. `over_render_frames`
floors every window below ~3.271 s at `H3_MIN_RENDER_FRAMES` = 107, so for 107 it answered
**0.0417 s**. Every arm in the screen rendered a 42 ms window and was conditioned on 42 ms of
song. **Fixed** by requiring the un-floored `margin_frames` arithmetic and taking the top of
the natural band (3.9583 s for 107), with a test asserting that the floored answer exists and
is *not* the one returned.

#### What each defect voided

| Result | Status |
|---|---|
| Backend ranking at 107 frames (`default` 9.08, `comfy-kitchen` 9.18, `sage-auto` 9.22, `sage-fp8-cuda++` 9.37, `pytorch` 11.41 s/it) | **Survives.** The fixture is identical in every arm and sampling cost is set by frame count and resolution, so the *relative* comparison holds. |
| "No backend beats the shipped default" | **Survives**, on the same basis. |
| The memory wall and its four signals | **Survives.** It rests on a did-not-complete arm, not on any of these numbers. |
| Every audio number from the 107 screen | **Void.** There was no song to correlate against. |
| The 107-frame contact sheets as lip-sync evidence | **Void.** There was nothing to sync to. |
| The ~2.96 cost exponent | **Re-measured and it survives: 2.93.** See §6.11 — the confound was real but small. |
| The mtime cost table's corroboration | **Corroborated at one point, not two.** The 226-frame arm (7.758 vs the reconstructed 8.02 s/frame) carried a proper 8.25 s of audio and stands. The 107-frame agreement (2.386 vs 2.58) was measured on the 42 ms fixture and is withdrawn — and the gap was explained as mtime overhead when lighter conditioning is the better explanation. |

**Read no absolute cost off the 107-frame screen.** `9.08 s/it` is a same-fixture comparison
point, not what a 107-frame render costs. A representative 107-frame render carries about
4.46 s of conditioning audio; that one carried 42 ms.

The defective-fixture arms are kept, under a `-vf` label suffix, with the caveat written into
each record. They are the backend result and deleting them would discard it; renaming them
only stops a corrected re-render being silently "reused" in their place.

### 6.11 MEASURED on the corrected fixture: the cost exponent is ~2.93, and the ranking reproduces

Two arms were re-rendered at 107 frames with the corrected fixture — natural 3.9583 s window,
lead-shifted `over_render_window` of 4.4583 s starting at 65.75 s, i.e. **4.46 s of
conditioning audio instead of 42 ms**. Three things came out of it, and the first is the one
that rescues the run's best number.

**1. Conditioning-audio length barely affects sampling cost.** `default` at 107 frames went
from **9.08 s/it** on 42 ms of audio to **9.28 s/it** on 4.46 s — **+2.2%** for a hundredfold
increase in reference audio. The confound behind the exponent was therefore real but small.

**2. The cost exponent survives.** Sampling-only, so no model load is in it:

| frames | s/it | conditioning audio |
|---|---|---|
| 107 | 9.28 | 4.46 s (corrected) |
| 226 | 83.15 | 8.25 s |

**8.96× the cost for 2.11× the frames — an exponent of 2.93**, against attention's theoretical
quadratic 2.0. On the void fixture it computed as 2.96, so correcting the confound moved it by
0.03. The residual mismatch is that the 226 arm's window was 8.25 s where the route would send
9.4167 s; by the scaling measured in (1), a 14% change in audio length is worth well under 1%
of cost, so it cannot account for the gap between 2.93 and 2.0.

**This is the memory wall as arithmetic.** Something superlinear beyond attention's own
quadratic term is happening between 107 and 226 frames, which is exactly what §6.9's
did-not-complete arm demonstrated qualitatively. **n=1 at each end** — this is a two-point
exponent and should be read as "clearly steeper than quadratic", not as a precise 2.93.

**2a. Reference audio is nearly free, and that is worth knowing on its own.** The +2.2%
figure above arrived as a confound correction, but it is a useful fact in its own right:
**a hundredfold increase in conditioning audio — 42 ms to 4.46 s — cost 2.2% of sampling
time.** Sampling cost is set by frame count and resolution; the audio reference is close to
free.

Two things follow directly. Conditioning on the *whole* over-render window rather than a
trimmed one costs essentially nothing, so `over_render_window`'s longer window is not a
performance concern and never was. And it closes off a category of optimisation before anyone
proposes it: **"send H3 less song to make renders cheaper" would buy nothing measurable.**
That matters because `song_audio_window` exists precisely to stop a shot riding 154 seconds of
song through every sampling step — a real fix whose *stated* motivation was cost, and this
measurement says the cost argument for it was weaker than the correctness argument. The
correctness argument stands untouched.

**3. The backend ranking reproduces across two different fixtures.** `pytorch` against
`default` at 107 frames: **25.7% slower** on the void fixture, **25.8% slower** on the
corrected one. Two independent fixtures, the same relative answer to a tenth of a percent.
That is the strongest evidence in the run that the backend ordering is a property of the
backends rather than of how they were measured.

**4. Audio, on a fixture that finally has a song in it.** Envelope correlation 0.4352
(`default`) and 0.4343 (`pytorch`), lag 193.4 ms and 193.9 ms. The two arms agree to 0.001 in
correlation and 0.5 ms in lag: **no backend changes the audio's relationship to the song**, now
measured against a window the render was actually conditioned on.

The ~193 ms offset is consistent across arms and is **not** a decode artefact — input-seek and
output-seek decodes of the same window cross-correlate at r=0.9985 with 0.0 ms lag, so ffmpeg
is not introducing it. What it is remains open: it may be a real systematic offset between
H3's generated audio and the song it was conditioned on, or it may simply be that a
*regenerated* phrase places its syllables slightly differently from the original, which is not
a defect at all. **It is a property of the render and not of the backend**, so it is outside
this experiment; naming it here so it is not rediscovered as a surprise.

### 6.12 Was the mtime reconstruction sound? Yes at long windows, no at short ones — and it understated the cliff

§1's whole table came from gaps between consecutive output-file mtimes. Whether that *method*
was sound is a separate question from whether any single number was right, and the corrected
arms answer it, because ComfyUI's own execution clock can now be set beside it at both ends.

An mtime gap is wall clock between two finished files. It contains everything: model load,
sampling, VAE decode, encode and write, plus queue wait, the application's polling interval
before it notices completion, and the gap before the next submission. ComfyUI's execution span
is a strict subset. So these are different measurements and closeness, not equality, is the
most that can be asked.

| | mtime-derived | ComfyUI execution | residual | residual as share of the gap |
|---|---|---|---|---|
| 107 frames | 276 s (2.58 s/frame) | **193 s warm** (1.804 s/frame) | 83 s | **30%** |
| 226 frames | 1813 s (8.02 s/frame) | **1753 s** (7.758 s/frame) | 59 s | **3%** |

*(The corrected 107 arm ran cold at 259 s / 2.422 s/frame — within 6% of 2.58 — but a serial
batch is warm after its first render, so the warm figure is the one the overnight batch
resembles. Both are given rather than the flattering one.)*

**The residual is roughly constant — 60–80 s per render — which is exactly what a fixed
per-render overhead looks like.** It is 30% of a short render's gap and 3% of a long one's. I
cannot decompose it further from what was captured; queue wait, poll latency, file write and
inter-render gap are all inside it and none was measured separately.

**The consequence is the useful part, and it runs the opposite way to a caveat.** A constant
overhead inflates the *short* end of the table more than the long end, so it flattens the
ratio between them:

| cliff, 107 → 226 frames | ratio |
|---|---|
| mtime-derived | **6.57×** |
| ComfyUI execution | **9.08×** |
| sampling only | **8.98×** |

**The mtime table understated the cliff by about a third.** The reconstruction was directionally
right and its 226-frame number is good to 3%, but it was measuring renders *plus* a fixed cost,
and the cliff is steeper than the table the window ceiling was justified from. Anyone re-deriving
`POPULATE_MAX_WINDOW_SECONDS` should use execution or sampling time, not mtime gaps — and should
know that doing so makes the case for a *low* ceiling stronger, not weaker.

**Verdict on the method:** sound for long windows, unreliable for short ones, and systematically
compressive of any ratio between the two. It was the right thing to do with no instrumentation
available, and it is no longer the right thing to do now that `/history` carries the clock.

### 6.13 MEASURED: the turbo bundles, and the finding that a backend swap re-rolls the take

**226 frames, corrected fixture, `default` attention, n=1 each. Sampling time only.**

| bundle | steps | s/it | sampling | s/frame | vs default | envelope r |
|---|---|---|---|---|---|---|
| `default` | 20 | 73.91 | 1478 s | 6.540 | — | 0.3399 |
| `turbo-references2v` | 8 | 40.06 | 320 s | 1.416 | **4.62× cheaper** | 0.3230 |
| `turbo` | 4 | 72.29 | 289 s | 1.279 | **5.11× cheaper** | 0.2491 |

**The bundle is worth four to five times what the backend is worth, and the Director was
right that this is where the cost lives.** A 226-frame render costs ~25 minutes of sampling on
`default` and ~5 minutes on either turbo bundle.

Two details worth keeping. `turbo`'s saving is *entirely* step count — its per-step cost
(72.29 s) is the same as `default`'s (73.91 s), and 20/4 = 5.00 against a measured 5.11×.
`turbo-references2v` is the more interesting one: only 2.5× fewer steps but 4.62× cheaper,
because its per-step cost is **half** (40.06 s). Why per-step costs differ this much between
bundles is **not explained here** — `res_multistep` versus `euler` is the obvious suspect and
a first-step fixed cost is another, and neither was measured. Recorded as an open question
rather than guessed at.

**Run-to-run variance at 226 frames is ~12%.** The same `default` configuration measured
1662 s and 1478 s in two runs. Everything at this length is n=1 with that spread, so the cost
exponent is **2.78–2.93 depending on which baseline is used** — "clearly steeper than
quadratic" survives; a precise figure does not.

#### The finding that changes how any of this can be judged

**Changing the attention backend does not re-render the same take. It produces a different
one.** The 107-frame contact sheets put `default` beside `pytorch` at the same seed, prompt,
references and window. The set, wardrobe, character and framing style are plainly the same —
and by frame 20 the performance is not: different head angle, eyes open in one and closed in
the other, different mouth shape, different hand position, the framing shifted. By frame 44
the two are visibly different performances of the same shot.

This is expected once stated: attention numerics differ between an int8/fp8 kernel and SDPA,
diffusion amplifies small differences, and the trajectory diverges. But it has two consequences
that the experiment's design did not anticipate.

**1. The frame-indexed lip-sync comparison cannot answer the question it was built for — for
this kind of change.** The method came from the LTX enhancer investigation, and it worked
there because the enhancer *post-processes an existing take*: frame N is the same instant, so a
moved mouth is a real regression. A backend change re-rolls the take, so frame N is a
different instant of a different performance. **Mouths differ in these sheets because the
performances differ, not because sync degraded.** Whether each take is *itself* in sync can
only be judged by watching it against the song — which is the Director's ear, not a contact
sheet. The sheets remain useful for "is this an acceptable shot", and that is all they can
carry here.

The same applies with more force to the bundle arms: at 226 frames `default`,
`turbo-references2v` and `turbo` produced three **entirely different shots** — frontal
close-up, side view, three-quarter view. There is no frame correspondence between them at all.

**2. A backend swap is not a free optimisation, and this is the strongest argument against
one.** It re-rolls every take. An approved take would not survive the change. That outranks a
1–3% timing difference by a wide margin, and it means the backend question is settled on
grounds better than speed: **there is nothing to gain and a take to lose.**

**What the audio measure does and does not say.** `default` and `pytorch` at 107 frames scored
0.4352 and 0.4343 envelope correlation against the same song window — near-identical, and that
is a real result: both takes sit the same way against the song. It is *not* a statement that
the mouth matches the audio, because it never looks at the picture. Audio-to-song is measured;
mouth-to-audio is not, and no number in this experiment measures it.

### 6.14 Preview generation is paid on every render, for nobody — measured cost deferred

`ModelPreviewOverrideKJ` is emitted at **three sites** in `workflows.py` —
`build_h3_director_payload` (node `2344`), `build_h3_reference_payload` and
`build_h3_keyframe_payload` (both `mvp:preview`) — every one carrying
`max_resolution 1024, jpeg_quality 80, suppress_default_preview True, preview_frames 12,
preview_fps 12`.

**Those are not our values.** `h3-ultra-references-user-export.json` node `2376` carries
exactly them, so the adapter is faithfully reproducing the Director's own export. This is not
a defect anyone introduced; it is a setting that made sense in a graph someone was watching
render, and does not in a batch. That distinction decides the shape of any fix: **a batch-mode
option, not a bug correction.**

**ComfyUI generates previews whether or not anyone is listening.** Read from `server.py`
rather than assumed: `send_image` performs the resize and the quality-95 encode *before*
`send_bytes` ever consults `self.sockets`, and with no clients attached that is an empty loop
over zero sockets. The previewer itself is constructed from `args.preview_method` (forced by
this node), not from client count, so the latent→image decode in the sampler callback is paid
too. Attaching a listener adds only a socket write of an already-encoded buffer.

`preview_frames: 12` therefore means **twelve frames sampled from each step's latent, decoded,
resized and WebP-encoded, on every sampling step, for an audience of nobody** during a batch.
On a 20-step render that is 240 decode-and-encode operations per shot; across a 33-shot batch,
roughly eight thousand.

**MEASURED 2026-08-22, and it is a NULL. There is no optimisation here.** Two interleaved
pairs at 158 frames on `turbo-references2v`, warmup discarded, no `/free`:

| arm | `preview_frames` | sampling | s/it |
|---|---|---|---|
| p12-r1 | 12 | 218 s | 27.37 |
| p1-r1 | 1 | 223 s | 27.89 |
| p12-r2 | 12 | **154 s** | 19.36 |
| p1-r2 | 1 | **154 s** | 19.27 |

Pair 1 has 12 previews coming out **2.2% faster** than 1 — noise, and both arms sat in the slow
power regime. Pair 2 is the clean pair, both arms in the fast regime, and they are
**identical**: 154 s against 154 s, 0.09 s/it apart.

**Per 158-frame shot the measured cost of `preview_frames: 12` is zero seconds.** The control's
2.5% warm reproducibility (§6.19) bounds anything undetected below **~3.9 s per shot**, so
across a 33-shot batch the preview cost is **bounded under ~2 minutes and measured at zero.**

So the ~8,000 decode-and-encode operations per batch are real arithmetic and **not on the
critical path** in any way this can detect — most likely overlapped with GPU work, or trivial
beside it. **`preview_frames: 12` stays exactly as the export has it, and this should not be
raised with the Director as an opportunity at all.**

This retires an earlier claim in this document, which called it "a real optimisation, unlike
everything else tonight". It was not. The arithmetic was persuasive and the measurement
disagreed — which is the entire reason it was measured rather than acted on.

**Limitation:** this run predates the per-arm power instrumentation, so regime membership is
inferred from the timings clustering at 218/223 and 154/154 rather than recorded. Pair 2 is
treated as the clean pair on that basis.

**Nothing was changed to measure it.** `--preview-frames` patches the value at *submission*
only; with the flag absent nothing is patched and every payload stays byte-identical to what
the builders emit.

A second question rides on it for free: thousands of PIL allocations and WebP encodes per
render is the shape of workload that fragments a host heap, and this machine has an
unexplained host-memory accumulation (§6.15). If the `preview_frames: 1` arm accumulates
visibly less, that is a mechanism. If they accumulate identically, previews are exonerated.
**No evidence connects them today and none is asserted.**

### 6.15 `/free` releases VRAM between renders and does not stop host-memory accumulation

`POST /free` with `{"unload_models": true, "free_memory": true}` is core ComfyUI
(`server.py:1192`); it sets queue flags that `main.py:398-410` consumes on the worker's next
tick, calling `unload_all_models()`, resetting the executor cache and forcing a gc. It is
**asynchronous** — nothing has happened when the 200 arrives — and it answers **200 with a
zero-length body**, which is worth knowing because parsing that reply as JSON raises and makes
a successful call look like a failed one.

Measured across the clean five-point sweep, which called it before every arm: host RAM free
went from **48.58 GiB after the Director's restart to 8.11 GiB four arms later.** So `/free`
does not prevent the host-side accumulation. Whatever grows is not what `unload_all_models()`
releases.

For contrast, the preceding session reached **94.75 GiB committed against 61.6 GiB of physical
RAM** (`python` pid 24088, 22.78 GiB resident) with Windows holding 19 GiB in Memory
Compression, and a full ComfyUI restart recovered it to 48.58 GiB free. **A restart clears it;
`/free` does not.**

**What this does not establish:** that the accumulation costs render time. The Director's own
overnight batch rendered 14 shots at a constant 141 frames across 30 positions and 4½ hours in
one session, and its second-half median (6.3 min) was *faster* than its first-half median
(7.2 min), with the five fastest all after position 24. Memory pressure was presumably
accumulating there too and cost did not rise. **So host-memory accumulation and render cost
are not demonstrably coupled**, and no production recommendation follows from this yet.

### 6.16 The band sweep: two attempts, no usable curve, and why that is the honest result

The Director approved a sweep of the unmeasured band — **175, 192 and 209 frames** — between
the last known-good count (158, which is what `POPULATE_MAX_WINDOW_SECONDS = 6.0` maps to) and
the measured cliff at 226. It was run twice on `turbo-references2v`, and **the two runs
disagree in shape, not merely in level.**

| frames | sweep A (no `/free`, warm) | sweep B (`/free` between arms) | spread |
|---|---|---|---|
| 158 | 140 s (17.61 s/it) | 137 s (17.22 s/it) | **2%** |
| 175 | 191 s (23.93 s/it) | 222 s (27.81 s/it) | **16%** |
| 192 | 328 s (41.00 s/it) | 534 s (66.76 s/it) | **63%** |
| 209 | 657 s (82.25 s/it) | 523 s (65.39 s/it) | **26%** |

Sweep A climbs monotonically. Sweep B jumps between 175 and 192 and then **plateaus, going
slightly down** at 209. One accelerates where the other flattens, and they cross. The same
frame count on the same bundle, same machine, same fixture arithmetic and same seed differs by
up to **63%**.

**Neither sweep is a curve.** Sweep A's local exponents rise with render position — 3.04, 5.83,
8.19 — across frame steps all within 2% of each other in size; sweep B's rise faster still
(4.72, 9.47) and then reverse. An exponent near 9 for a 10% frame increase is not attention
scaling by any reading; attention is quadratic.

**Order and size are perfectly confounded in both designs.** Both render ascending frame counts
sequentially in one invocation, so nothing either produced can separate "bigger render" from
"later render". That was a design fault, and it is the reason a **constant-frame control** —
the same count rendered repeatedly — is the measurement that was actually needed.

**Counter-evidence against the order reading, from production.** The Director's overnight batch
rendered **14 shots at a constant 141 frames across 30 positions and 4½ hours** in one session.
First-half median 7.2 min, **second-half median 6.3 min** — later renders were *faster*, and the
five fastest all landed after position 24. That is a real constant-size control, on the `default`
20-step bundle through the app's batch path, and it shows **no degradation with position.** Its
caveat is that it is mtime-derived, so it carries the ~60–80 s fixed overhead of §6.12 — but
that overhead is roughly constant and therefore neither creates nor hides a trend.

**And the strongest single fact against a frame-count reading is inside the data already:** on
this bundle, **226 frames cost 320 s** while 209 cost 657 s and 192 cost 534 s. If size drove
cost, 226 could not be the cheapest of the three. That 226 point was measured in a separate,
earlier invocation — which is precisely the escape hatch, and precisely why it had to be
re-measured inside a sweep.

**So the band question is unresolved and no ceiling recommendation follows from it.** What the
run established instead is that **cost measurements on this machine are not reproducible at
these frame counts** — a characterised measurement problem, which every future measurement here
inherits, and which is more durable than a curve would have been. `POPULATE_MAX_WINDOW_SECONDS`
stays where it is, on the evidence it already had.

**What is unaffected by any of this:** the bundle comparison at 226 frames (§6.13). Those three
arms were measured **warm, in one invocation**, so the drift that ruins the band sweeps does not
touch them — and the effect there is 4.6–5.1×, far outside the spread seen here.

### 6.17 What was NOT measured, and what to run first next time

**The reproducibility control was never run.** It is the single measurement that would make
§6.16 conclusive instead of inconclusive, and it is cheap:

```
uv run python tests/measure_h3_attention.py --project <id> --shot <id> \
    --run-dir <new> --frames 158 --repeats 3 --sampling turbo-references2v \
    --profiles default --free-between-arms --confirm-gpu
```

Three identical renders — **frame count held constant, so any rise is render order and only
order.** Flat within a few percent means order is innocent, the sweeps' 63% spread is variance
far larger than anyone assumed, and no cost number from this machine should be quoted at n=1
again. Scattered by tens of percent and the sweeps are artefacts of the harness's back-to-back
path, which the Director's production batch does not share. Either answer is worth more than a
fourth attempt at the curve. **Run this before the preview measurement.**

**The in-sweep 226 point was cancelled**, by the operator, to release the GPU. It had reached
step 5 of 8 at roughly 155 s/step. Recorded as `cancelled-by-operator` with **no timing
fields** — it sampled partially, so any duration in a cost column would sit beside completed
arms pretending to mean the same thing. This was the sharpest test available: the
separately-measured 226 on this bundle cost 320 s, *below* the same sweep's 192 (534 s) and 209
(523 s), and only an in-sweep 226 could have separated run conditions from frame count. It
remains unmeasured.

**The preview cost is unmeasured** (§6.14). Second in the queue, after the control.

**A note on the harness's own watchdog, since a cancelled arm looks like a cut one.** It did
not fire and could not have: the arm logged `loaded completely`, which is one of the four
signals `should_cut` requires in conjunction, and the run log carries no `cutting this arm`
line. The cancellation was external. The distinction is recorded because "the harness killed
its own arm" is exactly the sort of thing that becomes true in a retelling.

---

## 7. What the Director should take from all of this

**Solid, and the only thing here worth acting on:**

* **The sampling bundle is where the cost is.** At 226 frames, `turbo-references2v` (8 steps)
  sampled **4.62× cheaper** than `default` (20 steps), and `turbo` (4 steps) **5.11× cheaper** —
  roughly 5 minutes of sampling against 25. All three arms were measured **warm, in one
  invocation, on one fixture**, so none of the instability in §6.16 touches them.
* **But it is a quality trade, not a free win**, and this run cannot settle the quality half.
  The three bundles produced **three entirely different shots** at the same seed — frontal
  close-up, side view, three-quarter — so there is no frame-to-frame comparison to make. `turbo`
  (4 steps) also scored the lowest envelope correlation against the song (0.2491 against
  `default`'s 0.3399), with `turbo-references2v` close to `default` at 0.3230. That is one
  number at n=1 and not a verdict, but it points the same way caution does:
  **`turbo-references2v` looks the safer of the two on both counts.** The takes are preserved;
  the picture judgement is the Director's.
* ~~**The batch and "Render Again" already ship different bundles**~~ — **RECONCILED 2026-08-23.**
  This bullet stood for two days as "recorded, not reconciled, because whether the batch should
  move is the same quality decision as above". The Director made that decision on the 8-step
  comparison — *"both still look good so **up to user**, and perhaps the video style would benefit
  from it in some cases"* — and the split closed with it. `Project.sampling_profile` is now the one
  place a bundle is chosen; `H3Request.profile` and `GenerateBatchRequest.profile` are
  `SamplingProfile | None`, and an omitted profile (which every shipped client sends) resolves to
  the project's choice in `app.resolved_sampling_profile`. Generate All, Re-queue flagged and
  Render Again therefore render the same bundle, the browser shows which one with its step count
  before the batch is confirmed, and the untouched default is digest-proved byte-identical.
  `app.js`'s hardcoded `profile: "turbo"` is gone.

  **Consequence for every cost figure in this document.** §6.7's observation that "the whole table
  is a 20-step measurement" is now a statement about the past: from 2026-08-23 a render's bundle is
  whatever the project is set to, so **no cost number may be quoted without naming the bundle it
  was taken on**. Every table here is `default` unless its own row says otherwise.

**Settled, and requiring no action:**

* **The attention backend is a dead end.** No backend beat the shipped default; the whole spread
  was 3.3% at 107 frames with `default` marginally fastest. And a backend swap **re-rolls every
  take**, so an approved take would not survive one — nothing to gain and a take to lose.
* **The premise that started this was wrong**: acceleration was never off. ComfyUI runs
  `--use-sage-attention` and the adapters' `sage_attention: "disabled"` declines to override it
  rather than disabling anything.
* **Plain PyTorch attention cannot fit 226 frames on this 32 GB card** — 97 minutes, zero
  sampling steps, memory-bound. It fits at 107. The wall sits between.

**Open, with the next step named:**

* **The window ceiling — now MEASURED, and the answer is a shape rather than a number**
  (§6.20). Per-frame cost is **flat out to 175 frames / 6.79 s** — a 6.79 s window is
  marginally *cheaper* per frame than the 6.083 s the current ceiling produces — and then
  climbs steeply: **+48%** at 7.50 s, **+184%** at 8.21 s, **+264%** at 8.92 s. **Linearity
  ends between 6.79 s and 7.50 s.** So roughly 0.7 s of extra window is free and the fourth
  second beyond that is not. `POPULATE_MAX_WINDOW_SECONDS` stays the Director's to set: this
  is what each length *costs*, and the picture quality of a long take is unmeasured.
* **`/free` / Manager's "Unload Models": do not use it as render hygiene.** It frees memory
  from *torch's bookkeeping*, not from the device — `nvidia-smi` still showed 27 GB in use at
  the moment a render began right after one — and it pays for that by pushing ~8–12 GiB into
  host RAM per call, driving a 61.6 GiB machine toward zero. It made a 226-frame render **25%
  slower**, not faster (§6.21). Only a **full ComfyUI restart** produces a genuinely clean card.
  It remains the right button for freeing VRAM for a *different* workflow.
* **`preview_frames: 12` costs nothing — no action** (§6.14). It looked like a real
  optimisation (~8,000 encode operations per batch) and measured at **zero seconds per shot**,
  bounded under ~4 s. Left exactly as the audited export has it.

### 6.18 Known differences between measurement sessions

Recorded because unrecorded differences between runs cost this experiment four corrections.
**These are differences, not explanations** — none is asserted as causal.

**ComfyUI launch argv changed between sessions:**

| session | argv |
|---|---|
| band sweeps A and B, and everything before them | `main.py --use-sage-attention --fast` |
| repeat control (after the Director's second restart) | `main.py --windows-standalone-build --use-sage-attention --fast` |

The acceleration flags are identical and `--windows-standalone-build` concerns launcher
behaviour rather than kernels, so it is not a plausible cause of a timing difference. What it
does record is that ComfyUI was started a **different way** — the `.bat` rather than directly.
ComfyUI 0.33.3 and torch 2.7.0+cu128 in both.

**Machine load during the repeat control**, checked rather than assumed: GPU 6% / 48.8 W /
7275 MiB / 54 °C between arms, SM clock 967 MHz,
`clocks_throttle_reasons.active = 0x0` — nothing throttling. Compute clients were ComfyUI,
LM Studio holding a context with **no model loaded**, and TrackIR5 (head tracking, trivial).
**No competing GPU load.**

**A 158-frame render now occupies nearly the whole card.** Measured during the repeat
control's second arm: `memory.used = 31056 MiB` of 32607. For comparison, the 226-frame renders
that defined the memory wall sat at ~31700 MiB. **A 158-frame render is at nearly the same
occupancy as a 226-frame one**, in a session where the identical fixture costs 224 s against
the 137 s and 140 s measured before.

If that holds, "near the wall" may be a property of the **session** rather than of the frame
count — which would dissolve the band question rather than answer it, because the wall would
not sit at a frame count at all. Recorded as an **observation**. Whether it is ComfyUI caching
more aggressively after this restart, allocator state, or something else is not established.

**And the two sessions ran in different power regimes.** During this control's arm 2 the card
sat at **476–502 W with `clocks_throttle_reasons.active = 0x4` (SW Power Cap)**, SM 2745–2782
MHz, memory 13801 MHz, 71→80 °C. The band sweep's 209-frame arm, by contrast, logged **284 W
and no throttling at all**.

**That is backwards from any simple story and is the strongest single reason to distrust
cross-session comparison.** The earlier, *faster* arms drew moderate power and were not
power-capped; these slower arms are pinned at the cap. High power at the cap is the opposite of
the low-power-at-high-utilisation signature that characterised the memory-stalled PyTorch arm
(§6.9) — so the slower session is working *harder*, not less. `clocks.sm`, `clocks.mem` and
`clocks_throttle_reasons.active` are now captured per arm; they were not when the band sweeps
ran, which is why that difference went unrecorded until it was sampled by hand.

**Sessions are not interchangeable, and that is the point of §6.19.** Every cross-session
comparison in this document — including the drift-versus-clean disagreement of §6.16 — spans a
ComfyUI restart. If between-session variance is large, those comparisons were never valid, and
the disagreement they show is a property of the sessions rather than of the frame counts.

### 6.19 MEASURED: warm consecutive renders reproduce to 2.5% — and `/free` is what eats the host

Five identical 158-frame renders, one bundle, one seed, one fixture, one session,
`/free` before each.

| repeat | sampling | s/it | VRAM before | host RAM free **after** its `/free` |
|---|---|---|---|---|
| 1 | **224 s** | 28.01 | 2132 MiB | 44.44 GiB |
| 2 | 157 s | 19.66 | 27557 MiB | 0.78 GiB |
| 3 | 158 s | 19.82 | 27617 MiB | 0.86 GiB |
| 4 | 161 s | 20.16 | 27640 MiB | 0.84 GiB |
| 5 | **220 s** | 27.59 | 27679 MiB | **0.01 GiB** |

**n=5 · min 157 · max 224 · median 161 · range 42.7%.**

**Read the shape, not the range.** 42.7% says "nothing is reproducible" and that is not what was
measured. The distribution is **bimodal**: {224, 220} and {157, 158, 161}. **Warm consecutive
renders reproduce to 2.5%.** That is the finding, and it is what makes any future measurement
on this machine possible.

r1 is explained: it began with an empty card (2132 MiB) and paid a cold start — which costs
**sampling** time, not merely load. **r5 is an anomaly and stays one.** It is the only arm that
began with host RAM at 0.01 GiB, which is a candidate; but r1 was equally slow with 44 GiB free,
so memory pressure cannot be the whole story. n=1 on a bimodal distribution is exactly where a
tidy explanation would be wrong.

#### `/free` offloads to host RAM — it does not free memory

The measurement that was not asked for, and the most useful thing in this document.

`/free` genuinely releases VRAM in torch's view: `comfy vram_free` went 4.7 → 21.5 GiB on every
call. But its **host RAM deltas were −11.98, −10.95, −9.68 and −9.22 GiB.** It does not discard
the weights; **it moves them from VRAM into system RAM.** Host free fell 44.43 → 12.76 → 11.81 →
10.52 → 9.23 GiB across the run and sat at ~0.8 GiB — finally 0.01 GiB — immediately after each
call.

**So `/free` is the cause of the host-memory pressure, not a failed mitigation for it.** That
retracts this document's earlier reading in §6.15 and explains the clean band sweep's 48.58 →
8.11 GiB decline, which also ran `--free-between-arms`. **The instruction to add
`--free-between-arms` came from the coordinator, and it made that sweep worse rather than
cleaner** — recorded because a mitigation that harms is worth more as a warning than as a
footnote.

**The Director's actual question, answered directly.** Manager/Crystools **"Unload Models"
offloads the model stack to system RAM rather than freeing it.** On this machine — 61.6 GiB of
RAM against a ~20 GB model stack — repeated use drives host memory to zero. It is the right
button when you need VRAM headroom for a *different* workflow, and the wrong one as a general
hygiene habit between renders of the same one. **Only a full ComfyUI restart actually recovers
the memory** (48.58 GiB free after the Director's restart).

#### What this settles about the band work

**The band sweeps' 63% disagreement cannot be within-session noise**, because within-session
warm consecutive renders reproduce to 2.5%. It is therefore real, or it is between-session — and
it is now **undiagnosable**, because those arms carry no state capture: no VRAM occupancy, no
power, no throttle state, no host RAM. §6.18 records what was sampled by hand afterwards and it
is not enough.

That is the honest close on the ceiling question, and it is also the specification for what
would answer it: **one session, warm, state on every arm, and the first arm discarded.** Nothing
measured before this section met all four conditions.

### 6.20 MEASURED: the band curve, and it is believable

Fifth attempt, and the first that answers the question. **One session, warm, warmup discarded on
the arms' own bundle, no `/free`, state and mid-render power on every arm, and — the change that
made it readable — a render order chosen so that position and frame count are uncorrelated**
(158, 226, 209, 192, 175; Spearman ρ between execution slot and frame count = **0.0**, against
**1.0** for the ascending order both failed sweeps used).

`turbo-references2v`, 8 steps, n=1 per point:

| frames | window | exec# | sampling | s/it | **s/frame** | power median | VRAM before |
|---|---|---|---|---|---|---|---|
| 158 | 6.083 s | 1 | 162 s | 20.27 | 1.025 | 460.5 W | 2233 MiB |
| 175 | 6.792 s | **5** | 171 s | 21.46 | **0.977** | **478.5 W** | 27085 MiB |
| 192 | 7.500 s | 4 | 278 s | 34.76 | 1.448 | 373.4 W | 27165 MiB |
| 209 | 8.208 s | 3 | 580 s | 72.57 | 2.775 | 254.7 W | 27042 MiB |
| 226 | 8.917 s | 2 | 804 s | 100.52 | 3.558 | 226.6 W | 27749 MiB |

**Monotonic in frame count; not monotonic in execution order (1, 5, 4, 3, 2). The order effect
is dead.** The decisive point is **175**: it rendered **last**, on an occupied card, and was the
**cheapest per frame of all five** at the **highest median power**. Under any "later is slower"
or "residency degrades" story it should have been among the worst.

**This also refines the VRAM-inheritance model rather than confirming it.** 175, 192, 209 and
226 all began with residency within 2.6% of each other (27042–27749 MiB) and their costs span
**3.6×**. At roughly constant resident weight, **frame count alone predicts cost**. Only the 158
arm started on a nearly-clean card, so that single point stays confounded; the other four are a
controlled series.

**The mechanism is visible for the first time.** Median power falls monotonically —
478 → 460 → 373 → 255 → 227 W — while **maximum power stays ~576 W throughout**. The card keeps
its full capability and progressively spends more of each step waiting on memory. That is §6.9's
memory-stall signature measured directly rather than inferred from a failure.

**There is a sharp knee between 175 and 192 frames.** Local exponents across consecutive points:

| step | cost ratio | frame ratio | exponent |
|---|---|---|---|
| 158 → 175 | 1.06× | 1.108× | **0.53** |
| 175 → 192 | 1.63× | 1.097× | **5.24** |
| 192 → 209 | 2.09× | 1.089× | **8.67** |
| 209 → 226 | 1.39× | 1.081× | 4.18 |

Per-frame cost is **flat to 175 frames** and climbs steeply after.

**In the Director's units — reported, not recommended.** A 6.083 s window costs 1.025 s/frame; a
6.792 s window costs **0.977 s/frame**, marginally *cheaper*. So the current 6.0 s ceiling sits
comfortably inside the flat region, and roughly **0.7 s of extra window appears to be free**.
Beyond that: 7.50 s is **+48%** per frame, 8.21 s **+184%**, 8.92 s **+264%**. **Linearity ends
between 6.79 s and 7.50 s.** `POPULATE_MAX_WINDOW_SECONDS` is the Director's to set and the
picture-quality half of that decision is unmeasured.

### 6.21 `/free` does NOT mitigate the wall — and it never cleaned the card

Prediction under the VRAM-inheritance model: if a 226-frame render is slow because it inherits
the previous arm's residency, then running it after a `/free` should be dramatically faster.
Tested as its own labelled run — 158 then 226, `/free` before each, so 226 starts "clean":

| ex | frames | sampling | s/it | s/frame | power median | VRAM before |
|---|---|---|---|---|---|---|
| 1 | 158 | 141 s | 17.74 | 0.892 | 500.8 W | 27087 MiB |
| 2 | 226 | **1004 s** | 125.53 | 4.442 | 213.3 W | 27126 MiB |

**226 after a `/free` cost 1004 s against 804 s on an occupied card — 24.9% SLOWER, not faster.
The prediction is disconfirmed.**

**And the reason matters more than the result: the card was never actually cleaned.** `/free`
reported releasing 14.25 GiB in torch's view, but `nvidia-smi` still showed **27126 MiB in use**
at the moment the render began — essentially identical to the 27749 MiB of the "occupied" arm.
Torch's caching allocator holds the driver-level allocation regardless. **The only thing that
ever produced a genuinely clean card in this entire run was a full ComfyUI restart** (2233 MiB).

So `/free` frees memory *from torch's bookkeeping*, not from the device, and it pays for that
bookkeeping by pushing ~8–10 GiB into host RAM (deltas of −8.35 and −9.66 GiB here, consistent
with §6.19). **It buys nothing at large frame counts and costs host memory. It should not be
recommended as a mitigation for the wall.**

Caveat, stated because cross-run comparison is what has burned this experiment repeatedly: the
804 s and 1004 s figures come from **different invocations**. Both are n=1 and the difference is
far outside the 2.5% warm reproducibility of §6.19, but a same-session A/B was not run.

---

## 8. The "soft skin" prompt clause: RUN — and the answer is "we cannot tell"

**Status: specified 2026-08-23, run 2026-08-23 on the Director's authorisation. §8.1–§8.7 below
are the specification as written *before* the run and are left unedited; §8.8 is the result.
Three renders, ~9.5 minutes of GPU. No prompt clause was added anywhere in the application, and
§8.7 still stands.**

The Director, reading the 8-step-vs-20-step comparison
(`docs/measurements/2026-08-23-bundle-comparison/index.html`):

> *"Unknown if 'soft skin' in the prompt would counteract the effect."*

The effect is the one that comparison measured and the report named: `turbo-references2v` is
**sharper, not softer** — high-frequency energy +127% / +152% / +52% across the three shots — and
the cost is texture realism, skin that reads waxier at high zoom with flatter mid-tone
micro-texture. That is the familiar few-step-LoRA trade of acutance for grain. The open question is
whether a prompt clause can buy some of the grain back.

**It is a cheap, well-formed experiment**, which is exactly why it is written down rather than
guessed at: one shot, one seed, one bundle, one clause added, two renders.

### 8.1 Why it is worth running at all

The measurement it would settle is not "does turbo look good" — the Director has ruled on that
("both still look good so up to user"). It is narrower and more useful: **is the waxiness a
property of the sampling bundle, or of the prompt under that bundle?** If a clause moves it, the
bundle's one stated cost becomes a thing the Director can dial, and the ruling's "up to user" gains
a second control. If it does not, the waxiness is a property of 8-step sampling and the answer is
to stop looking for a prompt fix — which is worth as much, and is the more likely result.

### 8.2 Preconditions

1. ComfyUI up, GPU free, and the Director scheduling it — this is their card.
2. The shot is `shot_f82bf42e3abc` ("Close-up, singing", 141 frames, seed 14, window 66.58–71.69 s
   with lead 0.25 s). It is the close-up, because skin texture is only legible on a face filling
   the frame, and the b-roll's +52% is the weakest signal of the three.
3. **The arms differ in the prompt clause and in nothing else.** Same seed, same references, same
   song window, same bundle, same frame count, same resolution. `turbo-references2v` for both arms:
   the question is about turbo's own texture, so running it against `default` would answer a
   different question that has already been answered.
4. Timing from ComfyUI's own execution clock, as everywhere else in this document. It is not the
   measure here, but it is recorded.

### 8.3 The arms

**Arm A (control):** the shot's existing prompt, verbatim, exactly as
`docs/measurements/2026-08-23-bundle-comparison/report.json` records it. It is already rendered —
`job_3a177562476c`, prompt `71de8e17-293e-4c58-8a55-9bb069bf1e61` — so **arm A may be reused rather
than re-rendered**, provided nothing in the adapter has moved since. Verify by digest of the
submitted payload before reusing it; if it has moved, re-render.

**Arm B (treatment):** the identical prompt with one clause appended. The clause is the Director's
own words and must not be paraphrased into a longer instruction: **"soft skin"**. Appended to the
shot's prose, not inserted into the reference map and not made part of any section look — the
reference map is machine-numbered, and a hand edit there is a different change.

**Two arms and one variable.** Not three; not "soft skin" plus "film grain" plus a negative prompt.
Two variables in one experiment measures neither — §4's own rule, and the reason that section's arm
list stayed at two.

### 8.4 What is measured

**Primary, and it is the same instrument the comparison used**, so the numbers are comparable to
its table rather than to a new scale:

* **`detail`** — mean Laplacian energy over the take. Arm A read **376.82** on
  `turbo-references2v` against `default`'s 166.22. A clause that "counteracts the effect" moves arm
  B's figure **toward** 166 and away from 377.
* **`bytes_per_frame`** — 18137.2 on arm A against default's 14494.9. It moves with the texture the
  encoder finds worth keeping, and it is an independent read on the same thing.
* **`flicker`** — 10.9769 on arm A against 5.8596. Reported, not used as the verdict: it rises with
  real motion as well as instability, and a re-rolled take has different motion.

**Secondary, and it can veto on its own:** the picture. **A bundle change re-rolls the take, and so
does a prompt change** — §6.13's finding applies here with full force. Arm B is a *different
performance*, so there is no frame-to-frame comparison to make and no contact sheet that means what
one would mean for a post-process. The judgement is "is this shot acceptable, and is its skin less
waxy", and it is the Director's eye. A number saying the texture moved while the Director says the
shot got worse is a refusal, not a trade.

**Audio is recorded and is not the question.** Envelope correlation and lag, for consistency with
every other table here; a prompt clause about skin has no reason to touch the vocal, and a figure
that moved would be a signal that something else changed.

### 8.5 What each result would justify

| Result | What it justifies |
|---|---|
| Arm B's `detail` falls materially toward the default's figure **and** the Director judges the skin better | A recommendation the Director can apply per shot, written up in `docs/WORKFLOW-MAP.md` as a prompt technique. **Not** an automatic clause: nothing in this application may add words to a Director's prompt without them asking. |
| `detail` falls and the picture is worse — softer everywhere, not just the skin | Recorded as a blunt instrument. The clause trades global acutance, which is not what was asked for, and the waxiness stays a property of the bundle. |
| `detail` does not move | **The useful null.** The waxiness is a property of 8-step sampling and no prompt clause reaches it. Stop looking for a prompt fix, and read the ruling as final: the trade is the trade, and the choice is the control that now exists. |
| Arm B's audio numbers move | Something other than the intended variable changed. Investigate before reporting anything about texture. |

### 8.6 Cost, and the honesty constraint on n

**One render** if arm A is reused, two if it is not. At the comparison's measured 240.3 s of ComfyUI
execution for this shot on `turbo-references2v`, that is **four to eight minutes of GPU** — the
cheapest experiment in this document by two orders of magnitude.

**It is also n=1**, and this document exists because n=1 produced a claim wrong by 3.4×. A single
pair can show a large effect or a flat null; it cannot size a small one. If the first pair lands
ambiguously — `detail` moving by less than the run-to-run variance this machine has shown — the
answer is "unresolved at n=1", not a number. Three seeds would settle it and cost under half an
hour; that is the escalation, and it is the Director's to authorise.

### 8.7 What must not happen while this is unrun

**No "soft skin" clause exists anywhere in the application, and none may be added on the strength of
this section.** Not in `SONG_AUDIO_CONTINUITY_CLAUSE`, not in the expansion prompts, not in a
section look, not as a default. This is a specified test, and the whole point of writing it down
instead of doing it is that the effect is **unmeasured**.

### 8.8 MEASURED 2026-08-23: the clause moves nothing that a re-rolled take does not move more

**Run on the Director's authorisation. Three renders, one session, warm, consecutive, no `/free`,
first render discarded, state and mid-render power on every arm — §6.19 and §6.20's protocol, all
four conditions met. Pictures and stills:
`docs/measurements/2026-08-23-soft-skin-clause/index.html`.**

**The answer to the Director's question is "we cannot tell from this", and that is the true
answer rather than a hedge.**

#### What was run, and what it cost

| ex# | arm | prose | seed | comfy exec | sampling | s/step | VRAM before | host RAM free | power median | power max | temp max | throttle |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | **warmup / noise floor** | control | 15 | 212.5 s | 169.9 s | 21.23 | **2165 MiB** | 20.36 GiB | 506.6 W | 584.2 W | 82 °C | SW power cap |
| 2 | **A — control** | control | 14 | 133.3 s | 133.3 s | 16.66 | 27170 MiB | 12.12 GiB | 520.7 W | 575.5 W | 85 °C | SW power cap |
| 3 | **B — treatment** | control + `soft skin` | 14 | 221.2 s | 183.2 s | 22.90 | **8976 MiB** | 28.03 GiB | 484.0 W | 575.5 W | 83 °C | SW power cap |

All three ran **compute-bound** — 484–521 W median at 99–100% utilisation, against §6.20's
~250 W memory-starved signature. Nine and a half minutes of GPU in total.

**The timings are recorded and are not comparable, and the reason is a finding.** Arm B began
with **8976 MiB** resident where arm A began with **27170 MiB**: between the two arms ComfyUI
released ~18 GiB of VRAM *and* ~16 GiB of host RAM (host free rose 12.12 → 28.02 GiB during arm
A), so arm B paid a model reload arm A did not. **This is not `/free` — nothing called it, and
`/free` moves weights to host RAM rather than releasing them (§6.19).** Something in ComfyUI's own
memory management evicted the stack unprompted. The consequence for method is direct: **"warm and
consecutive" is not guaranteed by running arms back to back**, and residency has to be read per
arm rather than assumed. It was read here, which is the only reason the 66% timing spread between
arms 2 and 3 is explicable rather than mysterious.

#### The measure

The §8.4 instrument, unchanged, plus two additions that are named as additions.

| arm | `detail` | skin detail | skin, normalised | `flicker` | `bytes_per_frame` |
|---|---|---|---|---|---|
| **A — control, seed 14** | 369.08 | 140.36 | 0.0503 | 11.3339 | 20133.5 |
| **B — + "soft skin", seed 14** | 335.68 | 124.84 | 0.0534 | 9.7944 | 17853.3 |
| noise floor — control, seed 15 | 484.39 | 118.91 | 0.0675 | 8.0678 | 14666.3 |
| archived arm A — same payload, previous session | **376.82** | 147.19 | 0.0615 | 10.9769 | 18137.2 |
| 20-step default — the bundle comparison's soft arm | 166.22 | 65.22 | 0.0373 | 5.8596 | 14494.9 |

**skin detail** is the same Laplacian statistic over the face rectangle only and over the first
2.58 s. That restriction is load-bearing: **past ~2.6 s each take makes its own camera move to its
own destination** — the seed-15 take cuts to a bright static wide, the seed-14 takes keep a moving
figure in a dark room — so the whole-take `detail` figure past that point is comparing *content*,
not texture. **skin, normalised** divides out each take's own contrast, which separates "more
grain" from "flatter picture".

#### The result, against two floors

| measure | A | B | change | re-roll floor | same-payload floor | read |
|---|---|---|---|---|---|---|
| skin detail | 140.36 | 124.84 | **−11.1%** | **15.3%** | 4.9% | inside the noise |
| skin, normalised | 0.0503 | 0.0534 | **+6.2%** | 34.2% | 22.3% | inside, and the wrong way |
| `detail`, whole take | 369.08 | 335.68 | −9.0% | 31.2% | 2.1% | inside the noise |
| `flicker` | 11.3339 | 9.7944 | −13.6% | 28.8% | 3.1% | inside the noise |
| `bytes_per_frame` | 20133.5 | 17853.3 | −11.3% | 27.2% | 9.9% | inside the noise |

**Two floors, and only one of them applies.** The *same-payload* floor is what a byte-identical
graph at a byte-identical seed produces in a different session: **2.1% on `detail`**, which is
essentially §6.19's 2.5% warm reproducibility appearing in a picture statistic. The *re-roll*
floor is what a different take of the **unchanged** prose produces. **A prompt change is a
re-roll (§6.13, §8.7), so the re-roll floor is the bar, and nothing clears it.**

**Every measure moves less than re-rolling the take moves it.** The primary `detail` figure went
369.08 → 335.68, which is the direction that would indicate the clause works; re-rolling the seed
on the control prose moved the same number to 484.39. **The effect is under a third of the
noise.**

#### Three things that argue further against reading the movement as texture

1. **The normalised measure goes the other way (+6.2%).** The face's contrast fell (std 52.82 →
   48.36) and its detail fell with it. What little changed is a marginally flatter picture, not
   micro-texture coming back. Mean face luma is unchanged (53.42 → 53.95), so this is not an
   exposure artefact either.
2. **The gap is the wrong size by an order of magnitude.** On this same instrument the 8-step
   bundle sits at 147.19 skin detail against the 20-step default's 65.22 — **−55.7%**. The clause
   produced −11.1%, inside a 15.3% floor.
3. **By eye, arm B's skin is not less waxy.** The sheen on the forehead, the nose and the
   cheekbone is present in both arms at every matched moment. The stills are on the page; the
   judgement is the Director's and not this document's.

#### The finding that was not asked for, and matters more than the result

**A byte-identical payload at a byte-identical seed does not reproduce the same take across
sessions — and the difference is larger than the prompt clause's.**

Arm A here and the archived arm A are the same graph and the same seed, verified node by node.
Their frames differ by **mean |Δ| = 20.3/255 with pixel correlation 0.48**. Arm A against arm B —
*different prose* — differs by **18.7/255, correlation 0.55**. **The same payload is less alike to
itself across sessions than the two arms are to each other.**

Summary statistics reproduce to a few percent; frames do not reproduce at all. **This retracts the
reuse allowance in §8.3**: "arm A may be reused rather than re-rendered, provided nothing in the
adapter has moved" is not safe, because a matching payload digest does not buy a matching take.
The digest check was performed and passed, and it would still have been the wrong control.
Anything downstream that plans to reuse an old take as a control should re-render instead.

#### What this justifies, read off §8.5's own table

The row that fires is **"`detail` does not move" — the useful null**, with the qualification §8.6
requires: at n=1 per arm this is *"unresolved at n=1"* rather than a proven zero. A single pair
can show a large effect or a flat null; it cannot size a small one, and −11.1% against a 15.3%
floor is exactly the ambiguous landing §8.6 anticipated.

**So: nothing here justifies adding the clause anywhere, and §8.7 stands unchanged.** No "soft
skin" clause exists in `SONG_AUDIO_CONTINUITY_CLAUSE`, in the expansion prompts, in a section
look, or as a default, and this section is not grounds to add one. The escalation §8.6 named —
**three seeds per arm, under half an hour of GPU** — remains the Director's to authorise, and it
is the only thing that would turn this null into a measurement.

#### Method notes for the next person

* **The arms differ in the prompt clause and in nothing else**, verified rather than asserted: the
  control payload rebuilt for this run is byte-identical to the archived arm A submission
  (`71de8e17-293e-4c58-8a55-9bb069bf1e61`, recovered from ComfyUI's own `/history`) in every node
  and every input except the output filename prefix; control and treatment differ in exactly one
  input, `mvp:condition.prompt`, by the ten characters `" soft skin"`.
* **The renders bypassed the application deliberately.** The payload was rebuilt read-only from
  the manifest through the route's own helpers and submitted straight to ComfyUI, so no job, take,
  status or prompt was written into `project_59f14d19ff10`. The project was digested before the
  GPU was touched and verified byte-identical afterwards (`project.json`
  `b15efc116073158e31d376a8ceba211c2857ac346619f6f2e1089d86fda5035f`, unchanged).
* **A specular-highlight proxy was tried and discarded.** Share of face pixels above 200/255 read
  1.61% on arm A and 0.59% on arm B — a −63% "effect" that looks decisive. It is not: the same
  payload in the previous session read 0.605%, so that statistic's own same-payload noise is
  **±165%**, far larger than the effect it appears to show. Recorded because it is exactly the
  shape of number that would otherwise have been reported as a finding.
* **The window's usable span is 0.00–2.58 s.** Anything measured or shown past that is comparing
  two different camera moves.

#### FOLLOW-UP MEASURED 2026-08-23: it reproduces **bit-exactly** — until ComfyUI reloads the model

**Run on the Director's authorisation to separate *time* from *session* in the finding above.
Four renders of the identical payload at the identical seed, one session, back to back, no
`/free`, first render discarded for timing, state read per arm. ~12.5 minutes of GPU. No job,
take, status or prompt was written into `project_59f14d19ff10`.**

**The answer is the first of the two outcomes, and it lands harder than "correlation near 1.0":
two of the four repeats are bit-identical to each other, and the other two are bit-identical to
each other, and the two pairs are different takes. H3 at fixed seed is deterministic. What
re-rolls the take is not time, not the session, and not the seed — it is ComfyUI reloading the
model between renders.**

##### What was run, and what it cost

Every arm is the *same* graph at seed 14 — the control payload of §8.8, rebuilt read-only through
the route's own helpers and verified against ComfyUI's `/history` copy of the archived arm A
submission (`71de8e17-…`): identical in all 19 nodes and every input except the output filename
prefix.

| ex# | arm | comfy exec | sampling | s/step | VRAM before | host RAM free | power median | power max | temp max | util | `mvp:model` re-run? |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | **R0 — warmup / discard** | 219.1 s | 190.2 s | 23.77 | **8344 MiB** | 22.25 GiB | 475.4 W | 574.7 W | 81 °C | 99% | **yes — loaded** |
| 2 | *(first submission — see below)* | **1.2 s** | — | — | 28014 MiB | 12.98 GiB | — | — | — | — | *cache hit, no render* |
| 2 | **R1** | 152.2 s | 152.1 s | 19.01 | **28013 MiB** | 13.04 GiB | 489.5 W | 571.0 W | 84 °C | 99% | no — resident |
| 3 | **R2** | 229.2 s | 190.7 s | 23.83 | **9760 MiB** | 27.00 GiB | 481.6 W | 575.2 W | 84 °C | 99% | **yes — reloaded** |
| 4 | **R3** | 152.9 s | 152.9 s | 19.11 | **28040 MiB** | 11.07 GiB | 488.6 W | 578.5 W | 84 °C | 99% | no — resident |

All four ran compute-bound, 475–490 W median at 99% utilisation under the SW power cap, exactly
as §8.8's arms did.

**Residency did *not* hold across the repeats, and that is the whole result.** Between R1 and R2
ComfyUI again released ~18 GiB of VRAM and ~14 GiB of host RAM unprompted — host free rose
13.04 → 27.00 GiB — reproducing §8.8's eviction on a second occasion, this time between two
submissions of a *byte-identical* graph. "Warm and consecutive" is not a property of running back
to back; it is a condition that has to be read per arm. It was read here, node by node.

##### The result

Matched-frame mean absolute difference and Pearson correlation over 8-bit luma at 528×304 — the
same frame reader the picture metrics use — reported overall and at fifteen matched frame indices
so the spread is visible rather than averaged.

| pair | what it is | mean \|Δ\|/255 | r | per-frame \|Δ\| range | per-frame r range |
|---|---|---|---|---|---|
| **R0 vs R1** | same payload, model stayed resident | **0.00** | **1.0000** | 0.0 – 0.0 | 1.000 – 1.000 |
| **R2 vs R3** | same payload, model stayed resident | **0.00** | **1.0000** | 0.0 – 0.0 | 1.000 – 1.000 |
| R0/R1 vs R2/R3 | same payload, **across one model reload** | 16.73 | 0.580 | 1.6 – 33.9 | 0.352 – 0.992 |
| R0/R1 vs archived 03:11 | same payload, earlier agent session | 16.59 | 0.541 | 1.3 – 41.1 | 0.131 – 0.993 |
| R2/R3 vs archived 03:11 | same payload, earlier agent session | 16.25 | 0.583 | 1.2 – 37.5 | 0.061 – 0.994 |
| archived 03:11 vs arm A 16:16 | **§8.8's cross-session pair, re-read** | 20.69 | 0.470 | 1.4 – 49.2 | 0.117 – 0.990 |
| arm A 16:16 vs arm B 16:19 | §8.8's *prompt change* | 19.19 | 0.538 | 2.0 – 43.2 | 0.268 – 0.981 |

`bit_identical` is literal: every one of the 141 frames of R1 is byte-for-byte the decoded frame
of R0, and the same for R3 against R2. The §8 summary statistics agree to the last digit printed
— `detail` 381.26 / 381.26 for the first pair and 348.36 / 348.36 for the second, `flicker`
10.2116 / 10.2116 and 10.5493 / 10.5493.

**Cross-reload divergence is the same size as cross-session divergence, and the same size as a
prompt change.** 16.7 against 20.7 and 19.2, on one instrument, with overlapping per-frame ranges.

*Instrument note:* this re-read of §8.8's own cross-session pair gives 20.69 / 0.470 where §8.8
recorded 20.3 / 0.48, and 19.19 / 0.538 where it recorded 18.7 / 0.55 — a ~2% difference on \|Δ\|
and ~0.01 on r, from a slightly different decode of the same videos. Every figure in the table
above is on the one instrument, so the rows are comparable to each other.

##### The mechanism, read off ComfyUI's own cached-node lists

ComfyUI reports which nodes it served from its execution cache. The correlation with the pixels is
perfect:

| arm | nodes ComfyUI re-executed (of the model chain) | pixels |
|---|---|---|
| R0 | `mvp:model`, `mvp:lora`, `mvp:shift`, `mvp:attention`, `mvp:condition`, `mvp:split` | **episode 1** |
| R1 | none of them — 12 nodes served from cache | **identical to R0** |
| R2 | `mvp:model`, `mvp:lora`, `mvp:shift`, `mvp:attention`, `mvp:condition`, `mvp:clip`, `mvp:references`, `mvp:split` | **episode 2** |
| R3 | none of them — 12 nodes served from cache | **identical to R2** |

**Given identical input tensors and identical resident weights, the sampler is bit-deterministic
on this card.** The non-reproducibility does not live in sampling. It lives in the load-and-encode
path: two executions of that path produce tensors that differ enough to send the sample somewhere
else, and every observed re-roll of an unchanged payload sits exactly on a boundary where that
path re-ran.

**Which node in that set carries it is not resolved here** — R0 and R2 both re-ran the UNET
loader, the LoRA, the sigma shift, the attention patch *and* the conditioning encode, so n=2
cannot separate them. One further render would: re-execute `mvp:condition` while `mvp:model` stays
resident, and see whether the take moves. That is the next cheap experiment and it is not run.

##### An inert variable, and why it is in the payloads

ComfyUI serves an unchanged graph from its execution cache. The first submission of R1 was
byte-identical to R0 and came back **in 1.157 seconds with `mvp:sample` reported cached** — no
sampling, no power draw, the previous file re-saved under a new name. **That is not a render, and
anything that re-submits an unchanged payload expecting a new take will get this instead.**

So R1, R2 and R3 were re-keyed by decrementing `mvp:preview.jpeg_quality` (80 → 79 → 78 → 77).
KJNodes documents that input as "JPEG quality for the live preview transport"; it cannot reach
sampling arithmetic, and **the run proves it rather than asserting it** — R0 at quality 80 and R1
at quality 79 are bit-identical, as are R2 at 78 and R3 at 77. The variable that changed inside
each episode changed nothing; the reload between episodes changed everything.

##### What this corrects in §8.8 above

* **"Frames do not reproduce at all" is wrong as written.** Frames reproduce *exactly* within a
  residency episode and not at all across one. The sentence should read: a byte-identical payload
  at a byte-identical seed reproduces the take bit-for-bit while ComfyUI keeps the model resident,
  and re-rolls it when the model is reloaded.
* **The retraction of §8.3's reuse allowance stands, but for a different reason.** Not "a matching
  payload digest does not buy a matching take" — it does, conditionally. It is that *nothing in a
  digest tells you whether a reload happened in between*, and ComfyUI evicts unprompted. Reuse of
  an archived take as a control is unsafe because the condition is unobservable after the fact, not
  because rendering is random.
* **The "same-payload floor" column is a cross-reload floor.** §8.8's 2.1% on `detail` was measured
  across a model reload (arm A at 27170 MiB resident, the archived take from a separate load). The
  true same-payload floor, within one residency episode, is **exactly 0.0% on every statistic**.
* **§8.8's arms A and B were themselves in different residency episodes** — A began at 27170 MiB
  resident, B at 8976 MiB after an eviction. So the clause effect there is not separable from a
  reload. A bare reload on this shot moved `detail` −8.6% (381.26 → 348.36); §8.8 measured the
  clause at −9.0% (369.08 → 335.68). The sizes and signs coincide. `flicker` and `bytes_per_frame`
  do not line up the same way, so this is suggestive rather than an accounting — but it removes any
  remaining reason to read §8.8's −9.0% as texture. **The null stands, and stands more firmly.**
* **§6.19's "warm consecutive renders reproduce to 2.5%" is a timing figure and is unaffected.**
  On pixels, warm consecutive renders reproduce to zero.

##### What this buys the next experiment

The escalation §8.6 named — three seeds per arm — can now be run with an **exact** control instead
of a noise floor. Two arms rendered inside one residency episode differ only by the treatment, and
any pixel difference at all is the treatment, because the floor is zero. The conditions are:

1. Warm the model with a discarded render.
2. Render control and treatment consecutively, and **read `execution_cached` on both**.
3. If either arm re-executed `mvp:model`, `mvp:lora` or `mvp:condition`, the pair crossed a reload
   — discard it and render the pair again. Do not average over it.

Eviction cannot be prevented from here, but it is fully observable, and an arm that crossed one is
a void arm rather than a noisy one.

##### Method notes

* **ComfyUI was never restarted, and it never had been.** The server process has been up since
  2026-08-22 14:09:49 — it spans the archived 03:11 take, §8.8's 16:16 arm A, and this run.
  "Cross-session" in §8.8 meant a different *agent* session; the divergence it found happened
  inside one continuous ComfyUI process, which is why the answer turned out to be residency rather
  than anything about process start-up.
* **The project is untouched.** `data/projects/project_59f14d19ff10` was digested file by file
  before the GPU was touched and verified byte-identical afterwards — `project.json`
  `b15efc116073158e31d376a8ceba211c2857ac346619f6f2e1089d86fda5035f`, aggregate
  `7ae3a765e2d825d2ec4a75cee44871dd42cf622554d63a1a77dac23e4f046fb1`, unchanged.
* **No contact sheet was produced**, deliberately: two of the four takes are bit-identical to the
  other two, so there is nothing a picture shows that the zero does not show better.
* **No code was changed.** The claims in the application that this bears on are listed in the
  handover; none were edited under this task's authorisation.

---

## 9. The unload-between-renders question: ANSWERED FROM SOURCE AND LOG, no render submitted (2026-08-23)

**Status: DIAGNOSIS. No code changed, no render submitted, no restart, no `/free`. The Director's
batch was running throughout (1 executing, 22 pending) and was not touched.**

The Director asked whether ComfyUI can be made to unload models between generations — via
`/free`, via an existing custom node, or via a new unloader node placed at the end of each
workflow — on the hypothesis that the wild cost variance is a model-load cost paid against a full
card.

**The hypothesis is refuted by ComfyUI's own log, and the fix it implies would make things worse.**
§6.21 already ran the weak form of it and measured 25% *slower*. This section says why, from the
source and from primary telemetry.

### 9.1 The variance is entirely in the sampler. Model loading is flat.

`user/comfyui.log` prints a tqdm line per sampler run and a `Prompt executed in` line per prompt,
and `load_models_gpu` prints `Requested to load X` / `loaded completely` around every load. That
decomposes each render exactly. For the five renders the Director tabulated, all
`turbo-references2v`, all 8 steps:

| clock (2026-08-23) | frames | prompt total | model loads | **sampler s/it** |
|---|---|---|---|---|
| 20:16→20:20 | 141 | 273.09 s | ~93 s | **20.44** |
| 20:21→20:24 | 141 | 246.50 s | ~70 s | **19.46** |
| 20:40→21:18 | 124 | **00:38:28** | ~101 s | **272.37** |
| 21:19→21:26 | 124 | 468.09 s | ~74 s | **47.07** |
| 21:27→21:30 | 124 | 235.17 s | ~72 s | **~16.6** |

**Model loading costs a flat 70–101 s in every one of them, including the 38-minute render.** The
38 minutes is 36:18 of *sampling* — 272.37 s/it against a 16.6 s/it floor on the very next render
of the same size. There is no model-load term to recover. An unloader cannot return time that is
not being spent.

Across the whole log, 8-step `s/it` is bimodal: a tight band at **15.1–21.5 s/it** (fourteen
renders) and a heavy tail at **34.8, 47.1, 72.6, 72.7, 125.5, 272.4** (six). Same graph, same
model, same step count. The 20-step renders show the same shape: 17.7, 19.5, 29.6, **136.1**.

### 9.2 The tail is a recovery curve, and it always follows a break in the render stream

Sorting the tail by clock position rather than by size, the pattern is unmistakable — every
outlier is the *first* render after the card was disturbed, and the next two or three converge
monotonically back to the floor:

* `72.57 → 34.76 → 21.46 → 17.74` (2026-08-22 15:45–16:02, four consecutive)
* `72.68 → 20.44 → 19.46` (2026-08-23 20:15–20:24, three consecutive)
* `272.37 → 47.07 → ~16.6` (2026-08-23 21:17–21:30, three consecutive)

The 272 s/it render is the first render after a **15-minute idle gap** (last prompt finished
20:24:47; nothing ran until 20:40:02, when the Director's batch dumped 25 prompts in four
seconds — `got prompt` ×25 between 20:40:02.686 and 20:40:05.984). And §6.21's `/free` arm, at
**125.53 s/it**, is the second-worst per-step figure in the entire log. `/free` manufactured the
break.

### 9.3 What actually holds the card: measured, not inferred

Read live at 21:31–21:35 while the batch was mid-render, all read-only:

* `GET /system_stats`: `vram_total` 32607 MiB, `torch_vram_total` (= `reserved_bytes.all.current`)
  **30426 MiB**, `vram_free` == `torch_vram_free` == 5294 MiB. Those two are equal only when
  `mem_free_cuda` is **zero** — see `get_free_memory`, `model_management.py:1781-1787`
  (`mem_free_total = mem_free_cuda + mem_free_torch`). **The driver reports no free VRAM at all;**
  every byte of apparent headroom is torch's own cached-but-inactive blocks.
* `nvidia-smi`: 32091–32099 / 32607 MiB used, **97 MiB free**.
* Windows perf counter `\GPU Process Memory(pid_15072…)\Dedicated Usage` = **30487 MiB** for the
  ComfyUI process — cross-validating torch's 30426 MiB reserve within 61 MiB.
* Same counter, the other tenants on the same adapter: **Discord 2384 MiB, NVIDIA Overlay 1696 MiB,
  dwm 409 MiB, opera 369 MiB, brave 127 MiB, WindowsTerminal 55 MiB, msedgewebview2 52 MiB,
  explorer 33 MiB** — about **5.1 GiB**, and `nvidia-smi --query-compute-apps` lists roughly thirty
  processes holding a CUDA context on this card, LM Studio among them.
  (`discord_clips` reports 13.6 million MiB — a broken counter, twice sampled, stable and
  slowly incrementing. Discard it as evidence; note it as a process to close anyway.)
* 30487 + 5100 = **35.6 GiB requested on a 32.6 GiB card.**
* `\GPU Adapter Memory(…0x14322…)\Shared Usage` = **1229 MiB**. That is the direct measurement:
  **WDDM has spilled 1.2 GiB of GPU allocations into system RAM, reachable only over PCIe.**
* Telemetry, sixteen samples at 2 s: **99% GPU utilisation, 1–4% memory-controller utilisation,
  137–170 W, 55–56 °C, SM 2887–2895 MHz, PCIe gen 5 ×16.**

150 W on a 576 W card at "99% utilisation", with the memory controller idle and clocks boosting
normally, is not compute and it is not thermal. The SMs are stalled on memory that is not on the
card. This is worse than the 230–280 W memory-starved band; it is the same failure, deeper.

Host side: 4719 of 63080 MiB free, **26230 MiB in Windows Memory Compression**, ComfyUI's working
set 19634 MiB. Both sides of the PCIe bus are out of room.

### 9.4 What `POST /free` does, read from the source, and why 27 GiB survived it

The path is short and every step is checkable.

1. `server.py:1192-1200` — `/free` only sets two queue flags. It does no memory work and returns
   an empty 200 immediately.
2. `execution.py:1399-1402` — `set_flag` also calls `not_empty.notify()`, which wakes the worker's
   blocked `q.get()`. So on an **idle** server the flags are honoured promptly.
3. `main.py:399-421` — the worker consumes them **after** `e.execute()` returns, i.e. between
   prompts:
   * `unload_all_models()` → `model_management.py:2063` → `free_memory(1e30, device)` for each
     device.
   * `e.reset()` → `execution.py:672-675`, rebuilds the `CacheSet`.
   * then `gc.collect()` and `soft_empty_cache()`, with `last_gc_collect = 0` forcing the 10 s
     gate open immediately.
4. `free_memory` (`model_management.py:863-907`) calls `soft_empty_cache()` at line 901 whenever
   anything was unloaded.
5. `soft_empty_cache` (`model_management.py:2045-2061`) on CUDA runs
   **`torch.cuda.synchronize(); torch.cuda.empty_cache(); torch.cuda.ipc_collect()`**,
   unconditionally. `force` is irrelevant on this path.

**So `torch.cuda.empty_cache()` is definitely called — twice, and promptly.** Of the four candidate
explanations put to this investigation, three are ruled out:

* *"Not called on that path"* — **false**, established above.
* *"The next render had already begun allocating"* — **false here.** The measurement came from
  `tests/measure_h3_attention.py`, which waits `FREE_SETTLE_SECONDS = 8.0` and runs with an empty
  queue between arms.
* *"Consumed only between prompts, reading mistimed"* — **false.** `set_flag` notifies the waiter;
  the idle worker falls straight through to the flag block.
* *"Emptied, but a reference is still held"* — **partly, and it is the wrong shape.** The raw
  record settles it. `test-artifacts/2026-08-22-h3-freed-226/records/turbo-references2v+default-f226-r1.json`,
  key `free_before_arm.freed`:
  `{"comfy_vram_free_gib": 14.25, "host_ram_free_gib": -9.66, "vram_used_mib": 25.0}`, and the
  f158 sibling `{"comfy_vram_free_gib": 15.65, "host_ram_free_gib": -8.35, "vram_used_mib": -4.0}`.

`vram_used_mib` is the **delta** in `nvidia-smi`'s `memory.used`. **`/free` moved 14–15 GiB in
torch's accounting and returned 25 MiB — and on the second call, −4 MiB — to the device.** Not
"most of it stayed"; essentially none of it moved. Held references would have shown up as still
*active*; instead the memory went from active to free-**within**-the-reserve. Cross-check the
arithmetic against `get_free_memory`: `mem_free_torch = mem_reserved - mem_active`. Unloading
dropped `mem_active` by 14.25 GiB, so ComfyUI's `vram_free` rose by 14.25 GiB — while
`mem_reserved` did not move, so `nvidia-smi` did not move. That is the whole result.

**The reserve is what never comes back.** `LoadedModel.model_unload` (`:805-816`) calls
`model.detach()` / `partially_unload(self.model.offload_device, …)` — the weights are **moved to
CPU, not discarded**, which is precisely the measured −8 to −12 GiB of host RAM. The VRAM blocks
they vacate return to torch's cache, and `empty_cache()` then fails to hand the cache back to the
driver. The launcher is `run_nvidia_gpu.bat`, confirmed live: PID 15072's command line is
`.\python_embeded\python.exe -s run_comfyui_natten.py --windows-standalone-build
--use-sage-attention --fast`, and that .bat sets **`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`**
(added 2026-08-19 to fight this same VRAM creep). Under expandable segments, allocations of a
size class live in one large virtual reservation whose physical pages can only be released from
the top down; a single live tensor high in the segment pins everything beneath it. Combine that
with a card whose driver-level free memory is already zero and there is nothing for `empty_cache`
to give back.

**Consequence, and it is the answer to the crux question the Director asked to have settled first:
`empty_cache()` is already being called and it already returns ~25 MiB. A node that calls it will
return ~25 MiB too.** Nothing about wrapping the same call in a graph node changes what the call
does.

### 9.5 The installed-node survey — what each one really calls

Surveyed `custom_nodes/` for anything that frees memory, reading call bodies rather than names.
Four packs register such a node.

| node class | pack / file | reaches a real `empty_cache`? | scope | output node | passthrough |
|---|---|---|---|---|---|
| `VRAM_Debug` | KJNodes `nodes/nodes.py:613-658` | **yes** — `mm.soft_empty_cache()`, toggle `empty_cache` default True | core `current_loaded_models` | **no** | `any_input`/`image_pass`/`model_pass` |
| `easy cleanGpuUsed` | Easy-Use `py/nodes/logic.py:1415-1430` → `py/libs/utils.py:283-300` | **yes** — `mm.unload_all_models()` then `mm.soft_empty_cache()`, no toggles | core + Easy-Use private cache | **yes** | `anything` → `output` |
| `Trellis2UnloadAllModels` | Trellis2 `nodes.py:5400-5455` | **yes, hardest** — `soft_empty_cache()` + `torch.cuda.empty_cache()` + `ipc_collect()` + 2×`gc.collect()`, plus a forced `model_unload(1e32)` loop, RAM unpin, and clearing `current_loaded_models` and `controlnet_loaded_models` | core, forcibly | **yes** | `input_1` (`*`) → `output_1` |
| `ReActorUnload` | reactor-node `nodes.py:1614-1629` | **no** — `del`s ReActor's own globals only | private | no | IMAGE |

Non-node routes to the same thing: rgthree's `purge_vram()` callable inside `Power Puter`
(`py/power_puter.py:63-74` — `gc.collect()` + `torch.cuda.empty_cache()` + `ipc_collect()` +
`unload_all_models()` + `soft_empty_cache()`); Easy-Use's HTTP `POST /easyuse/cleangpu`
(`py/routes.py:24-30`); and ComfyUI-Manager's "Unload Models" button, which is just core `/free`
(`js/comfyui-manager.js:1581-1612`) — category (b), no better than what §6.21 already measured.

**Which of them beat `/free`? None of them, materially.** Three reach `torch.cuda.empty_cache()`,
but §9.4 establishes that `/free` reaches it too and gets 25 MiB back. Trellis2's node is the only
one that does anything `/free` does not — it clears the registry outright and unpins host RAM —
and there is no evidence that changes the device-level result. The one real defect it would fix is
a subtlety worth recording: `unload_all_models()` alone does **not** guarantee an allocator flush
(`free_memory` calls `soft_empty_cache()` only when something was evicted, or when torch's free
reserve exceeds 25% of total), which is why the well-written nodes append their own
`soft_empty_cache()`. On the `/free` path the worker's own `gc` block appends it anyway.

KJNodes' `VRAM_Debug` additionally has an ordering quirk: it empties the cache **before**
unloading, so with both toggles on the memory the unload frees is not flushed by that node.

Also worth knowing, because they fire as side effects mid-graph: WanVideoWrapper's loader and
sampler call `unload_all_models()` + `soft_empty_cache()` (`nodes_model_loading.py:1120-1122`,
`nodes_sampler.py:877-879`), as does HunyuanVideoWrapper (`nodes.py:311-312`, `:1440-1442`); both
also remove their own patcher from `current_loaded_models`, hiding it from core accounting.
None of the `MiniMaxH3`/`Spectrum` packs contain any memory-freeing node.

### 9.6 Can an end-of-workflow unloader work? Yes mechanically, no usefully.

Honest answers to each half of the Director's proposal:

* **Can a node be made to run last?** Yes. `easy cleanGpuUsed` and `Trellis2UnloadAllModels` are
  both `OUTPUT_NODE = True` with an `any → any` passthrough, so wiring
  `VHS_VideoCombine.filenames → cleanGpuUsed.anything` forces it after the video is written.
  Output-node ordering is otherwise not guaranteed; the wire is what guarantees it.
* **Can a node free the model the prompt is still using?** For the H3 graph, yes — by the time
  `VHS_VideoCombine` runs, sampling and both VAE decodes are done and nothing further needs the
  weights. `unload_all_models()` at that point is a proven pattern; WanVideoWrapper does exactly
  this at sampler start.
* **Does it free anything `/free` does not?** No, and this is the load-bearing point. It calls the
  same `unload_all_models()` and the same `soft_empty_cache()` → `torch.cuda.empty_cache()`, which
  this project measured returning 25 MiB.
* **Does the freed memory survive to the next prompt?** In torch's bookkeeping, yes. On the device,
  there is nothing to survive. And **only for the models this graph loaded** — it cannot touch the
  ~5 GiB held by Discord, the NVIDIA Overlay, dwm, the browsers, or LM Studio, and those are the
  processes that make the card oversubscribed.
* **Would it help?** **No — the evidence says it would hurt.** §6.21 ran the equivalent: a 226-frame
  render immediately after a `/free` cost 1004 s against 804 s occupied, 24.9% slower, at
  125.53 s/it. §9.2 says why: unloading manufactures exactly the break in residency that precedes
  every slow render in the log. A batch that unloaded after every shot would hand the card back to
  the other tenants between shots and turn every render into a first-render-after-idle.

The proposal is a well-aimed answer to the wrong diagnosis. The card is not slow because ComfyUI
holds too much; it is slow because ComfyUI cannot hold enough.

### 9.7 The reproducibility trade — already spent

§8.9's follow-up established that a fixed seed reproduces **bit-exactly** while the model stays
resident and re-rolls across a reload. So unloading between renders would, in principle, cost
seed reproducibility for every take.

**In this configuration that price has already been paid in full, by the model sizes.**
`load_models_gpu` logs `Requested to load X` **only** when X is absent from
`current_loaded_models` (`model_management.py:945-948` — the already-resident branch logs nothing).
The batch log shows `Requested to load MiniMaxH3TEModel_` and `Requested to load MiniMaxH3` at the
head of **every single prompt**. They cannot coexist: the text encoder is 25883.83 MB and the
transformer is 19996.14 MB, 45.9 GB against a 30.4 GB reserve, so each one evicts the other every
render, and the VAEs (577.08 + 4966.19 MB) sometimes evict the transformer in turn. That mutual
eviction is the flat 70–101 s of §9.1 — roughly 46 GB of PCIe traffic per render.

So: **an unloader would cost no additional reproducibility, because H3 takes in this batch are
already not reproducible by seed.** That is not an argument for building it. It is the correct
statement of the trade, and it is also a standing caveat that belongs on any future H3 A/B: the
§6.19 rule "discard an arm that re-executed `mvp:model`" would void **every** H3 arm at these
model sizes. Only the restart path or a smaller bundle can restore bit-exact H3 reproduction.

### 9.8 What to do — and what only a restart can do

Ranked by measured value, and none of it is code:

1. **Take the other tenants off the card.** ~5.1 GiB measured across Discord, `discord_clips`,
   the NVIDIA Overlay, dwm, Opera, Brave, Edge/WebView2 and Windows Terminal, plus LM Studio's
   context. The card is oversubscribed by about 3 GiB; this is more than enough to clear it, it
   is free, and it needs no restart. The existing `MVP_LLM_EJECT_BEFORE_RENDER` eject
   (`src/music_video_producer/vram.py`) already handles LM Studio and nothing else.
2. **Restart ComfyUI between batches, not between shots.** This is the one thing that has ever
   produced a clean card (2233 MiB, §6.19) — because only process exit destroys the CUDA context
   and returns torch's reserve to the driver. It costs one cold reload per batch, ~80–100 s, which
   the batch is already paying on every render anyway. **The Director can automate this; agents
   must not (AGENTS.md:10).** If the honest conclusion has to be one line, it is this one: nothing
   inside a running ComfyUI process gives the card back, so the only lever that works is process
   lifetime, and it belongs at batch boundaries.
3. **Do not build the unloader node.** If it is built anyway, build it as an experiment arm and
   not as a default, and expect §6.21's result.
4. Leave `expandable_segments:True` alone unless it is measured. It was added for this exact
   symptom on 2026-08-19 and has never been A/B'd; changing it is a separate experiment.

### 9.9 SPECIFIED, NOT RUN: does clearing the card's other tenants remove the tail?

**Not run. 22 renders were pending when this was written and nothing was submitted.** Run it only
on the Director's explicit authorisation, after the batch drains.

> **Arm A was subsequently run on 2026-08-23 22:15 — see §9.10. Arm B was NOT run and the
> experiment is not decided.** §9.10 also invalidates one half of this section's decision rule
> before Arm B is attempted: mid-render Shared Usage was ~3.2 GiB on *every* Arm A render while
> every one of them sampled at the floor, so nonzero spill is **not** sufficient for the tail and
> "arm B's Shared Usage stays at 0 MiB" cannot be carried as a condition. Read §9.10 before
> running Arm B.

**Question.** Is the heavy tail in `seconds_per_step` caused by VRAM oversubscription from
non-ComfyUI tenants? Not the median — the median is already at the 15–21 s/it floor in every
arm. The tail is the entire cost.

**Arms.** Fixed shot, fixed seed, `turbo-references2v`, 8 steps, **141 frames** (the modal size and
the middle of the flat per-frame region).

* **Arm 0 — discarded.** One warm render immediately after the batch drains, not scored. §6.19's
  rule; the first arm is always cold.
* **Arm A — as-is.** Desktop exactly as it is today: Discord and `discord_clips` running, NVIDIA
  Overlay running, Opera/Brave/Edge open, LM Studio resident.
* **Arm B — cleared.** Discord (all three processes), `discord_clips`, NVIDIA Overlay, all
  browsers closed; LM Studio ejected via the existing `lms unload --all` path. dwm cannot be
  closed and stays in both arms.

**Design.** `A×5, B×5, A×5` — 15 scored renders plus the discard, ~1.5 h at the floor and longer
if the tail appears. A block design is unavoidable because the treatment is opening and closing
desktop applications; the trailing A block is the price of that, and it is what detects drift and
re-tests the tail. Do **not** average across blocks that disagree — §6.19: an anomaly is re-run,
never explained.

**Measured per render** — `tests/measure_h3_attention.py` already records all but the last two:

* `seconds_per_step` — **the primary**
* `sampling_seconds`, `execution_seconds`, `non_sampling_seconds` — the reload term, which should
  stay flat at 70–101 s in both arms and confirms the §9.1 decomposition
* `power_w_median`, `power_w_max` — the diagnostic: ~470–520 W compute-bound, 150–280 W at high
  utilisation memory-starved
* `state_before` / `state_after`: `vram_used_mib`, `comfy_vram_free_gib`, `host_ram_free_gib`,
  temperature, SM clock, active throttle reasons
* `execution_cached` on every arm (§6.19 rule 2), noting that at these model sizes every H3 arm
  will show a reload — record it, do not void on it
* **new:** `\GPU Adapter Memory(luid…0x14322…)\Shared Usage` sampled mid-render alongside power.
  Nonzero shared usage is the direct signature of WDDM spill and is the mechanism this experiment
  is actually testing.
* **new:** `\GPU Process Memory(*)\Dedicated Usage` for the 5090's LUID, once per arm, to record
  what the other tenants were actually holding.

**Decision rule, fixed before the run.**

* **Justifies the change:** arm B's median `s/it` is at or below arm A's, **and** arm B produces no
  render above **25 s/it** while arm A produces at least one, **and** arm B's mid-render Shared
  Usage stays at 0 MiB while arm A's is above 0. Then the recommendation is operational — keep the
  card single-tenant during batches — and no code changes.
* **Kills the diagnosis:** arm B still throws renders above 25 s/it with Shared Usage at 0. Then
  the tail is not tenancy, §9.2's recovery curve needs another explanation, and this section
  reopens.
* **Ambiguous:** the tail appears in neither arm. Then the batch drained into a quiet machine and
  the experiment did not reproduce the condition; say so and re-run when the tail is observable,
  rather than reporting a clean result from a run that could not have shown a dirty one.

**Optional Arm C, only if the Director wants the unloader closed empirically:** five renders with
`easy cleanGpuUsed` wired from `VHS_VideoCombine.filenames`. §6.21 predicts no better and probably
worse. It costs five renders to end the question permanently, and that may be worth it.

**Constraints carried from this section's own run.** No ComfyUI restart from an agent. No `/free`
anywhere in the harness (`--free-between-arms` stays off — it changes what is being measured).
`data/projects/project_59f14d19ff10` digested before and verified byte-identical after.

##### Method notes for §9

* **Nothing was submitted, restarted, or interrupted.** The batch ran throughout — `/queue` showed
  1 running and 22 pending. Only `/system_stats`, `/queue`, `nvidia-smi`, Windows perf counters,
  and file reads were used.
* **No code was changed** and nothing was committed.
* **Primary sources**, all read directly:
  `J:/…/ComfyUI/main.py:329-421`, `server.py:685-716`, `server.py:1192-1200`,
  `execution.py:672-675`, `:1251-1279`, `:1399-1411`,
  `comfy/model_management.py:741-830`, `:863-907`, `:909-1011`, `:1748-1790`, `:2045-2066`,
  `ComfyUI_windows_portable/run_nvidia_gpu.bat`, `run_comfyui_natten.py`,
  and `J:/…/ComfyUI/user/comfyui.log` (session opened 2026-08-22 14:09:57, still running).
* **The `discord_clips` counter is not evidence.** 13.6 million MiB on a 32 GiB card, stable across
  two samples. Every other per-process figure cross-validates against torch's own reserve to
  within 61 MiB.

---

### 9.10 MEASURED 2026-08-23: Arm A only. The tail did not reproduce, and the spill signature was present the whole time anyway.

**Status: ARM A RUN AND SCORED. ARM B NOT RUN — its treatment is closing the Director's own
applications, which is theirs to do. No code changed, no commit, no `/free`, no restart, nothing
written to `data/projects/project_59f14d19ff10`.**

**The headline, stated the way §9.9 asked for it in advance: no Arm A render exceeded 25 s/it.
The five landed at 18.72–20.14 s/it, a 7.6% spread, every one inside §9.1's 15.1–21.5 s/it floor
band. The pathology this experiment exists to treat was not happening while Arm A ran, so Arm A
could not have demonstrated that anything cures it.** That is §9.9's "ambiguous" outcome, named
before the run, and it is a result rather than a failed run.

It is not, however, an empty one. Arm A was instrumented for the two counters §9.9 added, and
what they show contradicts the mechanism §9.9 assumed.

#### The tenancy baseline

Read the moment the Director's queue drained (2026-08-23 21:45, card idle), and again after Arm A
finished (22:16, card idle). Per-process `\GPU Process Memory(*)\Dedicated Usage` for the 5090's
LUID `0x00014322`, and `\GPU Adapter Memory(…0x14322…)\Shared Usage`.

| | 21:45 idle, pre-run | 22:16 idle, post-run |
|---|---|---|
| **Adapter Shared Usage** | **778.4 MiB** | **365.9 MiB** |
| ComfyUI (`python`, pid 15072) | 648.6 MiB | 26197.9 MiB |
| Discord (pid 51400 / 27476) | 2775.0 + 47.3 | 2861.3 + 72.1 |
| NVIDIA Overlay (16196 / 18376) | 1696.2 + 16.3 | 1696.2 + 16.3 |
| opera | 476.5 | 663.5 |
| dwm | 432.2 | 434.3 |
| brave | 192.2 | 337.6 |
| WindowsTerminal | 57.7 | 57.7 |
| msedgewebview2 / msedge | 50.9 / 16.1 | 50.9 / 23.1 |
| explorer | 33.0 | 66.5 |
| **LM Studio** | **14.1** | **14.1** |
| System / csrss | 4.3 / 3.9 | 4.3 / 19.1 |
| **non-ComfyUI total** | **5815.7 MiB (5.68 GiB)** | **6317.0 MiB (6.17 GiB)** |
| host RAM free | 14432 / 63080 MiB | 14988 / 63080 MiB |
| `nvidia-smi` compute contexts | — | 32 processes |

* **`discord_clips` (pid 38436) read 13,731,280 MiB pre-run and 15,303,477 MiB post-run.** Broken,
  as §9 already flags, and now confirmed to be *incrementing* rather than stable. Discarded from
  every total above.
* **The other tenants are heavier than §9.3 measured, not lighter: 5.68–6.17 GiB against ~5.1 GiB.**
  Nothing was closed for Arm A and nothing needed to be — this is the desktop exactly as the
  Director has it.
* **LM Studio held 14.1 MiB and had no model loaded.** `lms ps` answered "No models are currently
  loaded" during the run. **This matters for Arm B:** ejecting LM Studio is one of Arm B's named
  treatments and on today's machine it would free approximately nothing. Arm B's real treatment is
  Discord (2.9 GiB) and the NVIDIA Overlay (1.7 GiB); those two are three quarters of the tenancy.

#### The five renders

One session, consecutive, warm, no `/free`, no restart, nothing interrupted. `turbo-references2v`,
8 steps, `default` attention (inherited `--use-sage-attention`), 141 frames, shot
`shot_f82bf42e3abc` of `project_59f14d19ff10`, window 66.33–72.205 s, lead 0.25 s.

**Discarded cold warmup**, 107 frames, submitted 21:48:28 into an idle card: 191.76 s execution,
~96 s of model load, **10.69 s/it**. Not scored.

| # | seed | s/it | sampling | comfy exec | non-sampling | W median | W max | VRAM before | comfy vram free | host RAM free | temp before → mid max | SM mid max | throttle | cached nodes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 20260822 | **18.82** | 150 s | 164.17 s | 14.17 s | 457.3 W | 563.4 W | 28764 MiB | 3.04 GiB | 8.51 GiB | 49 → 81 °C | 2850 MHz | 0x0 / 0x4 | 14 |
| 2 | 20260823 | **19.04** | 152 s | 243.45 s | 91.45 s | 455.1 W | 567.7 W | 8158 MiB | 25.25 GiB | 26.38 GiB | 63 → 81 °C | 2895 MHz | 0x0 / 0x4 | 1 |
| 3 | 20260824 | **20.14** | 161 s | 240.48 s | 79.48 s | 432.1 W | 568.8 W | 8681 MiB | 24.68 GiB | 26.54 GiB | 62 → 79 °C | 2895 MHz | 0x0 / 0x4 | 4 |
| 4 | 20260825 | **19.29** | 154 s | 225.86 s | 71.86 s | 454.8 W | 567.3 W | 8700 MiB | 24.68 GiB | 24.31 GiB | 63 → 83 °C | 2887 MHz | 0x0 / 0x4 | 4 |
| 5 | 20260826 | **18.72** | 149 s | 162.76 s | 13.76 s | 464.4 W | 573.3 W | 28762 MiB | 3.20 GiB | 15.00 GiB | 63 → 82 °C | 2842 MHz | 0x0 / 0x4 | 14 |

**Median 19.04 s/it. Maximum 20.14 s/it. Nothing above 25.** `0x4` is SW Power Cap and appeared
mid-render on every arm: the card was *power-limited*, which is the compute-bound regime, not the
memory-starved one.

A sixth render exists and agrees. Before this table was produced, the same shot rendered at seed
20260821 through `tests/measure_h3_attention.py` at **19.18 s/it**, 247.64 s execution, 94.64 s
non-sampling, 456.0 W median / 570.0 W max. It is reported separately only because the run it came
from turned out to be measuring the wrong thing after its first render — see the caching finding
below — not because anything about that render is in doubt.

#### The two new counters, and what they refute

`\GPU Adapter Memory(…0x14322…)\Shared Usage` and per-process `Dedicated Usage` were sampled every
~7 s across the whole session, 190 samples, and joined to each render by ComfyUI's own execution
span.

| # | Shared Usage min / **median** / max, mid-render | ComfyUI dedicated max | other tenants median | memory-controller util median / max |
|---|---|---|---|---|
| 1 | 340.3 / **3282.0** / 3404.4 MiB | 29305.6 MiB | 6080.3 MiB | 28% / 46% |
| 2 | 337.5 / **3214.9** / 5057.1 MiB | 29389.5 MiB | 6094.0 MiB | 30% / 58% |
| 3 | 344.5 / **3217.6** / 4992.1 MiB | 29389.5 MiB | 6084.0 MiB | 26% / 48% |
| 4 | 340.0 / **3285.2** / 4922.7 MiB | 29325.6 MiB | 6247.3 MiB | 29% / 57% |
| 5 | 334.7 / **3282.4** / 3303.6 MiB | 29305.6 MiB | 6264.9 MiB | 31.5% / 59% |

**Every scored render ran with ~3.2 GiB of GPU memory spilled into system RAM, and every one of
them sampled at the floor.** The one-second trace through render 3 makes the shape unmistakable:
Shared Usage sits at ~350 MiB while the models load, jumps to ~3.2 GiB at the *first sampling
step*, and then holds within ±30 MiB for the entire 161 s of sampling while the card draws
427–466 W at 99% utilisation with the memory controller between 15% and 48%.

```
22:05:58  shared  352 MiB  comfy  29182  others 6247  vram 31804  100 W  gpu  20%  mem  5%   (loading)
22:06:05  shared 3359 MiB  comfy  29262  others 6221  vram 31852  420 W  gpu  99%  mem 42%   (sampling)
22:06:19  shared 3209 MiB  comfy  29389  others 6062  vram 31821  438 W  gpu  99%  mem 26%
…24 samples, shared 3190–3225 MiB throughout…
22:08:33  shared 3220 MiB  comfy  29389  others 6080  vram 31839  454 W  gpu  99%  mem 27%
22:08:40  shared 3225 MiB  comfy  26480  others 6082  vram 23065  118 W  gpu   9%  mem  1%   (decode)
```

29389 + 6080 = **35.5 GiB requested on a 32.6 GiB card** — §9.3's arithmetic, reproduced almost
exactly, and the ~2.9 GiB overdraft matches the ~3.2 GiB that WDDM put in system RAM to within
the counter's own noise. **The oversubscription is real, it is measured, it is slightly worse than
§9.3 found it, and it cost 7.6% of run-to-run spread and nothing else.**

**So the direct signature §9.9 nominated is not sufficient for the tail.** §9.9's "justifies the
change" branch required arm B's Shared Usage to reach 0 MiB while arm A's stayed above it. Arm A's
never went below 334 MiB and spent every sampling second at 3.2 GiB, at the floor. That condition
cannot discriminate anything and must be struck from the decision rule before Arm B runs. What
distinguished §9.3's pathological render was never the spill on its own — it was **137–170 W with
the memory controller at 1–4%**. Arm A today was 432–464 W with the memory controller at 26–31%.
Two different regimes, one identical spill.

#### Two method findings, both of which change how the next run must be built

**1. `--repeats N` on an unchanged payload does not produce N renders. It produces one render and
N−1 cache hits.** The first attempt at Arm A ran `measure_h3_attention.py … --repeats 5`. Repeat 1
sampled normally at 19.18 s/it. Repeats 2, 3, 4 and 5 returned in **1.23, 1.22, 1.24 and 1.34
seconds** at **74.5–84.6 W**, with no tqdm summary in their log windows at all — ComfyUI's
execution cache served the sampler's latents and only the video mux re-ran. The harness recorded
them dutifully as `0.009 s/frame (whole-render)`, and its own audio comparison returned the
identical `correlation 0.963, lag_ms 0.1` for all five because all five files came from one
sampling pass.

Nothing in the harness is wrong; the assumption that repeats are renders is. **Any future repeat
design at a fixed frame count must vary the seed**, which busts the cache without changing a
single cost term. This run used a fixed, written-down seed list — **20260822, 20260823, 20260824,
20260825, 20260826** — and **Arm B must render those same five seeds in that same order**, or the
two arms are not comparable.

This also puts a caveat on §6.19's rule 2 in the other direction. `execution_cached` was recorded
on every arm here, as §9.9 asked: 14, 1, 4, 4, 14 nodes. A *large* cached-node count is not proof
an arm was cheap-and-fine — at 14 nodes cached the sampler still ran and only the text-encode was
reused — but a count that leaves the sampler cached produces a 1.2 s "render". Read the tqdm line,
not the node count.

**2. §9.1's "flat 70–101 s" model-load term is a property of that batch, not a law.** Here it was
**13.76, 91.45, 79.48, 71.86 and 13.76 s** — a 6.6× spread, because renders 1 and 5 reused a
resident text encoder and renders 2–4 paid the `MiniMaxH3TEModel_` ↔ `MiniMaxH3` mutual eviction
§9.7 describes. **`seconds_per_step` moved by 7.6% across that 6.6× swing**, which is the cleanest
confirmation this document has of §9.1's central claim: the sampler's cost and the loader's cost
are independent, and only the first of them is the tail.

#### What this settles, and what it does not

* **Settled: the tail is not a constant.** The card was oversubscribed by ~2.9 GiB, spilled ~3.2
  GiB over PCIe, ran 32 CUDA contexts and 6.2 GiB of foreign tenants, and produced five renders
  inside a 1.4 s/it band. Oversubscription alone does not produce the tail.
* **Settled: nonzero Shared Usage is not the tail's signature.** It is the *overdraft's* signature,
  and the overdraft was continuously present and continuously harmless here.
* **Not settled, and untouched: whether clearing the tenants removes the tail.** The tail was not
  available to be removed. Arm B run now would compare a floor against a floor.
* **Weakened, not refuted, is §9.2's recovery curve.** Every §9.2 outlier followed a break in the
  render stream. Arm A had exactly one break — the 15-minute idle after the Director's 21:44
  cancellation — and the render that followed it was the *warmup*, at 107 frames and 10.69 s/it,
  the fastest per-step figure in this session. One cold render is not a test of that hypothesis,
  but it is the first datum that runs against it.

#### What Arm B should be, when the Director is ready

**Do not run Arm B against today's machine as a speed test.** It would return "no difference"
from two floors and that result would be worthless — worse than worthless, because it reads as a
refutation.

Two things are worth doing instead, in this order:

1. **Run Arm B as a *spill* test, not a speed test.** The question it can still answer cheaply is
   mechanical: does closing Discord and the NVIDIA Overlay (4.6 GiB of the 6.2) actually take
   Shared Usage to zero, or does WDDM spill anyway? That is one arm of five renders and it
   calibrates the instrument for whenever the tail next appears. Its decision rule is
   Shared-Usage-only; s/it is recorded and is not the verdict.
2. **Catch the tail in the wild.** The tail is a property of the Director's real batch conditions —
   25 prompts dumped in four seconds after an idle gap — not of a five-render probe. The cheapest
   route to an answer is to leave the Shared-Usage sampler running across the Director's *next*
   batch and read what the counters say during a render that actually goes long. That costs no GPU
   minutes at all.

##### Method notes for §9.10

* **The Director's queue was drained before anything was submitted.** `/queue` was polled to 0
  running / 0 pending. Their last render was **cancelled by them** at 21:44:47 (`Cancelling running
  prompt c22e2196…`, `Prompt executed in 00:14:25`, interrupted), along with seven pending prompts
  at 21:41. Nothing here interrupted, cancelled, cleared, restarted or `/free`d anything.
* **The project is untouched, verified by digest either side.** `project.json`
  `915c7f60f833329d6ed4f8de348a35e1ebe6846be25daee9569694a816d6e9ba`, three-file aggregate
  `0638531beba551e2c311cd8875f5ac3355ce00099ce456085465229a650f67d4` — identical before and after.
  (An earlier digest at 21:45:10 differed; it was a torn read taken *during* the application's own
  write of the cancelled render's job record, and the file settled to the value above and stayed
  there. The application writes this manifest; this run did not.)
* **No code was changed.** `tests/measure_h3_attention.py` aborts on this project because its
  `build_references` resolves every cited asset under the project directory, while
  `app.resolve_asset_path` (`src/music_video_producer/app.py:7098`) resolves by `Asset.source` —
  and `shot_f82bf42e3abc` cites one `krea-multiview` asset that lives under ComfyUI's `output/`.
  Rather than edit the harness, a scratchpad script imported it as a module and rebound that one
  module-global to a resolver applying the route's own rule. Payload construction, submission,
  timing, log-window attribution and every recorded field are the audited harness's own functions.
  **This is a real gap in the harness and it is not fixed** — see the handover below.
* **All artefacts are in the session scratchpad, not in the repo**, because the manifest and the
  rendered takes carry the song's copyrighted lyrics: `armA/` (the first, cache-poisoned attempt),
  `armA-seeds/` (the five scored renders and their per-render ComfyUI log windows),
  `telemetry.jsonl` (190 counter samples), `armA-joined.json`, `baseline-tenancy.json`,
  `final-tenancy.json`.
* **One confound I cannot rule out, stated rather than assumed away.** Another agent ran the full
  pytest suite — about two minutes of heavy CPU — around 21:48, which overlaps the discarded warmup
  and possibly the seed-20260821 render. The five scored renders ran 21:58:01–22:15:30, after all
  other CPU-heavy work in this session was held. **I did not sample host CPU utilisation, so I
  cannot confirm the scored window was clean from my own instrumentation** — only that nothing was
  scheduled into it.
* **`nvidia-smi` reported 32 processes holding a CUDA context on this card**, matching §9.3's
  "roughly thirty".
