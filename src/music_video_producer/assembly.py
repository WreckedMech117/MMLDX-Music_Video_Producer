"""Assembly: every approved take, trimmed to its window, joined to the master song.

AD-9's implementation. Everything in this module is decidable without a process: which
shots block assembly and why (all of them, in one report), how many frames each clip
contributes (a cumulative grid, so per-clip rounding cannot accumulate), and the exact
argv ffmpeg runs. The route in `app.py` owns resolution (manifest → absolute paths) and
execution (`asyncio.create_subprocess_exec`); tests drive this half with plain data and
the runner with a fake exec, and one integration test runs the real binary.

Two design facts worth stating once:

* **The trim is not optional.** Grid alignment makes every rendered clip longer than the
  window that requested it — a 4.0 s shot renders 4.458 s on H3's 17k+5 grid — and joining
  untrimmed clips drifts ~11 %, twenty seconds over a three-minute song.
* **Frame counts come from one cumulative grid, not per-clip rounding.** Clip *i* gets
  `round(start_{i+1}·24) − round(start_i·24)` frames (the last clip closes on its own end),
  so the total telescopes to `round(end·24)` exactly, however the individual windows round.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: Every adapter this application ships renders 24 fps — both H3 canonical exports, the
#: keyframe export and the LTX evidence agree — so assembly normalizes to it rather than
#: probing per take: a take at any other rate is conformed, not refused.
ASSEMBLY_FPS = 24

#: Adjacent windows must touch within half a frame. Windows are floats the Director edits;
#: half a frame is where "the same boundary, written twice" ends and a real gap begins.
BOUNDARY_TOLERANCE_SECONDS = 1 / (2 * ASSEMBLY_FPS)

#: FR-22's own bound, applied to the plan's coverage of the song and to the verified
#: export: within one frame.
COVERAGE_TOLERANCE_SECONDS = 1 / ASSEMBLY_FPS

# ------------------------------------------------------------------------------------------
# Refusal wordings. Every sentence names its shots by label, and the report carries every
# blocking reason at once — a Director fixing a 15-shot plan one refusal at a time is a
# Director being rationed.
# ------------------------------------------------------------------------------------------

ASSEMBLY_NO_SHOTS_REFUSAL = (
    "There are no shots to assemble. Assembly joins every approved take in shot order, "
    "and this project's plan is empty."
)
ASSEMBLY_UNAPPROVED_REFUSAL = (
    "{shot} has no approved take. Approve the take that belongs in the video, then assemble."
)
ASSEMBLY_LEGACY_APPROVAL_REFUSAL = (
    "{shot} was approved before window snapshots existed, so assembly cannot tell whether "
    "its window has moved since. Un-approve and re-approve the take to record its window."
)
ASSEMBLY_STALE_REFUSAL = (
    "{shot}'s window moved after its take was approved: approved at {approved_start:.3f}s "
    "for {approved_duration:.3f}s, now {start:.3f}s for {duration:.3f}s. The approved take "
    "was rendered for the old window — re-approve a take for the new one, or restore the "
    "window."
)
ASSEMBLY_TAKE_MISSING_REFUSAL = (
    "{shot}'s approved take is not on disk: {path}. Restore the file, or re-render and "
    "re-approve."
)
ASSEMBLY_GAP_REFUSAL = (
    "The song is uncovered from {start:.3f}s to {end:.3f}s, between {before} and {after}."
)
ASSEMBLY_OVERLAP_REFUSAL = "{before} and {after} overlap from {start:.3f}s to {end:.3f}s."
ASSEMBLY_OVERRUN_REFUSAL = (
    "The plan runs past the song: {shot} ends at {end:.3f}s but the song ends at {song:.3f}s."
)
ASSEMBLY_TOO_SHORT_REFUSAL = (
    "{shot}'s window is shorter than one frame at {fps} fps and would contribute nothing."
)
ASSEMBLY_OFFSET_NEGATIVE_REFUSAL = (
    "{shot}'s cut sits {behind:.3f}s before its take begins — the trim nudge reaches "
    "further back than the take's recorded lead. Ease the nudge forward."
)
ASSEMBLY_OFFSET_OVERRUN_REFUSAL = (
    "{shot}'s cut runs off the end of its take: the take holds {take:.3f}s, but "
    "{offset:.3f}s of trim offset plus the {duration:.3f}s window needs {needed:.3f}s. "
    "Ease the trim nudge back, or re-render for a longer take."
)

#: The two ends of the timeline, named as what they are in gap sentences.
SONG_START_LABEL = "the start of the song"
SONG_END_LABEL = "the end of the song"

# ------------------------------------------------------------------------------------------
# Verification wordings — what a written export must prove before it is presented.
# ------------------------------------------------------------------------------------------

EXPORT_DURATION_PROBLEM = (
    "The export runs {measured:.3f}s but the song runs {song:.3f}s — off by more than one "
    "frame, so the video is not synchronized to its song."
)
EXPORT_STREAMS_PROBLEM = (
    "The export carries {streams} — an assembled video must carry exactly one video and "
    "one audio stream."
)


@dataclass(slots=True)
class ClipWindow:
    """One shot's contribution to the plan, as data: identity, window, snapshot, take."""

    shot_id: str
    label: str
    start: float
    duration: float
    approved_output: str
    approved_start: float
    approved_duration: float
    #: The route's resolution of `approved_output` — an absolute path when the file exists
    #: inside ComfyUI's output root, ``None`` when it is missing or escapes. Resolution is
    #: the route's job; this module only judges the result.
    source: Path | None
    #: Where in the take the window's first frame sits: the recorded sync lead plus the
    #: Director's trim nudge, resolved by the route from the Shot's own fields. 0 for
    #: every take rendered before the over-render margin existed.
    offset: float = 0.0
    #: The take's measured duration in seconds (ffprobe's reading), or ``None`` when the
    #: file is missing — the overflow refusal below is decidable only when it is known.
    take_seconds: float | None = None

    @property
    def end(self) -> float:
        return self.start + self.duration


def assembly_refusals(clips: list[ClipWindow], song_seconds: float) -> list[str]:
    """Every reason this plan cannot assemble, one sentence each, all at once.

    Per-shot problems first (unapproved, legacy, stale, missing file), in timeline order,
    then the tiling problems (gaps, overlaps, overrun) — so the report reads top to bottom
    the way the timeline does. An empty list is the one green light.
    """
    if not clips:
        return [ASSEMBLY_NO_SHOTS_REFUSAL]
    ordered = sorted(clips, key=lambda clip: clip.start)
    refusals: list[str] = []
    for clip in ordered:
        if not clip.approved_output:
            refusals.append(ASSEMBLY_UNAPPROVED_REFUSAL.format(shot=clip.label))
            continue
        if clip.approved_duration == 0:
            # `duration` is gt=0 on the model, so a zero snapshot is unrepresentable as a
            # real window: this approval predates snapshots, and staleness is undecidable.
            refusals.append(ASSEMBLY_LEGACY_APPROVAL_REFUSAL.format(shot=clip.label))
        elif clip.approved_start != clip.start or clip.approved_duration != clip.duration:
            # Exact inequality, deliberately: the snapshot is a *copy* of the window, so
            # unchanged means bit-identical, and any edit changed the plan after the
            # editorial decision.
            refusals.append(
                ASSEMBLY_STALE_REFUSAL.format(
                    shot=clip.label,
                    approved_start=clip.approved_start,
                    approved_duration=clip.approved_duration,
                    start=clip.start,
                    duration=clip.duration,
                )
            )
        if clip.approved_output and clip.source is None:
            refusals.append(
                ASSEMBLY_TAKE_MISSING_REFUSAL.format(
                    shot=clip.label, path=clip.approved_output
                )
            )
        if clip_frames_on_grid(clip.start, clip.end) < 1:
            refusals.append(
                ASSEMBLY_TOO_SHORT_REFUSAL.format(shot=clip.label, fps=ASSEMBLY_FPS)
            )
        # The over-render offset, judged against the take the manifest actually holds. A
        # negative offset is a nudge pulled past the recorded lead; an overrun is a cut
        # that needs more take than the file measures. Both name every number the fix
        # needs, and both are decidable only here — the client clamps, but the manifest
        # is writable by clients that do not.
        if clip.offset < 0:
            refusals.append(
                ASSEMBLY_OFFSET_NEGATIVE_REFUSAL.format(
                    shot=clip.label, behind=-clip.offset
                )
            )
        elif (
            clip.take_seconds is not None
            and clip.offset + clip.duration
            > clip.take_seconds + BOUNDARY_TOLERANCE_SECONDS
        ):
            refusals.append(
                ASSEMBLY_OFFSET_OVERRUN_REFUSAL.format(
                    shot=clip.label,
                    take=clip.take_seconds,
                    offset=clip.offset,
                    duration=clip.duration,
                    needed=clip.offset + clip.duration,
                )
            )
    refusals.extend(tiling_refusals(ordered, song_seconds))
    return refusals


def tiling_refusals(ordered: list[ClipWindow], song_seconds: float) -> list[str]:
    """Gaps and overlaps against the song, each range named with its neighbours.

    The windows are plan facts independent of approval, so this reports on the plan as it
    stands — a Director sees the hole *and* the unapproved shot in one answer rather than
    peeling them in sequence. `ordered` must be sorted by start.
    """
    problems: list[str] = []
    cursor = 0.0
    cursor_label = SONG_START_LABEL
    for clip in ordered:
        if clip.start - cursor > BOUNDARY_TOLERANCE_SECONDS:
            problems.append(
                ASSEMBLY_GAP_REFUSAL.format(
                    start=cursor, end=clip.start, before=cursor_label, after=clip.label
                )
            )
        elif cursor - clip.start > BOUNDARY_TOLERANCE_SECONDS:
            problems.append(
                ASSEMBLY_OVERLAP_REFUSAL.format(
                    before=cursor_label, after=clip.label, start=clip.start, end=cursor
                )
            )
        cursor = max(cursor, clip.end)
        cursor_label = clip.label
    if song_seconds - cursor > COVERAGE_TOLERANCE_SECONDS:
        problems.append(
            ASSEMBLY_GAP_REFUSAL.format(
                start=cursor, end=song_seconds, before=cursor_label, after=SONG_END_LABEL
            )
        )
    elif cursor - song_seconds > COVERAGE_TOLERANCE_SECONDS:
        problems.append(
            ASSEMBLY_OVERRUN_REFUSAL.format(
                shot=cursor_label, end=cursor, song=song_seconds
            )
        )
    return problems


def clip_frames_on_grid(start: float, end: float) -> int:
    """Frames between two timeline positions on the 24 fps grid.

    Both ends round to grid positions independently, which is what lets consecutive clips
    telescope: clip *i* closing at `round(end_i·24)` and clip *i+1* opening at
    `round(start_{i+1}·24)` are the same number whenever the boundary passed the
    contiguity check, so the sum over a valid plan is exactly `round(plan_end·24)`.
    """
    return round(end * ASSEMBLY_FPS) - round(start * ASSEMBLY_FPS)


@dataclass(slots=True)
class AssemblyPlan:
    """What will actually run: sources, per-clip frame counts, and the normalized geometry."""

    clips: list[ClipWindow]
    frames: list[int]
    width: int
    height: int
    song_seconds: float

    @property
    def total_frames(self) -> int:
        return sum(self.frames)


def assembly_plan(
    clips: list[ClipWindow], song_seconds: float, dimensions: dict[str, tuple[int, int]]
) -> AssemblyPlan:
    """The plan for a set of clips that passed `assembly_refusals`.

    `dimensions` maps shot id → the take's probed (width, height). The normalization
    target is the largest-area take present: mid-iteration plans legitimately mix draft
    and master takes, and scaling everything *up* to the best take present degrades no
    clip that is already there. Aspect is preserved and padded, never stretched — the
    house resolutions differ in aspect (640×384 is 5:3, 1056×608 is ~1.74:1), and a 5 %
    silent stretch is a defect with a face in it.
    """
    ordered = sorted(clips, key=lambda clip: clip.start)
    frames = [clip_frames_on_grid(clip.start, clip.end) for clip in ordered]
    width, height = max(
        (dimensions[clip.shot_id] for clip in ordered),
        key=lambda size: size[0] * size[1],
    )
    return AssemblyPlan(
        clips=ordered, frames=frames, width=width, height=height, song_seconds=song_seconds
    )


# ------------------------------------------------------------------------------------------
# ffmpeg argv construction — pure, and pinned by tests, because a flag drifting here is a
# render defect that no Python test would otherwise see.
# ------------------------------------------------------------------------------------------


def trim_args(
    source: Path, dest: Path, frames: int, width: int, height: int, offset: float = 0.0
) -> list[str]:
    """One take → one normalized intermediate: exact frame count, target geometry, no audio.

    Re-encode, deliberately: stream-copy trims cut on keyframes only, and frame-accurate
    cuts are the whole point (see the module docstring's drift numbers). The cut starts
    `offset` seconds into the take — the over-render lead plus the Director's nudge,
    expressed as a frame-exact `trim=start_frame=` so no seek heuristic decides which
    frame is first — and `-frames:v` closes it: takes run *longer* than their windows by
    the margin, and the window is the slice from the offset. The filter chain scales
    aspect-preserved, pads to center, conforms the rate and pins yuv420p so every
    intermediate is concat-identical.
    """
    skip = round(offset * ASSEMBLY_FPS)
    stages = (
        [f"trim=start_frame={skip}", "setpts=PTS-STARTPTS"] if skip > 0 else []
    ) + [
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
        f"fps={ASSEMBLY_FPS}",
        "setsar=1",
        "format=yuv420p",
    ]
    filters = ",".join(stages)
    return [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        source.as_posix(),
        "-vf",
        filters,
        "-frames:v",
        str(frames),
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        dest.as_posix(),
    ]


def concat_manifest(intermediates: list[Path]) -> str:
    """The concat demuxer's list file. Single-quoted with quote-escaping, posix separators —
    the demuxer's own quoting rule, and backslashes double through its parser on Windows."""
    lines = []
    for path in intermediates:
        quoted = path.as_posix().replace("'", "'\\''")
        lines.append(f"file '{quoted}'")
    return "\n".join(lines) + "\n"


def concat_args(list_file: Path, song: Path, dest: Path) -> list[str]:
    """All intermediates → the export, with the master song as the sole audio track.

    `-c:v copy` because every intermediate came out of `trim_args` with identical encode
    parameters — the join costs no second generation loss. The song is re-encoded to AAC
    (sources are mp3 or flac depending on origin; mp4 wants neither). `-shortest` guards
    the rounding edge: the video is the verified length, and a song file a hair longer
    than its stored duration must not stretch the container past it.
    """
    return [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        list_file.as_posix(),
        "-i",
        song.as_posix(),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        "-movflags",
        "+faststart",
        dest.as_posix(),
    ]


def probe_duration_args(path: Path) -> list[str]:
    return [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "csv=p=0",
        path.as_posix(),
    ]


def probe_take_args(path: Path) -> list[str]:
    """One probe per take: geometry and length together, because both gate the plan —
    dimensions pick the normalization target and the duration decides whether the offset
    cut fits. csv output: one `width,height` line, then one `duration` line."""
    return [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height:format=duration",
        "-of",
        "csv=p=0",
        path.as_posix(),
    ]


def probe_streams_args(path: Path) -> list[str]:
    return [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "csv=p=0",
        path.as_posix(),
    ]


def verification_problems(
    song_seconds: float, export_seconds: float, stream_types: list[str]
) -> list[str]:
    """FR-22's last consequence: the written file is checked, and failure is reported.

    Duration within one frame of the song, exactly one video and one audio stream. The
    sentence carries the measured numbers — a failed verification whose message is "failed"
    would send the Director to ffprobe the file themselves, which is this function's job.
    """
    problems: list[str] = []
    if abs(export_seconds - song_seconds) > COVERAGE_TOLERANCE_SECONDS:
        problems.append(
            EXPORT_DURATION_PROBLEM.format(measured=export_seconds, song=song_seconds)
        )
    if sorted(stream_types) != ["audio", "video"]:
        described = ", ".join(sorted(stream_types)) if stream_types else "no streams"
        problems.append(EXPORT_STREAMS_PROBLEM.format(streams=described))
    return problems
