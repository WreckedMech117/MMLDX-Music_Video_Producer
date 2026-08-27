# LLM Director

## Purpose

The Director translates natural creative conversation into structured, editable production records:

- assistant reply
- treatment
- visual continuity/style bible
- timed shot suggestions

It never queues expensive H3 or LTX renders directly.

## Provider abstraction

The current adapter targets an OpenAI-compatible `/chat/completions` endpoint and works with local or remote providers that support JSON response format.

Examples:

```text
LM Studio: http://127.0.0.1:1234/v1
Ollama:    http://127.0.0.1:11434/v1
```

Configure:

```text
MVP_LLM_BASE_URL=http://127.0.0.1:1234/v1
MVP_LLM_MODEL=<loaded-model-name>
MVP_LLM_API_KEY=
```

No provider or secret is hard-wired. Missing configuration produces a visible unavailable state and a 503 API response.

## Structured contract

The model must return one JSON object:

```json
{
  "message": "What changed and why",
  "treatment": "Full editable treatment",
  "style_bible": "Continuity, color, lighting, lenses, wardrobe and locations",
  "shots": [
    {"start": 0, "duration": 5, "prompt": "Shot intent"}
  ]
}
```

Shot duration validation allows up to 30 seconds for planning, but the system prompt prefers H3's reliable 4–15 second range. Timeline validation warns when a render window falls outside that range.

## Shot expansion contract

`POST /api/projects/{id}/director/expand` is a second, separate call with its own schema. It turns the treatment, the style bible and the existing timed shot windows into one written prompt per shot, in a single whole-plan pass — per-shot calls cannot see each other, and cross-shot variance is the point.

```json
{
  "message": "The through-line written across the plan",
  "shots": [
    {"shot_id": "shot_ab12cd34ef56", "prompt": "Render-ready prompt for that shot"}
  ]
}
```

`shot_id` is required and is how results are merged: **never by position.** A prompt is free text, so a positional merge after a concurrent add, delete or split would write a plausible prompt onto the wrong shot and nothing downstream would fail.

The input is not the chat route's project dump. `timeline.expansion_input(project)` builds a purpose-built, trimmed payload: the creative brief, treatment and style bible; a `song` block carrying title and duration, plus `lyrics` and `caption` when the song has them; the H3 shot window; and per shot its id, ordered index, start, end, duration, current prompt, lock state, an `outside_h3_window` flag, and the neighbouring shots' **id and window** — deliberately not their prompts, which on a first expansion are all `""` or `"New shot"` placeholders anyway, and carrying them would have shipped every prompt three times over in a payload whose stated purpose is to be trimmed. The full neighbour entry stays reachable by id or by `index ± 1`. It carries no `status`, `prompt_id`, `latest_output`, `latest_review` or `approved_output`, because the recorded root cause of Director degradation is rich context.

The index the model is given and the index the reply's notices name are the same one, both derived from `timeline.ordered_shots` — shots sorted by start, which is not necessarily manifest order.

The route takes **no request body** — expansion is over the whole plan, so there is nothing to parameterise. Status codes: **422** when the project has no shots to expand, refused before any model call; **503** when no language model is configured or reachable; **502** when the model replies with something unusable. On any of them nothing is written.

Two slots are **absent rather than fabricated**: `song_fraction` when there is no song or its duration is unknown, and `section` when the project has no sections — `timeline.song_section` returns `None` rather than an invented boundary, and the key is omitted rather than sent empty.

*Corrected 2026-08-26.* This sentence used to say `section` was absent **always**, "nothing in this project analyses song structure". Both halves are false. `Project.sections` has existed since 2026-08-19 and is filled by `POST .../song/align-lyrics` (Whisper transcription, the sheet's `[Tag]` blocks timed against it) and by the two-stage populate's structure call. When a project has sections, `expansion_input` sends `section: {label, prompt}` on each Shot and the H3 payload sends `{label, prompt, clip_position}`. The absent-rather-than-fabricated rule is what actually generalises, and it still holds — it is now conditional rather than unconditional.

Expansion writes prompts only. It never retimes a window, never queues a render, and never rewrites a shot that is either **locked** or **carries render provenance** — anything with a submitted prompt id, a take on disk, an approval, or a status past `draft`. For those, the prompt is no longer an intention but the record of what produced a specific piece of media, so rewriting it in place would leave the take and its prompt quietly disagreeing. Note the consequence: marking a shot `ready` takes it out of expansion's reach.

A returned id matching no shot, a shot the model omitted, a prompt that parses as JSON, and the model answering for the same shot twice are each reported in the reply and not applied — on a duplicate, the first answer wins. The refused text itself is kept **beside** the notice, in the notice's `raw` field — not inside the message's `content`. The thread becomes context for the next chat turn, so persisting degraded JSON in `content` would feed the exact failure `document_rejection` exists to catch; carrying it in a field that `DIRECTOR_CONTEXT_EXCLUDE` strips lets the Director inspect what was refused without the model ever seeing it again. This replaced an earlier design that dropped the refused text entirely, which kept the invariant but left a refusal you could not examine.

The reply is appended to the project's chat thread as an assistant turn **with no preceding user turn** — expansion is a button, not a question, but it shares the thread because that thread is the audit trail for what the Director wrote. It is therefore visible in the Treatment workspace alongside chat replies, and it becomes part of the context a later chat turn sees.

## Assistant ProducerBot

`POST /api/projects/{id}/assistant/fill` is the third call, beside chat and expansion. It takes a `message` and a **required, non-empty `shot_ids`**, and it is the Director's own language model given one tool.

**The selection is the consent.** There is no `apply_*` flag here. `shot_ids` decides both what the model is shown and what it may write to, and a tool call naming any other shot — including a real, unlocked, perfectly writable one elsewhere in the plan — is refused and reported. Chat and expansion answer "you may write" with a boolean; this answers "you may write **here**". That is a stronger consent, not a weaker one, because it cannot be left ticked.

**One tool, and the taxonomy is its contract.** `fill_shots` takes entries whose `mode` is `ShotMode`, `role` is `AssetRole` and `singing` is `SingingState`, with the wire schema **generated from the model class** rather than transcribed — so the enums the model is allowed to say are the enums `models.py` declares, and they cannot drift. A mode the taxonomy has never had is a validation error at the edge, reported to the Director with the raw arguments beside it. Entries are validated **one at a time**: one bad mode does not discard the twenty-nine good shots in the same call. An absent field leaves that field alone; `citations` replaces the whole list.

The spec asked for "a set of tools" and the honest answer turned out to be **one**. Mode, prompt, citations and singing are four halves of a single act: split into separate calls, a model that chose `first_middle_last` and then failed the citation call would leave a shot declared as something its assets cannot satisfy.

**Every tool call meets the refusals a click meets.** `shot_write_refusal` is shared with expansion — same precedence, locks then render provenance, and the **same wordings verbatim** rather than reworded. The consequence already recorded for expansion therefore applies here too: **marking a shot `ready` takes it out of the assistant's reach.** The prompt gate is `batch.prompt_rejection` rather than `expansion_rejection`, deliberately: the former also catches the `"New shot"` placeholder, which a local model echoing back `current_prompt` will produce. Mode fit comes from `mode_specification_problems` and is reported as a flag, never a refusal, because planning a mode before its assets exist is the point. Unknown asset ids are caught by `dangling_citations`, and only ids *this answer* introduced count against it.

**No path spends GPU time**, asserted over every outcome: no prompts, no uploads, no jobs, no status change, no approval, no asset and no Song. The Director's own description places image generation *after* the assistant's work, as their next act.

**Nothing infers `singing`.** It is applied only when the tool call carried it, and then named out loud in the applied notice.

**All-or-nothing per shot, and every selected shot is named** — applied, locked, rendered, unknown-asset, prompt-refused, out-of-scope, omitted, answered-empty, duplicated or missing. Nothing is persisted until every shot has been judged: a single terminal `store.save` is what prevents a half-applied manifest, not the staging that precedes it. That distinction was found by a mutation and the code's own comment had overclaimed it.

Status codes: **422** for an empty or unwritable selection, refused before any model call; **503** unconfigured; **502** an unusable reply. Refused tool arguments are kept in the notice's `raw`, which `DIRECTOR_CONTEXT_EXCLUDE` strips, for the same reason the expansion route does it — so the model cannot read its own rejected output back.

The payload is `timeline.assistant_input`: selection-scoped, carrying the asset library and the mode table, and carrying **no production state**. One round trip, `tool_choice: "auto"`, no `response_format`; the per-shot report is assembled by the route, not by the model.

**The system prompt lives in `src/music_video_producer/assistant_prompt.py`, and it is meant to be edited.** Its own module, no interpolation, so rewording it touches no transport, no route and no behavioural test. `PROMPT_CRAFT` is split out as the half most likely to change between live runs. Two absences are deliberate and recorded in its docstring: there is **no anti-transcription clause** — literalism is the likely failure but the project's rule is to watch it on real output rather than pre-empt it, and the fix has a named home — and no worked example.

## H3 prompt expansion — pass two

`POST /api/projects/{id}/shots/{shot_id}/expand-prompt` takes **no body** and turns one Shot's intent into an H3-format prompt. It is the second of two passes, and the two want **opposite shapes**:

| | Pass one — `director/expand` | Pass two — `expand-prompt` |
|---|---|---|
| Call shape | One call, whole plan | One call **per Shot** |
| Why | Flow and cross-shot variance need the Shots seen together | One H3 prompt is long; thirty will not fit one context, and quality degrades well before the limit |
| Output | A short intent per Shot, into `Shot.prompt` | The full three-field structure, into `Shot.h3_prompt` |

Reading "this cannot be done in one call" as a criticism of pass one is the obvious mistake and the wrong one. Pass one is correct as it is.

**Why this exists at all.** H3 does not want a sentence. MiniMax publishes a 20-page format guide, and a rewriting model — `H3-Context-IR` — was supposed to turn an idea into that format. **It was never open-sourced.** This route is its replacement, which is a much narrower job than "write a good prompt" and is why the specialist is separate from ProducerBot's persona.

**Two fields, never one.** `Shot.prompt` keeps the human-readable intent; `Shot.h3_prompt` holds the expansion. Overwriting the intent would destroy what pass one wrote and leave nothing to re-expand from — and the first expansion will not be the good one. `h3_prompt` is **withheld from the Director's context**, the first field ever withheld from a Shot: a thirty-Shot plan of expansions would add many thousands of tokens to every chat turn, and rich context is this project's recorded cause of Director degradation. Withholding it is not a removal, because it was never in the dump.

**A malformed answer is never stored.** `h3_prompt.check` runs before the write; a prompt that fails comes back with its problems and the Shot is untouched. Storing it would put a broken prompt in the manifest that the *next render* submits, so the failure would surface as a bad take rather than a message. The refused text is returned so it can be read and judged — the argument `MessageNotice.raw` already makes.

**What the checker can and cannot decide** is the important distinction. It checks the mechanical rules: field order, `[Shot 1]` carrying no timestamp, shots numbered in order, cut times strictly increasing and inside the clip, `<d>` balanced and language-tagged, sentence bounds on the two sound fields, no speaker id in `retention_analysis`. It **cannot** check that every cut introduces new information, that only vocalizing characters carry ids, or that amplitude is given only where meaningful. Those live in `h3_expansion_prompt.py` because nothing else will carry them, and a clean check means well-*formed*, not well-*written*.

**Refusals** are the shared ones — `shot_write_refusal` then `prompt_is_missing` — and the order matters: a locked Shot hears it is locked rather than being told to write an intent it would then be refused for. The snapshot is re-read after the await and the refusal re-checked, because a Shot can be locked or rendered while the model is thinking.

**The payload** is `timeline.shot_expansion_input`: the Shot's own facts, the **neighbours' intents**, the treatment and style bible, and the song. Not the neighbours' expansions — two long-form prompts per call is the bloat that makes one-shot-for-all impossible. Neighbour intents *are* carried here where pass one withholds them, because on this pass they are real rather than placeholders, and a cut that lands well needs to know what it is cutting from.

**One claim it refuses to make.** The Director asked for "the song's words for this window", and the payload does not answer it: the whole sheet goes, labelled as whole.

*The reason was corrected 2026-08-26; the behaviour was not.* This paragraph used to justify the refusal with *"that cannot be built: nothing in this project aligns lyrics to time — `song_section` is an empty branch for exactly that reason"*. Lyrics **are** aligned to time now: `align-lyrics` stores `Song.lyric_words` as Whisper word timestamps, and `Song.vocal_spans` merged from them. So per-window words are buildable, and the payload still does not send them **by decision rather than by inability** — twice-measured, 2026-08-19: given words, the model plants them into the wrong windows, and words in the prompt fight the audio reference that actually drives the mouth. `section_lyrics` remains for planning surfaces; the expansion never sees words. That is a stronger reason than the one this paragraph used to give, and it is the one to preserve if the sentence is ever rewritten again. The whole sheet goes as `lyrics` with `song_fraction` beside it as the honest position signal, and the specialist's prompt tells the model the sheet is unaligned and that `song_fraction` is a hint about section and mood, never a claim about which line is sung here. Aligning lyrics is a real unbuilt feature, and it is the same empty slot FR-26 left.

**Scoped to the reference render.** `reference_prompt` submits the expansion when a Shot has one and the exact pre-change string when it does not — that equality is the safety argument for the whole feature. When an expansion is used the "Reference map:" preamble is **dropped**, because an H3 prompt must open with its instruction line or its first field and prose in front of that breaks the format; the tags are not lost, since the specialist is handed them and writes them into the description. The text-only Director path is untouched: it feeds `shot.prompt` into a structured timeline *segment*, and a three-field document there is an unevidenced shape.

**Automatic retries, ruled by the Director.** Every expansion door makes up to **four calls per Shot** — the first attempt plus three retries — stopping at the first well-formed answer, through one shared loop (`attempt_expansion`) so the sweep, the ProducerBot tool and the single-shot route cannot drift apart. A checker-rejected answer retries as a *corrective follow-up turn*: the failed text is replayed as the assistant's own message and the checker's problem sentences are sent back as the correction, so the model has a target rather than a fresh roll. A budget exhaustion (`DirectorBudgetExhausted`) retries clean. Any other model error fails immediately, and unavailability never retries — it is a configuration fact that would be identical on every attempt. The attempt count is reported everywhere a result is (`attempts` on the route, "took N tries" in sweep notices), because a shot that needed three tries is diagnostic signal about the model, not noise. Worst-case arithmetic worth knowing: a 30-shot sweep's ceiling is now 120 model calls.

**The thinking flag does not reliably survive a model reload.** Measured 2026-08-18: after LM Studio reloaded the model, it resumed spending its whole budget on reasoning *despite* `chat_template_kwargs: {"enable_thinking": false}` — 7 of 10 calls lost to budget exhaustion in one batch, probe-confirmed as host drift rather than anything in this codebase. This is precisely the failure class the retries absorb, but a sweep that suddenly reports many "took N tries" lines after you reloaded a model is telling you about the host.

**Model behaviour worth knowing before debugging a prompt.** Measured on the Director's own machine, 2026-08-18: a reasoning model spent **899 of 900** tokens thinking and returned empty content, and all 6000 of a 6000-token budget the same way. `/no_think` in the prompt did not suppress it; `chat_template_kwargs: {"enable_thinking": false}` did, though not reliably — the same flag gave 467 reasoning tokens on a short system prompt and 1494 on a longer one. So an empty completion beside a full `reasoning_content` is reported as a **budget** problem naming the number, not as an invalid response: calling it invalid would send a reader to rewrite a prompt that was fine. `chat_template_kwargs` is an LM Studio / vLLM extension and the error names it, because a stricter provider will 400 and the fix is to drop it.

**Three doors, one engine.** `expand_shots` runs the calls and **writes nothing**; `apply_expansions` commits in a single pass afterwards, re-checking every refusal against the project as it is *then*; `expansion_sweep_notices` renders the report. They are three functions rather than one so that "nothing is persisted until every Shot has been judged" is a testable claim rather than a described one.

The doors are the single-Shot route above, `POST /api/projects/{id}/shots/expand-prompts` for the whole plan, and `expand_prompts` — ProducerBot's second tool. The sweep takes **no body** and sweeps every Shot including ones nothing can be written to, because a Shot skipped in silence is indistinguishable from one the sweep forgot. Each is named in the report: `applied`, `malformed`, `locked`, `rendered`, `no_intent`, `missing`, `failed`.

A `DirectorError` on one Shot is *that Shot's* outcome and the sweep continues; `DirectorUnavailable` is a 503 for the whole sweep, because it is a configuration fact that would be identical for every Shot. Worth knowing in practice: the 4000-token budget still exhausts on reasoning roughly one call in six on this machine's model, which in a sweep is one `failed` line and the rest carrying on.

The tool is typed to **nothing but the Shot id** — an expansion has no vocabulary to get wrong, unlike `fill_shots`. It runs *after* the fills, so a Shot filled in and expanded in the same turn expands from the intent that turn just wrote. It may not name a Shot outside the turn's selection. The assistant is told only `expanded: bool` and never the expansion itself: the boolean is what makes the tool usable, and the text is what would degrade the model.

**A known cost, not a defect to discover later.** A sweep holds the read-to-save window open for the length of N model calls. The browser closes that window with `shotWriteInFlight` — and the 2 s render poll honours the same flag, skipping its tick rather than interleaving a reconciliation patch mid-sweep. A *second* client editing the same project during a sweep would still lose its edit. Single-Director use is unaffected.

## Project context

The director receives the current song metadata — including the song's `lyrics` and `caption` when it has them — creative documents, assets, shots, and prior messages.

An **imported** song can now carry both. Until this existed, `upload_song` took a title and a duration and nothing else, so a finished track's Treatment and Style Bible were written for a song whose words and sonic character the model had never seen, from a filename.

**Shot expansion sees both too, as of 2026-08-18.** It did not at first: the import work reached the chat route only, and expanded shot prompts were written without the words for two commits — which the documentation here wrongly implied otherwise until review caught it. The Director ratified the change rather than the docs being quietly narrowed to match the code.

The two fields ride inside `expansion_input`'s `song` block and are **omitted rather than emptied** when the song does not carry them, matching how `song_fraction` and `section` already behave, so a song without lyrics produces a byte-identical payload to the one that shipped before. `EXPANSION_SYSTEM_PROMPT` names both — a field the payload carries but the prompt never mentions is a field the model may ignore, which would make the change look done while doing nothing.

The size objection that deferred this does not apply to expansion, and the distinction is the point: the recorded root cause of Director degradation is context *accumulating* across chat turns, and expansion is a single stateless whole-plan call. A maximum lyric sheet costs its ~3,050 tokens once per expansion, not once per turn. That is why this was accepted on the same day the chat thread's unbounded growth was deliberately left alone. The two fields already existed on `Song` and were already in this dump; the import simply had no way to fill them. Nothing about how the Director is *prompted* changed: this made existing context available, it did not tune how that context is used. The lyrics arrive unparsed — see `docs/DATA-MODEL.md` — so the model sees the sheet as written and nothing in the application claims to know where a chorus falls. Render jobs and internal message IDs/timestamps are omitted to reduce irrelevant context, and so are the recovery slots (`treatment_previous`, `style_bible_previous`) and every message's `notices` list.

The `notices` omission is a correctness rule, not an economy: a notice's `raw` field holds the degraded output a refusal is *about*, and this dump is what the next call is handed, so leaving it in would make the guard that catches degraded output the thing supplying it. The whole list is dropped rather than the `raw` field within it, because each notice's sentence already appears in `content` — keeping the structured copy would echo a second copy of every notice into the prompt — and because a nested exclusion path silently stops covering a field that is later renamed or added beside it.

## Editing model

When the current UI sends a Director request it explicitly applies the returned shots as a new editable shot plan. Nothing renders. The user can then drag, resize, split, rewrite, attach references, and compile each shot.

A later change-set review will allow accepting treatment, bible, and shot changes independently and protect locked fields.
