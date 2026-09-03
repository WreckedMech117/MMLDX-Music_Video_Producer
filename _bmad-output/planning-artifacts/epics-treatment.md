---
stepsCompleted: [1, 2, 3, 4]
inputDocuments:
  - _bmad-output/planning-artifacts/prds/prd-MusicVideoProducer-treatment-2026-08-22/prd.md
  - _bmad-output/planning-artifacts/prds/prd-MusicVideoProducer-treatment-2026-08-22/addendum.md
  - _bmad-output/planning-artifacts/treatment-planning-findings-and-rulings-2026-08-22.md
  - _bmad-output/planning-artifacts/ux-designs/ux-treatment-2026-08-22/DESIGN.md
  - _bmad-output/planning-artifacts/ux-designs/ux-treatment-2026-08-22/EXPERIENCE.md
  - _bmad-output/planning-artifacts/architecture/architecture-MusicVideoProducer-treatment-2026-08-22/ARCHITECTURE-SPINE.md
  - _bmad-output/planning-artifacts/architecture/architecture-MusicVideoProducer-treatment-2026-08-22/BUILD-ORDER.md
---

# MusicVideoProducer - Epic Breakdown: Treatment Planning

## Overview

This document breaks down the **Treatment Planning** feature (21 TP requirements, 5 TP-NFRs) into implementable stories. It is the third epic document for this product: `epics.md` covers Epics 1–7 (base product), `epics-effects.md` covers Epics 8–11 (Shot Effects and Transitions), and this one continues at **Epic 12**, so a story ID is unambiguous across all three.

Brownfield, and smaller than it looks. Reading the current code corrected three premises and each correction shrank the plan: the Brief already reaches the Director in the project dump; the multiview gate was already widened to `character`/`prop`/`setting`; and the **Analyze structure** control already sits on the Song page and already proposes section boxes from timed `[Tag]` blocks. What is genuinely absent is short — protection for the Brief, a place for an asset proposal to *wait*, and planning tools for an assistant whose only two verbs are shot-level.

**On the relationship to `BUILD-ORDER.md`.** Its eight slices A–H remain the build sequence and are unchanged. They are not the epic structure — slices B (tools) and C (attribution) deliver nothing a Director can see alone, and B, C and D touch the same files end to end. Organised by user value they collapse into the six epics below. Each story names the slice it implements.

## Requirements Inventory

### Functional Requirements

TP-1: Lock, recover and restore the Brief, so FR-16 holds for all three documents without exception
TP-2: The Brief states what belongs in it and that Treatment and Style Bible are generated from it
TP-3: Suggest a complete Brief from the Project's Song, using song sections when they exist
TP-4: A long pass fails safely, retries, reports honestly, and shows an in-progress indicator
TP-5: Suggest Video is never required by anything
TP-6: Enter and leave Planning Mode, which grants document-write consent for the session
TP-7: The assistant asks as well as executes; a question-only turn is a valid turn
TP-8: Edits are visible where they land, and assistant-written text is attributable
TP-9: Step back through a session's Brief revisions
TP-10: Planning writes the Brief and proposals, never the Treatment or Style Bible
TP-11: A reviewable list of Asset Proposals, each recording the Brief passage that called for it
TP-12: Accepted proposals generate as one confirmed batch
TP-13: A stale proposal is flagged with its reason, never removed automatically
TP-14: Plan the cast in conversation, asking about every voice the Song is marked for
TP-15: Bring an existing character image; report whether it is already a multiview; convert on confirmation
TP-16: Recurring non-singing characters get reference sheets and citations, never character slots
TP-17: Proceed to the next step from Song, Treatment and Assets — proceeding offers, never does
TP-18: Proceeding from Song offers the song analysis, as one moment covering both computations
TP-19: Proceeding from Treatment offers to generate the accepted Suggested Assets
TP-20: Turn a plain-language song idea into Title, Creative Direction and Lyrics/Section Plan
TP-21: The Song Planner fills the form and stops — no Song write, no generation

### NonFunctional Requirements

TP-NFR-1: Nothing the Director wrote is lost without a way back
TP-NFR-2: Nothing spends GPU time without an explicit confirmation naming what will run
TP-NFR-3: Local-first, unchanged — every pass on the configured LM Studio model, no cloud
TP-NFR-4: The application stays usable during a long pass, with no unmeasurable progress shown
TP-NFR-5: Model output crosses the persistence boundary only through guards

### Additional Requirements

*From the architecture spine (AD-32…AD-47) and its inherited constraints.*

- Attribution renders as a **mirror overlay** — a styled read-only div behind a transparent textarea sharing its metrics. **No `contenteditable` is introduced anywhere in this application** (AD-32)
- Attribution ranges are offsets on the Project, reconciled by one pure diff on every Brief write: an untouched range shifts, a range whose text changed is dropped (AD-33)
- The session undo stack is **frontend-only and bounded**; the persisted recovery slot is the durable floor (AD-34)
- **Session consent is a client affordance; every request carries consent explicitly.** The server never stores, infers or remembers it (AD-35)
- Asset proposals live on the Project, narrow-gated by the `_adopt_*` idiom, each recording its origin text; a proposal is inert and queues nothing (AD-36)
- Staleness is **derived at read time** — the origin text no longer appears in the Brief. Never a stored flag; a proposal with no origin is never stale (AD-37)
- **Asking and writing are separate tools, not one tool with optional fields.** On a model that drops fields silently, an optional field and a dropped field are the same bytes. Every required field promoted through `_promoted()`, which raises on an unknown name (AD-38)
- A long pass validates before it writes; a failure is reported by exception class and elapsed time, never by `str(exc)` — a `ReadTimeout` stringifies to `""` (AD-39)
- One analysis job composes structure alignment and the Song Envelope **without merging the computations** (AD-40)
- The Brief joins the existing document apparatus — same lock, same slot, same restore route — but **captures on the Director's own save** rather than on an applied reply, because no reply can write it. Adopted in `replace_project` (AD-41, amended 2026-09-03)
- The Song Planner route **returns fields and stores nothing** (AD-42)
- A planning turn is an ordinary `TreatmentMessage`; what it changed is carried as `MessageNotice` entries (AD-43)
- Undo restores a snapshot **verbatim and bypasses reconciliation** — an undo is not an edit (AD-44)
- The client never sends attribution ranges; the server is their sole writer (AD-45)
- An accepted proposal is **marked with the Asset id it produced**, not removed (AD-46)
- Each half of the combined analysis is **skipped when already current**; effects `FX-1` analyses on song import, so the common case is one half already done (AD-47)
- Inherited and binding: AD-11 (derived not persisted), AD-14 (guarded persistence boundary), AD-15 (ComfyUI untouched, local model only), AD-16/AD-21/AD-25 from the effects spine

### UX Design Requirements

UX-TP1: Introduce **no seventh accent**. Every meaning reuses an existing one; attribution uses surface and rule, not hue, because provenance is not a state
UX-TP2: Planning bar — a slim strip above the document column reusing the wizard guidance-banner shape, amber dot, one sentence of what the mode means, `[Exit planning]`
UX-TP3: While planning is on, the composer's `Apply document changes` checkbox is disabled and visibly superseded — the suspended control must not look operable
UX-TP4: Attribution treatment — `--surface-1` wash and a 2px `--line-strong` left rule on assistant ranges; **the Director's own text carries nothing**; editing a range clears its mark; hover names the turn
UX-TP5: Brief contract line — a persistent `--muted` sentence under the existing "Creative brief" label, not a placeholder and not a tooltip
UX-TP6: Session undo control that names what it will undo before doing it, and never removes anything from the chat thread
UX-TP7: A fourth `Assets` tab in the existing `document-tabs` strip, with a count chip and an amber dot when anything is flagged
UX-TP8: Proposal card — Consolas kind label, name, prompt, and its **origin** quoted from the Brief; actions to edit, delete, accept
UX-TP9: Staleness treatment — amber left edge, Consolas `MAY BE STALE`, one sentence naming what changed. **Never red**; still acceptable
UX-TP10: Indeterminate pass indicator — amber pulsing dot and **elapsed time only**, no percentage, no bar, no estimate; abandon available; `prefers-reduced-motion` respected
UX-TP11: Proceed controls at the foot of Song, Treatment and Assets, each naming its next phase; any offer renders **inline above the button**, never as a modal
UX-TP12: Microcopy — say what a mode means rather than what it is called; announce automatic changes in the past tense naming what they touched; never imply unmeasurable precision
UX-TP13: Accessibility floor — state never colour-alone; the attribution mirror is `aria-hidden` with the range list as its accessible form; the long pass announced in a polite live region without a ticking clock; the Planning bar is a status region; focus and cursor position survive the background rebuild

### FR Coverage Map

TP-1: Epic 12 — the Brief's protections
TP-2: Epic 12 — the Brief's contract
TP-3: Epic 13 — the Suggest Video pass
TP-4: Epic 13 — failure paths and the in-progress indicator
TP-5: Epic 13 — Suggest Video is never a prerequisite
TP-6: Epic 14 — Planning Mode and session consent
TP-7: Epic 14 — the planning tools
TP-8: Epic 14 — attribution
TP-9: Epic 14 — session undo
TP-10: Epic 14 — planning writes nothing else
TP-11: Epic 15 — the proposal list and origins
TP-12: Epic 15 — confirmed batch generation
TP-13: Epic 15 — staleness
TP-14: Epic 15 — planning the cast
TP-15: Epic 15 — bringing an existing character image
TP-16: Epic 15 — sheets, not slots
TP-17: Epic 16 — proceed controls
TP-18: Epic 16 — the Song → Treatment analysis offer
TP-19: Epic 16 — the Treatment → Assets offer
TP-20: Epic 17 — idea into song fields
TP-21: Epic 17 — the planner fills and stops
TP-NFR-1: Epic 12, Epic 14 — recovery slot, attribution reconciliation, session undo
TP-NFR-2: Epic 15 — nothing generates without a confirmation naming the count
TP-NFR-3: Epic 13, Epic 14, Epic 17 — every pass local
TP-NFR-4: Epic 13 — usable during a long pass
TP-NFR-5: Epic 13, Epic 14, Epic 15 — guarded tool output

## Epic List

### Epic 12: The Brief Becomes a Real Document
The Brief stops being the one creative document the application will let a language model destroy, and starts saying what it is for. Prerequisite for everything else, and worth building whether or not anything else here ever ships.
**FRs covered:** TP-1, TP-2, TP-NFR-1

### Epic 13: An Idea to React To
The Director presses one button and gets a whole video idea back — premise, cast, locations, arc, look — to disagree with. Reacting is easier than inventing, and half-wrong is more useful than blank.
**FRs covered:** TP-3, TP-4, TP-5, TP-NFR-3, TP-NFR-4, TP-NFR-5

### Epic 14: Refining by Talking
The Director talks about the video and watches the Brief change, seeing which half came from where and stepping back when it goes wrong. The spine of the feature.
**FRs covered:** TP-6, TP-7, TP-8, TP-9, TP-10, TP-NFR-1, TP-NFR-3, TP-NFR-5

### Epic 15: Knowing What the Video Needs
The Director finds out which characters, locations and props the video requires — and which are stale, and which duplicate the library — before spending a GPU minute on any of them.
**FRs covered:** TP-11, TP-12, TP-13, TP-14, TP-15, TP-16, TP-NFR-2, TP-NFR-5

### Epic 16: The Workflow Says Where It Goes
Each phase names the next one and offers the work the Director is likely to want at that boundary — offering, never doing.
**FRs covered:** TP-17, TP-18, TP-19

### Epic 17: A Song Idea Becomes a Song Form
A plain-language idea becomes the Title, Creative Direction and Lyrics/Section Plan MiniMax wants. Fully independent of every other epic, and it sits at the true start of the workflow.
**FRs covered:** TP-20, TP-21, TP-NFR-3

## Epic 12: The Brief Becomes a Real Document

Standalone and prerequisite. Nothing else in this feature should point a language model at an unprotected document. *(Slice A.)*

### Story 12.1: Lock, Recover and Restore the Brief

As the Director,
I want the Brief protected exactly as my Treatment and Style Bible are,
So that nothing can destroy an idea I typed and leave me no way back.

**Acceptance Criteria:**

**Given** the three creative documents
**When** the Brief's protections are implemented
**Then** `creative_brief_previous` and `creative_brief_locked` exist, defaulted so every existing `project.json` loads unchanged (TP-1, AD-41)
**And** the Brief gains a `DOCUMENT_CONTROLS` entry so **lock and restore** behave identically to `treatment` and `style_bible`
**And** its recovery slot is filled by the Director's own save rather than by an applied reply, since no reply can write the Brief — a byte-equal re-save captures nothing (AD-41, amended 2026-09-03)
**And** a locked Brief refuses every automatic write by name.

**Given** any write that replaces Brief text — the Director's own save today, a planning pass once one exists
**When** it runs
**Then** the prior version is preserved in the recovery slot and the Director can restore it (TP-1, TP-NFR-1)
**And** a test asserts the Brief against the same expectations already asserted for `treatment` and `style_bible`, so **FR-16 holds against every machine write for all three documents with no exception**. *Amended 2026-09-03, when this shipped:* the unqualified version of that sentence is not true and was not made true here. Against the Director's **own save**, the Brief is protected and `treatment`/`style_bible` are not — `PUT /documents` writes their text with no capture and no confirmation, measured. That residue is the reverse of the exception this story was written to remove, it needs a ruling rather than an implementation, and it is recorded in `docs/BUILD-HANDOFF.md` §6.

**Given** the generic `PUT /api/projects/{project_id}`
**When** a body omits the Brief's fields, or invents them
**Then** the stored values are preserved, adopted server-side via the established `_adopt_*` idiom (AD-41)
**And** a test asserts a full-project PUT omitting each of them leaves it intact.

### Story 12.2: The Brief Says What It Is For

As the Director,
I want the Brief to tell me what belongs in it,
So that the first box in the creative workflow stops being the least explained one.

**Acceptance Criteria:**

**Given** the Brief editor
**When** the Treatment workspace is shown
**Then** a persistent sentence beneath the existing "Creative brief" label states that this is the source document, and that Treatment and Style Bible are generated from it (TP-2, UX-TP5)
**And** it is not a placeholder that vanishes on the first keystroke, and not a tooltip.

**Given** the project dump
**When** the Brief's role is changed on screen
**Then** nothing about the wire changes — all three documents travel exactly as they do today (TP-2, R-6).

## Epic 13: An Idea to React To

Depends on Epic 12. Delivers the feature's opening move. *(Slice E.)*

### Story 13.1: Suggest a Video from the Song

As the Director,
I want a complete video idea proposed from my song,
So that I have something to disagree with instead of an empty box.

**Acceptance Criteria:**

**Given** a Project whose Song carries lyrics and creative direction
**When** Suggest Video runs
**Then** it produces a Brief covering at minimum premise, cast, locations, arc and look (TP-3)
**And** it does not care whether those song details arrived by generation or by hand — an imported track with hand-filled details is a first-class starting point (R-7)
**And** where the Song is already sectioned, the pass uses that structure; where it is not, it runs on lyrics and style alone. Sections are used when present and **never required** (TP-3, R-15).

**Given** the Song's details are incomplete
**When** the control is used
**Then** it refuses by name, saying which field is missing, and offers where to fill it (TP-3, UX-TP12).

**Given** a Brief that already has text, or is locked
**When** Suggest Video runs
**Then** the existing text goes to the recovery slot, and a locked Brief refuses the write by name (TP-3, Story 12.1).

**Given** the pass completes
**When** its output is stored
**Then** it writes the Brief and **nothing else** — no Treatment, no Style Bible, no Shots, no Assets (TP-3, TP-10)
**And** every required field of its schema is promoted through `_promoted()`, which raises on an unknown name (AD-38, TP-NFR-5)
**And** the pass runs on the configured local model with no cloud service introduced (TP-NFR-3).

### Story 13.2: The Long Pass Fails Safely and Says So

As the Director,
I want to see that it is working, and to be told the truth when it does not,
So that a slow model does not look like a broken application.

**Acceptance Criteria:**

**Given** a pass in flight
**When** it is running
**Then** an in-progress indicator is shown for its whole life, with an amber pulsing dot and **elapsed time only** — no percentage, no bar, no estimate (TP-4, UX-TP10)
**And** the indicator respects `prefers-reduced-motion` with a static dot
**And** the rest of the application stays usable, and the Director can abandon the pass (TP-4, TP-NFR-4).

**Given** a pass that times out or returns malformed output
**When** it fails
**Then** it **retries once**, because reasoning length varies 26× across identical rolls and a second roll is genuinely a different roll (AD-39)
**And** on final failure the Brief is left **byte-identical** to what it was (TP-4)
**And** the failure is reported by exception class and elapsed time, **never by its string** — a `ReadTimeout` stringifies to `""` and would surface as a blank (AD-39).

**Given** a reply that validates but is thin against its required fields
**When** it is stored
**Then** it is **reported as partial**, never presented as a finished Brief (TP-4)
**And** nothing is written until the reply validates (AD-39, TP-NFR-5).

**Given** a Director who never uses Suggest Video
**When** they use every other capability in this feature
**Then** nothing refuses, warns or degrades — Suggest Video is a prerequisite for nothing (TP-5).

## Epic 14: Refining by Talking

Depends on Epic 12. Does not depend on Epic 13 — a hand-written Brief refines just as well. *(Slices B, C, D.)*

### Story 14.1: The Assistant Can Ask, and Can Write the Brief

As the Director,
I want the assistant to ask me the questions a producer would ask,
So that it stops jumping straight to rewriting documents because that is all it can do.

**Acceptance Criteria:**

**Given** the assistant's toolset
**When** the planning tools are added
**Then** **asking and writing are separate tools**, not one tool with an optional document field (AD-38, TP-7)
**And** a turn that asks a question and writes nothing is a complete, successful turn
**And** each tool has its own strict schema with every required field promoted through `_promoted()`.

**Notes:** this is the load-bearing decision, not a stylistic one. On a model documented to drop fields silently, *an optional field and a dropped field are the same bytes* — so the shape that makes a question-only turn representable is a different tool, not a missing key. `DirectorResult` never requiring `shots` was the root cause of every empty-shots failure.

**Given** a planning turn
**When** it is recorded
**Then** it is an ordinary `TreatmentMessage`, and what it changed is carried as `MessageNotice` entries rather than as a convention inside `content` (AD-43).

**Given** any planning tool
**When** it acts
**Then** it inherits the existing refusals — locked documents, locked Shots, render provenance, the prompt gate (TP-7, R-5)
**And** it can write the Brief and propose assets, and **has no tool for writing the Treatment or the Style Bible** (TP-10).

### Story 14.2: Planning Mode

As the Director,
I want to enter a mode where the Brief is edited live,
So that refining an idea does not mean ticking a checkbox on every sentence.

**Acceptance Criteria:**

**Given** the Treatment workspace
**When** Planning Mode is entered
**Then** it is entered and left explicitly, and a **Planning bar** sits above the document column for as long as it is on, stating what the mode means and offering `[Exit planning]` (TP-6, UX-TP2)
**And** the bar reuses the wizard guidance-banner shape rather than inventing new chrome
**And** the composer's `Apply document changes` checkbox is **disabled and visibly superseded** — the suspended control must not sit there looking operable (UX-TP3).

**Given** a planning request that writes a document
**When** it reaches the server
**Then** it carries its document-write consent **explicitly, on that request** (AD-35)
**And** the server never stores, infers or remembers consent, and a write request without it is **refused**
**And** a test asserts that a planning write with no explicit consent is refused regardless of anything sent earlier.

**Notes:** session consent is a *client* affordance. What it buys is that the Director ticks nothing per turn; what it must not buy is a server that will write a document because of something it was told earlier — that ambient authority is the shape of every guard hole this project has found.

**Given** Planning Mode
**When** the Director leaves it, changes project, or reloads
**Then** consent ends, and per-turn consent returns unticked (TP-6).

### Story 14.3: See Which Half Came From Where

As the Director,
I want the assistant's contributions marked in the Brief and mine left alone,
So that I can tell what I wrote from what it wrote, six months later.

**Acceptance Criteria:**

**Given** the Brief editor
**When** attribution is rendered
**Then** it is drawn by a **mirror overlay** — a styled read-only div behind a transparent textarea, sharing its font, size, line-height, padding, wrapping and scroll exactly (AD-32)
**And** **no `contenteditable` is introduced anywhere in this application**
**And** the mirror is `aria-hidden`, with the range list as attribution's accessible form (UX-TP13).

**Given** assistant-written text
**When** it is shown
**Then** it carries a `--surface-1` wash and a 2px `--line-strong` left rule, and hovering names the turn that wrote it (UX-TP4)
**And** **the Director's own text carries no treatment at all** — the unmarked default is theirs
**And** no seventh accent is introduced; attribution uses surface and rule, not hue, because provenance is not a state (UX-TP1).

**Given** a Brief write of any kind
**When** it is stored
**Then** one **pure reconciliation** runs over (stored text, stored ranges, new text): a range whose exact text still appears survives with adjusted offsets, and a range whose text changed is **dropped** (AD-33)
**And** that function is asserted by comparison in tests
**And** this is what makes *"editing a range clears its mark"* true rather than aspirational.

**Given** an ordinary Brief save
**When** the client sends it
**Then** it carries **text only**, and the server is the sole writer of `brief_attribution` (AD-45)
**And** attribution survives reload, because it is a property of the document rather than of the session.

### Story 14.4: Step Back Through the Conversation's Changes

As the Director,
I want to undo the last few things the assistant did to my Brief,
So that changing my mind twice does not cost me the paragraph I liked.

**Acceptance Criteria:**

**Given** a Planning Mode session
**When** revisions are made
**Then** each is held in a **frontend-only, bounded** undo stack carrying prior Brief text *and* its attribution ranges (AD-34, TP-9)
**And** nothing is persisted; a reload loses the stack, and the persisted recovery slot is the durable floor holding the version the session began from.

**Given** a step back
**When** it is applied
**Then** the snapshot is restored **verbatim, bypassing reconciliation entirely** (AD-44)
**And** a test asserts that stepping back restores the marks it was meant to restore.

**Notes:** an undo is not an edit. Reconciliation exists to answer *"the Director edited this, what survives"*; routing undo through the ordinary write path would trigger it and silently strip every mark being restored — and it would look like it worked.

**Given** a step back
**When** the control is shown
**Then** it names what it will undo before doing it (UX-TP6)
**And** stepping back **never removes anything from the chat thread** — what was said stands even when what was written is undone.

## Epic 15: Knowing What the Video Needs

Depends on Epic 12 and on Epic 14's proposal tool. Works from a hand-written Brief. *(Slice F.)*

### Story 15.1: A Reviewable List of Proposals

As the Director,
I want suggested assets to wait in a list instead of going straight to the GPU,
So that I decide what the video needs before paying for it.

**Acceptance Criteria:**

**Given** asset proposals
**When** they are produced
**Then** they are held in a **Suggested Assets** tab, a fourth entry in the existing `document-tabs` strip with a count chip (TP-11, UX-TP7)
**And** each proposal shows its kind, its name, the prompt that would generate it, and **the Brief passage that called for it** (TP-11, UX-TP8)
**And** a proposal can be edited, deleted or accepted individually.

**Given** a stored proposal
**When** it exists
**Then** it is **inert** — no GPU time, no Asset, no job (TP-11, TP-NFR-2)
**And** proposals persist with the Project, written only by dedicated routes and adopted in `replace_project` via the `_adopt_*` idiom (AD-36)
**And** a test asserts a full-project PUT omitting `asset_proposals` leaves them intact.

### Story 15.2: A Stale Proposal Says So

As the Director,
I want a proposal from an idea I have dropped to tell me,
So that the list stays a queue instead of becoming a graveyard.

**Acceptance Criteria:**

**Given** a proposal whose recorded origin text no longer appears in the Brief
**When** the list is read
**Then** it is flagged as possibly stale, **decided at read time by comparison**, with nothing writing a stale flag (AD-37, TP-13)
**And** it carries an amber left edge, a Consolas `MAY BE STALE` label, and one sentence naming what changed (UX-TP9)
**And** it is **never red** — a stale proposal is not an error and nothing is broken.

**Given** a flagged proposal
**When** the Director acts on it
**Then** it is never removed automatically and never blocks anything, and accepting it is permitted (TP-13).

**Given** a proposal with no recorded origin
**When** staleness is evaluated
**Then** it is never flagged — an unknown origin is not evidence of staleness (AD-37).

### Story 15.3: Generate What I Accepted, Once

As the Director,
I want accepted proposals to render as one batch I confirmed,
So that the GPU covers exactly what the video needs and nothing it does not.

**Acceptance Criteria:**

**Given** accepted proposals
**When** generation runs
**Then** it runs as one batch after a single confirmation naming the count (TP-12, TP-NFR-2)
**And** an accepted proposal becomes an ordinary Asset, indistinguishable from one generated any other way
**And** declining costs nothing and leaves the list intact.

**Given** an accepted proposal
**When** acceptance is recorded
**Then** the proposal is **marked with the Asset id it produced, not removed** (AD-46)
**And** it is not re-offered, does not re-generate, and is not flagged stale
**And** a test asserts that accepting twice neither duplicates the Asset nor silently does nothing.

**Given** a proposal duplicating an existing Asset
**When** the list is shown
**Then** it is flagged as such **before** acceptance, naming the asset — not after it renders (TP-12).

### Story 15.4: Plan the Cast, and Bring Your Own Character

As the Director,
I want to work out who is in the video and use a photo I already have,
So that the kid on the bus is the same kid in all nine b-roll shots.

**Acceptance Criteria:**

**Given** a Song marked for more than one voice
**When** the cast is planned in conversation
**Then** the conversation asks about each of them rather than assuming one (TP-14)
**And** where the Director has no character in mind, it works out an appearance and produces a proposal for it
**And** a settled character becomes an Asset Proposal like any other.

**Given** an image the Director already has
**When** it is submitted for a planned character
**Then** the application reports whether it is already a multiview reference sheet or a single view, **saying which it found rather than guessing silently** (TP-15)
**And** a single view can be converted through the existing multiview path on confirmation
**And** the submitted image is never modified, and conversion produces a new Asset beside it.

**Given** a recurring **non-singing** character
**When** consistency is established
**Then** it is held by a character Asset, a reference sheet and citations — the same path that holds a singer's identity (TP-16)
**And** **no non-singing character is given a character slot**, because a slot is H3's speaker id and would declare them a voice in the song
**And** the standing rule that nothing infers a slot is unchanged.

## Epic 16: The Workflow Says Where It Goes

The plain navigation depends on nothing. The Song offer depends on effects Story 8.1; the Assets offer depends on Epic 15. *(Slice G.)*

### Story 16.1: Proceed to the Next Step

As the Director,
I want each workspace to name what comes next,
So that the path through the application is stated rather than inferred.

**Acceptance Criteria:**

**Given** the Song, Treatment and Assets workspaces
**When** each is shown
**Then** each offers a control at its foot naming the next phase explicitly (TP-17, UX-TP11)
**And** the control states what is not yet ready rather than being silently disabled.

**Given** any proceed control
**When** it is used
**Then** **proceeding offers; it never does** — it never generates, renders, analyses or writes a document unasked (TP-17)
**And** where a boundary carries an offer, the offer renders **inline above the button, never as a modal** — the pre-flight remains the application's only modal
**And** declining always proceeds.

### Story 16.2: One Analysis Moment at the Song Boundary

As the Director,
I want to be asked to analyse the song once, on my way to the treatment,
So that structure is there when the planner needs it instead of depending on whether I noticed a button.

**Acceptance Criteria:**

**Given** a Song that has not been analysed
**When** the Director proceeds from Song to Treatment
**Then** the analysis is **offered**, naming what it does and roughly what it costs (TP-18)
**And** declining proceeds normally — structure is never a precondition of proceeding or of Suggest Video (TP-18, R-7).

**Given** the offer is accepted
**When** the job runs
**Then** it runs as **one job with one progress state**, covering both the existing `align-lyrics` structure pass and `audio.py`'s Song Envelope (AD-40)
**And** **the two computations stay separate functions in separate modules**, each independently callable and tested; only the trigger and the reporting are shared
**And** either half failing is reported by name and does not fail the other.

**Given** a half that is already current
**When** the job runs
**Then** it is **skipped** — the envelope by its song fingerprint, the structure pass by whether sections exist for the current Song (AD-47)
**And** the job reports what it ran and what it skipped, so a fast completion reads as *"already done"* rather than as a failure to do anything.

**Notes:** effects `FX-1` produces an envelope automatically on song import, so the common case at this trigger is that one half is already done. This story cannot start before effects Story 8.1 ships.

### Story 16.3: Proceeding to Assets Offers to Build Them

As the Director,
I want the step into Assets to offer to make what I accepted,
So that the plan becomes a library in one confirmation.

**Acceptance Criteria:**

**Given** accepted Suggested Assets
**When** the Director proceeds from Treatment to Assets
**Then** generating them is offered, including any character reference-sheet conversions (TP-19)
**And** accepting is the same confirmed batch as Story 15.3, not a second path
**And** declining proceeds without spending anything.

## Epic 17: A Song Idea Becomes a Song Form

Fully independent of every other epic in this document. *(Slice H.)*

### Story 17.1: Turn an Idea into Song Fields

As the Director,
I want to describe a song in plain language and have the form filled in,
So that I am not translating my own idea into someone else's prompt format.

**Acceptance Criteria:**

**Given** the Music 3 section
**When** the Director enters a plain-language song idea
**Then** the pass fills **Title**, **Creative Direction** and **Lyrics/Section Plan**, shaped for MiniMax's documented prompt style (TP-20)
**And** every filled field remains editable before anything is generated
**And** the pass runs on the configured local model (TP-NFR-3).

**Given** a field the Director has already typed into
**When** the planner runs
**Then** it does not overwrite it without saying so (TP-20, TP-NFR-1).

### Story 17.2: The Planner Fills the Form and Stops

As the Director,
I want the planner to touch nothing but the form,
So that a fill-the-form pass never becomes a change-the-song pass.

**Acceptance Criteria:**

**Given** the Song Planner route
**When** it returns
**Then** it **writes no server state** — no Song write, no Project write, no job (TP-21, AD-42)
**And** triggering generation remains the Director's separate act
**And** a test asserts the route leaves the stored Project byte-identical.

**Given** a song generated after the planner filled the form
**When** it completes
**Then** its details transfer to Song Context by the path that already exists, unchanged by this feature (TP-21).
