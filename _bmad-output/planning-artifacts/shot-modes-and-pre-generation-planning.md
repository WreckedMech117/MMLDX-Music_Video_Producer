# Shot Modes and Pre-Generation Planning

**Status:** direction accepted 2026-08-18. Not yet decomposed into stories.
**Origin:** the Director's per-mode API exports of 2026-08-17 and the direction set out on 2026-08-18.

## The shape of the change

Until now a Shot has been a window with a prompt, and rendering it meant one of two paths — text-only, or reference-driven. The Director's per-mode exports show that the generator families support a **taxonomy of shot kinds**, each with its own asset arity, its own duration behaviour and its own adapter. That taxonomy is the organizing idea this document exists to record: **`Shot.mode` is the thing everything else hangs off.** Asset requirements, duration bounds, which adapter runs, what the assistant needs in order to help, and what the timeline draws for that section all follow from it.

The second idea is that **planning is where the confidence comes from.** The goal is not to make rendering cheaper; it is to make the moment of firing a whole plan uneventful, because everything it will do was visible beforehand. A Director should be able to scroll the timeline against the music, see what each section will be built from, fix what is wrong, and only then commit GPU.

## The modes, grounded in the exports

Each row was read from the reachable subgraph of the named export, not from its title.

| Mode | Source graph | Assets it takes | Notes read from the file |
|---|---|---|---|
| Text-to-video | `MiniMaxH3 T2V`, `LTX2.5 T2V` | none | The B-roll case. H3 routes through `MiniMaxH3ImageToVideo` with no image; LTX takes audio and prompt only |
| Image-to-video | `LTS2.5 I2V` | 1 image | `LTXVImgToVideoInplace` applied twice — strength 1 at base, 0.7 after upsample |
| First / last frame | `MiniMaxH3 I2V-FLframe` | 2 images | H3's own first-last control |
| First / middle / last | `LTX2.5 FML` | 3 images | `LTXVFirstLastFrameControl_TTP` (first and last strength, applied at two stages: 1.0 then 0.5) plus `LTXVMiddleFrame_TTP` at `position 0.51`, `strength 0.35`. **The middle frame's position is a parameter, not a fixed centre** |
| References to video | `MiniMaxH3 References2V` | up to 9 images, 3 videos with paired audio, 3 audios | Already adapted and rendered live |
| Image editing | `MiniMaxH3 ImageEditing` | reference image(s) | Runs `MiniMaxH3ReferenceToVideo` at **`length: 5`** with `ref_image_size: "max"` — it is a five-frame reference render, not a separate image pipeline. Worth knowing before designing a UI around it |
| Extend | `LTX2.5 VideoExtender` (+ `NoAudio` variant) | an existing video | `LTXVAudioVideoMask` with `max_length: "pad"`, `existing_mask_mode: "add"`. This is the escape hatch for a section longer than a generator's ceiling |
| Enhance | `LTX2.5 VideoEnhancerUpscaler` | an existing video | Already specified; see `spec-ltx25-enhancer-adapter.md` |
| Slice / replace audio | `LTX2.5 VideoSlicer`, `LTX2.5 AudioReplacer` | an existing video | Small utility graphs; both carry orphaned loaders (see the reference-exports `MANIFEST.md`) |

## What follows from the taxonomy

**Asset roles, not just asset lists.** `Shot.asset_ids` is currently an ordered list with deterministic numbering (FR-19). A first/middle/last shot needs to know *which* image is the middle one, and a references shot needs to distinguish pictures from reference videos from reference audio. The ordering convention that serves references does not express a role.

**Assets recur across shots and that is the normal case, not an edge case.** A wolf, a location, one character in their verse and another in theirs, then both together for a duet. The library already stores assets per project; what is missing is the idea that a shot *cites* library assets in roles, and that the same asset is cited by many shots. Anything that copies assets into shots rather than referencing them will make a plan that cannot be revised.

**Duration bounds are per mode and must be enforced before the timeline lets a shot exist at that length.** The generators disagree: H3's reference node accepts 5–3600 frames on a 17k+5 grid; the LTX boundary measured an 8k+1 grid and did **not** preserve frame count (192 in, 185 out). A section shorter or longer than its mode allows is a broken render discovered after the GPU time, and the Director named this directly. The extend mode is what a section longer than a ceiling becomes, which means the bounds check has an answer rather than only a refusal.

**T2V sections have nothing to show and that is meaningful.** Clicking a B-roll section should say "this is text-only" rather than showing an empty asset tray that looks like a mistake.

## The assistant

The Director's framing is an in-application assistant — "Assistant ProducerBot" — running on the same LM Studio model, used as a **toolset** rather than only a conversationalist. The concrete interaction named: selecting a timeline section surfaces a control beside the chat that quick-fills the composer with whatever that shot's context is — enough for the model to target the right assets and the right mode.

Two things about this are worth fixing early, because they are easy to get wrong:

- **Quick-fill is a convenience, not a channel.** The text it writes should be the same text a Director could have typed. If the assistant needs structured shot context, that belongs in the request the route already builds, not smuggled into a message body that then also has to be human-readable.
- **A toolset means the assistant can change project state**, which is a different risk class from writing a treatment. Every existing Director write is either opt-in per turn (`apply_documents`, `apply_shots`) or refuses to touch render provenance. Whatever tools the assistant gets should inherit that, not start over.

## Impact on what is already recorded

**FR-4 and FR-5 already describe the generation controls.** "Submit every ready Shot as one Batch after a single cost confirmation" is Generate All; "regenerate a single Shot in place without touching any other Shot" is the per-clip Generate Video. The Director's addition is the **Replace Existing** toggle, which FR-4 and FR-5 do not currently express.

**`spec-arm-a-plan.md` is in direct tension and must be revisited.** It was written on the reasoning that arming a whole plan puts an hour of GPU one dialog away, and its frozen block says the action *must never queue anything*. The Director's counter-argument is that with real pre-planning the confidence is earned before the button is pressed, which is a different premise, not a disagreement about safety. That spec should not be implemented as written until the two are reconciled.

**Readiness gains a richer meaning.** `mark-ready` currently asks one question: does this shot have a prompt worth rendering? Under the taxonomy the question becomes "is this shot fully specified *for its mode*" — a first/middle/last shot missing its middle image is not ready, and no prompt check would notice.

**FR-17's 4–15 s window is one mode's bound, generalised.** It should become per-mode bounds with the extend mode as the answer above a ceiling.

**FR-11 is still unbuilt** — naming the loaded language model at render confirmation, informational and never a gate. It becomes more relevant with an assistant in the loop.

**Unaffected and still valid:** the LTX enhancer adapter spec, the browser QA owed on four controls, and every guarantee around refusals, recovery slots and context exclusion.

## Decisions taken 2026-08-18

**Asset roles live on the Shot's citation, not on the asset.** A shot holds entries of the shape `{asset_id, role, order}`, so the same library asset can be a reference in one shot and a last frame in another. The wolf is not "a middle frame"; it is a middle frame *in this shot*. This is what keeps assets reusable and makes a shot self-describing for its mode.

**The timeline constrains while dragging.** A shot's mode bounds what the Director can draw, snapping to the generator's frame grid during a resize, so a plan that cannot render is never built in the first place. This is the substance of the "polished pre-gen editor" feel — the constraint is the feature, not an obstacle to it.

**Generate All skips shots that already have a take, with an explicit Replace Existing toggle.** The expensive choice is the deliberate one.

**The assistant is tool-calling against real routes**, and the Director's reasoning is the important part of this decision rather than the mechanism:

> There are a lot of shots, and filling them out automatically but intelligently is something that could be done to help get the project initially filled out and a rough plan in place **after the initial assets and plan have been figured out**. If the system prompt for the initial setup agent is good and creative enough — knows it is a professional music video director/producer — then there is a good chance of good one-shot setups.

Two constraints follow from that framing and should not be lost:

- **The bulk fill runs after an asset library and a rough plan exist**, not from nothing. The assistant's job is to populate many shots against material that is already there, which is a far more tractable task than inventing the material. Sequencing this the other way round would ask the model to do the part it is worst at.
- **The system prompt is a deliverable, not a detail.** The Director is explicitly betting on one-shot quality coming from a well-written professional-director persona. That makes the prompt something to iterate against real output, not something to write once.

### The interaction the Director described, exactly

1. Click a shot on the timeline.
2. Click the prefill button beside the chat — the composer fills with that shot's context.
3. Ask in plain language: *"make that shot a B-roll of a grey wolf walking through a forest."*
4. The assistant expands the request into what the video generator prefers, and **updates that shot section**.
5. The Director then opens the shot and has an image generated — to aim at something specific, to get a preview, or to serve as the first frame, **whichever role they assigned it**.

Step 5 is the one that constrains the model: an image generated for a shot has a *role* chosen by the Director, and the same generated image is a preview or a first frame depending on that choice. It is not a separate "preview" concept.

## Still open

1. Does `Shot.mode` replace the current text-only/reference branch, or sit above it as a selector that chooses among adapters including those two?
2. Which routes does the assistant get as tools, and does every tool inherit the existing refusals (locked shots, render provenance, the prompt gate) or does it get a narrower set?
3. Does a bulk assistant fill produce one reviewable change-set, or write shot by shot as it goes?

## New capability asks, 2026-08-18

**Multiview promotion on objects — asked, and answered by running it, 2026-08-18. It works.** A Flux-generated cargo spaceship promoted through the existing Krea QuadView path produced a clean multi-view sheet: front, three-quarter, side profile and rear, with hull markings, panel detail and proportions consistent across every view. The same machinery that gives character consistency gives object consistency, with no model work required.

**The only thing preventing it is our own gate.** `generate_multiview` refuses with a 422 unless `source.kind == "character"`, so a `prop`, `setting` or `style` asset cannot be promoted at all. The probe was run by labelling the ship a character — a deliberate workaround to separate *our policy* from *the model's capability*, and the distinction turned out to be the whole answer. Widening the gate is a small change; the prompt also needs to stop being character-specific, since the shipped one says "the same person… identical face, hair, and wardrobe". Worth noting the run produced **six** panels rather than four despite the LoRA's name and a prompt asking for four, so nothing should assume a panel count.

**A reference sheet is one third of the creator's pipeline.** `Music-Video.md` in the Advanced set documents the Krea character sheet as **three** sampling stages — a 10-step euler layout pass at CFG 1, an 8-step euler refine at CFG 0.3, and an 8-step `res_multistep` final — plus a refine-prompt toggle to regenerate weak panels, at 44 nodes. Our adapter runs **only the first**: one `KSampler`, 10 steps, euler, CFG 1.0. Every reference sheet driving character identity through H3 is therefore a layout pass without its refine or finish. That is a quality gap in the input to everything downstream, and it is the likeliest single lever on reference fidelity. The creator also names 1 MP as the sweet spot for sheets; the adapter emits 1536×1024, which is 1.57 MP.

**The SongPlanner `max_duration` headroom rule is documented and not implemented.** `Music-Video.md`: *"max_duration: set this 50% longer than your target (60s lyrics → 90s max duration)."* `duration_seconds` tells the planner how long a song to write; `max_duration` caps the encoder's latent length. The adapter passes the same value to both, leaving no headroom, so a song whose lyrics run slightly long loses its ending. Both live SongPlanner runs were at the 30 s floor and returned 29.989 s, which is exactly where this would never show. Note honestly that the audited export *also* sets both to 200, so the creator's own example does not follow their stated advice — which is why this needs a decision rather than a silent fix.

**Workflow tuning is available.** The Director has offered to adjust ComfyUI workflows and export new API versions on request. That materially changes what is buildable: a graph that is nearly right no longer has to be worked around in the adapter. Where an adapter would otherwise have to reproduce awkward wiring or drop a capability, asking for a tuned export is now the better move.


---

## The H3 prompt format, found 2026-08-18 - ProducerBot is standing in for a model that was never released

The Director ran the assistant's first live smoke and judged its output "potentially sparce if it wants allot":

> A grey wolf pacing through trees under amber light from behind; 35mm lens, grainy texture.

That instinct was right, and the reason is stronger than sparseness. From `ComfyUI-Fantastic-MiniMaxH3-PromptBuilder`'s README, installed on this machine:

> **H3 doesn't want a casual sentence - it wants a structured prompt with named sections, shot timings, speaker IDs, and tags pointing at your reference media.** MiniMax publishes a written guide for that format, and normally a separate rewriting model (`H3-Context-IR`) turns your idea into it. **That rewriter wasn't open-sourced.** This node pack is the hand-driven replacement.

**So the assistant's real job is to be that rewriter.** Not to write a nicer sentence, but to produce a formally structured prompt in a documented format from a plain-language request. That is a much clearer job description than the spec had, and it is what "expands out that request as the video generator would prefer" actually means.

### The format, from MiniMax's own guide

The authoritative source is `Video_Prompt_Writing_Guide.pdf`, 20 pages, bundled at `custom_nodes/ComfyUI-Fantastic-MiniMaxH3-PromptBuilder/web/`. It is a third-party document and is **not** copied into this repository - what follows are the structural rules derived from it, which is what an adapter needs.

**Part one - an instruction line**, present only for the keyframe modes, always first, followed by one blank line. T2VA has none and begins directly at part two. I2VA, FL2VA and L2VA each have a fixed wording stating how each picture aligns to a time in the target video.

**Part two - three named core fields**, in this order:

| Field | Content |
|---|---|
| `integrated_multimodal_description` | Visuals, actions, shots, speakers, dialogue, singing and diegetic audio along the timeline |
| `overall_soundscape` | Ambient sound, physical action sounds and non-verbal human sounds across the whole video |
| `non_diegetic_music` | Score the characters cannot hear and only the audience can |

**Within the description:**

- `[Shot 1]` opens it and **must not carry a timestamp**. Later shots are `[Shot N] At MM:SS.mmm`, numbered in order with increasing cut times inside the video's length.
- **A line break reads as a shot boundary**, so only `[Shot N]` may introduce one. Dialogue joins the description it belongs to rather than sitting on its own line.
- Camera motion is written as motion type, amplitude and speed - "pushes in with small amplitude at slow speed", not "slow push in".
- Speakers are `(S1)`, `(S2)` in the target video's speaking order; dialogue is wrapped `<d>[English] ...</d>`.
- Reference media is tagged `<Picture 1>`, `<Video 1>`, `<Audio 1>`, `<Subject 1>`.
- Full-reference mode adds one or two style sentences before `[Shot 1]`, subject definitions, and a `retention_analysis` whose markers (`fully_preserved`, `partially_preserved`, `attribute_transfer`, `weak_reference`, `reference`, `partially_copy`) must match how each reference was defined.

**The five modes map onto ours**: T2VA, I2VA, FL2VA, L2VA and Reference - the same taxonomy `SHOT_MODE_SPECS` already declares, which is independent confirmation that the mode split is right.

### What follows

1. **`assistant_prompt.py` should teach the format**, and the assistant's output should be judged against it rather than against taste. This is the iteration the story shipped without.
2. **The format is checkable.** The node pack validates shot numbering, monotonic cut times, `[Shot 1]` carrying no timestamp, balanced `<d>` tags, and references cited but never defined. Those are the same class of guarantee this project enforces elsewhere, and they can be checked before a render rather than after a bad one.
3. **A cut time implies a length.** The pack snaps cut times to the 17k+5 grid the adapter already knows, so prompt structure and frame arithmetic are not independent concerns.


---

## Two passes, and a specialist per job - the Director's structure, 2026-08-18

Set out by the Director after the first live assistant run:

> The model was never released, fine, we understand well enough what it wants, so we could write an agent that has a sole task of replicating that prompt expansion. ProducerBot is just the one we are chatting with, specialized subagents and tools are in its box. [...] Given the length of these [...] that cant be done in one shot by one model in one context, rather it should be structured that when a shots prompt is generated the LLM knows enough context and awareness of the project and what shot it is working on to write out that shots prompt and then rinse and repeat for the next. Essentially the general shot plan which lays the shots out so they make sense and will flow together, then the expansion pass which goes through and fills in the detail.

### Pass one already exists

`POST /api/projects/{id}/director/expand` is a **single whole-plan call**, and `docs/LLM-DIRECTOR.md` states why in exactly these terms: *"per-shot calls cannot see each other, and cross-shot variance is the point."* That is the general shot plan - short intents, laid out so they make sense together and differ from one another. It is pass one, already built and already correct for the job.

The mistake would be to read the Director's "cannot be done in one shot" as a criticism of that route. It is not. **The two passes want opposite shapes**, and that is the insight:

| | Pass one - the plan | Pass two - the expansion |
|---|---|---|
| Call shape | One call, whole plan | One call **per shot** |
| Why | Cross-shot variance and flow require seeing the shots together | A single H3 structured prompt is long; thirty of them will not fit one context, and quality would degrade well before the limit |
| Output | A short intent per shot | The full `integrated_multimodal_description` / `overall_soundscape` / `non_diegetic_music` structure |
| Failure it prevents | Thirty shots that each read as the only shot | A prompt that is a sentence where the model wanted a document |

### The specialist, and what it must know

ProducerBot is the conversational surface; the expansion agent is a **specialist with one job** - turn a shot's intent into H3's documented format. It is the `H3-Context-IR` replacement, and its narrowness is the point: a model doing one well-specified transformation with a good system prompt is a far better bet than the same model doing that plus conversation plus tool selection.

The open design question is what a per-shot call must carry to keep continuity without paying for the whole plan. Candidates, in rough order of obviousness:

- The shot's own intent, window, mode, cited assets and roles, and whether the performer is singing.
- The treatment and style bible - the continuity contract already exists for this.
- **The neighbours' intents**, which pass one deliberately withheld from itself. Expansion withheld neighbour prompts because on a first pass they were all placeholders; on this pass they are real, and a cut that lands well needs to know what it is cutting from.
- The song's words **for this shot's window**, now that windowing exists.

That last pair is the interesting one: the same trimming discipline `expansion_input` established applies, but the trim is different because the job is different. A per-shot payload is not a smaller whole-plan payload.

### Where it is triggered

Three surfaces, all the same specialist:

1. **Per shot, on demand** - an "Expand prompt" control in the shot's text section, for a Director editing one shot.
2. **Across a plan** - the pass-two sweep, shot by shot, after pass one has laid them out.
3. **Through ProducerBot** - as a tool in its box, so a conversational request can reach it.

### What this changes about what is already recorded

- **`Shot.prompt` holds an intent, not an H3 prompt**, and pass two produces something structurally different and much longer. Overwriting the intent with the expansion would destroy the human-editable, human-readable thing pass one wrote, and make re-expansion impossible. These want to be two fields. That decision belongs to the Director.
- **The existing expansion's neighbour rule was reasoned for pass one** and should be re-reasoned for pass two rather than inherited.
- **VRAM sequencing gets more important, not less.** Thirty per-shot calls is thirty language-model calls, which is exactly the text-heavy front-loading the Director asked for on 2026-08-17 - do it all before any render, so the model loads once.


---

## Corrections to the derived format rules, 2026-08-18 - read after the guide's own checklist

I wrote the rules above from the node pack's README, its validator, and three pages of MiniMax's guide. Reading the guide's **Output Checklist** (its own sections 3, pages 19-20) shows my summary was directionally right, **materially incomplete, and wrong in one place.** The guide is the authority; this section supersedes mine where they differ.

### One rule I had actively wrong

**Camera motion.** I wrote it as "motion type, amplitude and speed" as though all three were always required. The guide asks for camera motion *written as natural action*, with **amplitude and speed only where meaningful**. My version would have produced stilted, over-specified prompts that name an amplitude for every pan — the opposite of the intent.

### Formatting I had wrong

In the guide's full-reference worked example, `overall_soundscape:` and `non_diegetic_music:` sit on their **own lines with the value beneath**, not inline after the colon as I showed. And **`N/A` is a legitimate value** for a field that genuinely does not apply — the checklist asks only that it be used where warranted.

### Rules I did not capture at all

Semantic, not syntactic — and these are the ones a checker cannot enforce but a system prompt must:

- **Every cut must introduce new subject, space, state, viewpoint or time information.** A cut that shows the same thing from the same place is a wasted shot boundary.
- **Speaker ids are stable across shots**, and a character who never vocalizes **carries no id at all**. Handing every subject an `(Sx)` is wrong.
- `<d>` contains **only** the language tag and the verbatim speech — no stage direction inside it.
- **Voiceover has a fixed idiom**: the speaker *says in an off-screen voiceover*, plus a closed-lips statement. It is not just dialogue with a note attached.
- `<scenetrans>` must appear in **both** parts when a line crosses a cut; `<cutoff>` marks speech that is truncated.
- On-screen text goes in double quotation marks and is **left untranslated**.
- `overall_soundscape` is **one to four sentences**, and must contain **no dialogue, singing or diegetic music** — those belong in the description.
- `non_diegetic_music` is **one to three sentences** covering instrumentation, tempo and dynamics.

For full-reference mode specifically, where my summary was weakest:

- **Six sections, present and in order.** I described three loose parts; there are six, and their order is checked.
- Every label is **defined once** and used consistently across all sections; no new labels may be introduced in the summary.
- The **task-type prefix must match the reference's actual role** — a picture used for its palette is not a subject reference.
- **No `(Sx)` anywhere in `retention_analysis`.** Speaker ids belong to the description.
- Relationship markers must be chosen **within the role each label was defined as**, not freely.
- Style is established in **one or two sentences before `[Shot 1]`** — not woven through the description.

### Why this matters more than a corrected list

Several of these are exactly the errors a competent model writing from taste would make: giving every character a speaker id, putting stage direction inside `<d>`, describing ambience and dialogue together in `overall_soundscape`, naming an amplitude for every camera move. **They are cheap to check and expensive to discover after a render.** The checker should cover the mechanical ones; the specialist's prompt has to carry the semantic ones, because nothing else will.


---

## Eight rulings, 2026-08-18 (second decision round)

**Pipeline:**
1. **H3 keyframe adapter first** — the `MiniMaxH3 I2V-FLframe` graph (`fl2va` checkpoint), covering both first-frame-only and first/last, staying in the lip-sync-capable family. Serves the Director's own described flow: generate an image, use it as the first frame.
2. **Enhancer gate: `singing` refused; `unknown` refused with the fix named** ("set the singing state first" — one click). Only `not_singing` passes. A wrong guess silently destroys lip-sync, and in a music video an unlabelled shot is likelier singing than not.
3. **Per-clip Generate is one click, gates still run** — the click on *this* clip is the consent; it performs the arming transition itself when the prompt/mode/lock gates pass, then renders. Batch keeps the two-act flow (arm-a-plan report → Generate All).
4. **Sequence: finishing lane (approval → assembly) as the main lane, with the keyframe adapter built in parallel.**

**Layout:**
5. **Video playback is a launch requirement for the Monitor** — the Director overrode the audio-first recommendation. The first layout pass therefore includes take-serving (a route that plays takes in the browser), which the approval work needs regardless: the two lanes share this dependency, so it is built once, early.
6. **One shared composer**, docked with the chat, prefill beside it.
7. **Width floor 1440**, docks collapsing below it (bin/chat to icons first, inspector to a drawer); the e2e width matrix re-baselines against these points.
8. **Song setup lives in a Song stage tab**, always one click away.


---

## Corrected 2026-08-18: reference mode subsumes keyframes — with audio

The keyframe adapter's schema reading was accurate (`MiniMaxH3ImageToVideo` has no audio input of any kind) but the conclusion drawn from it — "a keyframe shot has nothing to sync to" — treated the simple path as a ceiling. The Director pushed back, and the Fantastic prompt builder plus MiniMax's own guide settle it:

- Guide §2.2.2: *"Use a standalone `<Picture N>` when the reference image itself serves as a shot's first frame, keyframe, last frame, edited keyframe, or composition anchor"* — with the worked wording *"`<Picture 2>` is the first frame of `[Shot 1]`, showing …"*.
- The builder's Reference-mode picture roles include **First frame** and **Last frame** (`fully_preserved`, task `keyframe completion`), alongside composition, look, setting, attribute-transfer and storyboard roles.
- All of this rides `MiniMaxH3ReferenceToVideo` — the node with `ref_audios`, i.e. the windowed master song.

**So keyframes and lip-sync combine in references mode.** The picture is an ordinary reference slot; its *meaning* travels in the structured prompt — which is H3's whole design, and why the prompt expansion machinery matters as much as the graphs. The dedicated keyframe modes remain the efficient audio-less path.

One line blocks it in our taxonomy today: `SHOT_MODE_SPECS["references"]` accepts only the `reference` role. The unlock is that plus the prompt wording (reference map for un-expanded shots, the specialist for expanded ones).