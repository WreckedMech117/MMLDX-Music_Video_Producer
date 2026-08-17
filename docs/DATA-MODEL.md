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
- `lyrics`, `caption`
- `prompt_id` for generated songs

Imported and generated songs have equal project status.

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
