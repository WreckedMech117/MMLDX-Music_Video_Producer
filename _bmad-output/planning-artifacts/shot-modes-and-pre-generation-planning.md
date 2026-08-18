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

## Open questions this direction raises

1. Does `Shot.mode` replace the current text-only/reference branch, or sit above it as a selector that chooses among adapters including those two?
2. Do asset roles live on the Shot's citation of an asset, or on the asset itself? The wolf is not "a middle frame" — it is a middle frame *in this shot*.
3. Does a section's mode constrain what the timeline will let the Director draw, or does the timeline allow anything and the readiness check refuse it later?
4. How much of the assistant's ability to act is tool-calling against real routes versus proposing changes the Director applies?
5. Is Replace Existing a per-render choice, a batch-level toggle, or a property of the shot?
