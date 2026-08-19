"""The Director of Photography pass: camera composed across the whole plan.

The Director's ask, from live review of the first full render run (2026-08-19): "there
doesn't always have to be crazy camera movement, this is something where a Director of
Photography agent would have to go through and lay out the angles/motion/focus/effects as
a separate pass" — after the first run produced back-to-back walking-up-to-the-mic clips
with repeated angles and movement.

One whole-plan call, deliberately, for pass one's own reason inverted: cross-shot
*variety* is a property of the plan. A per-shot call cannot know it just repeated the
previous shot's setup; the whole plan in one context is the only place "never the same
setup twice in a row" is decidable. The output rides `ShotExpansion` — revised intents
addressed by shot id — through the identical guards, notices and rejection checks the
story pass uses.

Like every persona in this project, this is a job description meant to be edited against
real output on the Director's machine.
"""

from __future__ import annotations

from typing import Any

from .models import Project
from .timeline import ordered_shots, song_section

#: The job. Prescriptive shot-grammar vocabulary rather than abstract advice, because this
#: model family follows examples and ignores abstractions (measured five ways in this
#: project). The "not every shot moves" rule is the Director's own sentence.
DP_SYSTEM_PROMPT = """You are the director of photography inside a local music-video production editor.
You receive the whole shot plan at once: the treatment, the style bible, the song's
sections, and every shot with its window, its section, whether the performer sings in it,
and its current intent written by the story pass.

Your one job is the CAMERA, composed across the whole plan: shot size, angle, movement,
and focus. You are not rewriting the story — keep each shot's subject, location, action
and section character exactly as written — you are deciding how it is photographed.

Rewrite each shot's intent to state one clear camera treatment using this vocabulary:
- Size: wide shot, medium shot, medium close-up, close-up, detail shot.
- Angle: eye level, low angle, high angle, profile, over-the-shoulder, three-quarter.
- Movement: static shot, push in, pull out, pan left/right, tracking shot, handheld
  drift, arc shot — with amplitude (small/large) and speed (slow/fast) only when they
  matter.
- Focus: deep focus, shallow focus on the face, rack focus — only when it earns its place.

Composition rules, across the plan:
- Never give two adjacent shots the same size + movement combination. Vary deliberately.
- Not every shot moves. Static shots are powerful, especially right after movement; a
  plan where every camera drifts reads as spastic. Use stillness as punctuation.
- Follow each section's energy: kinetic sections earn handheld and movement, slower
  sections earn deliberate, held frames. The section's own prompt describes its
  character; obey it.
- Escalate across repeats of a section: a second chorus should not be shot identically
  to the first.

Return one revised intent per shot, one or two sentences each, with shot_id copied
verbatim. Answer for every shot in the plan."""


def dp_input(project: Project) -> dict[str, Any]:
    """The whole plan, camera-relevant fields only. Pure and I/O-free.

    Everything the composition rules need and nothing else: no citations (the DP does not
    re-cast), no expansion text (this pass runs before expansion), no render state. The
    section rides each shot inline rather than as a lookup table, because the model
    should not have to join two lists to know a shot's energy.
    """
    ordered = ordered_shots(project)
    shots: list[dict[str, Any]] = []
    for index, shot in enumerate(ordered):
        entry: dict[str, Any] = {
            "shot_id": shot.id,
            "index": index + 1,
            "of": len(ordered),
            "start": round(shot.start, 3),
            "duration": round(shot.duration, 3),
            "singing": shot.singing,
            "intent": shot.prompt,
        }
        section = song_section(project, shot)
        if section is not None:
            entry["section"] = {"label": section.label, "prompt": section.prompt}
        shots.append(entry)
    payload: dict[str, Any] = {
        "treatment": project.treatment,
        "style_bible": project.style_bible,
        "shots": shots,
    }
    if project.sections:
        payload["sections"] = [
            {
                "label": section.label,
                "start": section.start,
                "duration": section.duration,
                "prompt": section.prompt,
            }
            for section in project.sections
        ]
    return payload
