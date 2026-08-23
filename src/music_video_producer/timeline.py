from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from .models import (
    ASSET_ROLE_LABELS,
    CHARACTER_SLOT_LIMIT,
    SHOT_MODE_SPECS,
    Asset,
    Project,
    Shot,
    numbered_references,
    resolve_shot_mode,
    shot_label,
    song_audio_tag,
    speaker_notation,
)

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


#: The over-render margin, ruled by the Director 2026-08-19: "do not generate a clip to
#: exact or lesser length than the time it was given, going over is acceptible as it can
#: give a bit of editable room at either end … Doesnt have to be more than a half second."
#: Before this, margin was a grid accident — a 3.75 s shot rendered exactly 90 frames and
#: left nothing to fine-tune with.
OVER_RENDER_SECONDS = 0.5

#: How much of the margin sits *before* the shot's window when the song allows it. A
#: quarter second each way is the "either end" of the ruling; the grid's own snap-up
#: usually adds more tail on top.
OVER_RENDER_LEAD_SECONDS = 0.25

#: The one frame rate every adapter in this application renders at.
H3_FPS = 24

#: The shortest take this application ever asks H3 for: `H3_MIN_SHOT_SECONDS` on the grid.
#: 4.0 s is 96 frames, which snaps up to **107** — 4.4583 s. Every render, however short its
#: window, is at least this long.
H3_MIN_RENDER_FRAMES = align_h3_frames(round(H3_MIN_SHOT_SECONDS * H3_FPS))


def margin_frames(duration: float) -> int:
    """What the over-render *margin alone* asks for: ``duration + 0.5`` on the 17k+5 grid.

    Separated from `over_render_frames` because two things now read it and they must read the
    same arithmetic: the frame count itself, floored below, and `over_render_centred` — which
    decides where the buffer sits by asking whether that floor fired at all. The grid's own
    ``frame_count <= 5`` clamp is what answers a zero or negative duration.
    """
    return align_h3_frames(round((duration + OVER_RENDER_SECONDS) * H3_FPS))


def over_render_centred(duration: float) -> bool:
    """Whether this window's take is *centred* on it rather than merely extended past it.

    True exactly when the H3 minimum floor fires — when the margin's own answer
    (`margin_frames`) is shorter than `H3_MIN_RENDER_FRAMES`, which happens below about
    3.271 s. That boundary, and not `H3_MIN_SHOT_SECONDS`, is the deliberate reading of the
    Director's "if a shot is below the minimum typical then the math would just center around
    that clip", and it is stated here because the two readings differ over a 0.73 s band:

    * Below ~3.271 s the floor invents buffer the window never asked for — up to 2.4 s of it —
      and hanging all of that off one end is what the centring ruling is *about*. Centred.
    * From ~3.271 s to 4 s the take is already the H3 minimum by the margin's own arithmetic,
      and the margin's rule for it is itself a Director ruling ("a bit of editable room at
      either end … Doesnt have to be more than a half second", 2026-08-19). Re-centring those
      would move a shipped, pinned lead — a 3.75 s shot's quarter second — for no gain, and
      would change the submitted audio window of every such shot. Left exactly as it was.

    So the rule is: **the buffer the floor invents is centred; the buffer the margin was
    already giving is not.** Nothing at or above ~3.271 s changes by a frame or a byte.
    """
    return margin_frames(duration) < H3_MIN_RENDER_FRAMES


def over_render_frames(duration: float) -> int:
    """The frames a shot of ``duration`` seconds is actually rendered for.

    ``duration + OVER_RENDER_SECONDS``, snapped up to the 17k+5 grid and then floored at
    `H3_MIN_RENDER_FRAMES` — so the take is always at least half a second longer than the
    window that will consume it, never exactly its length again, and never shorter than the
    length H3 is trained to render. Every consumer of the extra length (the Monitor, the trim
    nudge, assembly's offset) exists because this margin does.

    **The floor is the Director's ruling of 2026-08-20**, and it is the whole of what changed:

        "some may go long, some may be short … some videos even use micro-cuts for fast
        paced stuff … Shots still generated to the 4 second minimum and they just have a
        bigger invisible 'buffer' while still being lined up so the exposed part matches."

    A sub-4-second window is legitimate music-video editing, not an error, so the *rendered*
    length is decoupled from the *exposed* length: a 2.083 s window renders 107 frames
    (4.4583 s) and carries 2.375 s of invisible buffer that the trim discards. Where that
    buffer sits is `over_render_lead`'s half of the ruling — centred on the window, by the
    Director's second ruling the same day — and the exposed slice is the same seconds of the
    song either way.

    Only windows under about 3.271 s reach the floor: at 3.2708 s the margin already asks for
    91 frames, which snaps to 107 on its own. Every window at or above that renders the frame
    count it has always rendered, byte for byte — pinned in `tests/test_timeline.py`.
    """
    return max(H3_MIN_RENDER_FRAMES, margin_frames(duration))


def populate_windows(
    proposals: list[tuple[float, float]],
    song_duration: float,
    *,
    minimum: float = H3_MIN_SHOT_SECONDS,
    maximum: float = H3_MAX_SHOT_SECONDS,
) -> list[tuple[float, float]]:
    """Tile the whole song with shot windows shaped by the model's proposals.

    Populate Timeline's repair pass (spec-populate-timeline): a local model's layout is
    treated as *shape*, never as arithmetic — its relative durations survive, but the
    result must satisfy what assembly will later demand (contiguous from 0 to the song's
    end, no gaps, no overlaps) and what H3 renders reliably (windows in the 4–15 s range).

    The count is clamped to the feasible band first — at least ``ceil(song/maximum)``
    segments, at most ``floor(song/minimum)`` — then the proposal durations are scaled to
    the song and water-filled into the per-window clamps until the total is exact. A song
    shorter than one minimum window is a single whole-song shot. The output starts at
    exactly 0 and ends at exactly ``song_duration``, by construction.
    """
    if not math.isfinite(song_duration) or song_duration <= 0:
        raise TimelineError("A song must have a positive length to populate against")
    if song_duration <= minimum:
        return [(0.0, song_duration)]
    lower = max(1, math.ceil(song_duration / maximum))
    upper = max(1, math.floor(song_duration / minimum))
    count = len(proposals) or round(song_duration / ((minimum + maximum) / 2))
    count = max(lower, min(count, upper))
    weights = [max(duration, 0.5) for _, duration in proposals[:count]]
    weights += [song_duration / count] * (count - len(weights))
    scale = song_duration / sum(weights)
    durations = [weight * scale for weight in weights]
    # Water-fill: clamp every window, then push the residual into whichever windows still
    # have headroom. The count sits inside the feasible band, so this always converges.
    for _ in range(64):
        durations = [min(max(duration, minimum), maximum) for duration in durations]
        residual = song_duration - sum(durations)
        if abs(residual) < 1e-9:
            break
        adjustable = [
            index
            for index, duration in enumerate(durations)
            if (residual > 0 and duration < maximum) or (residual < 0 and duration > minimum)
        ]
        if not adjustable:
            break
        share = residual / len(adjustable)
        for index in adjustable:
            durations[index] += share
    windows: list[tuple[float, float]] = []
    cursor = 0.0
    for index, duration in enumerate(durations):
        end = song_duration if index == len(durations) - 1 else cursor + duration
        windows.append((round(cursor, 3), round(end - cursor, 3)))
        cursor = end
    # The rounding above must not reopen a gap at the very end — the last window absorbs
    # it — and must not overshoot the song either: a last window rounded 0.1 ms past the
    # end is a shot `song_audio_window` refuses whole (found live on the first-video run,
    # song 154.644898 s → last end rounded to 154.645). Floor the last duration to the
    # millisecond below the true remainder instead of rounding to the nearest.
    last_start, _ = windows[-1]
    remainder = song_duration - last_start
    # The +1e-6 absorbs binary-float noise just below a millisecond boundary; it can
    # overshoot the true remainder by at most a nanosecond, which nothing measures.
    windows[-1] = (last_start, math.floor(remainder * 1000 + 1e-6) / 1000)
    return windows


def repair_sections(
    proposals: list[tuple[str, float, float, str]], song_duration: float
) -> list[tuple[str, float, float, str]]:
    """Model-proposed sections made legal: sorted, clamped to the song, overlaps truncated.

    ``(label, start, duration, prompt)`` in, same shape out. A proposal is scaffolding —
    the Director will drag the boxes — so repair beats refusal: sort by start, clamp to
    ``[0, song]``, truncate each at the next one's start, drop what vanishes. Gaps are
    left alone; an unmarked stretch means unknown everywhere sections are read.
    """
    # Sub-second proposals drop *before* truncation, or a doomed sliver would truncate
    # its healthy neighbour on its way out.
    ordered = sorted(
        (p for p in proposals if p[2] >= 1.0 and p[1] < song_duration),
        key=lambda p: p[1],
    )
    repaired: list[tuple[str, float, float, str]] = []
    for index, (label, start, duration, prompt) in enumerate(ordered):
        start = max(0.0, start)
        end = min(start + duration, song_duration)
        if index + 1 < len(ordered):
            end = min(end, max(ordered[index + 1][1], start))
        if end - start >= 1.0:
            repaired.append((label, round(start, 3), round(end - start, 3), prompt))
    return repaired


def proposal_for_position(
    position: float, song_duration: float, proposal_count: int
) -> int:
    """Which proposal a tiled window draws its prompt from: the one whose *proportional*
    span of the song contains this position. Repairing the count must not orphan a window
    from the story — a segment in the song's second quarter takes the prompt the model
    wrote for the second quarter, whether the repair split, merged, or kept its windows.

    **The extent this divides is the caller's, and since 2026-08-22 it is usually a section
    rather than the song.** `app.paired_proposals` calls it twice over: once over the whole
    song for a layout with no section layer (the behaviour this function shipped with, byte
    for byte), and once per section over *ranks* — ``position`` a window's zero-based rank
    plus a half, ``song_duration`` how many windows that section holds — so the arithmetic
    that spreads proposals across an extent has one implementation whichever extent it is.
    """
    if proposal_count <= 0:
        raise TimelineError("No proposals to draw prompts from")
    if song_duration <= 0:
        return 0
    index = int(position / song_duration * proposal_count)
    return min(max(index, 0), proposal_count - 1)


#: How long an unmarked stretch of song has to be before a layout tiles it as its own span.
#:
#: Half a second, which is `lay_out_shots`' own literal moved here with `layout_spans`. Shorter
#: than this and the stretch is a rounding artefact of two section boxes the Director dragged
#: together rather than a piece of song with no section on it — tiling it would produce a
#: sub-frame window, and leaving it uncovered leaves a hole assembly already tolerates.
SPAN_MIN_SECONDS = 0.5


def layout_spans(
    sections: Sequence[Any], song_duration: float
) -> list[tuple[float, float]]:
    """The stretches of song a layout tiles **independently**, in order: ``(start, length)``.

    One span per marked section, plus every unmarked stretch before, between and after them
    that is longer than `SPAN_MIN_SECONDS`. Tiling each on its own is what keeps a shot from
    straddling a section boundary — the Director's ask, and the thing the lay-out instruction
    tells the model in words ("every shot sits inside one section and takes that section's
    character").

    **Extracted so the two steps that must agree about sections cannot disagree.** Lay-out
    tiles these spans; fill-in pairs proposals to windows inside them (`app.paired_proposals`).
    Until 2026-08-22 only lay-out knew about them, and fill-in mapped content to windows by
    global proportion — which undid the boundaries lay-out had just built, measured live on a
    31-window plan as four windows carrying prose written for a different section.

    An empty section layer answers ``[]`` rather than one whole-song span, and the emptiness is
    load-bearing: it is how a caller tells "no sections were marked" from "one section covers
    the song", and the two want different behaviour in both callers.
    """
    if not sections:
        return []
    spans: list[tuple[float, float]] = []
    cursor = 0.0
    for section in sections:
        if section.start - cursor > SPAN_MIN_SECONDS:
            spans.append((cursor, section.start - cursor))
        spans.append((section.start, section.duration))
        cursor = section.end
    if song_duration - cursor > SPAN_MIN_SECONDS:
        spans.append((cursor, song_duration - cursor))
    return spans


def section_edges(sections: Sequence[Any]) -> dict[float, str]:
    """Every second a marked section starts or ends at, mapped to the section's label.

    What `snap_window_plan` is handed so a cut sitting on one of these is left alone — see
    `SNAP_SECTION_BOUNDARY`. Both doors onto the snapper build it from the same function, so
    a boundary that is protected on a fresh layout is protected on a stored timeline.

    **A start wins a collision**, which is the ordinary case: a section that ends where the
    next begins is one boundary, and naming it by the section that *opens* there reads the way
    a Director reads a timeline. `song_section` breaks its own tie the same way.
    """
    edges: dict[float, str] = {}
    for section in sections:
        edges.setdefault(section.end, section.label)
    for section in sections:
        edges[section.start] = section.label
    return edges


def over_render_lead(
    *, start: float, duration: float, picture_seconds: float, song_duration: float
) -> float:
    """How far before the shot's window the conditioning audio (and thus the take) begins.

    The lead is what keeps an over-rendered singing take *sync-correct by construction*:
    frame ``round(lead·24)`` of the take is the shot's window start, and playing or
    assembling from that offset reproduces exactly the song seconds the model performed.
    It is recorded on the Shot at submission (``latest_take_lead``) because it cannot be
    derived later — a pre-margin take and a post-margin one are indistinguishable by
    arithmetic on their lengths.

    The shape of the rule, per the spec's matrix: ideally ``OVER_RENDER_LEAD_SECONDS``,
    never more than the margin itself, never before the song starts — and if the *tail*
    would run past the song's end, the lead grows to shift the whole window earlier
    rather than truncating the picture. A whole-song shot with no room either side gets
    lead 0 and renders with the mismatch, which is the pre-margin behaviour for that edge.

    **A short window centres its take on the window instead**, by the Director's ruling of
    2026-08-20: "if a shot is below the minimum typical then the math would just center around
    that clip, so it would grab an appropriate size chunk for the render spanning equally both
    directions." When `over_render_centred` is true the ideal lead is half the buffer rather
    than a quarter second, so the take's midpoint is the window's midpoint and the exposed cut
    sits in the middle of what H3 performed — the mouth arrives at the cut with real song
    either side of it instead of starting cold on the first frame.

    **There is one lead, not two, and they do not add.** `OVER_RENDER_LEAD_SECONDS` is the
    *ideal* for a margin-sized render; on a centred one the ideal is ``extra / 2`` and it
    replaces the quarter second rather than stacking on it. It is never smaller either: a
    centred window's buffer is at least 1.1875 s (the floor at the ~3.271 s boundary), so half
    of it is at least 0.594 s — always more sync context than the quarter second it stands in
    for. One number is computed, one number is recorded in ``latest_take_lead``, and one number
    is what assembly cuts at.

    The ideal is snapped to the 24 fps grid because that is how it is spent: assembly cuts
    with ``trim=start_frame=round(lead·24)`` and the Monitor seeks the same frame, so a lead
    of 1.1875 s (28.5 frames) is a lead the picture cannot honour, and the half frame comes
    off the exposed slice's alignment. Snapped, ``round(lead·24)`` is exact and the frame at
    that offset is the window's first second to the sample. Only the centred ideal is snapped;
    the quarter second is already six whole frames, and the overflow branch below must go on
    producing the unsnapped numbers it always has.

    **Clamping never moves the exposed slice**, which is the property the whole feature stands
    on. Every branch answers a lead ``L``, the audio window sent is ``[start - L, start - L +
    R)``, and the invariant that follows does not mention ``R`` or how ``L`` was chosen:

        take second ``t`` is song second ``start - L + t``

    — so take second ``L`` is song second ``start``, and the slice assembly cuts,
    ``[L + nudge, L + nudge + duration)``, is song seconds ``[start + nudge, start + nudge +
    duration)``. Three clamps can shorten ``L`` and none of them touch that:

    * **a shot at 0.0 s cannot centre** — ``min(…, start)`` answers 0, the take begins exactly
      at the window and the whole buffer is tail. There is no song before 0 s to grab, so the
      alternative would be conditioning on silence that does not exist;
    * **a shot at the song's end** hits the overflow branch, which grows the lead until the
      tail lands on the last second; a short window there can end up with its whole buffer
      ahead of it, which is the mirror of the 0 s case;
    * **``extra``** caps both, so the take can never begin before the window minus its own
      buffer.

    In each case the buffer moves and the exposure does not.
    """
    extra = max(0.0, picture_seconds - duration)
    ideal = OVER_RENDER_LEAD_SECONDS
    if over_render_centred(duration):
        ideal = round(extra / 2 * H3_FPS) / H3_FPS
    lead = min(ideal, extra, start)
    if song_duration > 0:
        overflow = (start - lead + picture_seconds) - song_duration
        if overflow > 0:
            lead = min(lead + overflow, extra, start)
    return lead


def over_render_window(
    *, start: float, lead: float, picture_seconds: float, song_duration: float
) -> tuple[float, float]:
    """The master song's own seconds **for a whole take**: ``(trim_start, trim_end)``.

    One expression of the invariant `over_render_lead` states, extracted so the two stages that
    need it cannot write it twice:

        take second ``t`` is song second ``start - lead + t``

    — so a take that is ``picture_seconds`` long was performed against the song from
    ``start - lead`` for ``picture_seconds``, and that is both the window `generate_h3`
    conditions it with and the window `restore_song_audio` must lay back over it. Those two were
    separate arithmetic until 2026-08-21, and the restore half was the one that was wrong: it
    windowed by the bare ``start``/``duration``, which is the *exposed slice* rather than the
    take, and on a centred micro-cut that is both the wrong length and the wrong offset (a
    2.083 s window renders 4.4583 s of picture beginning 1.2083 s before the window).

    ``lead`` is a value that was **recorded** — `Shot.latest_take_lead`, written at submission —
    and never one recomputed here, because a pre-margin take and a post-margin one are
    indistinguishable by arithmetic on their lengths. This function therefore takes it as a
    parameter rather than calling `over_render_lead`: the render's caller has just computed it,
    the restore's caller reads it off the Shot, and neither is allowed to guess it.

    The only clamp is the song's own end, and it is the render's: ``min(trim_end,
    song_duration)``. A whole-song shot has no room either side, so the file simply ends before
    the picture does and the take is rendered — and restored — with the mismatch rather than
    silently shortened. The clamp moves the *tail*, never ``trim_start``, so it cannot move the
    exposure. A ``song_duration`` of 0 means the length was never recorded and nothing can be
    compared against an unknown, so no clamp is applied; `song_audio_window`'s own refusal makes
    the same reading.

    Nothing is clamped at the front. ``start - lead`` below zero would mean a lead longer than
    the shot's own start, which `over_render_lead` cannot produce (``min(…, start)``); it means
    the recorded lead does not describe this shot, and it surfaces as `song_audio_window`'s
    refusal in the caller rather than as a silently repaired window here.
    """
    trim_start = start - lead
    trim_end = trim_start + picture_seconds
    if song_duration > 0:
        trim_end = min(trim_end, song_duration)
    return trim_start, trim_end


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


def song_section(project: Project, shot: Shot):
    """The `SongSection` whose window holds this Shot's midpoint, or ``None``.

    The slot this function held empty for two days is filled the way it predicted: not by
    an analyser, but by the Director's own marks (`Project.sections`, 2026-08-19). The
    midpoint decides membership because a shot straddling a boundary belongs to whichever
    section owns more of it; a later start wins a tie, matching the tiling grid's rule
    that a boundary belongs to the window it opens. Empty sections still mean unknown --
    callers omit rather than fabricate.
    """
    if not project.sections:
        return None
    midpoint = shot.start + shot.duration / 2
    best = None
    for section in project.sections:
        if section.start <= midpoint < section.end and (
            best is None or section.start > best.start
        ):
            best = section
    return best


#: Below this much measured voice in a window, a `singing` mark is physically impossible:
#: there is nothing in the reference for a mouth to sync to, and H3 told to sing over an
#: instrumental window invents its own words and lipsyncs to them (live, 2026-08-19 —
#: shot 02 sang invented lines over the intro, and all four outro shots were marked
#: singing over a vocal-free 20 seconds).
MIN_SINGING_VOCAL_SECONDS = 0.5


def shot_vocal_overlap(song, *, start: float, duration: float) -> float | None:
    """Seconds of measured voice inside one window, or ``None`` when nothing was measured.

    Reads `Song.vocal_spans` — Whisper word timestamps, merged — and follows its rule:
    an empty list is *unmeasured*, not silent, and answers ``None`` so callers stand
    aside rather than downgrade a mark on no evidence.
    """
    if song is None or not song.vocal_spans:
        return None
    end = start + duration
    return sum(
        max(0.0, min(end, span_end) - max(start, span_start))
        for span_start, span_end in song.vocal_spans
    )


#: The lyric sheet's block opener: `[Verse]`, `[Chorus 2]`, `[Pre-Chorus]`...
_SHEET_TAG = re.compile(r"^[ \t]*\[([^\]\r\n]+)\][ \t]*$", re.MULTILINE)


def _sheet_blocks(lyrics: str) -> list[tuple[str, str, tuple[int, ...]]]:
    """The sheet's blocks with the **sheet line numbers** each one owns.

    ``(tag, block text, line indices)``. The line numbers are `lyric_line_tags`' own — every
    line of the sheet counted, blanks and `[Tag]` headers included — so a block's words and a
    line's singer mark are addressed by the same number. Blank lines inside a block are left
    out of the tuple, because a blank line is not sung and nothing can be aligned to it.

    One segmentation, two readers: `lyric_blocks` projects the first two fields and
    `align_lyric_lines` needs the third. A second walk over the sheet's `[Tag]` positions is
    exactly the kind of duplicate rule this codebase keeps being bitten by — it would place a
    line in one block for timing and another for tagging the first time the two disagreed.
    """
    if not lyrics.strip():
        return []
    # `_SHEET_TAG` is anchored with `^[ \t]*`, so every match starts at a line start; that is
    # what makes this offset lookup exact rather than a search.
    parts = _LINE_SPLIT.split(lyrics)
    line_of: dict[int, int] = {}
    offset = 0
    for number, position in enumerate(range(0, len(parts), 2)):
        line_of[offset] = number
        offset += len(parts[position])
        if position + 1 < len(parts):
            offset += len(parts[position + 1])
    matches = list(_SHEET_TAG.finditer(lyrics))
    headers = [line_of[match.start()] for match in matches]
    line_count = (len(parts) + 1) // 2
    blocks: list[tuple[str, str, tuple[int, ...]]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(lyrics)
        text = lyrics[match.end():end].strip()
        if not text:
            continue
        stop = headers[index + 1] if index + 1 < len(matches) else line_count
        blocks.append(
            (
                match.group(1).strip(),
                text,
                tuple(
                    number
                    for number in range(headers[index] + 1, stop)
                    if parts[2 * number].strip()
                ),
            )
        )
    return blocks


def lyric_blocks(lyrics: str) -> list[tuple[str, str]]:
    """The sheet's own structure: ``(tag, block text)`` in order of appearance."""
    return [(tag, text) for tag, text, _lines in _sheet_blocks(lyrics)]


#: A per-line singer mark at the head of a lyric line: `(S1)`, `(S1, S2)`, `(s1,s2)`.
#:
#: The same grammar `h3_prompt._SPEAKER` already checks, anchored to the start of a line and
#: case-insensitive for the same reason that one is: a Director typing the mark by hand types
#: what is comfortable, and refusing `(s1)` because of its case would be refusing the Director's
#: own text on a technicality.
#: `MULTILINE` so the same one pattern serves both readers: `.match()` on a single line (where the
#: flag changes nothing, since `match` anchors at position 0 anyway) and `.sub()` over a whole
#: block in `_lyric_tokens`, which has to reach the head of every line inside it.
_LINE_SPEAKER = re.compile(
    r"^([ \t]*)\(([ \t]*S\d+(?:[ \t]*,[ \t]*S\d+)*[ \t]*)\)[ \t]*",
    re.IGNORECASE | re.MULTILINE,
)

#: A head that *looks* like a singer mark and is not one: `(S0)`, `(S9)` past the slot bound,
#: `(S1, S1)`, `(S1` left unclosed. Deliberately narrow — it requires `(`, an `S`, and a digit —
#: so an ordinary lyric line opening `(she said)` or a stage direction `(quietly)` is not dragged
#: into this at all. What it catches is a Director's own typo in a notation they were invited to
#: type, and the answer to one is to *say so*: silently ignoring it would leave a line the
#: Director believes is tagged carrying no tag, and silently rewriting it would edit their sheet.
_LINE_SPEAKER_SUSPECT = re.compile(r"^[ \t]*\([ \t]*S[ \t]*\d", re.IGNORECASE)

#: The sheet split into lines *and the separators between them*, so a rejoin is byte-exact.
#:
#: `str.splitlines()` is unusable here: it discards which separator each line ended with, so a
#: sheet pasted from Windows round-trips as `\n` and every byte after the first edit differs from
#: what the Director supplied. The lyric sheet is stored as supplied apart from edge whitespace —
#: interior blank lines and indentation reach the payload unchanged — and a tag edit that quietly
#: normalised line endings would be a far worse defect than the drift this design exists to
#: prevent.
_LINE_SPLIT = re.compile(r"(\r\n|\r|\n)")


@dataclass(frozen=True, slots=True)
class LyricLine:
    """One line of the sheet, and what its singer mark says.

    `index` counts **every** line including blanks and `[Tag]` headers, because it is an index
    into the sheet's own text and the writer uses it to touch exactly one line. A renderer that
    numbered only the taggable lines would hand the writer a number meaning a different line.

    `slots` are the `Asset.character_slot` numbers this line is tagged for — `()` for untagged,
    which is the state every line of every existing sheet is in.

    `text` is the line with its mark removed, which is what a dropdown row shows beside the
    select. `raw` is the line exactly as stored, mark and indentation included.

    `taggable` is false for blank lines and for `[Tag]` block headers: neither is sung, and a
    dropdown on the word `[Chorus]` would be offering to attribute a structural marker to a
    singer.

    `unreadable` is the honest third state. It is true when the head of the line matches the shape
    of a singer mark but not its grammar — a slot number past the bound, a repeat, an unclosed
    bracket. Such a line is **reported and left exactly as it is**: not dropped, not guessed at,
    and not rewritten. `slots` is `()` there, because nothing was understood, and the writer
    refuses to retag it until the Director fixes it by hand.
    """

    index: int
    raw: str
    text: str
    slots: tuple[int, ...]
    taggable: bool
    unreadable: bool


def _parse_line_slots(line: str) -> tuple[tuple[int, ...] | None, str]:
    """One line's mark as slot numbers and the text after it, or `(None, line)` when unreadable.

    `None` and `()` are different answers and the difference is the whole point: `()` is "this
    line carries no mark", `None` is "this line carries something that was meant to be a mark and
    could not be read". Only the second is worth telling the Director about.
    """
    match = _LINE_SPEAKER.match(line)
    if match is None:
        return (None, line) if _LINE_SPEAKER_SUSPECT.match(line) else ((), line)
    numbers = [int(value) for value in re.findall(r"\d+", match.group(2))]
    # Re-validated rather than trusted to the regex: the pattern proves the *shape*, and these two
    # are the facts that make a mark resolve to a cast. A slot past the bound names a character no
    # dropdown can offer and no asset may hold (`CHARACTER_SLOT_LIMIT`); a repeat names one singer
    # twice, which is not a duet line, it is a typo.
    if any(number < 1 or number > CHARACTER_SLOT_LIMIT for number in numbers):
        return None, line
    if len(set(numbers)) != len(numbers):
        return None, line
    return tuple(numbers), line[match.end():]


def lyric_line_tags(lyrics: str) -> list[LyricLine]:
    """Read the sheet's per-line singer marks **out of the sheet itself**.

    This is the whole storage decision, and the reason there is no second field to read. The
    Director asked for a per-line dropdown; the alternative implementation is a parallel map from
    line number to singer, and that map is wrong the instant a line is inserted, deleted or
    reordered — wrong *silently*, pointing every tag after the edit at the wrong words, which is
    exactly the class of defect this project keeps finding. The tag is stored where the line is,
    so an edit that moves a line moves its tag with it because they are the same characters. There
    is nothing to reconcile, nothing to invalidate, and no state in which the two disagree.

    The cost of the choice, stated: the marks are in the text the Director edits, so they can be
    typed, mistyped and deleted by hand. That is answered by `unreadable` rather than by taking the
    text away — see `LyricLine`.

    Reads and never writes. Every line of the sheet comes back, in order, blanks and `[Tag]`
    headers included, so a caller can address one by index.
    """
    parts = _LINE_SPLIT.split(lyrics)
    lines: list[LyricLine] = []
    for index, raw in enumerate(parts[::2]):
        header = bool(_SHEET_TAG.fullmatch(raw))
        slots, rest = _parse_line_slots(raw)
        lines.append(
            LyricLine(
                index=index,
                raw=raw,
                text=rest,
                slots=slots or (),
                taggable=bool(raw.strip()) and not header,
                unreadable=slots is None,
            )
        )
    return lines


class LyricTagError(ValueError):
    """The sheet could not be retagged at the line asked for, and nothing was written."""


def tag_lyric_line(lyrics: str, index: int, slots: Sequence[int]) -> str:
    """Set (or clear) one line's singer mark, and change **nothing else in the sheet**.

    The dropdown's write. `slots` empty removes the mark, which is what the "Untagged" entry
    means; anything else writes `speaker_notation(slots)` at the head of the line followed by a
    single space.

    Every other line comes back byte for byte, separators included — the rejoin is over the same
    `_LINE_SPLIT` pieces the read produced, and only `parts[2 * index]` is ever replaced. Nothing
    here re-wraps, re-indents, collapses blank lines or touches a `[Tag]` block: the sheet is the
    Director's own text and a tag edit is not a licence to reformat it. The one whitespace this
    does normalise is *inside the mark it is writing*, which is the mark's own spelling and not
    the Director's line.

    Refuses rather than guesses, and both refusals leave the sheet untouched:

    * a line that is blank, a `[Tag]` header, or out of range — there is no sung line there;
    * a line whose existing mark is unreadable. Overwriting it would silently repair a typo the
      Director cannot then see they made, and this codebase reports rather than repairs.
    """
    parts = _LINE_SPLIT.split(lyrics)
    position = 2 * index
    if index < 0 or position >= len(parts):
        raise LyricTagError(f"The lyric sheet has no line {index}.")
    line = parts[position]
    parsed, rest = _parse_line_slots(line)
    if parsed is None:
        raise LyricTagError(
            f"Line {index + 1} starts with something that looks like a singer mark but could not "
            f"be read: {line.strip()!r}. Fix the line in the lyric sheet and the dropdown will "
            "follow it."
        )
    if not line.strip() or _SHEET_TAG.fullmatch(line):
        raise LyricTagError(f"Line {index + 1} is not a sung line, so it carries no singer.")
    # Whatever this writes, `_parse_line_slots` must read back — otherwise a caller could store a
    # mark that this module's own reader then reports as unreadable, and the Director would be
    # told their sheet is broken by an edit they never made by hand. The two conditions are the
    # reader's own, checked before anything is written rather than discovered afterwards.
    if any(slot < 1 or slot > CHARACTER_SLOT_LIMIT for slot in slots):
        raise LyricTagError(
            f"A singer slot is a number from 1 to {CHARACTER_SLOT_LIMIT}, and {sorted(slots)} is "
            "not."
        )
    if len(set(slots)) != len(slots):
        raise LyricTagError(f"{sorted(slots)} names one singer twice, which is not a sung line.")
    # The line's own indentation is preserved, whichever side of the mark it was read from: a
    # matched mark carries it in group 1 (the pattern consumes it), and an unmarked line still
    # holds it at the head of `rest`. A tag edit must not out-dent a Director's indented line.
    marked = _LINE_SPEAKER.match(line)
    indent = marked.group(1) if marked else line[: len(line) - len(line.lstrip(" \t"))]
    body = rest.lstrip(" \t")
    notation = speaker_notation(slots)
    parts[position] = f"{indent}{notation} {body}" if notation else f"{indent}{body}"
    return "".join(parts)


def section_lyrics(project: Project, section) -> str:
    """The lyric block a section sings, paired **by order of appearance**.

    The sheet's tags carry structure but no timing; the Director's sections carry timing
    but no words. The pairing is positional within a label family: the Nth section whose
    label starts with a tag's word ("Verse 2" -> `[Verse]`, case-insensitive) takes the
    Nth such block. A section with no matching block -- an instrumental intro, a label
    the sheet never uses -- answers "", and the caller says *no words* rather than
    guessing. This is what ends the wrong-verse lipsync found on the first full render run:
    the expansion stops inferring a shot's words from a fraction and reads them off the
    section instead.
    """
    if section is None or not project.song or not project.song.lyrics:
        return ""
    blocks = lyric_blocks(project.song.lyrics)

    def family(name: str) -> str:
        return re.split(r"[\s\-_]", name.strip().lower())[0]

    wanted = family(section.label)
    ordinal = sum(
        1
        for other in sorted(project.sections, key=lambda item: item.start)
        if family(other.label) == wanted and other.start < section.start
    )
    matching = [text for tag, text in blocks if family(tag) == wanted]
    return matching[ordinal] if ordinal < len(matching) else ""


def section_looks_input(project: Project) -> dict[str, Any]:
    """What the section-look pass is shown: the treatment, the style bible, the boxes.

    Deliberately three keys and no more. `expansion_input` hands the model the whole plan
    because a prompt has to sit among its neighbours; this pass is answering "what does the
    Intro look like" out of prose that is already organised by section, and every extra key
    is another thing for it to write into a look that must not contain one — populate's
    measured leak was an internal asset label ending up in creative prose, and the cheapest
    guard against a name being echoed is a name never shown. No shots, no assets, no lyrics.

    Sections go out **in song order with their ids**, and both halves matter. The id is the
    address the answer comes back on (`ExpandedShot.shot_id`'s contract), and the order is
    what lets a treatment heading like "Verse 1 & Verse 2" be read against a list where the
    director's own numbering — "Verse", then "Verse 2" — is visible. The existing `prompt`
    rides along so a re-run can see what a section already carries.
    """
    ordered = sorted(project.sections, key=lambda section: section.start)
    return {
        "treatment": project.treatment,
        "style_bible": project.style_bible,
        "sections": [
            {
                "section_id": section.id,
                "label": section.label,
                "start": round(section.start, 3),
                "end": round(section.end, 3),
                "duration": round(section.duration, 3),
                "prompt": section.prompt,
            }
            for section in ordered
        ],
    }


def _lyric_tokens(text: str) -> list[str]:
    """Words as things two spellings of one sung line can agree on: lowercased, apostrophes
    dropped ("don't" == "dont"), and split on everything else — a hyphen splits, so the
    sheet's "spread-eagle" meets Whisper's "spread", "eagle" as two words, not one blob.

    Per-line singer marks are stripped first, and they have to be: `(S1)` lowercases to `(s1)`,
    out of which `[a-z]+` reads the bare token `"s"` — one phantom word per tagged line, inflating
    every block's token count and therefore its match threshold, against a transcript that
    contains no such word. A duet sheet would align measurably worse than the same sheet untagged,
    which would make tagging a song quietly degrade Analyze structure. The mark is markup, not a
    lyric, so it is removed before the words are counted.

    A no-op on a sheet with no marks: the pattern is anchored to the head of a line and matches
    nothing there, so every untagged sheet tokenises byte for byte as it always did.
    """
    return re.findall(r"[a-z]+", _LINE_SPEAKER.sub("", text).lower().replace("'", ""))


def _tokens_agree(sheet: str, heard: str) -> bool:
    """Whether a sheet word and a transcribed word are plausibly the same sung word.

    Exact, or one is a prefix of the other with at least four shared letters — which is
    what survives Whisper's habit of normalizing sung contractions ("runnin" -> "running",
    "screamin" -> "screaming") without letting "the" match "them"."""
    if sheet == heard:
        return True
    shorter, longer = (sheet, heard) if len(sheet) <= len(heard) else (heard, sheet)
    return len(shorter) >= 4 and longer.startswith(shorter)


def _heard_tokens(
    words: list[tuple[str, float, float]]
) -> list[tuple[str, float, float]]:
    """The transcript as comparable tokens, each carrying the word's own clock.

    One word can tokenise to several — Whisper writes "spread-eagle" as one word — and each
    piece keeps the whole word's span, which is the only honest answer: the transcript does
    not say where inside a word one syllable stopped.
    """
    heard: list[tuple[str, float, float]] = []
    for text, start, end in words:
        for token in _lyric_tokens(text):
            heard.append((token, start, end))
    return heard


def align_lyric_blocks(
    lyrics: str, words: list[tuple[str, float, float]]
) -> list[tuple[str, float, float]]:
    """Each `[Tag]` block of the sheet, timed against transcribed words: (tag, start, end)."""
    blocks = _sheet_blocks(lyrics)
    return [
        (blocks[index][0], start, end) for index, start, end in _align_blocks(lyrics, words)
    ]


def _align_blocks(
    lyrics: str, words: list[tuple[str, float, float]]
) -> list[tuple[int, float, float]]:
    """`align_lyric_blocks`, keyed by the block's index into `_sheet_blocks` rather than its tag.

    The index is what a caller needs to reach the block's *lines*, and a tag cannot give it:
    tags repeat, and a block that never anchored is omitted, so the Nth answer is not the Nth
    block. `align_lyric_blocks` projects the tag back out for every existing caller.

    The sheet is the truth about the words; the transcript is the truth about the clock.
    Both run in song order, so the walk is monotonic: one pointer into the transcript,
    advanced block by block. A block anchors where three of its first six tokens match
    inside a small transcript window (a single-word match is how "the" anchors a chorus
    to a verse), then consumes forward matches greedily; its span is first-matched-word
    start to last-matched-word end. A block that never anchors is *omitted* rather than
    guessed — the caller decides what an unplaced block means.

    The Director's ask, verbatim intent (2026-08-20): "knowing where words are and arent
    is useful for knowing which Shots have words, when the cuts should happen, when the
    chorus and verses are" — this is the sheet-tags half; `Song.vocal_spans` is the
    words-at-all half.
    """
    blocks = _sheet_blocks(lyrics)
    heard = _heard_tokens(words)
    aligned: list[tuple[int, float, float]] = []
    cursor = 0
    for block_index, (_tag, text, _lines) in enumerate(blocks):
        tokens = _lyric_tokens(text)
        if not tokens:
            continue
        # One block aligns inside a bounded transcript window past the cursor — three
        # times its own length plus slack, which admits mishearings and the odd ad-lib
        # without letting a chorus match the next chorus. The alignment is a plain LCS
        # (ordered, gaps both sides), which is what survives Whisper swapping "lap it up"
        # for "light me up" mid-line where a greedy walk loses its place.
        window = heard[cursor:cursor + 3 * len(tokens) + 40]
        if not window:
            break
        n = len(tokens)
        # Enough of the block must actually be heard — 40%, floored at three words — or
        # the block is omitted rather than pinned to stray matches.
        threshold = min(n, max(3, round(0.4 * n)))
        count, matches = _lcs_matches(tokens, window)
        if count < threshold:
            continue
        # A refrain matches its own later repeats: this song's second chorus LCS-matched
        # its closing line to the *outro's* identical words, 24 seconds and a whole bridge
        # later — and a global traceback can even split one block's matches across two
        # repeats. So: group the matches into time-dense runs (split at >8 s), then score
        # each run's own time-neighborhood with a FRESH local LCS, and the block lands on
        # the first run that clears the threshold on its own ground (or the longest when
        # none does). The local re-score is what stops a run being under-counted because
        # the global path gave half its words to a later copy.
        runs: list[list[int]] = [[matches[0]]]
        for position in matches[1:]:
            if window[position][1] - window[runs[-1][-1]][2] > 8.0:
                runs.append([position])
            else:
                runs[-1].append(position)
        chosen: tuple[int, int] | None = None
        fallback: tuple[int, tuple[int, int]] | None = None
        for run in runs:
            low, high = run[0], run[-1]
            # Two seconds, not more: this song's chorus-to-bridge rest is exactly 4.0 s,
            # and an expansion that bridges it re-glues the repeats the runs just split.
            # Two seconds still spans every breath inside a sung passage here (max 1.4 s).
            while low > 0 and window[low][1] - window[low - 1][2] <= 2.0:
                low -= 1
            while high + 1 < len(window) and window[high + 1][1] - window[high][2] <= 2.0:
                high += 1
            local_count, local = _lcs_matches(tokens, window[low:high + 1])
            if not local:
                continue
            span = (low + local[0], low + local[-1])
            if local_count >= threshold:
                chosen = span
                break
            if fallback is None or local_count > fallback[0]:
                fallback = (local_count, span)
        if chosen is None:
            if fallback is None:
                continue
            chosen = fallback[1]
        first, last = chosen
        aligned.append((block_index, window[first][1], window[last][2]))
        cursor = cursor + last + 1
    return aligned


@dataclass(frozen=True, slots=True)
class LyricLineSpan:
    """One sung line of the sheet, timed, with whoever the sheet says sings it.

    `index` is `LyricLine.index` — a number into the sheet's own lines — so a caller holding
    one of these can reach the very line the Director typed, mark and all.

    `slots` are the `Asset.character_slot` numbers the line's `(S1)` mark names, and `()` is
    **untagged, not unsung**: every line of every sheet written before per-line marks existed
    reads `()`, and nothing may read that as "the first singer". The distinction is the same
    one `shot_vocal_overlap` draws between silent and unmeasured.

    `start`/`end` are the first and last *heard* word this line matched, so the span is a
    measurement rather than a share-out of the block's length. A line no word of which was
    heard produces no span at all — it is omitted, `align_lyric_blocks`' rule for a block that
    never anchored.
    """

    index: int
    text: str
    slots: tuple[int, ...]
    start: float
    end: float


def align_lyric_lines(
    lyrics: str, words: list[tuple[str, float, float]]
) -> list[LyricLineSpan]:
    """Every sung line of the sheet, timed against the transcript, in song order.

    **Composed, not re-derived.** `_align_blocks` places each `[Tag]` block against the
    transcript — with all of the repeat-defeating machinery that took to get right — and this
    only splits one block's own span among its own lines. Aligning lines directly against the
    whole transcript would need a second answer to the refrain problem (a chorus's lines match
    every other chorus's), and two answers to that question is precisely how a line would end
    up timed to the wrong verse.

    Inside a block the walk is one LCS of the block's tokens, each token carrying the line it
    was typed on, against the words heard within the block's span. Because the LCS is ordered,
    the matched words run strictly forward, so the lines come out in order and no two spans
    overlap. A line whose words were all misheard is dropped rather than given the gap between
    its neighbours: this is the fact a shot's citations will be chosen from, and an invented
    line is worse than a missing one.

    Empty for an unmeasured song (`words` empty) and for a sheet with no `[Tag]` blocks —
    both are "nothing was aligned", stated by the empty list rather than by a guess. Neither
    needs a guard here: `_align_blocks` places no block without a transcript and no block from
    a sheet that has none, so both cases arrive as an empty loop.
    """
    sheet = lyric_line_tags(lyrics)
    blocks = _sheet_blocks(lyrics)
    heard = _heard_tokens(words)
    spans: list[LyricLineSpan] = []
    for block_index, start, end in _align_blocks(lyrics, words):
        _tag, _text, lines = blocks[block_index]
        # The heard words this block was measured to span. Whole words only: a word half
        # inside the block belongs to the block that heard its start.
        window = [
            token for token in heard if token[1] >= start - 1e-9 and token[2] <= end + 1e-9
        ]
        tokens: list[str] = []
        owners: list[int] = []
        for number in lines:
            for token in _lyric_tokens(sheet[number].text):
                tokens.append(token)
                owners.append(number)
        if not tokens or not window:
            continue
        _count, pairs = _lcs_pairs(tokens, window)
        matched: dict[int, list[int]] = {}
        for token_index, window_index in pairs:
            matched.setdefault(owners[token_index], []).append(window_index)
        for number in lines:
            hits = matched.get(number)
            if not hits:
                continue
            line = sheet[number]
            spans.append(
                LyricLineSpan(
                    index=number,
                    text=line.text.strip(),
                    slots=line.slots,
                    start=window[hits[0]][1],
                    end=window[hits[-1]][2],
                )
            )
    return spans


def _lcs_matches(
    tokens: list[str], window: list[tuple[str, float, float]]
) -> tuple[int, list[int]]:
    """Longest common subsequence of a block against a transcript slice.

    Returns the match count and the traceback's window indices, in order. `_lcs_pairs` with
    the token half projected away — the block walk only ever needed to know *which words* were
    heard, and the line walk needs to know which sheet token each of them answered.
    """
    count, pairs = _lcs_pairs(tokens, window)
    return count, [position for _token, position in pairs]


def _lcs_pairs(
    tokens: list[str], window: list[tuple[str, float, float]]
) -> tuple[int, list[tuple[int, int]]]:
    """Longest common subsequence, as ``(token index, window index)`` pairs in order.

    Plain LCS — ordered, gaps free on both sides — because that is what survives a mishearing
    mid-line where any greedy walk loses its place.
    """
    n, m = len(tokens), len(window)
    table = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        row, next_row = table[i], table[i + 1]
        for j in range(m - 1, -1, -1):
            if _tokens_agree(tokens[i], window[j][0]):
                row[j] = 1 + next_row[j + 1]
            else:
                row[j] = max(next_row[j], row[j + 1])
    i = j = 0
    matches: list[tuple[int, int]] = []
    while i < n and j < m:
        if _tokens_agree(tokens[i], window[j][0]):
            matches.append((i, j))
            i += 1
            j += 1
        elif table[i + 1][j] >= table[i][j + 1]:
            i += 1
        else:
            j += 1
    return table[0][0], matches


def proposed_sections_from_alignment(
    aligned: list[tuple[str, float, float]], song_duration: float
) -> list[tuple[str, float, float, str]]:
    """Aligned blocks as a whole-song section tiling: (label, start, duration, prompt).

    Repeated tags gain ordinals ("Verse", "Chorus", "Verse 2"…) so the timeline reads, and
    `section_lyrics`' family pairing still matches them to their sheet blocks. Each section
    runs to the next block's first word — an instrumental tail belongs to the section it
    follows, which is how a Director hears it — and the last runs out the song. Time before
    the first word is an "Intro" section when it is at least two seconds, because that gap
    is exactly the planning fact the Director asked for (b-roll space, no lipsync).
    Prompts are left empty: timing is measured, look is authored.
    """
    if not aligned or song_duration <= 0:
        return []
    proposals: list[tuple[str, float, float, str]] = []
    counts: dict[str, int] = {}
    first_start = round(aligned[0][1], 2)
    if first_start >= 2.0:
        proposals.append(("Intro", 0.0, first_start, ""))
    for index, (tag, start, _end) in enumerate(aligned):
        counts[tag] = counts.get(tag, 0) + 1
        label = tag if counts[tag] == 1 else f"{tag} {counts[tag]}"
        section_start = round(start, 2) if proposals or index > 0 else 0.0
        if index == 0 and not proposals:
            # No intro worth marking: the first section owns the opening seconds too.
            section_start = 0.0
        next_start = (
            round(aligned[index + 1][1], 2) if index + 1 < len(aligned) else song_duration
        )
        duration = round(next_start - section_start, 2)
        if duration > 0:
            proposals.append((label, section_start, duration, ""))
    return proposals


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


#: How much of an Asset's own words the assistant is shown. A vision summary is a paragraph and a
#: Flux generation prompt can be far longer; a library of forty assets at full length is a project
#: dump wearing a different key, which is the one thing this payload exists not to be.
ASSISTANT_DESCRIPTION_LIMIT = 300


def asset_anchor(asset: Asset) -> str:
    """This Asset's stored appearance anchor, whitespace-collapsed, or `""` when it has none.

    One reader for `Asset.consistency_prompt`, because "has an anchor" has to mean the same
    thing in the reference map, in the expansion input and in the library description — a
    field that is blank in one of them and a single space in another would put the anchor on
    two of the three surfaces and silently not the third.

    Collapsed rather than merely stripped: the anchor is typed into a textarea and travels
    inside one-line prompt sentences, where a pasted newline would read as a shot boundary to
    the H3 specialist (`h3_expansion_prompt.STRUCTURE`: "a line break reads as a shot
    boundary"). An all-whitespace value collapses to `""` and is therefore *no anchor*, which
    is what a Director who cleared the box meant.
    """
    return " ".join(asset.consistency_prompt.split())


def anchored_label(asset: Asset, label: str) -> str:
    """``label``, carrying this Asset's appearance anchor when it has one.

    The one composition, shared by `app.reference_map_tag_lines`, the submit route's own tag
    walk and `shot_expansion_input` below, so the anchor a shot is *told* about and the anchor
    its render is *conditioned* with can never be two different sentences.

    ``label`` is whatever names the asset in the caller's context — usually `Asset.name`, and
    for the reference map the shot's own `reference_labels` override when one is set. **A
    per-shot rename replaces the name and never the anchor**, and the two compose as
    apposition: `"the woman upstage, a woman in a red leather jacket and black boots"`. That
    is the only composition that keeps both facts, and each answers a different question — the
    rename says who this picture is *in this shot*, the anchor says what she looks like in
    every shot. A rename that was meant to replace the appearance too is expressed by clearing
    the anchor, which is a decision about the asset rather than about one shot.

    An asset with no anchor returns ``label`` unchanged, and that identity is load-bearing:
    every prompt this application has ever built used the bare label, and an anchor-free
    project must go on producing those exact bytes.
    """
    anchor = asset_anchor(asset)
    if not anchor:
        return label
    return f"{label}, {anchor}"


def _asset_description(asset: Asset) -> str:
    """What this Asset depicts, in as few characters as the honest answer takes.

    **The Director's own `consistency_prompt` wins over both of the machine-written sources.**
    It is the one description a human asserted, and the whole point of the field is that it
    overrides whatever a generator was asked for or a vision model reported — see
    `models.Asset.consistency_prompt`.

    Failing that, the vision inspection wins over the generation prompt, because it describes
    what the picture *is* rather than what was asked for — a Flux prompt and its output disagree
    often enough that citing the prompt would tell the assistant about a shot that was never made.
    An uploaded asset with none of the three has no description at all, and gets none rather than
    an invented one: its name and kind are what is actually known about it.
    """
    described = (
        asset_anchor(asset) or (asset.vision.summary if asset.vision else "") or asset.prompt
    )
    collapsed = " ".join(described.split())
    if len(collapsed) <= ASSISTANT_DESCRIPTION_LIMIT:
        return collapsed
    return f"{collapsed[:ASSISTANT_DESCRIPTION_LIMIT]}…"


def assistant_input(project: Project, *, shot_ids: list[str]) -> dict[str, Any]:
    """The purpose-built, trimmed input for one Assistant ProducerBot turn. Pure and I/O-free.

    Built the way `expansion_input` is and for the same recorded reason — rich context is the root
    cause of Director degradation — with three differences that follow from what this call is for:

    * **Only the selected shots.** `shot_ids` is the turn's scope, and it is the scope in two
      senses: the model is shown these shots and no others, and the route refuses to write to
      anything outside the list. Sending the whole plan would both cost the tokens and invite the
      model to answer for shots the Director did not select.
    * **The library, because citing is the point.** Each Asset carries its id (copied verbatim into
      a citation), its name, its kind and a bounded description. No path, no `prompt_id`, no
      `created_at`, and no vision record beyond its summary.
    * **The taxonomy, because the arity is not in the tool schema.** The schema constrains a mode to
      the enum; only this says that `first_middle_last` cites three images in three named roles, and
      that a mode may be plannable while `renderable` is false.

    What is deliberately *not* here is production state. No `status`, `prompt_id`, `latest_output`,
    `latest_review` or `approved_output`, and no derived "this has a take" flag either — the route
    refuses those shots on its own evidence, exactly as expansion does, and a flag would put back
    the state the trimming exists to keep out.

    Shots are ordered by `ordered_shots` — song order — and named by `shot_label`, which is the
    manifest position the timeline draws. The two orderings differ; the *name* is the timeline's,
    because it is the name the Director reads on the clip and the name the reply's notices use.
    """
    ordered = ordered_shots(project)
    position = {shot.id: index for index, shot in enumerate(ordered)}
    song_duration = project.song.duration if project.song else 0.0
    shots: list[dict[str, Any]] = []
    for shot_id in shot_ids:
        index = position.get(shot_id)
        if index is None:
            continue
        shot = ordered[index]
        entry: dict[str, Any] = {
            "shot_id": shot.id,
            "label": shot_label(project, shot),
            "start": round(shot.start, 3),
            "end": round(shot.end, 3),
            "duration": round(shot.duration, 3),
            "locked": shot.locked,
            "current_mode": resolve_shot_mode(shot),
            "current_prompt": shot.prompt,
            # Whether this Shot has an H3 expansion, and **never the expansion itself.** The
            # assistant has a tool that asks for one, so it needs to know which shots already have
            # one or it will re-expand a whole plan on every request; a boolean answers that at the
            # cost of one token. The text is what `SHOT_DIRECTOR_WITHHELD` keeps out of the chat
            # dump — MiniMax's own worked examples run past a thousand characters each — and this
            # payload is not the exception to that, it is the same model on the same machine.
            "expanded": bool(shot.h3_prompt.strip()),
            "singing": shot.singing,
            "use_song_audio": shot.use_song_audio,
            "citations": [
                {"asset_id": citation.asset_id, "role": citation.role}
                for citation in shot.citations
            ],
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
            # Clamped guidance rather than geometry, for `expansion_input`'s reason: a Shot can
            # legitimately sit past the end of a shorter song, and "3.5 of the way through" is not
            # a fact a model can use. `start` and `end` above carry the real timing unclamped.
            entry["song_fraction"] = round(min(1.0, max(0.0, shot.start / song_duration)), 4)
        section = song_section(project, shot)
        if section:
            # An explicit dict, never the model object: this payload is json.dumps'd
            # by the client, and a raw SongSection is a TypeError at send time. These
            # two branches were dead while song_section returned "" and came alive
            # with the wrong type when sections landed (found by the run-2 audit).
            entry["section"] = {"label": section.label, "prompt": section.prompt}
        shots.append(entry)
    payload: dict[str, Any] = {
        "creative_brief": project.creative_brief,
        "treatment": project.treatment,
        "style_bible": project.style_bible,
        # The taxonomy as data, derived from the table rather than described in the prompt, so a
        # mode added to `SHOT_MODE_SPECS` reaches the assistant without anyone editing prose.
        # `renderable` is the honest statement of the plannable-but-unrenderable pair: planning one
        # is allowed and is refused later, at the point GPU time would be spent.
        "modes": [
            {
                "mode": mode,
                "label": spec.label,
                "renderable": bool(spec.adapter),
                "song_audio": spec.song_audio,
                "roles": [
                    {
                        "role": requirement.role,
                        "minimum": requirement.minimum,
                        "maximum": requirement.maximum,
                    }
                    for requirement in spec.roles
                ],
            }
            for mode, spec in SHOT_MODE_SPECS.items()
        ],
        "asset_roles": dict(ASSET_ROLE_LABELS),
        "assets": [
            {
                key: value
                for key, value in (
                    ("asset_id", asset.id),
                    ("name", asset.name),
                    ("kind", asset.kind),
                    ("description", _asset_description(asset)),
                )
                if value
            }
            for asset in project.assets
        ],
        "h3_shot_window": {"min": H3_MIN_SHOT_SECONDS, "max": H3_MAX_SHOT_SECONDS},
        "shots": shots,
    }
    if project.song is not None:
        # Title, length, and what the Director already said the song *is*, on `expansion_input`'s
        # convention exactly: present when the Song carries them, absent rather than `""` when it
        # does not, because `""` is a confident claim that the song has no words.
        song: dict[str, Any] = {"title": project.song.title, "duration": project.song.duration}
        if project.song.lyrics:
            song["lyrics"] = project.song.lyrics
        if project.song.caption:
            song["caption"] = project.song.caption
        payload["song"] = song
    return payload


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
    the reason `song_section` documents. The song block's `lyrics` and `caption` follow the same
    convention: present when the Song carries them, absent rather than `""` when it does not.

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
            # An explicit dict, never the model object: this payload is json.dumps'd
            # by the client, and a raw SongSection is a TypeError at send time. These
            # two branches were dead while song_section returned "" and came alive
            # with the wrong type when sections landed (found by the run-2 audit).
            entry["section"] = {"label": section.label, "prompt": section.prompt}
        shots.append(entry)
    payload: dict[str, Any] = {
        "creative_brief": project.creative_brief,
        "treatment": project.treatment,
        "style_bible": project.style_bible,
        "h3_shot_window": {"min": H3_MIN_SHOT_SECONDS, "max": H3_MAX_SHOT_SECONDS},
        "shots": shots,
    }
    if project.song is not None:
        # Title, length, and what the Director already said the song *is*. The lyric sheet and
        # the style caption are the Song's bulkiest fields, and carrying them is a deliberate
        # decision about what this call sees rather than an oversight: the chat route has always
        # sent both, and expansion is the planning act most likely to want the words. The
        # context objection that keeps this input trimmed is about *accumulation* — one
        # expansion is a single stateless whole-plan call, so the sheet costs its tokens once
        # per expansion and not once per turn the way the chat thread does.
        #
        # Sent exactly as stored: nothing here parses, sections, excerpts or summarises a lyric
        # sheet, and a section tag inside one is structure, never a timestamp.
        #
        # Absent rather than empty, for the same reason `song_fraction` is absent when there is
        # no duration: `""` is not "this song has no words", it is a confident claim that it
        # has none. A Song carrying neither field therefore produces the payload this builder
        # produced before either field existed, byte for byte.
        song: dict[str, Any] = {"title": project.song.title, "duration": project.song.duration}
        if project.song.lyrics:
            song["lyrics"] = project.song.lyrics
        if project.song.caption:
            song["caption"] = project.song.caption
        payload["song"] = song
    return payload


def shot_expansion_input(project: Project, shot: Shot) -> dict[str, Any]:
    """The trimmed input for expanding **one** Shot into an H3-format prompt. Pure, I/O-free.

    Per-Shot, deliberately, and the opposite shape to `expansion_input` above. That one is a
    single whole-plan call because cross-shot variance is a property of the plan; this one is
    one call per Shot because a single H3 prompt is long and thirty of them will not fit one
    context — quality would degrade well before the limit. The two passes want opposite shapes
    and this is the second.

    What it carries was chosen by the Director on 2026-08-18: the Shot's own facts, the
    **neighbours' intents**, the treatment and style bible, and the song. Explicitly *not* the
    neighbours' expansions — those are the long form, and carrying two of them per call would
    reintroduce exactly the bloat that makes one-shot-for-all impossible.

    Neighbour *intents* rather than nothing, unlike `expansion_input`, which withholds neighbour
    prompts on the reasoning that on a first pass they are all `""` or placeholders. On this pass
    they are real: pass one has already run, and a cut that lands well needs to know what it is
    cutting from.

    **The lyric sheet is sent whole, and labelled as whole.** The Director asked for "the song's
    words for this window", and that cannot be built: nothing in this project aligns lyrics to
    time — `song_section` is an empty branch for exactly that reason, there is no BPM or section
    field on any model, and the analyser does not exist. Sending the whole sheet under a key that
    claimed it was this window's words would be a fabrication of precisely the kind this codebase
    keeps catching. So it goes as `lyrics`, with `song_fraction` beside it as the honest signal of
    where in the song this Shot sits, and the specialist's prompt is what tells the model the
    sheet is not aligned.
    """
    payload: dict[str, Any] = {
        "creative_brief": project.creative_brief,
        "treatment": project.treatment,
        "style_bible": project.style_bible,
    }
    ordered = ordered_shots(project)
    index = next((i for i, other in enumerate(ordered) if other.id == shot.id), None)
    if index is None:
        raise TimelineError("that Shot is not in this project")

    entry: dict[str, Any] = {
        "id": shot.id,
        "index": index + 1,
        "of": len(ordered),
        "start": shot.start,
        "end": shot.end,
        # The length the prompt's own cut times must fall inside. Named `duration` and not
        # `window` because the prompt's clock starts at 00:00.000 regardless of `start`.
        "duration": shot.duration,
        "mode": resolve_shot_mode(shot),
        "singing": shot.singing,
        "intent": shot.prompt,
    }
    # Tags the specialist may use, numbered here rather than left to the model. The prompt
    # forbids inventing one, and a model told "you have two pictures" will still guess at their
    # numbers; naming each tag alongside its role removes the guess entirely.
    #
    # Numbered by `models.numbered_references` — literally the same function the reference render
    # numbers its own tags and appends its media by, not a second implementation of the same idea.
    # That sharing is load-bearing, not tidy: the specialist writes "<Video 1>" into a prompt whose
    # payload fills its anonymous slots in the render's walk, so a tag numbered here under any
    # other rule declares a role for somebody else's media and the take renders plausibly and
    # wrongly. This builder used to run its own counter — one series for every kind — and on a
    # video-citing shot it disagreed with the payload for exactly that reason (2026-08-20). A
    # picture-only shot's numbering is unchanged, which is what the pinned digests below assert.
    references: list[dict[str, Any]] = []
    if shot.citations:
        for numbered in numbered_references(project, shot):
            citation = numbered.citation
            entry_reference: dict[str, Any] = {
                "tag": numbered.tag,
                "role": ASSET_ROLE_LABELS.get(citation.role, citation.role),
                "asset_id": citation.asset_id,
            }
            # The Director's stored appearance anchor for this picture, named and numbered
            # beside the tag that shows it — `<Picture 2>` and "Lucy, a woman in a red
            # leather jacket" are the same subject, and the specialist is told to carry that
            # phrase at her first mention rather than to invent a description from the
            # image it cannot see. Composed by the same `anchored_label` the reference map
            # uses, so what the specialist is handed and what the render is conditioned with
            # are one sentence.
            #
            # **Present only when an anchor is actually stored**, on this payload's own
            # absent-rather-than-empty convention: an anchor-free project's expansion input
            # is byte-for-byte the one it has always produced, which the pinned digests
            # assert. A citation whose Asset this project does not hold gets no key either —
            # nothing is known about it, and the render refuses it by name.
            asset = numbered.asset
            if asset is not None and asset_anchor(asset):
                entry_reference["anchor"] = anchored_label(
                    # The shot's own rename when it has one, exactly as the reference map
                    # resolves it, so the specialist and the render name the picture the
                    # same way.
                    asset,
                    shot.reference_labels.get(asset.id, asset.name),
                )
            references.append(entry_reference)
    # The master song's tag, when this shot rides it — numbered by the same walk the
    # render numbers it (`song_audio_tag`), so the "<Audio 1>" the specialist writes into
    # the description is the slot the conditioner actually fills. This is the handle the
    # creator's own working music-video prompts use for lipsync: the description names
    # the audio tag as the rhythm the lips follow, and no lyric text ever appears.
    if shot.use_song_audio and project.song:
        references.append(
            {
                "tag": f"<Audio {song_audio_tag(project, shot)}>",
                "role": "master song — this shot's exact window of the project track",
            }
        )
    if references:
        entry["references"] = references
    # The Shot's section, when the Director has marked any (2026-08-19). This is what
    # replaces guessing a shot's words from `song_fraction`: the section carries its own
    # label and its shared characteristics. NO lyric text, deliberately (2026-08-19,
    # twice-measured): given words, the model plants them into wrong windows, and words
    # in the prompt fight the audio reference that actually drives the mouth — the
    # Director's own LTX observation. `section_lyrics` remains for planning surfaces;
    # the expansion never sees words.
    section = song_section(project, shot)
    if section is not None:
        entry["section"] = {
            "label": section.label,
            "prompt": section.prompt,
            # How far into the section this clip sits, 0..1 — an energy-curve hint.
            "clip_position": round(
                min(1.0, max(0.0, (shot.start - section.start) / section.duration)), 3
            )
            if section.duration
            else 0.0,
        }
    payload["shot"] = entry

    neighbours: dict[str, Any] = {}
    if index > 0:
        neighbours["previous"] = {"id": ordered[index - 1].id,
                                  "intent": ordered[index - 1].prompt}
    if index + 1 < len(ordered):
        neighbours["next"] = {"id": ordered[index + 1].id, "intent": ordered[index + 1].prompt}
    if neighbours:
        payload["neighbours"] = neighbours

    if project.song:
        song: dict[str, Any] = {"title": project.song.title, "duration": project.song.duration}
        # The lyric sheet no longer rides the per-shot expansion at all — the same
        # no-words rule as the section block above. The caption (how the track sounds)
        # is the mood carrier, and it is all the specialist needs about the music.
        if project.song.caption:
            song["caption"] = project.song.caption
        # Where in the song this Shot sits, computed exactly as `expansion_input`
        # computes it: the energy-curve hint.
        song_duration = project.song.duration
        if song_duration:
            song["song_fraction"] = round(
                min(1.0, max(0.0, shot.start / song_duration)), 4
            )
        payload["song"] = song
    return payload


# ------------------------------------------------------------------------------------------
# Snapping cuts to phrase boundaries.
#
# The Director's ruling, 2026-08-20, on the roadmap's long-open "vocal transition points
# between shots" item: **cut placement is the lever.** Each references shot performs its own
# window of the song, so at a cut the mouth on shot A's last frame and the mouth on shot B's
# first frame were rendered by two calls that never saw each other. Pinning keyframes and
# crossfading both *mask* that; moving the cut to a moment the track leaves voiceless removes
# it — there is no mouth to mismatch if nobody is singing across the boundary. It costs no
# GPU and no re-render, which is why it was ruled the lever rather than one of the others.
#
# This is the first thing in the application that turns Whisper's alignment into an editing
# decision rather than a display. Everything below is pure and I/O-free: it reads a Project
# and answers with a proposal. Nothing here writes, and nothing here renders.
#
# Three geometric facts shape the whole design:
#
# * **A seam is shared.** Moving it changes shot A's duration and shot B's start together —
#   one move, not two — so the plan is modelled as a list of *seams* and the shots are derived
#   from them. Coverage is then true by construction rather than by arithmetic. A seam is a
#   single boundary when the two shots meet at one, and an **overlap** when the Director has
#   dragged them across each other; `_seam_overlaps` is the one place that tells them apart
#   and `SEAM_POINT` argues where an overlap's cut actually is.
# * **The plan's outer edges never move.** The first shot's start and the last shot's end are
#   copied through untouched, so whatever coverage the plan had of the song it still has,
#   exactly, to the last bit of the float. That is the frame grid (R-7) held structurally: the
#   assembled video's length against the song cannot change, whatever any seam does.
# * **Seams are decided left to right**, each against the edge its left neighbour has already
#   settled on and against its right neighbour's *original* edge. That is what makes the band
#   check exact rather than provisional: see `snap_window_plan`.
#
# **One implementation, two doors.** `snap_window_plan` is the whole of the decision and it
# knows nothing about Shots; `snap_cut_plan` is the door from a project's timeline (the
# snap-cuts route) and `app.line_up_shots` is the door from a freshly laid-out tiling that has
# no shots yet (populate's second step). A second snapper for the second caller would put a cut
# in one place or another depending on which door the Director came through, which is the
# reference-map-numbering defect wearing different clothes.
# ------------------------------------------------------------------------------------------

#: How far *inside* a voiceless gap a snapped cut must land, at either end of the gap.
#:
#: A cut placed on the exact instant a phrase stopped is not "where nobody is singing" in any
#: useful sense — the mouth is still closing, and `transcription.transcribe_song_words` rounds
#: Whisper's word times to the hundredth of a second with an accuracy well looser than that.
#: 0.15 s is 3.6 frames at `H3_FPS`, and it is deliberately far below the 0.75 s rest that
#: `transcription.merge_vocal_spans` requires before it will call two words separate spans —
#: so every gap that merge reports can hold this clearance at *both* ends with room to spare.
SNAP_CLEARANCE_SECONDS = 0.15

#: The shortest voiceless stretch `vocal_gaps` will report as a gap at all.
#:
#: Exactly twice the clearance, which is the whole argument: a stretch narrower than this
#: cannot give `SNAP_CLEARANCE_SECONDS` at both ends, so no cut can be *placed* in it on this
#: module's own terms. `_gap_snap_target` does answer for a narrower gap — its midpoint — but
#: that answer knowingly spends less than the clearance, and the clearance is the hard floor
#: the tolerance is tuned around.
#:
#: **Not a double-guard, and it only became necessary with word-level gaps.** While
#: `vocal_gaps` read `merge_vocal_spans`' output, every interior gap was ≥ 0.75 s by
#: construction and this floor was unreachable. Word timings have no such floor: on the
#: Director's own track (262 aligned words) the word-level complement holds **21** stretches
#: under 0.30 s — the pauses *inside* a sung phrase, between one word and the next. Offered
#: as snap targets those would be strictly worse than not moving, because each sits between
#: two syllables and is usually nearer the cut than the real rest a few seconds away, so the
#: nearest-gap rule would prefer it. Filtered here rather than in `snap_cut_plan` because it
#: is a statement about what a *gap* is, not about what one caller does with one.
SNAP_MINIMUM_GAP_SECONDS = 2 * SNAP_CLEARANCE_SECONDS

#: How far a cut may travel, by default, when the Director has expressed no preference.
#:
#: Pinned to `merge_vocal_spans`' own 0.75 s gap, and the coupling is the argument: a cut may
#: move at most as far as the shortest rest the measurement is willing to call a rest. A
#: larger default would let the feature rewrite the plan's rhythm — shot lengths are a
#: creative decision made elsewhere — in the name of a boundary fix.
SNAP_TOLERANCE_DEFAULT = 0.75

#: The largest tolerance a caller may ask for. Three seconds is already most of a
#: `POPULATE_TARGET_WINDOW_SECONDS` window; past that, "snap the cut" has stopped describing
#: what is happening to the plan.
SNAP_TOLERANCE_MAX = 3.0

#: How far two adjacent windows may disagree about where their shared boundary is before this
#: module stops calling it one boundary. Half a frame, the number assembly's own
#: `BOUNDARY_TOLERANCE_SECONDS` uses — the same number, because "the same boundary written twice" has to mean the same
#: thing to the pass that moves cuts and to the pass that assembles them. Deliberately a
#: literal rather than an import: `assembly` sits beside `timeline` in the graph, not below
#: it, and `app` is the module that imports both.
#:
#: **What it decides has changed, and only in one direction.** Past this tolerance a *positive*
#: disagreement — B starts after A ends — is a hole, and holes are still refused (`SNAP_HOLE`).
#: A *negative* one is an overlap, which since the Director's layers ruling (2026-08-20) is a
#: legitimate editing gesture and since R-3 (2026-08-21) is how a transition is authored. Those
#: are snapped, as units. The sign is the whole of the difference, and it is the sign
#: `assembly.tiling_refusals` already reads: a gap is a refusal there, an overlap is not.
SNAP_CONTIGUITY_TOLERANCE = 1 / (2 * H3_FPS)

# ------------------------------------------------------------------------------------------
# Why one cut did not move. Every sentence names the shots as the timeline names them
# (`shot_label`), because a bare `shot_a1b2c3d4e5f6` appears nowhere in the interface — the
# same reason `generate_h3`'s refusal carries the label.
#
# The three *protection* sentences are the load-bearing half of this feature and each states
# the concrete harm rather than merely that a flag is set, in the house shape shared by
# `RENDER_AGAIN_LOCKED_REFUSAL`, `MARK_READY_LOCKED_REFUSAL` and `SELECT_TAKE_LOCKED`.
# ------------------------------------------------------------------------------------------

SNAP_LOCKED_REFUSAL = (
    "{shot} is locked. A lock is a deliberate hands-off on this shot, and moving the cut at "
    "its edge changes its window, which is exactly the kind of change it refuses. Unlock the "
    "shot first."
)
#: AD-13, stated as the consequence. Approval snapshots the window (`Shot.approved_start`/
#: `approved_duration`) and `assembly.ASSEMBLY_STALE_REFUSAL` refuses a shot whose window
#: moved afterwards — so a silent nudge here would trade a mouth mismatch for a plan that no
#: longer assembles. **Refused rather than auto-un-approved**: an approval is an editorial
#: decision about one take, and `POPULATE_PROTECTED_REFUSAL` already rules that a protection
#: which vanishes with the thing it protected was never a protection.
SNAP_APPROVED_REFUSAL = (
    "{shot} carries an approved take, and an approval records the window it was approved in. "
    "Moving this cut would change that window and assembly would then refuse the shot as "
    "stale. Un-approve the take first if the cut should move."
)
#: `RENDER_AGAIN_IN_FLIGHT_REFUSAL`'s argument applied to a window rather than to a second
#: submission, including its staleness escape: job status only moves when the queue is polled.
SNAP_IN_FLIGHT_REFUSAL = (
    "A render for {shot} has not finished, and it was submitted for the window this cut "
    "would change. Wait for it, or refresh the render queue if it has already finished and "
    "this project has not been told yet."
)
#: The layout's own boundary, protected from the step that comes after it.
#:
#: **The defect this closes, measured live 2026-08-21.** Lay-out tiles each section separately so
#: no shot straddles a boundary, and then line-up — which knew nothing about sections — moved 2 of
#: 5 cuts *across* one: 11.000 → 10.850 over the Intro/Verse switch and 103.200 → 103.050 over
#: Chorus 2/Bridge. Step 2 was quietly spending the boundary step 1 exists to protect, and the
#: 0.15 s it bought was a phrase edge the section switch already is.
#:
#: A protection rather than a preference, and refused rather than clamped, for `SNAP_HOLE`'s
#: reason: moving such a cut *anywhere* runs one section's shot into the next section's seconds,
#: and choosing which side should give way is an editing decision this pass cannot make. A
#: section boundary is the music's own switch, so a cut already on one is already where the
#: phrase changes.
SNAP_SECTION_BOUNDARY = (
    "The cut between {before} and {after} at {boundary:.3f}s sits on the boundary of "
    "{section}, and the layout put it there so no shot straddles a section. Moving it would "
    "run one section's shot into the next section's seconds. Left where it was."
)
#: Not a refusal — the good case where there was nothing to do. Reported rather than dropped,
#: because "18 cuts already land in silence" is the sentence that tells a Director the plan is
#: healthier than they feared.
SNAP_ALREADY_SILENT = (
    "The cut between {before} and {after} at {boundary:.3f}s already lands clear of every "
    "sung phrase."
)
SNAP_NO_GAP_IN_TOLERANCE = (
    "The cut between {before} and {after} at {boundary:.3f}s sits inside a sung phrase, and "
    "the track has no voiceless gap within {tolerance:.2f}s of it. Raise the tolerance, or "
    "move this cut by hand."
)
#: The band refusal, with every number the decision used. `neighbour` is whichever of the two
#: shots the move would push out of the band, and the sentence says which end of the band.
SNAP_OUT_OF_BAND = (
    "The cut between {before} and {after} at {boundary:.3f}s would move to {proposed:.3f}s, "
    "which leaves {neighbour} at {length:.3f}s — {bound} the {limit:g}s {edge} H3 renders "
    "reliably. Left where it was."
)
#: The whole-plan honest-empty branches. Each is the *explicitly empty* answer this codebase
#: requires of an absent analysis (`song_section`, `shot_vocal_overlap`): never a guessed
#: boundary, never a fabricated silence.
SNAP_UNMEASURED = (
    "Nothing has been heard on this track yet, so where the singing is and is not is unknown "
    "and no cut can be placed against it. Run Analyze structure on the Song first — it "
    "transcribes the track once and keeps the words. Nothing was moved."
)
SNAP_TOLERANCE_OFF = (
    "Tolerance is 0, so snapping is off and nothing was examined. Raise it to let a cut move."
)
SNAP_WITHOUT_CUTS = (
    "A cut is the boundary two shots share, and this plan has {count} shot(s), so there is "
    "no cut to snap."
)
#: A gap and an overlap are *different* things to this application, and only one of them stops
#: this pass. A hole is seconds of song with no picture in them: `assembly.tiling_refusals`
#: refuses the export for it, and there is no seam there to place because there is nothing
#: there at all. An overlap is the opposite of an absence — it is an authored transition
#: (R-3) — and it snaps, as a unit. The sentence says both, so a Director who reads it knows
#: their overlaps are not what is being complained about.
#:
#: Refused rather than repaired, which is the rule the retired `SNAP_NOT_CONTIGUOUS` carried
#: and the half of it that survived: closing a hole is an editing decision, and it is one this
#: pass could only make by guessing which of the two windows should grow.
SNAP_HOLE = (
    "This plan has a hole in it — {before} ends at {end:.3f}s but {after} starts at "
    "{start:.3f}s, and nothing covers the {length:.3f}s between. Assembly refuses that hole "
    "too, so there is no seam here to place: close it first, or move this cut by hand. An "
    "overlap is not a hole — a transition is an overlap, and this pass moves one as a unit."
)
#: The other shape this module has no seam for, and it is a *plan* refusal like the hole
#: rather than a per-cut skip, for the same reason: the geometry is not one this pass can
#: reason about at all.
#:
#: An overlap that swallows a window whole is two visible cuts, not one — `assembly_plan`
#: splits the underneath clip around the overlay and resumes it afterwards — so "the cut
#: between these two" names no single instant. Worse, the seam *after* a swallowed window is
#: not what it appears to be: the next window may start well after the swallowed one ends and
#: still be covered by the clip underneath, so treating that as a shared boundary and moving
#: it would silently stretch a window by seconds. Refused rather than guessed at.
SNAP_NESTED = (
    "{after} sits entirely inside {before} — {before} runs {start:.3f}s to {end:.3f}s and "
    "{after}'s {inner_start:.3f}s to {inner_end:.3f}s is within it. One clip laid wholly over "
    "another has two visible cuts rather than one seam, so there is no single point here to "
    "place. Move one of them out from under the other, or place these cuts by hand."
)
#: R-7 as a per-cut refusal: **the frame grid is inviolable.**
#:
#: A transition is as wide as the Director made it, and a seam near either end of the plan can
#: have room to move without having room for its whole blend to move — the band check bounds
#: the two *windows*, and an overlap longer than the minimum window can still hang its far
#: edge off the end. That edge is the assembled video's last frame, so letting it travel would
#: change the video's length against the song, which nothing here may do. The cut stays.
SNAP_OFF_THE_PLAN = (
    "The cut between {before} and {after} at {boundary:.3f}s would move to {proposed:.3f}s, "
    "which would carry its {overlap:.3f}s transition {edge}. The assembled video's length "
    "against the song may not change, so a transition may not hang off either end of the "
    "plan. Left where it was."
)


@dataclass(slots=True, frozen=True)
class CutMove:
    """One cut that would move, named by both shots it belongs to.

    `gap` is how long the voiceless stretch this cut lands in *is*, and it is reported for the
    Director's own reason (2026-08-20): "a 1 second gap may just be an extended shot where a 4
    second gap would be great for a b-roll or non singing character shot." The number is the
    difference between two decisions this feature has already made — which gap, and where in
    it — and it costs nothing to carry, whereas re-deriving it from the report's boundary
    would be a second opinion about which gap was chosen. Nothing here suggests what to *do*
    with a long gap; that is a plan for elsewhere. This is the fact the plan would need.
    """

    before_id: str
    after_id: str
    before_label: str
    after_label: str
    #: Where the cut **is**, which for an overlapping seam is the transition's centre rather
    #: than either clip's edge — `SEAM_POINT` argues why. 0 for a hard cut means the two are
    #: the same number, so a plan without transitions reads exactly as it always did.
    boundary: float
    proposed: float
    #: Length in seconds of the voiceless stretch `proposed` lands in. Deliberately without a
    #: default: a move that could not say which gap it found would be a move nobody measured.
    gap: float
    #: How long the transition at this seam is, 0 for a hard cut. Carried because without it
    #: the report names an instant at which neither clip has an edge, and a Director looking
    #: for that instant on the timeline would find nothing there. It is also the number that
    #: says the move did not resize anything: the same length before and after.
    overlap: float = 0.0

    @property
    def shift(self) -> float:
        return self.proposed - self.boundary


@dataclass(slots=True, frozen=True)
class CutSkip:
    """One cut that would not move, and the sentence saying why.

    Every cut that is not in `CutSnapPlan.moves` is in here — protections, already-silent
    cuts, out-of-tolerance cuts and band refusals alike. A cut that appeared in neither list
    would be the silent skip the report exists to prevent.
    """

    before_id: str
    after_id: str
    before_label: str
    after_label: str
    boundary: float
    reason: str


@dataclass(slots=True)
class CutSnapPlan:
    """What snapping *would* do. Nothing in here has been written anywhere.

    `windows` is the **whole plan**, every shot in song order with the window it would have —
    unchanged shots included — so applying the plan is one assignment per shot and the
    tiling can be asserted from the proposal alone, without re-deriving it.

    `status` is the honest four-way answer, and three of its values mean *nothing was
    examined*: `"off"` (tolerance 0, the feature switched off), `"unmeasured"` (no Whisper
    alignment on this song) and `"no_cuts"` (fewer than two shots). Only `"ready"` claims the
    lists below were computed against a measurement.
    """

    status: str
    tolerance: float
    moves: list[CutMove]
    skips: list[CutSkip]
    windows: list[tuple[str, float, float]]
    message: str = ""


def vocal_gaps(song, *, start: float, end: float) -> list[tuple[float, float]] | None:
    """The stretches of ``[start, end]`` the track is *measured* to leave voiceless.

    ``None`` when nothing was measured, on `shot_vocal_overlap`'s rule exactly and for its
    reason: a song with **neither** `Song.lyric_words` nor `Song.vocal_spans` is
    **unmeasured, not silent**. Answering "the whole song is a gap" for an untranscribed track
    would place every cut in the plan against a silence nobody heard, which is the fabrication
    this codebase keeps catching. Neither list being empty is ever read as silence.

    **Words first, spans as the fallback, and that choice is the feature.** `Song.vocal_spans`
    is `transcription.merge_vocal_spans`' output, which bridges any rest shorter than 0.75 s
    so the timeline's vocal band does not flicker on every breath — a *display* decision.
    Cut placement is not display: the breath-sized rests that merge swallows are the cleanest
    cuts in the song, and a cut needs only `SNAP_MINIMUM_GAP_SECONDS` of room. Measured on the
    Director's "Harder Faster (Female Cover)" (154.6 s, 262 aligned words): the merged spans
    leave **3** interior gaps of 0.6 s or more, the words leave **7**. Four real rests were
    invisible to snapping purely because of a threshold about a drawn band.

    Spans remain the answer when there are no words — an older manifest, or a measurement
    written by anything but `transcribe_song_words` — and that path is unchanged.

    Whichever source is read, the spans are clamped to the window, sorted and merged on the
    way in, so overlapping, unsorted or inverted input — nothing forbids any of the three on
    the model, and Whisper word times are guaranteed neither monotonic nor disjoint — cannot
    produce a negative-length gap. Stretches under `SNAP_MINIMUM_GAP_SECONDS` are dropped; the
    reasoning is that constant's.
    """
    if song is None:
        return None
    if song.lyric_words:
        # `(word, start, end)`; the text is not read here, only the timing it carries.
        sung = [(float(word[1]), float(word[2])) for word in song.lyric_words]
    elif song.vocal_spans:
        sung = [(float(span_start), float(span_end)) for span_start, span_end in song.vocal_spans]
    else:
        return None
    bounded: list[list[float]] = []
    for span_start, span_end in sorted(sung):
        low, high = max(start, span_start), min(end, span_end)
        if high <= low:
            continue
        if bounded and low <= bounded[-1][1]:
            bounded[-1][1] = max(bounded[-1][1], high)
        else:
            bounded.append([low, high])
    gaps: list[tuple[float, float]] = []
    cursor = start
    for low, high in bounded:
        if low - cursor >= SNAP_MINIMUM_GAP_SECONDS - 1e-9:
            gaps.append((cursor, low))
        cursor = high
    if end - cursor >= SNAP_MINIMUM_GAP_SECONDS - 1e-9:
        gaps.append((cursor, end))
    return gaps


def _gap_snap_target(gap: tuple[float, float], boundary: float) -> float:
    """Where inside one voiceless gap this cut would land.

    **The rule, and it is the one editorial decision in this module.** The target is the
    boundary pulled the *shortest* distance that puts it at least `SNAP_CLEARANCE_SECONDS`
    inside the gap at both ends — a clamp, not a jump. For a gap too narrow to hold the
    clearance twice, the gap's midpoint, which is the most clearance that gap can give.

    Chosen over the two obvious alternatives, both of which were considered:

    * **The gap's start** (the instant the phrase stopped) fixes only half the problem. Shot
      B's head lands in silence, but shot A's tail sits hard against a word ending, at the
      one moment Whisper's timing is least trustworthy — the mouth on A's last frame is still
      closing a syllable. It is the *cut* that has to be voiceless, not one side of it.
    * **The gap's midpoint always** maximises clearance, and for a short rest — `vocal_gaps`
      reports none under `SNAP_MINIMUM_GAP_SECONDS` — that is a fine cut. It fails on the
      long ones: an instrumental outro's midpoint can be ten seconds from the boundary, so a
      cut that could
      have moved 0.3 s into genuine silence would be reported as out of tolerance instead.
      The clamp keeps the midpoint's guarantee — clearance at both ends — while asking for the
      smallest move that earns it, which is what "the nearest point where nobody is singing"
      says.

    The clearance is a hard floor and the tolerance is the tunable, deliberately in that
    order: a Director raising the tolerance is asking for cuts to travel further, never for
    them to land closer to a syllable.
    """
    low, high = gap
    if high - low <= 2 * SNAP_CLEARANCE_SECONDS:
        return (low + high) / 2
    return min(max(boundary, low + SNAP_CLEARANCE_SECONDS), high - SNAP_CLEARANCE_SECONDS)


# ------------------------------------------------------------------------------------------
# Where the cut is when the two shots overlap. **The decision, and the argument for it.**
#
# Under the Director's rulings a seam has two shapes. With no overlap it is one boundary the
# two shots share and the cut is that instant. With an overlap it is a **transition** (R-3):
# authored by dragging the edges across each other, drawn blue, A's *transition out* and B's
# *transition in* describing one blend that "cannot disagree". Two points could plausibly be
# the cut of such a seam, and this module snaps **the overlap's midpoint**.
#
# * **A dissolve has no event at B's start.** At that instant B is at zero opacity and nothing
#   about the picture changes; it is where the blend begins to be computed, not where it is
#   seen. Placing *that* instant in a voiceless gap guarantees silence at a moment the viewer
#   registers nothing — which is a guarantee about arithmetic rather than about the video. The
#   moment the change is registered is where the two images are equally present, and that is
#   the midpoint. Every editor's default agrees: an NLE applied to a cut *centres* the
#   transition on it, because the cut point of a dissolve is its centre.
# * **R-3 says the overlap is one artefact owned by both shots.** A's out sets B's in; the
#   pair is a single blend. Its two edges are the ends of one object, so electing one of them
#   "the cut" privileges half of something symmetric — and it privileges the half that today's
#   `assembly_plan` happens to resolve to, which is a rendering fallback for a transition
#   feature that is not built yet, not a statement about what the Director authored.
# * **It degenerates exactly.** A hard cut is a zero-length overlap and its midpoint is the
#   shared boundary itself, so there is one rule here rather than two, and a plan with no
#   transitions in it is snapped by arithmetic identical to Phase B's.
#
# What is *not* decided here is the length of the blend: both edges travel by the same delta,
# so the transition lands somewhere new and is never resized. Resizing it would be editing
# something the Director authored as a side effect of moving something else.
# ------------------------------------------------------------------------------------------

#: Named so the ruling above has an address the rest of the module can cite.
SEAM_POINT = "the overlap's midpoint"


def _seam_overlaps(windows: Sequence[SnapWindow]) -> list[float]:
    """How long the transition at each seam is, indexed like the plan's edge list.

    ``result[k]`` is the overlap at the seam between window ``k-1`` and window ``k``, so
    ``result[0]`` and ``result[-1]`` are the plan's outer edges and are always 0 — the outer
    edges have no neighbour to blend with, and they are the two numbers that never move.

    A disagreement inside `SNAP_CONTIGUITY_TOLERANCE` is 0: half a frame is where "the same
    boundary written twice" ends, and it is where it ends for `assembly.tiling_refusals` too.
    Past it, the **sign** decides, exactly as it decides there — a positive disagreement is a
    hole with no picture in it and raises, a negative one is an authored overlap and is
    returned as its length.

    Raises for the one other geometry this module has no seam for: a window that ends before
    the one before it does, which is a clip laid *wholly* over another — see `SNAP_NESTED`.
    Ruling that out is also what makes the last window's end the plan's end, which the frame
    grid guarantee leans on.
    """
    overlaps = [0.0] * (len(windows) + 1)
    for index, (previous, current) in enumerate(pairwise(windows)):
        slack = current.start - previous.end
        if slack > SNAP_CONTIGUITY_TOLERANCE:
            raise TimelineError(
                SNAP_HOLE.format(
                    before=previous.label,
                    after=current.label,
                    end=previous.end,
                    start=current.start,
                    length=slack,
                )
            )
        if current.end < previous.end - SNAP_CONTIGUITY_TOLERANCE:
            raise TimelineError(
                SNAP_NESTED.format(
                    before=previous.label,
                    after=current.label,
                    start=previous.start,
                    end=previous.end,
                    inner_start=current.start,
                    inner_end=current.end,
                )
            )
        if slack < -SNAP_CONTIGUITY_TOLERANCE:
            overlaps[index + 1] = -slack
    return overlaps


def window_move_refusal(
    project: Project, shot: Shot, *, rendering: frozenset[str] = frozenset()
) -> str:
    """Why this Shot's window may not be moved automatically, or ``""`` when it may.

    **Deliberately not `app.shot_write_refusal`.** That one gates automated writes to a
    Shot's *prose*, and its `"rendered"` arm is a provenance argument about text: a prompt
    beside a take must go on describing what produced it. A window is not prose. A rendered,
    unapproved shot's window is dragged in the timeline every day of production, and gating
    on render provenance would refuse most of a mid-production plan for a reason that does
    not apply to it. The three protections here are the ones that are actually about windows:

    * **locked** — the Director's own hands-off, `populate`'s first protection term.
    * **approved** — AD-13. The same pair `POPULATE_PROTECTED_REFUSAL` protects
      (`approved_output` or the `approved` status), because approval snapshots the window and
      assembly refuses a shot whose window moved after it.
    * **rendering** — a render already accepted for this exact window. Passed in as a set of
      shot ids rather than read here, because the evidence is the project's job records and
      `app.shot_render_in_flight` is the one function that reads them; a second copy of that
      walk is the guard hole this codebase keeps finding.

    The order is `populate`'s and `shot_write_refusal`'s: a lock is a decision the Director
    made, so when several apply it is the sentence worth reading.
    """
    if shot.locked:
        return SNAP_LOCKED_REFUSAL.format(shot=shot_label(project, shot))
    if shot.approved_output or shot.status == "approved":
        return SNAP_APPROVED_REFUSAL.format(shot=shot_label(project, shot))
    if shot.id in rendering:
        return SNAP_IN_FLIGHT_REFUSAL.format(shot=shot_label(project, shot))
    return ""


@dataclass(frozen=True, slots=True)
class SnapWindow:
    """One window offered to the snapper: where it is, what to call it, whether it may move.

    The whole of what `snap_window_plan` needs to know about a window, and deliberately not a
    `Shot`: the second caller — populate's line-up step — is snapping a tiling that has no
    shots yet, and a core that could only be given Shots would have forced a second
    implementation for it. The two protections a Shot carries arrive as `refusal`, already
    worded, because `window_move_refusal` is the one reader of a Shot's lock, its approval and
    its in-flight render, and a second walk over those is the guard hole this codebase keeps
    finding.

    `id` is the address the plan's windows come back on — a Shot's id for the timeline route,
    a positional name for a layout that has none. `refusal` is `""` when the window may move.
    """

    id: str
    start: float
    duration: float
    label: str
    refusal: str = ""

    @property
    def end(self) -> float:
        return self.start + self.duration


def shot_snap_windows(
    project: Project, *, rendering: frozenset[str] = frozenset()
) -> list[SnapWindow]:
    """A project's timeline as windows the snapping core can be handed, in song order.

    The one place a Shot becomes a `SnapWindow`, so the two routes that snap a *stored*
    timeline — `snap-cuts` and a project-sourced `line-up` — name their shots the same way and
    honour the same protections. Both facts would be trivially easy to write twice and
    trivially easy to get subtly different, which is how the same cut would come back
    protected on one route and movable on the other.
    """
    return [
        SnapWindow(
            id=shot.id,
            start=shot.start,
            duration=shot.duration,
            label=shot_label(project, shot),
            refusal=window_move_refusal(project, shot, rendering=rendering),
        )
        for shot in ordered_shots(project)
    ]


def snap_cut_plan(
    project: Project,
    *,
    tolerance: float = SNAP_TOLERANCE_DEFAULT,
    rendering: frozenset[str] = frozenset(),
    minimum: float = H3_MIN_SHOT_SECONDS,
    maximum: float = H3_MAX_SHOT_SECONDS,
) -> CutSnapPlan:
    """Snap the cuts of a **project's timeline**. Pure, I/O-free, writes nothing.

    One of the two doors onto `snap_window_plan`, and it is the door that knows about Shots:
    it puts them in song order, names each the way the Director sees it, and asks
    `window_move_refusal` which of them are protected. Every decision about where a cut goes
    is the core's — see `snap_window_plan`, including for the band and the contiguity
    guarantee. Populate's line-up step comes through the other door with the same core, so a
    cut cannot land in one place because it was reached from the timeline and another because
    it was reached from a fresh layout.

    Raises `TimelineError` when the plan has a hole in it, because there is no seam in a hole
    — see `SNAP_HOLE` — or when one shot is laid wholly over another (`SNAP_NESTED`). An
    **overlapping** plan is snapped, not refused: an overlap is an authored transition and it
    moves as a unit.

    The Director's own section marks travel with the windows (`section_edges`), so a cut sitting
    on a section boundary is refused here exactly as it is on populate's line-up step. Both
    doors observe the one decision; a boundary that is protected on a fresh layout cannot be
    spent on a stored timeline.
    """
    return snap_window_plan(
        shot_snap_windows(project, rendering=rendering),
        project.song,
        tolerance=tolerance,
        minimum=minimum,
        maximum=maximum,
        sections=section_edges(project.sections),
    )


def snap_window_plan(
    windows: Sequence[SnapWindow],
    song,
    *,
    tolerance: float = SNAP_TOLERANCE_DEFAULT,
    minimum: float = H3_MIN_SHOT_SECONDS,
    maximum: float = H3_MAX_SHOT_SECONDS,
    sections: Mapping[float, str] | None = None,
) -> CutSnapPlan:
    """Propose a new position for every cut in a plan. The one implementation.

    Each cut is offered the nearest voiceless moment within ``tolerance`` seconds
    (`_gap_snap_target` decides "nearest"), and takes it only when both windows it belongs to
    survive every check. Everything else is reported as a skip with its reason.

    **A seam may be an overlap, and then it moves as a unit.** Since R-3 an overlap is how a
    transition is authored, with a length the Director chose; the cut is `SEAM_POINT` and both
    edges travel by the same delta, so the blend lands somewhere new and is never resized.
    Moving the later clip's start alone would silently turn a 2 s dissolve into a 1.5 s one —
    editing something authored as a side effect of moving something else. A hard cut is the
    zero-length case of the same arithmetic and behaves exactly as it did before overlaps were
    admitted here.

    `windows` arrive **in song order** — the caller's ordering is the one used, unchanged —
    and each carries its own `refusal`, so this function decides nothing about protections
    beyond honouring them.

    **`sections` is the fourth protection and it belongs to the plan rather than to a window**,
    which is why it arrives as its own argument: `section_edges`' mapping of every second a
    marked section starts or ends at to that section's label. A cut within half a frame of one
    is skipped with `SNAP_SECTION_BOUNDARY` — see there for the live defect it closes.
    `None` (the default) is a caller with no section layer to honour, and is the behaviour every
    caller had before 2026-08-22.

    **The band.** ``minimum``/``maximum`` default to `H3_MIN_SHOT_SECONDS` and
    `H3_MAX_SHOT_SECONDS` — 4–15 s — which is the band `populate_windows` itself defaults to,
    the band `expansion_input` flags a shot against as `outside_h3_window`, and the band
    `build_director_timeline` warns about. `app.POPULATE_MAX_WINDOW_SECONDS` (6 s) is tighter
    but is an *argument the populate route passes at layout time*, not an invariant on stored
    windows: a Director may deliberately edit a shot to 9 s, and enforcing 6 s here would
    refuse every cut in such a plan for a rule it was never held to. Both are parameters, so
    a caller that does want the tighter ceiling asks for it — and populate's line-up step
    does, because a lay-out that capped its windows at 6 s must not be undone by the step
    after it.

    **Why the band check is exact and not provisional.** Cuts are decided left to right. When
    cut *i* is judged, the left edge of window *i* is a boundary already settled, so window
    *i*'s final length is known here. Window *i+1*'s right edge is still its original one —
    but cut *i+1* will re-judge window *i+1* against its now-settled left edge before moving,
    and will refuse to move if that leaves it out of band. So window *i+1* is in band whether
    cut *i+1* moves (checked there) or stays (checked here), and every window in the result is
    inside the band on both counts.

    **Coverage is structural, and so is the seam geometry.** The plan is carried as an edge
    list plus the overlap length at each seam (`_seam_overlaps`); the returned windows are
    derived from the two, and the two outer edges are copied through from the input untouched.
    There is no arithmetic by which a hole, a resized transition or an accumulated rounding
    drift can appear — only the interior edges are ever assigned, each once, from a seam point
    rounded to the millisecond the rest of this module works in, and the overlap lengths are
    *carried* rather than recomputed, so what comes out has the transitions that went in. The
    plan's first start and last end being untouched is R-7 held by construction: the assembled
    video's length against the song is the number it was. A window no seam of which moved
    keeps its given floats untouched to the bit, which is what stops an ulp of recomputation
    from making an approved shot read as stale to assembly.

    Raises `TimelineError` for the two geometries that have no seam at all: a **hole**
    (`SNAP_HOLE`) and a window laid **wholly over** another (`SNAP_NESTED`). An overlapping
    plan is snapped rather than refused; that is the whole of this change.
    """
    ordered = list(windows)
    unchanged = [(window.id, window.start, window.duration) for window in ordered]
    # Tolerance 0 is the feature switched off, and off means *nothing was examined* — not a
    # loop that happens to find no candidates. Checked before the song is read, before the
    # plan's shape is judged, before anything: a switched-off feature that can still raise is
    # not switched off.
    if not (tolerance > 0):
        return CutSnapPlan("off", 0.0, [], [], unchanged, SNAP_TOLERANCE_OFF)
    if len(ordered) < 2:
        return CutSnapPlan(
            "no_cuts", tolerance, [], [], unchanged,
            SNAP_WITHOUT_CUTS.format(count=len(ordered)),
        )
    overlaps = _seam_overlaps(ordered)
    gaps = vocal_gaps(song, start=ordered[0].start, end=ordered[-1].end)
    if gaps is None:
        return CutSnapPlan("unmeasured", tolerance, [], [], unchanged, SNAP_UNMEASURED)

    # The plan as its **earlier** edges: `boundaries[k]` is where window `k` starts, which for
    # an overlapping seam is the lower of the two edges and the transition's own start. The
    # window before it ends at `boundaries[k] + overlaps[k]`, so one assignment moves both
    # edges of a transition together and its length is carried, never recomputed.
    # `boundaries[0]` and `boundaries[-1]` are the plan's outer edges and are never assigned,
    # so the coverage of the song is bit-identical afterwards.
    boundaries = [window.start for window in ordered] + [ordered[-1].end]
    # Which boundaries were actually assigned. Read once, at the end, to keep every untouched
    # window's given floats rather than recomputing them — see the note there.
    touched: set[int] = set()
    moves: list[CutMove] = []
    skips: list[CutSkip] = []
    for index in range(len(ordered) - 1):
        before, after = ordered[index], ordered[index + 1]
        overlap = overlaps[index + 1]
        # The cut. `SEAM_POINT`: the centre of the transition, which for a hard cut is the
        # shared boundary itself because the transition is zero long.
        half = overlap / 2
        boundary = boundaries[index + 1] + half
        names = {
            "before_id": before.id,
            "after_id": after.id,
            "before_label": before.label,
            "after_label": after.label,
            "boundary": boundary,
        }
        labels = {"before": names["before_label"], "after": names["after_label"]}
        # A cut belongs to two windows, so either window's protection protects it. The first
        # refusal wins, and `window_move_refusal` orders its own three.
        refusal = before.refusal or after.refusal
        if refusal:
            skips.append(CutSkip(**names, reason=refusal))
            continue
        # The plan's own protection, asked immediately after the windows' three and before
        # anything about the track is read: it is structural, it is unconditional, and a cut on
        # a section boundary will not move whatever the gaps say. Matched within
        # `SNAP_CONTIGUITY_TOLERANCE` — this module's "the same boundary written twice" number —
        # because a section box the Director dragged and a window rounded to the millisecond
        # describe the same instant without being the same float.
        edge = next(
            (
                label
                for at, label in (sections or {}).items()
                if abs(at - boundary) <= SNAP_CONTIGUITY_TOLERANCE
            ),
            None,
        )
        if edge is not None:
            skips.append(
                CutSkip(
                    **names,
                    reason=SNAP_SECTION_BOUNDARY.format(
                        **labels, boundary=boundary, section=edge
                    ),
                )
            )
            continue
        # A measured track with no usable gap inside the plan's window — every second of it
        # sung, or every rest under `SNAP_MINIMUM_GAP_SECONDS`. There is nothing to be nearest
        # to, so every cut is out of tolerance by the only honest reading. Answered per cut
        # rather than as a whole-plan status, because the plan *was* examined: the protections
        # above still ran and still name the shots they protected.
        if not gaps:
            skips.append(
                CutSkip(
                    **names,
                    reason=SNAP_NO_GAP_IN_TOLERANCE.format(
                        **labels, boundary=boundary, tolerance=tolerance
                    ),
                )
            )
            continue
        # The *gap*, not just the target, because the report says how long it was — and the
        # nearest gap is by definition the one whose target is nearest.
        gap = min(
            gaps, key=lambda candidate: abs(_gap_snap_target(candidate, boundary) - boundary)
        )
        target = _gap_snap_target(gap, boundary)
        proposed = round(target, 3)
        gap_length = round(gap[1] - gap[0], 3)
        # "Already good" is the clamp being a no-op, rather than a second opinion about what
        # counts as silence: a cut deep inside a gap is its own snap target, so
        # `_gap_snap_target` is the only thing in this module that decides where silence
        # begins. A cut sitting hard against a phrase's edge is therefore *not* already good
        # — it is offered a move of at most `SNAP_CLEARANCE_SECONDS`.
        if abs(proposed - boundary) <= 1e-9:
            skips.append(
                CutSkip(**names, reason=SNAP_ALREADY_SILENT.format(**labels, boundary=boundary))
            )
            continue
        # The epsilon absorbs binary-float noise on a tolerance the Director typed as a
        # decimal; it can admit a move a nanosecond past the bound, which nothing measures.
        if abs(proposed - boundary) > tolerance + 1e-9:
            skips.append(
                CutSkip(
                    **names,
                    reason=SNAP_NO_GAP_IN_TOLERANCE.format(
                        **labels, boundary=boundary, tolerance=tolerance
                    ),
                )
            )
            continue
        # **R-7 before the band.** A transition travels whole, so the seam has room to move
        # only if its far edge does too. `boundaries[0]` and `boundaries[-1]` are the plan's
        # first frame and its last — the two numbers the assembled video's length against the
        # song is made of — and `_seam_overlaps` has already ruled out the geometry in which
        # the last window is not the plan's end. A hard cut cannot reach this: its target came
        # out of `vocal_gaps`, which is clamped to exactly this span.
        if (
            proposed - half < boundaries[0] - 1e-9
            or proposed + half > boundaries[-1] + 1e-9
        ):
            skips.append(
                CutSkip(
                    **names,
                    reason=SNAP_OFF_THE_PLAN.format(
                        **labels,
                        boundary=boundary,
                        proposed=proposed,
                        overlap=overlap,
                        edge=(
                            "back before the plan's first frame"
                            if proposed - half < boundaries[0]
                            else "past the plan's last frame"
                        ),
                    ),
                )
            )
            continue
        # Each neighbour's whole window, transitions included: `left` is where the shot before
        # this seam starts — an edge an earlier seam may already have settled — and `right` is
        # where the shot after it ends, which is past `boundaries[index + 2]` by whatever
        # transition sits at the seam beyond. A window's length is its own window, overlapped
        # or not, because that is the length H3 has to render.
        left, right = boundaries[index], boundaries[index + 2] + overlaps[index + 2]
        # Both neighbours, each named with the length the move would give it and the bound it
        # would break. The nearest gap is the only candidate considered: the ruling is that a
        # move which pushes a neighbour out of the band is rejected *for that cut*, and
        # walking on to a further gap would report a reason about a move nobody proposed.
        out_of_band = None
        for neighbour, length in (
            (names["before_label"], proposed + half - left),
            (names["after_label"], right - (proposed - half)),
        ):
            if length < minimum - 1e-9:
                out_of_band = (neighbour, length, "under", minimum, "minimum")
                break
            if length > maximum + 1e-9:
                out_of_band = (neighbour, length, "over", maximum, "maximum")
                break
        if out_of_band is not None:
            neighbour, length, bound, limit, edge = out_of_band
            skips.append(
                CutSkip(
                    **names,
                    reason=SNAP_OUT_OF_BAND.format(
                        **labels,
                        boundary=boundary,
                        proposed=proposed,
                        neighbour=neighbour,
                        length=length,
                        bound=bound,
                        limit=limit,
                        edge=edge,
                    ),
                )
            )
            continue
        # One assignment, both edges. The transition's start moves to `proposed - half` and
        # its end follows from the length carried in `overlaps`, so the blend is somewhere new
        # and is exactly as long as the Director made it. `half` is 0 for a hard cut, and
        # `proposed - 0.0` is `proposed` to the bit, so that path is Phase B's arithmetic
        # unchanged.
        boundaries[index + 1] = proposed - half
        touched.add(index + 1)
        moves.append(
            CutMove(**names, proposed=proposed, gap=gap_length, overlap=round(overlap, 3))
        )
    # A window neither of whose boundaries moved keeps its **given floats**, not a recomputed
    # difference of them. `start + duration` and `boundaries[k + 1]` are the same real number
    # and can be a bit apart in IEEE-754, so recomputing every window would rewrite untouched
    # shots by an ulp — and `assembly_refusals` compares an approved shot's window snapshot to
    # its live window with **exact** inequality, so an ulp on an untouched approved shot is a
    # plan that stops assembling for a move nobody made. Contiguity survives the mix because
    # an untouched boundary was never assigned: it is still the float both its windows hold.
    settled = [
        (window.id, window.start, window.duration)
        if index not in touched and index + 1 not in touched
        else (
            window.id,
            boundaries[index],
            boundaries[index + 1] + overlaps[index + 1] - boundaries[index],
        )
        for index, window in enumerate(ordered)
    ]
    return CutSnapPlan("ready", tolerance, moves, skips, settled)
