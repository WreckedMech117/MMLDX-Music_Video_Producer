---
title: Treatment Planning
status: final
created: 2026-08-22
updated: 2026-08-22
---

# PRD: Treatment Planning

## 0. Document Purpose

This PRD is for the Director (sole builder and operator) and for the downstream BMad workflows — architecture, UX, and epic/story creation — that consume it. It describes how a music video gets *decided*: the stretch between having a song and having a shot plan, which the application currently leaves as a blank textarea.

It builds on, and does not duplicate, the product PRD at `prds/prd-MusicVideoProducer-2026-08-16/prd.md`, which remains authoritative for everything it covers. The upstream input is `_bmad-output/planning-artifacts/treatment-planning-findings-and-rulings-2026-08-22.md` — six findings and ten binding rulings **R-1** through **R-10**. Requirements below cite them; a change to a ruling is a change to this PRD.

**Two findings in that document correct earlier mistaken premises**, and both matter to how this PRD reads:

- The Brief is **already** an input to document generation — `timeline.py` puts `creative_brief` into the project dump at three call sites and the Director's prompt names it first. This feature does not promote the Brief into the pipeline; it gives the Brief protection, a contract, and primacy among three documents that already travel together.
- The multiview gate that once refused non-character assets **has already been widened** to `character`, `prop` and `setting`. Character planning inherits a working path, not a blocked one.

**Requirement IDs are prefixed `TP-`** so they cannot collide with the product PRD's `FR-1`–`FR-26` or the effects PRD's `FX-1`–`FX-25`, all of which are live. Cross-cutting quality requirements are `TP-NFR-n`. Mechanism lives in `addendum.md`, not here.

## 1. Vision

Every part of this application knows what it is for except the beginning. The Song workspace imports or generates audio. The Timeline lays shots against it. The Queue renders them. Between those sits a textarea with no label, no contract, and no help — and it is where the video is actually decided.

**The Brief is the source document.** Treatment, Style Bible, shot plan, cast and every prompt downstream are elaborations of a decision made there. A thin Brief produces a thin video with more words in it. The application currently offers no way to make a Brief less thin except staring at it.

This feature makes that stretch a conversation. Not a generator that hands back a finished treatment — the Director's own words are *"chat with it to refine the idea as opposed to it just jumping to redoing treatment and story bible right away"* — but a collaborator that proposes a premise from the song it can already read, asks the questions a producer would ask, and **edits the Brief in front of you while you talk**. The value is not that a model can write. It is that a model that has read your lyrics can ask *"is she alone in the car?"* and put your answer somewhere it will still be in six months.

The bet is stated plainly: **this brings the Director ideas they did not already have.** A local model is a poor writer and a decent interlocutor, and the interaction is designed around that asymmetry — it proposes, the Director decides, and everything it writes is visible, attributable and reversible.

## 2. Target User

The Director, as established in the product PRD §2. This feature adds jobs rather than users.

### 2.1 Jobs To Be Done

- **Functional:** get from a finished song to a real idea for a video, without staring at an empty box.
- **Functional:** make a video for a song I did not generate here — an existing track with its details filled in by hand.
- **Functional:** be asked the questions I would not have thought to ask myself.
- **Functional:** know which assets this video needs before I spend GPU minutes finding out.
- **Functional:** keep the kid on the bus looking like the same kid across nine b-roll shots.
- **Emotional:** arrive at the shot plan feeling like the video was decided rather than defaulted into.

### 2.2 Non-Users (v1)

Unchanged from the product PRD §2.2. One addition:

- Anyone wanting the application to write the video for them. Every proposal is a proposal; nothing here decides anything on the Director's behalf.

### 2.3 Key User Journeys

- **UJ-7. The Director turns a finished song into an idea.**
  - **Persona + context:** the song is done and good. The Director has no idea what the video is.
  - **Entry state:** a Project with a Song carrying lyrics and creative direction, generated or hand-filled.
  - **Path:** opens Treatment → the Brief is empty → presses **Suggest Video** → waits, watching an honest indication that a long pass is running → a complete Brief arrives: premise, cast, locations, arc, look → reads it, disagrees with half of it.
  - **Climax:** half of it is wrong in a way that tells the Director what right would be. The idea they now have is not the one on screen and would not have arrived from a blank box.
  - **Resolution:** they enter planning mode and start correcting it in conversation.
  - **Edge case:** the pass times out. The Brief is untouched, the failure is named, and the Director can retry or write the Brief by hand — Suggest Video is never a prerequisite for anything.

- **UJ-8. The Director refines the idea by talking about it.**
  - **Persona + context:** a Brief exists and is roughly half right.
  - **Entry state:** planning mode on, in the existing Treatment thread.
  - **Path:** *"she's a passenger, not the driver"* → the Brief updates in place, visibly → the assistant asks who is driving → *"her brother, they don't speak"* → the Brief gains a second character and the assistant asks whether the brother appears in the choruses → the Director changes their mind twice about the ending and steps back through the revisions.
  - **Climax:** the Brief says something the Director recognises as theirs, arrived at by being asked about it.
  - **Resolution:** they leave planning mode. The Brief is settled; Treatment and Style Bible are generated from it by the path that already exists, and they are longer and more specific than they would have been.
  - **Edge case:** the assistant proposes something that overwrites a paragraph the Director wrote by hand. They step back one revision and say so; the paragraph returns.

- **UJ-9. The Director finds out what the video needs before paying for it.**
  - **Persona + context:** the Brief is settled and names a cast, two locations and a car.
  - **Entry state:** planning has produced a list of suggested assets.
  - **Path:** opens the **Suggested Assets** tab → a list of proposals, each with a kind, a name and the prompt that would generate it → deletes three that duplicate library assets → edits one prompt → the character with an existing reference photo is submitted and checked: it is a single photo, not a multiview, so it is queued for conversion → presses **Proceed to Assets**, which offers to generate what remains.
  - **Climax:** one confirmation, and the render batch covers exactly what the video needs and nothing it does not.
  - **Resolution:** the Assets library holds the cast and locations, with reference sheets for every recurring character — singing or not.
  - **Edge case:** the Director accepts nothing. No GPU time is spent, and the proposals stay in the list.

## 3. Glossary

Terms from the product PRD §3 are unchanged and used as defined there. New and clarified vocabulary:

- **Brief** — `Project.creative_brief`. The **source document**: the decision about what the video is, from which Treatment, Style Bible and every prompt downstream are elaborated. It already reaches the Director in the project dump; this feature gives it protection, a contract, and primacy (R-6).
- **Suggest Video** — one long generation pass that reads a Song record and writes a complete Brief (R-3, R-7).
- **Planning Mode** — a conversation state in the existing Treatment thread in which the assistant may edit the Brief live, without per-turn consent (R-2).
- **Planning Turn** — one exchange in Planning Mode. May ask a question, propose an edit, both, or neither.
- **Session Undo** — the bounded, in-memory stack of Brief revisions made during one Planning Mode session, with the persisted recovery slot as its durable floor (R-2, R-10).
- **Asset Proposal** — a suggested asset: a kind, a name and the prompt that would generate it. Already produced by `stage_manager`; what is new is that it can *wait* (F-2).
- **Suggested Assets** — the reviewable list of Asset Proposals, and the tab that holds it, beside Brief / Treatment / Style Bible.
- **Recurring Character** — a character appearing in more than one Shot, whether or not they sing. Held consistent by a reference sheet and citations, **never by a character slot** (R-8).
- **Character Slot** — unchanged and *narrowed by clarification*: H3's speaker id, naming one of the song's **singers**. A non-singing character never holds one.
- **Song Planner** — the pass that turns a plain-language song idea into Title, Creative Direction and Lyrics/Section Plan **in the Music 3 form fields**, and does nothing else (R-9).

## 4. Features

### 4.1 The Brief as a Protected Source Document

**Description:** the Brief acquires the protections the other two creative documents already have, and a stated contract. Prerequisite for everything else — nothing should point a language model at an unprotected document.

#### TP-1: Lock, recover and restore the Brief

The Brief gains the same three protections `treatment` and `style_bible` already carry.

**Consequences (testable):**
- The Brief can be locked, and a locked Brief refuses every automatic write by name, exactly as the other two documents do (R-1).
- A write that replaces Brief text preserves the prior version in a recovery slot, and the Director can restore it.
- **FR-16 — "never silently destroy a creative document" — holds for all three documents with no exception.** A test asserts the Brief's behaviour against the same expectations as `treatment` and `style_bible`.
- Existing manifests load unchanged; the new fields are defaulted.

#### TP-2: The Brief states what belongs in it

The Brief is presented as the source document rather than as an unlabelled box.

**Consequences (testable):**
- The Brief's surface names what it is for and what belongs in it, in the application's own voice.
- The relationship is stated where the Director can see it: Treatment and Style Bible are generated *from* the Brief.
- Nothing about the wire changes — all three documents still travel in the project dump exactly as they do today (R-6).

### 4.2 Suggest Video

**Description:** one press, one long pass, a complete Brief to react to. Realizes UJ-7.

#### TP-3: Suggest a video from the Song

The Director can produce a complete Brief from the Project's Song.

**Consequences (testable):**
- The control requires a **Song record carrying lyrics and creative direction**, and does not care whether they arrived by generation or by hand — an imported track with hand-filled details is a first-class starting point (R-7).
- With the Song's details incomplete, the control refuses by name, saying which field is missing, and offers where to fill it.
- The pass produces a Brief covering at minimum premise, cast, locations, arc and look.
- **Where the Song is already sectioned on the timeline, the pass uses that structure**; where it is not, it runs on lyrics and style alone (R-15). Sections are used when present and **never required**, so the control's precondition remains a Song record and nothing more.
- The Brief it writes is subject to TP-1: a locked Brief refuses it, and an existing Brief's text goes to the recovery slot.
- The pass never writes Treatment, Style Bible, Shots, or any Asset (R-10).

#### TP-4: A long pass fails safely and says so

**Consequences (testable):**
- The pass has a bounded timeout and **retries** within it before reporting failure (R-3).
- A failed or timed-out pass **leaves the Brief exactly as it was** and names what happened.
- A result that is incomplete or degraded is **reported as partial**, never presented as a finished Brief.
- **An in-progress indicator is shown for the whole life of the pass** — the Director's own stated requirement (R-11) — and it shows that work is happening without implying progress it cannot measure.
- The application stays usable while the pass runs.
- The timeout value is **set from live measurement during the build**, not fixed by this document; raising the director timeout is authorised where measurement shows it is needed (R-11).

**Notes:** this is knowingly the shape most exposed to the documented model envelope — populate has exceeded its 300 s timeout, roughly 90 % of a reply is reasoning with 26× variance across identical rolls, and this model silently drops fields. It was chosen because reacting to something substantial beats inventing from nothing (R-3). The consequences above are the price of that choice and are requirements, not aspirations.

**Read the measurement correctly.** Suggest Video's *workload* is smaller than populate's — a fresh project's dump is mostly lyrics, and the output is one document rather than a whole shot plan. A pass that exceeds its timeout is therefore evidence of **reasoning-length variance, not of too much work**, and that distinction picks the lever: a longer timeout buys the tail of the distribution, a retry re-rolls it. Both are required because they fix different failures (R-11).

#### TP-5: Suggest Video is never required

**Consequences (testable):**
- Every other capability in this PRD works on a Brief the Director wrote by hand.
- Nothing refuses, warns, or degrades because Suggest Video was not used.

### 4.3 The Planning Conversation

**Description:** the spine. The assistant proposes, asks, and edits the Brief visibly while the Director talks. Realizes UJ-8.

#### TP-6: Enter and leave Planning Mode

**Consequences (testable):**
- Planning Mode is entered and left explicitly, and its state is visible for as long as it is on (R-2).
- Entering grants document-write consent **for the session**, replacing the per-turn, unchecked-by-default consent for the duration — and the interface states that this is what entering means.
- Leaving ends the consent. Consent never survives leaving, a project change, or a reload.
- Outside Planning Mode, the existing per-turn consent behaviour is unchanged.

#### TP-7: The assistant asks as well as executes

**Consequences (testable):**
- A Planning Turn may ask a question, propose an edit, both, or neither. A turn that only asks is a valid and expected turn.
- The assistant can write the Brief — a capability it does not have today, and the reason it currently jumps to redoing Treatment and Style Bible (F-4).
- The assistant can propose cast, locations, scenes and story beats, and those proposals become Brief content or Asset Proposals rather than free-floating chat text.
- Every tool the assistant gains **inherits the existing refusals** — locked documents, locked Shots, render provenance, the prompt gate. A planning tool is not a new privilege class (R-5).

#### TP-8: Edits are visible where they land

**Consequences (testable):**
- A Brief edit made by a Planning Turn is visible **in the Brief**, at the moment it is made — not summarised in chat and applied later.
- What changed is distinguishable from what did not.
- An edit is attributable: the Director can tell assistant-written text from their own.

#### TP-9: Step back through a session's revisions

**Consequences (testable):**
- Every Brief revision made during a Planning Mode session can be stepped back through, in order, for the life of that session (R-2, R-10).
- The persisted recovery slot is the durable floor: it survives a reload or a crash, and holds the version the session began from at minimum.
- Stepping back never removes anything from the conversation thread — the record of what was said stands even when what was written is undone.

#### TP-10: Planning does not generate the other documents

**Consequences (testable):**
- Planning writes the Brief and Asset Proposals. It never writes Treatment or Style Bible (R-10).
- Generating those remains the Director's separate, existing act, and is unchanged by this feature.
- Leaving Planning Mode does not trigger generation of anything.

### 4.4 Suggested Assets

**Description:** proposals get somewhere to wait before they cost GPU minutes. Realizes UJ-9.

#### TP-11: A reviewable list of Asset Proposals

**Consequences (testable):**
- Proposals are held in a **Suggested Assets** tab beside Brief, Treatment and Style Bible.
- Each proposal shows its kind, its name and the prompt that would generate it.
- **Each proposal also records the Brief passage that called for it**, and shows it (R-13). A proposal explains why it exists without the Director rereading the conversation.
- A proposal can be edited, deleted, or accepted individually.
- **A proposal costs nothing until it is accepted** — no GPU time, no Asset, no job. This is the change: proposals currently go straight to a Flux render (F-2).
- Proposals persist with the Project.

#### TP-12: Accepted proposals generate as one batch

**Consequences (testable):**
- Accepting proposals generates them as a batch, after one confirmation that names the count.
- An accepted proposal becomes an ordinary Asset, indistinguishable from one generated any other way.
- A proposal duplicating an existing Asset is flagged as such before it is accepted, not after it renders.
- Declining costs nothing and leaves the list intact.

#### TP-13: A stale proposal is flagged, never removed

**Consequences (testable):**
- Where the Brief passage a proposal came from has changed, the proposal is **marked as possibly stale** and states that as the reason (R-14).
- A flagged proposal is never removed automatically, and never blocks anything. The Director keeps it, removes it, or asks for it to be re-proposed.
- A proposal with no recorded origin is never flagged — an unknown origin is not evidence of staleness.
- Accepting a flagged proposal is permitted; the flag is information, not a gate.

### 4.5 Character Planning

**Description:** the cast gets decided in conversation and made consistent by the machinery that already exists.

#### TP-14: Plan the cast in conversation

**Consequences (testable):**
- Where the Song is marked for more than one voice, planning asks about each of them rather than assuming one.
- Where the Director has no character in mind, the conversation works out an appearance and produces a proposal for it.
- A character the conversation settles becomes an Asset Proposal like any other.

#### TP-15: Bring an existing character image

**Consequences (testable):**
- The Director can submit an image they already have for a planned character.
- The application reports whether it is already a multiview reference sheet or a single view, and says which it found rather than guessing silently.
- A single view can be converted to a reference sheet through the existing multiview path, on the Director's confirmation.
- A submitted image is never modified, and conversion produces a new Asset beside it.

#### TP-16: Recurring characters get sheets, not slots

**Consequences (testable):**
- A recurring **non-singing** character is held consistent by a character Asset, a reference sheet and citations in the Shots it appears in — the same path that holds a singer's identity (R-8).
- **No non-singing character is given a character slot.** A slot is H3's speaker id and naming one would declare that character a voice in the song.
- The standing rule that nothing infers a slot is unchanged.
- Planning may propose recurring non-singing characters freely.

### 4.6 Moving Between Phases

**Description:** the workflow states where it goes next.

#### TP-17: Proceed to the next step

**Consequences (testable):**
- The Song, Treatment and Assets workspaces each offer a control naming the next phase explicitly.
- The control states what is not yet ready rather than being silently disabled.
- **Proceeding offers; it never does.** A proceed control may present work the Director is likely to want at that boundary, but it never generates, renders, analyses or writes a document unasked, and declining always proceeds.

#### TP-18: Proceeding from Song offers the structure analysis

**Consequences (testable):**
- Proceeding from Song to Treatment **offers structure analysis when it has not been run**, naming what it will do and roughly what it costs (R-16).
- The offer is declinable, and declining proceeds to Treatment normally. Structure is never a precondition of proceeding, and never a precondition of Suggest Video (R-7, R-15).
- Where analysis has already been run and the Song is unchanged, no offer is made.
- The offer runs the analysis that already exists — Whisper transcription, `[Tag]` block alignment, proposed section boxes with empty prompts — and adds no new analysis of its own.
- Existing sections are never replaced without the Director saying so, exactly as the existing route already refuses.

**Notes:** verified 2026-08-22 — the **Analyze structure** control already sits on the Song page beside **Build treatment →**, and already proposes section boxes from timed `[Tag]` blocks. This requirement is a *prompting* change to an existing capability, not new analysis work. Its value is that structure stops depending on whether the Director noticed a button.

**Cross-PRD dependency (R-17).** The effects PRD's `FX-1` puts a second, different song analysis — beats, onsets, per-band envelopes — at this same song. The two are not one computation and should not be merged, but they want the same moment: the one point where the Director is plainly willing to wait. Whichever feature is built second owns making them share one trigger and one indicator, so a song is analysed once rather than twice.

#### TP-19: Proceeding from Treatment offers the assets

**Consequences (testable):**
- Proceeding from Treatment to Assets offers to generate the accepted Suggested Assets, including any character reference-sheet conversions.
- The offer is declinable, and declining proceeds without spending anything.
- Accepting is the same confirmed batch as TP-12, not a second path.

### 4.7 The Song Planner

**Description:** a plain-language idea becomes the fields MiniMax Music 3 wants. Independent of everything above.

#### TP-20: Turn an idea into song fields

**Consequences (testable):**
- The Music 3 section accepts a plain-language song idea.
- The pass fills **Title**, **Creative Direction** and **Lyrics/Section Plan** in the form, shaped for MiniMax's documented prompt style.
- Every filled field remains editable before anything is generated.

#### TP-21: The planner fills the form and stops

**Consequences (testable):**
- The planner writes to the **form fields only**. It never writes the Song record and never starts a generation (R-9).
- Triggering generation remains the Director's act.
- Where the Director has already typed into a field, the planner does not overwrite it without saying so.
- On generation, the song's details transfer to Song Context by the path that already exists — unchanged by this feature.

## 4A. Cross-Cutting NFRs

### TP-NFR-1: Nothing the Director wrote is lost without a way back

**Consequences (testable):**
- Every automatic write to a creative document is recoverable — within a session by TP-9, across a session by TP-1's recovery slot.
- A locked document is never written by any automatic path.
- Restoring is always available to the Director and never conditional on how the text was produced.

### TP-NFR-2: Nothing spends GPU time without confirmation

**Consequences (testable):**
- No capability in this PRD queues a render without an explicit confirmation naming what will run.
- Asset Proposals, character conversions and batch generation all pass through that confirmation.
- Planning and Suggest Video spend language-model time only, never GPU render time.

### TP-NFR-3: Local-first, unchanged

**Consequences (testable):**
- Every model pass here runs on the configured local LM Studio model. No cloud model is introduced for planning or for anything else.
- No account, no network service and no telemetry is added.

### TP-NFR-4: The application stays usable during a long pass

**Consequences (testable):**
- Suggest Video and every Planning Turn run without blocking the interface, a render, a Batch or an Assembly.
- A pass in flight can be abandoned by the Director, and abandoning leaves the Brief untouched.
- No progress percentage is shown for work whose progress cannot be measured — the standing honesty rule applies here identically.

### TP-NFR-5: Model output crosses the persistence boundary only through guards

**Consequences (testable):**
- Everything the model produces — Brief text, Asset Proposals, song fields — is validated before it is stored, inheriting the existing guard discipline (AD-14, R-5).
- A degraded or malformed reply is refused and reported, never written.
- A missing field in a reply is treated as missing, never as an instruction to clear the stored value. **This model is documented to drop fields silently.**

## 5. Non-Goals (Explicit)

Inherited from the product PRD §5, plus:

- **The application does not decide the video.** Every output here is a proposal; nothing is accepted on the Director's behalf.
- **No cloud or larger model for planning**, however much the local model's limits show. The local-first constraint is not traded for planning quality.
- **Planning does not write the Treatment or the Style Bible** (R-10).
- **The Song Planner does not generate songs**, and does not write the Song record (R-9).
- **No character slot is ever inferred**, for singing or non-singing characters (R-8).
- **No new chat surface.** Planning runs in the Treatment thread that already exists.
- **No autonomous multi-turn planning.** The assistant does not run turns unprompted, work in the background, or continue a conversation the Director is not in.

## 6. Scope

### 6.1 In Scope

- The Brief's protections and its stated contract (TP-1, TP-2).
- Suggest Video and its failure behaviour (TP-3 – TP-5).
- The planning conversation: mode, tools, visible edits, session undo, and its limits (TP-6 – TP-10).
- Suggested Assets as a reviewable list, with recorded origins, staleness flagging and confirmed batch generation (TP-11 – TP-13).
- Character planning, existing-image submission, and reference-sheet consistency (TP-14 – TP-16).
- Proceed-to-next-step navigation, and the offers it carries at the Song → Treatment and Treatment → Assets boundaries (TP-17 – TP-19).
- The Song Planner (TP-20, TP-21).

### 6.2 Out of Scope

- Planning at Shot level. The existing shot-level assistant tools are unchanged.
- Any persisted revision history beyond one recovery slot per document.
- Re-measuring the Krea reference-sheet sampling stages. Flagged in the findings as needing re-verification; not a dependency of anything here.
- Section-by-section planning of the Treatment, and any change to how Treatment or Style Bible are generated.
- Importing a brief, treatment or style bible from another project or an external file.

## 7. Success Metrics

- **SM-T1 — The blank box is gone.** A Director with a finished song reaches a Brief they would defend, without writing one from nothing. Judged on a real project.
- **SM-T2 — It brings ideas the Director did not have.** At least one element of a finished video traces to something the conversation proposed rather than something the Director arrived with. The Director's own judgement; this is the feature's actual bet (§1).
- **SM-T3 — Downstream documents get longer and more specific.** Treatment and Style Bible generated from a planned Brief are measurably more detailed than those generated from a hand-written one on a comparable project.
- **SM-T4 — Nothing is lost.** Across a full planning session, no text the Director wrote by hand is unrecoverable. Binary; gates release.
- **SM-T5 — GPU spend matches the plan.** The asset batch generated from Suggested Assets covers what the video needs, with the Director declining proposals rather than deleting rendered assets afterwards.

**Counter-metrics** — signals the feature has damaged the product:

- **CM-T1 — Abandonment mid-pass.** If Suggest Video is routinely cancelled before it returns, the long-pass shape (R-3) was the wrong call and should be reconsidered against the chunked alternative that was offered and declined.
- **CM-T2 — Undo becomes routine.** If stepping back through revisions is a normal part of every session rather than an occasional rescue, the assistant is overwriting rather than proposing and TP-8's attribution is not doing its job.
- **CM-T3 — The Brief gets shorter.** If planning conversations end with less in the Brief than the Director would have written alone, the interaction is costing thinking rather than producing it.
- **CM-T4 — Proposals rot.** If the Suggested Assets list accumulates unaccepted proposals across projects, the list is a graveyard rather than a queue and the proposal step needs rethinking.

## 8. Open Questions

Three, and each names who owns it. Everything else raised during discovery has been settled into the rulings (R-1 – R-15).

1. **What does "attributable" look like in TP-8?** Distinguishing assistant-written text from the Director's own is required; whether that is a visual treatment, a marker in the text, or a separate record is undecided. **Owner: UX.**
2. **How deep is Session Undo?** TP-9 says "for the life of the session" without a bound. A long conversation produces many revisions, and unbounded in-memory history is a decision rather than a default. **Owner: architecture.**
3. **Does each planning turn send a bounded thread?** R-12 keeps LM Studio at 75k because Suggest Video does not need more and the accumulating thread is the pass that will creep. Whether a turn sends a windowed or summarised thread, and where that boundary sits, is undecided. **Owner: architecture.**

### Follow-up measurement, not a question

**`populate` should be re-measured once planning ships.** This feature makes Briefs richer, which makes Treatments richer — and a Treatment is `populate`'s input. Planning succeeding therefore *increases* what populate is handed, and populate is already the pass observed to exceed its timeout (R-11). This is not a decision anyone needs to make; it is a measurement someone needs to take, and it is recorded so it is taken deliberately rather than discovered.

## 9. Assumptions Index

- `[ASSUMPTION]` (TP-6) Entering Planning Mode is a deliberate enough act to carry session-long consent. The standing per-turn consent exists because this model has destroyed documents before; R-2 trades it knowingly for the interaction, backed by TP-9 and TP-1.
- `[ASSUMPTION]` (TP-9) A bounded in-memory undo stack, with the persisted recovery slot as its floor, is sufficient. Confirmed by the Director (R-10), but the bound itself is open question 2.
- `[ASSUMPTION]` (TP-14) Where a Song is marked for multiple voices, the Director wants to plan every one of them rather than only the lead.
- `[ASSUMPTION]` (TP-21) A field the Director has already typed into should not be overwritten silently. Stated as a consequence rather than asked, because the alternative contradicts TP-NFR-1.
