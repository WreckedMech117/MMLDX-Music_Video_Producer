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
    EXPORT_DURATION_PROBLEM,
    EXPORT_STREAMS_PROBLEM,
    SONG_END_LABEL,
    SONG_START_LABEL,
    AudioOverlay,
    ClipWindow,
    assembly_plan,
    assembly_refusals,
    clip_frames_on_grid,
    concat_args,
    concat_manifest,
    probe_duration_args,
    probe_streams_args,
    probe_take_args,
    trim_args,
    verification_problems,
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
