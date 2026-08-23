# Data Model

The recoverable source of truth is `data/projects/<project-id>/project.json`.

## Project

- `id`, `name`, `created_at`, `updated_at`
- `creative_brief`, `treatment`, `style_bible`
- `treatment_previous`, `style_bible_previous` — single-slot recovery, one prior version per document, written only when a replacement is actually applied
- `treatment_locked`, `style_bible_locked` — a lock stops the Director from replacing a document; it does not stop the human editing it
- optional `song`
- `assets[]`
- `shots[]`
- `messages[]`
- `jobs[]`

## Song

- `title`
- `source`: `imported` or `generated`
- `path`: project-relative upload or Comfy output-relative media path
- `duration`
- `lyrics`, `caption` — the song's own words and a short description of how it sounds. Both are optional, both reach the Director's context, and both are now reachable from either source: a generation path writes them from the request, and an import can carry them on the upload form or have them set afterwards through `PUT /api/projects/{id}/song/context`
- `lyrics_previous`, `caption_previous` — the one kept version of each context field, `None` when nothing has been displaced
- `vocal_type` — who sings the track: `unstated` (default), `instrumental`, `female`, `male`, `duet`, `ensemble` (3+ voices), `choir`. See below
- `prompt_id` for generated songs

Imported and generated songs have equal project status.

`lyrics` and `caption` are stored exactly as supplied apart from leading and trailing whitespace: interior blank lines, indentation and section tags are the structure of a lyric sheet and are kept byte for byte. **Nothing parses, sections or interprets them.** A section tag in a supplied sheet looks like timing information and is not — it carries no timestamps — so no verse boundary, no BPM and no `song_section` is derived from it. That remains the song-analysis work that does not exist yet (`docs/ROADMAP.md`).

The context edit route touches those two fields and nothing else: `path`, `duration`, `source` and `prompt_id` are not on its request model at all, so an edit can never move a project's timing spine or rewrite its provenance.

Bounds are `SONG_LYRICS_LIMIT` 8,000 and `SONG_CAPTION_LIMIT` 4,000 characters, measured **after** trimming, and the route is the only place they are enforced. The textareas deliberately carry no `maxlength`: it truncated an oversized paste at the client and dropped the tail with no message, while an API client sending the same text got a documented 422. The boxes show a live count measured the same way the server measures it, and refuse to save rather than silently shorten.

Song context has the same single-slot recovery the two creative documents gained in Story 2.1. `lyrics_previous` and `caption_previous` each hold the one version a save displaced, restored by `POST /api/projects/{id}/song/context/{field}/restore`, which **swaps rather than pops** so a restore is itself undoable. A save that changes nothing writes no slot — spending the one slot on a no-op would destroy what it exists to protect — and each field's slot moves independently. Slots do not outlive the song they describe: every route that replaces or removes the Song clears them, so a "previous version" can never belong to a track that is gone.

Both slots are `str | None`, which is where this **deliberately departs** from the document slots' `str = ""`. `None` means no save has ever displaced anything; `""` means a save displaced a blank, and that restores — a Director who pasted a sheet over an empty field has a real previous version and may want it back. The document slots cannot draw that distinction, which is why `restore_document` refuses an empty slot; mirroring their shape here would have made the blank-recovery case unimplementable.

Neither slot reaches the Director's context, and **that exclusion is a classification rather than a path**. `SONG_DIRECTOR_VISIBLE` and `SONG_DIRECTOR_WITHHELD` in `app.py` must between them account for every field `Song` declares; an unclassified, double-classified or stale entry raises at import. Adding a field without deciding what the Director sees aborts the test suite during collection with the field named, rather than silently leaking it into every prompt — which is what a nested exclusion path would have done. Verified by adding a field and watching it fail.

### Who sings (`Song.vocal_type`) and who sings each line

`vocal_type` is the Director's declaration of the cast, made in the Song workspace **before** the treatment. `unstated` is the default and asserts nothing — it is what every manifest written before the field existed loads as, and it is not `instrumental`: instrumental is a real declaration with real consequences, and reading it off a manifest that predates the question would be inventing a cast. Re-selecting `unstated` is how a declaration is taken back.

**Nothing infers it.** No route derives it from the lyric sheet's shape or from a library that happens to hold two characters, no vision inspection writes it, no model tool schema carries it, and the generic full-project `PUT` re-adopts the stored value rather than trusting a body — `PUT /api/projects/{id}/song/vocal-type` is its only writer. That request model carries one field and has **no default**: an omitted value would be `unstated`, so forgetting it fails loudly with a 422 instead of silently un-declaring the cast.

The per-line singer marks are **not a second field**. They live inline in `lyrics`, at the head of a line, in MiniMax H3's own speaker notation — `(S1)`, `(S1, S2)` — which `h3_prompt._SPEAKER` already parses and validates. The Director sets them from a per-line dropdown, and that dropdown *edits the lyric sheet*: there is exactly one copy of every tag, so a sheet edited afterwards cannot leave a tag pointing at the wrong words. A parallel map from line number to singer would be wrong the instant a line is inserted or deleted, and wrong silently. The tags therefore inherit the sheet's own write paths exactly and add none of their own; `PUT .../song/context` stores them, as it stores every other character of the sheet.

A tag edit touches one line and reformats nothing: separators (CRLF included), indentation, interior blank lines and `[Tag]` block structure all survive byte for byte, which the "stored exactly as supplied" contract above requires. A line whose head *looks* like a mark and cannot be read — a slot past the bound, a repeated singer, an unclosed bracket — is **reported**, never dropped and never rewritten, and the writer refuses to retag it until the Director fixes it by hand.

Which types are tagged, and why the rest are not (`models.VOCAL_TYPE_SPECS`, one table, nothing branches on a name):

| Vocal type | Slots needed | Per-line dropdown | Why |
| --- | --- | --- | --- |
| `unstated` | — | none | No cast has been declared to attribute a line to |
| `instrumental` | — | none | There is no sung line |
| `female`, `male` | — | none | One voice sings every line, and the song-level choice already names it. A dropdown whose answer never varies is noise on every line of the sheet |
| `duet` | S1, S2 | Untagged / Char 1 / Char 2 / Both | |
| `ensemble` (3+) | S1, S2, S3 | Untagged / Char 1 / Char 2 / Char 3 / All | Stops at three because that is the roster the Director named ("Char1/Char2/Char3+"); a fourth is one row here plus the bound, which is derived from this table |
| `choir` | — | none | A choir is a mass voice, not a cast — there is no Char 1 to distinguish from a Char 2. A choir song with named soloists is a duet or an ensemble *with* a choir, and is declared as one |

**Instrumental** is a real case, not an empty one. Declaring it offers no per-line tag, needs no character slot, and surfaces one recorded consequence (`models.INSTRUMENTAL_NOTE`, restating `docs/ROADMAP.md`'s stage-1 note that instrumental songs lean on the Treatment much harder). What it deliberately does **not** do is touch a shot: it does not sweep `singing` to `not_singing`, because nothing in this codebase infers a singing state and a declaration about the song is not a measurement of a window. The guard that acts here is the measured one and is untouched — `Song.vocal_spans` is Whisper's own voice activity, and a truly instrumental track measures voiceless everywhere, so populate already downgrades every window of it. A shot marked singing over a window Whisper measured as voiceless still gets no sings clause, whatever the vocal type says.

**Pass 1 withholds both new fields from every Director context dump**, and that is a decision rather than an oversight: nothing has yet been designed for a model to *do* with them, so shipping a bare key into every chat turn and every populate call would change what every existing project's model sees in exchange for a fact no instruction mentions. Pass 2 — populate reading the marks to choose a shot's character references — is where they enter the prompt, and deleting those two withhold entries is its first move.

## Asset

- stable `id`
- `name`
- `kind`: character, setting, prop, style, image, audio, video
- `path`
- `source`: upload, Flux, Krea multiview, or a later workflow adapter
- optional `parent_id` linking a multiview sheet to its source character
- `prompt`, `prompt_id`, `created_at`
- `consistency_prompt`: the **appearance anchor** — see below
- `character_slot`: which singer this character is, `0` for unslotted — see below
- optional structured vision inspection: summary, visible identity/environment details, continuity cues, prompt cues, risks, model, and analysis time

Every field an `Asset` declares is classified `ASSET_DIRECTOR_VISIBLE` or `ASSET_DIRECTOR_WITHHELD`, the same import-time guard `Song` and `Shot` carry: an unclassified, double-classified or stale entry aborts collection with the field named rather than leaking it into every Director prompt. Everything that was in the dump before that classification existed is classified visible, so it changed no prompt.

### The display name (`Asset.name`)

What this asset is called everywhere it is named: the library, the roster the planner is offered (`models.citable_assets`), the prose scan that turns a named asset into a citation (`models.assets_for_proposal`), and the reference map line a render is conditioned on (`timeline.anchored_label`).

`PUT /api/projects/{id}/assets/{asset_id}/name` is its **only editor** — five routes *create* an asset with a name (upload, Flux generation, the stage manager's fill, and the two derivations that mint a child from a source), and nothing else assigns it. The generic full-project `PUT` re-adopts the stored name per asset id, so an ordinary whole-manifest save can neither rename an asset nor undo a rename. The hazard runs the opposite way to `consistency_prompt`'s: `name` is required, so no client omits it — every client sends back whatever name it was holding, and a browser tab left open across a rename would otherwise reassert the old one on its next save. An id the stored project does not hold keeps the body's name, because a new asset has no other source for one; blanking it would produce a library row nobody can pick.

A rename **replaces the whole display name**. The ` · multiview` and ` · edit` suffixes are appended by the two derivation routes, not decorations to be preserved — preserving them would leave the Director unable to remove the very label they are renaming to get rid of, which was the Director's own reason for asking for the feature: the internal label `HarderFaster · multiview` was appearing in shot prose, and the picture is of a woman named Lucy. Trimmed, non-empty (there is no meaningful blank name, unlike an anchor or a slot), and bounded by `ASSET_NAME_LIMIT` 80 characters measured after trimming.

**What a rename cannot break, and what it does not touch.** Citations resolve by `AssetCitation.asset_id`, so no shot can lose its reference. Reference maps *are* re-derived where they can be for free, because the name is in the map. Prose already written — a shot's `prompt`, a per-shot `reference_labels` rename — keeps the old spelling: those are words a person or a model wrote, and no route edits them on a rename's behalf. The route says so in its response rather than leaving the Director to read the first prompt they open as evidence the rename failed.

Two assets may share a name. `models.assets_for_proposal` resolves a by-name reference to the first in library order and documents it, so this is a deterministic state rather than the ambiguity a character slot refuses — a slot is a link, and a name is a label that carries no citation.

### The appearance anchor (`Asset.consistency_prompt`)

One short phrase per asset naming what it looks like — "a woman in a red leather jacket and black boots" — carried into every place a description of that asset is consumed: the reference map's tag lines (`<Picture 1> is Lucy, a woman in a red leather jacket`), the H3 expansion specialist's per-reference block, and the assistant's asset library. Bounded at `CONSISTENCY_PROMPT_LIMIT` 400 characters, measured after trimming.

**It is user-owned and it wins.** Where an asset has both a `prompt` (what was asked for) and a `vision` summary (what a model saw), the anchor is what the Director says is true and outranks both; `timeline._asset_description` is the one place that ordering is written down. **Nothing infers one.** No route derives it from `prompt`, the vision inspection writes `vision` and only `vision`, and neither model-facing schema (`FillShotsArguments`, `AssetProposal`) carries the property — the recorded rule for a mechanical field, after a local model twice omitted booleans it claimed to have set.

`PUT /api/projects/{id}/assets/{asset_id}/consistency-prompt` is its **only** writer. In particular the generic full-project `PUT` re-adopts the stored value per asset id and writes `""` for an id the stored project does not hold, so an ordinary whole-manifest save can neither blank an anchor nor invent one. That is the same server-owned treatment the document recovery slots, the document locks and the message thread already get on that route, and for the same recorded reason: the body is a whole `Project` whose every field is defaulted, so a client that merely *omits* a field is indistinguishable from one that cleared it.

Empty means **no anchor stored**, not "this looks like nothing", and every consumer produces byte-identical output for it — which is what makes the field safe to add to a manifest full of existing work. Whitespace-only collapses to empty; the stored text is whitespace-collapsed when composed, because the anchor travels inside one-line prompt sentences where a line break reads as a shot boundary to the H3 specialist.

A per-shot `Shot.reference_labels` rename and an anchor compose as apposition — `the woman upstage, a woman in a red leather jacket`. The rename says who this picture is *in this shot*; the anchor says what she looks like in every shot. A rename meant to replace the appearance too is expressed by clearing the anchor, which is a decision about the asset rather than about one shot.

Child assets: a **Krea multiview sheet inherits** its source's anchor (the promotion's whole promise is that the child depicts the same subject unchanged, and the sheet is the asset shots actually cite), by copy rather than by link. An **AI Mod edit does not** — an edit is the act of changing what the subject looks like, so inheriting would carry a description the edit was run to invalidate.

### The character slot (`Asset.character_slot`)

Which of the song's singers this character *is*, as a number: a lyric line tagged `(S1)` resolves to whichever character asset holds slot 1. The link is a number on the Asset rather than an asset id in the sheet, because a sheet carrying asset ids breaks when an asset is replaced — re-slot the new character and every `(S1)` in the sheet follows it.

`0` is the default and means **unslotted**, which is what every existing asset is and what every non-singing character stays. Bounded by `CHARACTER_SLOT_LIMIT`, which is *derived* from `VOCAL_TYPE_SPECS` rather than typed, so no asset can hold a slot no dropdown can offer and no dropdown can offer a slot no asset may hold.

Meaningful only on `kind == "character"`. `PUT /api/projects/{id}/assets/{asset_id}/character-slot` is its **only** writer and refuses three ways, each leaving the asset untouched: a non-character asset by name (a slot names a singer, and a prop cannot sing), a slot another character already holds by name (one slot, one character — otherwise a tagged line points at two references and the render picks by accident), and a number past the bound, refused by the schema before the route runs. `character_slot_assets` re-validates both conditions on every read, so a hand-edited manifest cannot smuggle in a slotted prop.

The generic full-project `PUT` re-adopts the stored slot per asset id and writes `0` for an id the stored project does not hold — `consistency_prompt`'s treatment exactly, for its exact reason: `character_slot` is a defaulted `int`, so a body that merely omits it arrives as `0` and one ordinary save would un-slot the whole cast at once, leaving every `(S1)` in the sheet resolving to nothing. Nothing infers a slot: no route hands the only character asset slot 1 because it is the only one, and no model tool schema exposes the field.

**Checked at Populate Timeline, flagged and never refused.** `models.vocal_cast_problems` compares the declared type's slots against the library and reports the shortfall by name — "Duet declared, and 1 of the 2 character slot(s) it needs are filled — S2 unfilled" — on `PopulateTimelineResponse.cast_notices`. The plan still lands: the slots are set in another tab, and sending the Director away from the button they pressed would cost them the plan. It is silent for every type that names no cast, because a type with no per-line marks has no `(S1)` to resolve and therefore nothing a slot would be needed for.

## Shot

- stable `id`
- `start`, `duration`, computed `end`
- editable `prompt`
- `mode`: `ShotMode | None`, where **`None` means undeclared** — see below
- `citations[]`: `{asset_id, role, order}` — what the shot cites and what for
- `h3_prompt`: the H3-format expansion of `prompt`; empty means not expanded
- `asset_ids[]`: the reference-role **projection** of `citations`, kept for compatibility
- `singing`: `SingingState`, one of `unknown` / `singing` / `not_singing`
- stable `reference_labels` and optional `use_song_audio`
- `seed`
- `status`
- `prompt_id`
- `latest_output`: most recent completed render; never implies approval
- optional `latest_review`: vision continuity review of the latest output; never implies approval
- `approved_output`: only an explicitly approved take — and it finally has a writer: the approve route alone assigns it (`:= latest_output` at the moment of approval, never a client-supplied path), `status: "approved"` moves with it as one write, and un-approve is the sole reversal. While approved, `latest_output` cannot move (render-again refuses approved Shots), so `approved_output == latest_output` is an invariant, pinned by test
- `approved_start`, `approved_duration`: AD-13's window snapshot — the window the shot had at the moment of approval, written and cleared with `approved_output` by the same two routes and nothing else (a one-writer scan pins all three). Assembly compares them to the live window and refuses a moved one by shot ID. `approved_duration == 0` means *never snapshotted* (`duration` itself is `gt=0`, so zero is unrepresentable as a real window) — a legacy approval refuses assembly with re-approve wording, distinct from the stale wording. Withheld from the Director's context alongside `h3_prompt`: staleness bookkeeping, near-duplicates of the live window the chat model already sees
- `flagged`: AD-5's re-render mark — the Director's own, set by eye on a take, resubmitted by the flagged-scope batch, cleared only by that shot's successful resubmission or by hand. Never by the batch draining, and nothing infers it. Withheld from Director context
- `latest_take_lead`: how far before the shot's window its take begins — the sync-correct offset of the over-render margin, written at submission by `generate_h3` alongside `prompt_id` and by nothing else. Recorded rather than derived: a pre-margin take and a post-margin one are indistinguishable by arithmetic on their lengths, and every take rendered before the margin correctly reads 0. Withheld from Director context (render bookkeeping)
- `trim_nudge`: the Director's fine-tune on top of the lead, in seconds (negative allowed up to the lead). Effective cut = `latest_take_lead + trim_nudge`, **one rule** read by the Monitor, the inspector and assembly alike, contract-tested across the client/server pair. Deliberately not snapshotted at approval and still editable on an approved shot: it selects a slice of the approved file while the file stays immovable. Withheld from Director context (the human's own editorial control)
- `locked`

Shot timing is measured in seconds against the master song. Director compilation converts it to frames only at the workflow boundary.

**Two prompt fields, and the second is not a replacement.** `prompt` holds the short readable intent a human wrote or pass one produced; `h3_prompt` holds the long machine-facing expansion in MiniMax's documented format. Overwriting the intent would destroy what pass one wrote and leave nothing to re-expand from — and the first expansion will not be the good one. Empty means *not expanded*, which is a real state: a Shot is plannable long before it is expanded, and the render path falls back to `prompt` exactly as it always did.

`h3_prompt` is **withheld from the Director's context** — the first field ever withheld from a Shot. Not a removal, since it was never in the dump: withholding it adds nothing to the prompt rather than subtracting something. Withheld on the numbers, because a thirty-Shot plan of expansions would add many thousands of tokens to *every* chat turn, and rich context is this project's recorded cause of Director degradation. The chat Director writes treatments and intents; the expansion specialist gets its own purpose-built payload rather than this dump.

**A shot declares what it is; it is no longer inferred.** `generate_h3` used to choose between the text-only and reference paths by asking whether `asset_ids` happened to be empty, so a shot could not say what it was meant to be or be wrong about it before a render. `SHOT_MODE_SPECS` is now a table — label, per-role minimum and maximum, whether the mode can take song audio, and which adapter it routes to — so adding a mode is a row rather than a branch.

**`None` means undeclared, and that is deliberate.** A `Shot.mode` field already existed as a dropdown position nobody read, carrying legacy strings. Reading those as declarations would have changed what existing shots render, so they resolve to *undeclared* instead, and the new vocabulary deliberately shares no spelling with them (`"reference"` became `"references"`) so the two can never be confused. An undeclared shot resolves the way it always behaved — citations present means references, absent means text-only — and renders a byte-identical payload, pinned by digests taken from the previous commit.

**Roles live on the citation, never on the asset.** The same library asset is a reference in one shot and a middle frame in another; the wolf is not "a middle frame", it is a middle frame *in this shot*. Putting the role on the asset would force a duplicate per part and make a plan unrevisable. `citations` is the truth and `asset_ids` is its projection onto the reference role, kept in agreement by a model validator in both directions — so a legacy manifest migrates on read without being rewritten, and re-roling an asset to `middle` stops it being sent as a reference picture.

**One reference numbering, in one function.** `models.numbered_references` lays the tags over `citations_in_prompt_order`'s walk and is the only thing that assigns them: the stored reference map, the payload submitted to ComfyUI, the bounds count and the expansion input handed to the specialist all read it. Three counters, never one — H3 wires pictures, videos and audios into three separate per-kind slot lists, so `<Picture 2>` and `<Video 2>` are different slots and neither is "the second reference" — with keyframe roles numbering into the Picture series (a frame is a picture) and the master song taking the slot after every cited audio. It is one function because it was two: the expansion input ran its own single-series counter, so a shot citing a video told its specialist `<Picture 2>` for the slot the payload wires as `<Video 1>`, and H3's slots are anonymous enough that the take would have come back plausible and wrong (found and fixed 2026-08-20). A citation whose asset the project does not hold is still numbered, so the surviving tags do not shift; the render refuses it by name.

**`singing` is tri-state on purpose.** It is a `Literal`, not `bool | None`, so `if not shot.singing` cannot quietly mean "not singing". `unknown` is not `not_singing`: the LTX enhancer moves lip position, so a wrong default in either direction is worse than an honest absence. **Nothing infers it** — a test greps all of `src/` for any assignment that would. It is a property of the *performance*, not of the mode, because a references shot may or may not be a singing shot.

**Four modes are plannable and not yet renderable.** A mode with no adapter is refused at render with a clear reason rather than hidden, because laying out a first/middle/last section before its adapter exists is useful planning work. Three rows from the planning table are deliberately *not* modes: image editing is a `length: 5` reference render and so a parameter of `references`; enhancement is an operation on a take with its own route; and slicing and audio replacement are file utilities.

## SongSection

- stable `id`, free-text `label` (max 60), `start`, `duration`, computed `end`
- `prompt` — the section's shared characteristics, layered under every shot inside it

Held on `Project.sections` (defaulted, so every manifest loads unchanged; empty means unmarked and every reader treats absence as unknown). A shot belongs to the section holding its **midpoint**; the label pairs with the lyric sheet's `[Tag]` blocks by order of appearance within a label family, which is how sections carry the timing the sheet's tags lack. Nothing infers sections: the Director marks them (or accepts populate's repaired proposal, which never overwrites marks).

## TreatmentMessage

- stable `id`, `role` (user, assistant, system), `content`, `created_at`
- `notices[]` — the protective refusals, changes and flags a Director turn produced, carried as data rather than as text appended to `content`

## MessageNotice

- `text` — the sentence shown to the Director; also present in the message's `content`, which is what the marker-substring consumers read
- `raw` — the model output the notice is *about*, capped in length because the manifest is persisted

`notices` and its `raw` are excluded from the context sent back to the language model. That is the rule, not an optimisation: the raw field holds the degraded output a refusal was raised over, and feeding it back is the self-reinforcing failure `document_rejection` exists to catch. See `docs/LLM-DIRECTOR.md`.

Messages saved before 2026-08-17 carry no `notices` and still have the old inline `Raw output: …` text inside `content`; they load unchanged, but that historical text is not covered by the exclusion.

## RenderJob

- stable `id`
- `kind`: music, Flux, multiview, H3, LTX, post
- `status`: queued, running, complete, error, cancelled
- `batch_id` — AD-5: a batch is the set of jobs sharing this id, active iff any member is non-terminal — always derived, never stored as a status. Empty for jobs submitted outside a batch
- Comfy `prompt_id` — **empty means local work** (AD-9): an assembly runs in the app's own ffmpeg and never touches ComfyUI, and the empty id is the marker every consumer keys on — `reconcilable_jobs` skips it, the frontend poll ignores it, and the export reader selects by it
- `target_id` — the constant `"assembly"` for assemblies
- `seed` — 0 by design for local work; nothing sampled
- `output_files[]` — an assembly records its export here, media-relative (`exports/assembly_00001.mp4`)
- `inputs[]` — FR-24 adapted for local work: what the job consumed, as `"<shot_id>=<approved_output>"` pairs, so an export is rebuildable from its record. Empty for every ComfyUI job, whose inputs are the submitted graph `prompt_id` already names
- exact `error`
- `superseded_by` — the id of the job that replaced this one for the same target, or empty. Written only when a new render is accepted for a target that still had an unsettled record (`batch.supersede_target_jobs`): the leftover settles as `cancelled` and this names its successor, which is what tells a superseded record from one the Director cancelled by hand. Provenance only — nothing reads it for a decision — and the superseded record keeps its `prompt_id`, so a file an already-executing ComfyUI prompt still writes stays traceable to it
- timestamps

A `running` local job whose process died with the application — a restart's leftover — is healed to `error` **at application startup and again at the next assemble**, by one rule called at both moments, rather than blocking forever; the in-process registry of live assemblies is the one truth about "still running" for jobs no ComfyUI history can settle, and boot passes the empty registry a just-started process actually has. A job carrying a `prompt_id` is never healed this way: ComfyUI is user-managed and may still be executing it, so it stays the reconciler's.

## Provenance rule

No output is considered reproducible unless the manifest retains the workflow kind/version, prompt ID, seed, semantic target, and output path. Later schema revisions will add explicit workflow hashes and model selections while preserving backward compatibility.
