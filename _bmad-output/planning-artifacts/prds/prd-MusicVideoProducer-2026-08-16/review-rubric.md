# PRD Quality Review — Music Video Producer

Reviewed 2026-08-16 against `prd.md` (25 FRs, 8 features) and `addendum.md`.

## Resolution status — updated 2026-08-16 after fixes

All three **high** findings and every **medium** finding were resolved in the same session. Findings below are preserved as written at review time; this table records what happened to each.

| Finding | Severity | Outcome |
|---|---|---|
| No cross-cutting NFR section | high | **Fixed** — §4A added with NFR-1 (UI responsiveness during a Batch) and NFR-2 (project-state integrity). Scoped to the two the Director judged load-bearing; render economics and storage growth deliberately remain open questions, since no measurement yet supports a number. |
| "materially shorter" not testable (FR-15) | high | **Fixed** — rejects output that parses as JSON, or that is under 40% of the length of the non-empty document it would replace. Checks skipped when the target is empty. |
| "visibly insufficient headroom" not testable (FR-11) | high | **Fixed by redesign** — the threshold was rejected as needing per-model, per-machine retuning. FR-11 now detects and names the loaded language model instead, which is identifiable and actionable. |
| Finishing as schedule risk to SM-1 | medium | **Fixed** — sequenced last, with an explicit drop condition written into §4.7. |
| Non-Goals not updated for live timeline | medium | **Fixed** — non-goal added bounding live population against live *preview*. |
| "reliable H3 window" implicit (FR-17) | medium | **Fixed** — stated as 4–15 seconds inline. |
| Assumptions Index does not round-trip | medium | **Fixed** — 7 inline tags, 7 index entries. |
| FR-9 has no target, pending measurement | medium | **Accepted as-is** — remains blocked on Q1 by design. Must not be estimated as a story until Q1 is answered. |
| SM-3 unmeasurable | low | **Fixed** — restated as "without leaving the Production Wizard." |
| Open-item density | low | **Accepted** — appropriate at solo stakes. |
| UJs use a role, not a personal name | low | **Accepted deliberately** — naming a fictional protagonist for a single-operator tool whose only user is the author would be theater. |

## Overall verdict

The PRD is decision-ready on the things that matter most: it names hard reversals explicitly, its scope boundaries do real work, and its requirements are grounded in measurements taken the same day rather than in guesses. What is at risk is the layer beneath the features — there is **no cross-cutting NFR section at all**, which is a conspicuous omission for a product whose entire economics are render time, VRAM, and storage. Three FR consequences are written with adjectives instead of bounds, and the Assumptions Index does not round-trip. None of these are structural; all of them will hurt story creation if they go in unfixed.

## Decision-readiness — strong

Decisions are stated as decisions and the trade-offs name what was given up. §4.2 does not merely adopt targeted regeneration — it says outright that this reverses `ROADMAP.md`'s "Multiple takes and approval," and §4.7 says Finishing stays in MVP against the brief's own recommendation. A reader pushing back on either finds their objection already acknowledged with attribution.

Open Questions are genuinely open. Q1 (model residency) and Q8 (SageAttention) both name work that would change the answer, and neither is answered in the following sentence. Q10 explicitly separates fixing the symptom from fixing the cause, which is the kind of distinction that usually gets smoothed away.

### Findings
- **low** Open-item density is high for a document meant to green-light building (§8, §9) — 11 open questions, 4 inline assumptions, 5 PM notes. *Fix:* acceptable at solo/hobby stakes, but triage Q1 and Q8 before story creation since both change render economics for every FR.

## Substance over theater — strong

No persona theater: one user, honestly described, with an explicitly bounded "later" audience. No innovation theater — §"What Makes This Different" in the source brief goes out of its way to say the moat is execution rather than technology, and the PRD does not re-inflate it. The Vision paragraph could not be swapped into another PRD in this category; "ComfyUI renders anything and remembers nothing about a production" is specific to this problem.

The counter-metrics are the strongest evidence against theater. SM-C1, SM-C2, and SM-C3 each name a plausible way this product could be made worse by optimizing something reasonable, which is what counter-metrics are for.

## Strategic coherence — strong

There is a thesis: the hard part is structure around the clip, not the clip. Features follow it — the Wizard, batch-then-regenerate, continuity as a data relationship, and provenance all serve "hold the production together," while nothing in §4 is a capability included because it was easy.

MVP scope kind is coherently problem-solving: SM-1 is binary and gates everything else.

### Findings
- **medium** §4.7 Finishing sits awkwardly against the thesis (§1, §4.7). Finishing is a quality multiplier, and its own note concedes the prerequisite adapter does not exist. Its presence in MVP came from a user instruction that reversed the brief, not from the thesis. *Fix:* keep it, but sequence it last explicitly and state the condition under which it drops out, so the reversal does not silently become a schedule risk against SM-1.

## Done-ness clarity — thin

This is the weakest dimension and the one story creation will lean on hardest.

Most FRs are good — FR-1's "step is a pure function of the Project manifest," FR-12's "duration matches the Song within one frame," and FR-5's isolation conditions are all directly testable. But three consequences are written with adjectives where they need bounds, and one FR defers its own target.

### Findings
- **high** "materially shorter than the document it would replace" (§4.5 FR-15) is not testable. *Fix:* state a rule — e.g. reject when the replacement is under 40% of the existing document's length, or when it parses as JSON, whichever triggers first.
- **high** "visibly insufficient headroom" (§4.3 FR-11) is not testable. *Fix:* bind it to a number derived from the measurement already in the addendum — warn when free VRAM is below the observed peak requirement plus a margin.
- **medium** "longer than the reliable H3 window" (§4.5 FR-17) leaves the window implicit. *Fix:* say 4–15 seconds inline; the number is already established in `docs/LLM-DIRECTOR.md` and the Glossary does not carry it.
- **medium** FR-9 has no target at all, by design, pending measurement (§4.2). *Fix:* acceptable, but it cannot become a story until Q1 is answered. Mark it explicitly as blocked rather than letting it be estimated.
- **low** SM-3 "without the Director consulting documentation" is unmeasurable with a single user who wrote the documentation. *Fix:* restate as an observable — first completed Shot reached without leaving the Wizard.

## Scope honesty — strong

§5 Non-Goals does real work, particularly "Not a take-comparison tool," which prevents a whole class of well-intentioned future scope creep. §6.2 marks the emotionally load-bearing deferral (undo/redo) with a PM note rather than burying it. The brownfield honesty is unusual and genuine: §4.6 states plainly that the reference path "has never produced a live render from this application," and §4.5 documents a defect in the product's own current behaviour rather than describing only the desired end state.

### Findings
- **medium** §5 Non-Goals was not updated after the live-timeline requirement landed (§5 vs §4.2 FR-7, FR-8). *Fix:* add a non-goal bounding it — live population is not live *preview* of an in-progress render, and does not imply scrubbing partial output.

## Downstream usability — adequate

Glossary is present and the domain nouns are used consistently. FR IDs are contiguous 1–25 with no duplicates, and SM cross-references resolve after renumbering. Sections survive being pulled out alone.

### Findings
- **high** No cross-cutting NFR section exists (missing from §4–§7). For a product whose binding constraints are render duration, VRAM ceiling, and storage growth, these live nowhere. Feature-local notes cover fragments, but there is no home for "a production must fit in X GB," "the application must remain responsive while a batch runs," or "manifest writes must stay atomic under concurrent shot saves" — the last of which the existing code already implements. *Fix:* add a Cross-Cutting NFRs section pulling in the measured numbers from the addendum.
- **medium** Assumptions Index does not round-trip (§9). Seven index entries, four inline tags. The §2, §7 SM-6, and §7 SM-2 entries have no inline counterpart, and one inline tag is a bare `[ASSUMPTION]` with no content. *Fix:* reconcile both directions.
- **low** UJs use a role ("the Director") rather than a personal name, against the rubric's default. *Fix:* leave as is — see Shape fit. Naming a fictional protagonist for a single-operator tool whose only user is the author would be theater.

## Shape fit — strong

Correctly shaped as a brownfield, single-operator capability spec with light UJ scaffolding. Three journeys is right: enough to carry the Wizard's rationale and the batch-review interaction, not so many that they become furniture. Existing-code references are accurate — `app.py`'s unconditional assignment, `DirectorTimeline.aligned_frames`, and `PathchSageAttentionKJ` were each verified against source during this pass. New and existing behaviour are distinguished throughout.

Rigor is calibrated to solo stakes: no stakeholder sign-off, ROI, compliance, or rollout sections, correctly.

## Mechanical notes

- **ID continuity:** FR-1 → FR-25 contiguous, unique. UJ-1 → UJ-3, SM-1 → SM-8 plus SM-C1 → SM-C3, all resolving.
- **Glossary drift:** none material. "Batch" was added when FR-7/FR-8 introduced it, correctly.
- **Assumptions roundtrip:** fails in both directions (see above).
- **Cross-references:** FR-9 correctly updated in Q1 after renumbering; no stale FR-7 references remain.
- **Section numbering:** §4.1–§4.8 sequential after insertion.
