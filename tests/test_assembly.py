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
    TRANSITION_CROWDED_REFUSAL,
    TRANSITION_NESTED_REFUSAL,
    TRIM_SHARE,
    AudioOverlay,
    ClipWindow,
    ExportPreset,
    ExportProgress,
    TransitionChoice,
    TransitionClip,
    assembly_plan,
    assembly_refusals,
    clip_frames_on_grid,
    concat_args,
    concat_manifest,
    parse_progress_us,
    probe_duration_args,
    probe_streams_args,
    probe_take_args,
    transition_segment_args,
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


# ------------------------------------------------------------------------------------------
# Transitions (story 11.1, AD-18/AD-19/R-37/R-38/R-39). The plan half: what `assembly_plan`
# emits at an Overlap, and the argv the segment runs. The *rendered* half is in
# `test_assembly_route.py`, and it has to be there — the failure this slice is built around is
# a short render at rc 0, which no pinned argv can see.
# ------------------------------------------------------------------------------------------

DISSOLVE = TransitionChoice("dissolve", "fade")


def transitioning(*clips: ClipWindow) -> dict[str, tuple[int, int]]:
    """Every clip at one geometry, so a plan test says nothing about normalization."""
    return {item.shot_id: (128, 72) for item in clips}


def plan_of(clips, song_seconds, transitions=None):
    return assembly_plan(clips, song_seconds, transitioning(*clips), transitions)


def test_an_overlap_with_a_transition_emits_three_entries_and_the_middle_is_the_overlaps_frames():
    """AD-18's Rule, and R-39's correction to how it reads.

    **`assembly_plan` emits two entries at an Overlap today, not three** — the resolution loop
    subtracts each later clip's window from the earlier one, so the earlier clip is truncated and
    its overlap footage is discarded. Both halves are asserted here, in one test, because the
    third entry is a change to *what this function emits* and the two-entry answer is what it
    replaces.

    The middle entry is exactly `clip_frames_on_grid(overlap_start, overlap_end)` — the criterion
    Story 11.1 states — and the grid is untouched, which is the other half of R-39: every boundary
    is still some clip's own start (`b.start`) or its own end (`a.end`).
    """
    a = clip("a", 0.0, 4.0)
    b = clip("b", 3.5, 4.5)

    without = plan_of([a, b], 8.0)
    assert [type(entry).__name__ for entry in without.clips] == ["ClipWindow", "ClipWindow"]
    assert [(entry.start, entry.end) for entry in without.clips] == [(0.0, 3.5), (3.5, 8.0)]

    with_blend = plan_of([a, b], 8.0, {"a": DISSOLVE})
    assert [type(entry).__name__ for entry in with_blend.clips] == [
        "ClipWindow",
        "TransitionClip",
        "ClipWindow",
    ]
    assert [(entry.start, entry.end) for entry in with_blend.clips] == [
        (0.0, 3.5),
        (3.5, 4.0),
        (4.0, 8.0),
    ]
    assert with_blend.frames[1] == clip_frames_on_grid(3.5, 4.0)
    # The same total, from the same telescoping sequence of boundaries. This is FX-NFR-1 in its
    # smallest form: the third entry costs the plan nothing and takes nothing from its neighbours.
    assert sum(with_blend.frames) == sum(without.frames) == round(8.0 * ASSEMBLY_FPS)
    assert with_blend.transition_refusals == []


def test_both_legs_of_a_transition_read_their_own_takes_own_overlap_seconds():
    """R-38: the segment reads the two takes, and the frames are provably there.

    `before` is the outgoing Shot's **tail** — the frames `assembly_plan` truncates away, which
    are in no intermediate at all — and `after` is the incoming Shot's **head**, which is the
    frames the third entry no longer carries. Each leg's take offset advances by exactly the
    seconds skipped, which is the rule the resolution loop applies to every split it makes.

    No margin is borrowed (AD-19): the outgoing leg's offset plus its duration is inside the Shot's
    own window, which `assembly_refusals` has already proved against the take.
    """
    a = clip("a", 10.0, 4.0)
    b = clip("b", 13.5, 4.5)
    entry = plan_of([a, b], 18.0, {"a": DISSOLVE}).clips[1]

    assert entry.before.shot_id == "a"
    assert (entry.before.start, entry.before.duration) == (13.5, 0.5)
    #: The outgoing take is read 3.5 s in — the Overlap's start minus the Shot's own start — and
    #: that is inside `a`'s own 4.0 s window, so nothing reaches past what `assembly_refusals`
    #: proved the take holds.
    assert entry.before.offset == 3.5
    assert entry.before.offset + entry.before.duration <= a.duration

    assert entry.after.shot_id == "b"
    assert (entry.after.start, entry.after.duration) == (13.5, 0.5)
    assert entry.after.offset == 0.0
    # And the third entry picks up exactly where the blend stopped.
    assert (entry.end, plan_of([a, b], 18.0, {"a": DISSOLVE}).clips[2].offset) == (14.0, 0.5)


def test_an_overlap_with_no_transition_resolves_exactly_as_it_does_today():
    """FX-16's third criterion: an Overlap nobody typed a transition on is a hard cut, later Shot
    on top, byte for byte the plan this function has always emitted. Asserted against the same
    call with an empty mapping *and* with none at all, because the two are different arguments."""
    clips = [clip("a", 0.0, 4.0), clip("b", 3.5, 4.5)]
    shape = [(type(e).__name__, e.start, e.end) for e in plan_of(clips, 8.0).clips]
    for transitions in ({}, {"somebody-else": DISSOLVE}):
        other = plan_of(clips, 8.0, transitions)
        assert [(type(e).__name__, e.start, e.end) for e in other.clips] == shape
        assert other.transition_refusals == []


def test_a_transition_on_a_boundary_with_no_overlap_composes_nothing_and_refuses_nothing():
    """A stored type where the two Shots merely touch is a **one-sided** transition (AD-19,
    FX-16) — a treatment of this clip's own frames, story 11.4's, and not built here.

    It must compose nothing, and it must also *refuse* nothing: a sentence here would report a
    defect where a Director has simply not dragged the clips across each other yet. This is the
    sixth FX-NFR-1 case's own half — a one-sided transition beside a paired one is covered by
    `test_the_frame_rule_holds_over_every_fx_nfr_1_case`.
    """
    clips = [clip("a", 0.0, 4.0), clip("b", 4.0, 4.0)]
    plan = plan_of(clips, 8.0, {"a": DISSOLVE})
    assert [type(entry).__name__ for entry in plan.clips] == ["ClipWindow", "ClipWindow"]
    assert plan.transition_refusals == []
    assert sum(plan.frames) == round(8.0 * ASSEMBLY_FPS)


def test_more_than_two_clips_over_one_instant_refuses_the_transition_and_still_assembles():
    """R-37, shown whole and asserted verbatim.

    Three consecutive Shots each dragged over the next until the two Overlaps touch: `c` starts
    inside the Overlap `a` and `b` make, so three windows cover one instant. There is no pair of
    legs to blend, and the boundary stays the hard cut it already is — **the export is not
    refused**, because refusing it would be stricter than `assembly_plan` itself and would cost a
    Director a render over one geometry.

    The plan still assembles and the frame rule still holds, which is the half of R-37 that makes
    it a refusal of the transition rather than of the export.
    """
    a = clip("a", 0.0, 4.0)
    b = clip("b", 3.0, 4.0)
    c = clip("c", 3.5, 4.5)
    plan = plan_of([a, b, c], 8.0, {"a": DISSOLVE})

    assert plan.transition_refusals == [
        TRANSITION_CROWDED_REFUSAL.format(
            before=a.label, after=b.label, start=3.0, end=4.0, count=3
        )
    ]
    assert not any(isinstance(entry, TransitionClip) for entry in plan.clips)
    assert sum(plan.frames) == round(8.0 * ASSEMBLY_FPS)


def test_a_shot_swallowed_whole_refuses_the_transition_by_the_same_sentence():
    """The nested geometry `timeline.SNAP_NESTED` refuses on the timeline, met from the other side.

    An Overlap reaching past the incoming Shot's own end leaves no remainder, so there is no third
    entry and the "transition" would be the entire clip. It is counted by the crowding sentence
    because it *is* crowded: `assembly_plan` splits the underneath clip around the overlay and
    resumes it afterwards, so three visible ranges exist wherever one window is inside another.
    """
    a = clip("a", 0.0, 8.0)
    b = clip("b", 2.0, 2.0)
    plan = plan_of([a, b], 8.0, {"a": DISSOLVE})
    assert plan.transition_refusals == [
        TRANSITION_NESTED_REFUSAL.format(
            before=a.label, after=b.label, start=0.0, end=8.0, inner_start=2.0, inner_end=4.0
        )
    ]
    assert not any(isinstance(entry, TransitionClip) for entry in plan.clips)
    # **The frames this refusal exists for.** Without the nested branch the third entry ran from
    # the Overlap's end back to the incoming Shot's own earlier end and contributed **-96** frames
    # to the grid sum -- a plan that added up to the song by cancelling a window against itself.
    assert all(count > 0 for count in plan.frames)
    assert sum(plan.frames) == round(8.0 * ASSEMBLY_FPS)


def test_the_nested_refusal_says_what_the_snapper_says():
    """One wording for one geometry, held across a boundary an import cannot cross.

    R-37 asks the transition's nested refusal to reuse `timeline.SNAP_NESTED`'s wording, and
    `assembly.py` **may not import `timeline`** -- it is an AD-25 leaf and
    `tests/test_module_boundaries.py` enforces that as a guard rather than a discipline. A test
    file may import both, so this is where the two sentences are held identical: the statement of
    the geometry and the remedy are `SNAP_NESTED`'s own, character for character.

    The middle clause is deliberately not shared, and that is a finding rather than an exception.
    `SNAP_NESTED` says *"there is no single point here to place"*, which is about placing a cut; a
    Director who asked for a blend would be reading a true remedy with a reason that is not about
    what they did.
    """
    from music_video_producer.timeline import SNAP_NESTED

    opening = (
        "{after} sits entirely inside {before} — {before} runs {start:.3f}s to {end:.3f}s "
        "and {after}'s {inner_start:.3f}s to {inner_end:.3f}s is within it."
    )
    remedy = "Move one of them out from under the other"
    for sentence in (SNAP_NESTED, TRANSITION_NESTED_REFUSAL):
        assert sentence.startswith(opening), sentence
        assert remedy in sentence, sentence


#: The six cases FX-NFR-1 names, as data, so the frame rule is asserted over all of them by one
#: loop rather than six near-identical tests — and so a seventh can be added without a new test.
#:
#: Each is `(name, clips, song_seconds, transitions, expected transition count)`. The windows are
#: deliberately not round numbers where an Overlap is involved: a boundary that lands exactly on a
#: frame is the case where the grid cannot be got wrong, and it is not the case a Director makes
#: by dragging.
FX_NFR_1_CASES = (
    ("no overlap", [("a", 0.0, 4.0), ("b", 4.0, 4.0)], 8.0, {}, 0),
    ("one overlap", [("a", 0.0, 4.13), ("b", 3.57, 4.43)], 8.0, {"a": DISSOLVE}, 1),
    (
        "adjacent overlaps",
        [("a", 0.0, 4.13), ("b", 3.57, 4.11), ("c", 7.31, 4.69)],
        12.0,
        {"a": DISSOLVE, "b": DISSOLVE},
        2,
    ),
    (
        "an overlap at the song's start",
        [("a", 0.0, 1.37), ("b", 0.91, 7.09)],
        8.0,
        {"a": DISSOLVE},
        1,
    ),
    (
        "an overlap at the song's end",
        [("a", 0.0, 7.23), ("b", 6.71, 1.29)],
        8.0,
        {"a": DISSOLVE},
        1,
    ),
    (
        "a one-sided transition beside a paired one",
        [("a", 0.0, 4.0), ("b", 4.0, 4.19), ("c", 7.63, 4.37)],
        12.0,
        {"a": DISSOLVE, "b": DISSOLVE},
        1,
    ),
)


def test_the_frame_rule_holds_over_every_fx_nfr_1_case():
    """**FX-NFR-1, over all six cases: `sum(plan.frames) == round(plan_end * ASSEMBLY_FPS)`.**

    The one rule this project may never break, and the reason this slice merges alone. It holds
    structurally rather than arithmetically: a transition entry is a split of a boundary sequence
    that already telescoped, so nothing here depends on the *sizes* being right — only on every
    boundary still being some clip's own start or end.

    The plan's end is the last entry's end and not the song's, because a plan short of the song by
    up to a frame is accepted (`COVERAGE_TOLERANCE_SECONDS`) and this is a claim about the frames
    the plan lays, not about coverage.
    """
    for name, windows, song_seconds, transitions, expected in FX_NFR_1_CASES:
        clips = [clip(shot_id, start, duration) for shot_id, start, duration in windows]
        plan = plan_of(clips, song_seconds, transitions)
        blends = [entry for entry in plan.clips if isinstance(entry, TransitionClip)]
        assert len(blends) == expected, name
        assert plan.transition_refusals == [], name
        plan_end = max(entry.end for entry in plan.clips)
        assert sum(plan.frames) == round(plan_end * ASSEMBLY_FPS), name
        # Every entry's own frames are the grid's answer for its own boundaries, transition
        # included — which is what makes the sum telescope rather than merely happen to agree.
        assert plan.frames == [
            clip_frames_on_grid(entry.start, entry.end) for entry in plan.clips
        ], name
        for blend in blends:
            assert blend.before.duration == blend.after.duration == blend.duration, name


def test_the_frame_rule_survives_a_sweep_of_overlaps_the_grid_cannot_represent():
    """The property, swept, because six named cases are six samples of an arithmetic claim.

    Overlaps stepped by a third of a millisecond across a whole frame, so the pair of boundaries
    lands on every rounding relationship the 24 fps grid has: both up, both down, and each way
    round. `sum(frames)` is `round(end * 24)` at every one of them, and the transition's own frame
    count is the grid's answer for the Overlap's own two edges — which is the sentence Story 11.1
    puts the criterion in.
    """
    # Started above `BOUNDARY_TOLERANCE_SECONDS` -- half a frame -- because below it there is no
    # Overlap at all: that is where "the same boundary, written twice" ends, and it ends there for
    # `tiling_refusals` and `_seam_overlaps` too. The first run of this sweep started at 1 ms and
    # failed on its own first step, which is the tolerance being the tolerance.
    for step in range(125):
        overlap = 0.025 + step / 3000
        a = clip("a", 0.0, 4.0)
        b = clip("b", round(4.0 - overlap, 6), 4.0 + overlap)
        plan = plan_of([a, b], 8.0, {"a": DISSOLVE})
        blend = plan.clips[1]
        assert isinstance(blend, TransitionClip), overlap
        assert plan.frames[1] == clip_frames_on_grid(b.start, a.end), overlap
        assert sum(plan.frames) == round(8.0 * ASSEMBLY_FPS), overlap


#: The transition segment's argv, pinned exactly as `TODAYS_TRIM_ARGV` pins the trim's. A flag
#: drifting here is a render defect no Python test would otherwise see — and unlike the trim, this
#: one carries a filter graph whose *shape* is the safety property (see the assertions below).
TODAYS_TRANSITION_ARGV = [
    "ffmpeg",
    "-y",
    "-v",
    "error",
    "-i",
    "before.mp4",
    "-i",
    "after.mp4",
    "-filter_complex",
    (
        "[0:v]trim=start_frame=84:end_frame=96,setpts=PTS-STARTPTS,"
        "scale=128:72:force_original_aspect_ratio=decrease,"
        "pad=128:72:(ow-iw)/2:(oh-ih)/2,fps=24,setsar=1,format=yuv420p[xfa];"
        "[1:v]trim=start_frame=0:end_frame=12,setpts=PTS-STARTPTS,"
        "scale=128:72:force_original_aspect_ratio=decrease,"
        "pad=128:72:(ow-iw)/2:(oh-ih)/2,fps=24,setsar=1,format=yuv420p[xfb];"
        "[xfa][xfb]xfade=transition=fade:duration=0.500000:offset=0,setsar=1,format=yuv420p"
    ),
    "-frames:v",
    "12",
    "-an",
    "-c:v",
    "libx264",
    "-preset",
    "veryfast",
    "-crf",
    "18",
    "segment.mp4",
]


def test_the_transition_segment_argv_is_pinned_and_both_legs_close_themselves():
    """The argv, whole — and the three properties that keep the frame rule inside it.

    **`end_frame` on both legs is the safety, not the `-frames:v`.** Measured 2026-08-28 on
    ffmpeg 7.0: `xfade` with legs of unequal length **silently truncates to the shorter one** —
    thirteen-frame and twelve-frame legs give twelve frames out, rc 0, nothing at `-v warning` —
    and a frame cap caps from above only, so `-frames:v 13` does not see it. Both legs therefore
    close themselves, at the same count, by construction.

    **The tail after `xfade` is not decoration.** Without it the segment encodes `yuv444p`,
    profile High 4:4:4 Predictive, while every other intermediate is `yuv420p` / High — measured,
    at rc 0, with the correct frame count — and the join is `-c:v copy` (FX-NFR-2).

    **`offset=0` and `duration` is the whole segment** (AD-19): a paired transition's length *is*
    the Overlap's, so there is no second source for that number and no stored duration field.
    """
    assert (
        transition_segment_args(
            Path("before.mp4"),
            Path("after.mp4"),
            Path("segment.mp4"),
            12,
            128,
            72,
            "fade",
            before_offset=3.5,
        )
        == TODAYS_TRANSITION_ARGV
    )
    graph = TODAYS_TRANSITION_ARGV[TODAYS_TRANSITION_ARGV.index("-filter_complex") + 1]
    assert graph.count("end_frame=") == 2, "a leg that does not close itself can be truncated"
    assert graph.endswith(",setsar=1,format=yuv420p"), "the xfade's own output must be pinned"
    assert ",fps=" not in graph.split("xfade=")[1], (
        "a rate filter downstream of a framesync filter is what BRANCH_FRAME_GUARD compensates for"
    )



def test_the_boundary_previews_margins_are_absent_from_the_exports_own_argv():
    """Story 11.5's first constraint, from the side that matters most: the export's argv did not
    move.

    `lead_frames` and `tail_frames` default to nothing, so the segment this function builds for an
    export is the argv it built at `3322ace` — including `offset=0` spelled `0` and not
    `0.000000`, which is what every export argv this application has written carries. A preview is
    not entitled to move the export's own bytes to make its own arithmetic tidier.
    """
    assert transition_segment_args(
        Path("before.mp4"),
        Path("after.mp4"),
        Path("segment.mp4"),
        12,
        128,
        72,
        "fade",
        before_offset=3.5,
        lead_frames=0,
        tail_frames=0,
    ) == TODAYS_TRANSITION_ARGV


def test_the_preview_margins_extend_each_leg_on_its_own_side_and_move_the_blend_not_its_length():
    """FX-21 as arithmetic: the clip spans the boundary, and the blend inside it is unchanged.

    Three things are asserted together because they are one property. The **outgoing** leg gains
    its lead *before* the blend and still ends where the blend ends; the **incoming** leg gains its
    tail *after* it and still starts where the blend starts; and `-frames:v` becomes the whole
    window, because a cap that still said `12` would cut the clip back to the blend and the
    Director would be looking at exactly what they were looking at before.
    """
    argv = transition_segment_args(
        Path("before.mp4"),
        Path("after.mp4"),
        Path("segment.mp4"),
        12,
        128,
        72,
        "fade",
        before_offset=3.5,
        after_offset=0.0,
        lead_frames=12,
        tail_frames=6,
    )
    graph = argv[argv.index("-filter_complex") + 1]
    lead_leg, follow_leg = graph.split(";")[0], graph.split(";")[1]
    # 3.5 s at 24 fps is take frame 84; the lead reaches back twelve frames and the blend still
    # ends at 84 + 12.
    assert "trim=start_frame=72:end_frame=96" in lead_leg
    assert "trim=start_frame=0:end_frame=18" in follow_leg
    assert argv[argv.index("-frames:v") + 1] == "30"
    assert "xfade=transition=fade:duration=0.500000:offset=0.500000" in graph


def test_the_preview_and_the_export_write_one_xfade_by_name_and_by_duration():
    """FX-NFR-3, proved by string on the two composed graphs rather than by reading two builders.

    This is the measurement story 11.5's first constraint asks for. The preview's graph and the
    export's graph for the **same boundary** are built from the same call with different margins,
    and the clause `xfade_stage` writes — the transition's name and its duration — is character for
    character the same in both. Only the offset differs, which is where the blend sits in the
    window rather than what the blend is.
    """
    common = (Path("a.mp4"), Path("b.mp4"), Path("o.mp4"), 12, 128, 72, "fadeblack")
    export = transition_segment_args(*common, before_offset=3.5)
    preview = transition_segment_args(
        *common, before_offset=3.5, lead_frames=12, tail_frames=12
    )
    clause = lambda argv: (
        argv[argv.index("-filter_complex") + 1].split("xfade=")[1].split(":offset=")[0]
    )
    assert clause(export) == clause(preview) == "transition=fadeblack:duration=0.500000"
    # And they really are two different clips, so the comparison is not of one graph with itself.
    assert export != preview

def test_a_transition_leg_is_normalized_by_the_same_builder_the_trim_uses():
    """FX-NFR-2 structurally: the two argv builders share `normalized_stages`, so a stage added to
    one is added to the other, and a segment cannot drift out of concat-identity with its
    neighbours by a change nobody made to it.

    Asserted by comparison rather than by reading the source: a leg's stages, from `scale` onward,
    are character for character the trim's.
    """
    trim = trim_args(Path("t.mp4"), Path("o.mp4"), 12, 128, 72)
    tail = trim[trim.index("-vf") + 1].split("scale=", 1)[1]
    segment = transition_segment_args(
        Path("a.mp4"), Path("b.mp4"), Path("o.mp4"), 12, 128, 72, "fade"
    )
    graph = segment[segment.index("-filter_complex") + 1]
    for leg in graph.split(";")[:2]:
        assert leg.split("scale=", 1)[1].removesuffix("[xfa]").removesuffix("[xfb]") == tail


def test_every_catalogued_transition_reaches_the_argv_as_its_own_xfade_name():
    """FX-18: a named type is never quietly substituted. Twelve types, twelve distinct `xfade`
    names in the graph, and `hblur` under the name R-34 gave it."""
    from music_video_producer.effects import TRANSITION_CATALOGUE

    assert len(TRANSITION_CATALOGUE) == 12
    assert TRANSITION_CATALOGUE["blur_wipe"].label == "Blur wipe"
    assert TRANSITION_CATALOGUE["blur_wipe"].xfade == "hblur"
    assert sum(1 for e in TRANSITION_CATALOGUE.values() if e.pair_only) == 8
    seen = set()
    for entry in TRANSITION_CATALOGUE.values():
        argv = transition_segment_args(
            Path("a.mp4"), Path("b.mp4"), Path("o.mp4"), 12, 128, 72, entry.xfade
        )
        graph = argv[argv.index("-filter_complex") + 1]
        assert f"xfade=transition={entry.xfade}:" in graph
        seen.add(entry.xfade)
    assert len(seen) == 12, "two catalogue entries resolve to one xfade name"
