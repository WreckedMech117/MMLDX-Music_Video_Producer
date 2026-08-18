# Deferred Work

- source_spec: `_bmad-output/implementation-artifacts/spec-song-context-on-import.md`
  summary: A max-length song context adds ~12,200 characters (~3,050 tokens) to *every* Director call, not just the first, and it is re-sent whole on each turn. On an 8192-token model it cuts the usable conversation from ~27 turns to ~8.
  evidence: Measured 2026-08-17 against `snapshot.model_dump(exclude=DIRECTOR_CONTEXT_EXCLUDE)` as `director_chat` builds it, on a project with a 26-shot plan, a filled treatment and style bible, and both recovery slots occupied. The delta is exactly constant at 12,191 characters per call (8000 + 4000 plus JSON escaping) because the whole project is dumped fresh every turn and nothing windows or truncates `messages`. Whole-dump sizes with a max sheet: 26,698 chars (~6,700 tok) at turn 0, 33,136 (~8,300) at turn 10, 39,576 (~9,900) at turn 20, 52,456 (~13,100) at turn 40 — against 14,507 / 20,945 / 27,385 / 40,265 without it. The sheet is 46% of the turn-0 prompt and still 23% at turn 40. Adding `SYSTEM_PROMPT` (587 chars) and the `DirectorResult` json_schema (715 chars, sent every call), an 8192-token window is crossed at turn 8 with a max sheet versus turn 27 without; 16384 at turn 59 versus 78. Over a 20-turn conversation the same sheet is uploaded 20 times, ~61,000 tokens of identical repeated text, and the cumulative prompt cost rises 57%. **Not acted on, deliberately:** the project's recorded root cause of Director degradation is rich context, so this is a real tension with the feature's purpose — but capping or truncating the model's view of the lyrics is precisely the "change to how the Director is prompted" the spec marks Ask First, and a silent cap would recreate the failure this feature exists to fix (the model working from a partial song). Nothing is broken today: no path errors, and the 8k crossover is a degradation, not an exception. What the numbers argue for is a *prompt-budget* story — window or summarise `messages` (the only unbounded term), or send the lyric sheet once as a pinned system fact rather than re-serialising it per turn — which is Director-prompt design and belongs to the Director, not to this spec.

- source_spec: `_bmad-output/implementation-artifacts/spec-song-context-on-import.md`
  summary: The song-context edit route blanks whichever field a request omits, mirroring `PUT /documents`.
  evidence: The shipped client always sends both, so this is unreachable from the interface; a third-party client sending only `lyrics` would clear the style. Same shape as the documents route, and the same argument for and against making it tri-state.

- source_spec: `_bmad-output/implementation-artifacts/spec-3-2-first-live-reference-render.md`
  summary: RESOLVED 2026-08-18 — the four-panel layout carry-over was undersampling, nothing more. It does not occur at the application's own default step count.
  evidence: A/B against the same Reference Sheet, same project, same prompt, same 640x384 window, one variable changed. At 4 steps (prompt `6dbe4ff7-08d2-468c-bd5b-8dce37bd68fd`, 136.5 s) the sheet's panel bands and face plate persisted in the composition. At 20 steps — the audited export's setting and `H3Request.steps`'s default (prompt `3f2a3658-4f52-45bb-87b9-2c09d4496a9a`, 497.6 s) — the frame is a coherent cinematic shot of the character in a scene, with the sheet's wardrobe carried faithfully: corset lacing, arm guards, layered fringed skirt, boots. No panel structure at all. `ref_image_size="match"` was unchanged between the two, so it was never a factor. The 4-step render was a cost-saving override in the smoke script against a graph designed for 20 with no LoRA to compensate.

- source_spec: `_bmad-output/implementation-artifacts/spec-3-2-first-live-reference-render.md`
  summary: The reference path's live evidence covers exactly one shape: one picture reference plus the master song. Video references, paired video audio, multiple pictures, `ref_image_size="max"`, and windows near the frame ceiling have never been submitted.
  evidence: The 2026-08-18 run spent its two authorised jobs on the minimum window that proves the path. Everything else on that path remains schema-audited by `tests/preflight_h3_ultra.py` and unit-tested only, which is the same standing the whole path had before this run.

- source_spec: `_bmad-output/implementation-artifacts/spec-3-1-h3-ultra-preflight-audit.md`
  summary: A reference marked `enabled: False` desynchronises the H3 payload — the adapter's index loop counts every reference, while the node's partitioning skips disabled ones, so every splitter output after a disabled item shifts down one while the condition wiring still points at the unshifted slot.
  evidence: Traced against the node source during Story 3.1 investigation. Unreachable today because `app.py` never sets `enabled`, but it is one line away, and the adapter deliberately preserves a caller-supplied flag it then ignores when indexing.

- source_spec: `_bmad-output/implementation-artifacts/spec-3-1-h3-ultra-preflight-audit.md`
  summary: `audio_mode="standalone"` on a video with `has_audio` delivers the wrong audio reference — CONFIRMED mismatch, not theoretical.
  evidence: The adapter wires no `ref_video_audio` slot for that combination, but the node appends the video's extracted track into the `audios` group ahead of any later standalone audio, so `ref_audios.ref_audio_0` resolves to the video's track rather than the intended standalone file. Traced through the node's partitioning. Unreachable from the app today (nothing sets `audio_mode`), and no test covers `audio_mode` at all.

- source_spec: `_bmad-output/implementation-artifacts/spec-3-1-h3-ultra-preflight-audit.md`
  summary: `shift_audio` is 4 on the Director graph and 3 on the reference graph's shift node; both are in range so nothing catches the discrepancy.
  evidence: Raised as the spec's Ask First item and deliberately left unchanged — neither value is provably wrong, and picking one without the Director's judgement would be guessing at a creative/technical parameter.

- source_spec: `_bmad-output/implementation-artifacts/spec-3-1-h3-ultra-preflight-audit.md`
  summary: `resolve_asset_path` and `resolve_song_path` are closures inside `create_app`, and `generate_multiview` re-implements the same containment logic inline a third time.
  evidence: Story 3.1 investigation. Means a standalone script cannot reuse the containment guard without instantiating the app, and the third copy can drift from the other two. Lifting them to module scope is a refactor this verification epic deliberately avoided.

- source_spec: `_bmad-output/implementation-artifacts/spec-3-1-h3-ultra-preflight-audit.md`
  summary: Reference file paths are resolved on the application host but consumed by the ComfyUI process, so a remote ComfyUI would receive an unreachable local path and fail only at execution.
  evidence: Works today because both run on one machine (`comfy_root` on `J:`, `data_root` on `F:`). The node performs no pre-execution existence check of its own — `VALIDATE_INPUTS` only parses JSON and counts kinds — so a bad path fails after the job is queued and the model may already be loading.

- source_spec: `_bmad-output/implementation-artifacts/spec-2-4-director-safety-notices-unmissable.md`
  summary: `document_restore_notice` is the one protective/audit statement still carried as a plain text convention — a bare `system` message with its own styling — while the story's scope sentence claims every refusal is now structural.
  evidence: Blind-hunter review. Either it belongs in the notice structure like the rest, or the scope claim should name it as the exception. Low risk because it is already visually distinct via `.message.system`, but the inconsistency will confuse the next person to add a notice.

- source_spec: `_bmad-output/implementation-artifacts/spec-2-4-director-safety-notices-unmissable.md`
  summary: The prose-claims-Shots mismatch check is a heuristic over model prose and will produce false positives no tightening fully removes.
  evidence: It exists because the recorded 2026-08-16 defect said "a four-beat sequence" rather than "shot", so a literal-word match would have missed it. Review narrowed the obvious false positives (denials, replies about existing Shots, counted structures without shot context), but the check reports rather than blocks precisely because it cannot be exact. Revisit if the notice proves noisy in real use.

- source_spec: `_bmad-output/implementation-artifacts/spec-2-4-director-safety-notices-unmissable.md`
  summary: Pre-2.4 manifests still carry the old inline `"Raw output: …"` text inside `content`, with no `notices` structure — so the model's degraded output remains in those saved projects and reaches the Director context on the next turn.
  evidence: The exclusion only strips the structured `notices` field; historical messages predate it. A migration would have to rewrite stored message content, which is a data-rewriting decision rather than a code fix. Affects only projects chatted with before 2026-08-17.

- source_spec: `_bmad-output/implementation-artifacts/spec-2-2-story-expansion-into-prompted-shots.md`
  summary: Expansion overwrites an unlocked Shot's hand-written prompt with no recovery slot — the lock is the only protection, which is a real asymmetry with the document path that gained `*_previous` slots in Story 2.1.
  evidence: Flagged by the implementer. The spec did not ask for prompt recovery, but the same argument that justified single-slot document recovery (a plausible-looking rewrite nobody asked for is permanent) applies to a prompt the Director wrote by hand.

- source_spec: `_bmad-output/implementation-artifacts/spec-2-2-story-expansion-into-prompted-shots.md`
  summary: Two expansion behaviours are reported rather than acted on, by design: a Shot the model omitted is not retried, and a returned Shot id matching no Shot is discarded rather than creating a Shot.
  evidence: Both were the spec's Ask First items. Guessing either way changes the design — retrying omissions costs another model call, and creating Shots from unknown ids lets the model add untimed Shots to the plan.

- source_spec: `_bmad-output/implementation-artifacts/spec-2-2-story-expansion-into-prompted-shots.md`
  summary: `PUT /api/projects/{id}/shots` (`replace_shots`) is an unguarded whole-list save — no overlap check, no prompt check, no lock check, and no `updated_at` conflict check — so anything expansion writes can be silently clobbered by the next inspector change from a stale client.
  evidence: Investigation for Story 2.2. `saveShotsSilently` fires on any `change` in the shot inspector and on every drag, resize, drop, add, duplicate, delete and split. `replace_project` closes this class of hole with an `updated_at` 409; `replace_shots` has no equivalent. Same sibling-write-path pattern that left holes in the song and document stories.

- source_spec: `_bmad-output/implementation-artifacts/spec-2-2-story-expansion-into-prompted-shots.md`
  summary: Song structure analysis (BPM, sections) has no implementation, so the epic's "section boundaries when analysis exists" clause can never take its populated branch.
  evidence: The analyse-structure button is a disabled stub, `#bpm-value`/`#sections-value` are hardcoded "Not analyzed", `#section-track` exists in the timeline markup but is never populated, and no model carries section data. Listed as not-done in ROADMAP's Production editing section. Until an analyser ships, expansion's section slot is an empty branch.

- source_spec: `_bmad-output/implementation-artifacts/spec-2-1-reviewable-document-replacement.md`
  summary: Re-reading the project after the Director call means a shot added concurrently now participates in the `apply_shots` index merge, so `result.shots[i]` can align to a different shot than the model saw.
  evidence: Accepted consequence of closing the stale-snapshot window (review finding 6). Strictly better than the previous behaviour, which silently deleted the concurrent shot, but it is a real behaviour change on the `apply_shots=True` path and is untested. The UI hardcodes `apply_shots: false` today, which is why it is not urgent.

- source_spec: `_bmad-output/implementation-artifacts/spec-2-1-reviewable-document-replacement.md`
  summary: No way to review a kept document version before restoring it — no preview, no diff, no length or date hint — and the recovery slot carries no provenance (no timestamp, no originating message id).
  evidence: Blind-hunter review. For a requirement named *reviewable* replacement, the review step is the missing half: the only way to see what is recoverable is to restore, read, and restore back. Would need slot metadata, which is a model change.

- source_spec: `_bmad-output/implementation-artifacts/spec-2-1-reviewable-document-replacement.md`
  summary: The restore route has no optimistic-concurrency check, so two restores — or a restore racing a chat turn — can double-swap or lose an update.
  evidence: Both edge-case and blind-hunter review. Consistent with the app-wide absence of locking already deferred for the song routes; `replace_project`'s `updated_at` check is the only precedent.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-3c-song-replacement-safety.md`
  summary: The app has no request-level locking anywhere, so a whole-manifest `store.save` can clobber a concurrent one — e.g. `remove_song` racing a `PUT /shots` could drop shot data despite the no-deletion guarantee.
  evidence: Edge-case review. Every route reads the project, mutates, and writes the entire manifest; `store.save` is atomic per write but there is no read-modify-write guard. Architecture-level concern affecting all routes, not specific to song replacement.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-3c-song-replacement-safety.md`
  summary: After a confirmed replacement with a shorter Song, Shots can sit past the end of the audio with no signal anywhere in the app.
  evidence: The spec forbids auto-adjusting Shot windows (correctly — that would be its own data loss), so the remedy is a notice surfacing orphaned Shots. Separate feature; relates to Epic 2's window flagging.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-3c-song-replacement-safety.md`
  summary: A Song whose duration is 0 (ffprobe unavailable) is treated as both loaded and lengthless; downstream planning and UI have no notion of "present but unmeasured".
  evidence: Edge-case review. Would need a `needs_measurement` concept gating treatment and shot planning — a model change, which is Ask First territory.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-3b-import-duration-regression-guard.md`
  summary: Browser coverage for the undecodable-import path, the song removal/replacement confirmations, the GPU-cost confirm dialogs, and the expansion button. NARROWED 2026-08-17: `tests/e2e_epic2_surfaces.py` now covers the notice blocks, document lock/restore, consent clearing on project change, the unprompted-clip flag, the readiness region and the Song preset rules. What remains needs either a live language model or a `window.confirm` interaction.
  evidence: Both specs claim only the executable contract for their UI halves, and say so in the docs. Browser QA remains a human release gate.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-3-live-song-generation-smoke.md`
  summary: Browser-level playback coverage for GENERATED songs, which requires ComfyUI started with `--enable-cors-header`.
  evidence: Generated songs play from a cross-origin `<comfy_url>/view` URL while `loadPersistedWaveform` uses fetch + decodeAudioData, which needs CORS headers ComfyUI does not send by default. The smoke script's urllib fetch ignores CORS entirely, so its evidence cannot generalize to a browser even in principle; `tests/e2e_audio_playback.py` covers only the same-origin imported path. Needs its own spec.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-3-live-song-generation-smoke.md`
  summary: Smoke script ergonomics — an overall time budget across variants (currently 2400 s per job, so two variants can burn 80 minutes) and an `--only <variant>` selector to retry just the failed half.
  evidence: Raised by blind-hunter review; not a correctness defect, but a real cost/iteration issue on a GPU-spending script.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-3-live-song-generation-smoke.md`
  summary: Bind the smoke script's Python `view_url()` to the frontend's `comfyOutputUrl()` with a contract test instead of maintaining two implementations.
  evidence: The repo already maintains `tests/test_frontend_contract.py` for exactly this class of duplication; the separator bug was fixed in the Python copy but nothing prevents the two drifting apart again.

- source_spec: `_bmad-output/implementation-artifacts/spec-ltx25-dimension-boundary-repair.md`
  summary: Implement the 8k+1 frame-count alignment the LTX boundary imposes (192 frames in, 185 out) so assembly can account for it alongside H3's 17k+5 grid.
  evidence: Measured in the 2026-08-17 live run. `timeline.py` encodes only `align_h3_frames` (17k+5); there is no 8k+1 helper. Assembly is unimplemented, so this is a recorded constraint awaiting the work that consumes it.

- source_spec: `_bmad-output/implementation-artifacts/spec-ltx25-dimension-boundary-repair.md`
  summary: `patch_ltx25_dimension_boundary` hardcodes divisor 32 for any template it accepts; a graph whose VAE needs only 4 or 16 would be over-cropped by up to 31 px per axis.
  evidence: Edge-case review. Low risk today because the function is LTX-2.5-specific by name and guards on LTX-2.5 node ids, but the divisor should be derived from the target VAE if the function is ever generalized.

- source_spec: `_bmad-output/implementation-artifacts/spec-ltx25-dimension-boundary-repair.md`
  summary: `test_ltx25_normalizer_divisor_makes_seedvr2_output_exact_at_every_vae_stage` recomputes KJNodes' rounding itself and asserts its own arithmetic, so it proves nothing about how `ImageResizeKJv2` actually rounds.
  evidence: Blind-hunter and verification-gap both noted it. The load-bearing protection is the direct `divisible_by == 32` assertion; verifying the node's real rounding would need a live single-node probe.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-generate-a-song-with-invented-lyrics.md`
  summary: Surface the SongPlanner-generated caption and lyrics to the Director after a run (Song.lyrics stays empty; the export's preview nodes were UI-only and never reach /history).
  evidence: Blind-hunter review confirmed nothing in the app shows what Gemma-3 actually wrote; the Director hears the song but never sees its lyrics.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-generate-a-song-with-invented-lyrics.md`
  summary: Guard against two concurrent song-generation jobs — the older job's refresh reconciliation can overwrite the newer song's path/metadata.
  evidence: Pre-existing pattern shared with the direct Music 3 route (all kind=music jobs target "song", last refresh wins); surfaced by edge-case review.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-generate-a-song-with-invented-lyrics.md`
  summary: Generated Song.duration stores the requested duration; the model-resolved FLAC length is never measured, so the timing spine can diverge from the real audio.
  evidence: Node 49 seconds is model-resolved (["45",1]) by design; the generated path never calls ffprobe (pre-existing for the direct adapter too). Song is the timing spine for Shots/Assembly.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-generate-a-song-with-invented-lyrics.md`
  summary: Expose genre_hint in the Song workspace UI (currently API-only; the form never sends it).
  evidence: Two reviewers confirmed SongPlannerRequest accepts and threads genre_hint but app.js builds only {title, idea, duration, seed}.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-generate-a-song-with-invented-lyrics.md`
  summary: Consider a persisted marker distinguishing SongPlanner-generated songs from direct Music 3 songs (job and Song both record only kind=music).
  evidence: Story 1.3's live verification and future debugging cannot tell from persisted state which adapter produced a song; a new Song field is Ask-First per the spec.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-2-generate-a-cover-from-known-lyrics.md`
  summary: Song.lyrics is now populated for known-lyrics generations but permanently empty for invented ones, and nothing documents the asymmetry.
  evidence: Downstream treatment planning cannot distinguish "no lyrics" from "lyrics exist but were never captured"; DATA-MODEL.md lists the field with no semantics. Pairs with the 1.1 entry about surfacing planner-written lyrics.

- source_spec: `_bmad-output/implementation-artifacts/spec-1-2-generate-a-cover-from-known-lyrics.md`
  summary: The API selects the SongPlanner variant implicitly by presence of the lyrics field; consider an explicit variant discriminator so a client cannot silently degrade a cover request into an invented generation.
  evidence: Verification-gap and blind-hunter both flagged that {"lyrics": null} takes the invented path with no signal; the frontend has an explicit preset concept the route lacks. Behavior is now pinned by test, but the design question stands.


## Resolved

Kept for the audit trail: these were logged as deferred and have since shipped.

- source_spec: `_bmad-output/implementation-artifacts/spec-song-duration-headroom.md`
  summary: Expose `duration_headroom` in the Song workspace UI (currently API-only; the form never sends it, so it always takes the 1.5 default).
  evidence: The spec's own reasoning is that the creator's documentation and their audited export disagree, so the Director should be able to see the choice and settle it by ear — which a form field would serve and an API-only field does not. Same shape as the `genre_hint` entry above. Two further UI consequences of the default: a duration above 240 s now takes a 422 unless the headroom is lowered, and the duration field's 30-300 s `max` no longer describes what the form can actually submit; a headroom control would need `musicFormFieldUpdate` to relate the two.
  resolved: `_bmad-output/implementation-artifacts/spec-song-duration-headroom.md` change log, 2026-08-18 -- the Song workspace carries an Encoder headroom box on both SongPlanner presets, seeded from `SongPlannerRequest.duration_headroom`'s own default and bounded by its own `ge`/`le`, with every one of those numbers asserted equal to the model rather than typed into the markup. `musicFormFieldUpdate` does not relate the two fields the way this entry guessed it would have to: bounding either against the other makes the followed field a trap, since raising one slides the other's `max` under a value already in its box. The product is shown instead -- a live readout of duration x headroom against the encoder's 360 s ceiling, from the builder's own constant -- and a product outside the schema is refused before the submit in the readout's own words, naming both ways out. Neither number is ever clamped. The duration field's 30-300 s `max` again describes what the form can submit, because the form now says what the second number does to it.

- source_spec: `_bmad-output/implementation-artifacts/spec-song-context-on-import.md`
  summary: Song context has no recovery slot. `PUT /song/context` assigns both fields from the body, so one save can delete an 8000-character lyric sheet with nothing to restore it from — while the treatment and the style bible each kept a `*_previous` copy since Story 2.1.
  evidence: Raised in review as "extend the same recovery pattern to song context". Not done, for three reasons, in order of weight. (1) The spec's frozen intent says **"Never: No new model fields"**, and a slot is a new model field — `Song.lyrics_previous` cannot be added without a human renegotiating the block, which is not the implementer's to do. (2) The threat models differ. The document slots exist because the *model* rewrote a document nobody asked it to; song context is only ever written by the Director's own explicit save click, and the Director sees the text they are about to save on screen while they click. (3) A slot alone is not recovery — it needs a restore route, a restore control, and a `documentRestoreAvailable`-style enabled state, all of which the document pattern has and none of which this story scoped. The alternative, if it is wanted: mirror Story 2.1 exactly — `Song.lyrics_previous`/`Song.caption_previous`, captured by `replace_song_context` only when it overwrites non-empty text with different text, a `POST /song/context/restore` beside the document restore route, a control in the Song workspace, and an entry in `DIRECTOR_CONTEXT_EXCLUDE` so the kept copy is not echoed into every prompt. That last part needs care that the document pattern did not: the exclusion is a top-level `{field}_previous` mapping derived from `DOCUMENT_LABELS`, and a slot nested inside `song` would need `{"song": {"lyrics_previous"}}` — the nested-path shape `app.py`'s own comment warns against, because it stops covering a field renamed or added beside it. Shipped instead as a stopgap: `songContextClearing` asks before a save empties stored text, which covers the unrecoverable accident without pretending to be recovery. It is a client-side guard only — an API client still deletes silently.
  resolved: `_bmad-output/implementation-artifacts/spec-song-context-recovery.md` — the Director renegotiated the "No new model fields" constraint on 2026-08-18, which is reason (1) withdrawn rather than worked around; `spec-song-context-on-import.md` is deliberately left as it shipped, so the record of *why* the gap existed survives. Reason (3) is built out in full: `Song.lyrics_previous`/`caption_previous`, a per-field `POST /song/context/{field}/restore` that swaps rather than pops, two restore controls in the Song workspace enabled from their own slots, and the slots cleared by all five song-changing routes. Reason (2) stands as stated and is not the justification used — the case made is the size and irreplaceability of a pasted lyric sheet, not protection from the model. Two departures from the suggested alternative, both deliberate. The capture rule is "different from stored", not "overwrites non-empty text with different text": a blank is a real previous version and a Director who pasted over an empty field may want it back, so the slots are `str | None` (`None` = nothing kept, `""` = a kept blank) and the restore refuses only on `None`. And the exclusion is not the nested path this entry warned about — `SONG_DIRECTOR_VISIBLE`/`SONG_DIRECTOR_WITHHELD` classify every field `Song` declares, and `_withheld_fields` raises at import if any field is unclassified, so the drift this objection named fails the whole suite on collection instead of leaking silently.

- source_spec: `_bmad-output/implementation-artifacts/spec-song-context-on-import.md`
  summary: Unsaved song-context typing is not covered by `state.dirty`, so switching projects discards a lyric sheet mid-edit without the discard prompt.
  evidence: Deliberate — folding it into `dirty` risks a sticky discard prompt, and the import title behaves the same way today. But it is a real loss path for text a Director may have pasted rather than typed, and the feature exists precisely because that text is worth having. Review found it applied to the `beforeunload` guard as well, and that `loadProject` cleared the flag on refreshes of the project already on screen — so a queue refresh wiped a sheet mid-paste with no question at all.
  resolved: song-context review patch — `unsavedWorkPending` now gates both the project switch and `beforeunload`, `unsavedWorkQuestion` states why the Song workspace is involved, and `songContextSeedClearedOnLoad` clears the flag only when the project actually changes. The flag stays out of `state.dirty`: it also decides whether an incidental render may re-seed the boxes, which `state.dirty` must never do. All three are executed against a stub DOM in `tests/test_frontend_contract.py`.

- source_spec: `_bmad-output/implementation-artifacts/spec-2-1-reviewable-document-replacement.md`
  summary: Document replacement still has no opt-in, while shot application does (`apply_shots`). Both documents are replaced on every chat turn regardless of what the Director asked for.
  evidence: Blind-hunter review. Recovery, locks and a notice make an unrequested rewrite visible and reversible without making it consented. A genuine product decision — raised with the Director rather than settled here.
  resolved: 78d1424 — apply_documents shipped, off by default

- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-generate-a-song-with-invented-lyrics.md`
  summary: Decide policy on the reference export embedding full real-song explicit lyrics as immutable checksummed evidence (sanitized capture vs. as-is).
  evidence: Blind-hunter review noted songplanner-invented-user-export.json nodes 55/63 carry complete real-song lyric text; the spec's Ask-First SHA pinned this exact file, so changing it requires renegotiating the audit convention.
  resolved: 22e1eae — lyrics scrubbed from both exports and from history

- source_spec: `_bmad-output/implementation-artifacts/spec-1-2-generate-a-cover-from-known-lyrics.md`
  summary: The explicit-lyrics reference-export policy question now applies to a second checksummed file (songplanner-known-lyrics-user-export.json node 63 carries verbatim third-party lyric text).
  evidence: Blind-hunter review noted the open 1.1 policy item gained a second instance; both exports are pinned by Ask-First SHAs, so any sanitization requires renegotiating the audit convention.
  resolved: 22e1eae — same scrub covered both exports

- source_spec: none
  summary: FR-12 import regression guard — prove an undecodable WAV's ffprobe duration survives application restart, and fix the stale `state.audioBuffer` bug that bypasses the fallback entirely.
  evidence: Split from Story 1.3 (three independently shippable goals; live smoke taken first). Investigation found `app.js` never clears `state.audioBuffer` on a failed decode, so importing an undecodable file into a project that already has a decodable song sends the PREVIOUS song's duration and ffprobe is never consulted. Existing coverage (`tests/test_api.py:105`) tests the route fallback only, never a store round-trip.
  resolved: b1a2d0c — guard shipped with the stale-audioBuffer fix

- source_spec: none
  summary: Song replacement/removal safety — require explicit confirmation naming that Shot windows and Assembly synchronization depend on the Song, and guarantee no Shot data is deleted.
  evidence: Split from Story 1.3. Investigation found every song-mutating path (upload_song, generate_music, generate_songplanner, replace_project) overwrites `project.song` unconditionally with zero confirmation; there is no removal route at all, and `PUT /api/projects/{id}` can null the song and wipe shots in one body. The "no Shot data deleted" AC currently holds by accident, not by design or test.
  resolved: b1a2d0c — gate shipped on all five Song-changing routes
