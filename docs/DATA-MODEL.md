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
- `prompt_id` for generated songs

Imported and generated songs have equal project status.

`lyrics` and `caption` are stored exactly as supplied apart from leading and trailing whitespace: interior blank lines, indentation and section tags are the structure of a lyric sheet and are kept byte for byte. **Nothing parses, sections or interprets them.** A section tag in a supplied sheet looks like timing information and is not — it carries no timestamps — so no verse boundary, no BPM and no `song_section` is derived from it. That remains the song-analysis work that does not exist yet (`docs/ROADMAP.md`).

The context edit route touches those two fields and nothing else: `path`, `duration`, `source` and `prompt_id` are not on its request model at all, so an edit can never move a project's timing spine or rewrite its provenance.

Bounds are `SONG_LYRICS_LIMIT` 8,000 and `SONG_CAPTION_LIMIT` 4,000 characters, measured **after** trimming, and the route is the only place they are enforced. The textareas deliberately carry no `maxlength`: it truncated an oversized paste at the client and dropped the tail with no message, while an API client sending the same text got a documented 422. The boxes show a live count measured the same way the server measures it, and refuse to save rather than silently shorten.

Unlike `treatment` and `style_bible`, song context has **no `*_previous` recovery slot** — see `docs/DEVELOPMENT-LOG.md` for why it was declined rather than overlooked. The one unrecoverable accident, a save that replaces stored text with nothing, is guarded by a confirmation; replacing text with different text is not.

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
- `mode`: text, image, reference
- attached `asset_ids[]`
- stable `reference_labels` and optional `use_song_audio`
- `seed`
- `status`
- `prompt_id`
- `latest_output`: most recent completed render; never implies approval
- optional `latest_review`: vision continuity review of the latest output; never implies approval
- `approved_output`: only an explicitly approved take
- `locked`

Shot timing is measured in seconds against the master song. Director compilation converts it to frames only at the workflow boundary.

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
