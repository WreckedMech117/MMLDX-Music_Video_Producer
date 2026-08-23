# Treatment Planning — Findings and Director's Rulings

Analyst: Mary · 2026-08-22 · Status: complete — rulings R-1..R-10 settled, PRD in progress

The Director's ask, in their own words: a **Suggest Video** button that takes song, lyrics and style and produces a music-video idea into the Brief; then *"chat with it to refine the idea as opposed to it just jumping to redoing treatment and story bible right away"*; a planning skill that asks questions, suggests assets, scenes and stories, and makes **visible edits as they go**; a **Suggested Assets** tab beside Brief/Treatment/Style Bible; character planning including multiview conversion; **Proceed to \<next step\>** navigation on Song, Treatment and Assets; and a **Song Planner** that turns a song idea into Title, Creative Direction and Lyrics/Section Plan in MiniMax's prompt style.

---

## Part 1 — Findings

### F-1: The Brief is the least-protected document, and this feature makes it the most-written

`DOCUMENT_CONTROLS` in `api.js` covers **two** documents:

| Document | Lock | Recovery slot | Restore | Per-turn consent |
|---|---|---|---|---|
| `treatment` | ✅ | ✅ `treatment_previous` | ✅ | ✅ |
| `style_bible` | ✅ | ✅ `style_bible_previous` | ✅ | ✅ |
| `creative_brief` | ❌ | ❌ | ❌ | ❌ |

The Brief is a bare textarea (`#creative-brief`) saved with the project and nothing else. **FR-16 — "never silently destroy a creative document" — currently has an unstated exception, and it is exactly the document this feature points a language model at.** Resolved by R-1.

**It is not, however, unused.** `creative_brief` already travels in the project dump (`timeline.py`, three call sites) and the Director prompt names it first. The gap is protection and contract, not reach.

### F-2: "Suggested assets" already exists on the backend — it just skips the reviewable step

`director.stage_manager()` returns `StageManagerResult{message, assets: [AssetProposal{kind, name, prompt}]}`, and the route at `app.py:7800` is wired to the frontend. But the control's own wording is:

> *"Ask the Stage Manager to assess the library and queue up to N Flux asset render(s)? Each proposal becomes an ordinary asset you can keep, delete, or AI Mod."*

It goes **straight from proposal to GPU**. The Director's Suggested Assets tab is therefore mostly **inserting a review step into a pipeline that already runs** — the proposal model, the prompt, the kind vocabulary and the route all exist. What does not exist is a place for a proposal to *sit* before it costs GPU minutes.

### F-3: This answers a question left open on 2026-08-18

`shot-modes-and-pre-generation-planning.md` § *Still open* asks:

> 3. Does a bulk assistant fill produce one reviewable change-set, or write shot by shot as it goes?

The Director's *"chat with it to refine … as opposed to it just jumping to redoing treatment and story bible"* is that question being answered: **incremental and visible, not a bulk change-set.** That artifact's § *The assistant* also fixes a constraint this feature inherits — *"a toolset means the assistant can change project state … Whatever tools the assistant gets should inherit [the existing refusals], not start over."*

### F-4: The assistant has two tools today, and neither is a planning tool

`director.assistant_tools()` exposes exactly `fill_shots` and `expand_prompts` — both **shot-level**. There is no tool that writes a Brief, proposes a cast, or asks a question. That is the mechanical reason the assistant "jumps to redoing treatment and story bible": those are the only things it can do.

### F-5: The character path's two known issues — **both stale, re-verified 2026-08-22**

The 2026-08-18 artifact records two problems, and checking them against the current code found the first already fixed and the second changed:

- **The multiview gate is widened.** `MULTIVIEW_SUBJECTS = {"character": "character", "prop": "object", "setting": "object"}` replaced the `kind != "character"` refusal, with two prompt templates rather than one, a frontend half in `api.js` and a contract test holding the kinds level. The character-specific "same person / identical face" wording is gone. **Nothing to do here.**
- **The Krea sampling stages need re-measuring, not assuming.** `res_multistep` now appears in the workflow builders, so the "one of three stages" finding no longer describes what ships. Whether the sheet is now the creator's full three-pass chain was not established, and this artifact does not claim it either way. **Re-verify before treating reference fidelity as a lever.**

The lesson is the finding: this codebase moves fast enough that an eight-day-old constraint is worth re-running before it is planned around.

### F-6: The model's reliability envelope is documented, and this feature lives inside it

Standing project knowledge: populate has **exceeded its 300 s timeout**; roughly **90 % of a reply is reasoning**, swinging 26× across identical rolls; the local model **silently drops boolean tool fields**; and `DirectorResult` never required `shots`, which was the root cause of every empty-shots failure. A long unattended generation is the shape most exposed to all four. Constrains R-3.

---

## Part 2 — Director's Rulings

Binding. The PRD is built on these; a change here is a change to the PRD.

### R-1 — The Brief gets the same protections as the other two documents

`creative_brief` gains `creative_brief_previous` and `creative_brief_locked`, and a `DOCUMENT_CONTROLS` entry, so lock / recovery / restore work identically across all three documents. **FR-16 stops having an exception.**

### R-2 — Planning mode grants consent for the session, not per turn

Entering planning mode grants document-write consent for that conversation; the assistant edits the Brief live and in place. This is a deliberate departure from the standing per-turn, unchecked-by-default, spent-on-send consent — chosen because *watching it converge* is the point of the interaction, and a checkbox per turn defeats it.

**`[ASSUMPTION]` A bounded in-memory undo stack backs the session.** R-1's single recovery slot holds only the previous version, and a live-editing session produces many. The two answers pair badly on their own: text the Director wrote and liked three turns back is unrecoverable from one slot. The stated resolution is a **planning-session undo stack held in memory for the life of the session, with the persisted recovery slot as the durable floor** — multi-step undo while planning, no new persisted pattern, and the slot still catches a reload or a crash. Raised with the Director 2026-08-22 and proceeding on it; a change here changes R-2.

### R-3 — Suggest Video is one long pass; refinement turns are short

**Suggest Video** performs a single generation producing a complete Brief — premise, cast, locations, arc, look — which the Director then reacts to. Subsequent refinement turns are small and fast.

**This is knowingly the risky shape** (F-6), chosen because reacting to something substantial beats inventing from nothing. It therefore carries obligations the PRD must express rather than assume:

- the pass has a bounded timeout and **retries**, because a retry is what has recovered this class of failure before;
- a failed or timed-out pass **leaves the Brief untouched** and says what happened;
- a partial or degraded result is **reported as partial**, never presented as a finished brief;
- nothing about the pass is required for the rest of planning — the Director can write a Brief by hand and refine it conversationally.

### R-4 — The planning conversation is the spine

Of the five asks, **Suggest Video plus refine-by-chat plus the Brief protections** is the spine and is planned first. The rest hang off it: Suggested Assets consumes a planned Brief, character planning is one topic inside the conversation, and *Proceed to Assets* is only meaningful once there is something to proceed with.

The other four are in scope for the feature and sequenced after: **Suggested Assets** as a reviewable list (F-2), **character planning** including multiview conversion (F-5), **Proceed to \<next step\>** navigation on Song / Treatment / Assets, and the **Song Planner** idea-to-fields pass on the Song page.

### R-6 — The Brief is named as the source, not merely included

The Brief already reaches the Director as one of three co-equal documents in the project dump. This feature makes it the thing planning writes, so it is stated as the **source document** the other two derive from: Suggest Video and the planning conversation write the Brief; Treatment and Style Bible are generated *from* a Brief the Director has settled.

Nothing about the wire changes — all three still travel in the dump. What changes is that the Brief acquires a contract (what belongs in it), primacy (it is written first and deliberately), and protection (R-1). The Director's own reading was right; only the analyst's "promotion it has never had" was wrong.

### R-7 — Suggest Video requires a Song record, however it got there

Suggest Video's precondition is a **Song with its details populated** — lyrics and creative direction — and it does not care whether they arrived by generation or by hand.

> "you need a song before the video … These being separate is because an already made song might be used and the song details manually filled in so a video could be created for existing music."

This is why the Song Planner and Suggest Video are **two features and not one flow**: an imported track with hand-filled lyrics and style is a first-class starting point, equal to a generated one. Suggest Video reads the Song record, never the generation path that produced it.

### R-8 — Recurring non-singing characters get consistency, not slots

The Director's ask, and the reasoning behind it:

> "if the narrative is about a kid doing something like going to school so the b-roll through the video tells a story that goes with the song then yes we would want them consistent."

**The consistency is granted; the mechanism is not a slot.** A `character_slot` is H3's own speaker id — the `S1`/`S2` a lyric line is tagged with to say who sings it, refused by name for any non-character asset because *"a character slot names one of the song's singers."* A recurring non-singing character placed in a slot would be declared a voice in the song.

What delivers the ask is the machinery that already exists and already works: **a character Asset, promoted to a multiview reference sheet, cited by every Shot the character appears in.** That is the same path that holds the singer's identity across thirty shots, and it is indifferent to whether the subject sings.

**Consequence for planning:** the conversation may propose non-singing recurring characters freely, and they become ordinary character Assets with reference sheets. Nothing infers a slot for them — the standing rule that nothing infers a slot is unchanged.

### R-9 — The Song Planner populates the form and stops

The Song Planner writes **Title, Creative Direction and Lyrics/Section Plan into the Music 3 form fields**, in MiniMax's prompt style, and does nothing else.

> "Song planner would just populate the form, triggering song generation is on the user."

The Director edits what it wrote and presses generate themselves. Nothing is written to the Song record and no GPU time is spent by the planner itself. The generated song's details transfer to Song Context on generation, by the path that already exists.

### R-5 — Standing constraints this feature inherits

Not renegotiated:

- **Assistant tools inherit existing refusals** — locked documents and shots, render provenance, the prompt gate. A planning tool is not a new privilege class (F-3).
- **Local-first stands.** Planning runs on the same LM Studio model as everything else. No cloud model is introduced for planning or for anything else.
- **Nothing renders without confirmation.** A suggested asset costs GPU minutes only when the Director says so — which is the whole point of R-4's reviewable list.
- **The song's own words are never re-mastered or rewritten** by the Song Planner; it fills empty fields and proposes, it does not silently replace a lyric sheet the Director supplied.

---

### R-10 — Three assumptions, confirmed by the Director 2026-08-22

Carried into the PRD as settled, not as open items:

- **Planning does not generate the Treatment and Style Bible.** It produces a Brief worth generating from; the Director triggers those separately, by the path that exists today. Planning improves their input, it does not replace the step.
- **The conversation lives in the existing Treatment chat thread**, in a planning mode — not a new surface. `Project.messages` and the existing thread are what it runs in.
- **R-2's undo stack stands as written**: bounded in-memory undo for the life of a planning session, with the persisted recovery slot as the durable floor.

### R-11 — The timeout is measured during build, and the indicator is the actual requirement

The Director, 2026-08-22:

> "We could increase the director timeout if necessary, though it's not doing the whole video just the brief, so it's not like it's doing much more work. All I ask for is an indicator it is in process. Tests could be run when developed to find a happy place."

**Raising the director timeout is authorised** where measurement shows it is needed. The value is not fixed here — it is set from live runs during the build, which is the same discipline the export presets and the over-render margin were settled by.

**The Director's stated requirement is the in-progress indicator**, not a particular number. That is already TP-4's consequence and stays there.

**One structural note the measurement should be read against.** Suggest Video's *workload* is genuinely smaller than populate's — a fresh project's dump is mostly lyrics, and the output is one document rather than a whole shot plan. So a Suggest Video pass that exceeds its timeout is evidence of **reasoning-length variance**, not of too much work. That distinction picks the lever: a longer timeout buys the tail of the distribution, a retry re-rolls it. They fix different failures, which is why TP-4 requires both.

**Two consequences for `populate`, both real:**

- Its own timeout is the one already observed to be exceeded, and the same measurement pass should settle it.
- A richer Brief produces a richer Treatment, which is `populate`'s input — so this feature *increases* what populate is handed. Planning succeeding makes populate's job larger, not smaller, and that should be measured after planning ships rather than assumed away.

### R-12 — The LM Studio context window stays at 75k unless measurement says otherwise

The Director offered to raise or lower it. **Recommendation, recorded as the ruling: leave it at 75k.**

- **Suggest Video does not need more.** Its input is a fresh project's dump — lyrics, style, an empty or thin Brief — and its output is one document. Even with reasoning at ~90 % of the reply, this is not a 75k-shaped job.
- **The planning conversation is the pass that will creep**, because the thread accumulates across turns while the project dump rides along on every one of them. The fix for that is **bounding what each turn sends** — a windowed or summarised thread — not a larger context, because a larger context only postpones the same ceiling while making every turn slower.
- **Context costs VRAM**, and this project's standing coordination ejects LM Studio before a render. A larger resident context is more to reclaim, for a job that did not need it.

Revisit only if a measured planning session actually approaches the ceiling.

### R-13 — A proposal records what motivated it

An Asset Proposal carries, besides its kind, name and prompt, **the Brief passage that called for it**. There are no Shots at planning time, so the Brief is what it points at.

This is the enabling decision for R-14: without an origin there is nothing to compare a Brief change against, and staleness is undetectable. It also makes the list explain itself — *"why is there a red bicycle in my asset list"* has an answer six months later.

### R-14 — A stale proposal is flagged, never deleted

Where the Brief passage a proposal came from has changed, the proposal is **marked as possibly stale, with the reason**, and the Director decides. Nothing is removed automatically.

The rejected alternative was clearing unaccepted proposals on a substantial Brief rewrite: it destroys proposals the Director might still want, and "substantial" is a judgement the application would have to make on its own. Doing nothing was also rejected — that is CM-T4, the list becoming a graveyard.

Sometimes the bus depot survives the rewrite. Only the Director knows.

### R-15 — Suggest Video uses song sections when they exist

Where the Song is already sectioned on the timeline, Suggest Video reads that structure; where it is not, the pass runs on lyrics and style as before.

Sections say where the choruses land and how the song is shaped, which is the skeleton a video's arc hangs on — a Brief written against real structure beats one written against a lyric sheet alone. **Sections are used when present and never required**, so R-7 stands unchanged: the control's precondition is still a Song record and nothing more.

### R-16 — Proceeding from Song offers the structure analysis

The Director's ask:

> "In the Song section, when clicking the button to progress to Treatment is when the user should be prompted to analyze the song, which would handle that section's work thus giving the planner that structure instead of it only being there if a user has progressed to the timeline and gone back."

**The premise needed one correction and the ask stands.** Verified 2026-08-22: `#analyze-song` — labelled **Analyze structure** — already sits on the **Song** page, immediately beside `#send-treatment` (**Build treatment →**). It posts to `/song/align-lyrics`, which transcribes with Whisper, times the lyric sheet's `[Tag]` blocks against the audio, and *"proposes the section boxes themselves, one per aligned block plus an Intro when the voice starts late"*, leaving prompts empty because *"timing is measured, look is authored."* Sections are neither Timeline-only nor hand-drawn from scratch.

**What is actually missing is the prompt.** `Build treatment →` does not check whether analysis has run and does not offer it, so a Director who never notices the control proceeds without structure — and Suggest Video then works from a lyric sheet alone. **The ruling is therefore a placement and prompting change to an existing capability, not new analysis work.**

Proceeding from Song to Treatment **offers** structure analysis when it has not been run. It offers; it never runs it unasked, and it never blocks proceeding.

### R-17 — One analysis moment, two analyses

The effects PRD (`prd-MusicVideoProducer-effects-2026-08-21`, **FX-1**) puts a *different* song analysis at the same song: RMS, peak, spectral flux, onsets, beats, BPM and per-band envelopes, written to a sidecar. This PRD's R-16 puts structure analysis at the Song → Treatment boundary.

**Two analyses of one song, wanting the same moment — the one point where the Director is plainly willing to wait.** They are not the same computation and should not be merged into one function, but they should share one trigger and one indicator, so a Director analyses their song once rather than twice for reasons they did not ask about.

Recorded here as a **cross-PRD dependency**, owned by whichever of the two features is built second. Neither PRD is changed by it; both should cite it.

**Ownership settled 2026-08-22: Treatment Planning owns it.** The Director accepted the recommendation that effects **Story 8.1** (the Song Envelope) is built first, as the story with no dependencies and no blockers. Treatment Planning therefore arrives second at the same song and inherits the obligation: **TP-18's offer must present one analysis moment covering both computations, with one indicator**, rather than adding a second pass the Director sits through separately. Designing for it now is cheaper than retrofitting it.

## Open questions for the PRD

1. ~~**What exactly is a "Brief" for, now?**~~ **Answered 2026-08-22, and the analyst's premise was wrong.** The Brief is *already* upstream: `timeline.py` puts `creative_brief` into the project dump at three call sites, and the Director's own prompt opens *"You are handed the whole project: creative brief, treatment, style bible, the song's words."* Treatment and Style Bible have always been generated with the Brief as an input — the Director's original understanding was correct.

   What the Brief actually lacks is narrower than a promotion: **no protection** (F-1), **no stated contract** about what belongs in it, and **no designated primacy** — it is one of three co-equal strings in the dump rather than the source the other two derive from. See R-6.
2. ~~**Does planning mode produce the Treatment and Style Bible?**~~ **Answered — see R-10.** No. Planning improves their input; the Director triggers generation separately, as today.
3. ~~**Where does the planning conversation live?**~~ **Answered — see R-10.** The existing Treatment chat thread, in a planning mode. No new surface.
4. ~~**How many characters is "multiple"?**~~ **Answered — see R-8.** Recurring non-singing characters get reference-sheet consistency; slots stay singer-only.
5. ~~**Does the Song Planner's output land in the form fields?**~~ **Answered — see R-9.** The form fields, for editing. Generation stays the Director's act.
