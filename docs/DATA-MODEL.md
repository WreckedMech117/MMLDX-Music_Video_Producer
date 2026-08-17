# Data Model

The recoverable source of truth is `data/projects/<project-id>/project.json`.

## Project

- `id`, `name`, `created_at`, `updated_at`
- `creative_brief`, `treatment`, `style_bible`
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
