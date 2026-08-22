# H3 attention backend: a specified experiment

**Status as first written (2026-08-21, §1–§5): SPECIFIED, NOT RUN, no adapter changed.**
**Status now (2026-08-21, §6 onward): BUILT AND RUNNING.** The capability landed, the default
is digest-proved unchanged, the pre-flight covers every profile, and the harness is
`tests/measure_h3_attention.py`. **§6 corrects the premise §1–§5 were written on**, so read §6
before acting on anything above it.

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
