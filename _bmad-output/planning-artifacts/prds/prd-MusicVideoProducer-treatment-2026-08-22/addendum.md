# Addendum — Treatment Planning

Depth that belongs downstream rather than in the PRD: what already exists, mechanism, the alternatives that were offered and declined, and notes for architecture, UX and epics.

---

## A. What already exists, verified 2026-08-22

The single most useful thing about this feature is how much of it is already built. Verified against the current tree, not assumed:

| Capability | State |
|---|---|
| `Project.creative_brief` | **Exists**, editable at `#creative-brief`, saved with the project |
| Brief reaching the Director | **Already happens** — `timeline.py` puts `creative_brief` in the project dump at three call sites; `director.py`'s prompt opens *"You are handed the whole project: creative brief, treatment, style bible, the song's words"* |
| Brief lock / recovery / restore | **Absent.** `DOCUMENT_CONTROLS` covers `treatment` and `style_bible` only |
| Treatment chat thread | **Exists** — `Project.messages: list[TreatmentMessage]`, with `threadHtml` rendering it |
| Per-turn document consent | **Exists** — `APPLY_DOCUMENTS_CONTROL`, unchecked by default, spent on send, cleared on project change |
| Assistant tools | **Two, both shot-level** — `fill_shots`, `expand_prompts`. No document tool, no question-asking turn shape |
| Asset proposals | **Exist** — `director.stage_manager()` → `StageManagerResult{message, assets: [AssetProposal{kind, name, prompt}]}`, route at `app.py:7800` |
| A place for a proposal to wait | **Absent.** The control's own words: *"queue up to N Flux asset render(s)? Each proposal becomes an ordinary asset"* — proposal straight to GPU |
| Multiview promotion | **Exists and is already widened** — `MULTIVIEW_SUBJECTS = {"character": "character", "prop": "object", "setting": "object"}`, two prompt templates, a frontend half in `api.js`, a contract test holding both sides level |
| Character slots | **Exist**, singer-only by design — `Asset.character_slot` is H3's speaker id, refused by name for any non-character asset |
| Music 3 form | **Exists**, and already reuses the `caption` field as the "idea" for both SongPlanner presets (`api.js`) |

**Two corrections to earlier analysis are recorded here because they changed the plan.** The Brief was described as needing "promotion" into the pipeline — it was already in it. The multiview gate was described as refusing non-character assets — that was fixed some time before 2026-08-22. Both were caught by reading the current code rather than trusting an eight-day-old artifact. The general lesson is worth keeping: **this codebase moves fast enough that a constraint recorded last week should be re-run before it is planned around.**

---

## B. Mechanism — the planning tools

The behaviour change in TP-7 is mostly a tools change, not a persona change. `director.assistant_tools()` today exposes `fill_shots` and `expand_prompts`; both act on Shots. An assistant whose only verbs are shot-level will reach for shot-level verbs, which is exactly the complaint — *"it just jumps to redoing treatment and story bible right away"*.

What planning needs, at minimum:

- a **Brief-write** tool, subject to the lock and the recovery slot;
- an **asset-proposal** tool, writing to the Suggested Assets list rather than to the render queue;
- a turn shape in which **asking a question and writing nothing is a complete, successful turn** — the current schema has no such shape, and a model that must always produce a document will always produce one.

The last is the subtle one. The reason the assistant redoes documents is partly that it has no way to *not*.

**Every tool inherits the existing refusals** — locked documents, locked Shots, render provenance, the prompt gate. This is the 2026-08-18 constraint restated: *"a toolset means the assistant can change project state … Whatever tools the assistant gets should inherit that, not start over."*

---

## C. The model reliability envelope, and what TP-4 is buying

Standing measurements on this machine, all of which bear on R-3's long unattended pass:

- Populate has **exceeded its 300 s timeout**; the director timeout was raised to 300 s for this reason.
- Roughly **90 % of a reply is reasoning**, and the reasoning length swings **26× across identical rolls** — so a pass that succeeds once may time out on the next identical attempt.
- The local model **silently drops boolean tool fields** — `fill_shots` applied modes and citations while omitting `use_song_audio` and `singing` twice, and the narration claimed otherwise.
- `DirectorResult` never required `shots`, which was the **root cause** of every empty-shots failure — a schema's `required` list, not prompt wording.
- `enable_thinking: false` stopped working on 2026-08-19; the model reasons and then answers.
- A `ReadTimeout` stringifies to `""`, so a timeout reported naively reads as an empty error.

**TP-NFR-5's "a missing field is missing, never an instruction to clear"** is written directly from the third of those. **TP-4's retry** is written from the first two — a retry has recovered this class of failure before, and with 26× variance a second roll is a genuinely different roll rather than a hopeful repeat. **TP-4's "names what happened"** is written from the last: a failure that stringifies to nothing must not surface as a blank.

When the schema for Suggest Video is designed, the `required` list is the load-bearing part. Every field the Brief must contain belongs in it.

---

## D. Alternatives offered and declined

Recorded because a declined option is a decision, and because two of them are the obvious first suggestions anyone will make again later.

**Consent model.** Three were offered:
- *Proposed edits accepted inline* — assistant shows a diff, nothing is written until accepted. Preserves the opt-in principle exactly.
- *Session consent* — **chosen** (R-2). Live editing for the session, undo as the safety net.
- *Scratch draft applied at the end* — safest, but converges out of sight, which is the bulk change-set the Director explicitly did not want.

The chosen option is the least conservative, and it pairs with TP-1 and TP-9 rather than standing alone. The single recovery slot plus live editing was flagged as a hole — many revisions, one slot — and the in-memory session stack is the resolution.

**Latency shape.** Three were offered:
- *Small turns, one question at a time* — most reliable given §C, many turns to finish.
- *One big pass, then fast edits* — **chosen** (R-3), knowingly the shape most exposed to §C.
- *Big pass, chunked and streamed* — premise, then cast, then locations, then look, each landing as it completes. Avoids the one-huge-call timeout, most engineering.

If CM-T1 fires — Suggest Video routinely cancelled before it returns — the chunked option is the designed fallback and should be reconsidered rather than re-derived.

**Character slots for non-singing characters.** The Director initially leaned toward yes. Declined on inspection: a slot is H3's speaker id, and the refusal message in `app.py` says so outright — *"a character slot names one of the song's singers."* A recurring non-singing character in a slot would be declared a voice. The ask (consistency across b-roll) is fully served by the existing Asset → reference sheet → citation path, which is indifferent to whether the subject sings. **The Director got what they asked for; the mechanism was the correction.**

---

## E. Downstream notes

**For UX.** Four surfaces need design the PRD deliberately does not specify: the Planning Mode indicator and what entering it communicates about consent (TP-6); how an assistant edit is made visible *and attributable* in the Brief (TP-8, open question 2); the Suggested Assets tab, which should follow the existing Assets subtab strip precedent; and the honest long-running indication for Suggest Video, which must not imply measurable progress (TP-NFR-4). The Brief's own surface also needs to stop being an unlabelled textarea (TP-2).

**For architecture.** The load-bearing decisions are the tool schema and its `required` lists (§C), where Suggested Assets are stored and how they are invalidated when the Brief moves on (open question 5), and the Session Undo bound (open question 3). The Brief protections (TP-1) are a third instance of a pattern that exists twice — follow it rather than inventing.

**For epics.** TP-1 and TP-2 are a genuine prerequisite for everything else and are small; they can start immediately. TP-18 and TP-19 (the Song Planner) are fully independent of the rest of the PRD and can be built in parallel by anyone. TP-16 (proceed navigation) is independent of everything except TP-17's offer, so the plain navigation can ship before Suggested Assets exists. The spine — TP-3 through TP-12 — is the sequenced part.

**Quick wins already identified.** TP-1/TP-2, TP-16 without its offer, and the Song Planner are each independently justified and buildable today, without waiting for the spine.
