from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from .models import (
    ASSET_ROLE_LABELS,
    SHOT_MODE_SPECS,
    Asset,
    Project,
    Shot,
    citations_in_prompt_order,
    resolve_shot_mode,
    shot_label,
    song_audio_tag,
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


def over_render_frames(duration: float) -> int:
    """The frames a shot of ``duration`` seconds is actually rendered for.

    ``duration + OVER_RENDER_SECONDS``, snapped up to the 17k+5 grid — so the take is
    always at least half a second longer than the window that will consume it, and never
    exactly its length again. Every consumer of the extra length (the Monitor, the trim
    nudge, assembly's offset) exists because this margin does.
    """
    return align_h3_frames(max(5, round((duration + OVER_RENDER_SECONDS) * H3_FPS)))


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
    wrote for the second quarter, whether the repair split, merged, or kept its windows."""
    if proposal_count <= 0:
        raise TimelineError("No proposals to draw prompts from")
    if song_duration <= 0:
        return 0
    index = int(position / song_duration * proposal_count)
    return min(max(index, 0), proposal_count - 1)


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
    """
    extra = max(0.0, picture_seconds - duration)
    lead = min(OVER_RENDER_LEAD_SECONDS, extra, start)
    if song_duration > 0:
        overflow = (start - lead + picture_seconds) - song_duration
        if overflow > 0:
            lead = min(lead + overflow, extra, start)
    return lead


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


#: The lyric sheet's block opener: `[Verse]`, `[Chorus 2]`, `[Pre-Chorus]`...
_SHEET_TAG = re.compile(r"^[ \t]*\[([^\]\r\n]+)\][ \t]*$", re.MULTILINE)


def lyric_blocks(lyrics: str) -> list[tuple[str, str]]:
    """The sheet's own structure: ``(tag, block text)`` in order of appearance."""
    if not lyrics.strip():
        return []
    blocks: list[tuple[str, str]] = []
    matches = list(_SHEET_TAG.finditer(lyrics))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(lyrics)
        text = lyrics[match.end():end].strip()
        if text:
            blocks.append((match.group(1).strip(), text))
    return blocks


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


def _asset_description(asset: Asset) -> str:
    """What this Asset depicts, in as few characters as the honest answer takes.

    The vision inspection wins over the generation prompt when there is one, because it describes
    what the picture *is* rather than what was asked for — a Flux prompt and its output disagree
    often enough that citing the prompt would tell the assistant about a shot that was never made.
    An uploaded asset with neither has no description at all, and gets none rather than an invented
    one: its name and kind are what is actually known about it.
    """
    described = (asset.vision.summary if asset.vision else "") or asset.prompt
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
    # numbers; naming each tag alongside its role removes the guess entirely. Pictures are the
    # only kind a Shot can cite today, so every role numbers into the Picture series.
    #
    # Numbered by `citations_in_prompt_order` — the same walk the reference render numbers its
    # own tags and appends its media by. That sharing is load-bearing, not tidy: the specialist
    # writes "<Picture 1>" into a prompt whose payload fills its anonymous slots in the render's
    # walk, so a tag numbered here under any *other* order would declare a role for somebody
    # else's picture, and the take would render plausibly and wrongly.
    references: list[dict[str, Any]] = []
    if shot.citations:
        ordered_citations = citations_in_prompt_order(shot)
        references.extend(
            {
                "tag": f"<Picture {position}>",
                "role": ASSET_ROLE_LABELS.get(citation.role, citation.role),
                "asset_id": citation.asset_id,
            }
            for position, citation in enumerate(ordered_citations, start=1)
        )
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
