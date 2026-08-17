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

## Project context

The director receives the current song metadata, creative documents, assets, shots, and prior messages. Render jobs and internal message IDs/timestamps are omitted to reduce irrelevant context.

## Editing model

When the current UI sends a Director request it explicitly applies the returned shots as a new editable shot plan. Nothing renders. The user can then drag, resize, split, rewrite, attach references, and compile each shot.

A later change-set review will allow accepting treatment, bible, and shot changes independently and protect locked fields.
