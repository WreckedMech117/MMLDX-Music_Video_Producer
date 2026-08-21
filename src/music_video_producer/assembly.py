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

from dataclasses import dataclass, replace
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
#: Retired as a refusal (2026-08-20, the Director's ruling: "later shots on top of
#: earlier shots"): an overlap is an editing gesture, and `assembly_plan` resolves it by
#: cutting the earlier clip at the later one's start. The constant remains for the tests
#: that assert it is no longer reported.
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
ASSEMBLY_NO_AUDIO_TO_MIX_REFUSAL = (
    "{shot}'s take audio is accepted into the mix, but its take carries no audio stream. "
    "Un-accept it, or re-render the shot."
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
    #: Whether this shot's take audio is accepted into the mix (`Shot.mix_take_audio`),
    #: and whether the take actually carries an audio stream — ``None`` when unprobed or
    #: missing. An acceptance with nothing to accept is a refusal, not a silent skip.
    mix_audio: bool = False
    has_audio: bool | None = None

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
        if clip.mix_audio and clip.has_audio is False:
            refusals.append(ASSEMBLY_NO_AUDIO_TO_MIX_REFUSAL.format(shot=clip.label))
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
        # An overlap is NOT a problem, by the Director's ruling (2026-08-20): "later
        # shots on top of earlier shots" — `assembly_plan` cuts the earlier clip at the
        # later one's start, so an overlapping plan is an editing gesture, not a defect.
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
    # Overlaps resolve as layers, later-on-top — the Director's ruling (2026-08-20):
    # "later shots on top of earlier shots". A clip's visible ranges are its window minus
    # every later-starting clip's window, so an overlaid head is cut, a nested overlay
    # splits the clip around itself and the underneath RESUMES when the overlay ends,
    # with the take offset advanced by exactly the skipped stretch — the same seconds of
    # the take land at the same seconds of the song, before and after. A clip completely
    # covered contributes nothing. Every visible boundary is some clip's own start or
    # end, so the frame telescoping the module docstring proves is untouched.
    resolved: list[ClipWindow] = []
    for index, clip in enumerate(ordered):
        segments = [(clip.start, clip.end)]
        for later in ordered[index + 1:]:
            if later.start >= clip.end:
                break
            remaining: list[tuple[float, float]] = []
            for seg_start, seg_end in segments:
                if later.end <= seg_start or later.start >= seg_end:
                    remaining.append((seg_start, seg_end))
                    continue
                if later.start - seg_start > BOUNDARY_TOLERANCE_SECONDS:
                    remaining.append((seg_start, later.start))
                if seg_end - later.end > BOUNDARY_TOLERANCE_SECONDS:
                    remaining.append((later.end, seg_end))
            segments = remaining
        for seg_start, seg_end in segments:
            if seg_end - seg_start <= BOUNDARY_TOLERANCE_SECONDS:
                continue
            if seg_start == clip.start and seg_end == clip.end:
                resolved.append(clip)
            else:
                resolved.append(
                    replace(
                        clip,
                        start=seg_start,
                        duration=seg_end - seg_start,
                        offset=clip.offset + (seg_start - clip.start),
                    )
                )
    resolved.sort(key=lambda clip: clip.start)
    ordered = resolved
    frames = [clip_frames_on_grid(clip.start, clip.end) for clip in ordered]
    width, height = max(
        (dimensions[clip.shot_id] for clip in ordered),
        key=lambda size: size[0] * size[1],
    )
    return AssemblyPlan(
        clips=ordered, frames=frames, width=width, height=height, song_seconds=song_seconds
    )


# ------------------------------------------------------------------------------------------
# Export presets (Phase 4.2). Two named builds of the same plan — one to review a cut with,
# one to deliver — and nothing else about assembly changes between them: the same clips, the
# same cumulative grid, the same trim rule, the same verification.
# ------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExportPreset:
    """One build's encoder settings. Everything a preset can move lives here and nowhere
    else, so "what does `master` actually do differently" is one object rather than a grep."""

    name: str
    #: libx264's speed/efficiency dial, applied at the *trim* stage — which is where the
    #: only video encode happens, because the join stream-copies (`-c:v copy`).
    x264_preset: str
    crf: str
    #: Filters appended to the export's audio, in order. Empty for `draft`, which is what
    #: keeps its join argv byte-identical to the one this application has always built.
    audio_filters: tuple[str, ...] = ()


#: `draft` is **exactly what this application has shipped since FR-22** — veryfast/CRF 18 on
#: the trims, no audio filter on the join — and it is the default for precisely that reason:
#: an "Assemble" click that names no preset must keep producing the byte-identical file it
#: produced yesterday. It is also already cheap (veryfast is the second-fastest x264 preset),
#: so "draft is at least as fast as today's export" holds by identity rather than by promise.
DRAFT_PRESET = ExportPreset(name="draft", x264_preset="veryfast", crf="18")

#: **`master` normalizes no loudness, by the Director's ruling of 2026-08-20.** It used to
#: carry `loudnorm=I=-16:TP=-1.5:LRA=11`, the broadcast/podcast convention Calliope ships —
#: and that is the wrong instrument here. The export's audio track *is* the Director's master
#: song: either supplied already mastered or produced by MiniMax Music 3. Re-normalizing it
#: re-masters someone's finished work, and -16 LUFS audibly pulls a modern music master down
#: — measured on this machine, a -8.50 LUFS source came out of the old master preset at
#: -16.20 LUFS, a 7.7 LU haircut nobody asked for. The song's own levels are the delivery.
#:
#: The sample-rate conform stays, on its own merits rather than by inheritance. Its original
#: reason is gone: `loudnorm` ran internally at 192 kHz and handed that on, AAC caps at
#: 96 kHz, and a 44.1 kHz song therefore *used* to deliver at 96 kHz — reproduced, and it no
#: longer happens (44.1 kHz in, 44.1 kHz out, with no filter at all). What remains is the
#: source this application actually takes: a Director-supplied master can be any rate, and a
#: 192 kHz one still lands at 96 kHz through AAC's ceiling — also reproduced. This makes the
#: delivery rate deterministic at the one rate video delivery expects, and it is level-
#: transparent, which is the property the ruling cares about: a clean tone measured -27.15
#: LUFS before and -27.15 LUFS after. Sample-rate conversion is not mastering.
MASTER_SAMPLE_RATE = "aresample=48000"

#: `master` is the delivery build: a slower x264 preset and a lower CRF for the picture (both
#: land at the trim stage, the only encode), `-movflags +faststart` — which the join has
#: always carried, for both presets — and the 48 kHz conform above on the audio. Nothing here
#: touches the song's level.
MASTER_PRESET = ExportPreset(
    name="master",
    x264_preset="slow",
    crf="16",
    audio_filters=(MASTER_SAMPLE_RATE,),
)

#: Name → preset. The route's `Literal` is asserted against these keys, so a preset added
#: here and not offered on the wire fails loudly rather than becoming unreachable.
EXPORT_PRESETS: dict[str, ExportPreset] = {
    preset.name: preset for preset in (DRAFT_PRESET, MASTER_PRESET)
}
DEFAULT_EXPORT_PRESET = DRAFT_PRESET.name


# ------------------------------------------------------------------------------------------
# ffmpeg argv construction — pure, and pinned by tests, because a flag drifting here is a
# render defect that no Python test would otherwise see.
# ------------------------------------------------------------------------------------------


def trim_args(
    source: Path,
    dest: Path,
    frames: int,
    width: int,
    height: int,
    offset: float = 0.0,
    preset: ExportPreset = DRAFT_PRESET,
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

    The preset moves two literals and nothing else — the x264 speed dial and the CRF. This
    is where the export's only video encode happens (the join copies), so it is where a
    delivery build is decided. Omitted, it is `draft`, whose two values are the two this
    function has always written.
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
        preset.x264_preset,
        "-crf",
        preset.crf,
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


@dataclass(slots=True)
class AudioOverlay:
    """One accepted take's contribution to the mix: which file, which slice, and where.

    The slice is the *same* one the picture was cut to — the effective offset in, the
    window's grid seconds long — and `delay_seconds` is the clip's cumulative timeline
    position, so the accepted sound sits exactly under its own picture.
    """

    source: Path
    offset_seconds: float
    window_seconds: float
    delay_seconds: float


def concat_args(
    list_file: Path,
    song: Path,
    dest: Path,
    overlays: list[AudioOverlay] | None = None,
    preset: ExportPreset = DRAFT_PRESET,
) -> list[str]:
    """All intermediates → the export, master song plus any *accepted* take audio.

    `-c:v copy` because every intermediate came out of `trim_args` with identical encode
    parameters — the join costs no second generation loss. The song is re-encoded to AAC
    (sources are mp3 or flac depending on origin; mp4 wants neither). `-shortest` guards
    the rounding edge: the video is the verified length, and a song file a hair longer
    than its stored duration must not stretch the container past it.

    With no overlays this builds the identical argv it always has — the Director's
    default is "only the main music track comes through", and a pinned test holds the
    bytes. Each overlay becomes one extra input plus `atrim → adelay` into a single
    `amix` whose **first** input is the song and whose `normalize=0` keeps the master's
    level untouched — mixing under the song must never duck the song
    (spec-take-audio-mix, the Director's own wording). `duration=first` ends the mix at
    the song, exactly as `-shortest` already ends the container.

    The preset's audio filters ride the *end* of whichever audio path this builds — `-af`
    when the song is the only source, the tail of the mix graph when it is not — so what
    they see is the finished programme and not one contributor to it. A preset with no
    audio filters writes no `-af` and no trailing separator on the graph, which is what
    keeps `draft` byte-identical to the argv it always built; `master` carries only the
    48 kHz delivery conform, and **neither preset touches the song's loudness**.
    """
    audio_chain = ",".join(preset.audio_filters)
    if not overlays:
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
            *(["-af", audio_chain] if audio_chain else []),
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
    inputs: list[str] = []
    chains: list[str] = []
    labels: list[str] = []
    for index, overlay in enumerate(overlays):
        inputs.extend(["-i", overlay.source.as_posix()])
        delay_ms = round(overlay.delay_seconds * 1000)
        chains.append(
            f"[{2 + index}:a]"
            f"atrim=start={overlay.offset_seconds}"
            f":end={overlay.offset_seconds + overlay.window_seconds},"
            f"asetpts=PTS-STARTPTS,"
            f"adelay={delay_ms}:all=1"
            f"[take{index}]"
        )
        labels.append(f"[take{index}]")
    graph = (
        ";".join(chains)
        + f";[1:a]{''.join(labels)}amix=inputs={len(overlays) + 1}"
        + ":duration=first:normalize=0"
        + (f",{audio_chain}" if audio_chain else "")
        + "[mix]"
    )
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
        *inputs,
        "-filter_complex",
        graph,
        "-map",
        "0:v:0",
        "-map",
        "[mix]",
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


# ------------------------------------------------------------------------------------------
# Progress. ffmpeg will report its own clock if asked; the alternative is a Director staring
# at a held-open request with nothing to read. Pure here, executed by the route.
# ------------------------------------------------------------------------------------------


def with_progress(args: list[str]) -> list[str]:
    """The same ffmpeg command, told to report — and only that.

    `-progress pipe:1` opens a stdout channel of `key=value` lines; `-nostats` closes the
    interactive stderr line that would otherwise carry the same information a second time
    and, being unterminated, would not survive line reading anyway. Both are *global*
    options, so they go in front of the inputs, and **neither writes a byte to the output
    file** — which is what lets the pinned argv above stay the description of what this
    application encodes while the route still runs the reporting form.
    """
    return [args[0], "-progress", "pipe:1", "-nostats", *args[1:]]


def parse_progress_us(line: str) -> int | None:
    """One `-progress` line → elapsed output microseconds, or ``None`` for anything else.

    ffmpeg writes a block of `key=value` lines per interval, of which exactly one is the
    clock. Every other key, the `N/A` this one carries before the first frame lands, a
    blank line, and a fragment a pipe handed over mid-word are all *not a number this can
    use*, and are ignored rather than raised: a progress reader that can kill an export is
    worse than an export with no progress reader.
    """
    key, separator, value = line.strip().partition("=")
    if not separator or key != "out_time_us":
        return None
    try:
        microseconds = int(value)
    except ValueError:
        return None
    return microseconds if microseconds >= 0 else None


#: How the bar is split between assembly's two ffmpeg stages. The trims re-encode every frame
#: of the timeline; the join stream-copies them (`-c:v copy`) and re-encodes only audio, which
#: on the synthetic clips this is tested against runs two orders of magnitude faster. 90/10 is
#: therefore roughly where the wall clock goes. It is a reporting weight and nothing else — no
#: output, no verification and no refusal reads it.
TRIM_SHARE = 0.9


@dataclass(slots=True)
class ExportProgress:
    """Percent complete across a whole assembly, fed from ffmpeg's own clock.

    Monotonic by construction, because the stages are not: each trim restarts the clock
    against its own clip, so a raw reading walks backwards several times per export. Every
    reading can only raise the number here, and 100 is a ceiling rather than an arithmetic
    outcome — a song measured a hair shorter than the frames laid against it must not report
    101 %, which `RenderJob.progress` would refuse outright. `total_seconds` is the export's
    known duration; a plan with no length reports 0 rather than dividing by it.
    """

    total_seconds: float
    percent: int = 0

    def trim(self, elapsed_seconds: float, out_time_us: int) -> int:
        """A trim's clock, placed on the timeline: the finished clips before it, plus how
        far into this one ffmpeg has read."""
        done = elapsed_seconds + max(0.0, out_time_us / 1_000_000)
        return self._advance(TRIM_SHARE * self._fraction(done) * 100)

    def join(self, out_time_us: int) -> int:
        """The join's clock, which already runs against the whole export."""
        fraction = self._fraction(max(0.0, out_time_us / 1_000_000))
        return self._advance((TRIM_SHARE + (1 - TRIM_SHARE) * fraction) * 100)

    def _fraction(self, seconds: float) -> float:
        """A stage's own clock as a share of the export, capped at its whole.

        The cap is the *only* thing bounding the reported percent, which is why it lives
        here rather than being repeated at the write: the two stage weights sum to one, so
        a fraction that cannot exceed 1 is a percent that cannot exceed 100. It is also
        what stops an over-running trim — a clip whose take reads a hair long — from
        jumping the bar to 100 while the join has not started.
        """
        if self.total_seconds <= 0:
            return 0.0
        return min(1.0, seconds / self.total_seconds)

    def _advance(self, value: float) -> int:
        """Monotonicity, and nothing else — see `_fraction` for the ceiling."""
        self.percent = max(self.percent, int(value))
        return self.percent


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
