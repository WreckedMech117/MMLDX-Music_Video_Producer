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

Two slots are **absent rather than fabricated**: `song_fraction` when there is no song or its duration is unknown, and `section` always — nothing in this project analyses song structure, so `timeline.song_section` is a named empty branch rather than an invented boundary list.

Expansion writes prompts only. It never retimes a window, never queues a render, and never rewrites a shot that is either **locked** or **carries render provenance** — anything with a submitted prompt id, a take on disk, an approval, or a status past `draft`. For those, the prompt is no longer an intention but the record of what produced a specific piece of media, so rewriting it in place would leave the take and its prompt quietly disagreeing. Note the consequence: marking a shot `ready` takes it out of expansion's reach.

A returned id matching no shot, a shot the model omitted, a prompt that parses as JSON, and the model answering for the same shot twice are each reported in the reply and not applied — on a duplicate, the first answer wins. The refused text itself is kept **beside** the notice, in the notice's `raw` field — not inside the message's `content`. The thread becomes context for the next chat turn, so persisting degraded JSON in `content` would feed the exact failure `document_rejection` exists to catch; carrying it in a field that `DIRECTOR_CONTEXT_EXCLUDE` strips lets the Director inspect what was refused without the model ever seeing it again. This replaced an earlier design that dropped the refused text entirely, which kept the invariant but left a refusal you could not examine.

The reply is appended to the project's chat thread as an assistant turn **with no preceding user turn** — expansion is a button, not a question, but it shares the thread because that thread is the audit trail for what the Director wrote. It is therefore visible in the Treatment workspace alongside chat replies, and it becomes part of the context a later chat turn sees.

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
