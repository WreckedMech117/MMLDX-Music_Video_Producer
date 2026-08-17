from __future__ import annotations

import json
import math
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from .models import Project, Shot

#: MiniMax H3 is trained primarily for shot windows in this range, in seconds.
H3_MIN_SHOT_SECONDS = 4.0
H3_MAX_SHOT_SECONDS = 15.0


class TimelineError(ValueError):
    pass


@dataclass(slots=True)
class DirectorTimeline:
    timeline_data: str
    requested_frames: int
    aligned_frames: int
    warnings: list[str]


def align_h3_frames(frame_count: int) -> int:
    """Round upward to MiniMax H3's 17k+5 temporal grid."""
    if frame_count <= 5:
        return 5
    quotient, remainder = divmod(frame_count - 5, 17)
    return frame_count if remainder == 0 else (quotient + 1) * 17 + 5


def build_director_timeline(
    shots: list[Shot],
    *,
    window_start: float,
    window_duration: float,
    fps: int = 24,
) -> DirectorTimeline:
    if window_start < 0 or window_duration <= 0 or fps <= 0:
        raise TimelineError("Timeline window and fps must be positive")
    # `inf` and `nan` clear the positivity check above and then raise `OverflowError` or
    # `ValueError` inside `round()`, which is not `TimelineError`, so the route's refusal
    # translation misses them and a client sees a 500 for a window it was allowed to send.
    if not math.isfinite(window_start) or not math.isfinite(window_duration):
        raise TimelineError("Timeline window must be a finite number of seconds")
    ordered = sorted(shots, key=lambda shot: shot.start)
    for previous, current in pairwise(ordered):
        if current.start < previous.end - 1e-6:
            raise TimelineError(f"Shots overlap: {previous.id} and {current.id}")
    window_end = window_start + window_duration
    visible = [shot for shot in ordered if shot.start < window_end and shot.end > window_start]
    segments = []
    for shot in visible:
        relative_start = max(shot.start, window_start) - window_start
        relative_end = min(shot.end, window_end) - window_start
        segments.append(
            {
                "id": shot.id,
                "start": round(relative_start * fps),
                "length": round((relative_end - relative_start) * fps),
                "prompt": shot.prompt,
            }
        )
    warnings: list[str] = []
    if window_duration < H3_MIN_SHOT_SECONDS or window_duration > H3_MAX_SHOT_SECONDS:
        warnings.append(
            f"MiniMax H3 is trained primarily for {H3_MIN_SHOT_SECONDS:g}–"
            f"{H3_MAX_SHOT_SECONDS:g} second shot windows."
        )
    requested = round(window_duration * fps)
    payload = {
        "version": 1,
        "fps": fps,
        "duration": window_duration,
        "segments": segments,
    }
    return DirectorTimeline(
        timeline_data=json.dumps(payload, separators=(",", ":")),
        requested_frames=requested,
        aligned_frames=align_h3_frames(requested),
        warnings=warnings,
    )


def song_section(project: Project, shot: Shot) -> str:
    """The song section a Shot sits in, or "" because nothing in this project produces one.

    FR-26 asks for section boundaries *when analysis exists*. None does: there is no BPM or
    section field on `Song`, `Project` or any other model, the analyse-structure button is a
    disabled stub, `#bpm-value` and `#sections-value` are hardcoded "Not analyzed", and
    `#section-track` is never populated. This is therefore one explicit empty branch with a
    named home for a future analyser, not a fabricated boundary list — and `expansion_input`
    omits the key rather than sending "" or "unknown", because an absent value must never
    reach the model as a confident one.
    """
    return ""


def ordered_shots(project: Project) -> list[Shot]:
    """The project's Shots in song order — the one ordering the expansion path uses.

    Exported because two places need to agree about it: `expansion_input` numbers the model's
    entries by this order, and the route labels its notices by the same numbers. Ordering the
    notices by manifest position instead would describe a different Shot than the `index` the
    model answered about, for any plan whose manifest order is not its time order.

    The sort is stable, so Shots sharing a start keep their manifest order rather than being
    silently reshuffled between two calls over the same project.
    """
    return sorted(project.shots, key=lambda shot: shot.start)


def _neighbour_framing(shot: Shot | None) -> dict[str, Any] | None:
    """Where a neighbouring Shot sits, as adjacency plus its window.

    Deliberately **not** the neighbour's prompt. On a first expansion — the primary case —
    every prompt is `""` or the `"New shot"` placeholder, so carrying it would convey nothing
    exactly when the variance mechanism matters most, while shipping every prompt three times
    (as `current_prompt`, as one neighbour's `next` and as another's `previous`) against a
    payload whose whole justification is that it is trimmed. The full entry for a neighbour is
    in `shots`, reachable by this id or by `index ± 1`; this states the adjacency and the
    timing, which are real on the first run.
    """
    if shot is None:
        return None
    return {"shot_id": shot.id, "start": round(shot.start, 3), "end": round(shot.end, 3)}


def expansion_input(project: Project) -> dict[str, Any]:
    """The purpose-built, trimmed input for one whole-plan shot expansion. Pure and I/O-free.

    Whole-plan rather than per-Shot because per-Shot calls cannot see each other, and the
    variance FR-26 asks for — holding identity, wardrobe, palette and lens fixed while action,
    framing and energy move — is a property of the plan, not of any one Shot.

    Deliberately *not* `project.model_dump()`. The chat route's dump ships every Shot's
    `status`, `prompt_id`, `latest_output`, `latest_review` and `approved_output`, and the
    recorded root cause of Director degradation is rich context: JSON in context begets JSON,
    which is the failure `document_rejection` exists to catch. Nothing here carries a take, a
    render id or a review.

    Shots are ordered by `ordered_shots`, so `index` and the neighbours are the Shot's position
    in the song rather than in the manifest, and the route's notices are numbered by the same
    call. `song_fraction` is **absent** when there is no Song or its duration is unknown — a
    fabricated 0.0 would tell the model every shot opens the song — and `section` is absent for
    the reason `song_section` documents.

    Every key here is described to the model in `EXPANSION_SYSTEM_PROMPT`. A payload whose
    semantics the model has to infer is a payload whose variance mechanism is hoped for rather
    than requested, so the two are written to be changed together.
    """
    ordered = ordered_shots(project)
    song_duration = project.song.duration if project.song else 0.0
    shots: list[dict[str, Any]] = []
    for index, shot in enumerate(ordered):
        entry: dict[str, Any] = {
            "shot_id": shot.id,
            "index": index,
            "start": round(shot.start, 3),
            "end": round(shot.end, 3),
            "duration": round(shot.duration, 3),
            # A locked Shot stays in the plan because the model needs the whole through-line to
            # write around it, and is flagged so it does not spend a slot on one that is never
            # applied. The chat context keeps the document locks for the same reason.
            "locked": shot.locked,
            "current_prompt": shot.prompt,
            # Descriptive, never gating: expansion writes prompts only, so no window changes
            # and nothing here can fail. It is direction — a 25 s shot wants different prose
            # from a 5 s one — reported as a flag over the project's own Shots.
            "outside_h3_window": not (
                H3_MIN_SHOT_SECONDS <= shot.duration <= H3_MAX_SHOT_SECONDS
            ),
            "neighbours": {
                key: framing
                for key, framing in (
                    ("previous", _neighbour_framing(ordered[index - 1] if index else None)),
                    (
                        "next",
                        _neighbour_framing(
                            ordered[index + 1] if index + 1 < len(ordered) else None
                        ),
                    ),
                )
                if framing is not None
            },
        }
        if song_duration > 0:
            # Clamped, because this is guidance rather than geometry. A Shot can legitimately
            # sit past the end of a shorter song — nothing retimes Shots when the Song changes,
            # which is exactly what SONG_REPLACEMENT_CONSEQUENCE promises — and telling the
            # model a Shot sits at 3.5 of the way through the song is not a fact it can use.
            # The absolute `start` and `end` above stay unclamped and carry the real timing.
            entry["song_fraction"] = round(min(1.0, max(0.0, shot.start / song_duration)), 4)
        section = song_section(project, shot)
        if section:
            entry["section"] = section
        shots.append(entry)
    payload: dict[str, Any] = {
        "creative_brief": project.creative_brief,
        "treatment": project.treatment,
        "style_bible": project.style_bible,
        "h3_shot_window": {"min": H3_MIN_SHOT_SECONDS, "max": H3_MAX_SHOT_SECONDS},
        "shots": shots,
    }
    if project.song is not None:
        # Title and length only. The lyric sheet and the generation caption are the Song's
        # bulkiest fields and neither says where a Shot sits, which is what this input is for.
        payload["song"] = {"title": project.song.title, "duration": project.song.duration}
    return payload
