from __future__ import annotations

import json
from dataclasses import dataclass
from itertools import pairwise

from .models import Shot


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
    if window_duration < 4 or window_duration > 15:
        warnings.append("MiniMax H3 is trained primarily for 4–15 second shot windows.")
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
