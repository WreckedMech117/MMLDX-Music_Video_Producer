"""The pure half of assembly: refusals, the frame grid, argv construction, verification.

Everything here runs without a process. The integration test that runs the real ffmpeg
lives with the route tests — this file is what makes the refusal report and the grid math
mutation-testable at the speed a mutation pass needs.
"""

import itertools
from pathlib import Path

from music_video_producer.assembly import (
    ASSEMBLY_FPS,
    ASSEMBLY_GAP_REFUSAL,
    ASSEMBLY_LEGACY_APPROVAL_REFUSAL,
    ASSEMBLY_NO_AUDIO_TO_MIX_REFUSAL,
    ASSEMBLY_NO_SHOTS_REFUSAL,
    ASSEMBLY_OFFSET_NEGATIVE_REFUSAL,
    ASSEMBLY_OFFSET_OVERRUN_REFUSAL,
    ASSEMBLY_OVERRUN_REFUSAL,
    ASSEMBLY_STALE_REFUSAL,
    ASSEMBLY_TAKE_MISSING_REFUSAL,
    ASSEMBLY_TOO_SHORT_REFUSAL,
    ASSEMBLY_UNAPPROVED_REFUSAL,
    DEFAULT_EXPORT_PRESET,
    DRAFT_PRESET,
    EXPORT_DURATION_PROBLEM,
    EXPORT_PRESETS,
    EXPORT_STREAMS_PROBLEM,
    MASTER_PRESET,
    MASTER_SAMPLE_RATE,
    SONG_END_LABEL,
    SONG_START_LABEL,
    TRIM_SHARE,
    AudioOverlay,
    ClipWindow,
    ExportPreset,
    ExportProgress,
    assembly_plan,
    assembly_refusals,
    clip_frames_on_grid,
    concat_args,
    concat_manifest,
    parse_progress_us,
    probe_duration_args,
    probe_streams_args,
    probe_take_args,
    trim_args,
    verification_problems,
    with_progress,
)


def clip(
    shot_id: str,
    start: float,
    duration: float,
    *,
    approved: bool = True,
    approved_start: float | None = None,
    approved_duration: float | None = None,
    source: str | None = "resolved.mp4",
) -> ClipWindow:
    """An approved, snapshot-matching, on-disk clip unless a fault is asked for."""
    return ClipWindow(
        shot_id=shot_id,
        label=f"SHOT ({shot_id})",
        start=start,
        duration=duration,
        approved_output=f"shots/{shot_id}.mp4" if approved else "",
        approved_start=start if approved_start is None else approved_start,
        approved_duration=(
            (duration if approved else 0)
            if approved_duration is None
            else approved_duration
        ),
        source=Path(source) if (source and approved) else None,
    )


def test_a_clean_tiling_plan_has_no_refusals():
    clips = [clip("a", 0, 3.75), clip("b", 3.75, 4.25), clip("c", 8.0, 2.0)]
    assert assembly_refusals(clips, song_seconds=10.0) == []


def test_an_empty_plan_is_refused_as_such():
    assert assembly_refusals([], song_seconds=10.0) == [ASSEMBLY_NO_SHOTS_REFUSAL]


def test_every_blocking_shot_is_named_in_one_report():
    """The report is comprehensive, not first-fault: a Director fixing a plan one refusal
    at a time is a Director being rationed. Two unapproved shots and a hole all land in the
    same answer, per-shot problems first in timeline order, tiling after."""
    clips = [
        clip("b", 5.0, 5.0, approved=False),
        clip("a", 0, 4.0, approved=False),
    ]
    report = assembly_refusals(clips, song_seconds=10.0)
    assert report == [
        ASSEMBLY_UNAPPROVED_REFUSAL.format(shot="SHOT (a)"),
        ASSEMBLY_UNAPPROVED_REFUSAL.format(shot="SHOT (b)"),
        ASSEMBLY_GAP_REFUSAL.format(
            start=4.0, end=5.0, before="SHOT (a)", after="SHOT (b)"
        ),
    ]


def test_a_stale_window_is_refused_with_both_windows_in_the_sentence():
    """AD-13's refusal: the snapshot is a copy, so any inequality — start or duration,
    however small — is a plan changed after the editorial decision."""
    moved_start = clip("a", 0.5, 9.5, approved_start=0.0, approved_duration=9.5)
    report = assembly_refusals([moved_start], song_seconds=10.0)
    assert (
        ASSEMBLY_STALE_REFUSAL.format(
            shot="SHOT (a)",
            approved_start=0.0,
            approved_duration=9.5,
            start=0.5,
            duration=9.5,
        )
        in report
    )

    grown = clip("a", 0, 10.0, approved_duration=9.75)
    assert any("moved after its take was approved" in line for line in
               assembly_refusals([grown], song_seconds=10.0))


def test_a_legacy_approval_gets_the_reapprove_wording_not_the_stale_wording():
    """`approved_duration == 0` is unrepresentable as a real window (`duration` is gt=0),
    so it means the approval predates snapshots and staleness is undecidable."""
    legacy = clip("a", 0, 10.0, approved_start=0, approved_duration=0)
    report = assembly_refusals([legacy], song_seconds=10.0)
    assert report == [ASSEMBLY_LEGACY_APPROVAL_REFUSAL.format(shot="SHOT (a)")]


def test_a_missing_take_file_is_refused_with_the_recorded_path():
    gone = clip("a", 0, 10.0, source=None)
    report = assembly_refusals([gone], song_seconds=10.0)
    assert report == [
        ASSEMBLY_TAKE_MISSING_REFUSAL.format(shot="SHOT (a)", path="shots/a.mp4")
    ]


def test_gaps_are_reported_at_the_start_between_shots_and_at_the_end():
    clips = [clip("a", 1.0, 3.0), clip("b", 5.0, 3.0)]
    report = assembly_refusals(clips, song_seconds=10.0)
    assert report == [
        ASSEMBLY_GAP_REFUSAL.format(
            start=0.0, end=1.0, before=SONG_START_LABEL, after="SHOT (a)"
        ),
        ASSEMBLY_GAP_REFUSAL.format(
            start=4.0, end=5.0, before="SHOT (a)", after="SHOT (b)"
        ),
        ASSEMBLY_GAP_REFUSAL.format(
            start=8.0, end=10.0, before="SHOT (b)", after=SONG_END_LABEL
        ),
    ]


def test_overlaps_are_an_editing_gesture_and_later_shots_win():
    """The Director's ruling (2026-08-20): "later shots on top of earlier shots". An
    overlapping plan passes the refusals, and the plan cuts the earlier clip at the later
    one's start; a clip completely covered by its successor contributes nothing."""
    overlapping = [clip("a", 0, 6.0), clip("b", 5.0, 5.0)]
    refusals = assembly_refusals(overlapping, song_seconds=10.0)
    assert not any("overlap" in line for line in refusals)
    assert refusals == []
    plan = assembly_plan(
        overlapping, song_seconds=10.0,
        dimensions={"a": (1056, 608), "b": (1056, 608)},
    )
    assert [round(c.duration, 3) for c in plan.clips] == [5.0, 5.0]
    assert plan.total_frames == 240  # exactly the song, telescoped at the cut
    covered = [clip("a", 0, 4.0), clip("b", 4.0, 6.0), clip("c", 4.0, 6.0)]
    plan = assembly_plan(
        covered, song_seconds=10.0,
        dimensions={"a": (1056, 608), "b": (1056, 608), "c": (1056, 608)},
    )
    # "b" is fully under "c" (same start; sorted order puts one first) and drops out.
    assert len(plan.clips) == 2
    assert plan.total_frames == 240


def test_overruns_are_refused_by_name():
    past_the_end = [clip("a", 0, 11.0)]
    assert ASSEMBLY_OVERRUN_REFUSAL.format(
        shot="SHOT (a)", end=11.0, song=10.0
    ) in assembly_refusals(past_the_end, song_seconds=10.0)


def test_the_boundary_tolerance_is_half_a_frame_and_coverage_is_one_frame():
    """The exact edges: a boundary off by half a frame touches; anything past it is a gap
    or an overlap. Coverage of the song gets FR-22's one-frame bound at either end."""
    half_frame = 1 / (2 * ASSEMBLY_FPS)
    touching = [clip("a", 0, 5.0), clip("b", 5.0 + half_frame, 5.0 - half_frame)]
    assert assembly_refusals(touching, song_seconds=10.0) == []

    parted = [clip("a", 0, 5.0), clip("b", 5.0 + half_frame * 2.2, 5.0)]
    assert any("uncovered" in line for line in assembly_refusals(parted, 10.0))

    lapped = [clip("a", 0, 5.0 + half_frame * 2.2), clip("b", 5.0, 5.0)]
    assert assembly_refusals(lapped, 10.0) == []  # later-wins: not a defect any more

    # The discriminating width: 0.75 of a frame. Over half a frame (refused under the
    # ruled tolerance), under a whole frame (a doubled tolerance would wave it through) —
    # this is the case that pins the constant itself, not merely the comparison.
    barely_parted = [clip("a", 0, 5.0), clip("b", 5.0 + half_frame * 1.5, 5.0)]
    assert any("uncovered" in line for line in assembly_refusals(barely_parted, 10.0))
    barely_lapped = [clip("a", 0, 5.0 + half_frame * 1.5), clip("b", 5.0, 5.0)]
    assert assembly_refusals(barely_lapped, 10.0) == []  # later-wins covers this too

    one_frame = 1 / ASSEMBLY_FPS
    shy = [clip("a", 0, 10.0 - one_frame)]
    assert assembly_refusals(shy, song_seconds=10.0) == []
    shyer = [clip("a", 0, 10.0 - one_frame * 2.1)]
    assert any("uncovered" in line for line in assembly_refusals(shyer, 10.0))


def test_a_clip_nested_inside_another_splits_it_and_the_underneath_resumes():
    """Layering, not truncation: a clip wholly inside another cuts a hole in it. The
    underneath plays to the overlay's start, the overlay plays, and the underneath
    RESUMES — with its take offset advanced by the skipped stretch, so the same seconds
    of the take land at the same seconds of the song on both sides of the hole. And no
    phantom "uncovered to the end" report: the cursor is the furthest covered moment."""
    nested = [clip("a", 0, 10.0), clip("b", 2.0, 2.0)]
    assert assembly_refusals(nested, song_seconds=10.0) == []
    plan = assembly_plan(
        nested, song_seconds=10.0, dimensions={"a": (1056, 608), "b": (1056, 608)}
    )
    windows = [(c.shot_id, round(c.start, 3), round(c.end, 3), round(c.offset, 3)) for c in plan.clips]
    assert windows == [("a", 0.0, 2.0, 0.0), ("b", 2.0, 4.0, 0.0), ("a", 4.0, 10.0, 4.0)]
    assert plan.total_frames == 240


def test_a_window_shorter_than_a_frame_is_refused():
    sliver = clip("a", 0, 0.01)
    assert ASSEMBLY_TOO_SHORT_REFUSAL.format(
        shot="SHOT (a)", fps=ASSEMBLY_FPS
    ) in assembly_refusals([sliver], song_seconds=10.0)


def test_the_frame_grid_telescopes_so_rounding_cannot_accumulate():
    """The property the cumulative grid exists for: whatever the individual windows round
    to, a contiguous plan's frames sum to exactly `round(end·24)`. The middle boundary here
    (4.02 s) rounds down while its neighbours don't — per-clip `round(duration·24)` would
    be off by one; the grid is not."""
    boundaries = [0.0, 4.02, 7.99, 12.5, 30.0]
    frames = [clip_frames_on_grid(a, b) for a, b in itertools.pairwise(boundaries)]
    assert sum(frames) == round(30.0 * ASSEMBLY_FPS) == 720
    assert clip_frames_on_grid(0, 3.75) == 90
    assert clip_frames_on_grid(30.0, 33.75) == 90
    assert clip_frames_on_grid(0, 5.0) == 120
    # Rounding, not truncation: 7.99 s is frame 191.76 → 192, so this clip holds 108
    # frames. A truncating grid would call it 109 and drift the plan by a frame — and it
    # would still telescope, which is why the sum property alone cannot catch it.
    assert clip_frames_on_grid(7.99, 12.5) == 108


def test_the_plan_normalizes_to_the_largest_area_take_and_orders_by_start():
    clips = [clip("b", 5.0, 5.0), clip("a", 0, 5.0)]
    plan = assembly_plan(
        clips,
        song_seconds=10.0,
        dimensions={"a": (640, 384), "b": (1056, 608)},
    )
    assert [c.shot_id for c in plan.clips] == ["a", "b"]
    assert (plan.width, plan.height) == (1056, 608)
    assert plan.frames == [120, 120]
    assert plan.total_frames == 240


def test_trim_args_pin_the_normalization_and_the_exact_frame_count():
    """The argv is the render contract: aspect-preserved scale, centered pad, 24 fps,
    square pixels, yuv420p, exact frames, no audio, one re-encode."""
    args = trim_args(Path("in.mp4"), Path("out.mp4"), frames=90, width=1056, height=608)
    assert args == [
        "ffmpeg", "-y", "-v", "error", "-i", "in.mp4",
        "-vf",
        (
            "scale=1056:608:force_original_aspect_ratio=decrease,"
            "pad=1056:608:(ow-iw)/2:(oh-ih)/2,fps=24,setsar=1,format=yuv420p"
        ),
        "-frames:v", "90", "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "out.mp4",
    ]


def test_concat_args_copy_video_and_carry_the_song_as_the_sole_audio():
    """The default: shot audio dropped, master song muxed, no second generation loss on
    video. Pinned byte-for-byte, because "an untouched project sounds exactly as the
    song-only ruling shipped" is a claim about this argv — and the empty-overlay call
    must be the identical bytes, not merely equivalent ones."""
    args = concat_args(Path("list.txt"), Path("song.mp3"), Path("export.mp4"))
    assert args == [
        "ffmpeg", "-y", "-v", "error",
        "-f", "concat", "-safe", "0", "-i", "list.txt",
        "-i", "song.mp3",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-movflags", "+faststart",
        "export.mp4",
    ]
    assert concat_args(Path("list.txt"), Path("song.mp3"), Path("export.mp4"), []) == args


def test_accepted_take_audio_becomes_one_trimmed_delayed_input_per_clip():
    """The mix graph, pinned: each accepted take is an extra input, cut to the same slice
    as its picture (offset in, window long), delayed to its cumulative timeline position,
    and mixed with the song FIRST and `normalize=0` — mixing under the song must never
    duck the song."""
    overlays = [
        AudioOverlay(
            source=Path("takes/a.mp4"),
            offset_seconds=0.25,
            window_seconds=3.75,
            delay_seconds=0.0,
        ),
        AudioOverlay(
            source=Path("takes/b.mp4"),
            offset_seconds=0.0,
            window_seconds=4.25,
            delay_seconds=3.75,
        ),
    ]
    args = concat_args(Path("list.txt"), Path("song.mp3"), Path("export.mp4"), overlays)
    assert args[args.index("-filter_complex") + 1] == (
        "[2:a]atrim=start=0.25:end=4.0,asetpts=PTS-STARTPTS,adelay=0:all=1[take0];"
        "[3:a]atrim=start=0.0:end=4.25,asetpts=PTS-STARTPTS,adelay=3750:all=1[take1];"
        "[1:a][take0][take1]amix=inputs=3:duration=first:normalize=0[mix]"
    )
    assert args[args.index("-map", args.index("-map") + 1) + 1] == "[mix]"
    assert args.count("-i") == 4
    assert "takes/a.mp4" in args and "takes/b.mp4" in args
    # Video is still the untouched concat copy.
    assert args[args.index("-c:v") + 1] == "copy"


def test_an_acceptance_with_no_audio_stream_is_refused_by_name():
    accepted = clip("a", 0, 10.0)
    accepted.mix_audio = True
    accepted.has_audio = False
    report = assembly_refusals([accepted], song_seconds=10.0)
    assert report == [ASSEMBLY_NO_AUDIO_TO_MIX_REFUSAL.format(shot="SHOT (a)")]

    # With audio present — or unprobed (missing file already reports separately) — the
    # acceptance itself is never a refusal.
    fine = clip("a", 0, 10.0)
    fine.mix_audio = True
    fine.has_audio = True
    assert assembly_refusals([fine], song_seconds=10.0) == []
    unknown = clip("a", 0, 10.0)
    unknown.mix_audio = True
    assert unknown.has_audio is None
    assert assembly_refusals([unknown], song_seconds=10.0) == []


def test_the_concat_manifest_quotes_the_demuxers_way():
    text = concat_manifest([Path("C:/tmp/a.mp4"), Path("C:/tmp/it's.mp4")])
    assert text == "file 'C:/tmp/a.mp4'\nfile 'C:/tmp/it'\\''s.mp4'\n"


def test_probe_argv_shapes():
    assert probe_duration_args(Path("x.mp4"))[-3:] == ["-of", "csv=p=0", "x.mp4"]
    assert "format=duration" in probe_duration_args(Path("x.mp4"))
    assert "stream=width,height:format=duration" in probe_take_args(Path("x.mp4"))
    assert "stream=codec_type" in probe_streams_args(Path("x.mp4"))


def test_the_trim_offset_is_a_frame_exact_start_frame_never_a_seek():
    """The cut point is `trim=start_frame=K` inside the filter chain — decoded, counted,
    exact — not an input `-ss`, whose seek heuristics decide differently per codec. Zero
    offset builds the identical argv it always did (legacy takes, byte-for-byte)."""
    with_offset = trim_args(
        Path("in.mp4"), Path("out.mp4"), frames=90, width=1056, height=608, offset=0.25
    )
    assert "-ss" not in with_offset
    filters = with_offset[with_offset.index("-vf") + 1]
    assert filters.startswith("trim=start_frame=6,setpts=PTS-STARTPTS,scale=")

    plain = trim_args(Path("in.mp4"), Path("out.mp4"), frames=90, width=1056, height=608)
    assert plain == trim_args(
        Path("in.mp4"), Path("out.mp4"), frames=90, width=1056, height=608, offset=0.0
    )
    assert "trim=" not in plain[plain.index("-vf") + 1]


def test_a_short_window_assembles_its_own_seconds_out_of_a_minimum_length_take():
    """A 2.083 s window against the 4.4583 s take the H3 minimum floor now renders for it.

    The Director's 2026-08-20 ruling makes the rendered length and the exposed length two
    different numbers, and this is the end of the chain that has to keep them apart: the take
    holds 107 frames, the timeline gets 50, and the 29-frame lead is where the cut starts. The
    numbers are written out rather than imported — assembly's job is to consume a recorded
    offset, and it must not learn to re-derive one.
    """
    take_seconds = 107 / ASSEMBLY_FPS
    short = clip("b", 3.75, 2.083)
    short.offset = 29 / ASSEMBLY_FPS  # the lead `timeline.over_render_lead` recorded
    short.take_seconds = take_seconds
    plan = assembly_plan(
        [clip("a", 0, 3.75), short, clip("c", 5.833, 4.167)],
        song_seconds=10.0,
        dimensions={"a": (640, 384), "b": (640, 384), "c": (640, 384)},
    )
    # The exposed length, on the cumulative grid — 50 frames, not the take's 107.
    assert plan.frames == [90, 50, 100]
    assert plan.total_frames == 240 == round(10.0 * ASSEMBLY_FPS)
    assert sum(plan.frames[:1]) == round(3.75 * ASSEMBLY_FPS)
    # The cut lands on the take's 29th frame and takes 50 from there, well inside 107 — the
    # remaining 28 frames are the tail half of the invisible buffer.
    args = trim_args(
        Path("in.mp4"),
        Path("out.mp4"),
        frames=plan.frames[1],
        width=640,
        height=384,
        offset=short.offset,
    )
    filters = args[args.index("-vf") + 1]
    assert filters.startswith("trim=start_frame=29,setpts=PTS-STARTPTS,")
    assert args[args.index("-frames:v") + 1] == "50"
    assert 29 + 50 < 107
    # And the plan is assemblable: the offset plus the window fits inside the measured take,
    # which is the refusal that would fire if the floor and the lead ever disagreed.
    assert assembly_refusals([short], song_seconds=2.083 + 3.75) == [
        ASSEMBLY_GAP_REFUSAL.format(
            start=0.0, end=3.75, before=SONG_START_LABEL, after="SHOT (b)"
        )
    ]


def test_an_offset_that_runs_off_either_end_of_the_take_is_refused_with_numbers():
    """The nudge is clamped in the client, but the manifest is writable by clients that do
    not clamp — so the report decides, against the take's measured length."""
    behind = clip("a", 0, 10.0)
    behind.offset = -0.1
    report = assembly_refusals([behind], song_seconds=10.0)
    assert report == [
        ASSEMBLY_OFFSET_NEGATIVE_REFUSAL.format(shot="SHOT (a)", behind=0.1)
    ]

    over = clip("a", 0, 10.0)
    over.offset = 0.5
    over.take_seconds = 10.25
    report = assembly_refusals([over], song_seconds=10.0)
    assert report == [
        ASSEMBLY_OFFSET_OVERRUN_REFUSAL.format(
            shot="SHOT (a)", take=10.25, offset=0.5, duration=10.0, needed=10.5
        )
    ]

    # Fits — the boundary tolerance keeps a half-frame rounding edge from refusing.
    fits = clip("a", 0, 10.0)
    fits.offset = 0.25
    fits.take_seconds = 10.25
    assert assembly_refusals([fits], song_seconds=10.0) == []

    # Unknown take length (file missing reports separately): the overflow is undecidable
    # and must not fabricate a refusal.
    unknown = clip("a", 0, 10.0)
    unknown.offset = 5.0
    assert unknown.take_seconds is None
    assert assembly_refusals([unknown], song_seconds=10.0) == []


def test_verification_reports_duration_drift_and_stream_shape_with_numbers():
    ok = verification_problems(10.0, 10.02, ["video", "audio"])
    assert ok == []

    drifted = verification_problems(10.0, 10.5, ["video", "audio"])
    assert drifted == [EXPORT_DURATION_PROBLEM.format(measured=10.5, song=10.0)]

    silent = verification_problems(10.0, 10.0, ["video"])
    assert silent == [EXPORT_STREAMS_PROBLEM.format(streams="video")]

    empty = verification_problems(10.0, 10.0, [])
    assert empty == [EXPORT_STREAMS_PROBLEM.format(streams="no streams")]


# ------------------------------------------------------------------------------------------
# Export presets (Phase 4.2). The default's argv is pinned twice — once as the call with no
# preset at all, once as the explicit `draft` — because the whole claim of this change is
# that an existing "Assemble" click keeps producing the identical file.
# ------------------------------------------------------------------------------------------


#: What `trim_args` built before presets existed, written out rather than derived. A test that
#: computed this from the preset object would pass for a preset whose values had drifted.
TODAYS_TRIM_ARGV = [
    "ffmpeg", "-y", "-v", "error", "-i", "in.mp4",
    "-vf",
    (
        "scale=1056:608:force_original_aspect_ratio=decrease,"
        "pad=1056:608:(ow-iw)/2:(oh-ih)/2,fps=24,setsar=1,format=yuv420p"
    ),
    "-frames:v", "90", "-an",
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
    "out.mp4",
]

#: What `concat_args` built before presets existed. Note `-movflags +faststart` and libx264:
#: two of the three details this change was asked to take from elsewhere were already here.
TODAYS_CONCAT_ARGV = [
    "ffmpeg", "-y", "-v", "error",
    "-f", "concat", "-safe", "0", "-i", "list.txt",
    "-i", "song.mp3",
    "-map", "0:v:0", "-map", "1:a:0",
    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
    "-shortest", "-movflags", "+faststart",
    "export.mp4",
]


def test_the_default_preset_is_draft_and_draft_is_what_this_application_already_built():
    """The pin. `draft` is not a new configuration — it is a name for the settings that
    shipped, and the default, so the body-less request this route has always taken produces
    the byte-identical command it always produced. Asserted three ways: no preset, the
    explicit default name, and the preset object."""
    assert DEFAULT_EXPORT_PRESET == "draft"
    assert EXPORT_PRESETS[DEFAULT_EXPORT_PRESET] is DRAFT_PRESET

    no_preset = trim_args(Path("in.mp4"), Path("out.mp4"), frames=90, width=1056, height=608)
    assert no_preset == TODAYS_TRIM_ARGV
    assert no_preset == trim_args(
        Path("in.mp4"), Path("out.mp4"), frames=90, width=1056, height=608,
        preset=EXPORT_PRESETS[DEFAULT_EXPORT_PRESET],
    )

    joined = concat_args(Path("list.txt"), Path("song.mp3"), Path("export.mp4"))
    assert joined == TODAYS_CONCAT_ARGV
    assert joined == concat_args(
        Path("list.txt"), Path("song.mp3"), Path("export.mp4"), [], preset=DRAFT_PRESET
    )
    # The one thing `draft` must never do.
    assert not any("loudnorm" in argument for argument in joined)
    assert DRAFT_PRESET.audio_filters == ()


def test_the_master_preset_moves_the_encoder_and_leaves_the_songs_loudness_alone():
    """Delivery quality, and only where delivery quality is decided: the picture at the trim
    stage (the join stream-copies, so a CRF there would do nothing), and on the audio nothing
    but the 48 kHz delivery conform. `-movflags +faststart` is asserted present rather than
    added — it always was.

    **The absence of `loudnorm` is the spec, not an omission** (Director's ruling,
    2026-08-20): the export's audio track *is* the Director's master song, and a delivery
    build must not re-master it.
    """
    trimmed = trim_args(
        Path("in.mp4"), Path("out.mp4"), frames=90, width=1056, height=608,
        preset=MASTER_PRESET,
    )
    assert trimmed[trimmed.index("-c:v") + 1] == "libx264"
    assert trimmed[trimmed.index("-preset") + 1] == "slow"
    assert trimmed[trimmed.index("-crf") + 1] == "16"
    # Only the encoder moved: every other argument is the draft's, in the draft's order.
    assert [
        argument for argument in trimmed if argument not in {"slow", "16"}
    ] == [argument for argument in TODAYS_TRIM_ARGV if argument not in {"veryfast", "18"}]

    joined = concat_args(
        Path("list.txt"), Path("song.mp3"), Path("export.mp4"), preset=MASTER_PRESET
    )
    assert joined[joined.index("-af") + 1] == MASTER_SAMPLE_RATE
    assert MASTER_SAMPLE_RATE == "aresample=48000"
    assert "+faststart" in joined
    assert joined[joined.index("-c:v") + 1] == "copy"
    # The conform is the only insertion; drop it and the draft's argv is back, in order.
    without = [
        argument for argument in joined
        if argument not in {"-af", MASTER_SAMPLE_RATE}
    ]
    assert without == TODAYS_CONCAT_ARGV

    # The ruling, asserted on the preset itself and on every argument it builds, on both
    # audio paths. A loudness filter anywhere here is a defect, whatever its target.
    overlays = [
        AudioOverlay(
            source=Path("take.mp4"), offset_seconds=0.25, window_seconds=3.75,
            delay_seconds=0.0,
        ),
    ]
    mixed = concat_args(
        Path("list.txt"), Path("song.mp3"), Path("export.mp4"), overlays,
        preset=MASTER_PRESET,
    )
    assert not any("loudnorm" in filter_ for filter_ in MASTER_PRESET.audio_filters)
    assert MASTER_PRESET.audio_filters == ("aresample=48000",)
    for command in (joined, mixed):
        assert not any("loudnorm" in argument for argument in command)
        assert not any("dynaudnorm" in argument for argument in command)
        assert not any(argument.startswith("volume=") for argument in command)


def test_the_masters_delivery_conform_rides_the_end_of_the_mix_graph():
    """With accepted take audio the export has no `-af` to hang a filter on, and hanging it on
    one contributor would conform that contributor rather than the programme. It goes after
    `amix`, last — and `normalize=0` still stands in front of it, so the song is not ducked.
    `draft` puts nothing there at all."""
    overlays = [
        AudioOverlay(
            source=Path("take.mp4"), offset_seconds=0.25, window_seconds=3.75,
            delay_seconds=0.0,
        ),
    ]
    graph_master = concat_args(
        Path("list.txt"), Path("song.mp3"), Path("export.mp4"), overlays,
        preset=MASTER_PRESET,
    )
    filters = graph_master[graph_master.index("-filter_complex") + 1]
    assert filters.endswith(
        f"amix=inputs=2:duration=first:normalize=0,{MASTER_SAMPLE_RATE}[mix]"
    )
    assert "-af" not in graph_master

    graph_draft = concat_args(
        Path("list.txt"), Path("song.mp3"), Path("export.mp4"), overlays
    )
    assert graph_draft[graph_draft.index("-filter_complex") + 1].endswith(
        "amix=inputs=2:duration=first:normalize=0[mix]"
    )
    assert "loudnorm" not in graph_draft[graph_draft.index("-filter_complex") + 1]


def test_a_preset_with_no_audio_filters_builds_a_clean_command_on_both_paths():
    """The plumbing, pinned independently of which presets happen to exist today.

    An empty filter tuple must produce *no* `-af` flag — not `-af ""`, which ffmpeg reads as
    an empty filter description and refuses — and no trailing `,` on the mix graph, which
    would leave a dangling separator before `[mix]` and fail to parse. Both are the shapes a
    naive "always append the chain" would build, and both are only reachable through a
    filterless preset, which is exactly what `draft` is.
    """
    silent = ExportPreset(name="silent", x264_preset="medium", crf="20")
    assert silent.audio_filters == ()
    overlays = [
        AudioOverlay(
            source=Path("take.mp4"), offset_seconds=0.25, window_seconds=3.75,
            delay_seconds=0.0,
        ),
    ]
    for preset in (DRAFT_PRESET, silent):
        song_only = concat_args(
            Path("list.txt"), Path("song.mp3"), Path("export.mp4"), preset=preset
        )
        assert "-af" not in song_only
        assert "" not in song_only
        assert song_only == TODAYS_CONCAT_ARGV

        mixed = concat_args(
            Path("list.txt"), Path("song.mp3"), Path("export.mp4"), overlays,
            preset=preset,
        )
        assert "-af" not in mixed
        graph = mixed[mixed.index("-filter_complex") + 1]
        assert graph.endswith("amix=inputs=2:duration=first:normalize=0[mix]")
        assert ",[mix]" not in graph
        assert ",," not in graph
        # And the same graph the master builds, minus exactly the conform.
        with_conform = concat_args(
            Path("list.txt"), Path("song.mp3"), Path("export.mp4"), overlays,
            preset=MASTER_PRESET,
        )
        assert graph == with_conform[with_conform.index("-filter_complex") + 1].replace(
            f",{MASTER_SAMPLE_RATE}", ""
        )


# ------------------------------------------------------------------------------------------
# Progress reporting.
# ------------------------------------------------------------------------------------------


def test_with_progress_adds_only_the_two_reporting_flags_and_only_at_the_front():
    """The flags are global options, so they must precede the inputs; and they must be the
    *only* difference, because the argv they wrap is the pinned description of what this
    application encodes. Neither writes to the output file."""
    plain = concat_args(Path("list.txt"), Path("song.mp3"), Path("export.mp4"))
    reporting = with_progress(plain)

    assert reporting[:4] == ["ffmpeg", "-progress", "pipe:1", "-nostats"]
    assert reporting[4:] == plain[1:]
    assert len(reporting) == len(plain) + 3


def test_a_progress_line_yields_microseconds_and_every_other_line_is_ignored():
    """`out_time_us` is the one key with a usable clock in it. Everything else in ffmpeg's
    block, the `N/A` this key itself carries before the first frame, and a fragment a pipe
    handed over mid-word must all read as "nothing to report" rather than raise: a progress
    reader that can kill an export is worse than an export with no progress reader."""
    assert parse_progress_us("out_time_us=7916667\n") == 7916667
    assert parse_progress_us("out_time_us=0") == 0
    assert parse_progress_us("  out_time_us=1500000  ") == 1500000

    for ignored in [
        "out_time_us=N/A",
        "out_time_ms=7916667",
        "out_time=00:00:07.916667",
        "frame=190",
        "speed= 277x",
        "progress=end",
        "out_time_us",
        "out_time_us=",
        "out_time_us=-5",
        "out_time_us=12.5",
        "",
        "   ",
        "=7916667",
    ]:
        assert parse_progress_us(ignored) is None, ignored


def test_export_progress_is_monotonic_capped_and_split_between_the_two_stages():
    """The trims cover the timeline once and the join covers it again, so a raw reading walks
    backwards several times per export; this reports a number that only ever rises. 100 is a
    ceiling, not an arithmetic outcome — a song measured a hair shorter than the frames laid
    against it must not report 101."""
    progress = ExportProgress(total_seconds=8.0)

    # First trim, four seconds of an eight-second timeline: half the trim share.
    assert progress.trim(0.0, 2_000_000) == int(TRIM_SHARE * 25)
    assert progress.trim(0.0, 4_000_000) == int(TRIM_SHARE * 50)
    # Second trim restarts ffmpeg's clock at zero; placed after four seconds of finished
    # clips, it may not walk the bar back to 0.
    assert progress.trim(4.0, 0) == int(TRIM_SHARE * 50)
    assert progress.trim(4.0, 4_000_000) == int(TRIM_SHARE * 100)
    # The join's clock already runs against the whole export, and owns the last share.
    assert progress.join(0) == int(TRIM_SHARE * 100)
    assert progress.join(4_000_000) == 95
    assert progress.join(8_000_000) == 100
    # Over-run in either stage is clamped, never reported past 100, never walked back.
    assert progress.join(99_000_000) == 100
    assert progress.trim(0.0, 0) == 100

    overshooting = ExportProgress(total_seconds=8.0)
    assert overshooting.trim(0.0, 99_000_000) == int(TRIM_SHARE * 100)

    # A plan with no measured length divides by nothing and reports the stage floor.
    lengthless = ExportProgress(total_seconds=0.0)
    assert lengthless.trim(0.0, 4_000_000) == 0
    assert lengthless.percent == 0


def test_every_named_preset_is_reachable_by_name():
    """The dict is what the route indexes into; a preset defined and not registered would be
    a delivery build nothing could ask for."""
    assert set(EXPORT_PRESETS) == {"draft", "master"}
    assert EXPORT_PRESETS["master"] is MASTER_PRESET
    assert all(name == preset.name for name, preset in EXPORT_PRESETS.items())
