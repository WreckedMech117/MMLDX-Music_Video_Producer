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
- `prompt_id` for generated songs

Imported and generated songs have equal project status.

`lyrics` and `caption` are stored exactly as supplied apart from leading and trailing whitespace: interior blank lines, indentation and section tags are the structure of a lyric sheet and are kept byte for byte. **Nothing parses, sections or interprets them.** A section tag in a supplied sheet looks like timing information and is not — it carries no timestamps — so no verse boundary, no BPM and no `song_section` is derived from it. That remains the song-analysis work that does not exist yet (`docs/ROADMAP.md`).

The context edit route touches those two fields and nothing else: `path`, `duration`, `source` and `prompt_id` are not on its request model at all, so an edit can never move a project's timing spine or rewrite its provenance.

Bounds are `SONG_LYRICS_LIMIT` 8,000 and `SONG_CAPTION_LIMIT` 4,000 characters, measured **after** trimming, and the route is the only place they are enforced. The textareas deliberately carry no `maxlength`: it truncated an oversized paste at the client and dropped the tail with no message, while an API client sending the same text got a documented 422. The boxes show a live count measured the same way the server measures it, and refuse to save rather than silently shorten.

Song context has the same single-slot recovery the two creative documents gained in Story 2.1. `lyrics_previous` and `caption_previous` each hold the one version a save displaced, restored by `POST /api/projects/{id}/song/context/{field}/restore`, which **swaps rather than pops** so a restore is itself undoable. A save that changes nothing writes no slot — spending the one slot on a no-op would destroy what it exists to protect — and each field's slot moves independently. Slots do not outlive the song they describe: every route that replaces or removes the Song clears them, so a "previous version" can never belong to a track that is gone.

Both slots are `str | None`, which is where this **deliberately departs** from the document slots' `str = ""`. `None` means no save has ever displaced anything; `""` means a save displaced a blank, and that restores — a Director who pasted a sheet over an empty field has a real previous version and may want it back. The document slots cannot draw that distinction, which is why `restore_document` refuses an empty slot; mirroring their shape here would have made the blank-recovery case unimplementable.

Neither slot reaches the Director's context, and **that exclusion is a classification rather than a path**. `SONG_DIRECTOR_VISIBLE` and `SONG_DIRECTOR_WITHHELD` in `app.py` must between them account for every field `Song` declares; an unclassified, double-classified or stale entry raises at import. Adding a field without deciding what the Director sees aborts the test suite during collection with the field named, rather than silently leaking it into every prompt — which is what a nested exclusion path would have done. Verified by adding a field and watching it fail.

## Asset

- stable `id`
- `name`
- `kind`: character, setting, prop, style, image, audio, video
- `path`
- `source`: upload, Flux, Krea multiview, or a later workflow adapter
- optional `parent_id` linking a multiview sheet to its source character
- `prompt`, `prompt_id`, `created_at`
- optional structured vision inspection: summary, visible identity/environment details, continuity cues, prompt cues, risks, model, and analysis time

## Shot

- stable `id`
- `start`, `duration`, computed `end`
- editable `prompt`
- `mode`: `ShotMode | None`, where **`None` means undeclared** — see below
- `citations[]`: `{asset_id, role, order}` — what the shot cites and what for
- `asset_ids[]`: the reference-role **projection** of `citations`, kept for compatibility
- `singing`: `SingingState`, one of `unknown` / `singing` / `not_singing`
- stable `reference_labels` and optional `use_song_audio`
- `seed`
- `status`
- `prompt_id`
- `latest_output`: most recent completed render; never implies approval
- optional `latest_review`: vision continuity review of the latest output; never implies approval
- `approved_output`: only an explicitly approved take
- `locked`

Shot timing is measured in seconds against the master song. Director compilation converts it to frames only at the workflow boundary.

**A shot declares what it is; it is no longer inferred.** `generate_h3` used to choose between the text-only and reference paths by asking whether `asset_ids` happened to be empty, so a shot could not say what it was meant to be or be wrong about it before a render. `SHOT_MODE_SPECS` is now a table — label, per-role minimum and maximum, whether the mode can take song audio, and which adapter it routes to — so adding a mode is a row rather than a branch.

**`None` means undeclared, and that is deliberate.** A `Shot.mode` field already existed as a dropdown position nobody read, carrying legacy strings. Reading those as declarations would have changed what existing shots render, so they resolve to *undeclared* instead, and the new vocabulary deliberately shares no spelling with them (`"reference"` became `"references"`) so the two can never be confused. An undeclared shot resolves the way it always behaved — citations present means references, absent means text-only — and renders a byte-identical payload, pinned by digests taken from the previous commit.

**Roles live on the citation, never on the asset.** The same library asset is a reference in one shot and a middle frame in another; the wolf is not "a middle frame", it is a middle frame *in this shot*. Putting the role on the asset would force a duplicate per part and make a plan unrevisable. `citations` is the truth and `asset_ids` is its projection onto the reference role, kept in agreement by a model validator in both directions — so a legacy manifest migrates on read without being rewritten, and re-roling an asset to `middle` stops it being sent as a reference picture.

**`singing` is tri-state on purpose.** It is a `Literal`, not `bool | None`, so `if not shot.singing` cannot quietly mean "not singing". `unknown` is not `not_singing`: the LTX enhancer moves lip position, so a wrong default in either direction is worse than an honest absence. **Nothing infers it** — a test greps all of `src/` for any assignment that would. It is a property of the *performance*, not of the mode, because a references shot may or may not be a singing shot.

**Four modes are plannable and not yet renderable.** A mode with no adapter is refused at render with a clear reason rather than hidden, because laying out a first/middle/last section before its adapter exists is useful planning work. Three rows from the planning table are deliberately *not* modes: image editing is a `length: 5` reference render and so a parameter of `references`; enhancement is an operation on a take with its own route; and slicing and audio replacement are file utilities.

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
- Comfy `prompt_id`
- `target_id`
- `seed`
- `output_files[]`
- exact `error`
- timestamps

## Provenance rule

No output is considered reproducible unless the manifest retains the workflow kind/version, prompt ID, seed, semantic target, and output path. Later schema revisions will add explicit workflow hashes and model selections while preserving backward compatibility.
